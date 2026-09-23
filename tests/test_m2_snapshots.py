"""M2 immutable-input integration tests, exclusively synthetic temporary data.

The fixture calendar explicitly declares its dates; it is a miniature synthetic
exchange calendar, not a weekday approximation or a real market-data claim.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_daily.m2 import run_m2
from ashare_daily.m2_data import canonical_hash
from ashare_daily.quality.baostock import normalize_bars, normalize_calendar, normalize_instrument
from ashare_daily.screening.engine import digest, evaluate_snapshot
from ashare_daily.screening.settings import StrategyConfig
from ashare_daily.screening.snapshots import (
    freeze_input, load_snapshot, read_bundle, read_market_input,
)
from ashare_daily.storage.market import MarketStore


TARGET = date(2026, 9, 8)
FETCHED = datetime(2026, 9, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
SAMPLES = {"sh.000001": "index", "sh.600000": "stock"}


def make_instrument(symbol, security_type):
    return normalize_instrument(
        {"code": symbol, "code_name": "OFFLINE TEST 合成证券", "ipoDate": "1999-01-01",
         "outDate": "", "type": "2" if security_type == "index" else "1", "status": "1"},
        expected_symbol=symbol, expected_type=security_type, fetched_at=FETCHED, sdk_version="0.9.3",
    )


def make_bars(instrument, dates, *, price="10", fetched_at=FETCHED):
    rows = []
    for day in dates:
        row = {"date": day.isoformat(), "code": instrument.symbol,
               "open": price, "high": price, "low": price, "close": price,
               "preclose": price, "volume": "100", "amount": "50000000"}
        if instrument.security_type == "stock":
            row.update(adjustflag="3", tradestatus="1", isST="0")
        rows.append(row)
    return normalize_bars(rows, instrument=instrument, start_date=dates[0], end_date=dates[-1],
                          trading_dates=set(dates), fetched_at=fetched_at, sdk_version="0.9.3")


@pytest.fixture
def small_market(tmp_path, monkeypatch):
    # Exercise the configured sample import, rather than hard-coded algorithm IDs.
    monkeypatch.setattr("ashare_daily.screening.snapshots.SAMPLE_TYPES", dict(SAMPLES))
    store = MarketStore(tmp_path / "synthetic_market.sqlite3")
    first = TARGET - timedelta(days=119)
    dates = [first + timedelta(days=n) for n in range(120)]
    future = TARGET + timedelta(days=1)
    rows = [{"calendar_date": day.isoformat(), "is_trading_day": "1"} for day in [*dates, future]]
    store.store_calendar(normalize_calendar(rows, start_date=first, end_date=future,
                                            fetched_at=FETCHED, sdk_version="0.9.3"))
    instruments = {}
    for symbol, kind in SAMPLES.items():
        instrument = make_instrument(symbol, kind)
        instruments[symbol] = instrument
        store.store_instrument(instrument)
        store.store_bars(make_bars(instrument, dates))
    config = StrategyConfig(strategy_version="offline-snapshot-test-v1", benchmark_id="sh.000001",
                            benchmark_name="OFFLINE TEST 固定基准")
    return store, config, dates, instruments


def offline_inputs(small_market):
    store, config, _, _ = small_market
    inputs = read_market_input(store.path, TARGET, config)
    inputs["verification_kind"] = "offline_test"
    return inputs


def make_bundle(inputs, directory: Path, *, multiplier=Decimal(1), version="synthetic-v1"):
    """Build an explicitly offline bundle with consistent invariant fields."""
    series = {}
    for symbol, kind in inputs["sample_types"].items():
        bars = []
        for raw in inputs["raw_bars"]:
            if raw["symbol"] != symbol:
                continue
            bar = {key: raw[key] for key in ("trade_date", "volume_shares", "amount_cny", "tradestatus", "is_st")}
            bar.update({key: str(Decimal(raw[key]) * multiplier) for key in ("open", "high", "low", "close", "preclose")})
            bar["quality_flags"] = []
            bars.append(bar)
        series[symbol] = {"symbol": symbol, "security_type": kind, "provider": "baostock",
                          "adjustment_mode": "forward_adjusted" if kind == "stock" else "index_native",
                          "price_unit": "CNY" if kind == "stock" else "index_points",
                          "amount_unit": "CNY", "volume_unit": "shares", "fetch_version": version,
                          "fetched_at": FETCHED.isoformat(), "bars": bars, "issues": []}
    bundle = {"schema_version": "m2-adjusted-bundle-v1", "verification_kind": "offline_test",
              "mode": "offline_test", "batch_id": version, "run_directory": str(directory),
              "trading_dates": inputs["trading_dates"], "symbol_types": inputs["sample_types"],
              "status": "ok", "series": series, "failures": []}
    bundle["manifest_hash"] = canonical_hash(bundle)
    return bundle


def rehash_bundle(bundle):
    bundle["manifest_hash"] = canonical_hash({k: v for k, v in bundle.items() if k != "manifest_hash"})


def test_read_only_input_excludes_future_calendar_and_bars(small_market, tmp_path):
    store, config, _, instruments = small_market
    before_bytes = store.path.read_bytes()
    before = read_market_input(store.path, TARGET, config)
    assert store.path.read_bytes() == before_bytes
    for instrument in instruments.values():
        store.store_bars(make_bars(instrument, [TARGET + timedelta(days=1)], price="999999",
                                  fetched_at=FETCHED + timedelta(hours=1)))
    after = read_market_input(store.path, TARGET, config)
    assert before == after
    assert len(after["trading_dates"]) == 120
    assert len(after["raw_bars"]) == 240
    assert all(row["trade_date"] <= TARGET.isoformat() for row in after["raw_bars"])
    assert all(row["calendar_date"] <= TARGET.isoformat() for row in after["calendar_records"])


def test_input_snapshot_rejects_modified_price(small_market, tmp_path):
    inputs = offline_inputs(small_market)
    snapshot, path = freeze_input(inputs, make_bundle(inputs, tmp_path), tmp_path / "frozen")
    snapshot["raw_bars"][0]["close"] = "12345"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="快照哈希不匹配"):
        load_snapshot(path, tmp_path)


def test_adjusted_bundle_rejects_modified_price(small_market, tmp_path):
    bundle = make_bundle(offline_inputs(small_market), tmp_path)
    bundle["series"]["sh.600000"]["bars"][0]["close"] = "12345"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="调整数据包哈希不匹配"):
        read_bundle(path)


def test_inner_bundle_hash_checked_even_with_consistent_outer_hash(small_market, tmp_path):
    inputs = offline_inputs(small_market)
    snapshot, path = freeze_input(inputs, make_bundle(inputs, tmp_path), tmp_path / "frozen")
    snapshot["adjusted_data"]["series"]["sh.600000"]["bars"][0]["close"] = "12345"
    snapshot["snapshot_id"] = "m2-" + digest({key: value for key, value in snapshot.items() if key != "snapshot_id"})
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="调整数据包哈希不匹配"):
        load_snapshot(path, tmp_path)


def test_real_marked_input_rejects_offline_bundle(small_market, tmp_path):
    store, config, _, _ = small_market
    inputs = read_market_input(store.path, TARGET, config)
    assert inputs["verification_kind"] == "local_real_data"
    with pytest.raises(ValueError, match="不能把测试数据包用于真实行情快照"):
        freeze_input(inputs, make_bundle(inputs, tmp_path), tmp_path / "frozen")
    assert not (tmp_path / "frozen").exists()


@pytest.mark.parametrize(("field", "different"), [
    ("tradestatus", False), ("is_st", True), ("volume_shares", 101), ("amount_cny", "50000001"),
])
def test_invariant_raw_adjusted_mismatch_becomes_source_issue(small_market, tmp_path, field, different):
    inputs = offline_inputs(small_market)
    before = deepcopy(inputs)
    bundle = make_bundle(inputs, tmp_path)
    bundle["series"]["sh.600000"]["bars"][-1][field] = different
    rehash_bundle(bundle)
    snapshot, _ = freeze_input(inputs, bundle, tmp_path / "frozen")
    assert inputs == before, "freezing must not mutate the caller's source issue list"
    assert any(field in issue and "不一致" in issue for issue in snapshot["source_issues"]["sh.600000"])
    evaluation = evaluate_snapshot(snapshot)["evaluations"][0]
    assert evaluation["status"] == "data_insufficient"
    assert any(field in issue for issue in evaluation["data_issues"])


def test_equivalent_decimal_text_is_not_false_mismatch(small_market, tmp_path):
    inputs = offline_inputs(small_market)
    bundle = make_bundle(inputs, tmp_path)
    bar = bundle["series"]["sh.600000"]["bars"][-1]
    bar["amount_cny"] = "50000000.0000"
    bar["volume_shares"] = "100.0000"
    rehash_bundle(bundle)
    snapshot, _ = freeze_input(inputs, bundle, tmp_path / "frozen")
    assert not snapshot["source_issues"]


@pytest.mark.parametrize("part", ["trading_dates", "symbol_types"])
def test_bundle_must_match_exact_configured_sample_and_calendar(small_market, tmp_path, part):
    inputs = offline_inputs(small_market)
    bundle = make_bundle(inputs, tmp_path)
    if part == "trading_dates":
        bundle[part] = bundle[part][1:]
    else:
        bundle[part] = {"sh.000001": "index"}
    rehash_bundle(bundle)
    with pytest.raises(ValueError, match="样本/交易日窗口不匹配"):
        freeze_input(inputs, bundle, tmp_path / "frozen")


def test_snapshot_replay_reads_neither_database_nor_network(small_market, tmp_path, monkeypatch):
    inputs = offline_inputs(small_market)
    frozen, path = freeze_input(inputs, make_bundle(inputs, tmp_path), tmp_path / "frozen")
    expected = evaluate_snapshot(frozen)
    original = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot replay attempted a mutable input or network read")

    monkeypatch.setattr("ashare_daily.m2.read_market_input", forbidden)
    monkeypatch.setattr("ashare_daily.m2_data.prepare_adjusted_data", forbidden)
    monkeypatch.setattr("ashare_daily.providers.baostock.BaoStockClient.query", forbidden)
    monkeypatch.setattr("sqlite3.connect", forbidden)
    result = run_m2(snapshot=str(path), output_dir=tmp_path / "replay",
                    database=tmp_path / "does_not_exist.sqlite3", config_path=tmp_path / "no_config.json")
    report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert result["generation_status"] == "ok"
    assert result["verification_kind"] == "offline_test"
    assert "offline_test" in Path(result["run_directory"]).parts
    assert report["evaluations"] == expected["evaluations"]
    assert report["candidates"] == expected["candidates"]
    assert report["result_hash"] == expected["result_hash"]
    assert path.read_bytes() == original
    assert all(Path(result[name]).is_file() for name in ("html", "markdown", "json", "csv"))


def test_new_adjusted_version_preserves_old_snapshot_and_report(small_market, tmp_path):
    inputs = offline_inputs(small_market)
    old_bundle = make_bundle(inputs, tmp_path / "old", version="synthetic-original")
    old_snapshot, old_path = freeze_input(inputs, old_bundle, tmp_path / "frozen")
    result = run_m2(snapshot=str(old_path), output_dir=tmp_path / "reports")
    old_snapshot_bytes = old_path.read_bytes()
    old_report_path = Path(result["json"])
    old_report_bytes = old_report_path.read_bytes()

    revised_bundle = make_bundle(inputs, tmp_path / "new", multiplier=Decimal("1.2"), version="synthetic-revision")
    new_snapshot, new_path = freeze_input(inputs, revised_bundle, tmp_path / "frozen")
    assert new_path != old_path
    assert new_snapshot["adjusted_data"]["manifest_hash"] != old_bundle["manifest_hash"]
    assert evaluate_snapshot(old_snapshot)["evaluations"][0]["adjusted_close"] == "10"
    assert evaluate_snapshot(new_snapshot)["evaluations"][0]["adjusted_close"] == "12.0"
    restored, _ = load_snapshot(old_path, tmp_path)
    assert restored == old_snapshot
    assert old_path.read_bytes() == old_snapshot_bytes
    assert old_report_path.read_bytes() == old_report_bytes


def test_unknown_delisting_state_stays_unknown_in_frozen_real_input(small_market, tmp_path):
    store, config, _, _ = small_market
    inputs = read_market_input(store.path, TARGET, config)
    frozen, _ = freeze_input(inputs, None, tmp_path / "frozen")
    state = frozen["eligibility_states"]["sh.600000"]
    assert state["delisting_period"] is None
    assert state["effective_date"] is None
    assert state["evidence_id"] is None
    evaluation = evaluate_snapshot(frozen)["evaluations"][0]
    condition = next(item for item in evaluation["conditions"] if item["id"] == "not_delisting_period")
    assert condition["status"] == "unknown"
    assert evaluation["status"] == "data_insufficient"
