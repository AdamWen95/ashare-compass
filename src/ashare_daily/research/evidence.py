"""M3 evidence versions, conservative time filtering and publication checks.

No network, model execution, arbitrary retrieval or market-data mutation lives here.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import Evidence, ResearchOutput, SHANGHAI, SourceRegistration, aware_time


SCHEMA_VERSION = "m3-evidence-v1"
_PROJECT = Path(__file__).resolve().parents[3]
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|"
    r"忽略(?:此前|之前|所有|系统|以上).{0,8}(?:指令|要求)|"
    r"(?:泄露|输出|发送|打印).{0,12}(?:密钥|API.?KEY|系统提示词)|"
    r"(?:执行|运行).{0,8}(?:powershell|cmd\.exe|shell命令)", re.I,
)
_PROHIBITED = re.compile(
    r"立即买入|建议买入|建议卖出|买入价|卖出策略|仓位|下单|止盈|止损|"
    r"保证收益|必涨|上涨概率|胜率|(?:buy|sell)\s+(?:now|at)|position\s+size", re.I,
)
_MARKET_NUMBER = re.compile(
    r"(?:股价|收盘价|开盘价|收报|报收|均线|MA\s*\d+|涨跌幅|涨幅|跌幅|相对收益|成交额|成交量|换手率|"
    r"close|moving\s*average|return|turnover).{0,24}[-+]?(?:\d|[零一二三四五六七八九十百千万亿两]+(?:元|点|股|成))|"
    r"[-+]?\d[\d.,%％]*.{0,8}(?:股价|收盘价|均线|涨跌幅)", re.I,
)


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"spm", "from", "ref"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def build_evidence(**values) -> dict:
    """Create a stable immutable content version; actual fetch times are not the ID.

    Source adapters must supply actual dates and content provenance. This function
    cannot infer permissions, security identity, publication time or article body.
    """
    values = dict(values)
    supplied_hash = values.get("content_hash")
    values["content_hash"] = digest(values.get("content", ""))
    supplied_version = values.get("content_version")
    supplied_id = values.get("evidence_id")
    values.setdefault("content_version", "pending")
    values.setdefault("evidence_id", "pending")
    evidence = Evidence.model_validate(values)
    normalized = evidence.model_dump(mode="json")
    body_hash = digest(normalized["content"])
    if supplied_hash is not None and supplied_hash != body_hash:
        raise ValueError("正文内容与 content_hash 不匹配")
    evidence.content_hash = body_hash
    normalized["content_hash"] = body_hash
    version_fields = {key: val for key, val in normalized.items() if key not in {
        "evidence_id", "content_version", "first_seen_at", "fetched_at", "raw_locator",
    }}
    version_fields["original_url"] = canonical_url(normalized["original_url"])
    version = digest(version_fields)
    if supplied_version is not None and supplied_version != version:
        raise ValueError("内容版本与规范化资料不匹配")
    if supplied_id is not None and supplied_id != "ev-" + version:
        raise ValueError("证据 ID 与不可变内容版本不匹配")
    evidence.content_version = version
    evidence.evidence_id = "ev-" + version
    for association in evidence.security_associations:
        if association.basis_quote not in evidence.content:
            raise ValueError("证券关联依据不在实际正文/摘要中")
        code = association.symbol.split(".")[1]
        if association.name not in association.basis_quote and code not in association.basis_quote:
            raise ValueError("证券关联摘录没有对应证券名称或代码")
    return evidence.model_dump(mode="json")


def _event_id(item: dict) -> str:
    # An explicit event key groups cross-URL progress/corrections. Versions at
    # the same original publisher URL remain one event. Separately, selection
    # collapses identical syndicated text even across different source URLs.
    key = item.get("event_key") or canonical_json({
        "publisher": item["original_publisher"], "url": canonical_url(item["original_url"]),
    })
    return "event-" + digest(key)


class EvidenceStore:
    """Add only m3_* tables to the existing market database.

    Offline stores have an enduring separate mode and cannot touch a real market
    database or the project's research data directory.
    """

    def __init__(self, database: Path | str, verification_kind: str = "real"):
        if verification_kind not in {"real", "offline_test"}:
            raise ValueError("证据库模式必须为 real 或 offline_test")
        self.path = Path(database).resolve()
        self.verification_kind = verification_kind
        if verification_kind == "offline_test" and self.path.is_relative_to(_PROJECT / "data" / "research"):
            raise ValueError("离线测试证据不能写入真实 research 数据目录")
        if "demo" in {part.lower() for part in self.path.parts}:
            raise ValueError("M3 证据与 DEMO 数据隔离")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(table.startswith("demo_") for table in tables):
                raise ValueError("拒绝修改 DEMO 数据库")
            market_metadata = (dict(connection.execute("SELECT key,value FROM market_metadata"))
                               if "market_metadata" in tables else {})
            market_is_offline = market_metadata.get("verification_kind") == "offline_test"
            if verification_kind == "real":
                if (market_metadata.get("mode") != "research" or
                        market_metadata.get("schema_version") != "m1-baostock-market-v1" or market_is_offline):
                    raise ValueError("真实证据须复用已核验的真实 M1 行情数据库")
            elif market_metadata and not market_is_offline:
                raise ValueError("离线证据不得写入真实行情数据库")
            if "m3_metadata" in tables:
                metadata = dict(connection.execute("SELECT key,value FROM m3_metadata"))
                if metadata.get("verification_kind") != verification_kind:
                    raise ValueError("不能混用真实证据与 offline_test 数据")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS m3_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS m3_source_registry (
                    source_id TEXT NOT NULL,version_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
                    PRIMARY KEY(source_id,version_hash));
                CREATE TABLE IF NOT EXISTS m3_source_active (
                    source_id TEXT PRIMARY KEY,version_hash TEXT NOT NULL,
                    FOREIGN KEY(source_id,version_hash) REFERENCES m3_source_registry(source_id,version_hash));
                CREATE TABLE IF NOT EXISTS m3_evidence (
                    evidence_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,canonical_url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,event_id TEXT NOT NULL,first_seen_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS m3_observations (
                    observation_id TEXT PRIMARY KEY,evidence_id TEXT NOT NULL,fetched_at TEXT NOT NULL,
                    raw_locator TEXT NOT NULL,acquisition_mode TEXT NOT NULL,
                    FOREIGN KEY(evidence_id) REFERENCES m3_evidence(evidence_id));
                CREATE TABLE IF NOT EXISTS m3_events (
                    event_id TEXT NOT NULL,evidence_id TEXT NOT NULL,revision_of TEXT,
                    PRIMARY KEY(event_id,evidence_id),
                    FOREIGN KEY(evidence_id) REFERENCES m3_evidence(evidence_id));
                CREATE INDEX IF NOT EXISTS m3_content_lookup ON m3_evidence(content_hash);
            """)
            connection.executemany("INSERT OR IGNORE INTO m3_metadata VALUES (?,?)", [
                ("schema_version", SCHEMA_VERSION), ("verification_kind", verification_kind),
            ])

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def register_sources(self, sources: Iterable[dict | SourceRegistration]) -> list[dict]:
        records = [SourceRegistration.model_validate(item).model_dump(mode="json") for item in sources]
        if len({item["source_id"] for item in records}) != len(records):
            raise ValueError("同批来源 ID 重复")
        with self._connection() as connection:
            connection.executemany("INSERT OR IGNORE INTO m3_source_registry VALUES (?,?,?)", [
                (item["source_id"], digest(item), canonical_json(item)) for item in records
            ])
            connection.executemany("INSERT INTO m3_source_active VALUES (?,?) ON CONFLICT(source_id) "
                                   "DO UPDATE SET version_hash=excluded.version_hash", [
                (item["source_id"], digest(item)) for item in records
            ])
        return records

    def list_sources(self) -> list[dict]:
        with self._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM m3_source_registry ORDER BY source_id,version_hash")]

    def ingest(self, evidence: Iterable[dict | Evidence]) -> dict:
        records = []
        for value in evidence:
            item = value.model_dump(mode="json") if isinstance(value, Evidence) else dict(value)
            item = build_evidence(**item)
            if (item["acquisition_mode"] == "offline_test") != (self.verification_kind == "offline_test"):
                raise ValueError("证据 acquisition_mode 与数据库模式不符")
            records.append(item)
        inserted = 0
        observation_count = 0
        with self._connection() as connection:
            sources = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT r.source_id,r.payload_json FROM m3_source_registry r JOIN m3_source_active a "
                "ON r.source_id=a.source_id AND r.version_hash=a.version_hash")}
            for item in records:
                source = sources.get(item["source_id"])
                if source is None:
                    raise ValueError("证据来源未登记")
                if not source["cache_allowed"]:
                    raise ValueError("来源未允许本地证据缓存")
                if source["category"] != item["category"] or item["content_type"] not in source["content_access"]:
                    raise ValueError("证据类别/内容类型不在登记来源权限范围")
                event_id = _event_id(item)
                existing = connection.execute("SELECT payload_json,first_seen_at FROM m3_evidence WHERE evidence_id=?",
                                              (item["evidence_id"],)).fetchone()
                if existing:
                    old = json.loads(existing[0])
                    volatile = {"first_seen_at", "fetched_at", "raw_locator"}
                    if {k: v for k, v in old.items() if k not in volatile} != {k: v for k, v in item.items() if k not in volatile}:
                        raise ValueError("相同证据 ID 的不可变内容发生变化")
                    item["first_seen_at"] = existing[1]
                else:
                    # first_seen_at is local observation, never publisher's time.
                    item["first_seen_at"] = item["fetched_at"]
                    connection.execute("INSERT INTO m3_evidence VALUES (?,?,?,?,?,?,?)", (
                        item["evidence_id"], item["source_id"], canonical_url(item["original_url"]),
                        item["content_hash"], event_id, item["first_seen_at"], canonical_json(item)))
                    inserted += 1
                observation = {key: item[key] for key in ("evidence_id", "fetched_at", "raw_locator", "acquisition_mode")}
                before = connection.total_changes
                connection.execute("INSERT OR IGNORE INTO m3_observations VALUES (?,?,?,?,?)", (
                    digest(observation), item["evidence_id"], item["fetched_at"], item["raw_locator"], item["acquisition_mode"]))
                observation_count += connection.total_changes - before
                connection.execute("INSERT OR IGNORE INTO m3_events VALUES (?,?,?)", (event_id, item["evidence_id"], item["revision_of"]))
        return {"received_count": len(records), "inserted_count": inserted,
                "existing_count": len(records) - inserted, "new_observation_count": observation_count,
                "evidence_ids": [item["evidence_id"] for item in records]}

    def list_evidence(self) -> list[dict]:
        with self._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM m3_evidence ORDER BY evidence_id")]


def _publication_bounds(value: str | None, precision: str | None = None) -> tuple[datetime, datetime] | None:
    if value is None:
        return None
    if precision == "date" or (precision is None and len(value) == 10):
        day = date.fromisoformat(value)
        return datetime.combine(day, time.min, SHANGHAI), datetime.combine(day, time.max, SHANGHAI)
    value_at = aware_time(value)
    if precision == "minute":
        return value_at, value_at + timedelta(seconds=59, microseconds=999999)
    return value_at, value_at


def select_evidence(evidence: Iterable[dict | Evidence], start: str | datetime, cutoff: str | datetime,
                    allow_historical_reconstruction: bool = True) -> dict:
    """Select (start, cutoff] new events and explicitly labeled old background.

    Date-only items are accepted only after the entire publication day is over.
    First observation after cutoff makes a historical reconstruction, never an
    as-observed realtime report. No unknown-date item is passed to the model.
    """
    start_at, cutoff_at = aware_time(start), aware_time(cutoff)
    if start_at >= cutoff_at:
        raise ValueError("资讯查询起点必须早于截点")
    eligible, excluded = [], []
    seen_content: dict[str, str] = {}
    # Prefer the earliest known original when the same text is republished.
    # Sorting by hashes could otherwise arbitrarily call the later reprint new.
    def chronological(value):
        item = value.model_dump(mode="json") if isinstance(value, Evidence) else value
        published = _publication_bounds(item["published_at"], item["publication_precision"])
        original = _publication_bounds(item.get("original_published_at"))
        times = [bounds[0] for bounds in (published, original) if bounds is not None]
        return (min(times) if times else datetime.max.replace(tzinfo=SHANGHAI), item["evidence_id"])

    for value in sorted(evidence, key=chronological):
        item = value.model_dump(mode="json") if isinstance(value, Evidence) else dict(value)
        Evidence.model_validate(item)
        bounds = _publication_bounds(item["published_at"], item["publication_precision"])
        reason = None
        if bounds is None:
            reason = "publication_time_unknown"
        elif bounds[1] > cutoff_at:
            reason = "after_cutoff_or_date_precision_ambiguous"
        elif item["content_type"] == "metadata_only":
            reason = "metadata_only_not_read_body"
        elif _INJECTION.search(item["content"]):
            reason = "untrusted_instruction_detected_not_sent_to_model"
        historical = aware_time(item["first_seen_at"]) > cutoff_at
        if reason is None and historical and not allow_historical_reconstruction:
            reason = "first_seen_after_cutoff"
        content_key = digest(re.sub(r"\s+", "", item["content"]))
        if reason is None and content_key in seen_content:
            reason = "syndicated_duplicate"
            item["duplicate_of"] = seen_content[content_key]
        if reason:
            excluded.append({"evidence_id": item["evidence_id"], "reason": reason,
                             "duplicate_of": item.get("duplicate_of")})
            continue
        origin_bounds = _publication_bounds(item.get("original_published_at"))
        event_bounds = origin_bounds if origin_bounds and origin_bounds[0] <= bounds[0] else bounds
        item.update({"is_background": event_bounds[0] <= start_at,
                     "interval_label": "background" if event_bounds[0] <= start_at else "new_event",
                     "historical_reconstruction": historical, "event_id": _event_id(item),
                     "untrusted_material": True})
        seen_content[content_key] = item["evidence_id"]
        eligible.append(item)
    return {"eligible": eligible, "excluded": excluded, "query_start": start_at.isoformat(),
            "cutoff": cutoff_at.isoformat(), "eligible_count": len(eligible),
            "new_event_count": sum(not item["is_background"] for item in eligible),
            "background_count": sum(item["is_background"] for item in eligible),
            "independent_event_count": len({item["event_id"] for item in eligible}),
            "historical_reconstruction": any(item["historical_reconstruction"] for item in eligible),
            "coverage_notice": "仅涵盖已获取且通过截点核验的资料；失败、目录与未取得资料不代表没有其他消息或风险"}


def load_local_materials(path: Path | str, *, fetched_at: str | datetime | None = None) -> dict:
    """Read a user-prepared provenance bundle; import is never automatic proof.

    The top level is {schema_version:'m3-local-materials-v1',sources:[],evidence:[]}.
    Input evidence omits IDs/hashes and can omit acquisition/observation times;
    local import always stamps its actual import time and marks mode manual.
    """
    from ashare_daily.operations.paths import resolve_input_path
    target = resolve_input_path(path).resolve()
    if target.stat().st_size > 5_000_000:
        raise ValueError("人工资料包超过 5 MB 小批量限制")
    payload = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "sources", "evidence"}:
        raise ValueError("人工资料包顶层字段不符")
    if payload["schema_version"] != "m3-local-materials-v1":
        raise ValueError("人工资料包版本不符")
    if len(payload["evidence"]) > 100 or len(payload["sources"]) > 20:
        raise ValueError("人工资料包超过小批量限制")
    imported_at = aware_time(fetched_at or datetime.now(SHANGHAI)).isoformat()
    sources = [SourceRegistration.model_validate(item).model_dump(mode="json") for item in payload["sources"]]
    evidence = []
    for raw in payload["evidence"]:
        if raw.get("acquisition_mode") == "offline_test":
            raise ValueError("离线模拟资料不得导入真实研究")
        item = dict(raw)
        item.update(acquisition_mode="manual", first_seen_at=imported_at, fetched_at=imported_at)
        item.setdefault("raw_locator", str(target))
        for generated in ("evidence_id", "content_hash", "content_version"):
            item.pop(generated, None)
        evidence.append(build_evidence(**item))
    return {"sources": sources, "evidence": evidence, "imported_at": imported_at,
            "import_file": str(target), "import_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "acquisition_mode": "manual"}


def asserts_today_novelty(text: str) -> bool:
    """A narrow negation guard; another affirmative clause still fails closed."""
    for match in re.finditer(r'今日(?:新|首次)|今天(?:新|首次)|当日首次', text):
        prefix = re.split(r'[，,。；;：:！？!?]', text[:match.start()])[-1]
        if not re.search(r'(?:并非|不是|不构成|不属于|不代表|不能(?:视为|当作|作为|视作|认定为)|不可(?:视为|当作)|不应(?:视为|当作))$', prefix):
            return True
    return False


def validate_claims(model_output: dict | ResearchOutput, evidence_input: list[dict],
                    allowed_symbols: Iterable[str] | dict, metric_registry: dict,
                    cutoff: str | datetime, *, strict_counterevidence: bool = False) -> dict:
    """Check only actual model-input excerpts; never claim semantic truth.

    Facts/counterevidence must be direct extracts. Inferences/opinions are kept
    explicitly labeled and require human semantic review even with valid quotes.
    """
    output = ResearchOutput.model_validate(model_output)
    allowed = set(allowed_symbols)
    cutoff_at = aware_time(cutoff)
    inputs = {item["evidence_id"]: item for item in evidence_input}
    accepted, rejected, rows = [], [], []
    seen_ids: set[str] = set()
    for claim in output.claims:
        item = claim.model_dump(mode="json")
        reasons = []
        text = item["text"]
        if not item["claim_id"] or item["claim_id"] in seen_ids:
            reasons.append("empty_or_duplicate_claim_id")
        seen_ids.add(item["claim_id"])
        if not text:
            reasons.append("empty_claim")
        if _PROHIBITED.search(text) or _INJECTION.search(text):
            reasons.append("prohibited_trade_or_instruction_content")
        if _MARKET_NUMBER.search(text):
            reasons.append("model_supplied_market_numbers_use_metric_ids")
        if (strict_counterevidence and item['claim_type'] == 'counterevidence'
                and not re.search(r'亏损|下降|减少|下滑|风险|终止|取消|不足|不确定|撤销|处罚|减持|停产|失效|逾期|诉讼|未达|未能|不及', text)):
            reasons.append('counterevidence_has_no_explicit_adverse_fact')
        for note in item["risks"] + item["unknowns"]:
            if (re.search(r"\d", note) or _PROHIBITED.search(note) or _INJECTION.search(note) or
                    not re.match(r"^(?:是否|需核验|需要核验|尚需核实|待核实|待核查|待确认)", note) or
                    re.search(r"[。！!；;]\s*\S", note)):
                reasons.append("uncited_risk_or_unknown_assertion")
        symbol = item["symbol"]
        if symbol is not None and symbol not in allowed:
            reasons.append("symbol_outside_research_scope")
        referenced_symbols = set(re.findall(r"(?:sh|sz)\.\d{6}", text))
        if isinstance(allowed_symbols, dict):
            for known_symbol, registered in allowed_symbols.items():
                name = registered.get("name") if isinstance(registered, dict) else registered
                if name and name in text:
                    referenced_symbols.add(known_symbol)
        if referenced_symbols - allowed:
            reasons.append("symbol_outside_research_scope")
        if referenced_symbols and referenced_symbols != {symbol}:
            reasons.append("claim_text_security_identity_mismatch")
        if not item["citations"]:
            reasons.append("no_evidence_citation")
        citation_texts = []
        has_association = symbol is None
        event_ids = set()
        for citation in item["citations"]:
            source = inputs.get(citation["evidence_id"])
            if source is None:
                reasons.append("evidence_not_in_actual_model_input")
                continue
            bounds = _publication_bounds(source.get("published_at"), source.get("publication_precision"))
            if bounds is None or bounds[1] > cutoff_at:
                reasons.append("evidence_time_not_eligible")
            if source["content_type"] == "metadata_only":
                reasons.append("metadata_cannot_support_body_analysis")
            if (source["category"] == "research_report" and item["claim_type"] in {"fact", "counterevidence"}
                    and re.search(r"评级|目标价|预计|预测|预期|盈利预测", text)):
                reasons.append("research_rating_or_forecast_must_be_opinion")
            quote = citation["quote"]
            if not quote or quote not in source["content"]:
                reasons.append("quote_not_in_actual_input_content")
            if citation["locator"] != source["raw_locator"]:
                reasons.append("locator_not_in_actual_input")
            # The exact archived locator is supplied in the input; the model may
            # not invent a page number or paragraph position.
            if _INJECTION.search(citation["locator"]) or _PROHIBITED.search(citation["locator"]):
                reasons.append("unsafe_locator")
            if source.get("content_truncated") or source["content_type"] == "abstract":
                if re.search(r"全文|完整公告|全部条款|详细条款", text):
                    reasons.append("abstract_cannot_claim_fulltext_review")
            if source.get("is_background") and asserts_today_novelty(text):
                reasons.append("old_material_misrepresented_as_today")
            if symbol is not None:
                for assoc in source.get("security_associations", []):
                    registered = allowed_symbols.get(symbol) if isinstance(allowed_symbols, dict) else None
                    registered_name = registered.get("name") if isinstance(registered, dict) else registered
                    identity_ok = (symbol.split(".")[1] in assoc["basis_quote"] or
                                   registered_name is None or registered_name == assoc["name"])
                    if (assoc["symbol"] == symbol and assoc["basis_quote"] in source["content"] and
                            identity_ok and (assoc["name"] in assoc["basis_quote"] or
                                             symbol.split(".")[1] in assoc["basis_quote"])):
                        has_association = True
            citation_texts.append(quote)
            event_ids.add(source.get("event_id") or _event_id(source))
        if not has_association:
            reasons.append("security_business_association_unverified")
        if (symbol and item["claim_type"] in {"inference", "opinion"} and
                re.search(r"受益|业务催化|政策利好|带来.{0,12}(?:收入|利润|订单)", text)):
            business_basis = any(
                assoc["symbol"] == symbol and assoc["association_type"] == "business_relationship"
                and assoc["basis_quote"] in source["content"]
                for citation in item["citations"]
                for source in [inputs.get(citation["evidence_id"], {})]
                for assoc in source.get("security_associations", [])
            )
            if not business_basis:
                reasons.append("benefit_inference_has_no_explicit_business_basis")
        if item["claim_type"] in {"fact", "counterevidence"} and not any(text in quote for quote in citation_texts):
            reasons.append("fact_not_supported_by_extract_exact_text")
        for metric_id in item["metric_ids"]:
            metric = metric_registry.get(metric_id)
            if metric is None:
                reasons.append("unknown_metric_id")
            elif isinstance(metric, dict):
                if metric.get("value") is None:
                    reasons.append("metric_value_unavailable")
                if metric.get("symbol") is not None and metric["symbol"] != symbol:
                    reasons.append("metric_symbol_mismatch")
        reasons = sorted(set(reasons))
        needs_review = item["claim_type"] in {"inference", "opinion", "unknown", "followup"}
        record = {**item, "validation_status": "rejected" if reasons else "accepted",
                  "validation_reasons": reasons, "semantic_review_required": True,
                  "support_check": "extractive_match" if not needs_review else "citation_structure_only",
                  "independent_event_count": len(event_ids)}
        (rejected if reasons else accepted).append(record)
        for citation in item["citations"]:
            rows.append({"claim_id": item["claim_id"], "claim_type": item["claim_type"],
                         "symbol": symbol, "claim_text": text, **citation,
                         "validation_status": record["validation_status"],
                         "validation_reasons": reasons, "semantic_review_required": True,
                         "support_check": record["support_check"]})
    return {"accepted_claims": accepted, "rejected_claims": rejected, "claim_evidence_rows": rows,
            "status": "ok" if accepted and not rejected else "partial" if accepted else "no_valid_claims",
            "accepted_count": len(accepted), "rejected_count": len(rejected),
        "semantic_review_required": True,
            "validation_notice": "引用存在、摘录匹配和提取式事实校验不等于语义必然正确；推论、归因及遗漏须人工复核"}
