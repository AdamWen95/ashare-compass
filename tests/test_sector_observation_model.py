"""Offline licensed-news context; socket access is blocked by conftest."""
from datetime import datetime
import json
from pathlib import Path

import pytest

from ashare_daily.research.evidence import build_evidence, digest
from ashare_daily.research.model import ChatCompletionsModel, HTTPResponse
from ashare_daily.research.model_settings import ModelSettings
from ashare_daily.research.runner import archive_materials
from ashare_daily.sector_observation_model import run_sector_market_context


START = "2025-04-27T00:00:00+08:00"
CUTOFF = "2025-04-30T23:59:59+08:00"
FACT = "有关部门开展公开征求意见。"
BODY = "OFFLINE TEST 合成新闻背景材料，不是真实证券研究。" + FACT * 10


@pytest.fixture
def inputs(tmp_path):
    source = {"source_id": "offline_news", "name": "OFFLINE TEST", "category": "news",
              "source_url": "https://context.example.invalid", "access_method": "offline_test",
              "usage_limits": "Synthetic fixture only", "enabled": True,
              "content_access": ["fulltext", "abstract", "metadata_only"],
              "model_use_allowed": True, "publish_excerpt_allowed": True, "cache_allowed": True,
              "permission_basis": "Self-authored synthetic fixture",
              "checked_at": "2025-04-25T10:00:00+08:00"}
    evidence = build_evidence(source_id=source["source_id"], category="news",
        original_url="https://context.example.invalid/a", raw_locator="OFFLINE paragraph 1",
        title="OFFLINE 新闻资料", published_at="2025-04-28T10:00:00+08:00",
        publication_precision="datetime", first_seen_at="2025-05-01T12:00:00+08:00",
        fetched_at="2025-05-01T12:00:00+08:00", content_type="fulltext", content=BODY,
        original_publisher="OFFLINE TEST", acquisition_mode="offline_test")
    archive = archive_materials(collection={"sources": [source], "evidence": [evidence], "source_health": []},
        database=tmp_path / "offline.sqlite3", output_dir=tmp_path, start=START, cutoff=CUTOFF, offline=True)
    registry = {"schema_version": "m3-source-registry-v1", "sources": [
        {"registration": source, "adapter": "registered_html", "pages": []}]}
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    return {"bundle": Path(archive["bundle_file"]), "registry": path,
            "source": source, "evidence": evidence, "budget": tmp_path / "runtime.sqlite3"}


def output(evidence, **changes):
    claim = {"claim_id": "background-1", "claim_type": "fact", "text": FACT,
             "citations": [{"evidence_id": evidence["evidence_id"], "quote": FACT,
                            "locator": evidence["raw_locator"]}],
             "symbol": None, "metric_ids": [], "risks": [], "unknowns": []}
    claim.update(changes)
    return {"claims": [claim]}


def fake_client(value, *, responses=None, **settings):
    configured = ModelSettings(base_url="https://context.example.invalid/v1", api_key="OFFLINE_CONTEXT_SECRET",
                               model_name="offline", max_retries=0, **settings)
    seen = []
    queued = list(responses or [])
    def transport(url, headers, payload, timeout):
        seen.append(payload)
        response = queued.pop(0) if queued else json.dumps(value, ensure_ascii=False)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, HTTPResponse):
            return response
        return HTTPResponse(200, json.dumps({"choices": [{"message": {"content": response}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode(), {})
    return ChatCompletionsModel(configured, transport=transport, sleep=lambda _: None), seen


def run(inputs, **changes):
    parameters = {"evidence_bundle": inputs["bundle"], "source_registry": inputs["registry"],
                  "budget_database": inputs["budget"], "run_id": "offline-context", "start": START,
                  "cutoff": CUTOFF, "offline": True}
    parameters.update(changes)
    return run_sector_market_context(**parameters)


def rewrite_bundle(inputs, change):
    data = json.loads(inputs["bundle"].read_text(encoding="utf-8"))
    change(data)
    data["bundle_hash"] = digest({key: value for key, value in data.items() if key != "bundle_hash"})
    inputs["bundle"].write_text(json.dumps(data), encoding="utf-8")


def test_only_frozen_news_is_sent_no_market_or_stock_inputs(inputs):
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client, start=datetime.fromisoformat(START), cutoff=datetime.fromisoformat(CUTOFF))
    assert result["status"] == "ok"
    assert result["analysis"]["accepted_count"] == 1
    assert result["market_data_sent_to_model"] is False
    assert result["coverage"]["historical_reconstruction"] is True
    payload = json.loads(seen[0]["messages"][1]["content"])
    assert set(payload) == {"prompt_version", "max_claims", "untrusted_evidence", "output_schema"}
    assert payload["untrusted_evidence"][0]["content"] == BODY
    assert payload["untrusted_evidence"][0]["security_associations"] == []
    assert "OFFLINE_CONTEXT_SECRET" not in json.dumps(result)
    assert result["model_run"]["daily_budget"]["reserved_attempts"] == 1
    snapshot = result["input_snapshot"]
    assert snapshot["snapshot_id"] == "sector-context-" + digest({k: v for k, v in snapshot.items() if k != "snapshot_id"})


@pytest.mark.parametrize("field", ["enabled", "cache_allowed", "model_use_allowed", "publish_excerpt_allowed"])
@pytest.mark.parametrize("where", ["current", "archive"])
def test_current_and_archive_permissions_must_both_allow_export(inputs, field, where):
    if where == "archive":
        rewrite_bundle(inputs, lambda data: data["sources"][0].update({field: False}))
    else:
        registry = json.loads(inputs["registry"].read_text(encoding="utf-8"))
        registry["sources"][0]["registration"][field] = False
        if field == "cache_allowed":
            registry["sources"][0]["registration"]["enabled"] = False
        inputs["registry"].write_text(json.dumps(registry), encoding="utf-8")
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client)
    assert result["status"] == "no_eligible_evidence"
    assert not seen
    assert not result["evidence_catalog"]
    assert "有关部门" not in json.dumps(result["input_snapshot"], ensure_ascii=False)


def test_nonregistered_source_and_wrong_host_cannot_export(inputs):
    rewrite_bundle(inputs, lambda data: data["sources"][0].update(source_url="https://other.example.invalid"))
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client)
    assert result["status"] == "no_eligible_evidence" and not seen


def test_material_hash_tampering_never_reaches_model(inputs):
    inputs["bundle"].write_text("{}", encoding="utf-8")
    client, seen = fake_client({"claims": []})
    result = run(inputs, client=client)
    assert result["status"] == "invalid_material_bundle" and not seen


@pytest.mark.parametrize("changes", [
    {"symbol": "sz.000532"},
    {"metric_ids": ["sz.000532.ma_short"]},
    {"text": "某公司主营高科技且业绩增长"},
    {"text": "收盘价123元"},
    {"text": "立即买入"},
    {"citations": [{"evidence_id": "fake", "quote": FACT, "locator": "fake"}]},
])
def test_company_claim_market_number_instruction_and_fabricated_citation_rejected(inputs, changes):
    client, _ = fake_client(output(inputs["evidence"], **changes))
    result = run(inputs, client=client)
    assert result["status"] == "evidence_validation_failed"
    assert result["analysis"]["accepted_count"] == 0


def test_shared_ledger_stops_seventh_attempt_across_fresh_runs(inputs):
    for index in range(6):
        client, _ = fake_client(output(inputs["evidence"]))
        assert run(inputs, client=client, run_id=f"offline-run-{index}")["status"] == "ok"
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client)
    assert result["status"] == "budget_exhausted" and not seen
    assert result["model_run"]["daily_budget"]["reserved_attempts"] == 6


def test_stricter_daily_configuration_stops_at_its_limit(inputs):
    for index in range(2):
        client, _ = fake_client(output(inputs["evidence"]))
        assert run(inputs, client=client, max_calls_per_day=2, run_id=f"restricted-{index}")["status"] == "ok"
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client, max_calls_per_day=2)
    assert result["status"] == "budget_exhausted" and not seen
    assert result["model_run"]["daily_budget"]["reserved_attempts"] == 2


@pytest.mark.parametrize("limit", [True, 0, 7, 2.0])
def test_daily_budget_cannot_be_disabled_or_increased(inputs, limit):
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client, max_calls_per_day=limit)
    assert result["status"] == "invalid_budget_configuration" and not seen
    assert not inputs["budget"].exists()


def test_stricter_per_run_settings_not_increased_for_format_repair(inputs):
    client, seen = fake_client(output(inputs["evidence"]), responses=["invalid"], max_calls=1)
    result = run(inputs, client=client)
    assert result["status"] == "budget_exhausted"
    assert len(seen) == result["model_run"]["daily_budget"]["reserved_attempts"] == 1


def test_format_repair_counts_toward_two_attempt_run_limit(inputs):
    client, seen = fake_client(output(inputs["evidence"]), responses=["invalid", "invalid again"], max_calls=10)
    result = run(inputs, client=client)
    assert result["status"] == "invalid_json"
    assert len(seen) == 2
    assert result["model_run"]["call_count"] == 2
    assert result["model_run"]["daily_budget"]["reserved_attempts"] == 2


def test_timeout_retry_and_format_repair_cannot_make_third_request(inputs):
    client, seen = fake_client(output(inputs["evidence"]), responses=[TimeoutError(), "invalid"])
    client.settings = client.settings.model_copy(update={"max_retries": 2, "max_calls": 20})
    result = run(inputs, client=client)
    assert result["status"] == "budget_exhausted"
    assert len(seen) == result["model_run"]["daily_budget"]["reserved_attempts"] == 2


@pytest.mark.parametrize("failure,status", [
    (TimeoutError("sensitive exception detail"), "timeout"),
    (HTTPResponse(403, b"OFFLINE_CONTEXT_SECRET", {}), "permission_denied"),
])
def test_failure_returns_empty_analysis_and_sanitized_status(inputs, failure, status):
    client, _ = fake_client({}, responses=[failure])
    result = run(inputs, client=client)
    assert result["status"] == status
    assert result["analysis"]["accepted_claims"] == []
    assert "OFFLINE_CONTEXT_SECRET" not in json.dumps(result)
    assert "sensitive exception" not in json.dumps(result)


def test_offline_inputs_cannot_start_real_model(inputs):
    result = run(inputs, settings=ModelSettings())
    assert result["status"] == "invalid_execution_mode"
    assert result["model_run"]["call_count"] == 0
    assert not inputs["budget"].exists()


def test_offline_client_cannot_be_used_with_real_evidence(inputs):
    rewrite_bundle(inputs, lambda data: data.update(verification_kind="real_materials", evidence=[
        {**item, "acquisition_mode": "automatic"} for item in data["evidence"]]))
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client, offline=False)
    assert result["status"] == "invalid_execution_mode" and not seen


def test_missing_bundle_and_skip_are_zero_call_local_fallbacks(inputs):
    client, seen = fake_client({})
    assert run(inputs, evidence_bundle=None, client=client)["status"] == "no_eligible_evidence"
    assert run(inputs, skip_model=True, client=client)["status"] == "skipped"
    assert not seen and not inputs["budget"].exists()


def test_small_prompt_budget_never_silently_sends_partial_uncounted_input(inputs):
    client, seen = fake_client(output(inputs["evidence"]), max_input_chars=256)
    result = run(inputs, client=client)
    assert result["status"] == "no_eligible_evidence" and not seen
    assert result["coverage"]["input_exclusions"][0]["reason"] == "complete_prompt_input_limit"


def test_invalid_naive_interval_is_fixed_error(inputs):
    client, seen = fake_client({})
    result = run(inputs, client=client, cutoff="2025-04-30T21:00:00")
    assert result["status"] == "invalid_interval"
    assert result["errors"] == ["invalid_interval"] and not seen


class RuntimeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("limit", [0, .5])
def test_exhausted_runtime_budget_skips_without_attempt(inputs, limit):
    client, seen = fake_client(output(inputs["evidence"]))
    result = run(inputs, client=client, max_seconds=limit)
    assert result["status"] == "skipped_runtime_limit"
    assert not seen and not inputs["budget"].exists()


def test_model_format_repair_shares_total_timeout_budget(inputs, monkeypatch):
    import ashare_daily.sector_observation_model as context_module
    clock = RuntimeClock()
    monkeypatch.setattr(context_module.time, "monotonic", clock.monotonic)
    client, seen = fake_client(output(inputs["evidence"]), responses=["invalid"])
    original = client.transport
    timeouts = []
    def transport(url, headers, payload, timeout):
        timeouts.append(timeout)
        clock.now += timeout
        return original(url, headers, payload, timeout)
    client.transport = transport
    result = run(inputs, client=client, max_seconds=5)
    assert result["status"] == "ok" and len(seen) == 2
    assert sum(timeouts) == pytest.approx(5)
    assert result["elapsed_seconds"] == pytest.approx(5)


def test_deadline_before_format_repair_does_not_reserve_or_request_again(inputs, monkeypatch):
    import ashare_daily.sector_observation_model as context_module
    clock = RuntimeClock()
    monkeypatch.setattr(context_module.time, "monotonic", clock.monotonic)
    client, seen = fake_client(output(inputs["evidence"]), responses=["invalid"])
    original = client.transport
    def transport(*args):
        clock.now += 5
        return original(*args)
    client.transport = transport
    result = run(inputs, client=client, max_seconds=5)
    assert result["status"] == "skipped_runtime_limit" and len(seen) == 1
    assert result["model_run"]["daily_budget"]["reserved_attempts"] == 1
    assert result["model_run"]["stopped_reason"] == "runtime_limit"


def test_rate_limit_wait_and_network_timeouts_share_deadline(inputs, monkeypatch):
    import ashare_daily.sector_observation_model as context_module
    clock = RuntimeClock()
    monkeypatch.setattr(context_module.time, "monotonic", clock.monotonic)
    client, seen = fake_client(output(inputs["evidence"]), responses=[HTTPResponse(429, b"{}", {"retry-after": "5"})])
    client.settings = client.settings.model_copy(update={"max_retries": 1})
    client.sleep = clock.sleep
    original = client.transport
    timeouts = []
    def transport(url, headers, payload, timeout):
        timeouts.append(timeout)
        clock.now += timeout
        return original(url, headers, payload, timeout)
    client.transport = transport
    result = run(inputs, client=client, max_seconds=10)
    assert result["status"] == "ok" and len(seen) == 2
    assert clock.sleeps == [5]
    assert sum(timeouts) + sum(clock.sleeps) == pytest.approx(10)


def test_retry_sleep_not_started_if_source_consumed_deadline(inputs, monkeypatch):
    import ashare_daily.sector_observation_model as context_module
    clock = RuntimeClock()
    monkeypatch.setattr(context_module.time, "monotonic", clock.monotonic)
    client, seen = fake_client({}, responses=[HTTPResponse(429, b"{}", {"retry-after": "5"})])
    client.settings = client.settings.model_copy(update={"max_retries": 1})
    client.sleep = clock.sleep
    original = client.transport
    def transport(*args):
        clock.now += 6
        return original(*args)
    client.transport = transport
    result = run(inputs, client=client, max_seconds=10)
    assert result["status"] == "skipped_runtime_limit" and len(seen) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("limit", [True, -1, float("inf"), float("nan"), 14401])
def test_invalid_runtime_limit_is_fixed_zero_call_error(inputs, limit):
    client, seen = fake_client({})
    result = run(inputs, client=client, max_seconds=limit)
    assert result["status"] == "invalid_runtime_configuration" and not seen
