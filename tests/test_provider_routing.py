"""Offline policy contracts; synthetic responses never enter research storage."""
from copy import deepcopy

import pytest

from ashare_daily.providers.base import DailyBarRequest, MarketDataProvider, ProviderResult, SecurityIdentity
from ashare_daily.providers.routing import ProviderRouter


def request(adjustment="unadjusted"):
    identity = SecurityIdentity("source-backed-test-id", "600000", "SSE", "sse_main", metadata_verified=True)
    return DailyBarRequest(identity, "2026-09-09", "2026-09-10", ("2026-09-09", "2026-09-10"), adjustment)


def result(provider, *, ok=True, complete=True, error="0", records=None, status=None):
    return ProviderResult(provider, "daily_bars", status or ("success" if complete and ok else "partial" if ok else "timeout"),
        {"provider": provider, "ok": ok, "error_code": error, "error_msg": "isolated fixture", "verification_kind": "offline_test"},
        records=deepcopy(records if records is not None else [{"provider": provider, "trade_date": "2026-09-10"}] if ok else []),
        quality={"quote_complete": complete and ok, "missing_dates": [] if complete else ["2026-09-10"]},
        metrics={"requests": 1, "retries": 0, "verification_kind": "offline_test"})


class Provider(MarketDataProvider):
    def __init__(self, name, response):
        self.name, self.response, self.calls = name, response, []

    def fetch_daily_bars(self, request):
        self.calls.append(request)
        return deepcopy(self.response)


@pytest.mark.parametrize("exchange,board,code,bao,em", [
    ("SSE", "sse_main", "600519", "sh.600519", "1.600519"),
    ("SSE", "star", "688981", "sh.688981", "1.688981"),
    ("SZSE", "szse_main", "000001", "sz.000001", "0.000001"),
    ("SZSE", "chinext", "300750", "sz.300750", "0.300750"),
])
def test_source_verified_exchange_mapping(exchange, board, code, bao, em):
    identity = SecurityIdentity("id", code, exchange, board, metadata_verified=True)
    assert identity.symbol == bao and identity.source_symbol("eastmoney") == em


@pytest.mark.parametrize("fields", [
    {"exchange": "BSE", "board": "bse"}, {"exchange": "SSE", "board": "chinext"},
    {"metadata_verified": False}, {"security_type": "cdr"}, {"security_type": "index"},
    {"security_type": "fund"}, {"security_type": "b_share"}, {"code": "６０００００"},
])
def test_non_target_or_unverified_identity_is_rejected(fields):
    values = dict(security_id="id", code="600000", exchange="SSE", board="sse_main", metadata_verified=True)
    with pytest.raises(ValueError):
        SecurityIdentity(**(values | fields))


def test_explicit_adjustment_and_ordered_verified_dates_required():
    good = request()
    with pytest.raises(ValueError):
        DailyBarRequest(good.identity, good.start_date, good.end_date, (), "unadjusted")
    with pytest.raises(ValueError):
        DailyBarRequest(good.identity, good.start_date, good.end_date, good.expected_dates, "default")
    with pytest.raises(ValueError):
        DailyBarRequest(good.identity, good.start_date, good.end_date, tuple(reversed(good.expected_dates)))


def test_valid_primary_does_not_contact_backup():
    bao, em = Provider("baostock", result("baostock")), Provider("eastmoney", result("eastmoney"))
    routed = ProviderRouter([bao, em]).fetch_daily_bars(request())
    assert routed.selected.provider == "baostock" and not em.calls
    assert routed.metrics()["requests"] == 1
    assert routed.selected.provenance()["verification_status"] == "sample_verified"


@pytest.mark.parametrize("adjustment", ["unadjusted", "forward_adjusted"])
@pytest.mark.parametrize("failure", ["network", "incomplete"])
def test_fallback_retains_failure_and_replaces_whole_response(adjustment, failure):
    primary = result("baostock", ok=failure != "network", complete=False, error="10002007")
    bao, em = Provider("baostock", primary), Provider("eastmoney", result("eastmoney"))
    req = request(adjustment)
    routed = ProviderRouter([bao, em]).fetch_daily_bars(req)
    assert routed.selected.provider == "eastmoney" and routed.selected.fallback_level == 1
    assert bao.calls == em.calls == [req]
    assert routed.candidates[0].response["error_code"] == "10002007"
    assert all(row["provider"] == "eastmoney" for row in routed.selected.records)
    assert routed.metrics()["requests"] == 2 and routed.metrics()["fallback_verified"] == 1
    assert routed.events[0]["fallback_to"] == "eastmoney"


def test_both_sources_fail_without_fabricated_data():
    bao = Provider("baostock", result("baostock", ok=False, error="10002007"))
    em = Provider("eastmoney", result("eastmoney", ok=False, error="network_exception"))
    routed = ProviderRouter([bao, em]).fetch_daily_bars(request())
    assert not routed.selected.usable and not routed.selected.records
    assert len(routed.candidates) == 2 and routed.events[-1]["event"] == "provider_chain_incomplete"
    assert routed.metrics()["fallback_verified"] == 0


def test_partial_backup_facts_are_retained_without_promoting_quality():
    bao = Provider("baostock", result("baostock", ok=False))
    em = Provider("eastmoney", result("eastmoney", complete=False))
    routed = ProviderRouter([bao, em]).fetch_daily_bars(request())
    assert routed.selected.provider == "eastmoney" and routed.selected.has_facts
    assert not routed.selected.usable
    assert routed.metrics()["fallback_selected"] == 1 and routed.metrics()["fallback_verified"] == 0


def test_circuit_stops_primary_after_three_failures_and_continues_authorized_backup():
    bao = Provider("baostock", result("baostock", ok=False))
    em = Provider("eastmoney", result("eastmoney"))
    router = ProviderRouter([bao, em])
    outputs = [router.fetch_daily_bars(request()) for _ in range(6)]
    assert len(bao.calls) == 3 and len(em.calls) == 6
    assert outputs[-1].candidates[0].status == "circuit_open"
    assert outputs[-1].metrics()["requests"] == 1
    assert outputs[-1].candidates[0].response["network_sent"] is False


@pytest.mark.parametrize("stop", ["permission_denied", "rate_limited", "permission_required"])
def test_access_stop_latches_for_provider_and_preserves_reason(stop):
    blocked = result("baostock", ok=False, status=stop)
    bao, em = Provider("baostock", blocked), Provider("eastmoney", result("eastmoney"))
    router = ProviderRouter([bao, em])
    router.fetch_daily_bars(request())
    second = router.fetch_daily_bars(request())
    assert len(bao.calls) == 1 and len(em.calls) == 2
    assert second.candidates[0].response["stop_evidence"]["status"] == stop


def test_programming_exception_is_not_hidden_as_empty_market():
    class Broken(Provider):
        def fetch_daily_bars(self, request):
            raise RuntimeError("implementation defect")
    em = Provider("eastmoney", result("eastmoney"))
    with pytest.raises(RuntimeError, match="implementation defect"):
        ProviderRouter([Broken("baostock", {}), em]).fetch_daily_bars(request())
    assert not em.calls


def test_provider_cannot_report_another_source_identity():
    with pytest.raises(ValueError, match="identity"):
        ProviderRouter([Provider("baostock", result("eastmoney"))]).fetch_daily_bars(request())
