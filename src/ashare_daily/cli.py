"""Offline demo, bounded data/research and standalone M4 daily operations."""

import argparse
from datetime import date
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo


def iso_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD，例如 2026-09-09")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期不存在，请使用有效的 YYYY-MM-DD") from exc


def positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("耗时上限必须是正秒数") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("耗时上限必须是有限正秒数")
    return parsed


def market_runtime_seconds(value: str) -> float:
    parsed = positive_seconds(value)
    if parsed > 14400:
        raise argparse.ArgumentTypeError("单次F2耗时上限不得超过14400秒")
    return parsed


def doctor_offline() -> dict:
    checks = []

    def check(name, action):
        try:
            detail = action()
            checks.append({"name": name, "status": "ok", "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "status": "failed", "detail": str(exc)})

    def python_check():
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("本项目要求 Python 3.12")
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("请使用项目 .venv 中的 Python")
        return {"version": sys.version.split()[0], "executable": sys.executable}

    def sqlite_check():
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE demo_check(value TEXT)")
            connection.execute("INSERT INTO demo_check VALUES (?)", ("DEMO",))
            assert connection.execute("SELECT value FROM demo_check").fetchone()[0] == "DEMO"
        return {"version": sqlite3.sqlite_version, "probe": "in_memory_write_read_ok"}

    def demo_check():
        from ashare_daily.demo import build_demo_report
        from ashare_daily.reports.render import render_html, render_markdown

        report = build_demo_report(date(2026, 9, 9))
        assert "DEMO" in render_markdown(report) and "DEMO" in render_html(report)
        return "人工合成 fixture、schema 和两种文本渲染通过；未测试外部源"

    check("python_virtual_environment", python_check)
    for package in ("pandas", "pydantic", "tzdata", "pytest", "baostock"):
        check(package, lambda package=package: version(package))
    check("Asia/Shanghai", lambda: str(ZoneInfo("Asia/Shanghai")))
    check("sqlite", sqlite_check)
    check("demo_pipeline", demo_check)
    return {
        "mode": "demo",
        "status": "ok" if all(c["status"] == "ok" for c in checks) else "failed",
        "network_access": "disabled",
        "external_sources": "not_tested",
        "model_api": "not_used",
        "notice": "M0 离线环境检查；SDK 可导入、真实接口连接、取数及许可均未验证。",
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    # Explicit UTF-8 makes Windows output and redirected logs readable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="A股今日方向简报 Agent｜M0 DEMO / M1 行情 / M2 量价 / M3 研究 / M4 日常运行")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="离线环境检查，或明确指定 BaoStock 实际联网检查")
    doctor_mode = doctor.add_mutually_exclusive_group(required=True)
    doctor_mode.add_argument("--offline", action="store_true")
    doctor_mode.add_argument("--source", choices=["baostock"])
    doctor_mode.add_argument("--model", action="store_true", help="M3：安全检查本项目研究模型，不修改 Codex 配置")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    doctor.add_argument("--date", type=iso_date, help="目标交易日；省略时从交易日历选择已完成交易日")
    doctor.add_argument("--output-dir", type=Path, default=Path("outputs/research/m1"))
    doctor.add_argument("--timeout", type=float, default=20, help="单次 SDK 子进程硬超时秒数（最多120）")
    doctor.add_argument("--attempts", type=int, default=2, help="最多尝试1至3次；权限/限流错误不重试")
    collect = commands.add_parser("collect", help="M1：先验证单日，再增量采集固定小样本约一年未复权日线")
    collect.add_argument("--date", type=iso_date, help="目标交易日；省略时用实际交易日历选择")
    collect.add_argument("--start-date", type=iso_date, help="起始日期；默认目标日前365天，最多366个自然日")
    collect.add_argument("--db", type=Path, default=Path("data/research/market.sqlite3"))
    collect.add_argument("--output-dir", type=Path, default=Path("outputs/research/m1"))
    collect.add_argument("--refresh", action="store_true", help="明确刷新本次小样本区间，保留旧内容版本")
    collect.add_argument("--timeout", type=float, default=20)
    collect.add_argument("--attempts", type=int, default=2)
    check_data = commands.add_parser("check-data", help="M1：离线核查本地日线覆盖、缺口及日期，并导出 CSV")
    check_data.add_argument("--date", type=iso_date, required=True)
    check_data.add_argument("--start-date", type=iso_date, required=True)
    check_data.add_argument("--db", type=Path, default=Path("data/research/market.sqlite3"))
    check_data.add_argument("--output-dir", type=Path, default=Path("outputs/research/m1"))
    sample_collect = commands.add_parser("collect-sample", help="M2.1：按固定配置增量获取扩展样本必要窗口，不改变 M1 默认范围")
    sample_collect.add_argument("--sample-file", type=Path, required=True)
    sample_collect.add_argument("--date", type=iso_date, required=True)
    sample_collect.add_argument("--db", type=Path, default=Path("data/research/market.sqlite3"))
    sample_collect.add_argument("--output-dir", type=Path, default=Path("outputs/research/m21_collection"))
    sample_collect.add_argument("--adjusted-dir", type=Path, default=Path("data/research/m21_adjusted"))
    materials = commands.add_parser("collect-materials", help="M3：获取登记的少量资料并归档，不改变行情股票池")
    material_start = materials.add_mutually_exclusive_group(required=True)
    material_start.add_argument("--start", help="资讯查询起点，ISO 时间须含 +08:00 时区")
    material_start.add_argument("--previous-cutoff", help="以上一期资讯截点作为增量起点")
    materials.add_argument("--cutoff", required=True, help="资讯截点，独立于行情分析日")
    materials.add_argument("--sources", type=Path, default=Path("config/m3_sources.json"))
    materials.add_argument("--market-snapshot", help="已有 M2.1 快照；省略时使用当前 latest 所指冻结样本")
    materials.add_argument("--db", type=Path, default=Path("data/research/market.sqlite3"))
    materials.add_argument("--output-dir", type=Path, default=Path("outputs"))
    materials.add_argument("--import-file", type=Path, help="带真实出处和日期的人工资料包")
    materials.add_argument("--import-only", action="store_true", help="只导入本地资料，不宣称自动采集")
    brief = commands.add_parser("brief", help="生成 DEMO，或基于冻结真实行情的 M2 日报")
    brief.add_argument("--mode", choices=["demo", "research"], required=True)
    brief.add_argument("--empty-candidates", action="store_true", help="演示零候选报告")
    screen = commands.add_parser("screen", help="M2：冻结行情或重放快照，计算规则并输出日报和逐股核验表")
    for m2_parser in (brief, screen):
        target_group = m2_parser.add_mutually_exclusive_group(required=True)
        target_group.add_argument("--date", type=iso_date, help="分析交易日；DEMO 模式下为合成场景日期")
        target_group.add_argument("--snapshot", help="M2 冻结快照 ID 或 JSON 文件路径；完全离线重放")
        target_group.add_argument("--replay-report", type=Path, help="M3：重放已存档研究报告，完全不调用模型")
        m2_parser.add_argument("--output-dir", type=Path, default=Path("outputs"), help="输出根目录；自动隔离 demo / research/m2")
        m2_parser.add_argument("--config", type=Path, default=Path("config/m2.json"), help="M2 版本化策略参数；重放时使用快照内配置")
        m2_parser.add_argument("--db", type=Path, default=Path("data/research/market.sqlite3"))
        m2_parser.add_argument("--adjusted-dir", type=Path, default=Path("data/research/m2_adjusted"))
        m2_parser.add_argument("--adjusted-manifest", type=Path, help="使用并校验已有完整调整数据包")
        m2_parser.add_argument("--fetch-adjusted", action="store_true", help="缺少缓存时联网补齐必要调整窗口；已有合格缓存则复用")
        m2_parser.add_argument("--refresh-adjusted", action="store_true", help="明确联网重新获取整个小样本依赖窗口，保存新版本")
        m2_parser.add_argument("--eligibility-evidence", type=Path, help="M2.1：导入有来源与适用日期的人工资格核验包；不得用于旧快照重放")
        m2_parser.add_argument("--research-enhanced", action="store_true", help="明确启用 M3 证据研究；旧命令行为保持")
        m2_parser.add_argument("--evidence-bundle", type=Path, help="collect-materials 返回的不可变资料包")
        m2_parser.add_argument("--start", help="M3 资讯区间起点，须含时区")
        m2_parser.add_argument("--cutoff", help="M3 资讯分析截点，须含时区")
        m2_parser.add_argument("--m3-config", type=Path, default=Path("config/m3.json"))
        m2_parser.add_argument("--env-file", type=Path, default=Path(".env"))
        m2_parser.add_argument("--model-capabilities", type=Path, help="本服务/模型已实际检测的能力记录")
        m2_parser.add_argument("--skip-model", action="store_true", help="M3：显式跳过模型，输出资料与量价降级版")
    daily = commands.add_parser("run-daily", help="M4：独立日任务，网页无需启动")
    daily.add_argument("--config", help="默认沪深A股F2配置，暂不含北交所；--sample 时默认旧M4样本配置")
    daily.add_argument("--sample", action="store_true", help="显式手动运行旧固定样本，不能用于生产调度")
    daily.add_argument("--date", type=iso_date, help="历史研究只处理明确指定的一天")
    daily.add_argument("--cutoff", help="资料截点，含时区；默认目标日21:00，提前运行取实际开始时刻")
    daily.add_argument("--start", help="资料区间起点，含时区；默认上一截点或配置回看区间")
    daily.add_argument("--dry-run", action="store_true", help="只预览，不访问接口或调用模型")
    daily.add_argument("--force", action="store_true", help="显式重新研究并生成新版本；仍受日累计预算限制")
    daily.add_argument("--scheduled", action="store_true", help="计划任务入口，单日有限补尝试")
    daily.add_argument("--planned-trigger", help="带时区的计划触发时间，另记实际开始时刻")
    daily.add_argument("--skip-model", action="store_true", help="真实采集和量价流程，显式跳过模型")
    daily.add_argument("--market-max-seconds", type=market_runtime_seconds,
        help="仅本次F2行情步骤耗时上限，须大于0且不超过14400秒；不修改配置或预算")
    backup = commands.add_parser("backup", help="M4：数据库及必要存档的一致备份，不包含.env")
    backup.add_argument("--destination", type=Path, help="必须是未存在的新目录；省略则在backups下生成唯一目录")
    restore = commands.add_parser("restore", help="M4：仅恢复到独立的新目录，不覆盖当前数据")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    universe = commands.add_parser("universe", help="F1：动态名单、状态、别名与完整性快照，默认沪深四板块")
    universe_commands = universe.add_subparsers(dest="universe_command", required=True)
    universe_sync = universe_commands.add_parser("sync", help="核验目标交易日并动态刷新获准来源；缺板块则阻塞")
    universe_sync.add_argument("--date", type=iso_date, required=True)
    universe_sync.add_argument("--config", default="config/sse_szse_universe.json")
    universe_sync.add_argument("--cutoff", help="带时区资料截点；默认实际开始时间，历史补采保留实际观察时间")
    market = commands.add_parser("market", help="F2：冻结名单范围的行情初始化、增量更新与中断恢复")
    market_commands = market.add_subparsers(dest="market_command", required=True)
    for operation in ("bootstrap", "update", "resume"):
        operation_parser = market_commands.add_parser(operation)
        operation_parser.add_argument("--config", default="config/sse_szse_market.json")
        operation_parser.add_argument("--max-seconds", type=positive_seconds, help="本次运行耗时上限，结束后可 resume")
        if operation == "resume":
            operation_parser.add_argument("--job", required=True, help="原有F2任务编号；沿用其冻结范围和目标日")
        else:
            operation_parser.add_argument("--date", type=iso_date, required=True)
            operation_parser.add_argument("--universe-snapshot", type=Path, help="已核验的同范围名单快照；省略则查找本地快照")
    provider_check = market_commands.add_parser("provider-check", help="Provider许可/四板块样本检查；默认不联网、不写行情库")
    provider_check.add_argument("--provider", choices=("baostock", "eastmoney"), required=True)
    provider_check.add_argument("--date", type=iso_date, required=True)
    provider_check.add_argument("--config", default="config/sse_szse_market_providers.json")
    provider_check.add_argument("--online", action="store_true", help="许可已确认后才实际请求四板块样本；不代表全市场验收")
    quality = commands.add_parser("quality", help="F2：只读查看已保存任务的质量核验")
    quality_commands = quality.add_subparsers(dest="quality_command", required=True)
    quality_report = quality_commands.add_parser("report")
    quality_report.add_argument("--job", required=True)
    quality_report.add_argument("--config", default="config/sse_szse_market.json")
    sector = commands.add_parser("sector", help="F2-S1/F3-S：冻结行业范围、按需历史与确定性技术观察；无模型研究")
    sector_commands = sector.add_subparsers(dest="sector_command", required=True)
    for operation in ("catalog", "select", "run", "prepare", "resume", "report"):
        entry = sector_commands.add_parser(operation)
        entry.add_argument("--config", default="config/sector_first.json")
        entry.add_argument("--dry-run", action="store_true", help="只列计划，零网络、零模型调用")
        entry.add_argument("--max-seconds", type=market_runtime_seconds)
        if operation in {"prepare", "resume", "report"}:
            entry.add_argument("--selection", required=True, help="冻结selection_id；不刷新行业或成员")
        else:
            entry.add_argument("--date", type=iso_date, required=True)
            entry.add_argument("--cutoff", help="带时区截点，默认目标日21:00；历史补采保留实际观察时间")
            entry.add_argument("--source-run", help="复用同名单、同日历且原始哈希通过的来源归档；只补未完成轻量步骤")
            entry.add_argument("--reuse-only", action="store_true", help="只复验--source-run的真实归档，不发新的轻量请求")
            entry.add_argument("--status-evidence", action="append", help="重新核验有出处的单日停牌响应归档；不从零价猜测状态")
    for operation in ("validation-freeze", "validation-prepare", "validation-resume", "validation-benchmark", "screen", "technical-report"):
        entry = sector_commands.add_parser(operation)
        entry.add_argument("--dry-run", action="store_true")
        entry.add_argument("--max-seconds", type=market_runtime_seconds, default=600)
        entry.add_argument("--validation-config", default="config/sector_validation.json")
        entry.add_argument("--strategy-config", default="config/sector_screening.json")
        if operation == "validation-freeze":
            entry.add_argument("--source-selection", required=True, help="完整核验的真实生产来源快照；不改原选择")
        else:
            entry.add_argument("--selection", required=True, help="显式冻结选择编号，生产/工程用途不可混用")
        if operation == "validation-benchmark":
            entry.add_argument("--online", action="store_true", help="仅在已登记许可内补一个必要指数窗口，默认只查缓存")
    for operation in ("f4s1", "f4s1-resume", "f4s1-report", "gaps", "qualify"):
        entry = sector_commands.add_parser(operation, help="F4-S1手动资格与证据准备；模型调用为零")
        entry.add_argument("--selection", required=True, help="显式冻结选择编号；成员与生产/工程用途保持不变")
        entry.add_argument("--validation-config", default="config/sector_validation.json")
        entry.add_argument("--strategy-config", default="config/sector_screening_f4s1.json")
        entry.add_argument("--dry-run", action="store_true", help="只列计划，不联网、不写证据、不调用模型")
        entry.add_argument("--max-seconds", type=market_runtime_seconds, default=120)
        if operation in {"gaps", "qualify"}:
            entry.add_argument("--online", action="store_true", help="仅在现有许可和有限请求范围内显式补证；默认离线")
        if operation == "gaps":
            entry.add_argument("--source-revision", help="复用已归档的定向历史补证版本")
        if operation in {"qualify", "f4s1"}:
            entry.add_argument("--eligibility-evidence", type=Path, required=operation == "f4s1",
                help="重新核验已取得的字段级资格证据包及其原始来源")
        if operation == "f4s1":
            entry.add_argument("--gap-diagnosis", type=Path, required=True, help="已有逐日期历史缺口诊断；不覆盖旧报告")
        if operation in {"f4s1-resume", "f4s1-report"}:
            entry.add_argument("--revision", required=True, help="原F4-S1修订版本；复用冻结输入")
    sector_publish = sector_commands.add_parser("publish-report", help="从冻结证据生成独立本地日报；不采集、不调用模型")
    sector_publish.add_argument("--selection", required=True, help="原冻结选择编号；工程与生产报告保持隔离")
    sector_publish.add_argument("--revision", help="工程报告对应的已完成F4-S1修订")
    sector_publish.add_argument("--validation-config", default="config/sector_validation.json")
    sector_publish.add_argument("--dry-run", action="store_true", help="只验证发布输入，不写报告或更新索引")
    sector_publish.add_argument("--report-id", help="只读复验已有报告编号及原始文件哈希")
    args = parser.parse_args(argv)
    try:
        if args.command == "sector":
            from ashare_daily.operations.daily import PROJECT
            from ashare_daily.operations.lock import AlreadyRunning, ProcessLock
            from ashare_daily.sector_pipeline import run_sector
            from uuid import uuid4
            if args.sector_command == "publish-report":
                from ashare_daily.reports.sector_research import publish_sector_report
                kwargs = {"selection_id": args.selection, "revision": args.revision,
                    "validation_config_path": args.validation_config, "dry_run": args.dry_run, "report_id": args.report_id}
                try:
                    if args.dry_run:
                        result = publish_sector_report(PROJECT, **kwargs)
                    else:
                        with ProcessLock(PROJECT / "data/operations/daily.lock", "sector-report-" + uuid4().hex):
                            result = publish_sector_report(PROJECT, **kwargs)
                    code = 0
                except AlreadyRunning:
                    result, code = {"status": "already_running", "model_calls": 0}, 3
                print(json.dumps({**result, "exit_code": code}, ensure_ascii=False, indent=2))
                return code
            if args.sector_command in {"f4s1", "f4s1-resume", "f4s1-report", "gaps", "qualify"}:
                from ashare_daily.sector_f4s1 import run_f4s1
                try:
                    if args.dry_run:
                        result, code = run_f4s1(PROJECT, args)
                    else:
                        with ProcessLock(PROJECT / "data/operations/daily.lock", "f4s1-" + uuid4().hex):
                            result, code = run_f4s1(PROJECT, args)
                except AlreadyRunning:
                    result, code = {"status": "already_running", "model_calls": 0}, 3
                print(json.dumps({**result, "exit_code": code}, ensure_ascii=False, indent=2))
                return code
            if args.sector_command in {"validation-freeze", "validation-prepare", "validation-resume", "validation-benchmark", "screen", "technical-report"}:
                from ashare_daily.sector_workflow import run_f3s
                try:
                    if args.dry_run:
                        result, code = run_f3s(PROJECT, args)
                    else:
                        with ProcessLock(PROJECT / "data/operations/daily.lock", "f3s-" + uuid4().hex):
                            result, code = run_f3s(PROJECT, args)
                except AlreadyRunning:
                    result, code = {"status": "already_running", "model_calls": 0}, 3
                print(json.dumps({**result, "exit_code": code}, ensure_ascii=False, indent=2))
                return code
            kwargs = {"root": PROJECT, "config_path": args.config, "operation": args.sector_command,
                "target": getattr(args, "date", None), "selection_id": getattr(args, "selection", None),
                "cutoff": getattr(args, "cutoff", None), "dry_run": args.dry_run, "max_seconds": args.max_seconds}
            kwargs["source_run"] = getattr(args, "source_run", None)
            kwargs["reuse_only"] = getattr(args, "reuse_only", False)
            kwargs["status_evidence"] = getattr(args, "status_evidence", None)
            try:
                if args.dry_run or args.sector_command == "report":
                    result, code = run_sector(**kwargs)
                else:
                    with ProcessLock(PROJECT / "data/operations/daily.lock", "sector-" + uuid4().hex):
                        result, code = run_sector(**kwargs)
            except AlreadyRunning:
                result, code = {"status": "already_running", "model_calls": 0}, 3
            # Prices and source payloads stay in local artifacts, outside this summary.
            summary = {key: result[key] for key in ("status", "target_date", "selection_id", "counts",
                "report_path", "json_path", "reason", "evidence_directory", "model_calls", "network_requests",
                "steps", "operation", "market_scope", "research_mode") if key in result}
            summary["exit_code"] = code
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return code
        if args.command in {"market", "quality"}:
            from ashare_daily.operations.daily import PROJECT
            from ashare_daily import market_pipeline
            if args.command == "quality":
                result = market_pipeline.quality_report(PROJECT, args.config, args.job)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            from ashare_daily.operations.lock import AlreadyRunning, ProcessLock
            from uuid import uuid4
            if args.market_command == "provider-check":
                from ashare_daily.provider_diagnostics import provider_check
                try:
                    with ProcessLock(PROJECT / "data/operations/daily.lock", "provider-check-" + uuid4().hex):
                        result, code = provider_check(project_root=PROJECT, config_path=args.config,
                            provider=args.provider, target_date=args.date.isoformat(), online=args.online)
                except AlreadyRunning:
                    print(json.dumps({"status": "already_running", "exit_code": 3}, ensure_ascii=False))
                    return 3
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return code
            arguments = {"project_root": PROJECT, "config_path": args.config,
                "operation": args.market_command, "max_seconds": args.max_seconds}
            if args.market_command == "resume":
                arguments["job_id"] = args.job
            else:
                arguments.update(target_date=args.date, universe_snapshot_path=args.universe_snapshot)
            try:
                with ProcessLock(PROJECT / "data/operations/daily.lock", "market-" + uuid4().hex):
                    result, code = market_pipeline.run_market(**arguments)
            except AlreadyRunning:
                print(json.dumps({"status": "already_running", "exit_code": 3}, ensure_ascii=False))
                return 3
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return code
        if args.command == "universe":
            from ashare_daily.operations.daily import PROJECT
            from ashare_daily.operations.lock import AlreadyRunning, ProcessLock
            from ashare_daily.universe_service import sync_date
            from uuid import uuid4
            try:
                with ProcessLock(PROJECT / "data/operations/daily.lock", "universe-" + uuid4().hex):
                    result = sync_date(project=PROJECT, config_path=args.config, target=args.date, cutoff_at=args.cutoff)
            except AlreadyRunning:
                print(json.dumps({"status": "already_running", "exit_code": 3}, ensure_ascii=False))
                return 3
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("universe_verified") or result["status"] == "non_trading_day" else 2
        if args.command in {"run-daily", "backup", "restore"}:
            import os
            from ashare_daily.operations.daily import PROJECT, run_daily
            os.chdir(PROJECT)
            if args.command == "run-daily":
                if args.sample and args.scheduled:
                    parser.error("--sample 仅供手动测试，不能与 --scheduled 同时使用")
                if args.sample and args.market_max_seconds is not None:
                    parser.error("--market-max-seconds 仅用于配置F2行情的动态名单日任务，不能与 --sample 同时使用")
                result = run_daily(config_path=args.config or ("config/m4.json" if args.sample else "config/sse_szse_daily.json"), target=args.date, cutoff=args.cutoff, start=args.start,
                    dry_run=args.dry_run, force=args.force, scheduled=args.scheduled,
                    planned=args.planned_trigger, skip_model=args.skip_model, sample_mode=args.sample,
                    f2_max_seconds=args.market_max_seconds)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return result["exit_code"]
            from ashare_daily.operations.lock import AlreadyRunning, ProcessLock
            from ashare_daily.operations.backup import create_backup, restore_backup
            from contextlib import ExitStack
            from uuid import uuid4
            try:
                with ExitStack() as locks:
                    owner = "backup-" + uuid4().hex
                    locks.enter_context(ProcessLock(PROJECT / "data/operations/daily.lock", owner))
                    if args.command == "backup":
                        locks.enter_context(ProcessLock(PROJECT / "data/engineering_validation/f3s/history.lock", owner))
                        result = create_backup(PROJECT, args.destination or (PROJECT / "backups" / ("m4-" + uuid4().hex)))
                    else:
                        result = restore_backup(args.backup, args.destination)
            except AlreadyRunning:
                print(json.dumps({"status":"already_running", "exit_code":3,
                    "reason":"日任务或备份正在运行，本次未修改任何数据库或归档"}, ensure_ascii=False))
                return 3
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "collect-materials":
            from ashare_daily.research.preparation import market_baseline
            from ashare_daily.research.runner import archive_materials, _interval
            start, cutoff = _interval(args.start or args.previous_cutoff, args.cutoff)
            frozen, market, _ = market_baseline(args.market_snapshot)
            if frozen.get("verification_kind") == "offline_test":
                parser.error("真实资料采集不能使用离线模拟股票池")
            collection = None
            if args.import_only and not args.import_file:
                parser.error("--import-only 需要 --import-file")
            if not args.import_only:
                from ashare_daily.research.sources import collect_materials
                collection = collect_materials(registry_path=args.sources, start=start, cutoff=cutoff,
                    sample_symbols={row['symbol']: row['name'] for row in market['evaluations']},
                    output_dir=args.output_dir / "research" / "m3" / "acquisition", previous_cutoff=args.previous_cutoff)
            result = archive_materials(collection=collection, import_file=args.import_file, database=args.db,
                                       output_dir=args.output_dir, start=start, cutoff=cutoff)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "ok" else 1
        if args.command == "doctor" and args.model:
            if args.date:
                parser.error("模型检查不接受行情 --date")
            from ashare_daily.research.model import doctor_model
            directory = Path("outputs/research/m3_model") if args.output_dir == Path("outputs/research/m1") else args.output_dir
            result = doctor_model(output_dir=directory, env_file=args.env_file)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "ok" else 1
        if args.command in {"brief", "screen"} and args.research_enhanced:
            if args.command != "brief" or args.mode != "research":
                parser.error("M3 只通过 brief --mode research --research-enhanced 明确启用")
            if args.fetch_adjusted or args.refresh_adjusted or args.adjusted_manifest or args.eligibility_evidence or args.empty_candidates:
                parser.error("M3 复用既有指标快照；请用原 M2.1 命令先准备量价/资格输入")
            from ashare_daily.research.runner import run_research, replay_research
            if args.replay_report:
                if args.evidence_bundle or args.start or args.cutoff or args.model_capabilities or args.skip_model:
                    parser.error("存档重放不接受新资料、截点或模型参数")
                result = replay_research(args.replay_report, args.output_dir)
            else:
                if not args.snapshot or not args.evidence_bundle or not args.start or not args.cutoff:
                    parser.error("研究增强需要 --snapshot、--evidence-bundle、--start、--cutoff")
                result = run_research(market_snapshot=args.snapshot, evidence_bundle=args.evidence_bundle,
                    start=args.start, cutoff=args.cutoff, config_path=args.m3_config, env_file=args.env_file,
                    output_dir=args.output_dir, model_capabilities=args.model_capabilities, skip_model=args.skip_model)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "complete_within_scope" else 1
        if args.command in {"brief", "screen"} and (args.replay_report or args.evidence_bundle or args.start or args.cutoff or args.model_capabilities or args.skip_model):
            parser.error("M3 参数须显式 --research-enhanced；不悄悄改变原量价命令")
        if args.command == "collect-sample":
            from ashare_daily.sample_data import collect_sample_data, load_sample_config
            result = collect_sample_data(load_sample_config(args.sample_file), target_date=args.date, database=args.db,
                                         output_dir=args.output_dir, adjusted_dir=args.adjusted_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "ok" else 1
        if args.command == "doctor":
            if args.offline:
                if args.date:
                    parser.error("--offline 不接受 --date；日期验证需要 --source baostock")
                result = doctor_offline()
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["status"] == "ok" else 1
        if args.command in {"doctor", "collect", "check-data"}:
            from ashare_daily.market import check_market, run_market

            if args.command == "check-data":
                result = check_market(start_date=args.start_date, target_date=args.date, database=args.db, output_dir=args.output_dir)
            else:
                options = dict(target_date=args.date, output_dir=args.output_dir, timeout_seconds=args.timeout, max_attempts=args.attempts)
                if args.command == "collect":
                    options.update(start_date=args.start_date, database=args.db, refresh=args.refresh)
                result = run_market(args.command, **options)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] in {"ok", "non_trading_day"} else 1
        if args.command == "screen" or args.command == "brief" and args.mode == "research":
            if getattr(args, "empty_candidates", False):
                parser.error("--empty-candidates 只属于 DEMO；真实候选由规则决定")
            if args.snapshot and args.config != Path("config/m2.json"):
                parser.error("快照重放使用冻结配置，不接受 --config 覆盖")
            from ashare_daily.m2 import run_m2
            run_research = run_m2
            m21_options = {}
            config_data = {}
            if args.snapshot:
                snapshot_path = Path(args.snapshot)
                if re.fullmatch(r"m2-[0-9a-f]{64}", args.snapshot):
                    candidate = args.output_dir / "research" / "m21" / "snapshots" / f"{args.snapshot}.json"
                    if candidate.is_file():
                        snapshot_path = candidate
                if snapshot_path.is_file():
                    snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
                    if not isinstance(snapshot_data, dict):
                        raise ValueError("快照必须为 JSON 对象")
                    config_data = snapshot_data.get("strategy_config", {})
            elif args.config.is_file():
                config_data = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if not isinstance(config_data, dict):
                raise ValueError("策略配置必须为 JSON 对象")
            if config_data.get("workflow_version") == "m2.1":
                from ashare_daily.m21 import run_m21
                run_research = run_m21
                m21_options["eligibility_evidence"] = args.eligibility_evidence
            elif args.eligibility_evidence:
                parser.error("资格证据导入需要 M2.1 配置；旧 M2 语义保持不变")
            result = run_research(target_date=args.date, snapshot=args.snapshot, config_path=args.config, database=args.db,
                            output_dir=args.output_dir, adjusted_dir=args.adjusted_dir,
                            fetch_adjusted=args.fetch_adjusted, refresh_adjusted=args.refresh_adjusted,
                            adjusted_manifest=args.adjusted_manifest, **m21_options)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # A generated gap report is usable, but not a successful eligibility check.
            return 0 if result["status"] in {"market_only", "non_trading_day"} else 1
        if args.snapshot or args.fetch_adjusted or args.refresh_adjusted or args.adjusted_manifest or args.eligibility_evidence:
            parser.error("DEMO 不接受真实数据或快照参数")
        from ashare_daily.demo import build_demo_report
        from ashare_daily.reports.publish import publish_report

        report = build_demo_report(args.date, empty_candidates=args.empty_candidates)
        directory = publish_report(report, args.output_dir)
        print("DEMO 日报已生成：全部证券、消息及分析为人工合成演示。")
        print(f"演示场景日期：{args.date}；未判断真实交易日。")
        print(f"HTML：{directory / 'daily_brief.html'}")
        print(f"Markdown：{directory / 'daily_brief.md'}")
        print(f"JSON：{directory / 'daily_brief.json'}")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
