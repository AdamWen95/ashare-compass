"""Source-bound, local-only reference scores; never change candidate eligibility."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from .sector_selection import digest, verify_selection
from .sector_screening import (
    _raw_prices_valid, _strategy_validation, _timestamp, evaluate_selection,
)

SCHEMA = "reference-strategy-review-v1"
VERSION = "reference-technical-120-v1"
UPSTREAM_COMMIT = "c1bc1797d0c0f314b728f7d9e7639830e87ff0e4"
UPSTREAM_URL = "https://github.com/ktoking/Intelligent-stock-selector/tree/" + UPSTREAM_COMMIT
POINTS = {
    "trend": {0, 12}, "macd": {0, 3, 5}, "stochastic": {0, 3, -4},
    "rsi": {0, 3, -4}, "divergence": {0, 5, -6, -1},
    "volume": {0, 4, -3}, "momentum": {0, 4, -5},
}
LIMITATIONS = [
    "辅助评分试运行，不改变正式候选资格或原有排序；分数不是上涨概率。",
    "只适配参考项目七组技术规则，未复刻其完整模型评分；尚未证明优于原策略。",
    "固定120个交易日计算；KDJ沿参考实现的14日随机指标及3日均值，不是9日KDJ。",
    "未纳入估值、机构观点、期权、当日外部报价及不足完整年度的52周高点加分。",
    "采用已观察的同一复权版本；历史重建、行业入选偏差与缺少历史资格证据限制效果判断。",
]
NUMERIC_INDICATORS = {
    "close", "ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_signal",
    "macd_previous_dif", "macd_previous_signal", "stochastic_k", "stochastic_d",
    "stochastic_j", "rsi14", "volume_ratio20", "return20_pct",
}
BOOLEAN_INDICATORS = {"daily_long_alignment", "macd_golden_cross", "macd_above_zero",
    "macd_bottom", "rsi_bottom", "macd_top", "rsi_top"}


def _sealed(value):
    return {**value, "content_hash": digest(value)}


def _check_seal(value, label):
    if not isinstance(value, dict) or value.get("content_hash") != digest({k: v for k, v in value.items() if k != "content_hash"}):
        raise ValueError("reference_" + label + "_hash_mismatch")


def load_reference_inputs(directory):
    """Read only the named frozen files; never follow source locators here."""
    directory = Path(directory).absolute()
    if any(p.is_symlink() or p.is_junction() for p in (directory, *directory.parents)):
        raise ValueError("reference_frozen_directory_link")
    def read(name, limit):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError("reference_frozen_file_invalid")
        return path.read_bytes()
    manifest = json.loads(read("manifest.json", 500_000))
    _check_seal(manifest, "manifest")
    if manifest.get("schema_version") != "sector-observation-manifest-v1":
        raise ValueError("reference_manifest_schema_mismatch")
    values = []
    for name in ("selection.json", "screening_inputs.json", "observation.json"):
        body = read(name, 256_000_000)
        if hashlib.sha256(body).hexdigest() != manifest.get("files", {}).get(name):
            raise ValueError("reference_frozen_file_hash_mismatch")
        values.append(json.loads(body))
    if manifest.get("observation_content_hash") != values[2].get("content_hash"):
        raise ValueError("reference_manifest_observation_mismatch")
    if (any(manifest.get(key) != values[0].get(key) for key in ("selection_id", "mode"))
            or manifest.get("purpose") != values[0].get("purpose", "production")
            or manifest.get("selection_content_hash") != values[0].get("content_hash")):
        raise ValueError("reference_manifest_selection_mismatch")
    return tuple(values)


def validated_panel(selection, inputs, observation):
    """Revalidate frozen inputs and export dated bars without filling missing days."""
    verify_selection(selection)
    _check_seal(observation, "observation")
    eligibility = observation["eligibility"]
    _check_seal(eligibility, "eligibility")
    if (observation.get("schema_version") != "sector-observation-v1"
            or observation.get("purpose") != selection.get("purpose", "production")
            or observation.get("production_eligible") is not (selection.get("mode") == "research")
            or observation.get("source_cutoff_at") != selection.get("cutoff_at")
            or observation.get("cutoff_at") != eligibility.get("cutoff_at")
            or type(observation.get("historical_reconstruction")) is not bool
            or observation["historical_reconstruction"] != eligibility.get("historical_reconstruction")
            or eligibility.get("selection_content_hash") != selection["content_hash"]
            or _timestamp(observation["cutoff_at"]) < _timestamp(inputs["cutoff_at"])
            or observation["historical_reconstruction"] != (_timestamp(observation["cutoff_at"]).date().isoformat() > selection["target_date"])):
        raise ValueError("reference_observation_metadata_binding_mismatch")
    technical = evaluate_selection(selection, inputs, observation["technical"]["strategy_config"])
    if (technical != observation["technical"]
            or eligibility.get("technical_result_hash") != technical["result_hash"]
            or observation.get("selection_id") != selection["selection_id"]
            or observation.get("selection_content_hash") != selection["content_hash"]
            or observation.get("target_date") != selection["target_date"]
            or observation.get("mode") != selection["mode"]
            or eligibility.get("selection_id") != selection["selection_id"]):
        raise ValueError("reference_frozen_input_binding_mismatch")
    from .sector_observation import combine_observations
    joined = combine_observations(selection, technical, eligibility)
    if any(observation.get(key) != value for key, value in joined.items()):
        raise ValueError("reference_observation_qualification_binding_mismatch")
    dates = inputs["calendar"].get("trading_dates", [])
    if selection["members"] and (inputs["calendar"].get("verified") is not True
            or dates != sorted(set(dates)) or not dates or dates[-1] != selection["target_date"]):
        raise ValueError("reference_calendar_unverified")
    cutoff = _timestamp(inputs["cutoff_at"])
    members = {}
    for member in selection["members"]:
        identity = member["security_id"]
        symbol = ("sh." if member["exchange"] == "SSE" else "sz.") + member["code"]
        packet = inputs["securities"].get(identity, {})
        raw, adjusted, issues, adjustment_issues, _, _, _ = _strategy_validation(member, packet, dates, cutoff)
        problems = sorted(set(issues + adjustment_issues))
        rows = {}
        if not problems:
            for day in dates:
                original, adj = raw.get(day), adjusted.get(day)
                if original is None or adj is None or not _raw_prices_valid(original) or not _raw_prices_valid(adj):
                    continue
                rows[day] = {"trade_date": day, **{key: adj[key] for key in ("close", "high", "low")},
                    "volume_shares": original["volume_shares"], "amount_cny": original.get("amount_cny"),
                    "amount_unit": original.get("amount_unit"), "tradestatus": original.get("tradestatus")}
        members[identity] = {"symbol": symbol, "name": member.get("name"), "rows": rows, "issues": problems}
    return {"dates": dates, "members": members, "source_input_hash": inputs["input_hash"],
        "source_observation_hash": observation["content_hash"]}


def build_reference_review(selection, inputs, observation):
    from .reference_indicators import score_reference_technical
    panel = validated_panel(selection, inputs, observation)
    expected = panel["dates"][-120:]
    records = []
    baseline = {r["security_id"]: r for r in observation["evaluations"]}
    for member in selection["members"]:
        identity = member["security_id"]
        row, source = baseline[identity], panel["members"][identity]
        bars = [source["rows"][day] for day in expected if day in source["rows"]]
        ready = len(expected) == len(bars) == 120 and row["technical_status"] in {"pass", "fail"}
        scored = score_reference_technical(bars) if ready else {
            "status": "unavailable", "score": None, "components": [], "indicators": {},
            "issues": sorted(set(source["issues"] + ["固定120日数据或原技术核验未就绪"]))}
        record = {"security_id": identity, "symbol": row["symbol"], "name": row.get("name"),
            "baseline_technical_status": row["technical_status"], "eligibility_status": row["eligibility_status"],
            **{key: deepcopy(scored[key]) for key in ("status", "score", "components", "indicators", "issues")},
            "window_hash": digest(bars) if ready else None,
            "window_start": expected[0] if ready else None, "window_end": expected[-1] if ready else None}
        if record["status"] != "available":
            record.update(components=[], score=None)
        records.append(record)
    packet = _sealed({"schema_version": SCHEMA, "strategy_version": VERSION, "application": "shadow_only",
        "changes_candidate_ranking": False, "trade_date": selection["target_date"],
        "source_input_hash": panel["source_input_hash"], "source_observation_hash": panel["source_observation_hash"],
        "selection_id": selection["selection_id"], "upstream_commit": UPSTREAM_COMMIT,
        "upstream_url": UPSTREAM_URL, "records": records, "limitations": list(LIMITATIONS),
        "historical_reconstruction": observation["historical_reconstruction"],
        "model_calls": 0, "network_requests": 0})
    validate_reference_review(packet, evaluations=observation["evaluations"], trade_date=selection["target_date"],
        source_observation_hash=observation["content_hash"])
    return packet


def validate_reference_review(packet, *, evaluations, trade_date, source_observation_hash=None):
    """Validate a frozen report attachment, without recomputing or requesting data."""
    _check_seal(packet, "packet")
    if (packet.get("schema_version") != SCHEMA or packet.get("strategy_version") != VERSION
            or packet.get("application") != "shadow_only" or packet.get("changes_candidate_ranking") is not False
            or packet.get("upstream_commit") != UPSTREAM_COMMIT or packet.get("trade_date") != trade_date
            or not isinstance(packet.get("selection_id"), str) or not packet["selection_id"]
            or source_observation_hash is not None and packet.get("source_observation_hash") != source_observation_hash):
        raise ValueError("reference_packet_policy_or_binding_mismatch")
    date.fromisoformat(trade_date)
    for key in ("source_input_hash", "source_observation_hash"):
        if not isinstance(packet.get(key), str) or not re.fullmatch("[0-9a-f]{64}", packet[key]):
            raise ValueError("reference_source_hash_invalid")
    if any(packet.get(key, 0) != 0 for key in ("model_calls", "network_requests")):
        raise ValueError("reference_external_calls_forbidden")
    if not isinstance(packet.get("limitations"), list) or not all(isinstance(v, str) for v in packet["limitations"]):
        raise ValueError("reference_limitations_invalid")
    rows = {row["security_id"]: row for row in evaluations}
    if len(rows) != len(evaluations):
        raise ValueError("reference_baseline_denominator_invalid")
    records = packet.get("records")
    if (not isinstance(records, list) or len(records) != len(rows)
            or not all(isinstance(r, dict) for r in records)
            or {r.get("security_id") for r in records} != set(rows)):
        raise ValueError("reference_denominator_mismatch")
    for record in records:
        row = rows[record["security_id"]]
        if (any(record.get(key) != row.get(key) for key in ("symbol", "name", "eligibility_status"))
                or record.get("baseline_technical_status") != row["technical_status"]):
            raise ValueError("reference_row_binding_mismatch")
        if (not isinstance(record.get("issues"), list) or not all(isinstance(v, str) for v in record["issues"])
                or not isinstance(record.get("indicators"), dict)):
            raise ValueError("reference_record_fields_invalid")
        available = record.get("status") == "available"
        indicators = record["indicators"]
        if (set(indicators) - NUMERIC_INDICATORS - BOOLEAN_INDICATORS
                or available and set(indicators) != NUMERIC_INDICATORS | BOOLEAN_INDICATORS):
            raise ValueError("reference_indicator_fields_invalid")
        numbers = {}
        for name, value in indicators.items():
            if name in BOOLEAN_INDICATORS:
                if type(value) is not bool:
                    raise ValueError("reference_indicator_boolean_invalid")
                continue
            if value is None and not available:
                continue
            try:
                if not isinstance(value, str) or not value or len(value) > 256 or value.strip() != value:
                    raise ValueError()
                parsed = Decimal(value)
                if not parsed.is_finite() or abs(parsed.adjusted()) > 200:
                    raise ValueError()
                numbers[name] = parsed
            except (InvalidOperation, ValueError):
                raise ValueError("reference_indicator_nonfinite_or_invalid") from None
        if record.get("status") == "unavailable":
            if record.get("score") is not None or record.get("components") != [] or not record["issues"]:
                raise ValueError("reference_missing_data_cannot_score")
            continue
        if (record.get("status") != "available" or row["technical_status"] not in {"pass", "fail"}
                or type(record.get("score")) is not int or not 0 <= record["score"] <= 100 or record["issues"]):
            raise ValueError("reference_score_invalid")
        if (any(numbers[key] <= 0 for key in ("close", "ma5", "ma10", "ma20", "ma60"))
                or any(not 0 <= numbers[key] <= 100 for key in ("stochastic_k", "stochastic_d", "rsi14"))
                or numbers["volume_ratio20"] < 0 or numbers["return20_pct"] <= -100
                or indicators["daily_long_alignment"] != (numbers["close"] > numbers["ma5"] > numbers["ma10"] > numbers["ma20"] > numbers["ma60"])
                or indicators["macd_above_zero"] != (numbers["macd_dif"] > 0)
                or indicators["macd_golden_cross"] != (numbers["macd_dif"] > numbers["macd_signal"] and numbers["macd_previous_dif"] <= numbers["macd_previous_signal"])):
            raise ValueError("reference_indicator_value_or_signal_invalid")
        if (not isinstance(record.get("window_hash"), str) or not re.fullmatch("[0-9a-f]{64}", record["window_hash"])
                or record.get("window_end") != trade_date
                or not isinstance(record.get("window_start"), str)
                or date.fromisoformat(record["window_start"]) >= date.fromisoformat(trade_date)):
            raise ValueError("reference_window_invalid")
        components = record.get("components")
        if (not isinstance(components, list) or len(components) != len(POINTS)
                or not all(isinstance(part, dict) for part in components)
                or {part.get("id") for part in components} != set(POINTS)):
            raise ValueError("reference_score_components_invalid")
        for part in components:
            if (type(part.get("points")) is not int or part["points"] not in POINTS[part["id"]]
                    or not isinstance(part.get("label"), str) or not isinstance(part.get("reason"), str)):
                raise ValueError("reference_score_component_value_invalid")
        if record["score"] != max(0, min(100, 50 + sum(part["points"] for part in components))):
            raise ValueError("reference_score_sum_mismatch")
        expected_points = {
            "trend": 12 if indicators["daily_long_alignment"] else 0,
            "macd": 5 if indicators["macd_golden_cross"] else 3 if indicators["macd_above_zero"] else 0,
            "stochastic": 3 if numbers["stochastic_k"] < 20 else -4 if numbers["stochastic_k"] > 80 else 0,
            "rsi": 3 if numbers["rsi14"] < 30 else -4 if numbers["rsi14"] > 70 else 0,
            "divergence": (5 if indicators["macd_bottom"] or indicators["rsi_bottom"] else 0)
                - (6 if indicators["macd_top"] or indicators["rsi_top"] else 0),
            "volume": 4 if numbers["volume_ratio20"] >= Decimal("1.5") else -3 if numbers["volume_ratio20"] < Decimal(".7") else 0,
            "momentum": 4 if numbers["return20_pct"] > 5 else -5 if numbers["return20_pct"] < -8 else 0,
        }
        if any(part["points"] != expected_points[part["id"]] for part in components):
            raise ValueError("reference_score_indicator_mismatch")
