"""Local market breadth and index metrics from independently verified inputs."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median

from ..sector_selection import confirmed_full_day_halt, quote_issues, symbol_of
from .contracts import decimal, text_number


def _benchmark(packet: dict, market_date: str) -> dict:
    expected = packet.get("expected_dates", [])
    rows = packet.get("records", [])
    if (packet.get("verified") is not True or packet.get("symbol") != "sh.000001"
            or packet.get("security_type") != "index" or packet.get("adjustment_mode") != "index_native"
            or packet.get("price_unit") != "index_points" or len(expected) != 21
            or expected != sorted(set(expected)) or expected[-1] != market_date
            or [r.get("trade_date") for r in rows] != expected
            or any(set(r.get("quality_flags", [])) - {"missing_amount_cny", "missing_volume_shares"} for r in rows)):
        raise ValueError("index_window_unverified")
    closes = [decimal(r.get("close"), positive=True) for r in rows]
    amount = mean_amount = None
    try:
        if any("missing_amount_cny" in r.get("quality_flags", []) for r in rows[-6:]):
            raise ValueError("index_activity_quality_gap")
        amounts = [decimal(r.get("amount_cny"), nonnegative=True) for r in rows[-6:]]
        baseline = sum(amounts[:-1]) / 5
        if baseline > 0:
            amount, mean_amount = text_number(amounts[-1]), text_number(baseline)
    except ValueError:
        pass  # Price evidence survives an independently missing activity series.
    return {"close": text_number(closes[-1]), "ma20": text_number(sum(closes[-20:]) / 20),
            "previous_ma20": text_number(sum(closes[:-1]) / 20),
            "return5": text_number(closes[-1] / closes[-6] - 1),
            "amount": amount, "previous_mean_amount5": mean_amount,
            "activity_status": "ready" if amount is not None else "unavailable",
            "amount_scope": "sse_composite_index", "market_as_of_date": market_date}


def market_snapshot(*, universe: dict, quotes: list[dict], benchmark: dict, market_date: str) -> dict:
    """Do not infer whole-market breadth from the selected industry subset."""
    members = universe.get("members", [])
    issues, changes, halted, missing = [], [], [], []
    if universe.get("structural_verified") is not True:
        issues.append("universe_unverified")
    identities = [m.get("security_id") for m in members]
    if (None in identities or len(identities) != len(set(identities))
            or universe.get("ordinary_a_count") != len(members) or not members
            or any(m.get("metadata_verified") is not True or m.get("security_type") != "ordinary_a"
                   or m.get("exchange") not in {"SSE", "SZSE"} for m in members)):
        issues.append("universe_denominator_invalid")
    lookup = defaultdict(list)
    for row in quotes:
        lookup[row.get("symbol")].append(row)
    for member in members:
        identity = member.get("security_id")
        rows = lookup[symbol_of(member)]
        if confirmed_full_day_halt(member, market_date):
            halted.append(identity)
            continue
        if len(rows) != 1 or quote_issues(rows[0], market_date):
            missing.append(identity)
            continue
        try:
            changes.append(decimal(rows[0]["change_pct"]))
        except ValueError:
            missing.append(identity)
    denominator = len(members) - len(halted)
    if missing or denominator <= 0:
        issues.append("market_cross_section_incomplete")
    breadth_complete = not issues and len(changes) == denominator
    index = None
    try:
        index = _benchmark(benchmark, market_date)
        if index["activity_status"] != "ready":
            issues.append("index_activity_unavailable")
    except (ValueError, KeyError, TypeError):
        issues.append("index_window_unavailable")
    advancing = sum(value > 0 for value in changes)
    complete = not issues and len(changes) == denominator
    return {"status": "ready" if complete else "partial", "market_as_of_date": market_date,
            "scope": "sse_szse_a", "universe_count": len(members), "sample_count": len(changes),
            "halted_count": len(halted), "missing_count": len(missing), "missing_ids": missing,
            "advancing_count": advancing, "flat_count": sum(v == 0 for v in changes),
            "declining_count": sum(v < 0 for v in changes), "denominator": denominator,
            "advancing_fraction": text_number(Decimal(advancing) / denominator) if breadth_complete else None,
            "sample_advancing_fraction": text_number(Decimal(advancing) / len(changes)) if changes else None,
            "median_change_pct": text_number(median(changes)) if breadth_complete else None,
            "components": {"breadth": "ready" if breadth_complete else "unavailable",
                "trend": "ready" if index is not None else "unavailable",
                "activity": index["activity_status"] if index is not None else "unavailable"},
            "benchmark": index, "issues": sorted(set(issues))}
