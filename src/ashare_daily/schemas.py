"""M0 离线演示的数据契约；严格拒绝未定义字段及失效引用。"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEMO_NOTICE = "DEMO / 人工合成数据，仅演示软件流程；不是实际行情、真实新闻或股票分析，不构成投资建议。"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*", mode="before")
    @classmethod
    def validate_date_inputs(cls, value: Any, info: Any) -> Any:
        if info.field_name == "scenario_date":
            if isinstance(value, datetime) or not isinstance(value, (date, str)):
                raise ValueError("演示日期必须为 date 或 YYYY-MM-DD 字符串")
            if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("演示日期字符串必须使用 YYYY-MM-DD 格式")
        return value

    @field_validator("*", mode="after")
    @classmethod
    def normalize_timestamp(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("时间必须包含时区；内部统一使用 Asia/Shanghai")
            return value.astimezone(SHANGHAI)
        return value


SourceStatus = Literal[
    "ok", "empty_confirmed", "stale", "partial", "permission_denied",
    "rate_limited", "schema_changed", "timeout", "unknown",
]


class DataSource(StrictModel):
    provider: Literal["local_synthetic"] = "local_synthetic"
    dataset: str
    status: SourceStatus
    fetched_at: datetime
    parameters: dict[str, str]
    content_version: str
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_mode: Literal["offline_fixture"] = "offline_fixture"
    license_status: Literal["self_authored_synthetic"] = "self_authored_synthetic"
    description: str


class Instrument(StrictModel):
    security_id: str = Field(pattern=r"^DEMO\.(SH|SZ)\.\d{3}$")
    name: str = Field(pattern=r"^DEMO虚构")
    security_type: Literal["synthetic_equity"] = "synthetic_equity"
    exchange: Literal["SH", "SZ"]

    @model_validator(mode="after")
    def validate_exchange(self) -> Instrument:
        if self.security_id.split(".")[1] != self.exchange:
            raise ValueError("证券标识与交易所不一致")
        return self


class Coverage(StrictModel):
    expected_count: int = Field(ge=0)
    covered_count: int = Field(ge=0)
    missing_security_ids: list[str]
    unknown_missing_count: int = Field(ge=0)
    scope_description: str
    market_coverage_verified: Literal[False] = False

    @model_validator(mode="after")
    def validate_counts(self) -> Coverage:
        if self.covered_count > self.expected_count:
            raise ValueError("已覆盖数量不能超过预期数量")
        if self.expected_count - self.covered_count != len(self.missing_security_ids):
            raise ValueError("缺失清单与覆盖数量不一致")
        if self.unknown_missing_count > len(self.missing_security_ids):
            raise ValueError("未知缺失数量不能超过缺失清单")
        return self


class Evidence(StrictModel):
    evidence_id: str
    security_id: str | None = None
    title: str
    provider: Literal["local_synthetic"] = "local_synthetic"
    dataset: str
    fetched_at: datetime
    first_seen_at: datetime
    published_at: datetime | None = None
    published_at_precision: Literal["not_applicable_synthetic"] = "not_applicable_synthetic"
    parameters: dict[str, str]
    content_version: str
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: str
    content_type: Literal["synthetic_fixture"] = "synthetic_fixture"
    status: SourceStatus = "ok"
    frozen_content: str
    is_synthetic: Literal[True] = True

    @model_validator(mode="after")
    def validate_snapshot(self) -> Evidence:
        if self.first_seen_at > self.fetched_at:
            raise ValueError("首次观察时间不能晚于本次获取时间")
        if self.published_at is not None:
            raise ValueError("人工合成资料没有真实原文发布时间，应为 null")
        if hashlib.sha256(self.frozen_content.encode("utf-8")).hexdigest() != self.raw_hash:
            raise ValueError("冻结证据内容与 raw_hash 不一致")
        if not self.content_version.strip():
            raise ValueError("证据必须包含内容版本")
        if not re.fullmatch(r"snapshot\.json#/frozen_report/evidence/\d+", self.locator):
            raise ValueError("演示证据必须定位到本地冻结快照")
        return self


class ComputedMetric(StrictModel):
    metric_id: str
    security_id: str | None = None
    label: str
    value: float | int | None
    unit: Literal["CNY", "shares", "ratio", "count"]
    scenario_date: date
    calculation: str
    evidence_ids: list[str] = Field(min_length=1)
    calculation_version: Literal["m0-demo-metrics-v1"] = "m0-demo-metrics-v1"
    is_synthetic: Literal[True] = True

    @field_validator("value")
    @classmethod
    def finite_metric(cls, value: float | int | None) -> float | int | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("指标不能为 NaN 或无穷大；缺失值应为 null")
        return value

    @model_validator(mode="after")
    def validate_unit_value(self) -> ComputedMetric:
        if self.value is not None and self.unit in {"CNY", "shares", "count"} and self.value < 0:
            raise ValueError("金额、股数和家数不能为负数")
        if self.value is not None and self.unit == "count" and int(self.value) != self.value:
            raise ValueError("家数指标必须为整数")
        return self


class ResearchClaim(StrictModel):
    claim_id: str
    claim_type: Literal["fact", "inference", "opinion"]
    text: str
    security_id: str | None = None
    evidence_ids: list[str]
    metric_ids: list[str]
    risks: list[str]
    unknowns: list[str]

    @model_validator(mode="after")
    def require_fact_evidence(self) -> ResearchClaim:
        if self.claim_type == "fact" and not self.evidence_ids:
            raise ValueError("事实观点必须有可定位证据")
        return self


class ScreeningStrategy(StrictModel):
    strategy_id: Literal["DEMO-TEMPLATE"] = "DEMO-TEMPLATE"
    name: str
    description: str
    selection_mode: Literal["manually_designated_demo"] = "manually_designated_demo"
    rules: list[str]
    exclusions: list[str]
    limitations: list[str]


class CandidateResearch(StrictModel):
    security_id: str
    name: str
    strategy_tags: list[str]
    review_status: Literal["synthetic_demo_only"] = "synthetic_demo_only"
    claims: list[ResearchClaim] = Field(min_length=1)
    metric_ids: list[str]
    evidence_ids: list[str] = Field(min_length=1)
    risks: list[str] = Field(min_length=1)
    observation_conditions: list[str] = Field(min_length=1)


class DailyReport(StrictModel):
    title: Literal["DEMO｜今日方向简报"] = "DEMO｜今日方向简报"
    mode: Literal["demo"] = "demo"
    demo_notice: Literal[DEMO_NOTICE] = DEMO_NOTICE
    scenario_date: date
    trade_date: None = None
    actual_generated_at: datetime
    cutoff_at: datetime
    snapshot_id: str
    status: Literal["complete_within_scope"] = "complete_within_scope"
    scope: str
    coverage: Coverage
    source_health: list[DataSource] = Field(min_length=1)
    instruments: list[Instrument]
    metrics: list[ComputedMetric]
    evidence: list[Evidence]
    market_review: list[ResearchClaim]
    important_news: list[ResearchClaim]
    focus_directions: list[ResearchClaim]
    screening_strategy: ScreeningStrategy
    candidates: list[CandidateResearch] = Field(max_length=10)
    pending_verifications: list[str]
    strategy_version: Literal["m0-demo-template-v1"] = "m0-demo-template-v1"
    prompt_version: None = None
    model_id: None = None

    @model_validator(mode="after")
    def validate_integrity(self) -> DailyReport:
        def unique_map(items: list[Any], key: str) -> dict[str, Any]:
            result = {getattr(item, key): item for item in items}
            if len(result) != len(items):
                raise ValueError(f"重复的 {key}")
            return result

        instruments = unique_map(self.instruments, "security_id")
        metrics = unique_map(self.metrics, "metric_id")
        evidence = unique_map(self.evidence, "evidence_id")
        unique_map(self.candidates, "security_id")
        claims = [*self.market_review, *self.important_news, *self.focus_directions]
        claims.extend(claim for candidate in self.candidates for claim in candidate.claims)
        unique_map(claims, "claim_id")
        if self.cutoff_at > self.actual_generated_at:
            raise ValueError("资料截点不能晚于真实生成时间")
        if self.coverage.covered_count != len(instruments):
            raise ValueError("已覆盖数量与合成证券清单不一致")
        for source in self.source_health:
            if source.fetched_at > self.cutoff_at:
                raise ValueError("数据源获取时间晚于资料截点")
        for index, item in enumerate(self.evidence):
            if item.locator != f"snapshot.json#/frozen_report/evidence/{index}":
                raise ValueError("证据定位与冻结快照中的位置不一致")
            if item.fetched_at > self.cutoff_at:
                raise ValueError("证据必须在资料截点前可获得")
            if item.security_id is not None and item.security_id not in instruments:
                raise ValueError("证据关联不存在的证券")
        for metric in self.metrics:
            if metric.scenario_date != self.scenario_date:
                raise ValueError("指标演示日期与报告不一致")
            if metric.security_id is not None and metric.security_id not in instruments:
                raise ValueError("指标关联不存在的证券")
            self._validate_references(metric.evidence_ids, [], metric.security_id, evidence, metrics)
        for claim in claims:
            if claim.security_id is not None and claim.security_id not in instruments:
                raise ValueError("观点关联不存在的证券")
            self._validate_references(claim.evidence_ids, claim.metric_ids, claim.security_id, evidence, metrics)
        for candidate in self.candidates:
            if candidate.security_id not in instruments:
                raise ValueError("候选证券不存在")
            if candidate.name != instruments[candidate.security_id].name:
                raise ValueError("候选名称与证券主数据不一致")
            self._validate_references(candidate.evidence_ids, candidate.metric_ids, candidate.security_id, evidence, metrics)
            for claim in candidate.claims:
                if claim.security_id != candidate.security_id:
                    raise ValueError("候选观点与候选证券不一致")
        return self

    @staticmethod
    def _validate_references(
        evidence_ids: list[str], metric_ids: list[str], security_id: str | None,
        evidence: dict[str, Evidence], metrics: dict[str, ComputedMetric],
    ) -> None:
        for ids, lookup, kind in [(evidence_ids, evidence, "证据"), (metric_ids, metrics, "指标")]:
            if len(ids) != len(set(ids)):
                raise ValueError(f"重复的{kind}引用")
            for item_id in ids:
                if item_id not in lookup:
                    raise ValueError(f"不存在的{kind}引用: {item_id}")
                linked_security = lookup[item_id].security_id
                if security_id is not None and linked_security is not None and linked_security != security_id:
                    raise ValueError(f"{kind}引用与证券关联不符: {item_id}")
