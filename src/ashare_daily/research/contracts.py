"""M3 local evidence and model contracts; external material remains untrusted."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


SHANGHAI = ZoneInfo("Asia/Shanghai")
Category = Literal["news", "announcement", "research_report"]
ContentType = Literal["metadata_only", "abstract", "fulltext"]


def aware_time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间必须包含时区，不得伪造当地时间")
    return result.astimezone(SHANGHAI)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceRegistration(Contract):
    source_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    category: Category
    source_url: str
    access_method: str
    usage_limits: str
    enabled: StrictBool = False
    supported_date_range: str = "未确认历史完整覆盖"
    pagination_limits: str = "固定页面，小批量；不代表完整覆盖"
    content_access: list[ContentType] = Field(default_factory=lambda: ["metadata_only"])
    model_use_allowed: StrictBool = False
    cache_allowed: StrictBool = False
    publish_excerpt_allowed: StrictBool = False
    permission_basis: str = "尚未核实，默认关闭"
    checked_at: str | None = None

    @field_validator("source_url")
    @classmethod
    def http_source(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("来源须为不含凭据的 HTTP(S) 原始定位")
        return value

    @field_validator("checked_at")
    @classmethod
    def check_time(cls, value):
        return aware_time(value).isoformat() if value is not None else None


class SecurityAssociation(Contract):
    symbol: str = Field(pattern=r"^(sh|sz)\.\d{6}$")
    name: str = Field(min_length=1)
    basis_quote: str = Field(min_length=1, max_length=1200)
    association_type: Literal["explicit_subject", "business_relationship"]


class Evidence(Contract):
    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    category: Category
    original_url: str
    raw_locator: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=2000)
    published_at: str | None
    publication_precision: Literal["datetime", "minute", "date", "unknown"]
    first_seen_at: str
    fetched_at: str
    effective_from: str | None = None
    effective_to: str | None = None
    content_type: ContentType
    content: str = Field(max_length=1_000_000)
    content_truncated: StrictBool = False
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_version: str = Field(min_length=1)
    original_publisher: str = Field(min_length=1)
    original_published_at: str | None = None
    event_key: str | None = None
    revision_of: str | None = None
    acquisition_mode: Literal["automatic", "manual", "offline_test"]
    security_associations: list[SecurityAssociation] = Field(default_factory=list)

    @field_validator("first_seen_at", "fetched_at")
    @classmethod
    def timestamp(cls, value):
        return aware_time(value).isoformat()

    @field_validator("original_url")
    @classmethod
    def safe_original_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("人工资料也必须保留不含凭据的真实来源网址")
        return value

    @model_validator(mode="after")
    def content_and_dates(self):
        if self.publication_precision == "unknown":
            if self.published_at is not None:
                raise ValueError("未知发布时间不能填入推测日期")
        elif self.published_at is None:
            raise ValueError("已声明精度但缺少发布时间")
        elif self.publication_precision == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.published_at):
                raise ValueError("日期精度仅保存 YYYY-MM-DD")
            date.fromisoformat(self.published_at)
        elif self.publication_precision == "minute":
            parsed = aware_time(self.published_at)
            if parsed.second or parsed.microsecond:
                raise ValueError("分钟精度不能携带已知秒或毫秒")
            self.published_at = parsed.isoformat(timespec="minutes")
        else:
            self.published_at = aware_time(self.published_at).isoformat()
        for value in (self.original_published_at, self.effective_from, self.effective_to):
            if value:
                date.fromisoformat(value) if len(value) == 10 else aware_time(value)
        if self.effective_from and self.effective_to:
            if self.effective_from[:10] > self.effective_to[:10]:
                raise ValueError("适用区间倒置")
        if aware_time(self.first_seen_at) > aware_time(self.fetched_at):
            raise ValueError("首次观察时间不得晚于本次获取时间")
        if self.content_type == "metadata_only":
            if self.content:
                raise ValueError("目录型资料正文必须为空，标题不得冒充已读摘要")
        elif not self.content or self.content.strip() == self.title.strip():
            raise ValueError("标题或空响应不能冒充摘要/全文")
        if self.content_type == "fulltext" and self.content_truncated:
            raise ValueError("截断内容只能标记为摘要，不能冒充全文")
        return self


# All model-output fields are required, with explicit nulls and empty lists.
# This schema can be used with a strict compatible endpoint; it needs no tools.
class Citation(Contract):
    evidence_id: str
    quote: str
    locator: str


class ResearchClaim(Contract):
    claim_id: str
    claim_type: Literal["fact", "inference", "opinion", "counterevidence", "unknown", "followup"]
    text: str
    citations: list[Citation]
    symbol: str | None
    metric_ids: list[str]
    risks: list[str]
    unknowns: list[str]


class ResearchOutput(Contract):
    claims: list[ResearchClaim]


class DailyResearchClaim(ResearchClaim):
    # Risk/unknown discussion belongs in a separately cited claim, avoiding
    # uncited prose hidden in ancillary arrays. Historical schemas stay intact.
    risks: list[str] = Field(max_length=0)
    unknowns: list[str] = Field(max_length=0)


class DailyResearchOutput(Contract):
    claims: list[DailyResearchClaim]
