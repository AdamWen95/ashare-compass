"""M2.1 dated, manually reviewed eligibility evidence. Never fetches websites.

An import is an auditable human assertion, not an automatic authentication of
the source text. No record or source is trusted merely because it has a URL.
The caller freezes both the imported bundle and the resolved states.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

from ashare_daily.market_schemas import SHANGHAI


SCHEMA_VERSION = "eligibility-evidence-v1"
UNKNOWN_REASON = "退市整理期缺少适用于分析日期和市场的独立证据，保持 unknown"
_SHA = r"^[0-9a-f]{64}$"
_SYMBOL = r"^(sh|sz)\.[0-9]{6}$"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def timezone_required(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("证据时间必须带时区")
            return value.astimezone(SHANGHAI)
        return value


class EvidenceSource(EvidenceModel):
    source_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    access_method: str = Field(min_length=1)
    field_meaning: str = Field(min_length=1)
    markets: list[Literal["SH", "SZ"]] = Field(min_length=1)
    coverage_scope: Literal["individual_securities", "all_mainboard_a_shares", "unknown"]
    supports_historical_dates: StrictBool | None
    supports_complete_lists: StrictBool
    approved_for_local_use: StrictBool
    usage_limits: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    reviewed_at: datetime
    review_basis: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def web_locator(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("原始出处必须是无凭据的 HTTP(S) URL")
        return value


class EvidencePage(EvidenceModel):
    number: int = Field(ge=1, strict=True)
    source_url: str = Field(min_length=1)
    raw_locator: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=_SHA)
    status: Literal["ok", "failed", "partial", "schema_changed"]
    symbols: list[str] = Field(max_length=10000)

    @field_validator("source_url")
    @classmethod
    def web_locator(cls, value):
        return EvidenceSource.web_locator(value)


class EligibilityEvidence(EvidenceModel):
    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    kind: Literal["security_state", "complete_delisting_list"]
    market: Literal["SH", "SZ"]
    source_url: str = Field(min_length=1)
    raw_locator: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=_SHA)
    content_version: str = Field(min_length=1)
    first_seen_at: datetime
    fetched_at: datetime
    effective_from: date
    effective_to: date
    temporal_basis: Literal["explicit_historical_interval", "same_date_complete_snapshot", "unknown"]
    retrieval_status: Literal["ok", "empty_confirmed", "empty", "request_failed", "timeout", "permission_denied", "rate_limited", "partial", "schema_changed"]
    parse_status: Literal["ok", "failed", "not_attempted"]
    reviewed_by: str = Field(min_length=1)
    reviewed_at: datetime
    review_basis: str = Field(min_length=1)
    # A concise original locator plus factual interpretation; not an executable instruction.
    evidence_excerpt: str = Field(min_length=1)
    assertion_basis: Literal["explicit_statement", "complete_list", "unknown"]
    symbol: str | None = None
    delisting_period: StrictBool | None = None
    complete_scope: Literal["all_mainboard_a_shares", "unknown"] = "unknown"
    expected_total_records: int | None = Field(default=None, ge=0, le=10000, strict=True)
    total_pages: int | None = Field(default=None, ge=1, le=1000, strict=True)
    pages: list[EvidencePage] = Field(default_factory=list, max_length=1000)

    @field_validator("source_url")
    @classmethod
    def web_locator(cls, value):
        return EvidenceSource.web_locator(value)

    @model_validator(mode="after")
    def ordered_times(self):
        if self.first_seen_at > self.fetched_at:
            raise ValueError("首次取得时间不能晚于本次取得时间")
        if self.reviewed_at < self.fetched_at:
            raise ValueError("人工核验时间不能早于取得证据时间")
        if self.effective_from > self.effective_to:
            raise ValueError("证据生效区间倒置")
        if self.symbol is not None and not re.fullmatch(_SYMBOL, self.symbol):
            raise ValueError("证据代码必须包含沪深市场和六位代码")
        if self.kind == "security_state" and not self.symbol:
            raise ValueError("个股状态证据必须指明证券代码")
        return self


def load_evidence_bundle(path: Path | str | None) -> dict:
    """Load without network or current-time inputs; absent path is NOT false.

    Bad JSON/envelopes are input errors. Invalid individual sources/records stay
    in the bundle as rejected evidence so other securities can still run.
    """
    if path is None:
        payload = {"schema_version": SCHEMA_VERSION, "verification_kind": "manual_review", "sources": [], "records": []}
    else:
        evidence_path = Path(path)
        if evidence_path.stat().st_size > 5_000_000:
            raise ValueError("资格导入包超过 5 MB；请仅保留必要状态定位和元数据")
        payload = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "verification_kind", "sources", "records"}:
        raise ValueError("资格证据包必须且只能包含 schema_version、verification_kind、sources、records")
    if payload["schema_version"] != SCHEMA_VERSION or payload["verification_kind"] not in ("manual_review", "offline_test", "automatic_source"):
        raise ValueError("不支持的资格证据版本或验证类型")
    if not isinstance(payload["sources"], list) or not isinstance(payload["records"], list):
        raise ValueError("sources 与 records 必须为列表")
    if len(payload["sources"]) > 100 or len(payload["records"]) > 1000:
        raise ValueError("资格包超出 M2.1 有限核验范围")
    # Validate serializability and stable identity, including rejected raw input.
    content_hash = _digest(payload)
    return {**payload, "bundle_id": f"eligibility-{content_hash}", "content_hash": content_hash}


def _validate_bundle(bundle: dict):
    sources, source_errors = {}, {}
    for raw in bundle.get("sources", []):
        source_id = raw.get("source_id") if isinstance(raw, dict) and isinstance(raw.get("source_id"), str) else None
        try:
            source = EvidenceSource.model_validate(raw)
            if source.source_id in sources or source.source_id in source_errors:
                source_errors[source.source_id] = "来源 ID 重复，不能选择性采用"
                sources.pop(source.source_id, None)
            else:
                sources[source.source_id] = source
        except ValidationError as exc:
            sources.pop(source_id, None)
            source_errors[source_id] = "来源登记不完整：" + str(exc.errors(include_url=False, include_input=False))
    records = []
    counts = {}
    for raw in bundle.get("records", []):
        if isinstance(raw, dict) and isinstance(raw.get("evidence_id"), str):
            counts[raw.get("evidence_id")] = counts.get(raw.get("evidence_id"), 0) + 1
    for raw in bundle.get("records", []):
        try:
            record = EligibilityEvidence.model_validate(raw)
            error = "证据 ID 重复" if counts[record.evidence_id] != 1 else None
        except ValidationError as exc:
            record = None
            error = "证据结构/必要字段不完整：" + str(exc.errors(include_url=False, include_input=False))
        records.append((raw, record, error))
    return sources, source_errors, records


def _record_value(record: EligibilityEvidence, source: EvidenceSource, symbol: str, target: date):
    market = symbol[:2].upper()
    if record.market != market or market not in source.markets:
        return None, "证据市场与证券不匹配"
    if not (record.effective_from <= target <= record.effective_to):
        return None, "证据适用区间不含分析日期"
    if not source.approved_for_local_use:
        return None, "来源本地核验使用权限尚未确认"
    source_host = urlsplit(source.source_url).hostname
    if urlsplit(record.source_url).hostname != source_host or any(urlsplit(page.source_url).hostname != source_host for page in record.pages):
        return None, "证据定位主机与已核对来源不匹配"
    if record.retrieval_status not in ("ok", "empty_confirmed"):
        return None, f"来源请求状态为 {record.retrieval_status}，不能当作空名单"
    if record.parse_status != "ok":
        return None, "来源解析未完成或失败"
    if record.temporal_basis == "unknown":
        return None, "证据历史适用依据未知"
    if record.temporal_basis == "same_date_complete_snapshot":
        if not (record.fetched_at.date() == record.effective_from == record.effective_to == target):
            return None, "当前状态快照不能套用于其它日期"
    elif source.supports_historical_dates is not True:
        return None, "来源未核实支持指定历史区间"
    if record.kind == "security_state":
        if record.assertion_basis != "explicit_statement" or record.delisting_period is None:
            return None, "个股状态没有直接明确证据；名称、ST、上市或可交易不替代证据"
        if record.retrieval_status != "ok":
            return None, "空响应不能支持明确个股状态"
        return record.delisting_period, None
    if record.assertion_basis != "complete_list" or not source.supports_complete_lists:
        return None, "来源未核实提供完整退市整理名单"
    if source.coverage_scope != "all_mainboard_a_shares" or record.complete_scope != "all_mainboard_a_shares":
        return None, "名单不能证明覆盖目标市场全部主板普通 A 股"
    if record.expected_total_records is None or record.total_pages is None:
        return None, "名单缺少总条数或总页数"
    if sorted(page.number for page in record.pages) != list(range(1, record.total_pages + 1)):
        return None, "名单分页不完整或页号重复"
    if any(page.status != "ok" for page in record.pages):
        return None, "名单存在请求或解析失败的页"
    members = [member for page in record.pages for member in page.symbols]
    if len(members) != len(set(members)) or len(members) != record.expected_total_records:
        return None, "名单总数不符或跨页重复，不能证明完整"
    if any(not re.fullmatch(_SYMBOL, member) or member[:2].upper() != market for member in members):
        return None, "名单代码解析异常或包含不匹配市场"
    if not members and record.retrieval_status != "empty_confirmed":
        return None, "空响应尚未明确证实为完整零名单"
    return symbol in members, None


def resolve_eligibility(bundle: dict, symbols: list[str], target_date: date) -> dict[str, dict]:
    """Resolve positive, negative and unknown dated assertions, without ranking.

    Invalid/failed records never become empty negative lists. Conflicting valid
    assertions become unknown; record order cannot pick a preferred source.
    """
    target = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    sources, source_errors, records = _validate_bundle(bundle)
    states = {}
    for symbol in sorted(set(symbols)):
        if not re.fullmatch(_SYMBOL, symbol):
            raise ValueError("证券 ID 必须包含 sh./sz. 和六位代码")
        valid, rejected = [], []
        for raw, record, error in records:
            # A malformed record without an identifiable subject remains visible.
            raw_symbol = raw.get("symbol") if isinstance(raw, dict) else None
            raw_kind = raw.get("kind") if isinstance(raw, dict) else None
            if raw_kind == "security_state" and raw_symbol and raw_symbol != symbol:
                continue
            if error:
                rejected.append({"evidence": raw, "reason": error})
                continue
            source = sources.get(record.source_id)
            if source is None:
                rejected.append({"evidence": raw, "reason": source_errors.get(record.source_id, "证据没有已核对来源登记")})
                continue
            value, reason = _record_value(record, source, symbol, target)
            provenance = {"record": record.model_dump(mode="json"), "source": source.model_dump(mode="json")}
            if reason:
                rejected.append({"evidence": provenance, "reason": reason})
            else:
                valid.append({"value": value, **provenance})
        valid.sort(key=lambda item: item["record"]["evidence_id"])
        rejected.sort(key=_canonical)
        values = {item["value"] for item in valid}
        value = next(iter(values)) if len(values) == 1 else None
        evidence_ids = [item["record"]["evidence_id"] for item in valid]
        if len(values) > 1:
            reason = "适用市场和日期的来源证据相互冲突，保持 unknown"
        elif value is True:
            reason = "独立日期证据确认处于退市整理期"
        elif value is False:
            reason = "经人工核验的直接状态证据或完整日期名单确认不处于退市整理期"
        else:
            reason = UNKNOWN_REASON
        states[symbol] = {
            "delisting_period": value,
            "status": "true" if value is True else "false" if value is False else "unknown",
            "effective_date": target.isoformat(), "evidence_id": _digest(valid) if value is not None else None,
            "evidence_ids": evidence_ids, "reason": reason,
            "evidence": valid, "rejected_evidence": rejected,
            "bundle_id": bundle.get("bundle_id"), "bundle_content_hash": bundle.get("content_hash"),
            "verification_kind": bundle.get("verification_kind", "manual_review"),
            "historical_reconstruction": any(item["record"]["fetched_at"][:10] > target.isoformat() for item in valid),
        }
    return states
