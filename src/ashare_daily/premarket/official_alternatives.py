"""Narrow MOF main-site news and SZSE original news parsers, without script execution."""
from datetime import date
from html import unescape
import re
from urllib.parse import urljoin, urlsplit

from ..research.sources import _PageParser

MOF_INDEX = "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/"
SZSE_INDEX = "https://www.szse.cn/aboutus/trends/news/index.html"


def _rows_checked(rows):
    if not rows or len(rows) > 100:
        raise ValueError("official_directory_empty_or_oversized")
    unique = {}
    for row in rows:
        date.fromisoformat(row["date"])
        old = unique.get(row["url"])
        if old and old != row:
            raise ValueError("official_directory_conflicting_duplicate")
        unique[row["url"]] = row
    values = list(unique.values())
    dates = [r["date"] for r in values]
    if dates != sorted(dates, reverse=True):
        raise ValueError("official_directory_unsorted")
    return values


def _blocks(body, class_name):
    text = body.decode("utf-8-sig", errors="strict")
    # SZSE includes an inert client-template copy after the real rows. It is
    # not a published item and must not be interpreted or executed.
    text = re.sub(r'''<script\b[^>]*type\s*=\s*['"]text/html['"][^>]*>.*?</script>''', "", text, flags=re.S | re.I)
    return re.findall(r'<ul\b[^>]*class="' + re.escape(class_name) + r'"[^>]*>(.*?)</ul>', text, re.S | re.I)


def parse_mof_index(body):
    rows = []
    for block in _blocks(body, "xwfb_listbox"):
        for cell in re.findall(r"<li\b[^>]*>(.*?)</li>", block, re.S | re.I):
            link = re.search(r'<a\b[^>]*href="([^"<>]+)"[^>]*>(.*?)</a>', cell, re.S | re.I)
            stamp = re.search(r"<span[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</span>", cell)
            if not link or not stamp:
                raise ValueError("mof_directory_row_invalid")
            url = urljoin(MOF_INDEX, unescape(link[1]))
            parsed = urlsplit(url)
            # Exact same main-site namespace only; subordinate ministries and
            # local bureaus require independent registration and robots checks.
            if parsed.scheme == "http" and parsed.netloc == "www.mof.gov.cn":
                url = "https://" + url[len("http://"):]
            in_scope = bool(re.fullmatch(r"https://www\.mof\.gov\.cn/zhengwuxinxi/caizhengxinwen/\d{6}/t\d{8}_\d+\.htm", url))
            rows.append({"url": url, "title": unescape(re.sub(r"<[^>]*>", "", link[2])).strip(),
                "date": stamp[1], "in_scope": in_scope})
    return _rows_checked(rows)


def parse_szse_index(body):
    rows = []
    for block in _blocks(body, "newslist date-right"):
        for cell in re.findall(r"<li\b[^>]*>(.*?)</li>", block, re.S | re.I):
            script = re.sub(r"(?m)^\s*//.*$", "", cell)
            def literal(name):
                values = re.findall(r"(?m)^\s*var " + name + r"\s*=\s*(['\"])([^'\"\\\r\n]*)\1\s*;?\s*$", script)
                if len(values) != 1:
                    raise ValueError("szse_directory_literal_invalid")
                return unescape(values[0][1])
            url = urljoin(SZSE_INDEX, literal("curHref"))
            if not re.fullmatch(r"https://www\.szse\.cn/aboutus/trends/news/t\d{8}_\d+\.html", url):
                raise ValueError("szse_directory_url_invalid")
            stamp = re.search(r'<span\b[^>]*class="time"[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</span>', cell)
            if not stamp:
                raise ValueError("szse_directory_date_missing")
            rows.append({"url": url, "title": literal("curTitle"), "date": stamp[1], "in_scope": True})
    return _rows_checked(rows)


def parse_alternative_article(body, provider, item):
    if provider not in {"mof", "szse"}:
        raise ValueError("official_article_provider_invalid")
    parser = _PageParser(body_class="my_doccontent" if provider == "mof" else "des-content",
        info_class="laiyuan" if provider == "mof" else "time", title_class="title_con" if provider == "mof" else "title")
    parser.feed(body.decode("utf-8-sig", errors="strict"))
    title, text, info = parser.text("title"), parser.text("body"), parser.text("info")
    if title != item["title"] or len(text) < 30 or parser.body_containers != 1 or parser.closed_body_containers != 1:
        raise ValueError("official_article_identity_or_container_invalid")
    match = re.search(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日)?", info)
    if not match:
        raise ValueError("official_article_visible_date_missing")
    published = date(*map(int, match.groups())).isoformat()
    if published != item["date"]:
        raise ValueError("official_article_date_mismatch")
    if provider == "mof" and not re.search(r"来源\s*[:：]\s*财政部新闻办公室", text[:200]):
        raise ValueError("mof_original_publisher_unverified")
    metadata = parser.meta.get("pubdate")
    return {"title": title, "content": text, "content_type": "abstract" if parser.attachment_urls else "fulltext",
        "published_at": published, "publication_precision": "date", "original_url": item["url"],
        "public_url": "https://www.szse.cn/" if provider == "szse" else item["url"],
        "original_publisher": "深圳证券交易所" if provider == "szse" else "财政部新闻办公室",
        "article_metadata_time": metadata, "directory_published_at": item["date"],
        "time_conflict": bool(metadata and metadata[:10] != published), "effective_at": None,
        "effective_time_status": "unverified", "model_use_allowed": False, "attachments_read": False,
        "semantic_review_required": True}
