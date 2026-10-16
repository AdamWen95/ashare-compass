"""Licensed news-only model context sharing the existing persistent budget."""
from copy import deepcopy
from datetime import datetime
import json

from ..operations.daily import local_path, atomic_json
from ..research.contracts import Evidence
from ..research.evidence import digest, select_evidence
from ..research.model_settings import load_model_settings
from ..research.runner import read_material_bundle
from ..sector_observation_model import run_sector_market_context
from .contracts import SHANGHAI, aware
from .events import validate_events


def extract_context_events(context, original_evidence, *, cutoff, entities):
    annotations = {e["evidence_id"]: e for e in context.get("evidence_catalog", [])}
    materials = [{**row, "is_background": annotations[row["evidence_id"]].get("is_background", True)}
                 for row in original_evidence if row["evidence_id"] in annotations]
    fields = ("claim_id", "claim_type", "text", "citations", "symbol", "metric_ids", "risks", "unknowns")
    claims = [{k:c[k] for k in fields} for c in context.get("analysis", {}).get("accepted_claims", [])]
    return validate_events(claims, materials, cutoff=cutoff, entities=entities)


def run_event_context(project, config, news, prepared, schedule, directory, run_id, seconds, skip_model):
    empty = {"events": [], "model_run": {"call_count": 0, "status": "skipped"}}
    if skip_model or seconds < 1 or not news.get("bundle_file"):
        return empty
    settings = load_model_settings(local_path(project, config.env_file), include_environment=False)
    settings = settings.model_copy(update={"max_retries": 0, "max_calls": min(2, config.model_max_calls_per_run),
        "timeout_seconds": min(config.model_timeout_seconds, seconds), "max_output_tokens": config.model_max_output_tokens or 4096})
    if settings.missing_fields:
        return {**empty, "model_run": {"call_count": 0, "status": "missing_configuration"}}
    original = read_material_bundle(local_path(project, news["bundle_file"]))
    end = min(aware(news["collection_cutoff_at"]), aware(original["cutoff"]), aware(schedule["news_cutoff_at"]))
    # Completion admits receipts obtained during collection, but must not widen
    # the publication window that collection actually queried and validated.
    publication_end = min(aware(original["collection"]["news_cutoff"]), end)
    strict = deepcopy(original)
    # Check records separately: cross-source deduplication must remain after
    # the downstream current/archived source-permission checks.
    strict["evidence"] = [e for e in original["evidence"]
        if aware(e["first_seen_at"]) <= end and aware(e["fetched_at"]) <= end
        and select_evidence([e], news["window"]["start_at"], publication_end,
            allow_historical_reconstruction=True)["eligible"]]
    strict.pop("bundle_hash", None)
    strict["bundle_hash"] = digest(strict)
    strict_path = directory / "context" / "premarket_materials.json"
    atomic_json(strict_path, strict)
    context = run_sector_market_context(evidence_bundle=strict_path, start=news["window"]["start_at"],
        cutoff=end.isoformat(), source_registry=local_path(project, config.sources_config),
        budget_database=project / "data/operations/runtime.sqlite3", run_id=run_id, settings=settings,
        max_calls_per_day=min(6, config.model_max_calls_per_day), max_seconds=seconds,
        context_scope="premarket", allowed_entities=prepared.get("entities", {}))
    checked = extract_context_events(context, strict["evidence"], cutoff=end.isoformat(), entities=prepared.get("entities", {}))
    atomic_json(directory / "context" / "model_context.json", context)
    return {**checked, "model_run": {k:context.get("model_run", {}).get(k) for k in ("status", "call_count", "daily_budget")}}
