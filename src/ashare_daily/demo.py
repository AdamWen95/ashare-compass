"""纯本地人工合成数据演示；不连接行情源、新闻源或模型。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from importlib.resources import files

import pandas as pd

from .schemas import (
    SHANGHAI, CandidateResearch, ComputedMetric, Coverage, DailyReport, DataSource,
    Evidence, Instrument, ResearchClaim, ScreeningStrategy,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_demo_report(
    scenario_date: date, *, empty_candidates: bool = False,
    generated_at: datetime | None = None,
) -> DailyReport:
    """生成任意场景日期的 DEMO；日期不表示已验证的真实交易日。

    ``generated_at`` 仅供离线测试注入时钟；CLI 始终使用当前真实时间。
    """
    if isinstance(scenario_date, datetime) or not isinstance(scenario_date, date):
        raise ValueError("scenario_date 必须为 date，不能使用 datetime 或字符串")
    now = generated_at if generated_at is not None else datetime.now(SHANGHAI)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("generated_at 必须包含时区")
    now = now.astimezone(SHANGHAI)
    fixture_text = files("ashare_daily").joinpath("fixtures/demo.json").read_text(encoding="utf-8")
    fixture = json.loads(fixture_text)
    rows = fixture["rows"]
    frame = pd.DataFrame(rows)
    # 比例以小数保存；pandas 执行运算，不由研究模板填写行情数值。
    frame["daily_return_ratio"] = (frame["close"] / frame["previous_close"] - 1).round(10)
    parameters = {"scenario_date": scenario_date.isoformat(), "mode": "demo"}
    evidence: list[Evidence] = []

    def add_evidence(evidence_id: str, title: str, content: object, security_id: str | None = None) -> str:
        frozen_content = _canonical_json(content)
        evidence.append(Evidence(
            evidence_id=evidence_id, security_id=security_id, title=title,
            dataset="m0_synthetic", fetched_at=now, first_seen_at=now, published_at=None,
            parameters=parameters, content_version=fixture["fixture_version"],
            raw_hash=_sha256(frozen_content), locator=f"snapshot.json#/frozen_report/evidence/{len(evidence)}",
            frozen_content=frozen_content,
        ))
        return evidence_id

    market_evidence_id = add_evidence("E-MARKET", "DEMO 人工合成行情样本（完整冻结）", {"notice": fixture["notice"], "rows": rows})
    instruments = [Instrument(security_id=row["security_id"], name=row["name"], exchange=row["exchange"]) for row in rows]
    metrics: list[ComputedMetric] = []
    metric_specs = [
        ("close", "DEMO 合成收盘价", "CNY", "人工合成收盘字段，非实际股价"),
        ("daily_return_ratio", "DEMO 合成日变化比例", "ratio", "close / previous_close - 1；保留 10 位小数"),
        ("amount_cny", "DEMO 合成成交额", "CNY", "人工合成 amount_cny 字段，单位元"),
        ("volume_shares", "DEMO 合成成交量", "shares", "人工合成 volume_shares 字段，单位股"),
    ]
    for row in frame.to_dict(orient="records"):
        security_id = row["security_id"]
        row_evidence_id = add_evidence(f"E-{security_id}-BAR", f"{row['name']}：DEMO 人工行情行", {"notice": fixture["notice"], "row": next(item for item in rows if item["security_id"] == security_id)}, security_id)
        for key, label, unit, calculation in metric_specs:
            metrics.append(ComputedMetric(
                metric_id=f"M-{security_id}-{key}", security_id=security_id,
                label=label, value=row[key], unit=unit, scenario_date=scenario_date,
                calculation=calculation, evidence_ids=[row_evidence_id],
            ))
    market_values = [
        ("amount_cny", "DEMO 样本成交额合计", float(frame["amount_cny"].sum()), "CNY", "pandas: amount_cny.sum()"),
        ("volume_shares", "DEMO 样本成交量合计", int(frame["volume_shares"].sum()), "shares", "pandas: volume_shares.sum()"),
        ("advancers", "DEMO 样本上涨家数", int((frame["daily_return_ratio"] > 0).sum()), "count", "pandas: (daily_return_ratio > 0).sum()"),
        ("decliners", "DEMO 样本下跌家数", int((frame["daily_return_ratio"] < 0).sum()), "count", "pandas: (daily_return_ratio < 0).sum()"),
        ("unchanged", "DEMO 样本平盘家数", int((frame["daily_return_ratio"] == 0).sum()), "count", "pandas: (daily_return_ratio == 0).sum()"),
    ]
    for key, label, value, unit, calculation in market_values:
        metrics.append(ComputedMetric(metric_id=f"M-MARKET-{key}", label=label, value=value, unit=unit, scenario_date=scenario_date, calculation=calculation, evidence_ids=[market_evidence_id]))

    important_news: list[ResearchClaim] = []
    candidates: list[CandidateResearch] = []
    for index, event in enumerate(fixture["synthetic_events"], start=1):
        security_id = event["security_id"]
        event_evidence = add_evidence(f"E-{security_id}-EVENT", event["title"], event, security_id)
        important_news.append(ResearchClaim(
            claim_id=f"C-NEWS-{index}", claim_type="fact", text=event["content"],
            security_id=security_id, evidence_ids=[event_evidence], metric_ids=[],
            risks=[event["counterevidence"]], unknowns=["真实公告、新闻和研报均未接入。"],
        ))
        if empty_candidates:
            continue
        row = next(item for item in rows if item["security_id"] == security_id)
        if not row["template_candidate"]:
            continue
        candidate_metric_ids = [f"M-{security_id}-{key}" for key, *_ in metric_specs]
        candidates.append(CandidateResearch(
            security_id=security_id, name=row["name"], strategy_tags=["DEMO 人工指定模板样本"],
            claims=[ResearchClaim(
                claim_id=f"C-CANDIDATE-{index}", claim_type="inference",
                text="【DEMO 推论示例】依据人工设定的业务关联展示观察池结构；本条仅演示推论与事实的区分，没有运行实际选股策略。",
                security_id=security_id, evidence_ids=[event_evidence], metric_ids=candidate_metric_ids,
                risks=[event["counterevidence"]], unknowns=["不存在可核验的真实公司业务信息。"],
            )],
            metric_ids=candidate_metric_ids, evidence_ids=[event_evidence, f"E-{security_id}-BAR"],
            risks=[event["counterevidence"], "人工样本不能用于判断现实证券机会或收益。"],
            observation_conditions=["在后续真实研究中核对有许可的原始公告与证券主体关联。", "核对数据日期、缺失值和业务反面证据后再形成研究结论。"],
        ))

    snapshot_hash = _sha256(_canonical_json({"fixture_hash": _sha256(fixture_text), "scenario_date": scenario_date.isoformat(), "actual_generated_at": now.isoformat(), "empty_candidates": empty_candidates}))
    return DailyReport(
        scenario_date=scenario_date, trade_date=None, actual_generated_at=now, cutoff_at=now,
        snapshot_id=f"demo-{now.strftime('%Y%m%dT%H%M%S%f')}-{snapshot_hash[:12]}",
        scope="仅限本地人工合成证券样本；complete_within_scope 仅表示演示样本结构完整，未验证真实市场覆盖。",
        coverage=Coverage(expected_count=len(rows), covered_count=len(rows), missing_security_ids=[], unknown_missing_count=0, scope_description="人工合成样本，不代表沪深 A 股全集或某行业。"),
        source_health=[DataSource(dataset="m0_synthetic", status="ok", fetched_at=now, parameters=parameters, content_version=fixture["fixture_version"], raw_hash=_sha256(fixture_text), description="本地自编合成 fixture 读取成功；没有执行真实接口、新闻检索或模型请求。")],
        instruments=instruments, metrics=metrics, evidence=evidence,
        market_review=[ResearchClaim(
            claim_id="C-MARKET-1", claim_type="fact",
            text="【DEMO 样本事实】下列成交额与涨跌分布由程序计算，统计对象仅为本地虚构样本；未获取主要指数，不能据此描述真实市场。",
            evidence_ids=[market_evidence_id], metric_ids=[f"M-MARKET-{key}" for key, *_ in market_values],
            risks=["没有实际行情或全市场覆盖。"], unknowns=["真实交易日历、指数表现及行业分类均未验证。"],
        )],
        important_news=important_news,
        focus_directions=[ResearchClaim(
            claim_id="C-DIRECTION-1", claim_type="opinion",
            text="【DEMO 观点示例】人工设定“流程设备”和“节能部件”两个方向，用来展示证据、反证与未知项的组织方式。方向名称不是当日市场热点判断。",
            evidence_ids=[item.evidence_ids[0] for item in important_news], metric_ids=[],
            risks=["合成事件没有现实依据，未验证行业内扩散或分歧。"], unknowns=["真实政策、行业数据和公司业务联系未核验。"],
        )],
        screening_strategy=ScreeningStrategy(
            name="DEMO 人工指定候选模板", description="M0 只演示研究报告结构，按 fixture 标记列出模板样本；均线、相对收益、策略排名等筛选功能尚未实现。",
            rules=["只展示带 DEMO 标记的人工合成证券。", "候选来自模板中的人工标记，不代表通过量价选股规则。", "每条候选关联冻结证据、程序指标、风险和待验证条件。"],
            exclusions=["拒绝不存在的证券、证据或指标引用。", "候选最多 10 条；允许完全无候选。"],
            limitations=["没有运行 M2 选股策略。", "没有完成公告、新闻或研报核查。"],
        ),
        candidates=candidates,
        pending_verifications=[
            "M0 完全离线：真实行情、新闻、公告、研报与模型 API 均未接入或验证。",
            "scenario_date 只是演示标签；trade_date 为 null，未验证该日是否交易日或存在当日行情。",
            "证据 first_seen_at/fetched_at 使用本次真实读取时间，published_at 为 null；演示日期不用于伪造历史可用时间。",
            "成交额以元、成交量以股、比例以小数保存；没有验证真实供应商的字段或单位转换。",
            "未实现真实选股筛选、自动调度、网络调用或外部模型评审。",
        ],
    )
