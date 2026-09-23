"""Bounded, registered public-page readers for M3; never a general web crawler.

Only URLs explicitly reviewed in the local registry are requested. No JavaScript,
attachments, search-result snippets, login, provider SDK or backend endpoints are used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from uuid import uuid4

from .contracts import SourceRegistration, SHANGHAI, aware_time
from .evidence import build_evidence
from .discovery import discover_links, validate_discovery
from .associations import link_subjects

USER_AGENT = "ashare-daily-research/0.4 (bounded personal research)"
MAX_RESPONSE_BYTES = 2_000_000


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _public_url(value: str, host: str | None = None) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise ValueError("只允许不含凭据、查询或片段的 HTTPS 已登记原始页面")
    if host and parsed.hostname != host:
        raise ValueError("页面主机与登记来源不匹配")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"} or re.fullmatch(r"[0-9.]+", parsed.hostname):
        raise ValueError("不允许本地地址或 IP 页面")
    return value


def load_source_registry(path: str | Path) -> dict:
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise ValueError("来源登记超过大小限制")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if raw.get("schema_version") != "m3-source-registry-v1":
        raise ValueError("来源登记版本不支持")
    if set(raw) - {"schema_version", "sources", "request_limits", "review_notes"}:
        raise ValueError("来源登记含未知字段")
    if not isinstance(raw.get("sources"), list) or len(raw["sources"]) > 20:
        raise ValueError("来源列表缺失或超出小批量范围")
    seen = set()
    for source in raw["sources"]:
        reg = SourceRegistration.model_validate(source["registration"])
        if reg.source_id in seen:
            raise ValueError("重复来源 ID")
        seen.add(reg.source_id)
        source["registration"] = reg.model_dump(mode="json")
        if source.get("adapter") not in {"mofcom_article", "registered_html", "permission_pending"}:
            raise ValueError("未知来源适配器")
        pages = source.get("pages", [])
        if not isinstance(pages, list) or len(pages) > 10:
            raise ValueError("每个来源最多十个明确登记页面")
        host = urlsplit(reg.source_url).hostname
        if source.get('discovery') is not None:
            validate_discovery(source['discovery'], host)
        for page in pages:
            _public_url(page["url"], host)
        if reg.enabled and (not reg.checked_at or not reg.cache_allowed or not reg.permission_basis.strip()):
            raise ValueError("启用来源须有审核时间、缓存许可及具体依据")
        if reg.enabled and source["adapter"] == "permission_pending":
            raise ValueError("待许可来源不能启用")
    limits = raw.setdefault("request_limits", {})
    for field, default, low, high in (("timeout_seconds", 15, 1, 30), ("max_attempts", 2, 1, 2),
                                     ("max_requests", 10, 1, 20), ("interval_seconds", 1, 0.1, 5)):
        value = limits.setdefault(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"{field} 超出小批量限制")
    for field in ("max_attempts", "max_requests"):
        if not isinstance(limits[field], int):
            raise ValueError(f"{field} 必须为整数")
    raw["registry_path"] = str(path.resolve())
    raw["registry_hash"] = _hash(path.read_bytes())
    return raw


class _PageParser(HTMLParser):
    """Extract reviewed visible article containers, excluding scripts/navigation."""
    def __init__(self, body_class="art-con", info_class="at-left", title_class="art-title", body_ergodic=False):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.parts = {"body": [], "info": [], "title": []}
        self.stack = []
        self.body_class, self.info_class, self.title_class = body_class, info_class, title_class
        self.body_ergodic = body_ergodic
        self.body_containers = 0
        self.closed_body_containers = 0
        self.attachment_urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("name", "").lower()] = attrs.get("content", "")
        if tag in {"meta", "link", "img", "input", "hr", "br", "source"}:
            if tag == "br":
                self.handle_data("\n")
            return
        classes = attrs.get("class", "").split()
        inherited = self.stack[-1][1] if self.stack else None
        skip = tag in {"script", "style", "iframe", "noscript", "svg"} or bool(self.stack and self.stack[-1][2])
        role = inherited
        is_body = self.body_class in classes and (not self.body_ergodic or attrs.get("ergodic") == "article")
        if is_body:
            role = "body"
            self.body_containers += 1
        elif self.info_class in classes:
            role = "info"
        elif self.title_class in classes:
            role = "title"
        self.stack.append((tag, role, skip, is_body))
        if role and tag in {"p", "div", "section", "li", "tr"} and not skip:
            self.parts[role].append("\n")
        if role == "body" and tag == "a" and attrs.get("href", "").lower().endswith(".pdf"):
            self.attachment_urls.append(attrs["href"])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                _, role, skip, is_body = self.stack[i]
                if is_body:
                    self.closed_body_containers += 1
                if role and not skip and tag in {"p", "div", "section", "li", "tr"}:
                    self.parts[role].append("\n")
                del self.stack[i:]
                return

    def handle_data(self, value):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.parts[self.stack[-1][1]].append(value)

    def text(self, role):
        return "\n".join(re.sub(r"[\s\u3000]+", " ", line).strip()
                         for line in "".join(self.parts[role]).splitlines() if line.strip())


class NewsAdapter:
    category = "news"

    def parse(self, body: bytes, source: dict, page: dict) -> dict:
        return parse_registered_article(body, source, page)


class AnnouncementAdapter(NewsAdapter):
    category = "announcement"


class ResearchReportAdapter(NewsAdapter):
    """Same registered-page contract, with metadata/abstract/fulltext kept distinct.

    No research-report supplier is enabled until its individual permissions and
    parser are registered. A PDF URL alone is never evidence of its contents.
    """
    category = "research_report"


ADAPTERS = {a.category: a() for a in (NewsAdapter, AnnouncementAdapter, ResearchReportAdapter)}


def parse_registered_article(body: bytes, source: dict, page: dict) -> dict:
    text = body.decode("utf-8-sig", errors="strict")
    if any(marker in text[:1500].lower() for marker in ("captcha", "access denied", "访问过于频繁")):
        raise PermissionError("响应为验证/访问限制页")
    parser = _PageParser(**source.get("selectors", {}))
    parser.feed(text)
    title = parser.text("title") or parser.meta.get("articletitle", "")
    info = parser.text("info")
    if not title or (source.get("require_original", False) and "类型：原创" not in info.replace(" ", "")):
        raise ValueError("缺少原始标题或未确认原创使用范围")
    # Visible publication time wins over potentially regenerated metadata time.
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", info)
    if not match:
        raise ValueError("缺少可见发布时间；不以抓取时间或目录年份代替")
    timestamp = match.group(0)
    precision = "date" if len(timestamp) == 10 else "minute" if len(timestamp) == 16 else "datetime"
    published = (datetime.fromisoformat(timestamp).replace(tzinfo=SHANGHAI).isoformat(
        timespec="minutes" if precision == "minute" else "seconds") if precision != "date" else timestamp)
    content_type = page.get("content_type", "fulltext")
    if content_type == "metadata_only":
        content = ""
    else:
        if parser.body_containers != 1 or parser.closed_body_containers != 1:
            raise ValueError("正文容器缺失或重复，不能用 Description/目录摘要冒充正文")
        content = parser.text("body")
        if len(content) < 40 or content == title:
            raise ValueError("未取得足够正文，拒绝标题冒充正文")
    if content_type not in source["registration"]["content_access"]:
        raise PermissionError("内容类型超出登记访问范围")
    max_chars = min(int(page.get("max_content_chars", 6000)), 20000)
    summary_marker = bool(re.search(r"(?:阅读全文|查看全文|点击.{0,4}全文|\.\.\.|……)\s*$", content))
    truncated = len(content) > max_chars or bool(parser.attachment_urls) or summary_marker
    if truncated:
        content_type = "abstract"
        if "abstract" not in source["registration"]["content_access"]:
            raise PermissionError("截断正文的摘要类型未经许可")
    # Retain natural paragraph boundaries when local budget restricts text.
    if len(content) > max_chars:
        content = content[:max_chars].rsplit("\n", 1)[0] or content[:max_chars]
    return {"title": title, "published_at": published, "publication_precision": precision,
            "content": content, "content_type": content_type, "content_truncated": truncated,
            "original_publisher": parser.meta.get("contentsource") or source["registration"]["name"],
            "visible_publication_value": timestamp, "metadata_publication_value": parser.meta.get("pubdate"),
            "publication_time_basis": "visible article metadata; original date/minute/second precision retained",
            "unread_attachments": parser.attachment_urls,
            "summary_marker_detected": summary_marker,
            "content_coverage": "网页正文；附件未获取" if parser.attachment_urls else "已取得登记网页正文"}


@dataclass
class HttpResponse:
    status: int
    body: bytes
    content_type: str = "text/html"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get(url: str, timeout: float) -> HttpResponse:
    # Redirect destinations are never automatically followed outside the registry.
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(MAX_RESPONSE_BYTES + 1),
                                response.headers.get("Content-Type", ""))
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.read(min(MAX_RESPONSE_BYTES + 1, 100_000)),
                            exc.headers.get("Content-Type", ""))


def collect_materials(*, registry_path: str | Path, start: str, cutoff: str,
                      sample_symbols: list[str] | dict, output_dir: str | Path,
                      previous_cutoff: str | None = None, include_background: bool = False,
                      transport: Callable | None = None, sleep: Callable = time.sleep,
                      max_seconds: float | None = None) -> dict:
    """Read a finite URL manifest; start/cutoff never imply whole-day coverage.

    ``previous_cutoff`` supports (previous, cutoff] incremental publication windows.
    A supplied test transport is always labelled offline_test, never automatic.
    Collection archives data, but evidence database ingestion is a separate layer.
    ``max_seconds`` bounds further requests/retries and waits; collected evidence
    remains available when the deadline prevents reading subsequent pages.
    """
    began = time.monotonic()
    if max_seconds is not None and (type(max_seconds) not in {int, float}
            or not math.isfinite(max_seconds) or not 0 <= max_seconds <= 14400):
        raise ValueError("material_runtime_limit_invalid")
    deadline = None if max_seconds is None else began + max_seconds
    runtime_limit_reached = False
    config = load_source_registry(registry_path)
    start_at, cutoff_at = aware_time(previous_cutoff or start), aware_time(cutoff)
    if start_at >= cutoff_at:
        raise ValueError("资讯起点必须早于截点")
    now = datetime.now(SHANGHAI)
    if cutoff_at > now:
        raise ValueError("资讯截点不能晚于实际采集时间")
    mode = "offline_test" if transport is not None else "automatic"
    out = Path(output_dir).resolve()
    if mode == "offline_test" and (Path("data/research").resolve() == out or Path("data/research").resolve() in out.parents):
        raise ValueError("离线来源测试不能写入真实研究资料目录")
    out = out / (now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    raw_dir = out / "responses"
    raw_dir.mkdir()
    get = transport or _http_get
    requests, health, evidence, all_evidence, rejected = [], [], [], [], []
    robots_cache = {}
    stopped_hosts = set()
    limits = config["request_limits"]

    def remaining():
        nonlocal runtime_limit_reached
        left = float("inf") if deadline is None else deadline - time.monotonic()
        if left <= 0:
            runtime_limit_reached = True
            raise RuntimeError("source_runtime_limit")
        return left

    def request(url, source_id, role):
        nonlocal runtime_limit_reached
        last = None
        for attempt in range(1, limits["max_attempts"] + 1):
            left = remaining()
            if len(requests) >= limits["max_requests"]:
                raise RuntimeError("达到本轮真实请求预算，停止新增请求")
            if requests:
                if left <= limits["interval_seconds"]:
                    # Never shorten the registered interval to squeeze in a call.
                    runtime_limit_reached = True
                    raise RuntimeError("source_runtime_limit")
                sleep(limits["interval_seconds"])
            timeout = min(limits["timeout_seconds"], remaining())
            row = {"request_index": len(requests) + 1, "source_id": source_id, "url": url,
                   "role": role, "attempt": attempt, "fetched_at": datetime.now(SHANGHAI).isoformat(),
                   "acquisition_mode": mode}
            try:
                response = get(url, timeout)
                if len(response.body) > MAX_RESPONSE_BYTES:
                    raise ValueError("响应超出大小限制")
                path = raw_dir / f"{row['request_index']:03d}-{role}.raw"
                path.write_bytes(response.body)
                row.update(http_status=response.status, raw_locator=str(path), raw_sha256=_hash(response.body),
                           byte_count=len(response.body), response_content_type=response.content_type,
                           status="ok" if response.status == 200 else "http_error")
                requests.append(row)
                if response.status in (401, 403, 429):
                    stopped_hosts.add(urlsplit(url).hostname)
                    return response, row
                if response.status >= 500 and attempt < limits["max_attempts"]:
                    continue
                return response, row
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                # No credentials are accepted in registry URLs; avoid echoing exception request internals.
                row.update(status="timeout" if isinstance(exc, TimeoutError) else "request_failed",
                           error_type=type(exc).__name__, error="公开页面请求失败；未记录网络环境或凭据")
                requests.append(row)
                last = row
        raise RuntimeError(f"公开页面请求有限尝试失败：{last['error_type']}")

    def robots_allowed(url, sid):
        host = urlsplit(url).hostname
        if host in stopped_hosts:
            raise PermissionError("同源先前鉴权/限流/访问限制，停止该源")
        if host not in robots_cache:
            response, row = request("https://" + host + "/robots.txt", sid, "robots")
            if response.status == 404:
                robots_cache[host] = None
                row["interpretation"] = "robots 404；无可用规则，不代表内容使用许可"
            elif response.status != 200:
                raise PermissionError("robots 规则未成功取得，停止该源")
            else:
                robot = RobotFileParser()
                robot.parse(response.body.decode("utf-8-sig", errors="strict").splitlines())
                robots_cache[host] = robot
        robot = robots_cache[host]
        if robot and not robot.can_fetch(USER_AGENT, url):
            raise PermissionError("robots 禁止此登记页面，停止该源")

    for source in config["sources"]:
        reg = source["registration"]
        entry = {"source_id": reg["source_id"], "category": reg["category"], "enabled": reg["enabled"],
                 "configured_pages": len(source.get("pages", [])), "parsed_count": 0, "selected_count": 0,
                 "status": "disabled" if not reg["enabled"] else "pending", "failures": [],
                 "coverage": reg["pagination_limits"], "not_whole_market": True}
        health.append(entry)
        if not reg["enabled"]:
            entry["failures"].append(source.get("disabled_reason", "来源默认关闭或权限未核实"))
            continue
        pages = list(source.get('pages', []))
        discovery = source.get('discovery')
        if discovery:
            pending_indexes = list(discovery['index_urls'])
            visited, found = set(), {}
            entry.update(discovery_status='running', index_pages_read=0, discovered_count=0)
            try:
                while pending_indexes and len(visited) < discovery['max_index_pages']:
                    index_url = pending_indexes.pop(0)
                    if index_url in visited:
                        continue
                    visited.add(index_url)
                    robots_allowed(index_url, reg['source_id'])
                    response, row = request(index_url, reg['source_id'], 'index')
                    if response.status != 200:
                        raise PermissionError(f'资讯目录 HTTP {response.status}')
                    if 'text/html' not in response.content_type.lower():
                        raise ValueError('资讯目录未返回 HTML')
                    discovered = discover_links(response.body, index_url, discovery)
                    entry['index_pages_read'] += 1
                    for page in discovered['articles']:
                        found.setdefault(page['url'], page)
                    pending_indexes.extend(p for p in discovered['next_pages'] if p not in visited)
                entry.update(discovered_count=len(found), discovery_status='ok',
                             index_limit_reached=bool(pending_indexes),
                             article_limit_reached=len(found) > discovery['max_articles'])
                pages = list(found.values())[:discovery['max_articles']] + pages
                entry['coverage'] = '每日自动发现已登记目录；有界样本，目录外和未读取文章不在覆盖范围'
            except (ValueError, RuntimeError, PermissionError) as exc:
                entry['failures'].append(str(exc))
                entry['discovery_status'] = 'failed'
                pages = []
        pages = list({p['url']: p for p in pages}.values())
        for page in pages:
            try:
                robots_allowed(page["url"], reg["source_id"])
                response, request_row = request(page["url"], reg["source_id"], "article")
                if response.status != 200:
                    raise PermissionError(f"HTTP {response.status}；该来源停止")
                if "text/html" not in response.content_type.lower():
                    raise ValueError("响应类型不是已核对 HTML 正文")
                parsed = ADAPTERS[reg["category"]].parse(response.body, source, page)
                entry["parsed_count"] += 1
                request_row["parsed_metadata"] = {k: v for k, v in parsed.items() if k != "content"}
                # Minute/date precision is retained; date-only items require the whole publication day before cutoff.
                if parsed["publication_precision"] == "date":
                    pub_latest = aware_time(parsed["published_at"] + "T23:59:59.999999+08:00")
                    pub_earliest = aware_time(parsed["published_at"] + "T00:00:00+08:00")
                elif parsed["publication_precision"] == "minute":
                    pub_earliest = aware_time(parsed["published_at"])
                    pub_latest = pub_earliest.replace(second=59, microsecond=999999)
                else:
                    pub_latest = pub_earliest = aware_time(parsed["published_at"])
                reason = None
                if pub_latest > cutoff_at:
                    reason = "after_cutoff_or_date_precision_ambiguous"
                elif pub_earliest < start_at or (previous_cutoff and pub_earliest == start_at):
                    if not include_background:
                        reason = "before_query_window"
                ev = build_evidence(source_id=reg["source_id"], category=reg["category"],
                    original_url=page["url"], raw_locator=request_row["raw_locator"], title=parsed["title"],
                    published_at=parsed["published_at"], publication_precision=parsed["publication_precision"],
                    first_seen_at=request_row["fetched_at"], fetched_at=request_row["fetched_at"],
                    effective_from=page.get("effective_from"), effective_to=page.get("effective_to"),
                    content_type=parsed["content_type"], content=parsed["content"],
                    content_truncated=parsed["content_truncated"],
                    original_publisher=parsed["original_publisher"], event_key=page.get("event_key"),
                    acquisition_mode=mode, security_associations=[])
                if isinstance(sample_symbols, dict):
                    ev = link_subjects(ev, sample_symbols)
                all_evidence.append(ev)
                if reason:
                    rejected.append({"source_id": reg["source_id"], "url": page["url"], "reason": reason,
                                     "published_at": parsed["published_at"], "request_index": request_row["request_index"],
                                     "evidence_id": ev["evidence_id"]})
                    continue
                evidence.append(ev)
                entry["selected_count"] += 1
            except (ValueError, RuntimeError, PermissionError) as exc:
                entry["failures"].append(str(exc))
                if isinstance(exc, (RuntimeError, PermissionError)):
                    stopped_hosts.add(urlsplit(page["url"]).hostname)
                if isinstance(exc, (RuntimeError, PermissionError)):
                    break
                # An unparseable/non-original article is a recorded gap; a later
                # registered article can still succeed within the same budget.
                continue
        entry["status"] = ("partial" if entry["selected_count"] else "failed") if entry["failures"] else (
            "ok" if entry["selected_count"] else "empty_bounded_window")
        if not pages and not entry['failures']:
            entry["status"] = "empty_configuration"
    result = {"schema_version": "m3-collection-v1", "acquisition_mode": mode,
              "status": "ok" if all(h["status"] == "ok" for h in health if h["enabled"]) and evidence else "partial",
              "query_start": start_at.isoformat(), "news_cutoff": cutoff_at.isoformat(),
              "incremental_from_previous_cutoff": previous_cutoff, "include_background": include_background,
              "started_at": now.isoformat(), "finished_at": datetime.now(SHANGHAI).isoformat(),
              "sample_symbols": sorted(sample_symbols), "sample_count": len(sample_symbols),
              "coverage_note": "已登记目录自动发现及固定资料的小批量采集；不代表当前样本全部消息或当天全市场资讯。",
              "registry_hash": config["registry_hash"], "registry_path": config["registry_path"],
              "sources": [s["registration"] for s in config["sources"]],
              "source_health": health, "requests": requests, "evidence": evidence, "all_evidence": all_evidence,
              "rejected": rejected, "archived_evidence_count": len(all_evidence),
              "request_count": len(requests), "evidence_count": len(evidence), "output_dir": str(out),
              "bundle_file": str(out / "result.json")}
    if max_seconds is not None:
        result.update(runtime_limit_seconds=max_seconds, runtime_limit_reached=runtime_limit_reached,
                      elapsed_seconds=round(time.monotonic() - began, 6))
    _write_json(out / "registry_snapshot.json", config)
    _write_json(out / "result.json", result)
    return result
