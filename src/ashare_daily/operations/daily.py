"""Standalone M4 daily pipeline. Viewing reports never imports this module."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import time as elapsed_time
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ashare_daily.market_schemas import CalendarDay, SHANGHAI
from ashare_daily.research.evidence import digest
from ashare_daily.research.model import ChatCompletionsModel, _redact
from ashare_daily.research.model_settings import load_model_settings
from ashare_daily.artifact_purpose import is_production_artifact, is_production_path

PROJECT = Path(__file__).resolve().parents[3]


class F2RuntimeLimitError(ValueError):
    """A safe, fixed message for an invalid invocation-only F2 argument."""


class SectorDailyModeError(ValueError):
    """A fixed, public boundary error for the manually accepted F2-S1 workflow."""


class DailyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_version: str = "m4-v1"
    scope_mode: Literal["sample", "all_a", "sse_szse_a"] = "sample"
    research_mode: Literal["full_market", "sector_first"] = "full_market"
    sector_config: str | None = None
    observation_config: str | None = None
    universe_config: str | None = None
    f2_config: str | None = None
    market_config: str | None = "config/m21_100.json"
    research_config: str = "config/m3_smoke.json"
    sources_config: str = "config/m3_sources.json"
    eligibility_sources_config: str | None = None
    database: str = "data/research/market.sqlite3"
    adjusted_directory: str = "data/research/m21_adjusted"
    env_file: str = ".env"
    output_directory: str = "outputs/research/m4"
    trigger_time: str = "21:00"
    first_query_lookback_days: int = Field(default=3, ge=1, le=14)
    market_timeout_seconds: int = Field(default=20, ge=1, le=60)
    market_max_attempts: int = Field(default=2, ge=1, le=2)
    master_refresh_days: int = Field(default=7, ge=1, le=30)
    model_timeout_seconds: int = Field(default=90, ge=1, le=120)
    model_max_output_tokens: int | None = Field(default=None, ge=256, le=8192)
    model_max_retries: int = Field(default=0, ge=0, le=1)
    model_max_calls_per_run: int = Field(default=2, ge=1, le=6)
    model_max_calls_per_day: int = Field(default=6, ge=1, le=12)
    scheduled_attempts_per_day: int = Field(default=2, ge=1, le=2)

    @model_validator(mode="after")
    def sector_scope(self):
        if self.research_mode == "sector_first":
            if self.scope_mode != "sse_szse_a" or not self.sector_config or not self.sector_config.strip():
                raise ValueError("sector_first requires the SSE/SZSE scope and explicit sector_config")
            if time.fromisoformat(self.trigger_time) != time(21):
                raise ValueError("sector_first retains the 21:00 Asia/Shanghai cutoff")
        elif self.sector_config is not None:
            raise ValueError("sector_config requires explicit research_mode=sector_first")
        if self.observation_config is not None and self.research_mode != "sector_first":
            raise ValueError("observation_config requires sector_first")
        return self


def local_path(project: Path, value: str | Path) -> Path:
    candidate = (project / value).resolve()
    if not candidate.is_relative_to(project.resolve()):
        raise ValueError("运行路径必须位于项目内")
    return candidate


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers/security scanners can briefly deny an atomic replacement.
    # Retry only that documented OS failure, preserving the original on failure.
    for attempt, delay in enumerate((0, 0.02, 0.05, 0.1)):
        if delay:
            elapsed_time.sleep(delay)
        try:
            os.replace(temporary, path)
            break
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32} or attempt == 3:
                raise


def read_runs(root: Path, *, scope: str | None = None, research_mode: str | None = None) -> list[dict]:
    rows = []
    run_root = (root / "runs").resolve()
    for path in (root / "runs").glob("*/result.json"):
        try:
            actual = path.resolve()
            if actual.parent.parent != run_root or path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                    or row["run_id"] != actual.parent.name or not isinstance(row.get("started_at"), str)):
                continue
            if not is_production_artifact(row) or not is_production_path(actual):
                continue
            clock_value(row["started_at"])
            if row.get("cutoff_at"):
                clock_value(row["cutoff_at"])
            # Pre-universe M4 records are the original fixed sample. A scope
            # migration must not recover or reuse another scope's old record.
            if scope is not None and row.get("scope", "sample") != scope:
                continue
            if research_mode is not None and row.get("research_mode", "full_market") != research_mode:
                continue
            # Recovery must use where the record was read, never its stored path.
            row["run_directory"] = str(actual.parent)
            rows.append(row)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda row: row.get("started_at", ""), reverse=True)


def calendar_status(database: Path, target: date) -> bool | None:
    if not database.is_file():
        return None
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM market_metadata"))
        if metadata.get("mode") != "research" or metadata.get("verification_kind") == "offline_test":
            raise ValueError("日任务拒绝模拟行情库")
        row = connection.execute("SELECT is_trading_day,payload_json FROM trading_calendar WHERE provider='baostock' AND calendar_date=?", (target.isoformat(),)).fetchone()
        if row is None:
            return None
        parsed = CalendarDay.model_validate_json(row[1])
        if parsed.calendar_date != target or parsed.is_trading_day != bool(row[0]):
            raise ValueError("交易日历证据不一致")
        return parsed.is_trading_day


def clock_value(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须含时区")
    return parsed.astimezone(SHANGHAI)


def resolve_times(target: date | None, cutoff: str | None, start: str | None, now: datetime,
                  config: DailyConfig, previous: list[dict], planned: str | None = None) -> dict:
    now = clock_value(now)
    target = target or now.date()
    if target > now.date():
        raise ValueError("不能生成未来交易日研究")
    trigger = time.fromisoformat(config.trigger_time)
    if trigger.tzinfo is not None:
        raise ValueError("trigger_time 为北京时间 HH:MM")
    expected = datetime.combine(target, trigger, SHANGHAI)
    cutoff_at = clock_value(cutoff) if cutoff else min(expected, now.replace(microsecond=0))
    if cutoff_at > now or cutoff_at.date() < target:
        raise ValueError("资料截点不得在未来或早于行情分析日")
    earlier = [clock_value(row["cutoff_at"]) for row in previous if row.get("generation_status") == "ok"
               and row.get("cutoff_at") and clock_value(row["cutoff_at"]) < cutoff_at]
    query_start = clock_value(start) if start else max(earlier, default=datetime.combine(
        target - timedelta(days=config.first_query_lookback_days), time.min, SHANGHAI))
    if query_start >= cutoff_at:
        raise ValueError("资料起点必须早于截点")
    return {"target_trade_date": target.isoformat(), "cutoff_at": cutoff_at.isoformat(),
            "query_start_at": query_start.isoformat(), "planned_trigger_at": clock_value(planned).isoformat() if planned else expected.isoformat(),
            "historical_run": target < now.date(), "timezone": "Asia/Shanghai"}


def input_state(database: Path, adjusted: Path, symbols: list[str], target: date, cutoff: str) -> str:
    """Only stable data versions count; fetch timestamps alone do not invalidate reuse."""
    state = {"market": [], "evidence": [], "adjusted": []}
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            placeholders = ",".join("?" for _ in symbols)
            state["market"].append(db.execute(f"SELECT symbol,trade_date,raw_hash FROM daily_bars WHERE symbol IN ({placeholders}) AND trade_date BETWEEN ? AND ? ORDER BY symbol,trade_date", (*symbols, (target-timedelta(days=365)).isoformat(), target.isoformat())).fetchall())
            state["market"].append(db.execute(f"SELECT symbol,raw_hash FROM instruments WHERE symbol IN ({placeholders}) ORDER BY symbol", symbols).fetchall())
            state["market"].append(db.execute("SELECT calendar_date,raw_hash FROM trading_calendar WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date", ((target-timedelta(days=365)).isoformat(),target.isoformat())).fetchall())
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='m3_evidence'").fetchone():
                for (payload,) in db.execute("SELECT payload_json FROM m3_evidence ORDER BY evidence_id"):
                    evidence = json.loads(payload)
                    from ashare_daily.research.evidence import _publication_bounds
                    bounds = _publication_bounds(evidence.get("published_at"),evidence.get("publication_precision"))
                    if bounds and bounds[1] <= clock_value(cutoff):
                        state["evidence"].append(evidence["evidence_id"])
    for root in (adjusted, database.parent / "m2_adjusted"):
        for path in root.glob("**/manifest.json"):
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("end_date") == target.isoformat():
                    state["adjusted"].append(value.get("manifest_hash"))
    state["adjusted"].sort()
    return digest(state)


def relocated_report(result: dict, *, anchor: Path = PROJECT) -> dict:
    """Resolve registered restored artifacts in memory; never edit the archive."""
    from ashare_daily.operations.paths import resolve_archived_path
    updated = dict(result)
    for key in ("json", "html", "markdown", "csv", "claim_audit_csv", "evidence_catalog", "snapshot_path"):
        if updated.get(key):
            updated[key] = str(resolve_archived_path(updated[key], anchor=anchor))
    if updated.get("json"):
        updated["run_directory"] = str(Path(updated["json"]).resolve().parent)
    return updated


def valid_report(result: dict, *, anchor: Path = PROJECT) -> bool:
    try:
        if not isinstance(result, dict) or not is_production_artifact(result):
            return False
        directory = Path(relocated_report(result, anchor=anchor)["run_directory"]).resolve()
        if not is_production_path(directory):
            return False
        from ashare_daily.operations.paths import resolve_archived_path
        raw_directory = result["run_directory"]
        pure = PureWindowsPath(raw_directory) if PureWindowsPath(raw_directory).drive else PurePosixPath(raw_directory)
        manifest_path = resolve_archived_path(str(pure / "manifest.json"), anchor=anchor)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.resolve().parent != directory:
            return False
        expected = {"daily_brief.json", "daily_brief.md", "daily_brief.html", "screening_audit.csv",
                    "claim_evidence_audit.csv", "evidence_catalog.json", "input_snapshot.json", "model_responses.json"}
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != "m3-report-manifest-v1"
                or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != expected
                or not is_production_artifact(manifest)):
            return False
        hashes_valid = all((directory / name).is_file() and not (directory / name).is_symlink()
            and (directory / name).resolve().parent == directory
            and isinstance(value, str) and len(value) == 64
            and hashlib.sha256((directory / name).read_bytes()).hexdigest() == value
            for name,value in manifest["files"].items())
        if not hashes_valid:
            return False
        for name in ("daily_brief.json", "input_snapshot.json", "sector_selection.json", "purpose.json"):
            path = directory / name
            if not path.exists():
                continue
            if path.is_symlink() or path.resolve().parent != directory:
                return False
            try:
                metadata = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError:
                # Legacy M3 registered opaque input snapshots by byte hash;
                # their historical publication contract did not require JSON.
                if name == "input_snapshot.json":
                    continue
                return False
            if not is_production_artifact(metadata):
                return False
        return True
    except (KeyError, OSError, ValueError, TypeError):
        return False


def publish_report(source: dict, root: Path, run: dict) -> dict:
    # The caller owns this output root. A separate restored checkout must not
    # impose its provenance map on this run (or a temporary offline project).
    if not is_production_artifact(run) or not is_production_path(root) or not valid_report(source, anchor=root):
        raise ValueError("待发布报告校验失败，原可读报告保持")
    destination = root / "reports" / source["trade_date"] / run["run_id"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.parent / (".pending-" + run["run_id"])
    original = Path(source["run_directory"])
    manifest = json.loads((original / "manifest.json").read_text(encoding="utf-8"))
    staged.mkdir(exist_ok=False)
    for name in (*manifest["files"], "manifest.json"):
        shutil.copyfile(original / name, staged / name)
    if not valid_report({"run_directory": str(staged)}, anchor=root):
        raise ValueError("报告复制后校验失败，原可读报告保持")
    updated = dict(source)
    for key, value in source.items():
        if isinstance(value, str) and value.startswith(source["run_directory"]):
            updated[key] = str(destination / Path(value).relative_to(source["run_directory"]))
    updated["run_directory"] = str(destination)
    atomic_json(staged / "result.json", updated)
    atomic_json(staged / "run_context.json", {k:run.get(k) for k in ("run_id","planned_trigger_at","started_at","target_trade_date","cutoff_at","query_start_at","historical_run","trigger_kind")})
    # Reuse the verified directory publication guard: bounded Windows sharing
    # retries, recheck links/absent target each time, and retain stage on failure.
    from ashare_daily.operations.backup import _publish_restored_stage
    _publish_restored_stage(staged, destination)
    atomic_json(root / "latest_report.json", updated)
    return updated


class LedgerGuard:
    def __init__(self, ledger, run_id: str, limit: int):
        self.ledger, self.run_id, self.limit = ledger, run_id, limit

    def before_attempt(self):
        return self.ledger.reserve(datetime.now(SHANGHAI).date().isoformat(), self.run_id, self.limit)

    def after_attempt(self, reservation, record):
        known = {"ok","timeout","rate_limited","refused","truncated","network_error","invalid_response"}
        self.ledger.record_usage(reservation, record.get("usage"), record["status"] if record["status"] in known else "error")


class LiveServices:
    def ensure_calendar(self, database, target, config, work):
        current = calendar_status(database, target)
        if current is not None:
            return current
        from ashare_daily.providers.baostock import BaoStockClient
        from ashare_daily.quality.baostock import normalize_calendar
        from ashare_daily.storage.market import MarketStore
        response = BaoStockClient(config.market_timeout_seconds, config.market_max_attempts).query(
            "calendar", start_date=target.isoformat(), end_date=target.isoformat())
        atomic_json(work / "calendar-response.json", response)
        if not response.get("ok"):
            detail = " / ".join(str(response.get(key, "unknown"))[:300] for key in ("status", "error_code", "error_msg"))
            raise ValueError("交易日历来源不可用：" + detail)
        days = normalize_calendar(response["rows"], start_date=target, end_date=target,
             fetched_at=datetime.fromisoformat(response["fetched_at"]), sdk_version=response["sdk_version"])
        MarketStore(database).store_calendar(days)
        return calendar_status(database, target)

    def collect_market(self, project, config, sample, target, work):
        from ashare_daily.sample_data import collect_sample_data
        return collect_sample_data(sample, target_date=target, database=local_path(project,config.database),
            output_dir=work / "market_collection", adjusted_dir=local_path(project,config.adjusted_directory),
            reuse_adjusted_dirs=[project / "data/research/m2_adjusted"], allow_current_day=True,
            refresh_master_days=config.master_refresh_days, timeout_seconds=config.market_timeout_seconds,
            max_attempts=config.market_max_attempts)

    def freeze_market(self, project, config, target, collection, work):
        from ashare_daily.m21 import run_m21
        eligibility_path = None
        if config.eligibility_sources_config:
            from ashare_daily.qualification_sources import collect_eligibility
            result = collect_eligibility(config_path=local_path(project, config.eligibility_sources_config),
                                         target=target, output_dir=work / 'eligibility')
            eligibility_path = Path(result['bundle_file'])
            collection['eligibility_collection'] = result
            market_conf = json.loads(local_path(project, config.market_config).read_text(encoding='utf-8-sig'))
            manual = market_conf.get('eligibility_evidence_file')
            if manual:
                from ashare_daily.qualification_sources import merge_eligibility
                eligibility_path = merge_eligibility(eligibility_path, local_path(project, manual), work / 'eligibility-merged.json')
        return run_m21(target_date=target, config_path=local_path(project,config.market_config),
            database=local_path(project,config.database), output_dir=work, adjusted_dir=local_path(project,config.adjusted_directory),
            adjusted_manifest=Path(collection["adjusted_manifest"]) if collection.get("adjusted_manifest") else None,
            project_root=project, eligibility_evidence=eligibility_path)

    def materials(self, project, config, symbols, times, work):
        from ashare_daily.research.sources import collect_materials
        from ashare_daily.research.runner import archive_materials
        from ashare_daily.storage.market import MarketStore
        store = MarketStore(local_path(project, config.database))
        identities = {}
        for symbol in symbols:
            instrument = store.get_instrument(symbol)
            if instrument is not None and instrument.security_type == 'stock':
                identities[symbol] = instrument.name
        collection = collect_materials(registry_path=local_path(project,config.sources_config), start=times["query_start_at"],
            cutoff=times["cutoff_at"], sample_symbols=identities, output_dir=work / "sources", include_background=True)
        return archive_materials(collection=collection, database=local_path(project,config.database), output_dir=work,
            start=times["query_start_at"], cutoff=times["cutoff_at"])

    def research(self, project, config, market, materials, times, settings, guard, work, skip_model):
        from ashare_daily.research.runner import run_research
        from ashare_daily.research.model import choose_response_mode
        capabilities = None
        for path in sorted((project / "outputs/research/m3_model").glob("*/result.json"), reverse=True):
            try:
                choose_response_mode(settings, path)
                capabilities = path
                break
            except (OSError, ValueError):
                continue
        return run_research(market_snapshot=market["snapshot_path"], evidence_bundle=Path(materials["bundle_file"]),
            start=times["query_start_at"], cutoff=times["cutoff_at"], config_path=local_path(project,config.research_config),
            output_dir=work, model_capabilities=capabilities, skip_model=skip_model,
            client=ChatCompletionsModel(settings, attempt_guard=guard))

    def failed_materials(self, project, config, times, work):
        from ashare_daily.research.runner import archive_materials
        sources = json.loads(local_path(project,config.sources_config).read_text(encoding="utf-8"))["sources"]
        collection = {"sources":[s["registration"] for s in sources],"evidence":[],"requests":[],
            "source_health":[{"source_id":s["registration"]["source_id"],"status":"failed",
                              "coverage":"本次资料采集异常；保留既有归档背景，不代表没有消息"} for s in sources]}
        return archive_materials(collection=collection,database=local_path(project,config.database),output_dir=work,
            start=times["query_start_at"],cutoff=times["cutoff_at"])


def _run_daily(*, project: Path = PROJECT, config_path: str = "config/m4.json", target: date | None = None,
              cutoff: str | None = None, start: str | None = None, dry_run: bool = False, force: bool = False,
              scheduled: bool = False, planned: str | None = None, skip_model: bool = False,
              now: datetime | None = None, services=None, sample_mode: bool = False,
              f2_max_seconds: float | None = None) -> dict:
    from ashare_daily.operations.lock import ProcessLock, AlreadyRunning
    from ashare_daily.operations.budget import BudgetLedger
    from ashare_daily.sample_data import load_sample_config
    project = project.resolve()
    if services is not None and project == PROJECT:
        raise ValueError("测试服务不得用于真实项目日任务")
    now = clock_value(now or datetime.now(SHANGHAI))
    config = DailyConfig.model_validate_json(local_path(project,config_path).read_text(encoding="utf-8"))
    if f2_max_seconds is not None:
        if type(f2_max_seconds) not in {int, float} or not 0 < f2_max_seconds <= 14400:
            raise F2RuntimeLimitError("本次F2耗时上限必须大于0且不超过14400秒")
        if config.scope_mode == "sample" or not (config.sector_config if config.research_mode == "sector_first" else config.f2_config):
            raise F2RuntimeLimitError("本次配置未启用F2行情，不能指定 --market-max-seconds")
    if config.research_mode == "sector_first":
        if config.observation_config:
            if sample_mode or services is not None:
                raise SectorDailyModeError("生产量价日报不接受固定样本或测试服务")
            from ashare_daily.operations.observation import run_observation_daily
            return run_observation_daily(project=project, config=config, config_path=config_path,
                target=target, cutoff=cutoff, start=start, now=now, dry_run=dry_run,
                scheduled=scheduled, planned=planned, max_seconds=f2_max_seconds,
                skip_model=skip_model, force=force)
        if scheduled:
            raise SectorDailyModeError("F2-S1仅允许手动验收；本轮未启用生产定时任务")
        if sample_mode or services is not None:
            raise SectorDailyModeError("板块精选研究不接受旧固定样本流程或样本测试服务")
        # Select this route before the all-universe F2 initializer. The sector
        # service owns its shared process lock, frozen selection and zero-model gate.
        from ashare_daily.sector_pipeline import run_sector_daily
        return run_sector_daily(project=project, config=config, config_path=config_path,
            target=target, cutoff=cutoff, start=start, now=now, dry_run=dry_run,
            scheduled=scheduled, planned=planned, max_seconds=f2_max_seconds)
    if config.scope_mode in {"all_a", "sse_szse_a"}:
        if sample_mode or services is not None:
            raise ValueError("动态名单范围不接受旧样本流程或样本测试服务")
        from ashare_daily.operations.full_market import run_full_market_daily
        return run_full_market_daily(project=project, config=config, config_path=config_path,
            target=target, cutoff=cutoff, start=start, now=now, dry_run=dry_run,
            scheduled=scheduled, planned=planned, f2_max_seconds=f2_max_seconds)
    if services is None and (scheduled or (not sample_mode and not dry_run)):
        raise ValueError("旧固定名单仅供显式 sample 手动运行，定时入口必须使用动态名单配置")
    root = local_path(project,config.output_directory)
    database = local_path(project,config.database)
    market_conf = json.loads(local_path(project,config.market_config).read_text(encoding="utf-8"))
    sample = local_path(project,market_conf["sample_file"])
    symbols = sorted(load_sample_config(sample)["symbol_types"])
    previous = read_runs(root, scope=config.scope_mode, research_mode="full_market")
    times = resolve_times(target,cutoff,start,now,config,previous,planned)
    target = date.fromisoformat(times["target_trade_date"])
    settings = load_model_settings(local_path(project,config.env_file), include_environment=False)
    settings = settings.model_copy(update={"timeout_seconds":float(config.model_timeout_seconds),
        "max_retries":config.model_max_retries,"max_calls":config.model_max_calls_per_run})
    if config.model_max_output_tokens is not None:
        settings = settings.model_copy(update={'max_output_tokens': config.model_max_output_tokens})
    versions = {path.relative_to(project).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in
        (local_path(project,config_path),local_path(project,config.market_config),sample,
         local_path(project,config.research_config),local_path(project,config.sources_config))}
    if config.eligibility_sources_config:
        path = local_path(project, config.eligibility_sources_config)
        versions[path.relative_to(project).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Original Windows M4 keys used backslashes. Accept that old identity only
    # when the cutoff, configuration contents and complete input state also match.
    legacy_versions = {key.replace("/", "\\"):value for key,value in versions.items()}
    evidence_file = market_conf.get("eligibility_evidence_file")
    if evidence_file:
        path = local_path(project,evidence_file)
        versions[evidence_file] = hashlib.sha256(path.read_bytes()).hexdigest()
        legacy_versions[evidence_file] = versions[evidence_file]
    identity_data = {**{k:times[k] for k in ("target_trade_date","cutoff_at","query_start_at")},
        "config_versions":versions,"model":settings.public_dict(),"skip_model":skip_model}
    identity = digest(identity_data)
    matching_identities = {identity, digest({**identity_data,"config_versions":legacy_versions})}
    run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    directory = root / ("previews" if dry_run else "runs") / run_id
    record = {"schema_version":"m4-run-v1","run_id":run_id,"started_at":now.isoformat(),"ended_at":None,
        **times,"status":"running","generation_status":"not_run","exit_code":None,"task_identity":identity,
        "trigger_kind":"scheduled" if scheduled else "manual","config_versions":versions,
        "configured_stock_count":len(symbols)-1,"scope":"sample","module_statuses":{},"sources":[],"model_summary":{"call_count":0},
        "model_key_state":"已配置" if settings.api_key.get_secret_value() else "未配置",
        "run_directory":str(directory),"log_path":str(directory / "run.log.jsonl"),"external_calls_allowed":not dry_run}
    if dry_run:
        try:
            flag = calendar_status(database,target)
            state = "trading_day" if flag else "non_trading_day" if flag is False else "calendar_unknown"
        except (ValueError,sqlite3.Error):
            state = "calendar_unavailable"
        record.update(status="dry_run",exit_code=0,calendar_preview=state,ended_at=now.isoformat(),duration_seconds=0,
            steps=["核对日历","增量行情与状态/一致复权窗口","冻结量价与资格","已登记消息采集","受日累计预算约束的研究","核验与原子发布"],
            project_root=str(project),python_executable=str(project / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")),model_configuration=settings.public_dict())
        atomic_json(directory / "result.json",record)
        return record
    services = services or LiveServices()
    began = elapsed_time.monotonic()
    secret = settings.api_key.get_secret_value()
    def log(stage, status, **details):
        row = _redact({"at":datetime.now(SHANGHAI).isoformat(),"stage":stage,"status":status,**details},secret)
        directory.mkdir(parents=True,exist_ok=True)
        with (directory / "run.log.jsonl").open("a",encoding="utf-8") as stream:
            stream.write(json.dumps(row,ensure_ascii=False) + "\n")
        record["module_statuses"][stage] = status
        atomic_json(directory / "result.json",record)
    def finish(status, code, **details):
        record.update(status=status,exit_code=code,ended_at=datetime.now(SHANGHAI).isoformat(),
                      duration_seconds=round(elapsed_time.monotonic()-began,3),**details)
        atomic_json(directory / "result.json",_redact(record,secret))
        return record
    try:
        with ProcessLock(project / "data/operations/daily.lock",run_id):
            # Only after the OS lock is acquired can abandoned running records be marked.
            for old in read_runs(root, scope=config.scope_mode, research_mode="full_market"):
                if old.get("status") == "running":
                    old.update(status="interrupted",exit_code=2,ended_at=datetime.now(SHANGHAI).isoformat(),
                               failure_reason="前次进程已结束且系统锁已释放；未自动重调模型")
                    recovery = (root / "runs" / old["run_id"] / "result.json").resolve()
                    if recovery.parent.parent != (root / "runs").resolve():
                        raise ValueError("旧运行记录恢复路径越界")
                    atomic_json(recovery,old)
            log("start","running")
            if scheduled and now < clock_value(times["planned_trigger_at"]):
                return finish("not_due",0,failure_reason="计划触发时刻未到；手动提前触发只记录状态，不调用外部接口")
            state = input_state(database,local_path(project,config.adjusted_directory),symbols,target,times["cutoff_at"])
            for old in read_runs(root, scope=config.scope_mode, research_mode="full_market"):
                if not force and old.get("task_identity") in matching_identities and old.get("input_state") == state and old.get("generation_status") == "ok" and valid_report(old.get("report",{}),anchor=project):
                    log("reuse","ok",original_run_id=old["run_id"])
                    return finish("reused",0,generation_status="ok",report=relocated_report(old["report"],anchor=project),input_state=state,
                                  reused_from=old["run_id"],report_status=old.get("report_status",old["status"]))
            if scheduled:
                attempts = [r for r in read_runs(root) if r["run_id"] != run_id and r.get("trigger_kind") == "scheduled"
                            and r.get("started_at","")[:10] == now.date().isoformat() and r.get("status") not in {"reused","already_running","dry_run","not_due"}]
                if len(attempts) >= config.scheduled_attempts_per_day:
                    return finish("catchup_limit",2,failure_reason="本日自动尝试上限已达；不自动回补多日历史")
            try:
                trading = services.ensure_calendar(database,target,config,directory / "work")
            except Exception as exc:
                log("calendar","failed",error=str(exc))
                return finish("calendar_unavailable",2,failure_reason=_redact("交易日历未通过；不按星期推断。" + str(exc),secret))
            log("calendar","trading_day" if trading else "non_trading_day" if trading is False else "unknown")
            if trading is False:
                return finish("non_trading_day",0)
            if trading is not True:
                return finish("calendar_unavailable",2)
            if target == now.date() and now.hour < 16:
                return finish("not_due",0,failure_reason="尚未到本项目盘后数据检查时段；没有模型调用")
            work = directory / "work"
            collection = services.collect_market(project,config,sample,target,work)
            log("market_collection",collection.get("status","unknown"),result=collection.get("run_directory"))
            market = services.freeze_market(project,config,target,collection,work)
            if collection.get('eligibility_collection'):
                record['eligibility_sources'] = collection['eligibility_collection']
            record["market_result"] = market
            if market.get("actual_market_date") != target.isoformat():
                log("market","stale",actual_market_date=market.get("actual_market_date"))
                return finish("market_stale",2,actual_market_date=market.get("actual_market_date"),failure_reason="目标日行情未取得，不用旧行情发布当日报告")
            log("market",market["status"])
            try:
                materials = services.materials(project,config,symbols,times,work)
            except Exception as exc:
                log("messages","failed",error=str(exc))
                materials = LiveServices().failed_materials(project,config,times,work)
            record["sources"] = materials.get("source_health",[])
            log("messages",materials.get("status","unknown"))
            ledger = BudgetLedger(project / "data/operations/runtime.sqlite3")
            guard = LedgerGuard(ledger,run_id,config.model_max_calls_per_day)
            researched = services.research(project,config,market,materials,times,settings,guard,work,skip_model)
            raw_report = json.loads(Path(researched["json"]).read_text(encoding="utf-8"))
            record["model_summary"] = raw_report["model_run"]
            record["module_statuses"].update(raw_report["statuses"])
            record["input_snapshot_id"] = researched["input_snapshot_id"]
            record["actual_market_date"] = researched["actual_market_date"]
            record["actual_generated_at"] = raw_report["actual_generated_at"]
            record["budget"] = ledger.summary(datetime.now(SHANGHAI).date().isoformat())
            published = publish_report(researched,root,record)
            state = input_state(database,local_path(project,config.adjusted_directory),symbols,target,times["cutoff_at"])
            status = "partial" if raw_report["statuses"]["model"] in {"ok","partial_validated"} else "market_only"
            log("publish","ok",report_directory=published["run_directory"])
            return finish(status,1,generation_status="ok",report=published,report_status=status,input_state=state)
    except AlreadyRunning:
        return finish("already_running",3,failure_reason="另一个日任务或备份正在运行；本次未发起采集或模型")
    except Exception as exc:
        log("failure","failed",error=str(exc),error_type=type(exc).__name__)
        return finish("failed",2,failure_reason=_redact(str(exc),secret))


def run_daily(**kwargs) -> dict:
    """Startup failures also become visible records, never raw configuration dumps."""
    try:
        # Live callers and the CLI share the approved SSE/SZSE default. The original
        # explicitly injected, isolated sample service API remains compatible.
        if "config_path" not in kwargs and kwargs.get("services") is None:
            kwargs["config_path"] = "config/m4.json" if kwargs.get("sample_mode") else "config/sse_szse_daily.json"
        return _run_daily(**kwargs)
    except Exception as exc:
        project = Path(kwargs.get("project",PROJECT)).resolve()
        now = datetime.now(SHANGHAI)
        run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
        root = project / "outputs/research/m4" / ("previews" if kwargs.get("dry_run") else "runs") / run_id
        result = {"schema_version":"m4-run-v1","run_id":run_id,"started_at":now.isoformat(),"ended_at":now.isoformat(),
            "status":"configuration_failed","generation_status":"not_run","exit_code":2,"duration_seconds":0,
            "failure_reason":str(exc) if isinstance(exc, (F2RuntimeLimitError, SectorDailyModeError)) else "配置或启动校验失败：" + type(exc).__name__ + "；值未输出，请核对项目配置路径和字段",
            "run_directory":str(root),"model_summary":{"call_count":0},"external_calls_allowed":False}
        atomic_json(root / "result.json",result)
        return result
