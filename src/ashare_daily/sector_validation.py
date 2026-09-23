"""Explicit real-data engineering scopes. Never a production selection fallback."""
from copy import deepcopy
from datetime import datetime
import csv
import hashlib
import io
import json
from pathlib import Path
import re

from .market_schemas import SHANGHAI
from .operations.daily import local_path
from .sector_pipeline import load_config, read_selection, write_new, permission_allowed
from .sector_selection import digest, verify_selection, map_members, symbol_of, confirmed_full_day_halt

VALIDATION_OUTPUT = "outputs/engineering_validation/f3s"
VALIDATION_DATA = "data/engineering_validation/f3s"


def validation_config(root, path="config/sector_validation.json"):
    config = load_config(root, path)
    if (config.get("purpose") != "engineering_validation" or config.get("production_eligible") is not False
            or config.get("validation_selection_policy") != "smallest_complete_multi_member_industry_v1"):
        raise ValueError("engineering_validation_config_required")
    root = Path(root).resolve()
    for key, parent in (("database", VALIDATION_DATA), ("output_directory", VALIDATION_OUTPUT),
                        ("history_lock_file", VALIDATION_DATA)):
        if not local_path(root, config[key]).is_relative_to(root / parent):
            raise ValueError("validation_write_target_must_be_isolated")
    return config


def directory_for(root, config, selection_id):
    if not re.fullmatch(r"validation-sector-\d{4}-\d{2}-\d{2}-[a-f0-9]{20}", selection_id):
        raise ValueError("explicit_validation_selection_id_required")
    return local_path(Path(root), config["output_directory"]) / selection_id


def _mapped_scope(parent, packet):
    candidates = []
    for sector in parent["sectors"]:
        if (sector.get("kind") != "industry" or sector.get("taxonomy") != parent["taxonomy"]
                or not sector.get("membership_complete") or not sector.get("quote_complete")):
            continue
        rows, members, issues = map_members(packet["memberships"][sector["sector_id"]], packet["universe"], sector)
        if not issues and len(members) >= 2:
            candidates.append((len(members), sector["sector_id"], sector, rows, members))
    if not candidates:
        raise ValueError("no_complete_multi_member_industry_for_validation")
    _, _, sector, rows, mapped = min(candidates, key=lambda item: item[:2])
    quotes = {row["symbol"]: row for row in packet["quotes"]["rows"]}
    members = []
    for identity in sorted(mapped):
        member = {**deepcopy(mapped[identity]), "sector_ids": [sector["sector_id"]]}
        extra = quotes.get(symbol_of(member), {}).get("full_day_halt_evidence")
        if isinstance(extra, dict) and confirmed_full_day_halt({"statuses": {"suspended": extra}}, parent["target_date"]):
            member["supplemental_status_evidence"] = {"suspended": deepcopy(extra)}
        members.append(member)
    return sector, rows, members


def freeze_validation(root, source_selection_id, *, config_path="config/sector_validation.json", dry_run=False):
    root = Path(root).resolve()
    config = validation_config(root, config_path)
    parent_config = load_config(root, "config/sector_first.json")
    parent, _ = read_selection(root, parent_config, source_selection_id)
    if parent.get("mode") != "research" or not parent["selection_verified"] or not parent["industry_comparison_complete"]:
        raise ValueError("verified_complete_source_selection_required")
    source_dir = local_path(root, parent_config["output_directory"]) / source_selection_id
    source_file = source_dir / "source_inputs.json"
    packet = json.loads(source_file.read_text(encoding="utf-8"))
    sector, mapping, members = _mapped_scope(parent, packet)
    if dry_run:
        return {"status": "dry_run", "purpose": "engineering_validation", "production_eligible": False,
                "sector_id": sector["sector_id"], "denominator": len(members), "network_requests": 0, "model_calls": 0}
    created = datetime.now(SHANGHAI).isoformat()
    payload = {key: deepcopy(value) for key, value in parent.items() if key not in {"selection_id", "content_hash"}}
    payload.update(schema_version="f3s-validation-selection-v1", purpose="engineering_validation", production_eligible=False,
        title="沪深 A 股·真实数据工程验收（不代表自动入选或投资关注）", automatic_selection=False,
        source_selection_id=source_selection_id, source_selection_hash=parent["content_hash"],
        source_cutoff_at=parent["cutoff_at"], cutoff_at=created, validation_created_at=created,
        source_selection_counts={key: parent[key] for key in ("preselected_count", "selected_count", "selected_security_count")},
        selection_basis="smallest_complete_multi_member_industry_v1",
        validation_reason="按完整成分数至少2、成分数升序、稳定行业ID选取一个全行业，验证多证券采集、计算和中断恢复；不是投资关注",
        validation_sector_id=sector["sector_id"], config_hash=digest(config), members=members,
        selection_status="selected", selected_count=1, selected_sectors=[deepcopy(sector)],
        selected_security_count=len(members), selected_raw_membership_count=len(mapping),
        scope_notice="真实数据工程验收，不代表自动入选或投资关注；禁止进入生产日报、候选、通知与统计",
        source_packet_ref={"path": source_file.relative_to(root).as_posix(), "sha256": hashlib.sha256(source_file.read_bytes()).hexdigest()})
    payload["content_hash"] = digest(payload)
    payload["selection_id"] = "validation-sector-" + payload["target_date"] + "-" + payload["content_hash"][:20]
    verify_selection(payload)
    directory = directory_for(root, config, payload["selection_id"])
    write_new(directory / "sector_selection.json", payload)
    write_new(directory / "frozen_config.json", config)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["sector_id", "sector_name", "security_id", "symbol", "listing_board", "mapping_status", "reason"])
    writer.writeheader()
    writer.writerows({key: row.get(key) for key in writer.fieldnames} for row in mapping)
    (directory / "validation_membership.csv").write_bytes(stream.getvalue().encode("utf-8-sig"))
    write_new(directory / "purpose.json", {"purpose": "engineering_validation", "production_eligible": False,
        "selection_id": payload["selection_id"], "source_selection_id": source_selection_id, "model_calls": 0})
    return {"status": "validation_scope_frozen", "selection_id": payload["selection_id"], "target_date": payload["target_date"],
        "purpose": "engineering_validation", "production_eligible": False, "sector_id": sector["sector_id"],
        "counts": {"validation_securities": len(members), "raw_memberships": len(mapping)},
        "json_path": str(directory / "sector_selection.json"), "network_requests": 0, "model_calls": 0}


def read_validation(root, selection_id, *, config_path="config/sector_validation.json", require_permission=False):
    root = Path(root).resolve()
    current = validation_config(root, config_path)
    directory = directory_for(root, current, selection_id)
    value = verify_selection(json.loads((directory / "sector_selection.json").read_text(encoding="utf-8")))
    if value.get("mode") != "research" or value.get("purpose") != "engineering_validation":
        raise ValueError("real_engineering_validation_selection_required")
    frozen = json.loads((directory / "frozen_config.json").read_text(encoding="utf-8"))
    if value["selection_id"] != selection_id or value["config_hash"] != digest(frozen):
        raise ValueError("frozen_validation_identity_or_config_mismatch")
    if any(frozen.get(key) != current.get(key) for key in ("database", "output_directory", "purpose", "production_eligible", "history_lock_file")):
        raise ValueError("validation_storage_config_changed")
    if require_permission and not permission_allowed(current):
        raise ValueError("validation_current_source_permission_required")
    parent_config = load_config(root, "config/sector_first.json")
    parent, _ = read_selection(root, parent_config, value["source_selection_id"])
    if parent["content_hash"] != value["source_selection_hash"] or parent["cutoff_at"] != value["source_cutoff_at"]:
        raise ValueError("validation_parent_evidence_mismatch")
    source_file = local_path(root, value["source_packet_ref"]["path"])
    expected_source = local_path(root, parent_config["output_directory"]) / parent["selection_id"] / "source_inputs.json"
    if source_file != expected_source:
        raise ValueError("validation_source_must_be_original_parent_archive")
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != value["source_packet_ref"]["sha256"]:
        raise ValueError("validation_original_source_hash_mismatch")
    sector, _, members = _mapped_scope(parent, json.loads(source_file.read_text(encoding="utf-8")))
    if sector["sector_id"] != value["validation_sector_id"] or members != value["members"]:
        raise ValueError("validation_members_incomplete_or_changed")
    return value, frozen
