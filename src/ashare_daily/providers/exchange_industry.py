"""Offline, evidenced industry sections from the existing exchange universe.

This is an explicit local normalization of exchange website labels. It does not
assert that both exchanges declare the same official classification revision.
No network capability, separate universe, or market-price calculation lives here.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from ..operations.backup import _io
from ..operations.paths import resolve_archived_path
from ..universe import SHANGHAI, BOARD_EXCHANGE, _json
from .exchange_universe import SSE_ENDPOINT, SZSE_ENDPOINT, sse_page, szse_page


TAXONOMY = "exchange_industry_section_v1"
MAPPING_VERSION = "exchange-website-sections-20260911-v1"
# Each pair is an exact label observed in the frozen source. O had no SSE rows;
# its display label comes from SZSE and no unseen SSE label is invented.
SECTION_LABELS = {
    "A": ("农、林、牧、渔业", "农林牧渔"), "B": ("采矿业", "采矿业"),
    "C": ("制造业", "制造业"), "D": ("电力、热力、燃气及水生产和供应业", "水电煤气"),
    "E": ("建筑业", "建筑业"), "F": ("批发和零售业", "批发零售"),
    "G": ("交通运输、仓储和邮政业", "运输仓储"), "H": ("住宿和餐饮业", "住宿餐饮"),
    "I": ("信息传输、软件和信息技术服务业", "信息技术"), "J": ("金融业", "金融业"),
    "K": ("房地产业", "房地产"), "L": ("租赁和商务服务业", "商务服务"),
    "M": ("科学研究和技术服务业", "科研服务"), "N": ("水利、环境和公共设施管理业", "公共环保"),
    "O": (None, "居民服务"), "P": ("教育", "教育"), "Q": ("卫生和社会工作", "卫生"),
    "R": ("文化、体育和娱乐业", "文化传播"), "S": ("综合", "综合"),
}
GROUPS = {("sse", "main_a"), ("sse", "star"), ("szse", "a_shares")}


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_source_json_key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite_source_number")
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)


def _read(root, locator):
    if not isinstance(locator, str) or not locator:
        raise ValueError("exchange_source_locator_missing")
    path = resolve_archived_path(locator, anchor=root).resolve()
    if not path.is_relative_to(root):
        raise ValueError("exchange_source_path_outside_project")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if _io(parent).is_symlink() or (hasattr(parent, "is_junction") and _io(parent).is_junction()):
            raise ValueError("exchange_source_link_not_allowed")
    if _io(path).stat().st_size > 32_000_000:
        raise ValueError("exchange_source_file_too_large")
    raw = _io(path).read_bytes()
    return _strict_json(raw), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _date_stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None or stamp > datetime.now(SHANGHAI):
        raise ValueError("exchange_source_observation_time_invalid")
    return stamp


def _mapping_rows():
    return [{"code": code, "name": sse or szse, "source_aliases": {"SSE": sse, "SZSE": szse},
        "normalization_version": MAPPING_VERSION, "source_official_revision": None,
        "mapping_basis": "explicit_source_code_and_label_pairs_not_name_only_join"}
        for code, (sse, szse) in SECTION_LABELS.items()]


def _validate_source_pages(root, universe, file_refs):
    manifests = universe.get("source_manifests", [])
    by_group = {}
    for value in manifests:
        key = (value.get("provider"), value.get("dataset"))
        if key in by_group or key not in GROUPS:
            raise ValueError("exchange_universe_source_group_conflict")
        if (value.get("complete") is not True or value.get("authoritative") is not True
                or value.get("permission_status") != "approved" or value.get("errors")
                or value.get("provenance_mode") != ("online" if universe["mode"] == "research" else "offline_test")):
            raise ValueError("exchange_universe_source_boundary_unverified")
        by_group[key] = value
    if set(by_group) != GROUPS:
        raise ValueError("exchange_universe_source_group_missing")
    pages = defaultdict(list)
    for value in universe.get("page_manifest", []):
        key = (value.get("provider"), value.get("dataset"))
        if key not in GROUPS:
            raise ValueError("exchange_page_group_unknown")
        pages[key].append(value)
    if set(pages) != GROUPS:
        raise ValueError("exchange_page_group_missing")
    found, enums, source_dates, frozen_paths = {}, [], [], set()
    for key in sorted(GROUPS):
        manifest = by_group[key]
        group_pages = sorted(pages[key], key=lambda p: p["page_number"])
        expected_pages, expected_records = manifest.get("expected_pages"), manifest.get("expected_records")
        if (type(expected_pages) is not int or type(expected_records) is not int or not 1 <= expected_pages <= 500
                or [p["page_number"] for p in group_pages] != list(range(1, expected_pages+1))):
            raise ValueError("exchange_pages_missing_duplicate_or_truncated")
        total = 0
        for page in group_pages:
            response, ref = _read(root, page.get("raw_response_path"))
            file_refs[ref["path"]] = ref
            # The unchanged sibling snapshot ties these pages back to the original U.
            snapshot_path = Path(ref["path"]).parent.parent.parent / "snapshot.json"
            if str(snapshot_path) not in frozen_paths:
                frozen, snapshot_ref = _read(root, str(snapshot_path))
                if frozen != universe:
                    raise ValueError("original_universe_snapshot_differs")
                file_refs[snapshot_ref["path"]] = snapshot_ref
                frozen_paths.add(str(snapshot_path))
            if (response.get("ok") is not True or response.get("http_status") != 200
                    or response.get("verification_kind") != ("live_network" if universe["mode"] == "research" else "offline_test")
                    or response.get("provenance_mode") != ("online" if universe["mode"] == "research" else "offline_test")
                    or response.get("target_date") != universe.get("requested_date")):
                raise ValueError("exchange_http_response_provenance_invalid")
            _date_stamp(response.get("fetched_at"))
            body, raw_ref = _read(root, response.get("raw_path"))
            if (raw_ref["sha256"] != page.get("raw_sha256") or raw_ref["sha256"] != response.get("raw_sha256")
                    or body != response.get("body")):
                raise ValueError("exchange_raw_body_hash_or_payload_conflict")
            file_refs[raw_ref["path"]] = raw_ref
            params = response.get("params")
            parts = urlsplit(response.get("url", ""))
            endpoint = parts.scheme + "://" + parts.netloc + parts.path
            if parts.fragment or parts.username or parts.password or not isinstance(params, dict):
                raise ValueError("exchange_source_request_identity_invalid")
            query = parse_qs(parts.query, keep_blank_values=True)
            if query != {str(k): [str(v)] for k, v in params.items()}:
                raise ValueError("exchange_source_request_parameters_conflict")
            if key[0] == "sse":
                stock_type = "1" if key[1] == "main_a" else "8"
                if (endpoint != SSE_ENDPOINT or params.get("STOCK_TYPE") != stock_type
                        or params.get("CSRC_CODE") != "" or params.get("STOCK_CODE") != ""
                        or params.get("REG_PROVINCE") != "" or params.get("COMPANY_STATUS") != "2,4,5,7,8"
                        or params.get("pageHelp.pageNo") != page["page_number"]):
                    raise ValueError("exchange_source_filtered_or_request_conflict")
                records, boundary = sse_page(body, stock_type=stock_type, requested_page=page["page_number"])
                exchange = "SSE"
            else:
                if (endpoint != SZSE_ENDPOINT or params != {"SHOWTYPE": "JSON", "CATALOGID": "1110", "TABKEY": "tab1", "PAGENO": page["page_number"]}):
                    raise ValueError("exchange_source_filtered_or_request_conflict")
                records, boundary = szse_page(body, requested_page=page["page_number"])
                metadata = next(t["metadata"] for t in body if t.get("metadata", {}).get("tabkey") == "tab1")
                options = [c for c in metadata.get("conditions", []) if c.get("name") == "selectHylb"]
                if len(options) != 1 or metadata.get("cols", {}).get("sshymc") != "所属行业":
                    raise ValueError("exchange_szse_industry_enum_missing")
                pairs = [(o.get("value"), o.get("text")) for o in options[0].get("options", []) if o.get("value") != ""]
                expected = [(code, code + " " + labels[1]) for code, labels in SECTION_LABELS.items()]
                if sorted(pairs) != sorted(expected):
                    raise ValueError("exchange_szse_industry_enum_changed")
                enums.append({"source": "SZSE", "values": dict(pairs), "evidence_path": ref["path"]})
                exchange = "SZSE"
            if (boundary != page.get("source_boundary") or len(records) != page.get("record_count")
                    or boundary["terminal"] is not page.get("terminal")
                    or boundary["expected_pages"] != expected_pages or boundary["expected_records"] != expected_records):
                raise ValueError("exchange_frozen_page_boundary_conflict")
            source_dates.append({"source": exchange, "source_business_date": boundary.get("source_as_of_date"),
                "observed_at": response["fetched_at"], "evidence_path": ref["path"]})
            total += len(records)
            for raw in records:
                code = raw.get("A_STOCK_CODE") if exchange == "SSE" else raw.get("agdm")
                identity_key = (exchange, code)
                if identity_key in found:
                    raise ValueError("exchange_source_duplicate_security")
                found[identity_key] = {"raw": raw, "page": ref, "source_business_date": boundary.get("source_as_of_date"),
                    "observed_at": response["fetched_at"], "dataset": key[1]}
        if total != expected_records or group_pages[-1].get("terminal") is not True:
            raise ValueError("exchange_source_terminal_reconciliation_failed")
    return found, source_dates, enums


def exchange_industry_snapshot(root, universe):
    """Derive source sections only after revalidating original immutable pages."""
    if not isinstance(universe, dict):
        raise ValueError("industry_universe_object_required")
    root = Path(root).resolve()
    mode = universe.get("mode") if isinstance(universe, dict) else None
    mapping = _mapping_rows()
    refs, issues, grouped, sources, enums = {}, [], defaultdict(list), [], []
    try:
        if (mode not in {"research", "offline_test"} or universe.get("scope") != "sse_szse_a"
                or universe.get("universe_verified") is not True or universe.get("collection_ready") is not True
                or universe.get("blockers")):
            raise ValueError("industry_requires_verified_scoped_universe")
        body = {k: v for k, v in universe.items() if k not in {"snapshot_id", "content_hash"}}
        hashed = _digest(body)
        if (hashed != universe.get("content_hash")
                or universe.get("snapshot_id") != "universe-"+universe["requested_date"]+"-"+hashed[:20]):
            raise ValueError("industry_universe_content_hash_invalid")
        _date_stamp(universe.get("observed_at"))
        found, sources, enums = _validate_source_pages(root, universe, refs)
        seen, security_ids = set(), set()
        for member in universe.get("members", []):
            key = (member.get("exchange"), member.get("code"))
            if key in seen or key not in found:
                raise ValueError("industry_universe_member_missing_or_duplicate")
            seen.add(key)
            if not isinstance(member.get("security_id"), str) or not member["security_id"] or member["security_id"] in security_ids:
                raise ValueError("industry_security_id_missing_or_conflicting")
            security_ids.add(member["security_id"])
            if (member.get("metadata_verified") is not True or member.get("metadata_issues")
                    or BOARD_EXCHANGE.get(member.get("board")) != key[0]
                    or not re.fullmatch(r"[0-9]{6}", key[1] or "")):
                raise ValueError("industry_security_identity_metadata_unverified")
            source = found[key]
            raw = source["raw"]
            snapshot_raw = member.get("raw", {}).get("listing") if key[0] == "SSE" else member.get("raw")
            if raw != snapshot_raw:
                raise ValueError("industry_member_raw_source_conflict")
            if key[0] == "SSE":
                section, label = raw.get("CSRC_CODE"), raw.get("CSRC_CODE_DESC")
                expected = SECTION_LABELS.get(section, (None, None))[0]
                field = "CSRC_CODE/CSRC_CODE_DESC"
            else:
                value = raw.get("sshymc")
                match = re.fullmatch(r"([A-S]) (.+)", value or "")
                section, label = (match[1], match[2]) if match else (None, None)
                expected = SECTION_LABELS.get(section, (None, None))[1]
                field = "sshymc + metadata.conditions.selectHylb"
            if expected is None or label != expected:
                issues.append("industry_code_or_label_unmapped:" + key[0] + ":" + key[1])
                continue
            symbol = {"SSE": "sh", "SZSE": "sz"}[key[0]] + key[1]
            grouped[section].append({"symbol": symbol, "security_id": member["security_id"], "code": key[1],
                "exchange": key[0], "name": member.get("name"), "security_type": member.get("security_type"),
                "listing_board": member["board"], "metadata_verified": True,
                "metadata_evidence": member.get("evidence_id"), "source_industry_code": section,
                "source_industry_label": label, "source_industry_field": field,
                "source_business_date": source["source_business_date"], "observed_at": source["observed_at"],
                "source_evidence_path": source["page"]["path"], "source_record": deepcopy(raw), "issues": []})
        if seen != set(found):
            raise ValueError("industry_universe_omits_source_members")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        issues.append(str(exc))
    complete = not issues
    common = {"provider": "exchange_lists", "schema_version": "exchange-industry-v1", "taxonomy": TAXONOMY,
        "normalization_version": MAPPING_VERSION, "source_official_revision": None,
        "status": "ok" if complete else "blocked", "complete": complete, "boundary_verified": complete,
        "provenance_mode": "online" if mode == "research" else "offline_test",
        "verification_kind": "archived_live_source_revalidation" if mode == "research" else "offline_test",
        "source_business_date": None, "date_basis": "observed_exchange_metadata_not_historical_classification",
        "derived_at": datetime.now(SHANGHAI).isoformat(), "source_dates": sources,
        "universe_snapshot_id": universe.get("snapshot_id"), "universe_content_hash": universe.get("content_hash"),
        "issues": sorted(set(issues)), "evidence": [], "file_refs": list(refs.values()),
        "boundary_basis": "original_universe_hash_and_each_exchange_source_page_boundary_revalidated",
        "new_network_requests": 0, "source_price_data": False}
    catalog_rows, memberships = [], {}
    for mapped in mapping:
        code, name = mapped["code"], mapped["name"]
        sector_id = "exchange-section:" + code
        rows = sorted(grouped[code], key=lambda r: r["symbol"])
        catalog_rows.append({"sector_id": sector_id, "provider_id": code, "name": name,
            "taxonomy": TAXONOMY, "kind": "industry", "mapping": mapped, "source_change_pct": None,
            "source_amount_cny": None, "date_verified": False, "source_business_date": None,
            "displayed_count": len(rows), "count_basis": "partition_of_source_boundary_verified_universe"})
        memberships[sector_id] = {**deepcopy(common), "operation": "members", "sector_id": sector_id,
            "rows": rows, "displayed_count": len(rows), "count_discrepancy": None,
            "zero_members": not rows, "raw_type_counts": dict(Counter(r["security_type"] for r in rows))}
    return {"catalog": {**common, "operation": "catalog", "rows": catalog_rows, "normalization_mapping": mapping,
        "source_enumeration_evidence": enums, "raw_discovered_members": sum(len(v) for v in grouped.values()),
        "source_member_type_counts": dict(Counter(r["security_type"] for values in grouped.values() for r in values)),
        "unmapped_count": len(universe.get("members", []))-sum(len(v) for v in grouped.values())},
        "memberships": memberships}
