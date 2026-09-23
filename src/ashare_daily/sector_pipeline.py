"""Manual F2-S1 orchestration. Frozen industry inputs, no model or screening calls."""
from __future__ import annotations

from copy import deepcopy
import csv
from datetime import date, datetime, time
import hashlib
import io
import json
from pathlib import Path
import re
import time as elapsed
from uuid import uuid4

from .market_schemas import SHANGHAI
from .operations.daily import atomic_json, local_path, resolve_times, read_runs
from .provider_diagnostics import _cached_calendar, _permissions_for_inputs, _read_snapshot
from .sector_selection import digest, evaluate, parameters, verify_selection, quote_issues, symbol_of
from .universe_service import load_universe_config


def now_iso():
    return datetime.now(SHANGHAI).isoformat()


def permission_allowed(config):
    policy = config.get("sina", {})
    return (policy.get("enabled") is True and policy.get("user_authorized") is True
        and policy.get("permission_status") == "approved"
        and policy.get("permitted_storage") is True and policy.get("permitted_automated_access") is True
        and policy.get("purpose") == "personal_noncommercial_local_research" and policy.get("llm_export") is False)


def load_config(root, config_path):
    root = Path(root).resolve()
    config = json.loads(local_path(root, config_path).read_text(encoding="utf-8-sig"))
    if (config.get("schema_version") != "f2s1-config-v1" or config.get("market_scope") != "sse_szse_a"
            or config.get("research_mode") != "sector_first" or config.get("model_calls") != 0
            or config.get("target_trading_days") != 320 or config.get("taxonomy") not in {"sina_industry", "exchange_industry_section_v1"}
            or config.get("themes_enabled") is not False):
        raise ValueError("sector_config_scope_rule_or_budget_invalid")
    parameters(config.get("parameters"))
    for key in ("database", "calendar_cache", "output_directory", "universe_config"):
        local_path(root, config[key])
    if not 0 < config.get("max_run_seconds", 0) <= 14400:
        raise ValueError("sector_runtime_limit_invalid")
    return config


def write_new(path, value):
    path = Path(path)
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError("immutable_sector_artifact_conflict")
    else:
        with path.open("xb") as stream:
            stream.write(body)
    return hashlib.sha256(body).hexdigest()


def selection_directory(root, config, selection_id):
    if not re.fullmatch(r"sector-\d{4}-\d{2}-\d{2}-[a-f0-9]{20}", selection_id):
        raise ValueError("invalid_selection_id")
    return local_path(Path(root), config["output_directory"]) / selection_id


def verify_http_evidence(root, packets):
    from .operations.paths import resolve_archived_path
    from .operations.backup import _io
    verified, http_checked, resolved = {}, set(), {}
    def resolve_once(raw_path):
        # An industry directory can repeat one frozen source across thousands
        # of memberships. Resolve its relocation once per verification call;
        # the expected hash conflict and the file/body SHA checks remain below.
        if raw_path not in resolved:
            resolved[raw_path] = local_path(Path(root), resolve_archived_path(raw_path, anchor=Path(root)))
        return resolved[raw_path]
    for packet in packets:
        for reference in packet.get("file_refs", []):
            path = resolve_once(reference["path"])
            if str(path) in verified:
                if verified[str(path)] != reference["sha256"]:
                    raise ValueError("source_file_reference_hash_conflict")
                continue
            if hashlib.sha256(_io(path).read_bytes()).hexdigest() != reference["sha256"]:
                raise ValueError("exchange_industry_evidence_file_hash_mismatch")
            verified[str(path)] = reference["sha256"]
        for reference in packet.get("evidence", []):
            raw_path, expected = reference.get("path"), reference.get("sha256")
            if not raw_path or not expected:
                raise ValueError("http_evidence_locator_or_hash_missing")
            path = resolve_once(raw_path)
            if str(path) in verified and verified[str(path)] != expected:
                raise ValueError("source_file_reference_hash_conflict")
            if str(path) in http_checked:
                continue
            raw = _io(path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("http_evidence_file_hash_mismatch")
            response = json.loads(raw)
            if response.get("verification_kind") != "live_network" or response.get("provenance_mode") != "online":
                raise ValueError("http_evidence_live_provenance_missing")
            body_path = resolve_once(response["body_path"])
            if hashlib.sha256(_io(body_path).read_bytes()).hexdigest() != response.get("body_sha256"):
                raise ValueError("http_response_body_hash_mismatch")
            verified[str(path)] = expected
            http_checked.add(str(path))
    return len(verified)


def revalidate_dated_quote_archive(root, packet, universe, target):
    """Reparse immutable HTTP bytes with a declared rule version, never edit old observations."""
    from .providers.sina_sectors import parse_quotes, QUOTE_BASE
    from .operations.paths import resolve_archived_path
    from .operations.backup import _io
    verify_http_evidence(root, [packet])
    members = {symbol_of(m): m for m in universe["members"] if m.get("security_type") == "ordinary_a"}
    latest, references = {}, packet.get("evidence", [])
    for reference in references:
        response = json.loads(_io(resolve_archived_path(reference["path"], anchor=root)).read_text(encoding="utf-8"))
        if not response["url"].startswith(QUOTE_BASE):
            raise ValueError("unexpected_quote_archive_endpoint")
        requested = response["url"][len(QUOTE_BASE):].split(",")
        if any(symbol not in members for symbol in requested):
            raise ValueError("quote_archive_scope_identity_mismatch")
        if not response.get("ok"):
            continue
        rows, missing = parse_quotes(response, [members[symbol] for symbol in requested], target)
        for row in rows:
            old = latest.get(row["symbol"])
            if not old or row["fetched_at"] >= old["fetched_at"]:
                latest[row["symbol"]] = row
    rows = [latest[symbol] for symbol in sorted(members) if symbol in latest]
    boundary = len(rows) == len(members)
    complete = boundary and all(not quote_issues(row, target) for row in rows)
    result = {**deepcopy(packet), "rows": rows, "complete": complete, "boundary_verified": boundary,
        "status": "ok" if complete else "blocked", "quote_complete": complete,
        "expected_count": len(members), "missing_symbols": sorted(set(members)-set(latest)),
        "source_business_date": target if rows and all(row.get("trade_date") == target and row.get("date_verified") for row in rows) else None,
        "issues": [] if complete else ["quote_field_date_or_response_gap"],
        "normalization_version": "sina-dated-light-quote-v2", "revalidated_at": now_iso(),
        "verification_kind": "archived_live_response_revalidation", "network_requests_this_validation": 0,
        "data_kind": "dated_lightweight_quote_observation_not_final_daily_bar"}
    # Re-parsing raw Sina bytes removes derived status fields. Revalidate the
    # independent original evidence before restoring them; metadata alone is not proof.
    overlays = result.pop("status_evidence_overlays", [])
    if overlays:
        from .providers.sector_status import apply_status_evidence
        for overlay in overlays:
            result = apply_status_evidence(root, universe, result, target,
                local_path(root, resolve_archived_path(overlay["result_path"], anchor=root)))
    return result


def read_selection(root, config, selection_id):
    directory = selection_directory(root, config, selection_id)
    result = verify_selection(json.loads((directory / "sector_selection.json").read_text(encoding="utf-8")))
    if result["selection_id"] != selection_id:
        raise ValueError("selection_directory_identity_mismatch")
    if result.get("mode") != "research":
        raise ValueError("test_selection_rejected")
    if result.get("purpose", "production") != "production" or result.get("production_eligible") is False:
        raise ValueError("production_rejects_engineering_validation")
    frozen_config = json.loads((directory / "frozen_config.json").read_text(encoding="utf-8"))
    if result["config_hash"] != digest(frozen_config):
        raise ValueError("frozen_config_hash_mismatch")
    source = json.loads((directory / "source_inputs.json").read_text(encoding="utf-8"))
    for key in ("catalog", "memberships", "quotes", "calendar"):
        if digest(source[key]) != result["evidence_hashes"][key]:
            raise ValueError("frozen_source_input_hash_mismatch")
    from .universe import _json
    universe = source["universe"]
    universe_body = {key: value for key, value in universe.items() if key not in {"snapshot_id", "content_hash"}}
    if (universe.get("snapshot_id") != result["universe_snapshot_id"]
            or universe.get("content_hash") != result["universe_content_hash"]
            or hashlib.sha256(_json(universe_body).encode()).hexdigest() != universe.get("content_hash")):
        raise ValueError("frozen_universe_evidence_hash_mismatch")
    verify_http_evidence(root, [source["catalog"], source["quotes"], *source["memberships"].values()])
    return result, frozen_config


def archive_selection(root, config, value, membership_rows, source_inputs):
    directory = selection_directory(root, config, value["selection_id"])
    write_new(directory / "sector_selection.json", value)
    write_new(directory / "frozen_config.json", config)
    write_new(directory / "source_inputs.json", source_inputs)
    stream = io.StringIO(newline="")
    fields = ["sector_id", "taxonomy", "sector_name", "symbol", "security_id", "code", "exchange",
        "listing_board", "security_type", "mapping_status", "reason", "metadata_source", "raw"]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in membership_rows:
        writer.writerow({**row, "raw": json.dumps(row["raw"], ensure_ascii=False, sort_keys=True)})
    body = stream.getvalue().encode("utf-8-sig")
    path = directory / "sector_membership.csv"
    if path.exists() and path.read_bytes() != body:
        raise ValueError("frozen_membership_conflict")
    if not path.exists():
        path.write_bytes(body)
    changes_path = directory / "selection_changes.json"
    if not changes_path.exists():
        previous = []
        for candidate in directory.parent.glob("sector-*/sector_selection.json"):
            if candidate.parent == directory:
                continue
            old = verify_selection(json.loads(candidate.read_text(encoding="utf-8")))
            if old.get("selection_verified") and old["target_date"] <= value["target_date"]:
                previous.append(old)
        old = max(previous, key=lambda row: (row["target_date"], row["cutoff_at"], row["selection_id"]), default=None)
        before = {m["security_id"] for m in old["members"]} if old else set()
        current = {m["security_id"] for m in value["members"]}
        write_new(changes_path, {"schema_version": "f2s1-selection-change-v1", "selection_id": value["selection_id"],
            "previous_selection_id": old["selection_id"] if old else None,
            "change_kind": "research_selection_change", "entered": sorted(current-before), "left": sorted(before-current),
            "retained": sorted(current & before), "left_history_policy": "retained_not_deep_fetched",
            "not_a_delisting_or_universe_scope_change": True})
    if not value["members"]:
        write_new(directory / "history_fetch_plan.json", {"schema_version": "f2s1-empty-history-plan-v1",
            "selection_id": value["selection_id"], "selection_content_hash": value["content_hash"],
            "status": "not_required" if value["selection_verified"] else "selection_blocked",
            "tasks": [], "history_requests_outside_selection": 0, "model_calls": 0})
    return directory


def collect_inputs(root, config, target, evidence_directory, *, max_seconds, source_run=None, reuse_only=False):
    """Only catalog, memberships and ONE-DAY quotes; no history import here."""
    from .providers.sina_sectors import SinaSectorProvider
    began = elapsed.monotonic()
    if reuse_only and not source_run:
        raise ValueError("reuse_only_requires_frozen_source_run")
    universe_config = load_universe_config(root, config["universe_config"])
    _permissions_for_inputs(universe_config)
    try:
        universe = _read_snapshot(root, {"scope": config["market_scope"]}, target, universe_config)
    except ValueError as exc:
        if str(exc) != "target_universe_snapshot_missing" or target != datetime.now(SHANGHAI).date().isoformat():
            raise
        # Maintain the existing dynamic universe, never create a second stock pool.
        from .universe_service import sync_date
        stamp = datetime.now(SHANGHAI)
        sync_date(project=root, config_path=config["universe_config"], target=date.fromisoformat(target),
                  cutoff_at=stamp.isoformat(), now=stamp)
        universe = _read_snapshot(root, {"scope": config["market_scope"]}, target, universe_config)
    calendar = _cached_calendar(root, [config["calendar_cache"], universe_config.calendar_cache], target)
    if not calendar.get("calendar_verified") or calendar.get("calendar", {}).get(target) is not True:
        raise ValueError("calendar_unverified_no_weekday_fallback")
    cached = None
    if source_run:
        old_directory = local_path(root, source_run)
        expected_parent = local_path(root, config["output_directory"]) / "source_runs"
        if old_directory.parent != expected_parent:
            raise ValueError("source_resume_directory_outside_registered_runs")
        cached = json.loads((old_directory / "source_inputs.json").read_text(encoding="utf-8"))
        if cached["universe"] != universe or cached["calendar"] != calendar:
            raise ValueError("source_resume_universe_or_calendar_changed")
        verify_http_evidence(root, [cached["catalog"], cached["quotes"], *cached["memberships"].values()])
        cached["quotes"] = revalidate_dated_quote_archive(root, cached["quotes"], universe, target)
    source = SinaSectorProvider(evidence_directory, mode="research", permission=config["sina"],
        timeout_seconds=config["timeout_seconds"], pause_seconds=config["pause_seconds"])
    source.client.deadline_monotonic = began + max_seconds
    try:
        if config["taxonomy"] == "exchange_industry_section_v1":
            from .providers.exchange_industry import exchange_industry_snapshot
            exchange = exchange_industry_snapshot(root, universe)
            catalog, memberships = exchange["catalog"], exchange["memberships"]
        else:
            same_taxonomy = cached and cached["catalog"].get("taxonomy") == config["taxonomy"]
            catalog = cached["catalog"] if same_taxonomy and cached["catalog"].get("complete") else source.fetch_catalog(taxonomy=config["taxonomy"])
            memberships = deepcopy(cached.get("memberships", {})) if same_taxonomy else {}
        for sector in catalog.get("rows", []):
            if sector.get("kind") != "industry":
                continue
            prior = memberships.get(sector["sector_id"], {})
            if prior.get("complete"):
                continue
            if reuse_only:
                continue
            if config["taxonomy"] == "exchange_industry_section_v1":
                continue  # an unverified exchange crosswalk cannot fall back to Sina's other taxonomy
            if elapsed.monotonic() - began >= max_seconds:
                break
            memberships[sector["sector_id"]] = source.fetch_members(sector)
            # Archive completed pages before another sector or a possible interruption.
            write_new(evidence_directory / ("members-" + digest(sector["sector_id"])[:20]+".json"), memberships[sector["sector_id"]])
            if getattr(source, "blocked", False):
                break
        identities = [m for m in universe["members"] if m.get("security_type") == "ordinary_a"
                      and m.get("metadata_verified") and m.get("exchange") in {"SSE", "SZSE"}]
        if reuse_only:
            quotes = cached["quotes"]
        elif cached and cached["quotes"].get("complete") and all(
                not quote_issues(row, target) for row in cached["quotes"]["rows"]):
            quotes = cached["quotes"]
        elif elapsed.monotonic() - began < max_seconds and catalog.get("complete"):
            old_rows = {row["symbol"]: row for row in cached["quotes"].get("rows", [])} if cached else {}
            required = [m for m in identities if symbol_of(m) not in old_rows or quote_issues(old_rows[symbol_of(m)], target)]
            fresh = source.fetch_quotes(required, target)
            if cached:
                combined = {**old_rows, **{row["symbol"]: row for row in fresh["rows"]}}
                rows = [combined[symbol_of(m)] for m in identities if symbol_of(m) in combined]
                boundary = len(rows) == len(identities)
                good = boundary and all(not quote_issues(row, target) for row in rows)
                quotes = {**fresh, "rows": rows, "complete": good, "boundary_verified": boundary,
                    "status": "ok" if good else "blocked", "target_date": target,
                    "expected_count": len(identities), "quote_complete": good,
                    "missing_symbols": sorted({symbol_of(m) for m in identities}-set(combined)),
                    "source_business_date": target if rows and all(row.get("trade_date") == target and row.get("date_verified") for row in rows) else None,
                    "linked_batches": {"cached": cached["quotes"].get("batches", []), "fresh": fresh.get("batches", [])},
                    "batches": [], "requested_symbols_this_run": [symbol_of(m) for m in required],
                    "evidence": cached["quotes"].get("evidence", [])+fresh.get("evidence", []),
                    "cache_reused_symbols": [m["security_id"] for m in identities if m not in required]}
            else:
                quotes = fresh
        else:
            quotes = deepcopy(cached["quotes"]) if cached else {"rows": [], "status": "pending", "complete": False,
                "provenance_mode": "online", "issues": ["lightweight_runtime_or_source_limit"]}
        if config.get("status_evidence_paths"):
            from .providers.sector_status import apply_status_evidence
            for status_path in config["status_evidence_paths"]:
                quotes = apply_status_evidence(root, universe, quotes, target, local_path(root, status_path))
        packet = {"universe": universe, "catalog": catalog, "memberships": memberships,
            "quotes": quotes, "calendar": calendar, "started_at": getattr(source, "started_at", None),
            "completed_at": now_iso(), "elapsed_seconds": round(elapsed.monotonic()-began, 3),
            "resumed_from_source_run": str(source_run) if source_run else None,
            "reuse_only": reuse_only,
            "network_requests_this_run": source.client.calls}
        references = {r["path"]: r for p in (catalog, quotes, *memberships.values()) for r in p.get("evidence", [])}
        write_new(evidence_directory / "source_request_manifest.json", {"schema_version": "f2s1-source-requests-v1",
            "environment": "workstation-network-enabled", "target_date": target, "model_calls": 0,
            "network_requests_this_run": source.client.calls, "reused_request_evidence": len(references)-source.client.calls,
            "source_evidence_count": len(references), "response_bytes": sum(r.get("bytes", 0) for r in references.values()),
            "elapsed_seconds_this_run": packet["elapsed_seconds"], "retries": 0, "history_requests": 0,
            "requests": list(references.values())})
        write_new(evidence_directory / "source_inputs.json", packet)
        return packet
    finally:
        source.close()


def report_readiness(root, config, selection, history=None):
    directory = selection_directory(root, config, selection["selection_id"])
    if history is None and (directory / "latest_history.json").is_file():
        from .sector_history import report_history
        history = report_history(root, selection, config)
    history = history or {}
    members = selection["members"]
    unknown = sum(any(m.get("statuses", {}).get(k, {}).get("value") is None
                       for k in ("st", "suspended", "delisting_period")) for m in members)
    if not selection["selection_verified"]:
        status = "selection_blocked"
    elif not members:
        status = "no_matching_sectors"
    else:
        status = history.get("status", "history_pending")
    if status == "technical_data_ready_qualification_pending":
        status = "technical_ready_risk_pending"
    # Technical data does not certify ST/delisting checks or formal qualification.
    if unknown and status in {"ready_for_screening", "complete", "technical_ready"}:
        status = "technical_ready_risk_pending"
    metrics = history.get("metrics", {})
    counts = {"catalog": selection["catalog_count"], "valid_ranking": selection["valid_ranking_count"],
        "preselected": selection["preselected_count"], "selected": selection["selected_count"],
        "raw_memberships": selection["raw_membership_count"],
        "selected_raw_memberships": selection["selected_raw_membership_count"], "selected_securities": len(members),
        "cache_reused": history.get("cache_reused_count"), "history_fetched": history.get("history_fetched_count"),
        "history_ready": history.get("history_ready_count"), "adjustment_ready": history.get("adjustment_ready_count"),
        "risk_unknown": unknown, "pending": history.get("pending_count")}
    if not members:
        counts.update(cache_reused=0, history_fetched=0, history_ready=0, adjustment_ready=0, pending=0)
    generated = now_iso()
    report = {"schema_version": "f2s1-readiness-v1", "title": "沪深 A 股·板块精选研究",
        "target_date": selection["target_date"], "selection_id": selection["selection_id"],
        "market_scope": selection["market_scope"], "research_mode": "sector_first", "status": status,
        "generated_at": generated, "cutoff_at": selection["cutoff_at"], "counts": counts,
        "universe": {"snapshot_id": selection["universe_snapshot_id"], "ordinary_a_count": selection["universe_count"],
            "board_counts": selection["universe_board_counts"], "observed_at": selection["universe_observed_at"],
            "source_business_dates": selection["universe_source_business_dates"],
            "maintenance_state": "verified_snapshot_reused", "current_date_resync": "not_performed"},
        "sector_selection": {k: selection[k] for k in ("taxonomy", "themes_enabled", "selection_verified", "blockers",
            "industry_comparison_complete", "time_basis", "historical_reconstruction", "sectors")},
        "history": history, "metrics": metrics,
        "not_requested_by_design": selection["universe_count"]-len(members) if selection["universe_count"] is not None else None,
        "history_requests_outside_selection": metrics.get("history_requests_outside_selection", 0),
        "full_market_history_complete": False, "formal_qualification_verified": False,
        "model_calls": 0, "not_run": ["F3_individual_screening", "F4_model_research", "deployment", "scheduling"],
        "limitations": [selection["scope_notice"], "当日量价观察，不是未来上涨预测；成交额不是资金净流入。",
            "上海源业务日期仍为null；保留观察日期，不将当前成分冒充历史当时成分。",
            "行业强度使用日期可核验的收盘轻量报价；盘后固定价格交易成交额是否计入尚未确认，不称最终完整日线。",
            "单日统计不包含5/20日行业趋势；政策、新闻、公告和主营关联尚未核查。",
            "风险unknown不等于正常；120日和近20日均成交额5000万元门槛保持，未运行正式候选筛选。",
            "使用用户确认的个人非商业本地研究用途；不把新浪网页端点称为对外授权官方API。"]}
    report["sector_selection"].update(raw_catalog_count=selection.get("raw_catalog_count"),
        catalog_mapped_security_count=selection.get("catalog_mapped_security_count"),
        unassigned_universe_count=selection.get("unassigned_universe_count"))
    report["board_coverage"] = []
    details = history.get("details", [])
    for board in ("sse_main", "szse_main", "chinext", "star"):
        board_members = [m for m in members if m["board"] == board]
        board_details = [row for row in details if row["board"] == board]
        has_result = len(board_details) == len(board_members)
        halted = sum(bool(row.get("confirmed_suspended")) for row in board_details)
        expected = len(board_members)-halted
        valid = sum(bool(row.get("valid_target_quote")) for row in board_details)
        report["board_coverage"].append({"board": board, "selected_securities": len(board_members),
            "confirmed_full_day_halted": halted, "expected_quotes": expected, "valid_quotes": valid if has_result else None,
            "missing_quotes": expected-valid if has_result else expected,
            "history_ready": sum(bool(row.get("raw_facts_ready")) for row in board_details),
            "adjustment_ready": sum(bool(row.get("adjustment_ready")) for row in board_details),
            "risk_unknown": sum(any(m.get("statuses", {}).get(k, {}).get("value") is None
                                    for k in ("st", "suspended", "delisting_period")) for m in board_members),
            "coverage": valid/expected if has_result and expected else None})
    labels = {"catalog": "行业目录", "valid_ranking": "可参与排名", "preselected": "预关注行业", "selected": "关注行业",
        "raw_memberships": "目录原始成分关联", "selected_raw_memberships": "关注行业原始关联", "selected_securities": "去重S",
        "cache_reused": "缓存复用证券", "history_fetched": "实际补历史证券", "history_ready": "历史就绪", "adjustment_ready": "复权就绪",
        "risk_unknown": "风险状态未知", "pending": "历史待处理"}
    lines = ["# 沪深 A 股·板块精选研究", "", "暂不含北交所；不覆盖关注板块以外的个股机会。", "",
        f"行情目标日：{selection['target_date']}；资料截点：{selection['cutoff_at']}；实际生成：{generated}",
        f"状态：{status}；冻结选择：{selection['selection_id']}", "", "| 项目 | 数量 |", "|---|---:|"]
    lines.extend(f"| {labels[key]} | {value if value is not None else '待核验'} |" for key, value in counts.items())
    reasons_zh = {"nonpositive_daily_change_or_amount": "平均涨跌幅或成交额未过正值门槛",
        "passed_daily_observation_rule": "通过当日观察规则", "outside_selected_rank_limit": "超出关注排名上限",
        "outside_preselection_rank_limit": "超出预关注排名上限", "median_or_breadth_below_gate": "中位数或上涨占比未达标",
        "complete_dated_members_required_for_aggregate": "成分或日期行情尚未完整核验",
        "no_in_scope_members": "无范围内成分", "selection_inputs_blocked": "前置数据阻塞"}
    def shown(value, percentage=False):
        if value is None:
            return "—"
        return f"{value * 100:.2f}%" if percentage else f"{value:.4f}" if isinstance(value, float) else str(value)
    lines += ["", "## 行业选择依据", "", "| 行业 | 初排 | 关注排名 | 平均涨跌幅% | 中位涨跌幅% | 上涨占比 | 沪深成分 | 原因 |",
        "|---|---:|---:|---:|---:|---:|---:|---|"]
    for sector in selection["sectors"]:
        lines.append("| " + " | ".join(shown(sector.get(k), k == "advancing_fraction").replace("|", "\\|") for k in
            ("name", "pre_rank", "selected_rank", "ranking_change_pct", "median_change_pct", "advancing_fraction", "scope_member_count"))
            + " | " + "；".join(reasons_zh.get(reason, reason) for reason in sector["reasons"]) + " |")
    lines += ["", "## 范围及限制", ""] + ["- " + s for s in report["limitations"]]
    lines += ["", "## S内逐上市板块就绪（非行业分类）", "", "| 上市板块 | S | 应有行情 | 有效行情 | 缺失 | 历史就绪 | 复权就绪 | 风险未知 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["board_coverage"]:
        lines.append("| " + " | ".join(str(row[k]) if row[k] is not None else "待核验" for k in
            ("board", "selected_securities", "expected_quotes", "valid_quotes", "missing_quotes", "history_ready", "adjustment_ready", "risk_unknown")) + " |")
    lines += ["", f"S外长历史请求：{report['history_requests_outside_selection']}；未关注证券：按设计未请求。",
        "名单完整、S内行情完整、研究资格完整分别判断；本报告不表示沪深全市场行情已完成。", "",
        "## 后续入口", "", f"`python -m ashare_daily sector resume --selection {selection['selection_id']}`", "",
        "本轮止于F2-S1；模型调用0，未修改预算或启用调度。", ""]
    report_dir = directory / "reports" / (datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")+"-"+uuid4().hex[:8])
    report_dir.mkdir(parents=True)
    json_hash = write_new(report_dir / "sector_data_readiness.json", report)
    markdown = "\n".join(lines).encode()
    (report_dir / "sector_data_readiness.md").write_bytes(markdown)
    manifest = {"schema_version": "f2s1-readiness-manifest-v1", "selection_id": selection["selection_id"],
        "files": {"sector_data_readiness.json": json_hash, "sector_data_readiness.md": hashlib.sha256(markdown).hexdigest()}}
    manifest_hash = write_new(report_dir / "manifest.json", manifest)
    atomic_json(directory / "latest_readiness.json", {"selection_id": selection["selection_id"],
        "report_directory": report_dir.relative_to(root).as_posix(), "manifest_sha256": manifest_hash})
    return {**report, "report_path": str(report_dir / "sector_data_readiness.md"), "json_path": str(report_dir / "sector_data_readiness.json")}


def run_sector(*, root, config_path="config/sector_first.json", operation="run", target=None,
               selection_id=None, cutoff=None, dry_run=False, max_seconds=None, source_run=None, invocation_time=None,
               reuse_only=False, status_evidence=None):
    root = Path(root).resolve()
    config = load_config(root, config_path)
    if status_evidence:
        config["status_evidence_paths"] = [local_path(root, value).relative_to(root).as_posix() for value in status_evidence]
    source_authorized_now = permission_allowed(config)
    if config.get("purpose", "production") != "production" or config.get("production_eligible") is False:
        raise ValueError("production_rejects_engineering_validation")
    began = elapsed.monotonic()
    limit = max_seconds if max_seconds is not None else config["max_run_seconds"]
    if type(limit) not in {float, int} or not 0 < limit <= 14400:
        raise ValueError("sector_runtime_limit_invalid")
    if dry_run:
        return {"status": "dry_run", "market_scope": config["market_scope"], "research_mode": "sector_first",
            "target_date": str(target) if target else None, "selection_id": selection_id,
            "operation": operation, "network_requests": 0, "model_calls": 0,
            "steps": ["复用可信名单和日历", "行业目录及完整成分和一日行情", "冻结行业选择与S", "仅对S按需历史", "中文就绪报告"],
            "full_market_history_required": False}, 0
    if operation != "report" and not source_authorized_now:
        return {"status": "permission_required", "reason": "current_personal_local_source_permission_not_active",
                "model_calls": 0, "network_requests": 0}, 2
    if operation in {"resume", "prepare", "report"}:
        selection, config = read_selection(root, config, selection_id)
    else:
        target = target.isoformat() if isinstance(target, date) else target
        date.fromisoformat(target)
        now = invocation_time or datetime.now(SHANGHAI)
        if now.utcoffset() is None:
            raise ValueError("sector_invocation_time_requires_timezone")
        now = now.astimezone(SHANGHAI)
        expected = datetime.combine(date.fromisoformat(target), time(21), SHANGHAI)
        if now < expected:
            return {"status": "not_due", "target_date": target, "model_calls": 0, "network_requests": 0}, 0
        cutoff = cutoff or expected.isoformat()
        parsed_cutoff = datetime.fromisoformat(cutoff)
        if parsed_cutoff.utcoffset() is None or parsed_cutoff > now or parsed_cutoff.astimezone(SHANGHAI) < expected:
            raise ValueError("invalid_sector_cutoff")
        evidence = local_path(root, config["output_directory"]) / "source_runs" / (now.strftime("%Y%m%dT%H%M%S%f")+"-"+uuid4().hex[:8])
        evidence.mkdir(parents=True)
        try:
            packet = collect_inputs(root, config, target, evidence, max_seconds=limit, source_run=source_run, reuse_only=reuse_only)
        except Exception as exc:
            failure = {"status": "source_or_universe_blocked", "target_date": target, "error_type": type(exc).__name__,
                "reason": str(exc)[:400], "evidence_directory": str(evidence), "model_calls": 0}
            write_new(evidence / "failure.json", failure)
            return failure, 2
        selection, rows = evaluate(packet["universe"], packet["catalog"], packet["memberships"], packet["quotes"],
            target=target, cutoff=cutoff, config=config, calendar=packet["calendar"])
        archive_selection(root, config, selection, rows, packet)
    history = None
    if operation in {"run", "resume", "prepare"} and selection["selection_verified"] and selection["members"]:
        from .sector_history import prepare_history
        remaining = max(0.01, limit - (elapsed.monotonic() - began))
        history = prepare_history(root, selection, config, max_seconds=remaining)
    report = report_readiness(root, config, selection, history)
    code = 0 if report["status"] in {"no_matching_sectors", "ready_for_screening", "technical_ready_risk_pending"} else 2
    return report, code


def run_sector_daily(*, project, config, config_path, target, cutoff, start, now,
                     dry_run, scheduled, planned, max_seconds=None):
    from .operations.lock import AlreadyRunning, ProcessLock
    if scheduled:
        raise ValueError("F2-S1 manual acceptance only; scheduling disabled")
    times = resolve_times(target, cutoff, start, now, config,
        read_runs(local_path(project, config.output_directory), scope="sse_szse_a", research_mode="sector_first"), planned)
    identifier = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    root = local_path(project, config.output_directory) / ("previews" if dry_run else "runs") / identifier
    record = {"schema_version": "m4-run-v1", "run_id": identifier, **times, "scope": "sse_szse_a",
        "research_mode": "sector_first", "scope_label": "沪深 A 股·板块精选研究，暂不含北交所",
        "started_at": now.isoformat(), "trigger_kind": "manual", "generation_status": "not_run",
        "implementation_stage": "F2-S1", "model_summary": {"call_count": 0, "status": "not_run"},
        "module_statuses": {"screening": "not_run_F3", "research": "not_run_F4", "publish": "not_run"},
        "run_directory": str(root)}
    try:
        with ProcessLock(project / "data/operations/daily.lock", identifier):
            result, code = run_sector(root=project, config_path=config.sector_config, operation="run",
                target=times["target_trade_date"], cutoff=times["cutoff_at"], dry_run=dry_run, max_seconds=max_seconds,
                invocation_time=now)
    except AlreadyRunning:
        result, code = {"status": "already_running"}, 3
    record.update(status=result["status"], exit_code=code, completed_at=now_iso(),
        ended_at=now_iso(), sector_result=result, selection_id=result.get("selection_id"))
    atomic_json(root / "result.json", record)
    return record
