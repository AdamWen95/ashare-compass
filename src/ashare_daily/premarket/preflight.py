"""One immutable morning date-semantics check, isolated from daily business data.

Run explicitly with ``python -m ashare_daily.premarket.preflight --project ROOT``.
It never changes the production switch, schedule, reports, or model budgets.
"""
from datetime import datetime, time
import json
from pathlib import Path

from ..operations.daily import atomic_json, local_path
from .contracts import SHANGHAI, aware
from .timing import universe_budget_seconds

BOARDS = {"sse_main", "star", "szse_main", "chinext"}


def preflight_plan(now, calendar):
    now = aware(now)
    if not time(8, 30) <= now.time() < time(8, 50):
        raise ValueError("outside_premarket_window")
    today = now.date().isoformat()
    if calendar.get(today) is not True:
        raise ValueError("premarket_calendar_not_open_or_unknown")
    previous = sorted(day for day, opened in calendar.items() if day < today and opened is True)
    if not previous:
        raise ValueError("premarket_previous_calendar_missing")
    return {"report_date": today, "master_date": today, "market_as_of_date": previous[-1],
        "started_at": now.isoformat(), "expires_at": now.replace(hour=8, minute=50, second=0, microsecond=0).isoformat()}


def assess_preflight(plan, master, quotes):
    rows = quotes.get("rows", [])
    expected = plan["market_as_of_date"]
    issues = []
    if (master.get("mode") != "research" or master.get("requested_date") != plan["master_date"]
            or master.get("universe_verified") is not True or master.get("collection_ready") is not True):
        issues.append("current_master_unverified")
    if (quotes.get("provenance_mode") != "online" or quotes.get("verification_kind") != "live_network"
            or quotes.get("quote_complete") is not True):
        issues.append("live_quotes_unverified")
    if len(rows) != 4 or {r.get("identity", {}).get("board") for r in rows} != BOARDS or len({r.get("symbol") for r in rows}) != 4:
        issues.append("four_board_sample_incomplete")
    if any(r.get("source_business_date") != expected or r.get("date_verified") is not True or r.get("issues") for r in rows):
        issues.append("prior_close_quote_date_unverified")
    try:
        if not aware(plan["started_at"]) <= aware(quotes["fetched_at"]) < aware(plan["expires_at"]):
            issues.append("observed_outside_premarket_window")
    except (ValueError, KeyError, TypeError):
        issues.append("quote_observation_time_invalid")
    return {"sample_date_semantics_verified": not issues, "issues": issues,
        "sample_only": True, "full_market_verified": False, "report_generated": False,
        "notice": "四板块各一只证券的日期样本；不证明全市场、基准指数、财务、消息或自然09:00任务已验收"}


def run_preflight(project, *, universe_max_seconds=900):
    universe_budget_seconds(0, universe_max_seconds)
    now, project = datetime.now(SHANGHAI), Path(project).resolve()
    if not time(8, 30) <= now.time() < time(8, 50):
        return {"status": "outside_premarket_window", "observed_at": now.isoformat(), "network_requests": 0, "model_calls": 0}
    relative = ".local/premarket_date_acceptance/" + now.date().isoformat()
    directory = local_path(project, relative)
    if directory.exists():
        return {"status": "already_attempted", "network_requests": 0, "model_calls": 0, "result_path": str(directory / "result.json")}
    directory.mkdir(parents=True, exist_ok=False)
    result = {"schema_version": "premarket-date-preflight-v1", "status": "running", "started_at": now.isoformat(),
        "network_requests": 0, "model_calls": 0, "sample_only": True, "full_market_verified": False, "report_generated": False,
        "universe_max_seconds": universe_max_seconds, "quote_time_reserve_seconds": 20}
    atomic_json(directory / "result.json", result)
    provider = None
    try:
        from ..providers.exchange_calendar import ensure_packet, verify_packet
        from ..universe_service import sync_date, load_universe_config
        from ..provider_diagnostics import _read_snapshot, _select_requests
        from ..providers.sina_sectors import SinaSectorProvider
        packet = ensure_packet(now.date(), directory / "calendar", mode="research")
        _, days = verify_packet(json.loads(packet.read_text(encoding="utf-8")), mode="research")
        plan = preflight_plan(now, {d.isoformat(): opened for d, opened in days.items()})
        result["plan"] = plan
        config = json.loads(local_path(project, "config/sse_szse_universe.json").read_text(encoding="utf-8"))
        if config.get("calendar_provider") != "exchange_calendars":
            raise ValueError("preflight_calendar_provider_unreviewed")
        config.update(database=relative + "/universe.sqlite3", output_directory=relative + "/universe",
            calendar_cache=relative + "/calendar", exchange_metadata_cache=relative + "/exchange_cache",
            legacy_market_database=None, max_attempts=1, timeout_seconds=15)
        config_path = directory / "universe_config.json"
        atomic_json(config_path, config)
        def left():
            return max(0, (aware(plan["expires_at"])-datetime.now(SHANGHAI)).total_seconds())
        effective_budget = universe_budget_seconds(left(), universe_max_seconds, reserve=20)
        if effective_budget < 1: raise TimeoutError("preflight_deadline")
        result["effective_universe_seconds"] = effective_budget
        atomic_json(directory / "result.json", result)
        master_result = sync_date(project=project, config_path=config_path, target=now.date(), max_seconds=effective_budget)
        result["network_requests"] = len(master_result.get("source_requests", []))
        result["master_result_path"] = str(Path(master_result["run_directory"]) / "result.json")
        master = _read_snapshot(project, {"scope": "sse_szse_a"}, plan["master_date"], load_universe_config(project, config_path))
        identities = [request.identity for request in _select_requests(master, plan["master_date"], "sse_szse_a")]
        if left() < 2: raise TimeoutError("preflight_deadline")
        policy = json.loads(local_path(project, "config/sector_first.json").read_text(encoding="utf-8"))["sina"]
        provider = SinaSectorProvider(directory / "quotes", mode="research", permission=policy,
            max_requests=1, timeout_seconds=min(15, left()), pause_seconds=0)
        quotes = provider.fetch_quotes(identities, plan["market_as_of_date"])
        atomic_json(directory / "quotes.json", quotes)
        result.update(assess_preflight(plan, master, quotes))
        result["status"] = "sample_verified" if result["sample_date_semantics_verified"] else "partial"
        result["sample_dates"] = [{**{k: r.get(k) for k in ("symbol", "source_business_date", "quote_at", "fetched_at", "issues")},
            "board": r.get("identity", {}).get("board")} for r in quotes.get("rows", [])]
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        result.update(status="failed", error_type=type(exc).__name__, sample_date_semantics_verified=False)
    finally:
        if provider is not None:
            result["network_requests"] += provider.client.calls
            provider.close()
        result["completed_at"] = datetime.now(SHANGHAI).isoformat()
        atomic_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--universe-max-seconds", type=int, default=900)
    args = parser.parse_args()
    outcome = run_preflight(args.project, universe_max_seconds=args.universe_max_seconds)
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["status"] == "sample_verified" else 2)
