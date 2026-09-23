"""F2 scoped, resumable fact acquisition. No screening, models or publishing.

Each job freezes the entire discovered cohort and calendar before its first
request. Checkpoints never reduce the denominator when a request fails.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from .market_foundation import F2MarketStore, plan_history
from .operations.backup import _io
from .providers.baostock_f2 import BaoStockF2Client, resolve_history_calendar
from .providers.base import DailyBarRequest, SecurityIdentity
from .providers.routing import ProviderRouter, ROUTING_VERSION
from .universe import UniverseStore, scope_boards

TZ = ZoneInfo("Asia/Shanghai")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(TZ).isoformat()


def _write(path, value):
    path = Path(path)
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    with _io(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return hashlib.sha256(_io(path).read_bytes()).hexdigest()


def _path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("F2 paths must remain within project root")
    return path


def _job_directory(root, config, plan):
    expected = _path(root, config["output_directory"]) / plan["target_date"] / plan["job_id"]
    frozen = Path(plan["output_directory"])
    if frozen.resolve() != expected.resolve():
        from .operations.backup import resolve_restored_reference
        mapped = resolve_restored_reference(str(frozen / "plan.json"), root)
        if mapped.resolve() != (expected / "plan.json").resolve():
            raise ValueError("checkpoint output path differs from verified restore mapping")
    return expected


def load_market_config(project_root, config_path):
    root = Path(project_root).resolve()
    config = json.loads(_io(_path(root, config_path)).read_text(encoding="utf-8-sig"))
    scope_boards(config["scope"])
    if config.get("schema_version") != "f2-market-config-v1" or not config.get("config_version"):
        raise ValueError("invalid F2 config version")
    if config.get("provider") != "baostock" or config.get("permission_status") != "approved" or not config.get("permission_basis"):
        raise ValueError("F2 source permission is not confirmed")
    if config.get("model_calls") != 0 or config.get("target_trading_days") != 320:
        raise ValueError("F2 requires zero model calls and the existing 320-session history target")
    if not 0 <= config.get("recheck_days", -1) <= 20 or not 1 <= config.get("max_attempts", 0) <= 2:
        raise ValueError("invalid finite recheck/retry limits")
    if not 1 <= config.get("timeout_seconds", 0) <= 60 or config.get("pause_seconds", -1) < 0.5:
        raise ValueError("invalid bounded source pacing")
    for key in ("database", "checkpoint_database", "calendar_cache", "output_directory", "universe_config"):
        _path(root, config[key])
    _provider_policy(config)
    return config


def _provider_policy(config):
    """Do not insert new defaults into old config hashes or old checkpoints."""
    policy = config.get("provider_routing")
    if policy is None:
        return None
    if (not isinstance(policy, dict) or policy.get("schema_version") != ROUTING_VERSION
            or policy.get("order") != ["baostock", "eastmoney"]
            or type(policy.get("failure_threshold")) is not int or not 1 <= policy["failure_threshold"] <= 3):
        raise ValueError("invalid bounded provider routing policy")
    permission = policy.get("eastmoney")
    if not isinstance(permission, dict) or type(permission.get("enabled")) is not bool:
        raise ValueError("explicit EastMoney source permission registry required")
    if permission.get("llm_export") is not False:
        raise ValueError("provider data must not be sent to models")
    if permission["enabled"] and (permission.get("permission_status") != "approved"
            or not permission.get("permission_basis") or not permission.get("purpose")
            or permission.get("permitted_storage") is not True
            or permission.get("permitted_automated_access") is not True):
        raise ValueError("EastMoney automated access and storage permission is not confirmed")
    return policy


def _provider_order(config):
    policy = _provider_policy(config)
    return policy["order"] if policy else ["baostock"]


def _version_ref(value):
    """Old v1 string references always mean BaoStock, never the new default."""
    if isinstance(value, str):
        return {"provider": "baostock", "fact_hash": value}
    if (not isinstance(value, dict) or set(value) != {"provider", "fact_hash"}
            or value["provider"] not in {"baostock", "eastmoney"}
            or not isinstance(value["fact_hash"], str)):
        raise ValueError("invalid frozen provider fact reference")
    return value


def _bar_request(plan, request, member):
    if member.get("blockers") or member.get("symbol") != request["symbol"]:
        raise ValueError("provider request has no verified universe mapping")
    identity = SecurityIdentity(security_id=member["security_id"], code=member["code"],
        exchange=member["exchange"], board=member["board"], scope=plan["scope"], metadata_verified=True)
    if identity.symbol != request["symbol"]:
        raise ValueError("source mapping differs from frozen symbol")
    return DailyBarRequest(identity, request["start_date"], request["end_date"], tuple(request["expected_dates"]), request["adjustment_mode"])


class JobStore:
    def __init__(self, path, mode):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS f2_jobs_meta(mode TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS f2_jobs(job_id TEXT PRIMARY KEY,input_key TEXT UNIQUE NOT NULL,
                scope TEXT NOT NULL,target_date TEXT NOT NULL,config_hash TEXT NOT NULL,
                plan_hash TEXT NOT NULL,plan_json TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS f2_tasks(task_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                position INTEGER NOT NULL,request_json TEXT NOT NULL,status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,result_json TEXT);
              CREATE INDEX IF NOT EXISTS f2_job_tasks ON f2_tasks(job_id,position);
              CREATE TABLE IF NOT EXISTS f2_attempts(attempt_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                task_id TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,result_json TEXT);
              CREATE TABLE IF NOT EXISTS f2_invocations(invocation_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                started_at TEXT NOT NULL,finished_at TEXT,status TEXT,metrics_json TEXT);
              CREATE TRIGGER IF NOT EXISTS f2_job_no_update BEFORE UPDATE ON f2_jobs
                BEGIN SELECT RAISE(ABORT,'F2 plans are immutable'); END;
              CREATE TRIGGER IF NOT EXISTS f2_job_no_delete BEFORE DELETE ON f2_jobs
                BEGIN SELECT RAISE(ABORT,'F2 plans are immutable'); END;
            """)
            modes = db.execute("SELECT mode FROM f2_jobs_meta").fetchall()
            if modes and [row[0] for row in modes] != [mode]:
                raise ValueError("checkpoint provenance mode mismatch")
            if not modes:
                db.execute("INSERT INTO f2_jobs_meta VALUES(?)", (mode,))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def load(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM f2_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("unknown F2 job")
        plan = json.loads(row["plan_json"])
        if _hash(plan) != row["plan_hash"]:
            raise ValueError("frozen checkpoint plan hash mismatch")
        self.tasks(plan)
        return plan

    def tasks(self, plan):
        with closing(self.connect()) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM f2_tasks WHERE job_id=? ORDER BY position", (plan["job_id"],))]
        if len(rows) != len(plan["requests"]):
            raise ValueError("checkpoint task count differs from frozen plan")
        for index, (row, request) in enumerate(zip(rows, plan["requests"])):
            if row["position"] != index or row["task_id"] != plan["job_id"] + "-" + str(index) or json.loads(row["request_json"]) != request:
                raise ValueError("checkpoint request differs from frozen plan")
        return rows


def _snapshot(root, config, target, path, mode):
    uniconfig = json.loads(_io(_path(root, config["universe_config"])).read_text(encoding="utf-8-sig"))
    if uniconfig["scope"] != config["scope"]:
        raise ValueError("market/universe scope mismatch")
    with UniverseStore(_path(root, uniconfig["database"]), mode=mode) as store:
        if path:
            source = _path(root, path)
            snapshot = json.loads(_io(source).read_text(encoding="utf-8-sig"))
            if snapshot != store.get_snapshot(snapshot["snapshot_id"]):
                raise ValueError("snapshot file differs from immutable universe ledger")
        else:
            snapshot = store.latest_snapshot(scope=config["scope"])
            source = None
    if not snapshot or snapshot.get("scope") != config["scope"] or snapshot.get("mode") != mode:
        raise ValueError("current scoped universe snapshot missing")
    if snapshot.get("resolved_trade_date") != target or snapshot.get("requested_date") != target:
        raise ValueError("universe target date mismatch; run universe sync for this date")
    required = ("universe_verified", "collection_ready", "calendar_verified") if mode == "research" else ("structural_verified", "calendar_verified")
    if not all(snapshot.get(key) is True for key in required):
        raise ValueError("universe acquisition prerequisites have not passed")
    if tuple(snapshot.get("required_boards", [])) != scope_boards(config["scope"]):
        raise ValueError("universe required boards mismatch")
    return snapshot, str(source) if source else None


def _ordered_members(snapshot):
    groups = defaultdict(list)
    for member in snapshot["members"]:
        if member["discovery_classification"] == "ordinary_a":
            groups[member["board"]].append(member)
    for group in groups.values():
        group.sort(key=lambda item: (item["code"], item["security_id"]))
    # Interleave boards for observable progress; every member remains in the plan.
    for index in range(max(map(len, groups.values()), default=0)):
        for board in snapshot["required_boards"]:
            if index < len(groups[board]):
                yield groups[board][index]


def _prepare(root, config, target, snapshot, snapshot_path, calendar, store, jobs, operation):
    cohort, tasks = [], []
    for member in _ordered_members(snapshot):
        identity_ok = member.get("metadata_verified") is True and member.get("security_type") == "ordinary_a"
        exchange = member.get("exchange")
        code = member["code"]
        symbol = ({"SSE": "sh.", "SZSE": "sz."}.get(exchange, "") + code)
        mapping_ok = identity_ok and exchange in {"SSE", "SZSE"} and len(code) == 6 and code.isdigit()
        baseline = plan_history(target_date=target, calendar=calendar["calendar"], calendar_verified=calendar["verified"],
                                listing_date=member.get("listing_date"), recheck_days=0)
        previous_by_date = {}
        for provider in _provider_order(config):
            rows = store.read_bars(member["security_id"], baseline["window_start"], target, provider=provider) if baseline["window_start"] else []
            for row in rows:
                if row["symbol"] != symbol:
                    continue
                previous_row = previous_by_date.get(row["trade_date"])
                prior_valid = previous_row and previous_row["tradestatus"] is not None and (not previous_row["quality_flags"] or previous_row["tradestatus"] is False)
                current_valid = row["tradestatus"] is not None and (not row["quality_flags"] or row["tradestatus"] is False)
                if previous_row is None or (not prior_valid and current_valid):
                    previous_by_date[row["trade_date"]] = row
        previous = list(previous_by_date.values())
        stored = {row["trade_date"] for row in previous if row["tradestatus"] is not None and (not row["quality_flags"] or row["tradestatus"] is False)}
        history = plan_history(target_date=target, calendar=calendar["calendar"], calendar_verified=calendar["verified"],
            listing_date=member.get("listing_date"), stored_dates=stored,
            recheck_days=config["recheck_days"] if operation == "update" else 0)
        record = {key: member.get(key) for key in ("security_id", "code", "name", "exchange", "board", "listing_date", "statuses")}
        record.update(symbol=symbol if mapping_ok else None, history=history,
            baseline_raw_versions={row["trade_date"]: ({"provider": row["provider"], "fact_hash": row["fact_hash"]}
                                  if _provider_policy(config) else row["fact_hash"]) for row in previous},
            mapping_basis={"universe_snapshot_id": snapshot["snapshot_id"], "source": member.get("metadata_source"),
                           "evidence_id": member.get("evidence_id"), "rule": "verified exchange plus vendor six-digit code syntax"},
            blockers=history["blockers"] + ([] if mapping_ok else ["provider_identity_mapping_unavailable"]))
        cohort.append(record)
        if record["blockers"]:
            continue
        ranges = history["raw_ranges"]
        # history_f2 can return the full <=500-row window in one SDK response.
        # Preserve segmented missing-range plans for daily incremental requests.
        if set(history["missing_dates"]) == set(history["expected_dates"]) and history["expected_dates"]:
            ranges = [{"start_date": history["window_start"], "end_date": target}]
        requests = [("unadjusted", interval) for interval in ranges]
        requests.append(("forward_adjusted", {"start_date": history["window_start"], "end_date": target}))
        for adjustment, interval in requests:
            request = {"security_id": member["security_id"], "symbol": symbol, "board": member["board"],
                "adjustment_mode": adjustment, **interval,
                "expected_dates": [day for day in history["expected_dates"] if interval["start_date"] <= day <= interval["end_date"]]}
            if _provider_policy(config):
                request["provider_order"] = list(_provider_order(config))
            tasks.append(request)
    if len(cohort) != snapshot["ordinary_a_count"] or len({m["security_id"] for m in cohort}) != len(cohort):
        raise ValueError("cohort count/identity reconciliation failed")
    input_key = _hash({"scope": config["scope"], "config": config, "snapshot": snapshot["snapshot_id"],
                       "target": target, "operation": operation, "calendar": calendar["calendar"]})
    with closing(jobs.connect()) as db:
        existing = db.execute("SELECT job_id FROM f2_jobs WHERE input_key=?", (input_key,)).fetchone()
    if existing:
        return jobs.load(existing[0])
    job_id = "f2-" + target + "-" + input_key[:20]
    directory = _path(root, config["output_directory"]) / target / job_id
    plan = {"schema_version": "f2-job-plan-v1", "job_id": job_id, "mode": store.mode, "scope": config["scope"],
            "config_version": config["config_version"], "config_hash": _hash(config), "target_date": target,
            "operation": operation, "created_at": _now(), "universe_snapshot_id": snapshot["snapshot_id"],
            "universe_snapshot_path": snapshot_path, "universe_content_hash": snapshot["content_hash"],
            "universe_time_basis": "current_observed_cohort" if not snapshot.get("historical_reconstruction") else "historical_reconstruction",
            "source_manifests": snapshot["source_manifests"], "calendar": calendar,
            "cohort": cohort, "denominator": len(cohort), "required_boards": list(scope_boards(config["scope"])),
            "requests": tasks, "output_directory": str(directory), "model_calls": 0,
            "adjustment_policy": "single provider-current complete response per security/window; no segment concatenation"}
    if _provider_policy(config):
        plan.update(schema_version="f2-job-plan-v2", provider_routing=_provider_policy(config))
    _write(directory / "plan.json", plan)
    with closing(jobs.connect()) as db, db:
        db.execute("INSERT INTO f2_jobs VALUES(?,?,?,?,?,?,?,?)", (job_id, input_key, config["scope"], target, _hash(config), _hash(plan), _json(plan), _now()))
        db.executemany("INSERT INTO f2_tasks VALUES(?,?,?,?,?,0,NULL)",
                       [(job_id + "-" + str(index), job_id, index, _json(request), "pending") for index, request in enumerate(tasks)])
    return plan


def _coverage(plan, store, jobs):
    tasks = jobs.tasks(plan)
    by_security = defaultdict(list)
    for task in tasks:
        request = json.loads(task["request_json"])
        result = json.loads(task["result_json"]) if task["result_json"] else {}
        by_security[request["security_id"]].append({"task_id": task["task_id"], "status": task["status"], "request": request, "result": result})
    boards = {board: Counter() for board in plan["required_boards"]}
    details = []
    for member in plan["cohort"]:
        history = member["history"]
        expected = set(history["expected_dates"])
        member_tasks = by_security[member["security_id"]]
        versions = dict(member.get("baseline_raw_versions", {}))
        for task in member_tasks:
            if task["request"]["adjustment_mode"] == "unadjusted":
                versions.update(task["result"].get("saved", {}).get("raw_versions", {}))
                # Read early F2 checkpoints without rewriting their archived results.
                for record in task["result"].get("saved", {}).get("quality", {}).get("records", []):
                    versions[record["trade_date"]] = {"provider": record.get("provider", "baostock"), "fact_hash": _hash(record)}
        versions = {day: _version_ref(ref) for day, ref in versions.items()}
        allowed_providers = plan.get("provider_routing", {}).get("order", ["baostock"])
        if any(ref["provider"] not in allowed_providers for ref in versions.values()):
            raise ValueError("frozen facts use a provider outside the original plan")
        rows = []
        if versions:
            with store._connection() as db:
                candidates = db.execute("SELECT provider,trade_date,fact_hash,payload_json FROM f2_bar_versions WHERE security_id=? AND trade_date BETWEEN ? AND ?",
                                        (member["security_id"], history["window_start"], plan["target_date"])).fetchall()
            for candidate in candidates:
                if versions.get(candidate["trade_date"]) == {"provider": candidate["provider"], "fact_hash": candidate["fact_hash"]}:
                    record = json.loads(candidate["payload_json"])
                    if (_hash(record) != candidate["fact_hash"] or record["symbol"] != member["symbol"]
                            or record["provider"] != candidate["provider"]):
                        raise ValueError("frozen raw version hash/identity mismatch")
                    rows.append(record)
        usable = {row["trade_date"]: row for row in rows if (not row["quality_flags"] or row["tradestatus"] is False) and row["trade_date"] in expected}
        current = usable.get(plan["target_date"])
        suspended = bool(current and current["tradestatus"] is False)
        valid = bool(current and current["tradestatus"] is True)
        raw_tasks = [task for task in member_tasks if task["request"]["adjustment_mode"] == "unadjusted"]
        outstanding_raw = any(task["status"] != "done" for task in raw_tasks)
        windows = [task["result"].get("saved", {}).get("window_id") for task in member_tasks if task["status"] == "done" and task["request"]["adjustment_mode"] == "forward_adjusted"]
        adjustment_ready = False
        raw_providers = {row["provider"] for row in usable.values()}
        adjustment_providers = set()
        for window_id in filter(None, windows):
            window = store.get_adjustment_window(window_id)
            if window:
                adjustment_providers.add(window["provider"])
            adjustment_ready |= bool(window and set(window["expected_dates"]) == expected
                and window["symbol"] == member["symbol"] and window["security_id"] == member["security_id"]
                and window["provider"] in allowed_providers and raw_providers == {window["provider"]})
        missing = sorted(expected - usable.keys())
        history_ready = bool(expected and not missing and not member["blockers"] and not outstanding_raw
                             and all(row["tradestatus"] is not None for row in usable.values()))
        unknown_st = not current or current["is_st"] is None
        unknown_trade = not current or current["tradestatus"] is None
        unknown_delisting = member.get("statuses", {}).get("delisting_period", {}).get("value") is None
        counts = {"expected_target": 1, "valid_target": int(valid), "confirmed_suspended": int(suspended),
            "fact_target_present": int(any(row["trade_date"] == plan["target_date"] for row in rows)),
            "fact_history_dates": len({row["trade_date"] for row in rows if row["trade_date"] in expected}),
            "missing_target": int(not valid and not suspended), "history_expected_dates": len(expected),
            "history_valid_dates": sum(row["tradestatus"] is True for row in usable.values()),
            "history_unknown_trading_status_dates": sum(row["tradestatus"] is None and row["trade_date"] in expected for row in rows),
            "history_suspended_dates": sum(row["tradestatus"] is False for row in usable.values()),
            "history_field_issue_dates": sum(bool(row["quality_flags"]) for row in rows),
            "history_missing_dates": len(missing), "history_ready": int(history_ready),
            "adjustment_ready": int(adjustment_ready), "unknown_st": int(unknown_st),
            "unknown_trading_status": int(unknown_trade), "unknown_delisting_period": int(unknown_delisting),
            "research_ready": 0}
        boards[member["board"]].update(counts)
        details.append({"security_id": member["security_id"], "code": member["code"], "board": member["board"],
            "scope": plan["scope"], **counts, "missing_dates": missing, "blockers": member["blockers"],
            "raw_providers": sorted({row["provider"] for row in rows}), "adjustment_providers": sorted(adjustment_providers),
            "raw_adjustment_source_aligned": bool(adjustment_ready),
            "provider_fact_counts": dict(Counter(row["provider"] for row in rows)),
            "field_quality_issues": [{"date": row["trade_date"], "flags": row["quality_flags"], "tradestatus": row["tradestatus"]} for row in rows if row["quality_flags"]],
            "task_issues": [{"task_id": task["task_id"], "status": task["status"], "error": task["result"].get("error") or ("not_requested_yet" if task["status"] == "pending" else "request_in_progress" if task["status"] == "running" else "quality_or_source_failure"),
                            "response_path": task["result"].get("response_path"),
                            "provider": task["result"].get("selected_provider", "baostock"),
                            "provider_attempts": task["result"].get("provider_attempts", []),
                            "quality_issues": task["result"].get("saved", {}).get("quality", {}).get("quality_issues", [])}
                           for task in member_tasks if task["status"] != "done"],
            "adjustment_window_ids": list(filter(None, windows))})
    total = Counter({key: sum(board[key] for board in boards.values())
                     for key in {key for board in boards.values() for key in board}})
    complete = bool(plan["denominator"] and total["history_ready"] == total["adjustment_ready"] == plan["denominator"]
                    and total["valid_target"] + total["confirmed_suspended"] == plan["denominator"]
                    and total["history_unknown_trading_status_dates"] == 0
                    and all(task["status"] == "done" for task in tasks))
    return {"schema_version": "f2-quality-v1", "job_id": plan["job_id"], "scope": plan["scope"],
        "mode": plan["mode"], "verification_kind": "live_network" if plan["mode"] == "research" else "offline_test",
        "scope_label": "沪深 A 股全市场（暂不含北交所）" if plan["scope"] == "sse_szse_a" else "全 A 股（含北交所）",
        "config_version": plan["config_version"], "target_date": plan["target_date"], "universe_snapshot_id": plan["universe_snapshot_id"],
        "denominator": plan["denominator"], "board_coverage": {key: dict(value) for key, value in boards.items()}, "totals": dict(total),
        "tasks": dict(Counter(task["status"] for task in tasks)), "structural_market_complete": complete,
        "provider_routing_version": plan.get("provider_routing", {}).get("schema_version", "legacy-baostock-only"),
        "provider_fact_counts": dict(Counter({provider: sum(item["provider_fact_counts"].get(provider, 0) for item in details)
                                             for provider in {key for item in details for key in item["provider_fact_counts"]}})),
        "market_complete": complete and plan["mode"] == "research",
        "status": ("f2_complete" if complete else "f2_partial") if plan["mode"] == "research" else ("sample_complete" if complete else "sample_partial"),
        "f3_ready": False, "research_ready": False,
        "research_limitations": ["F3 has not been implemented or accepted", "risk states require separate evidence; unknown is not normal"],
        "adjustment_anchor": "provider_current_at_fetch", "point_in_time_adjustment_verified": False,
        "historical_membership_reconstructed": False, "details": details, "generated_at": _now(), "model_calls": 0}


class _ReadJobs(JobStore):
    def __init__(self, path, mode):
        self.path = Path(path)
        if not self.path.is_file():
            raise ValueError("checkpoint database does not exist")
        with closing(self.connect()) as db:
            if [row[0] for row in db.execute("SELECT mode FROM f2_jobs_meta")] != [mode]:
                raise ValueError("checkpoint provenance mode mismatch")

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db


class _ReadMarket(F2MarketStore):
    def __init__(self, path, mode):
        self.path, self.mode = Path(path), mode
        if not self.path.is_file():
            raise ValueError("market database does not exist")
        with self._connection() as db:
            row = db.execute("SELECT version,mode FROM f2_schema").fetchone()
            if not row or row[0] != 1 or row[1] != mode:
                raise ValueError("market provenance mode mismatch")

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            yield db


def quality_report(project_root, config_path, job_id, *, mode="research"):
    root = Path(project_root).resolve()
    config = load_market_config(root, config_path)
    jobs = _ReadJobs(_path(root, config["checkpoint_database"]), mode)
    plan = jobs.load(job_id)
    if plan["scope"] != config["scope"] or plan["mode"] != mode:
        raise ValueError("quality scope/provenance mismatch")
    store = _ReadMarket(_path(root, config["database"]), mode=mode)
    return _coverage(plan, store, jobs)


def run_market(*, project_root, config_path, target_date=None, operation="bootstrap",
               universe_snapshot_path=None, job_id=None, max_seconds=None, mode="research",
               client=None, calendar_resolver=None, fallback_provider=None):
    """Caller holds the existing daily process lock; one bounded invocation."""
    root = Path(project_root).resolve()
    config = load_market_config(root, config_path)
    if mode not in {"research", "offline_test"} or operation not in {"bootstrap", "update", "resume"}:
        raise ValueError("invalid F2 operation/provenance")
    if mode == "research" and (client is not None or calendar_resolver is not None or fallback_provider is not None):
        raise ValueError("injected test sources cannot enter research")
    if fallback_provider is not None and (_provider_policy(config) is None or fallback_provider.name != "eastmoney"):
        raise ValueError("test fallback must match the explicit provider routing policy")
    if mode == "offline_test" and any("research" in {part.casefold() for part in _path(root, config[key]).parts}
                                     for key in ("database", "checkpoint_database", "calendar_cache", "output_directory")):
        raise ValueError("test F2 paths cannot enter research")
    seconds = config["max_run_seconds"] if max_seconds is None else max_seconds
    if type(seconds) not in {int, float} or not 0 < seconds <= 14400:
        raise ValueError("max_seconds must be positive and at most four hours per invocation")
    started = time.monotonic()
    jobs = JobStore(_path(root, config["checkpoint_database"]), mode)
    store = F2MarketStore(_path(root, config["database"]), mode=mode)
    source = client or BaoStockF2Client(config["timeout_seconds"], config["max_attempts"], config["pause_seconds"])
    from .providers.baostock_provider import BaoStockProvider
    providers = [BaoStockProvider(source, mode=mode)]
    policy = _provider_policy(config)
    if policy:
        from .providers.eastmoney import EastMoneyProvider
        providers.append(fallback_provider or EastMoneyProvider(permission=policy["eastmoney"], mode=mode,
            timeout_seconds=min(15, config["timeout_seconds"]), max_attempts=config["max_attempts"], pause_seconds=config["pause_seconds"]))
    router = ProviderRouter(providers, failure_threshold=policy["failure_threshold"] if policy else 3)
    try:
        if job_id:
            plan = jobs.load(job_id)
            if plan["scope"] != config["scope"] or plan["config_hash"] != _hash(config) or plan["mode"] != mode:
                raise ValueError("resume scope/config/provenance mismatch")
            if target_date and str(target_date) != plan["target_date"]:
                raise ValueError("resume target date mismatch")
            if plan.get("provider_routing") != policy:
                raise ValueError("resume provider policy differs from frozen checkpoint; create a new job")
        else:
            target = date.fromisoformat(str(target_date))
            snapshot, snapshot_path = _snapshot(root, config, target.isoformat(), universe_snapshot_path, mode)
            calendar = (calendar_resolver or resolve_history_calendar)(target, _path(root, config["calendar_cache"]),
                history_days=320, client=source, mode=mode)
            if calendar.get("verified") is not True or calendar.get("resolved_trade_date") != target.isoformat():
                directory = _path(root, config["output_directory"]) / target.isoformat() / ("blocked-" + uuid.uuid4().hex[:12])
                result = {"status": "f2_blocked", "scope": config["scope"], "target_date": target.isoformat(),
                          "calendar": calendar, "model_calls": 0, "output_directory": str(directory)}
                _write(directory / "result.json", result)
                return result, 2
            plan = _prepare(root, config, target.isoformat(), snapshot, snapshot_path, calendar, store, jobs, operation)
        invocation = uuid.uuid4().hex
        job_directory = _job_directory(root, config, plan)
        directory = job_directory / "invocations" / invocation
        before = sum(_io(path).stat().st_size for path in (store.path, jobs.path) if _io(path).exists())
        metrics = {"requests": 0, "logical_requests": 0, "logical_requests_started": 0, "retries": 0, "inserted": 0, "updated": 0, "unchanged": 0, "response_bytes": 0,
                   "fallback_attempts": 0, "fallback_selected": 0, "fallback_verified": 0, "provider_metrics": {}}
        provider_events = []
        members = {member["security_id"]: member for member in plan["cohort"]}
        stop_reason = "queue_finished"
        with closing(jobs.connect()) as db, db:
            db.execute("INSERT INTO f2_invocations VALUES(?,?,?,NULL,NULL,NULL)", (invocation, plan["job_id"], _now()))
            # A previously interrupted request has no completed business conclusion.
            db.execute("UPDATE f2_attempts SET finished_at=?,result_json=? WHERE job_id=? AND finished_at IS NULL",
                       (_now(), _json({"error": "interrupted_before_checkpoint", "retry_required": True}), plan["job_id"]))
            db.execute("UPDATE f2_tasks SET status='pending' WHERE job_id=? AND status='running'", (plan["job_id"],))
            pending = [dict(row) for row in db.execute("SELECT * FROM f2_tasks WHERE job_id=? AND status!='done' ORDER BY CASE status WHEN 'failed' THEN 0 ELSE 1 END,position", (plan["job_id"],))]
        consecutive_failures = 0
        try:
            for task in pending:
                if time.monotonic() - started >= seconds:
                    stop_reason = "runtime_limit_checkpoint_saved"
                    break
                request = json.loads(task["request_json"])
                attempt = uuid.uuid4().hex
                with closing(jobs.connect()) as db, db:
                    db.execute("UPDATE f2_tasks SET status='running',attempts=attempts+1 WHERE task_id=?", (task["task_id"],))
                    db.execute("INSERT INTO f2_attempts VALUES(?,?,?,?,NULL,NULL)", (attempt, plan["job_id"], task["task_id"], _now()))
                normalized_request = _bar_request(plan, request, members[request["security_id"]])
                metrics["logical_requests_started"] += 1
                routed = router.fetch_daily_bars(normalized_request)
                selected = routed.selected
                response = selected.response
                provider_attempts = []
                for candidate in routed.candidates:
                    candidate_path = directory / "responses" / (attempt + ("" if candidate is selected else "-" + candidate.provider) + ".json")
                    candidate_hash = _write(candidate_path, candidate.response)
                    metrics["response_bytes"] += _io(candidate_path).stat().st_size
                    provider_attempts.append({"provider": candidate.provider, "selected": candidate is selected,
                        "response_path": str(candidate_path), "source_file_hash": candidate_hash,
                        "source_status": candidate.status, "error_code": candidate.response.get("error_code"),
                        "provenance": candidate.provenance(), "metrics": candidate.metrics})
                    if candidate is selected:
                        response_path, file_hash = candidate_path, candidate_hash
                    counts = metrics["provider_metrics"].setdefault(candidate.provider,
                        {"logical_requests": 0, "requests": 0, "retries": 0, "selected": 0, "verified": 0,
                         "elapsed_seconds": 0, "statuses": {}})
                    counts["logical_requests"] += 1
                    for key in ("requests", "retries", "elapsed_seconds"):
                        counts[key] += candidate.metrics.get(key, 0)
                    counts["selected"] += int(candidate is selected)
                    counts["verified"] += int(candidate.usable)
                    counts["statuses"][candidate.status] = counts["statuses"].get(candidate.status, 0) + 1
                routed_metrics = routed.metrics()
                metrics["logical_requests"] += 1
                for key in ("requests", "retries", "fallback_attempts", "fallback_selected", "fallback_verified"):
                    metrics[key] += routed_metrics[key]
                events = [{"task_id": task["task_id"], "attempt_id": attempt, **event} for event in routed.events]
                provider_events.extend(events)
                provenance_path = directory / "providers" / (attempt + ".json")
                _write(provenance_path, {"schema_version": ROUTING_VERSION, "request": request,
                    "scope": plan["scope"], "mode": mode, "universe_snapshot_id": plan["universe_snapshot_id"],
                    "selected_provider": selected.provider, "candidates": provider_attempts, "events": events})
                result = {"response_path": str(response_path), "source_file_hash": file_hash,
                          "source_status": response.get("status"), "error": None,
                          "selected_provider": selected.provider, "provenance": selected.provenance(),
                          "provider_attempts": provider_attempts, "provider_evidence_path": str(provenance_path)}
                status = "failed"
                try:
                    if response.get("ok") is not True:
                        raise ValueError(str(response.get("error_code")) + ":" + str(response.get("error_msg")))
                    saved = store.save_batch(security_id=request["security_id"], symbol=request["symbol"], scope=plan["scope"],
                        universe_snapshot_id=plan["universe_snapshot_id"], response=response, source_response_path=response_path,
                        source_response_hash=file_hash, trading_dates=request["expected_dates"],
                        provenance_mode="online" if mode == "research" else "offline_test", adjustment_mode=request["adjustment_mode"],
                        provider=selected.provider, request=normalized_request)
                    # The full normalized rows already live in immutable f2_batches
                    # and facts/windows. Keep checkpoint memory proportional to
                    # version references instead of parsing millions of duplicate rows.
                    result["saved"] = {**saved,
                        "raw_versions": {row["trade_date"]: ({"provider": row["provider"], "fact_hash": _hash(row)}
                                         if plan.get("provider_routing") else _hash(row)) for row in saved["quality"]["records"]}
                            if request["adjustment_mode"] == "unadjusted" else {},
                        "quality": {key: value for key, value in saved["quality"].items() if key != "records"}}
                    for key in ("inserted", "updated", "unchanged"):
                        metrics[key] += saved[key]
                    if saved["quality"]["quote_complete"] and (request["adjustment_mode"] == "unadjusted" or saved["window_id"]):
                        status = "done"
                    else:
                        result["error"] = "incomplete_or_invalid_quote_response"
                except (ValueError, TypeError, KeyError) as exc:
                    result["error"] = str(exc)
                with closing(jobs.connect()) as db, db:
                    db.execute("UPDATE f2_attempts SET finished_at=?,result_json=? WHERE attempt_id=?", (_now(), _json(result), attempt))
                    db.execute("UPDATE f2_tasks SET status=?,result_json=? WHERE task_id=?", (status, _json(result), task["task_id"]))
                if response.get("status") in {"permission_denied", "rate_limited"}:
                    stop_reason = "source_access_or_rate_stop"
                    break
                consecutive_failures = 0 if response.get("ok") is True else consecutive_failures + 1
                if consecutive_failures >= 3:
                    stop_reason = "three_consecutive_source_failures_checkpoint_saved"
                    break
                if client is None:
                    time.sleep(config["pause_seconds"])
        except KeyboardInterrupt:
            stop_reason = "user_interrupt_checkpoint_saved"
            # No completed network result exists for this attempt. Keep that
            # distinction from zero network activity, and immediately release
            # its checkpoint state instead of leaving a misleading running row.
            with closing(jobs.connect()) as db, db:
                interrupted = list(db.execute("SELECT attempt_id,task_id FROM f2_attempts WHERE job_id=? AND finished_at IS NULL", (plan["job_id"],)))
                for interrupted_attempt, interrupted_task in interrupted:
                    detail = {"error": "user_interrupt_before_completed_result", "retry_required": True,
                              "network_activity": "unknown_no_completed_response", "model_calls": 0}
                    db.execute("UPDATE f2_attempts SET finished_at=?,result_json=? WHERE attempt_id=?", (_now(), _json(detail), interrupted_attempt))
                    db.execute("UPDATE f2_tasks SET status='pending',result_json=? WHERE task_id=? AND status='running'", (_json(detail), interrupted_task))
                    provider_events.append({"event": "request_interrupted", "task_id": interrupted_task,
                                            "attempt_id": interrupted_attempt, **detail})
        quality = _coverage(plan, store, jobs)
        quality_path = directory / "quality.json"
        _write(quality_path, quality)
        problems_path = directory / "problems.json"
        _write(problems_path, quality["details"])
        provider_log_path = directory / "provider-log.json"
        _write(provider_log_path, {"schema_version": ROUTING_VERSION, "job_id": plan["job_id"], "mode": mode,
            "provider_order": [p.name for p in providers], "events": provider_events,
            "metrics": metrics["provider_metrics"], "model_calls": 0})
        after = sum(_io(path).stat().st_size for path in (store.path, jobs.path) if _io(path).exists())
        metrics.update(elapsed_seconds=round(time.monotonic() - started, 3), database_growth_bytes=after - before,
                       storage_growth_bytes=after - before + metrics["response_bytes"])
        result = {key: value for key, value in quality.items() if key != "details"}
        result.update(invocation_id=invocation, output_directory=str(directory), quality_path=str(quality_path),
            problems_path=str(problems_path), plan_path=str(job_directory / "plan.json"),
            metrics=metrics, stop_reason=stop_reason, checkpoint_database=str(jobs.path),
            provider_log_path=str(provider_log_path),
            resume_command=f"python -m ashare_daily market resume --job {plan['job_id']} --config {config_path}")
        _write(directory / "result.json", result)
        with closing(jobs.connect()) as db, db:
            db.execute("UPDATE f2_invocations SET finished_at=?,status=?,metrics_json=? WHERE invocation_id=?", (_now(), result["status"], _json(metrics), invocation))
        return result, 0 if quality["structural_market_complete"] else 2
    finally:
        try:
            router.close()
        finally:
            if client is None:
                source.close()
