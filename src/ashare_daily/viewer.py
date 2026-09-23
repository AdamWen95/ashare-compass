"""Read-only archive viewer. This module never imports collectors or model clients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo
from ashare_daily.reports.reader import reader_content
from ashare_daily.artifact_purpose import is_production_artifact, is_production_path
from ashare_daily.reports.observation import (scan_observation_reports, read_observation_report,
    EXPORTS as OBSERVATION_EXPORTS)


EXPORTS = {
    "daily_brief.html": ("HTML 日报", "text/html"),
    "daily_brief.md": ("Markdown 日报", "text/markdown"),
    "daily_brief.json": ("JSON 日报", "application/json"),
    "screening_audit.csv": ("全样本筛选核验 CSV", "text/csv"),
    "claim_evidence_audit.csv": ("主张与证据核验 CSV", "text/csv"),
    "evidence_catalog.json": ("证据目录 JSON", "application/json"),
}
SECTOR_EXPORTS = {
    "sector_data_readiness.md": ("板块数据就绪 Markdown", "text/markdown"),
    "sector_data_readiness.json": ("板块数据就绪 JSON", "application/json"),
}
SECTOR_RESEARCH_EXPORTS = {
    "sector_research_report.md": ("行业研究 Markdown", "text/markdown"),
    "sector_research_report.html": ("行业研究 HTML", "text/html"),
    "sector_research_report.json": ("行业研究 JSON", "application/json"),
    "eligibility_fields.csv": ("资格字段 CSV", "text/csv"),
    "history_gap_accounting.csv": ("历史缺日说明 CSV", "text/csv"),
}
_SECTOR_RESEARCH_FILES = set(SECTOR_RESEARCH_EXPORTS) | {"report_inputs.json", "sector_selection.json"}
MAX_ARTIFACT_BYTES = 25_000_000
MAX_MANIFEST_BYTES = 100_000
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._~-]{8,}", re.I)
_SENSITIVE_KEYS = {"api_key", "model_api_key", "authorization", "password", "access_token", "secret", "secret_key"}
STATUS_LABELS = {
    "ok": "通过", "complete_within_scope": "配置范围内完成", "partial": "部分覆盖",
    "market_only": "仅行情版", "failed": "失败", "running": "运行中", "reused": "复用已完成结果",
    "non_trading_day": "休市日，未生成新研究", "dry_run": "预览，未执行采集研究",
    "missing_configuration": "缺少模型配置", "skipped": "已跳过", "not_run": "未运行",
    "pass": "通过", "fail": "不通过", "pending": "待核实", "not_computable": "无法计算",
    "candidate": "正式预候选", "excluded": "已排除", "data_insufficient": "数据不足",
    "qualified_not_selected": "达标但未入选", "unknown": "未知", "disabled": "未启用",
    "budget_exhausted": "预算已用尽", "market_data_not_ready": "目标行情尚未齐备",
    "calendar_unavailable": "交易日历不可用", "market_stale": "目标行情尚未更新",
    "calendar_unverified": "交易日历未核验，已停止",
    "universe_blocked": "配置范围的名单存在核验缺口，已停止",
    "f2_pending": "名单已核验，待F2行情验收",
    "f2_partial": "F2行情部分完成，仍有数据缺口",
    "f2_blocked": "F2行情前置条件未通过，已停止",
    "f2_complete": "F2行情核验完成，尚未筛选、研究或生成新日报",
    "interrupted": "任务中断", "not_due": "尚未到盘后运行时间",
    "ready_for_screening": "数据阶段就绪；尚未执行个股筛选",
    "adjusted_not_ready": "可信复权输入尚未齐备",
    "no_sectors_selected": "无满足观察规则的行业",
    "sector_source_blocked": "行业来源、分类或日期尚未核验",
}


class ArchiveError(ValueError):
    """A registered artifact could not be safely read."""


def status_label(value: Any) -> str:
    return STATUS_LABELS.get(str(value), str(value) if value is not None else "未记录")


def scope_label(value: Any) -> str:
    return {"sse_szse_a": "沪深A股全市场，暂不含北交所", "all_a": "全A股五板块，包含北交所",
            "sample": "历史固定样本"}.get(str(value), "历史记录未标范围" if value is None else str(value))


def _label(value: Any) -> str:
    """Escape dynamic labels rendered by Streamlit's Markdown-capable widgets."""
    return re.sub(r"([\\`*_{}\[\]()#+.!<>|])", r"\\\1", str(public_value(value)))


def public_value(value: Any) -> Any:
    """Defense in depth for run summaries; never obtain the key itself."""
    if isinstance(value, dict):
        return {str(k): ("[已隐藏]" if str(k).lower() in _SENSITIVE_KEYS else public_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        return _SECRET.sub("[已隐藏]", value)
    return value


def _has_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any((str(k).lower() in _SENSITIVE_KEYS and v not in (None, "", "[已隐藏]")) or _has_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_secret(item) for item in value)
    return bool(_SECRET.search(value)) if isinstance(value, str) else False


def _inside(path: Path, root: Path) -> Path:
    path, root = path.resolve(), root.resolve()
    if path == root or not path.is_relative_to(root):
        raise ArchiveError("路径越出已登记的报告目录，已拒绝读取")
    return path


def _io(path: Path) -> Path:
    """Read long Windows archive paths without importing operational writers."""
    value = str(path.absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def _no_links(path: Path) -> None:
    if any(_io(part).is_symlink() or (hasattr(part, "is_junction") and _io(part).is_junction())
           for part in (path, *path.parents)):
        raise ArchiveError("只读报告路径不能经过符号链接或 junction")


def _bytes(path: Path, root: Path, limit: int) -> bytes:
    target = _inside(path, root)
    if not _io(target).is_file() or _io(target).stat().st_size > limit:
        raise ArchiveError("产物缺失、不是文件或超过大小上限")
    with _io(target).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ArchiveError("产物超过大小上限")
    return data


def _json(data: bytes) -> dict:
    try:
        result = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("JSON 无法解析；未以空报告代替") from exc
    if not isinstance(result, dict):
        raise ArchiveError("JSON 顶层必须为对象")
    return result


def _production_context(directory: Path, manifest: dict, report_name: str, *, selection_directory: Path | None = None) -> None:
    """Check each frozen identity independently, also immediately before export.

    Only fixed local companion names are inspected. A path embedded in an
    untrusted report is never followed to establish production eligibility.
    """
    if not is_production_path(directory) or not is_production_artifact(manifest):
        raise ArchiveError("工程验收产物不得进入正式报告、今日列表或生产统计")
    data = _bytes(directory / report_name, directory, MAX_ARTIFACT_BYTES)
    expected = manifest.get("files", {}).get(report_name)
    if not isinstance(expected, str) or not _HASH.fullmatch(expected) or hashlib.sha256(data).hexdigest() != expected:
        raise ArchiveError("报告产物哈希不符，已拒绝展示")
    if not is_production_artifact(_json(data)):
        raise ArchiveError("工程验收产物不得进入正式报告、今日列表或生产统计")
    for parent in {directory, selection_directory} - {None}:
        for name in ("sector_selection.json", "purpose.json"):
            path = parent / name
            if path.exists() and not is_production_artifact(_json(_bytes(path, parent, MAX_ARTIFACT_BYTES))):
                raise ArchiveError("工程验收选择不得进入正式报告、今日列表或生产统计")
    # Older M3 manifests register the frozen input separately from the public
    # report. Its validation marker must survive a public-file rename/copy.
    if ("input_snapshot.json" in manifest.get("files", {})
            and (directory / "input_snapshot.json").exists()):
        data = _bytes(directory / "input_snapshot.json", directory, MAX_ARTIFACT_BYTES)
        if hashlib.sha256(data).hexdigest() != manifest["files"]["input_snapshot.json"]:
            raise ArchiveError("报告冻结输入哈希不符")
        try:
            snapshot = json.loads(data.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            # Preserve the old byte-hashed opaque-snapshot contract. Public
            # report/manifest/selection markers above remain mandatory checks.
            return
        if not is_production_artifact(snapshot):
            raise ArchiveError("工程验收输入不得进入正式报告、今日列表或生产统计")


@dataclass(frozen=True)
class ReportArchive:
    directory: Path
    report: dict
    manifest: dict

    @property
    def version(self) -> str:
        return self.directory.name

    @property
    def date(self) -> str:
        return self.report["trade_date"]

    @property
    def generated_at(self) -> str:
        return str(self.report.get("actual_generated_at", ""))

    @property
    def label(self) -> str:
        origin = "历史补采" if self.report.get("historical_reconstruction") else "见任务触发记录"
        return f"{self.generated_at} ｜ {status_label(self.report.get('status'))} ｜ {origin} ｜ {self.version}"


def artifact_bytes(archive: ReportArchive, name: str) -> bytes:
    """Only exact export names registered in this report's manifest are readable."""
    if name not in EXPORTS:
        raise ArchiveError("该文件不在网页导出白名单中")
    # Re-read the manifest each time, including immediately before download.
    manifest = _json(_bytes(archive.directory / "manifest.json", archive.directory, MAX_MANIFEST_BYTES))
    _production_context(archive.directory, manifest, "daily_brief.json")
    return _artifact_bytes_checked(archive.directory, manifest, name)


def _artifact_bytes_checked(directory: Path, manifest: dict, name: str) -> bytes:
    """Read one export within a single, freshly validated report context.

    No validation state survives a read_report call. Public downloads always
    enter artifact_bytes above and validate the current frozen input again.
    """
    if name not in EXPORTS:
        raise ArchiveError("该文件不在网页导出白名单中")
    expected = manifest.get("files", {}).get(name)
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise ArchiveError("产物未登记有效哈希")
    data = _bytes(directory / name, directory, MAX_ARTIFACT_BYTES)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ArchiveError("产物哈希不符；保留旧报告但拒绝损坏文件")
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ArchiveError("产物不是已支持的 UTF-8 文本") from exc
    if _SECRET.search(decoded) or (name.endswith(".json") and _has_secret(_json(data))):
        raise ArchiveError("产物出现疑似凭据，已禁止展示与导出")
    return data


def read_report(directory: Path, allowed_root: Path) -> ReportArchive:
    directory = _inside(directory, allowed_root)
    manifest = _json(_bytes(directory / "manifest.json", directory, MAX_MANIFEST_BYTES))
    if not isinstance(manifest.get("files"), dict):
        raise ArchiveError("报告尚未完整发布或缺少产物登记")
    _production_context(directory, manifest, "daily_brief.json")
    report = _json(_artifact_bytes_checked(directory, manifest, "daily_brief.json"))
    if report.get("schema_version") not in {"m3-report-v1", "m4-report-v1"}:
        raise ArchiveError("报告结构版本暂不支持")
    if not _DATE.fullmatch(str(report.get("trade_date", ""))) or directory.parent.name != report["trade_date"]:
        raise ArchiveError("报告分析日期与存档目录不一致")
    for name in ("market", "analysis", "model_run", "statuses"):
        if not isinstance(report.get(name), dict):
            raise ArchiveError(f"报告缺少必要结构：{name}")
    if not isinstance(report["market"].get("counts"), dict):
        raise ArchiveError("报告缺少股票覆盖计数")
    for name in ("source_health", "source_registry", "evidence_catalog", "research_objects", "gaps"):
        if not isinstance(report.get(name), list) or any(not isinstance(x, dict) for x in report[name] if name != "gaps"):
            raise ArchiveError(f"报告资料结构损坏：{name}")
    for name in ("evaluations", "candidates", "pending_eligibility"):
        rows = report["market"].get(name)
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
            raise ArchiveError(f"报告股票结构损坏：{name}")
        for row in rows:
            for key in ("exclusion_reasons", "data_issues", "conditions"):
                if not isinstance(row.get(key, []), list):
                    raise ArchiveError(f"报告股票核验结构损坏：{key}")
    claims = report["analysis"].get("accepted_claims")
    if not isinstance(claims, list) or any(not isinstance(c, dict) or not isinstance(c.get("citations"), list)
                                         or any(not isinstance(x, dict) for x in c["citations"]) for c in claims):
        raise ArchiveError("报告主张结构损坏")
    if not isinstance(report["model_run"].get("configuration", {}), dict) or not isinstance(report["market"].get("benchmark", {}), dict):
        raise ArchiveError("报告模型配置或基准结构损坏")
    if not isinstance(report.get("metric_registry", {}), dict):
        raise ArchiveError("报告指标引用结构损坏")
    for item in report["source_health"] + report["source_registry"]:
        if not isinstance(item.get("source_id", ""), str):
            raise ArchiveError("报告来源编号结构损坏")
    archive = ReportArchive(directory, report, manifest)
    # A version with one damaged registered public artifact is not 'readable'.
    for name in EXPORTS:
        if name != "daily_brief.json" and name in manifest["files"]:
            _artifact_bytes_checked(directory, manifest, name)
    return archive


@dataclass(frozen=True)
class SectorReportArchive:
    directory: Path
    report: dict
    manifest: dict

    @property
    def selection_id(self) -> str:
        return self.directory.parent.parent.name

    @property
    def version(self) -> str:
        return self.directory.name

    @property
    def date(self) -> str:
        return self.report.get("target_date") or "待核验"

    @property
    def generated_at(self) -> str:
        return self.report.get("generated_at") or "待核验"

    @property
    def label(self) -> str:
        return f"{self.date} ｜ {self.generated_at} ｜ {self.selection_id} ｜ {self.version}"


def _sector_manifest(directory: Path) -> dict:
    manifest = _json(_bytes(directory / "manifest.json", directory, MAX_MANIFEST_BYTES))
    if (manifest.get("schema_version") != "f2s1-readiness-manifest-v1"
            or directory.parent.name != "reports"
            or manifest.get("selection_id") != directory.parent.parent.name
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != set(SECTOR_EXPORTS)):
        raise ArchiveError("板块就绪报告的登记版本或冻结选择编号不一致")
    return manifest


def sector_artifact_bytes(archive: SectorReportArchive, name: str) -> bytes:
    """Recheck exact registered public artifacts before every display/download."""
    if name not in SECTOR_EXPORTS:
        raise ArchiveError("该文件不在板块就绪报告导出白名单中")
    manifest = _sector_manifest(archive.directory)
    _production_context(archive.directory, manifest, "sector_data_readiness.json",
                        selection_directory=archive.directory.parent.parent)
    expected = manifest["files"][name]
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise ArchiveError("板块就绪产物未登记有效哈希")
    data = _bytes(archive.directory / name, archive.directory, MAX_ARTIFACT_BYTES)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ArchiveError("板块就绪产物哈希不符，已拒绝展示")
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ArchiveError("板块就绪产物不是 UTF-8 文本") from exc
    if _SECRET.search(decoded) or (name.endswith(".json") and _has_secret(_json(data))):
        raise ArchiveError("产物出现疑似凭据，已禁止展示与导出")
    return data


def read_sector_report(directory: Path, allowed_root: Path) -> SectorReportArchive:
    directory = _inside(directory, allowed_root)
    manifest = _sector_manifest(directory)
    archive = SectorReportArchive(directory, {}, manifest)
    report = _json(sector_artifact_bytes(archive, "sector_data_readiness.json"))
    # Missing display fields stay pending. Explicit identities cannot contradict
    # the directory/manifest, or relabel old all-market reports as sector research.
    for key, expected in (("selection_id", archive.selection_id), ("market_scope", "sse_szse_a"),
                          ("research_mode", "sector_first")):
        if key in report and report[key] != expected:
            raise ArchiveError("板块就绪报告的范围或选择编号不一致")
    if report.get("target_date") is not None:
        try:
            if not isinstance(report["target_date"], str) or date.fromisoformat(report["target_date"]).isoformat() != report["target_date"]:
                raise ValueError("noncanonical date")
        except ValueError as exc:
            raise ArchiveError("板块就绪报告日期无效") from exc
    if report.get("generated_at") is not None:
        try:
            stamp = datetime.fromisoformat(report["generated_at"])
            if stamp.utcoffset() is None:
                raise ValueError("timezone is required")
        except (ValueError, TypeError) as exc:
            raise ArchiveError("板块就绪报告观察时间无效") from exc
    sector_artifact_bytes(archive, "sector_data_readiness.md")
    return SectorReportArchive(directory, report, manifest)


def scan_sector_reports(output_root: Path) -> tuple[list[SectorReportArchive], list[dict]]:
    output_root = Path(output_root).resolve()
    root = output_root / "research/sse_szse_a/sector_first"
    reports, problems = [], []
    if not root.exists():
        return reports, problems
    try:
        _inside(root, output_root)
        for selection in sorted(root.iterdir()):
            if not selection.is_dir() or selection.name.startswith("."):
                continue
            _inside(selection, root)
            report_root = selection / "reports"
            if not report_root.is_dir():
                continue
            _inside(report_root, selection)
            for directory in sorted(report_root.iterdir()):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    reports.append(read_sector_report(directory, root))
                except (ArchiveError, OSError, ValueError, TypeError) as exc:
                    problems.append({"选择编号": selection.name, "版本": directory.name, "问题": public_value(str(exc))})
    except (ArchiveError, OSError, ValueError) as exc:
        problems.append({"版本": "板块就绪目录读取失败", "问题": public_value(str(exc))})
    reports.sort(key=lambda item: (item.report.get("target_date") or "", item.report.get("generated_at") or "", item.version), reverse=True)
    return reports, problems


@dataclass(frozen=True)
class SectorResearchArchive:
    directory: Path
    output_root: Path
    report: dict
    manifest: dict
    purpose: str

    @property
    def version(self) -> str:
        return self.directory.name

    @property
    def date(self) -> str:
        return self.report["trade_date"]

    @property
    def generated_at(self) -> str:
        return self.report["actual_generated_at"]

    @property
    def label(self) -> str:
        return f"{self.date} ｜ {self.generated_at} ｜ {self.version}"


def _research_json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ArchiveError("行业研究 JSON 出现重复字段")
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ArchiveError("行业研究 JSON 数值无效")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("行业研究 JSON 无法读取") from exc
    if not isinstance(value, dict) or _has_secret(value):
        raise ArchiveError("行业研究产物结构无效或含疑似凭据")
    return value


def _research_location(directory: Path, output_root: Path, purpose: str) -> tuple[Path, str]:
    if purpose not in {"production", "engineering_validation"}:
        raise ArchiveError("行业报告用途必须显式确认")
    output_root = Path(output_root).resolve()
    allowed = output_root / ("research/sse_szse_a/sector_reports" if purpose == "production" else "engineering_validation/f3s")
    try:
        _no_links(Path(directory).absolute())
        _no_links(allowed)
        directory = _inside(directory, allowed)
        _inside(allowed, output_root)
    except ValueError as exc:
        raise ArchiveError("行业报告不在对应用途的独立目录中") from exc
    parts = directory.relative_to(allowed.resolve()).parts
    if (purpose == "production" and len(parts) != 2 or purpose == "engineering_validation"
            and (len(parts) != 4 or parts[1:3] != ("f4s1", "reports"))):
        raise ArchiveError("行业报告目录层级无效")
    if not re.fullmatch(r"sector-report-[0-9a-f]{24}", directory.name):
        raise ArchiveError("行业报告版本编号无效")
    prefix = "sector-" if purpose == "production" else "validation-sector-"
    if not re.fullmatch(prefix+r"\d{4}-\d{2}-\d{2}-[0-9a-f]{20}", parts[0]):
        raise ArchiveError("行业报告选择编号或用途不符")
    return directory, parts[0]


def _research_bundle(directory: Path, output_root: Path, purpose: str) -> tuple[dict, dict, dict[str, bytes]]:
    # Only seven exact companion names are ever opened; archived URLs and
    # arbitrary source locators are not browser capabilities.
    directory, selection_id = _research_location(directory, output_root, purpose)
    _no_links(directory / "manifest.json")
    manifest = _research_json(_bytes(directory / "manifest.json", directory, MAX_MANIFEST_BYTES))
    if (manifest.get("schema_version") != "f4s1-sector-report-manifest-v1"
            or manifest.get("purpose") != purpose or manifest.get("production_eligible") is not (purpose == "production")
            or manifest.get("selection_id") != selection_id or manifest.get("report_id") != directory.name
            or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != _SECTOR_RESEARCH_FILES):
        raise ArchiveError("行业报告清单用途、身份或文件集合不完整")
    files, decoded = {}, {}
    for name, expected in manifest["files"].items():
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ArchiveError("行业报告产物未登记有效哈希")
        _no_links(directory / name)
        data = _bytes(directory / name, directory, MAX_ARTIFACT_BYTES)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ArchiveError("行业报告产物哈希不符")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ArchiveError("行业报告产物不是 UTF-8 文本") from exc
        if _SECRET.search(text):
            raise ArchiveError("行业报告产物含疑似凭据，已拒绝展示")
        files[name] = data
        if name.endswith(".json"):
            decoded[name] = _research_json(data)
    report, selection, inputs = (decoded[name] for name in
        ("sector_research_report.json", "sector_selection.json", "report_inputs.json"))
    if (report.get("schema_version") != "f4s1-sector-report-v1" or report.get("purpose") != purpose
            or report.get("production_eligible") is not (purpose == "production")
            or report.get("report_kind") != ("production_daily" if purpose == "production" else "engineering_diagnostic")
            or report.get("selection_id") != selection_id or report.get("report_id") != directory.name
            or selection.get("selection_id") != selection_id):
        raise ArchiveError("行业报告正文、选择与用途不一致")
    if (manifest.get("selection_content_hash") != selection.get("content_hash")
            or manifest.get("market_scope") != "sse_szse_a" or manifest.get("research_mode") != "sector_first"
            or inputs.get("schema_version") != "f4s1-sector-report-input-v1"
            or inputs.get("selection_content_hash") != selection.get("content_hash")
            or inputs.get("purpose") != purpose or inputs.get("production_eligible") is not (purpose == "production")
            or inputs.get("actual_generated_at") != report.get("actual_generated_at")
            or not isinstance(inputs.get("fingerprint"), str) or not _HASH.fullmatch(inputs["fingerprint"])
            or manifest.get("fingerprint") != inputs["fingerprint"]
            or not isinstance(inputs.get("source_refs"), list)):
        raise ArchiveError("行业报告冻结输入、指纹或范围不一致")
    # The publisher authenticates original sources. The browser never follows
    # these archived locators, opens a market database, or fetches source URLs.
    for reference in inputs["source_refs"]:
        if (not isinstance(reference, dict) or not isinstance(reference.get("path"), str)
                or not isinstance(reference.get("sha256"), str) or not _HASH.fullmatch(reference["sha256"])):
            raise ArchiveError("行业报告来源引用结构无效")
        parts = reference["path"].replace("\\", "/").split("/")
        if (not reference["path"] or "\0" in reference["path"] or "://" in reference["path"]
                or any(part == ".." or part.lower().startswith(".env") or part.lower() in {".git", "secrets", "credentials"} for part in parts)):
            raise ArchiveError("行业报告来源引用路径无效，网页不会读取该定位")
        if purpose == "production" and any(part.casefold() == "engineering_validation" or part.casefold().startswith("validation-sector-") for part in parts):
            raise ArchiveError("生产行业报告不得引用工程目录")
    if purpose == "production":
        if not all(is_production_artifact(value) for value in (manifest, report, selection, inputs)):
            raise ArchiveError("工程验收内容不得进入生产日报或候选统计")
    elif (selection.get("purpose") != "engineering_validation" or selection.get("production_eligible") is not False
            or inputs.get("purpose", purpose) != purpose or inputs.get("production_eligible", False) is not False):
        raise ArchiveError("工程验收必须保留独立用途标记")
    sections = report.get("sections")
    if not isinstance(sections, list):
        raise ArchiveError("行业报告缺少结构化展示区块")
    for section in sections:
        if (not isinstance(section, list) or len(section) != 2 or not isinstance(section[0], str)
                or not isinstance(section[1], list)):
            raise ArchiveError("行业报告展示区块结构无效")
        for block in section[1]:
            if not isinstance(block, list) or len(block) != 2:
                raise ArchiveError("行业报告展示内容结构无效")
            kind, value = block
            if kind in {"paragraph", "subheading"} and isinstance(value, str):
                continue
            if (kind != "table" or not isinstance(value, list) or len(value) != 2
                    or not isinstance(value[0], list) or not value[0] or not all(isinstance(header, str) for header in value[0])
                    or len(set(value[0])) != len(value[0]) or not isinstance(value[1], list)
                    or any(not isinstance(row, list) or len(row) != len(value[0]) for row in value[1])):
                raise ArchiveError("行业报告表格或内容类型无效")
    from ashare_daily.sector_selection import verify_selection
    from ashare_daily.reports.sector_contracts import validate_report, validate_report_inputs
    try:
        verify_selection(selection)
        validate_report(report, selection)
        validate_report_inputs(report, selection, inputs)
    except (ValueError, TypeError, KeyError) as exc:
        raise ArchiveError("行业报告冻结内容核验失败：" + str(exc)) from exc
    return report, manifest, files


def read_sector_research_report(directory: Path, output_root: Path, *, purpose: str = "production") -> SectorResearchArchive:
    report, manifest, _ = _research_bundle(directory, output_root, purpose)
    return SectorResearchArchive(Path(directory).resolve(), Path(output_root).resolve(), report, manifest, purpose)


def sector_research_artifact_bytes(archive: SectorResearchArchive, name: str) -> bytes:
    if name not in SECTOR_RESEARCH_EXPORTS:
        raise ArchiveError("该文件不在行业报告公开导出白名单中")
    # Purpose, selection and all hashes are checked again immediately before
    # every export; an earlier browser selection grants no cached trust.
    _, _, files = _research_bundle(archive.directory, archive.output_root, archive.purpose)
    return files[name]


def scan_sector_research_reports(output_root: Path, *, purpose: str = "production") -> tuple[list[SectorResearchArchive], list[dict]]:
    if purpose not in {"production", "engineering_validation"}:
        raise ArchiveError("未知行业报告用途")
    output_root = Path(output_root).resolve()
    root = output_root / ("research/sse_szse_a/sector_reports" if purpose == "production" else "engineering_validation/f3s")
    reports, problems = [], []
    if not _io(root).exists():
        return reports, problems
    try:
        _no_links(root)
        _inside(root, output_root)
        for selection in sorted(_io(root).iterdir()):
            # Remove only the Windows IO prefix introduced by _io; identities
            # and persisted paths remain ordinary native paths.
            selection = root / selection.name
            if not _io(selection).is_dir() or selection.name.startswith("."):
                continue
            _no_links(selection)
            _inside(selection, root)
            parent = selection if purpose == "production" else selection / "f4s1/reports"
            if not _io(parent).is_dir():
                continue
            _no_links(parent)
            for item in sorted(_io(parent).iterdir()):
                directory = parent / item.name
                if not _io(directory).is_dir() or directory.name.startswith("."):
                    continue
                try:
                    reports.append(read_sector_research_report(directory, output_root, purpose=purpose))
                except (ArchiveError, OSError, ValueError, TypeError, KeyError) as exc:
                    problems.append({"选择编号": selection.name, "版本": directory.name, "问题": str(public_value(str(exc)))})
    except (ArchiveError, OSError, ValueError) as exc:
        problems.append({"版本": "行业报告目录", "问题": str(public_value(str(exc)))})
    reports.sort(key=lambda archive: (archive.date, archive.generated_at, archive.version), reverse=True)
    return reports, problems


def scan_reports(output_root: Path) -> tuple[list[ReportArchive], list[dict]]:
    output_root = Path(output_root).resolve()
    reports, problems = [], []
    roots = [output_root / "research/m3", output_root / "research/m4/reports"]
    for root in roots:
        if not root.exists():
            continue
        try:
            _inside(root, output_root)
            for day in sorted(root.iterdir(), reverse=True):
                if not day.is_dir() or not _DATE.fullmatch(day.name):
                    continue
                _inside(day, root)
                for directory in sorted(day.iterdir(), reverse=True):
                    if not directory.is_dir() or directory.name.startswith("."):
                        continue
                    try:
                        reports.append(read_report(directory, root))
                    except (ArchiveError, OSError, ValueError) as exc:
                        problems.append({"版本": directory.name, "日期": day.name, "问题": str(public_value(str(exc)))})
        except (ArchiveError, OSError, ValueError) as exc:
            problems.append({"版本": "目录读取失败", "日期": "未记录", "问题": str(public_value(str(exc)))})
    reports.sort(key=lambda x: (x.date, x.generated_at, x.version), reverse=True)
    return reports, problems


def scan_runs(output_root: Path) -> tuple[list[dict], list[dict]]:
    root = Path(output_root).resolve() / "research/m4/runs"
    runs, problems = [], []
    if not root.exists():
        return runs, problems
    try:
        _inside(root, Path(output_root))
        for directory in root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                _inside(directory, root)
                result = _json(_bytes(directory / "result.json", directory, 2_000_000))
                if not is_production_artifact(result) or not is_production_path(directory):
                    continue
                # No generic local log/file path is followed from a run record.
                result = public_value(result)
                result["archive_run_id"] = directory.name
                runs.append(result)
            except (ArchiveError, OSError, ValueError) as exc:
                problems.append({"运行": directory.name, "问题": public_value(str(exc))})
    except (ArchiveError, OSError, ValueError) as exc:
        problems.append({"运行": "目录读取失败", "问题": public_value(str(exc))})
    runs.sort(key=lambda item: (str(item.get("started_at", "")), item["archive_run_id"]), reverse=True)
    return runs, problems


def unresolved_daily_failure(runs: list[dict], *, day: str | None = None, scope: str | None = None) -> dict | None:
    """A preview/deferred/reused task never clears a still-unresolved failure."""
    day = day or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    excluded = {"dry_run", "non_trading_day", "not_due", "reused"}
    failures = {"failed", "calendar_unavailable", "calendar_unverified", "universe_blocked", "f2_pending", "f2_partial", "f2_blocked", "market_stale", "market_data_not_ready", "interrupted"}
    ordered = sorted(runs, key=lambda row: (str(row.get("started_at", "")), str(row.get("archive_run_id", row.get("run_id", "")))), reverse=True)
    for row in ordered:
        if not is_production_artifact(row):
            continue
        if scope is not None and row.get("scope", "sample") != scope:
            continue
        if row.get("target_trade_date") != day or row.get("status") in excluded:
            continue
        # F2 completion resolves an F2 collection failure in the explicitly
        # selected scope. Its label still states that no new report exists.
        if row.get("status") == "f2_complete" and row.get("exit_code") == 0:
            return None
        # Generation success is independent of source/research completeness.
        # Only a later actual generation for this target date resolves the alert.
        if row.get("generation_status") == "ok":
            return None
        if row.get("status") in failures or row.get("exit_code") not in (None, 0):
            return row
    return None


def _decimal(value: Any, *, percent: bool = False) -> str:
    if value is None:
        return "未取得"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "未取得"
        return f"{number * 100:.2f}%" if percent else f"{number:,.4f}".rstrip("0").rstrip(".")
    except (ValueError, InvalidOperation):
        return "无法展示"


def stock_rows(items: list[dict]) -> list[dict]:
    return [{"代码": item.get("symbol"), "名称": item.get("name"), "行情日期": item.get("actual_data_date"),
             "量价": status_label(item.get("technical_screen_status")), "资格": status_label(item.get("eligibility_status")),
             "主分类": status_label(item.get("status")), "收盘价（未复权，元）": _decimal(item.get("display_close")),
             "20 日相对基准收益": _decimal(item.get("relative_return"), percent=True),
             "20 日平均成交额（元）": _decimal(item.get("avg_amount_cny")),
             "缺口或排除原因": "；".join(map(str, item.get("exclusion_reasons", []) + item.get("data_issues", [])))} for item in items]


def _table(st, rows: list[dict], *, empty: str = "暂无记录") -> None:
    if rows:
        st.dataframe(public_value(rows), hide_index=True, width="stretch")
    else:
        st.info(empty)


def _text(st, value: Any) -> None:
    # Keep structured diagnostics in one component per field. Recursively
    # emitting every leaf can create tens of thousands of WebSocket deltas,
    # even inside a collapsed expander. JSON/text never execute archived HTML.
    if isinstance(value, dict):
        for key, item in public_value(value).items():
            if isinstance(item, (dict, list)):
                st.caption(_label(key))
                st.json(item, expanded=False)
            else:
                st.text(f'{key}：{item if item is not None else "未记录"}')
    elif isinstance(value, list):
        if not value:
            st.text('暂无记录')
        else:
            st.json(public_value(value), expanded=False)
    else:
        st.text(str(public_value(value)))


def _prose(st, text) -> None:
    st.markdown(_label(text))


def _stock_card(st, card, *, compact=False):
    with st.container(border=True):
        st.subheader(_label(card['name'] + ' · ' + card['symbol']))
        st.caption(_label(card['label']))
        _prose(st, '为什么进入观察：' + '；'.join(card['reasons']))
        columns = st.columns(3)
        for column, metric in zip(columns, card['metrics']):
            column.metric(metric['label'], metric['value'])
        if compact:
            if card['claims']:
                _prose(st, card['claims'][0]['text'])
            else:
                st.caption('当前为程序量价观察，个股消息证据尚未形成有效模型结论。')
            _prose(st, '主要待查：' + (card['risks'][0] if card['risks'] else '核对最新资料'))
            return
        st.markdown('**业务关联依据**')
        if not card['business_evidence']:
            st.info('尚未取得充分的公司业务原文，不据此判断受益方向。')
        for evidence in card['business_evidence']:
            st.caption(_label(('历史业务背景' if evidence['is_background'] else '本期业务依据') + ' · ' + str(evidence['published_at'])))
            _prose(st, evidence['quote'])
            st.caption(_label(str(evidence['title'])))
            with st.expander('核对业务出处 ' + evidence['evidence_id'][-8:]):
                _text(st, evidence['original_url'])
                _text(st, evidence['evidence_id'])
        st.markdown('**研究结论与反面证据**')
        if not card['claims']:
            st.info('暂无通过引用核验的个股分析，保留量价依据和资料缺口。')
        for claim in card['claims']:
            labels = {'fact': '事实', 'inference': '研究推论', 'opinion': '来源观点',
                      'counterevidence': '反面证据', 'unknown': '未知事项', 'followup': '后续核验'}
            st.caption(labels.get(claim['claim_type'], '研究'))
            _prose(st, claim['text'])
            for citation in claim.get('citations', []):
                with st.expander('引用核对 ' + str(citation.get('evidence_id', ''))[-8:]):
                    _text(st, citation.get('quote', ''))
                    _text(st, citation.get('locator', ''))
        st.markdown('**风险与资料缺口**')
        for risk in card['risks']:
            _prose(st, '• ' + risk)
        st.markdown('**继续观察的条件**')
        for condition in card['followup']:
            _prose(st, '• ' + condition)


def _claims(st, report: dict, *, symbol: str | None = None) -> None:
    labels = {"fact": "事实", "inference": "研究推论", "opinion": "来源观点", "counterevidence": "反面证据", "unknown": "未知事项", "followup": "后续核验"}
    claims = [c for c in report["analysis"]["accepted_claims"] if symbol is None or c.get("symbol") == symbol]
    if not claims:
        st.info("暂无已通过引用核验的研究主张；不代表没有新闻或风险。")
    for claim in claims:
        st.caption(_label(f"{claim.get('claim_id')} · {labels.get(claim.get('claim_type'), '研究主张')}"))
        related = {c.get('evidence_id') for c in claim.get('citations', [])}
        for evidence in report.get('evidence_catalog', []):
            if evidence.get('evidence_id') in related:
                st.caption(_label(('历史背景 · ' if evidence.get('is_background') else '本期消息 · ') +
                                  str(evidence.get('published_at', '日期未知')) + ' · ' + str(evidence.get('title', '原始资料'))))
        _prose(st, claim.get("text", "未记录"))
        for citation in claim.get("citations", []):
            if isinstance(citation, dict):
                with st.expander(_label("引用 " + str(citation.get("evidence_id", "未记录")))):
                    _prose(st, citation.get("quote", "未记录摘录"))
                    _text(st, "原始定位：" + str(citation.get("locator", "未记录")))
        for metric_id in claim.get("metric_ids", []):
            metric = report.get("metric_registry", {}).get(metric_id, {})
            _text(st, {"程序指标": metric_id, "值": metric.get("value"), "单位": metric.get("unit"), "日期": metric.get("trade_date")})
        if claim.get("risks") or claim.get("unknowns"):
            _text(st, {"待核查风险": claim.get("risks", []), "未知事项": claim.get("unknowns", [])})
    st.caption("引用结构通过不等于语义一定正确；请核对原文、推论和反面证据。")


def _overview(st, archive: ReportArchive) -> None:
    report, market = archive.report, archive.report["market"]
    reading = reader_content(report)
    st.subheader('今日研究摘要')
    _prose(st, reading['summary'])
    _prose(st, '量价观察、资格核验和消息研究分别展示。先看入选依据，再核对业务证据与风险。')
    st.subheader('重点方向与研究逻辑')
    if reading['directions']:
        for claim in reading['directions']:
            _prose(st, claim['text'])
            st.caption(_label('研究对象 ' + str(claim['symbol']) + ' · 推论仍需核查原文'))
    else:
        st.info('本期尚无通过证据核验的个股方向推论；可以先阅读量价观察及具体待查事项。')
    st.subheader('优先观察')
    st.caption('展示范围内最多三只；完整观察池、业务依据和逐项风险见「候选与核验」。')
    for card in reading['cards'][:3]:
        _stock_card(st, card, compact=True)
    if not reading['cards']:
        st.info('本期没有可列入观察池的对象，详见筛选条件和数据缺口。')
    st.subheader('报告范围与数据状态')
    _prose(st, report.get("notice", "范围说明未记录"))
    if report.get("status") != "complete_within_scope":
        st.warning("本报告为" + status_label(report.get("status")) + "。请同时查看模块状态和数据缺口。")
    _table(st, [{"项目": label, "记录": str(report.get(key, "未记录"))} for label, key in [
        ("行情分析日期", "trade_date"), ("实际行情日期", "actual_market_date"), ("资讯区间起点（不含）", "query_start_at"),
        ("资讯截点（包含）", "cutoff_at"), ("实际生成时间", "actual_generated_at"), ("输入快照", "input_snapshot_id")]])
    if report.get("historical_reconstruction"):
        st.info("历史资料补采研究：部分资料在截点之后才首次取得，不代表当时已掌握。")
    _table(st, [{"模块": label, "状态": status_label(report["statuses"].get(key))} for label, key in [
        ("行情与资格", "market"), ("消息", "messages"), ("模型", "model"), ("文件生成", "generation"),
        ("引用结构核验", "citation_validation"), ("语义复核", "semantic_review")]])
    counts = market["counts"]
    cols = st.columns(4)
    for col, label, key in zip(cols, ["配置股票", "行情完整", "正式预候选", "量价达标、资格待查"],
                               ["stock_count", "market_data_success_count", "candidate_count", "pending_eligibility_count"]):
        col.metric(label, counts.get(key, "未记录"))
    st.subheader("市场复盘：已有基准与股票样本")
    benchmark = market.get("benchmark", {})
    _table(st, [{"基准": str(benchmark.get("symbol", "")) + " " + str(benchmark.get("name", "未记录")),
                 "行情日期": benchmark.get("actual_data_date"), "原生点位": _decimal(benchmark.get("display_close")),
                 "当日涨跌幅": _decimal(benchmark.get("daily_return"), percent=True), "20 日收益": _decimal(benchmark.get("period_return"), percent=True)}])
    st.caption("统计仅限当前冻结样本。全市场涨跌家数、全市场成交额、行业轮动及资金流入未覆盖。")
    st.subheader("重要消息、研究依据与风险")
    _claims(st, report)
    st.subheader("关注方向与选股研究思路")
    st.caption('方向结论见本页开篇；每只股票的量价依据、业务原文和风险分列在观察卡片。')
    _text(st, "沿用已验证量价和资格规则；事件观察与正式量价候选分开，资格未知继续待核查。")
    with st.expander("本期策略、参数与缺口"):
        _text(st, {"策略版本": market.get("strategy_version"), "提示词版本": report.get("prompt_version"), "参数": market.get("strategy_config")})
        for gap in report["gaps"]:
            _text(st, gap)


def _candidates(st, archive: ReportArchive) -> None:
    report, market = archive.report, archive.report["market"]
    st.subheader('个股研究观察池')
    st.caption('最多十只；待核查观察对象不计入正式候选，数量和行情指标由程序生成。')
    for card in reader_content(report)['cards']:
        _stock_card(st, card)
    st.subheader("正式量价预候选")
    _table(st, stock_rows(market["candidates"]), empty="本期正式量价预候选为 0；没有放宽条件，也不以待核查股票填充。")
    st.subheader("量价达标、资格待核查")
    st.warning("以下股票不计入正式候选，不构成已经核验的选股结论。")
    _table(st, stock_rows(market["pending_eligibility"]), empty="本期没有量价达标、资格待核查条目。")
    st.subheader("事件观察")
    observations = [item for item in report["research_objects"] if item.get("path") == "event_observation"]
    _table(st, [{"代码": item.get("symbol"), "名称": item.get("name"), "研究路径": item.get("path"),
                 "量价": status_label(item.get("technical_screen_status")), "资格": status_label(item.get("eligibility_status")),
                 "关联证据": str(item.get("association_evidence_ids", []))} for item in observations],
           empty="本次没有资料关联充分的事件观察对象；不自动扩展股票范围。")
    st.subheader("全部股票核验")
    query = st.text_input("搜索代码或名称", key="stock_search").strip().lower()
    evaluations = market["evaluations"]
    rows = [item for item in evaluations if not query or query in str(item.get("symbol", "")).lower() or query in str(item.get("name", "")).lower()]
    _table(st, stock_rows(rows))
    if rows:
        options = {str(item.get("symbol")): item for item in rows}
        symbol = st.selectbox("逐项查看量价、资格及证据", list(options), key="stock_detail")
        item = options[symbol]
        _table(st, [{"检查": c.get("label"), "结论": status_label(c.get("status")), "原因": c.get("reason")} for c in item.get("conditions", []) if isinstance(c, dict)])
        _text(st, {"趋势价格口径": item.get("trend_adjustment_mode"), "有效历史数量": item.get("valid_history_count"),
                   "MA20": item.get("ma_short"), "MA60": item.get("ma_long"), "20 日收益": item.get("period_return"),
                   "基准收益": item.get("benchmark_period_return"), "退市整理期状态": item.get("delisting_period_status")})
        _claims(st, report, symbol=symbol)


def _evidence(st, report: dict) -> None:
    st.subheader("本次归档证据")
    st.caption("只显示报告已发布的正文或摘要；文件定位是溯源文本，网页不会据此读取任意本地文件。")
    if not report["evidence_catalog"]:
        st.info("本期没有可显示的归档资料；不能推断没有其他消息或风险。")
    for item in report["evidence_catalog"]:
        with st.expander(_label(item.get("title", "未记录标题"))):
            _text(st, {"证据 ID": item.get("evidence_id"), "来源": item.get("source_id"), "类别": item.get("category"),
                       "内容层级": item.get("content_type"), "截断摘要": item.get("content_truncated"),
                       "发布时间": item.get("published_at"), "时间精度": item.get("publication_precision"),
                       "首次取得": item.get("first_seen_at"), "取得方式": item.get("acquisition_mode"),
                       "用途": "背景资料" if item.get("is_background") else "区间内资料",
                       "原始来源": item.get("original_url"), "归档定位": item.get("raw_locator"), "内容版本": item.get("content_version")})
            _text(st, item.get("content") or "本条仅有目录或无公开摘录许可，正文未显示。")


def _run_details(st, runs: list[dict], report: dict | None) -> None:
    st.subheader("日任务运行记录")
    _table(st, [{"运行编号": item.get("run_id", item["archive_run_id"]), "状态": status_label(item.get("status")),
                 "研究范围": scope_label(item.get("scope")),
                 "计划触发": item.get("planned_trigger_at"), "实际开始": item.get("started_at"), "实际结束": item.get("ended_at"),
                 "耗时（秒）": item.get("duration_seconds"), "行情交易日": item.get("target_trade_date"),
                 "资讯截点": item.get("cutoff_at"), "退出码": item.get("exit_code")} for item in runs[:100]],
           empty="尚无 M4 日任务运行记录；已有 M3 报告不代表日任务已运行。")
    if runs:
        selected = st.selectbox("查看单次任务详情", range(min(len(runs), 100)),
            format_func=lambda index: _label(str(runs[index].get("run_id", runs[index]["archive_run_id"]))),
            key="run_detail_version")
        item = runs[selected]
        keys = ("status", "module_statuses", "sources", "source_health", "model_summary", "failure_reason",
                "error", "errors", "reason", "report_directory", "scope", "scope_label", "workflow_version",
                "universe_config_version", "config_versions", "implementation_stage", "board_counts",
                "collection_ready", "research_ready", "universe_result", "f2_result", "market_job_id", "market_quality_path")
        st.json(public_value({key: item[key] for key in keys if key in item}), expanded=False)
    if report is None:
        return
    st.subheader("所选报告的模型与来源状态")
    model = report["model_run"]
    configuration = model.get("configuration", {})
    _table(st, [{"项目": key, "记录": str(value)} for key, value in {
        "模型状态": status_label(model.get("status")), "模型": configuration.get("model_name", "未记录"),
        "归档密钥状态": "已配置" if configuration.get("key_present") else "未配置",
        "实际调用数": model.get("call_count", "未记录"), "重试次数": model.get("retry_count", "未记录"),
        "供应商 token 用量": json.dumps(model.get("provider_reported_usage"), ensure_ascii=False),
        "预算上限": model.get("max_calls", "未记录")}.items()])
    st.caption("密钥状态来自所选报告的非敏感归档，不表示当前 .env 配置。没有读取或展示密钥。未配置可靠价格，不估算费用。")
    health = {item.get("source_id"): item for item in report["source_health"]}
    _table(st, [{"来源": item.get("name"), "类型": item.get("category"), "启用": "是" if item.get("enabled") else "否",
                 "本次状态": status_label(health.get(item.get("source_id"), {}).get("status")),
                 "内容层级": str(item.get("content_access")), "覆盖范围": item.get("supported_date_range"),
                 "限制": item.get("usage_limits")} for item in report["source_registry"]])
    with st.expander("实际采集条数、失败原因与覆盖缺口"):
        _text(st, report["source_health"])
        _text(st, report.get("coverage", {}))


def _sector_readiness(st, archives: list[SectorReportArchive]) -> None:
    if not archives:
        return
    st.subheader("沪深 A 股·板块精选研究")
    st.caption("所选行业成分的数据就绪状态；个股筛选、公告核查及模型研究尚未执行。")
    selected = st.sidebar.selectbox("行业就绪快照", range(len(archives)),
        format_func=lambda index: _label(archives[index].label), key="sector_readiness_version")
    archive = archives[selected]
    # A refresh or download never starts collection and never follows a stored
    # latest_readiness path. The chosen immutable report is revalidated in place.
    archive = read_sector_report(archive.directory, archive.directory.parent.parent.parent)
    report = archive.report
    st.info(status_label(report.get("status") or "pending"))
    _text(st, {"目标交易日": archive.date, "报告生成时间": archive.generated_at,
               "选择编号": archive.selection_id, "研究方式": "行业成分精选" if report.get("research_mode") == "sector_first" else "待核验",
               "证券发现范围": "沪深普通 A 股，暂不含北交所" if report.get("market_scope") == "sse_szse_a" else "待核验",
               "报告版本": archive.version})
    if report.get("mode") == "offline_test" or report.get("verification_kind") == "offline_test":
        st.warning("隔离测试资料：仅用于验证流程与展示，不是真实行业研究结果。")
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    labels = (("catalog", "已取得分类目录"), ("valid_ranking", "有效汇总排名"),
              ("preselected", "预关注行业"), ("selected", "最终关注行业"),
              ("raw_memberships", "预关注原始成分关联"), ("selected_raw_memberships", "关注行业原始成分关联"),
              ("selected_securities", "去重深度行情范围 S"), ("cache_reused", "缓存复用证券"),
              ("history_fetched", "实际补历史证券"), ("history_ready", "历史数据就绪证券"),
              ("adjustment_ready", "可信复权就绪证券"), ("risk_unknown", "风险状态未知证券"),
              ("pending", "待处理证券"))
    _table(st, [{"层面或项目": label, "数量": str(counts[key]) if type(counts.get(key)) is int and counts[key] >= 0 else "待核验"}
               for key, label in labels])
    for key, title in (("universe", "证券发现范围与日期依据"), ("sector_selection", "行业选择与成分核验"),
                       ("history", "所选成分历史、复权与资格"), ("limitations", "数据限制与尚未核查事项")):
        with st.expander(title):
            _text(st, report.get(key) if report.get(key) is not None else "待核验")
    if type(report.get("model_calls")) is int and report["model_calls"] == 0:
        st.caption("本报告记录模型调用：0。F3 筛选、F4 公告研究和生产部署尚未执行。")
    else:
        st.warning("模型调用记录待核验；本页不执行任何模型请求。")
    if st.checkbox("准备中文就绪报告与导出", key="prepare_readiness_exports"):
        _text(st, sector_artifact_bytes(archive, "sector_data_readiness.md").decode("utf-8-sig"))
        for name, (label, mime) in SECTOR_EXPORTS.items():
            data = sector_artifact_bytes(archive, name)
            st.download_button(label, data=data, file_name=f"{archive.selection_id}-{archive.version}-{name}",
                               mime=mime, key="sector_download_" + name, on_click="ignore")


def _sector_research(st, archives: list[SectorResearchArchive], *, purpose: str) -> None:
    if not archives:
        return
    engineering = purpose == "engineering_validation"
    title = "工程验收观察报告（非生产）" if engineering else "今日方向简报 · 行业研究"
    st.subheader(title)
    if engineering:
        st.warning("此栏仅供显式工程验收：样本、技术通过及资格通过均不进入生产关注、候选、今日统计或通知。")
    selected = st.sidebar.selectbox("工程报告版本" if engineering else "行业日报版本", range(len(archives)),
        format_func=lambda index: _label(archives[index].label), key="sector_research_"+purpose)
    previous = archives[selected]
    archive = read_sector_research_report(previous.directory, previous.output_root, purpose=purpose)
    report = archive.report
    _prose(st, report.get("notice", "仅供盘后研究；已保存报告不代表自动任务已执行。"))
    _text(st, {"目标交易日": archive.date, "实际报告生成时间": archive.generated_at,
        "选择编号": report["selection_id"], "报告版本": archive.version,
        "报告用途": "工程验收（非生产）" if engineering else "生产范围的只读盘后日报"})
    counts = report.get("counts", {})
    labels = (("stock_count", "工程样本" if engineering else "关注股票"),
              ("technical_pass_count", "技术通过"), ("eligibility_pass_count", "资格通过"),
              ("formal_candidate_count", "正式候选"))
    for column, (key, label) in zip(st.columns(4), labels):
        column.metric(("工程 · " if engineering else "")+label, counts.get(key, "未记录"))
    if not engineering and counts.get("stock_count") == 0:
        st.info("本期生产关注范围 S=0：没有满足行业选择条件的关注股票；不会用工程样本补入。")
    # The report generator shares these deterministic sections with Markdown
    # and HTML. Display text and tables only; never execute archived HTML.
    for section_title, blocks in report.get("sections", []):
        st.subheader(_label(section_title))
        for kind, value in blocks:
            if kind == "paragraph":
                _prose(st, value)
            elif kind == "subheading":
                st.caption(_label(value))
            elif kind == "table":
                headers, rows = value
                _table(st, [{str(header): "未记录" if item is None else str(item)
                            for header, item in zip(headers, row)} for row in rows])
            else:
                raise ArchiveError("行业报告包含未支持的展示区块")
    if st.checkbox("准备只读报告导出（工程）" if engineering else "准备只读日报导出",
                   key="prepare_sector_exports_" + purpose):
        st.caption("仅导出已登记且哈希匹配的公开报告；输入快照和源文件不会作为任意下载路径。")
        for name, (label, mime) in SECTOR_RESEARCH_EXPORTS.items():
            data = sector_research_artifact_bytes(archive, name)
            st.download_button(label, data=data, file_name=archive.version+"-"+name,
                mime=mime, key="sector_research_download_"+purpose+"_"+name, on_click="ignore")


def _observation_daily(st, archives, output_root):
    index = st.sidebar.selectbox("选择观察日报", range(len(archives)),
        format_func=lambda i: archives[i]["report"]["trade_date"] + " · " + archives[i]["report"]["actual_generated_at"],
        key="daily_observation_version")
    archive = read_observation_report(output_root, archives[index]["directory"])
    from .viewer_dashboard import render_dashboard
    render_dashboard(st, archive, output_root)


def _legacy_daily(st, reports: list[ReportArchive], runs: list[dict]) -> None:
    """Render exactly one historical report view, including exports on demand."""
    st.sidebar.header("历史日报")
    days = sorted({entry.date for entry in reports}, reverse=True)
    day = st.sidebar.selectbox("行情分析日期", days, key="report_date")
    versions = [entry for entry in reports if entry.date == day]
    selected = st.sidebar.selectbox("存档版本", range(len(versions)), format_func=lambda index: versions[index].label, key="report_version")
    archive = versions[selected]
    st.sidebar.caption("查看旧日期不会重新分析。补采与部分覆盖状态以冻结报告为准。")
    st.subheader("所选报告：" + archive.date)
    st.caption(_label('资讯截至 ' + str(archive.report.get('cutoff_at', '未记录')) + ' · ' + ('历史资料补采研究' if archive.report.get('historical_reconstruction') else '按冻结资料生成')))
    with st.sidebar.expander('报告版本与阅读说明'):
        _text(st, "版本：" + archive.version)
        st.caption('切换、搜索、刷新和下载只读取已保存报告，不采集资料、不调用模型。')
    # Streamlit tabs eagerly execute every pane; a selection provides actual
    # lazy rendering and avoids preparing all downloads on every interaction.
    view = st.radio("历史日报内容", ["日报概览", "候选与核验", "证据目录", "运行与来源", "导出"],
                    horizontal=True, key="legacy_report_view", label_visibility="collapsed")
    if view == "日报概览":
        _overview(st, archive)
    elif view == "候选与核验":
        _candidates(st, archive)
    elif view == "证据目录":
        _evidence(st, archive.report)
    elif view == "运行与来源":
        _run_details(st, runs, archive.report)
    elif view == "导出":
        st.caption("仅下载此版本已登记且哈希匹配的公开产物；不提供密钥、原始模型响应或输入快照下载。")
        for name, (label, mime) in EXPORTS.items():
            if name not in archive.manifest.get("files", {}):
                st.info(label + "：本版本未登记")
                continue
            try:
                data = artifact_bytes(archive, name)
                st.download_button(label, data=data, file_name=f"{archive.date}-{archive.version}-{name}", mime=mime, key="download_" + name, on_click="ignore")
            except (ArchiveError, OSError, ValueError) as exc:
                st.error(label + "：" + str(public_value(str(exc))))


def render_app(output_root: Path) -> None:
    """Render one archive family per interaction, without persistent trust caches."""
    import streamlit as st
    from ashare_daily.viewer_dashboard import apply_theme, render_masthead

    st.set_page_config(page_title="今日方向简报", page_icon="📖", layout="wide")
    apply_theme(st)
    render_masthead(st)
    st.sidebar.button("刷新展示", key="refresh_display", help="重新读取已经保存的报告与运行记录，不生成新日报。")
    section = st.sidebar.selectbox("阅读栏目", ["最新简报", "行业研究历史", "行业就绪历史", "历史样本日报", "运行记录", "工程验收（非生产）"],
                                   key="archive_section")
    st.sidebar.caption("当前默认范围暂不含北交所；历史报告范围以各自冻结记录为准。")
    observation_reports, research_reports, sector_reports, reports = [], [], [], []
    observation_problems, research_problems, sector_problems, problems = [], [], [], []
    engineering, engineering_problems = [], []
    # Each reader continues to validate current bytes, purposes and hashes.
    # Only the requested family is read. On the default page, stop discovery
    # once a usable family is found, preserving fallback to older valid data.
    with st.spinner("正在读取并核验所选日报，请稍候…"):
        if section == "最新简报":
            observation_reports, observation_problems = scan_observation_reports(output_root)
            if not observation_reports:
                research_reports, research_problems = scan_sector_research_reports(output_root)
            if not observation_reports and not research_reports:
                sector_reports, sector_problems = scan_sector_reports(output_root)
            if not observation_reports and not research_reports and not sector_reports:
                reports, problems = scan_reports(output_root)
        elif section == "行业研究历史":
            research_reports, research_problems = scan_sector_research_reports(output_root)
        elif section == "行业就绪历史":
            sector_reports, sector_problems = scan_sector_reports(output_root)
        elif section == "历史样本日报":
            reports, problems = scan_reports(output_root)
        elif section == "工程验收（非生产）":
            engineering, engineering_problems = scan_sector_research_reports(output_root, purpose="engineering_validation")
        runs, run_problems = scan_runs(output_root)
    with st.sidebar.expander('任务状态与失败记录'):
        st.subheader("最近一次任务状态")
        actual_runs = [item for item in runs if item.get("status") != "dry_run"]
        if actual_runs:
            latest = actual_runs[0]
            label = status_label(latest.get("status"))
            if latest.get("status") in {"failed", "market_data_not_ready", "market_stale", "calendar_unavailable", "interrupted"} or latest.get("exit_code") not in (None, 0):
                st.warning(label)
            else:
                st.info(label)
            st.caption(_label(scope_label(latest.get("scope"))))
            st.caption(_label('研究日期：' + str(latest.get('target_trade_date', '未记录')) + ' · 运行时间：' + str(latest.get('started_at', '未记录'))))
        else:
            st.info("尚无 M4 日任务记录，不能认定今天已执行成功。")
        if runs and runs[0].get("status") == "dry_run":
            st.caption("最新记录为 dry-run 预览，不替代最近一次真实任务状态。")
        unresolved = unresolved_daily_failure(runs, scope=actual_runs[0].get("scope", "sample") if actual_runs else None)
        if unresolved:
            st.warning("今日尚未解决的失败：" + status_label(unresolved.get("status")) + "；目标日期 "
                       + str(unresolved.get("target_trade_date")) + "；运行 "
                       + str(unresolved.get("run_id", unresolved.get("archive_run_id", "未记录")))
                       + ("。之后的预览、未到时间或复用记录不表示已恢复；同范围、同目标日期的F2行情尚未核验完成。"
                          if unresolved.get("scope") == "sse_szse_a"
                          else "。之后的预览、未到时间或复用记录不表示已恢复；尚未有同目标日期的新报告生成成功。"))
    with st.sidebar.expander('可阅读数据与更新时间'):
        st.subheader("当前可阅读报告的日期")
        if observation_reports:
            st.metric("最近可阅读行情日期", observation_reports[0]["report"]["trade_date"])
            st.caption(_label("观察日报生成于 " + observation_reports[0]["report"]["actual_generated_at"]))
        elif research_reports:
            st.metric("最近可阅读行情日期", research_reports[0].date)
            st.caption(_label("行业日报生成于 "+research_reports[0].generated_at+"；本地报告不代表定时任务已执行。"))
        elif sector_reports:
            st.metric("就绪资料目标日期", sector_reports[0].date)
            st.caption("数据就绪不代表已经完成筛选或研究。")
        elif reports:
            st.metric("最近可阅读行情日期", reports[0].report.get("actual_market_date", "未记录"))
            st.caption(_label('生成于 ' + reports[0].generated_at + ' · ' + status_label(reports[0].report.get('status'))))
        else:
            st.info("当前栏目暂无可阅读报告。网页不会自动采集或调用模型。")
    if observation_problems:
        st.warning("部分观察日报未通过完整性检查，已保留旧报告。")
        with st.expander("观察日报读取问题"):
            _table(st, observation_problems)
    if problems or run_problems:
        st.warning(f"发现 {len(problems)} 个未完整发布或损坏的报告版本、{len(run_problems)} 个异常运行记录；没有将它们显示为成功。")
        with st.expander("存档读取问题"):
            _table(st, problems + run_problems)
    for entries, label in ((research_problems, "行业日报"), (sector_problems, "板块就绪报告"),
                           (engineering_problems, "工程报告（非生产）")):
        if entries:
            st.warning(label + "的部分存档未通过身份、用途或哈希核验，已拒绝展示。")
            with st.expander(label + "读取问题"):
                _table(st, entries)
    try:
        if section == "运行记录":
            _run_details(st, runs, None)
        elif section == "工程验收（非生产）":
            if engineering:
                _sector_research(st, engineering, purpose="engineering_validation")
            else:
                st.info("暂无通过用途和哈希核验的隔离工程报告。")
        elif observation_reports:
            _observation_daily(st, observation_reports, output_root)
        elif research_reports:
            _sector_research(st, research_reports, purpose="production")
        elif sector_reports:
            _sector_readiness(st, sector_reports)
        elif reports:
            _legacy_daily(st, reports, runs)
        else:
            st.info("暂无可阅读报告。请切换阅读栏目或核对运行记录。")
    except (ArchiveError, OSError, ValueError, TypeError, KeyError) as exc:
        st.warning("报告在读取时发生变化，已停止展示该版本：" + str(public_value(str(exc))))
