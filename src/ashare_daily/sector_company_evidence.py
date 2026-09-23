"""Read-only company material preparation against the existing M3 evidence index.

This is a bounded local search, not an announcement crawler or an absence-of-risk
check. The caller owns immutable revision publication. No model is called here.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .operations.backup import _io
from .operations.paths import resolve_archived_path
from .research.contracts import SHANGHAI, aware_time
from .research.evidence import build_evidence, canonical_url, select_evidence, validate_claims
from .research.sources import load_source_registry, parse_registered_article
from .sector_selection import digest, verify_selection

SCHEMA = "f4s1-company-materials-v1"
RULE_VERSION = "f4s1-local-materials-v1"
TABLES = ("market_metadata", "m3_metadata", "m3_source_registry", "m3_source_active",
          "m3_evidence", "m3_observations", "m3_events")


def _read(root, locator):
    path = Path(locator)
    if not path.is_absolute():
        path = root / path
    path = resolve_archived_path(str(path), anchor=root).resolve()
    if not path.is_relative_to(root) or any(part.lower() in {".env", ".git", "secrets"} for part in path.parts):
        raise ValueError("company material path outside permitted project")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if _io(parent).is_symlink() or (hasattr(parent, "is_junction") and _io(parent).is_junction()):
            raise ValueError("company material path link rejected")
    if _io(path).stat().st_size > 10_000_000:
        raise ValueError("company material file exceeds bounded size")
    raw = _io(path).read_bytes()
    return raw, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate company material JSON key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _index(root, mode):
    relative = "data/research/market.sqlite3" if mode == "research" else "data/offline_test/company_materials/market.sqlite3"
    path = root / relative
    snapshot = {"path": relative, "access": "sqlite_read_only_transaction", "tables": {},
                "evidence_count": 0, "status": "missing", "source_counts": {}}
    if not _io(path).exists():
        snapshot["content_hash"] = digest(snapshot)
        return snapshot, [], []
    # Check the complete lexical path before resolve(), including link parents.
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if _io(parent).is_symlink() or (hasattr(parent, "is_junction") and _io(parent).is_junction()):
            raise ValueError("company index path link rejected")
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        connection.row_factory = sqlite3.Row
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = {}
        for table in TABLES:
            if table not in names:
                snapshot["tables"][table] = {"status": "missing", "rows": 0, "sha256": None}
                rows[table] = []
                continue
            values = [dict(row) for row in connection.execute('SELECT * FROM "'+table+'"')]
            values.sort(key=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False))
            rows[table] = values
            snapshot["tables"][table] = {"status": "read", "rows": len(values), "sha256": digest(values)}
        metadata = {row["key"]: row["value"] for row in rows["m3_metadata"]}
        market = {row["key"]: row["value"] for row in rows["market_metadata"]}
        expected = "real" if mode == "research" else "offline_test"
        if metadata.get("verification_kind") != expected or metadata.get("schema_version") != "m3-evidence-v1":
            raise ValueError("company evidence index provenance mode mismatch")
        if mode == "research" and (market.get("mode") != "research" or
                market.get("schema_version") != "m1-baostock-market-v1" or market.get("verification_kind") == "offline_test"):
            raise ValueError("company evidence index is not verified research M1")
        if mode == "offline_test" and market and market.get("verification_kind") != "offline_test":
            raise ValueError("offline company material cannot read research index")
        registrations = {}
        for row in rows["m3_source_registry"]:
            payload = _json(row["payload_json"].encode())
            if digest(payload) != row["version_hash"] or payload.get("source_id") != row["source_id"]:
                raise ValueError("company index source registration hash mismatch")
            registrations[(row["source_id"], row["version_hash"])] = payload
        for row in rows["m3_source_active"]:
            if (row["source_id"], row["version_hash"]) not in registrations:
                raise ValueError("company index active source registration missing")
        evidence, rejected = [], []
        for row in rows["m3_evidence"]:
            try:
                item = build_evidence(**_json(row["payload_json"].encode()))
                if any(item[key] != row[key] for key in ("evidence_id", "source_id", "content_hash", "first_seen_at")):
                    raise ValueError("indexed columns disagree with immutable evidence")
                if canonical_url(item["original_url"]) != row["canonical_url"]:
                    raise ValueError("indexed canonical URL mismatch")
                if (item["acquisition_mode"] == "offline_test") != (mode == "offline_test"):
                    raise ValueError("indexed evidence mode mismatch")
                if not any(key[0] == item["source_id"] for key in registrations):
                    raise ValueError("indexed evidence source registration missing")
                evidence.append(item)
            except (ValueError, TypeError, KeyError) as error:
                rejected.append({"evidence_id": row.get("evidence_id"), "reason": str(error)})
        snapshot.update(status="verified" if not rejected else "index_contains_invalid_evidence",
                        evidence_count=len(rows["m3_evidence"]), valid_evidence_count=len(evidence),
                        source_counts=dict(sorted(Counter(row["source_id"] for row in rows["m3_evidence"]).items())))
    snapshot["content_hash"] = digest(snapshot)
    return snapshot, evidence, rejected


def _body_proof(root, item, source):
    """Replay the existing registered HTML archive; a title is never a body."""
    raw, raw_ref = _read(root, item["raw_locator"])
    directory = Path(raw_ref["path"]).parent.parent
    collection_raw, collection_ref = _read(root, directory / "result.json")
    registry_raw, registry_ref = _read(root, directory / "registry_snapshot.json")
    collection, registry = _json(collection_raw), _json(registry_raw)
    requests = list(collection.get("requests", []))
    # Earlier M3 collections nested requests under source_health.
    requests += [request for health in collection.get("source_health", []) for request in health.get("requests", [])]
    matching = [request for request in requests if request.get("raw_sha256") == raw_ref["sha256"]
                and request.get("url") == item["original_url"] and request.get("http_status") == 200
                and request.get("status") == "ok"]
    if not matching:
        raise ValueError("original article response/hash/HTTP provenance missing")
    sources = [value for value in registry.get("sources", []) if value.get("registration", {}).get("source_id") == item["source_id"]]
    if len(sources) != 1:
        raise ValueError("original article source snapshot missing")
    frozen_source = sources[0]
    if frozen_source["registration"].get("cache_allowed") is not True:
        raise ValueError("original article local cache permission missing")
    pages = [page for page in frozen_source.get("pages", []) if page.get("url") == item["original_url"]]
    if len(pages) != 1:
        raise ValueError("original article registered parsing page missing")
    parsed = parse_registered_article(raw, frozen_source, pages[0])
    for key in ("title", "published_at", "publication_precision", "content", "content_type", "content_truncated"):
        if parsed[key] != item[key]:
            raise ValueError("original article replay differs from indexed "+key)
    return {"raw_source": raw_ref, "collection": collection_ref, "registry_snapshot": registry_ref,
            "body_sha256": item["content_hash"], "source_registration_hash": digest(source["registration"])}


def _context(selection, technical, eligibility, cutoff):
    verify_selection(selection)
    purpose = selection.get("purpose", "production")
    if selection.get("mode") not in {"research", "offline_test"}:
        raise ValueError("company material mode invalid")
    for report, field, schema in ((technical, "result_hash", "f3s-screening-result-v1"),
                                  (eligibility, "content_hash", "f4s1-eligibility-result-v1")):
        if report.get("schema_version") != schema or report.get(field) != digest({key: value for key, value in report.items() if key != field}):
            raise ValueError("company material input result hash/schema mismatch")
        if any(report.get(key) != expected for key, expected in (("selection_id", selection["selection_id"]),
                ("target_date", selection["target_date"]), ("purpose", purpose), ("mode", selection["mode"]))):
            raise ValueError("company material input selection/date/purpose/mode mismatch")
        if cutoff < aware_time(report["cutoff_at"]):
            raise ValueError("company material cutoff precedes input")
        if purpose == "engineering_validation" and report.get("production_eligible") is not False:
            raise ValueError("engineering company material cannot be production eligible")
    if (eligibility.get("technical_result_hash") != technical["result_hash"] or
            eligibility.get("selection_content_hash") != selection["content_hash"]):
        raise ValueError("company material eligibility input binding mismatch")
    indexes = []
    expected_ids = {member["security_id"] for member in selection["members"]}
    for report in (technical, eligibility):
        rows = {row["security_id"]: row for row in report["evaluations"]}
        if len(rows) != len(report["evaluations"]) or set(rows) != expected_ids:
            raise ValueError("company material requires every frozen member exactly once")
        indexes.append(rows)
    return indexes


def prepare_company_materials(root, selection, technical_report, eligibility_report, *, cutoff_at, output_dir=None):
    """Return a deterministic diagnostic package; never write even with output_dir.

    The optional output_dir is accepted for the caller's publication API only.
    It does not affect the material fingerprint or initiate filesystem writes.
    """
    root = Path(root).resolve()
    cutoff = aware_time(cutoff_at)
    if cutoff > datetime.now(SHANGHAI):
        raise ValueError("company material cutoff cannot be in future")
    technical, eligibility = _context(selection, technical_report, eligibility_report, cutoff)
    snapshot, evidence, rejected = _index(root, selection["mode"])
    registry_path = root / "config/m3_sources.json"
    registry = load_source_registry(registry_path) if _io(registry_path).exists() else None
    sources = {source["registration"]["source_id"]: source for source in registry["sources"]} if registry else {}
    permissions = [dict(source["registration"]) for _, source in sorted(sources.items())]
    evaluations, packages = [], []
    # Keep the original event window independent of the actual later evidence
    # preparation time. This is the existing DailyConfig initial-window rule.
    daily_path = root / "config/sector_first_daily.json"
    daily_config = _json(_io(daily_path).read_bytes()) if _io(daily_path).exists() else {}
    lookback = daily_config.get("first_query_lookback_days", 3)
    if type(lookback) is not int or not 1 <= lookback <= 14:
        raise ValueError("company material lookback exceeds existing DailyConfig limits")
    start = datetime.combine(date.fromisoformat(selection["target_date"])-timedelta(days=lookback), time.min, SHANGHAI)
    event_cutoff = aware_time(selection.get("source_cutoff_at", selection["cutoff_at"]))
    if not start < event_cutoff <= cutoff:
        raise ValueError("company material original event window invalid")
    for member in sorted(selection["members"], key=lambda value: value["security_id"]):
        security_id = member["security_id"]
        symbol = {"SSE": "sh.", "SZSE": "sz."}.get(member["exchange"], "?") + member["code"]
        tech, qualified = technical[security_id], eligibility[security_id]
        if tech["technical_status"] not in {"pass", "fail", "pending", "unknown", "not_applicable"} or qualified["eligibility_status"] not in {"pass", "fail", "pending"}:
            raise ValueError("company material input status invalid")
        if any(row.get("symbol", symbol) != symbol for row in (tech, qualified)) or qualified.get("technical_status") != tech["technical_status"]:
            raise ValueError("company material security/status identity mismatch")
        triggered = tech["technical_status"] == "pass" and qualified["eligibility_status"] != "fail"
        evaluation = {"security_id": security_id, "symbol": symbol, "name": member.get("name"),
            "technical_status": tech["technical_status"], "eligibility_status": qualified["eligibility_status"],
            "material_queue_status": "diagnostic_pending" if triggered and qualified["eligibility_status"] == "pending" else "local_evidence_preparation" if triggered else "excluded" if tech["technical_status"] == "pass" else "not_triggered",
            "formal_candidate": False, "model_queue_eligible": False}
        evaluations.append(evaluation)
        if not triggered:
            continue
        matches, usable, item_rejections, proofs = [], [], [], {}
        for item in evidence:
            name = member.get("name") or ""
            associations = [assoc for assoc in item["security_associations"] if assoc["symbol"] == symbol and assoc["name"] == name]
            if not (member["code"] in item["title"]+item["content"] or name and name in item["title"]+item["content"] or associations):
                continue
            matches.append(item["evidence_id"])
            try:
                if aware_time(item["first_seen_at"]) > cutoff or aware_time(item["fetched_at"]) > cutoff:
                    raise ValueError("observed_after_preparation_cutoff")
                # A frozen association must actually locate the full issuer name
                # and security code in body text. Name-only hits remain diagnostic.
                if not any(assoc["association_type"] == "explicit_subject" and name in assoc["basis_quote"]
                           and member["code"] in assoc["basis_quote"] for assoc in associations):
                    raise ValueError("explicit_issuer_body_identity_unverified")
                source = sources.get(item["source_id"])
                if not source or source["registration"].get("enabled") is not True or source["registration"].get("cache_allowed") is not True:
                    raise ValueError("local_source_permission_unavailable")
                if item["content_type"] != "fulltext":
                    raise ValueError("company_fulltext_not_obtained")
                proofs[item["evidence_id"]] = _body_proof(root, item, source)
                usable.append(item)
            except (ValueError, TypeError, KeyError, OSError) as error:
                item_rejections.append({"evidence_id": item["evidence_id"], "reason": str(error)})
        timed = select_evidence(usable, start, event_cutoff, allow_historical_reconstruction=True)
        documents = []
        for item in timed["eligible"]:
            documents.append({key: item[key] for key in ("evidence_id", "source_id", "original_url", "raw_locator", "title",
                "published_at", "publication_precision", "first_seen_at", "fetched_at", "content_type", "content_hash", "content_version",
                "original_publisher", "security_associations", "is_background", "historical_reconstruction", "event_id")})
            documents[-1].update(provenance=proofs[item["evidence_id"]],
                body_locator={"raw_locator": item["raw_locator"], "normalized_body_sha256": item["content_hash"]},
                model_use_allowed=sources[item["source_id"]]["registration"]["model_use_allowed"],
                publish_excerpt_allowed=sources[item["source_id"]]["registration"]["publish_excerpt_allowed"])
        # No automatic business/beneficiary conclusion is made from a keyword,
        # a sector label or existence of an evidence ID. Existing citation gate
        # remains the only validator for downstream exact body extracts.
        claim_validation = validate_claims({"claims": []}, timed["eligible"], {symbol: member.get("name")}, {}, cutoff,
                                           strict_counterevidence=True)
        gaps = ["company_main_business_body_not_yet_established", "business_benefit_link_not_yet_established",
                "counterevidence_coverage_unverified", "human_body_claim_review_required"]
        if not documents:
            gaps.insert(0, "verified_explicit_issuer_fulltext_missing")
        if qualified["eligibility_status"] == "pending":
            gaps.append("eligibility_pending_diagnostic_only")
        package = {**evaluation, "status": "blocked" if not documents else "partial_diagnostic",
            "purpose": selection.get("purpose", "production"), "production_eligible": False,
            "search_record": {"method": "local_m3_index_exact_code_name_and_existing_association",
                "network_requests": 0, "index_snapshot_hash": snapshot["content_hash"],
                "query_start": start.isoformat(), "event_cutoff": event_cutoff.isoformat(), "prepared_cutoff": cutoff.isoformat(),
                "scanned_index_rows": snapshot["evidence_count"], "valid_index_rows": len(evidence),
                "searched_code": member["code"], "searched_name": member.get("name"), "matched_evidence_ids": matches,
                "match_count": len(matches), "scope": "existing indexed normalized titles/bodies/associations only; not all websites or announcements"},
            "documents": documents, "rejected_documents": item_rejections + timed["excluded"],
            "facts": [], "inferences": [], "opinions": [], "counterevidence": [], "unknowns": gaps,
            "claim_validation": claim_validation, "main_business": None, "benefit_evidence": None,
            "technical_reference": {"result_hash": technical_report["result_hash"], "security_id": security_id,
                "metric_ids": sorted(tech.get("metrics", {})), "source": "deterministic_frozen_technical_result"},
            "required_permissions": [{"material": "issuer periodic report and dated company announcements",
                "venue": "SZSE/CNINFO" if member["exchange"] == "SZSE" else "SSE/company issuer originals",
                "local_body_use": "permission_required_or_user_supplied_licensed_material",
                "model_upload": "separate_permission_review_required", "network_status": "not_requested"}],
            "coverage_notice": "本地零匹配或缺失正文不代表没有公告、利空或其他风险；行业归属不构成主营或受益证据"}
        packages.append(package)
    counts = Counter(package["status"] for package in packages)
    result = {"schema_version": SCHEMA, "rule_version": RULE_VERSION, "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "target_date": selection["target_date"], "cutoff_at": cutoff.isoformat(),
        "mode": selection["mode"], "purpose": selection.get("purpose", "production"), "production_eligible": False,
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "technical_result_hash": technical_report["result_hash"],
        "eligibility_content_hash": eligibility_report["content_hash"], "index_snapshot": snapshot,
        "index_rejections": rejected, "source_registry_hash": registry["registry_hash"] if registry else None,
        "source_permissions": permissions, "evaluations": evaluations, "packages": packages,
        "counts": {"stock_count": len(evaluations), "material_queue_count": len(packages), "blocked_count": counts["blocked"],
            "partial_diagnostic_count": counts["partial_diagnostic"], "formal_candidate_count": 0, "model_queue_count": 0},
        "model_queue": [], "model_calls": 0, "model_tokens": 0, "network_requests": 0,
        "query_start": start.isoformat(), "event_cutoff": event_cutoff.isoformat(),
        "query_start_basis": "existing DailyConfig target_date-minus-first_query_lookback_days at local midnight; earlier documents explicitly background",
        "window_config": {"first_query_lookback_days": lookback, "daily_config_sha256": hashlib.sha256(_io(daily_path).read_bytes()).hexdigest() if _io(daily_path).exists() else None},
        "status": "no_triggered_objects" if not packages else "blocked" if counts["blocked"] == len(packages) else "partial_diagnostic"}
    result["input_fingerprint"] = digest(result)
    result["content_hash"] = digest(result)
    return result
