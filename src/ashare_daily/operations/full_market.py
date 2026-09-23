"""Scoped daily discovery and optional F2 collection; no screening or publication."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
from pathlib import Path
import time
from uuid import uuid4

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.operations.daily import atomic_json, clock_value, local_path, read_runs, resolve_times
from ashare_daily.operations.lock import AlreadyRunning, ProcessLock


def run_full_market_daily(*, project: Path, config, config_path: str, target: date | None,
                          cutoff: str | None, start: str | None, now: datetime,
                          dry_run: bool, scheduled: bool, planned: str | None,
                          f2_max_seconds: float | None = None) -> dict:
    from ashare_daily.universe_service import load_universe_config, sync_date
    if not config.universe_config:
        raise ValueError("动态范围缺少 universe_config，禁止退回固定样本")
    universe = load_universe_config(project, config.universe_config)
    if universe.scope != config.scope_mode:
        raise ValueError("日任务范围与名单配置范围不一致")
    scope = config.scope_mode
    scope_label = "沪深A股全市场，暂不含北交所" if scope == "sse_szse_a" else "全A股五板块，包含北交所"
    root = local_path(project, config.output_directory)
    times = resolve_times(target, cutoff, start, now, config, read_runs(root, scope=scope, research_mode="full_market"), planned)
    target = date.fromisoformat(times["target_trade_date"])
    run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
    directory = root / ("previews" if dry_run else "runs") / run_id
    began = time.monotonic()
    record = {"schema_version": "m4-run-v1", "run_id": run_id, **times,
        "requested_date": target.isoformat(), "resolved_trade_date": None,
        "started_at": now.isoformat(), "ended_at": None, "status": "running",
        "generation_status": "not_run", "scope": scope, "scope_label": scope_label,
        "workflow_version": config.workflow_version, "universe_config_version": universe.config_version,
        "collection_ready": False, "research_ready": False,
        "f2_runtime_limit_seconds": f2_max_seconds,
        "implementation_stage": "F1.1" if any(s.provider == "exchange_lists" and s.enabled for s in universe.sources) else "F1",
        "capability_stage": "f1_discovery_only", "configured_stock_count": None,
        "required_boards": universe.required_boards, "module_statuses": {},
        "model_summary": {"call_count": 0, "status": "not_run"}, "sources": [],
        "trigger_kind": "scheduled" if scheduled else "manual", "run_directory": str(directory),
        "external_calls_allowed": not dry_run, "exit_code": None,
        "config_versions": {str(path): hashlib.sha256(local_path(project, path).read_bytes()).hexdigest()
                            for path in (config_path, config.universe_config, config.f2_config) if path}}

    def finish(status: str, code: int, **details):
        completed = datetime.now(SHANGHAI).isoformat()
        record.update(status=status, exit_code=code, ended_at=completed, completed_at=completed,
                      duration_seconds=round(time.monotonic() - began, 3), **details)
        atomic_json(directory / "result.json", record)
        return record

    if dry_run:
        return finish("dry_run", 0, project_root=str(project),
            steps=["核对有来源的目标日历", "动态发现并保存配置范围名单及未知项", "核验名单完整性",
                   "名单通过后执行F2增量行情与质量核验" if config.f2_config else "停止于F2待验收入口",
                   "本轮不运行F3筛选、F4模型研究或日报发布"],
            failure_reason="预览；未采集、未扫描、未生成新报告")
    try:
        with ProcessLock(project / "data/operations/daily.lock", run_id):
            for old in read_runs(root, scope=scope, research_mode="full_market"):
                if old.get("status") == "running":
                    old.update(status="interrupted", exit_code=2, ended_at=datetime.now(SHANGHAI).isoformat(),
                               failure_reason="前次进程已结束，锁已释放；未自动调用模型")
                    atomic_json(root / "runs" / old["run_id"] / "result.json", old)
            atomic_json(directory / "result.json", record)
            if scheduled and now < clock_value(times["planned_trigger_at"]):
                return finish("not_due", 0, failure_reason="计划触发时刻未到")
            if scheduled:
                attempts = [r for r in read_runs(root) if r["run_id"] != run_id
                    and r.get("trigger_kind") == "scheduled" and r.get("started_at", "")[:10] == now.date().isoformat()
                    and r.get("status") not in {"reused", "already_running", "dry_run", "not_due"}]
                if len(attempts) >= config.scheduled_attempts_per_day:
                    return finish("catchup_limit", 2, failure_reason="本日自动尝试上限已达")
            if target == now.date() and now.hour < 16:
                return finish("not_due", 0, failure_reason="尚未到盘后检查时段；可使用 universe sync 单独核验名单")
            result = sync_date(project=project, config_path=config.universe_config,
                               target=target, cutoff_at=times["cutoff_at"], now=now)
            record.update(universe_result=result, universe_snapshot_id=result.get("snapshot_id"),
                board_counts=result.get("board_counts"), configured_stock_count=result.get("ordinary_a_count"),
                resolved_trade_date=result.get("resolved_trade_date"),
                collection_ready=result.get("collection_ready") is True,
                module_statuses={"calendar": result["calendar"]["status"], "universe": result["status"],
                    "market": "not_run_F2", "screening": "not_run_F3", "research": "not_run_F4", "publish": "not_run"})
            if result["status"] == "non_trading_day":
                return finish("non_trading_day", 0)
            if result["calendar"]["status"] != "verified":
                return finish("calendar_unverified", 2, failure_reason="无法确认目标交易日；不按工作日或空结果推断")
            if not result.get("universe_verified"):
                return finish("universe_blocked", 2, failure_reason=scope_label + "名单未通过核验；缺口详见 universe_result")
            if not config.f2_config:
                return finish("f2_pending", 2, failure_reason="名单已核验；此配置未启用F2行情，后续扫描尚未验收，保留旧报告")
            if (result.get("scope") != scope or not record["collection_ready"]
                    or result.get("resolved_trade_date") != target.isoformat() or not result.get("snapshot_path")):
                return finish("universe_blocked", 2, failure_reason="F2名单前置条件未齐备：须有同范围、同交易日、可采集的冻结名单快照")
            # This function already holds the shared lock. The market service is
            # deliberately lock-free so daily and CLI use the same outer lock.
            from ashare_daily.market_pipeline import run_market
            market, code = run_market(project_root=project, config_path=config.f2_config,
                target_date=target, operation="update", universe_snapshot_path=result["snapshot_path"],
                max_seconds=f2_max_seconds)
            if market.get("status") not in {"f2_partial", "f2_complete", "f2_blocked"}:
                raise ValueError("F2服务返回未知状态")
            if (market["status"] == "f2_complete") != (code == 0):
                raise ValueError("F2状态与退出码不一致")
            record.update(f2_result=market, market_job_id=market.get("job_id"),
                market_output_directory=market.get("output_directory"), market_quality_path=market.get("quality_path"),
                capability_stage="f2_market_collection", implementation_stage="F2")
            record["module_statuses"]["market"] = market["status"]
            return finish(market["status"], code,
                failure_reason="F2行情与质量核验已完成；F3筛选、F4研究及新日报尚未运行" if code == 0
                    else "F2行情或质量核验存在缺口，详见质量报告；未运行筛选、模型或发布")
    except AlreadyRunning:
        return finish("already_running", 3, failure_reason="另一个日任务或备份持锁；未访问数据源")
    except Exception as exc:
        return finish("failed", 2, failure_reason="动态日任务失败：" + type(exc).__name__ + "；请核对配置及归档响应")
