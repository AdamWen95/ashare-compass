"""Local exchange-calendar evidence reaches history and dated status readers."""
from datetime import date, datetime
import json

import pytest

from ashare_daily.calendar import _digest
from ashare_daily.providers import baostock as bs
from ashare_daily.providers.baostock_f2 import resolve_history_calendar
from ashare_daily.providers import sector_status
from test_sector_status import fixture, write


TARGET = date(2026, 9, 10)


def local_packet(*, mode="offline_test"):
    from ashare_daily.providers.exchange_calendar import build_packet

    return build_packet(TARGET, mode=mode)


class NoQuery(bs.BaoStockClient):
    def query(self, *args, **kwargs):
        pytest.fail("verified local calendar must not query BaoStock")


class FailedQuery(bs.BaoStockClient):
    def __init__(self):
        super().__init__(max_attempts=1)
        self.calls = []

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        return bs.base_result(operation, parameters)


def baostock_packet(*, opened, fetched_at, target=TARGET, mode="offline_test"):
    parameters = {"start_date": target.isoformat(), "end_date": target.isoformat()}
    rows = [{"calendar_date": target.isoformat(), "is_trading_day": "1" if opened else "0"}]
    response = bs.base_result("calendar", parameters)
    response.update(ok=True, status="ok", error_code="0", fields=bs.CALENDAR_FIELDS,
        rows=rows, fetched_at=fetched_at, raw_hash=bs.raw_hash(bs.CALENDAR_FIELDS, rows),
        login={"ok": True, "error_code": "0", "error_msg": "success"})
    packet = {"schema_version": "f1-calendar-cache-v1", "provider": "baostock", "mode": mode,
        "first_seen_at": fetched_at, "response": response}
    packet["content_hash"] = _digest(packet)
    return packet


@pytest.mark.parametrize("history_days", [21, 320])
def test_local_calendar_supplies_exact_history_window_without_network_or_writes(tmp_path, history_days):
    path = tmp_path / "f1/local.json"
    write(path, local_packet())
    before = path.read_bytes()
    result = resolve_history_calendar(TARGET, tmp_path / "f2", history_days=history_days,
        additional_cache_roots=(path.parent,), mode="offline_test", client=NoQuery())
    assert result["verified"] is True
    assert len(result["trading_dates"]) == history_days
    assert result["trading_dates"][-1] == TARGET.isoformat()
    assert result["responses"] == []
    assert result["verification_kind"] == "offline_test"
    assert result["source_metadata"][0]["provider"] == "exchange_calendars"
    assert result["source_metadata"][0]["path"] == str(path)
    assert path.read_bytes() == before
    assert not (tmp_path / "f2").exists()


@pytest.mark.parametrize("local_first", [False, True])
def test_conflicting_providers_block_even_when_baostock_is_newer(tmp_path, local_first):
    packet = local_packet()
    write(tmp_path / ("00-local.json" if local_first else "99-local.json"), packet)
    write(tmp_path / ("99-baostock.json" if local_first else "00-baostock.json"),
        baostock_packet(opened=False, fetched_at=datetime.now(bs.SHANGHAI).isoformat()))
    result = resolve_history_calendar(TARGET, tmp_path, history_days=21,
        mode="offline_test", client=NoQuery())
    assert result["verified"] is False and result["trading_dates"] == []
    assert "冲突" in result["validation_error"]
    assert result["responses"] == []


def test_matching_providers_keep_both_actual_source_identities(tmp_path):
    write(tmp_path / "local.json", local_packet())
    write(tmp_path / "baostock.json", baostock_packet(opened=True, fetched_at="2026-09-10T21:00:00+08:00"))
    result = resolve_history_calendar(TARGET, tmp_path, history_days=21,
        mode="offline_test", client=NoQuery())
    assert result["verified"] is True
    assert {item["provider"] for item in result["source_metadata"]} == {"exchange_calendars", "baostock"}


@pytest.mark.parametrize("sources,expected", [
    ("local", "versioned_local_calendar"),
    ("mixed", "mixed_calendar_evidence"),
    ("baostock", "live_network"),
    ("local_with_unused_baostock", "versioned_local_calendar"),
    ("mixed_closed_date", "mixed_calendar_evidence"),
])
def test_research_provenance_describes_evidence_inside_the_selected_window(tmp_path, sources, expected):
    if sources != "baostock":
        write(tmp_path / "local.json", local_packet(mode="research"))
    if sources != "local":
        source_date = (date(2024, 1, 2) if sources == "local_with_unused_baostock"
            else date(2026, 9, 6) if sources == "mixed_closed_date" else TARGET)
        write(tmp_path / "baostock.json", baostock_packet(opened=sources != "mixed_closed_date",
            target=source_date, mode="research", fetched_at="2026-09-10T21:00:00+08:00"))
    result = resolve_history_calendar(TARGET, tmp_path, history_days=1 if sources == "baostock" else 21,
        mode="research", client=NoQuery())
    assert result["verified"] is True
    assert result["responses"] == []
    assert result["verification_kind"] == expected
    if sources == "local_with_unused_baostock":
        assert {row["provider"] for row in result["source_metadata"]} == {"exchange_calendars"}
    elif sources == "mixed_closed_date":
        assert {row["provider"] for row in result["source_metadata"]} == {"exchange_calendars", "baostock"}


def test_old_baostock_cache_keeps_same_provider_revision_order(tmp_path):
    write(tmp_path / "zzz-older.json", baostock_packet(opened=False, fetched_at="2026-09-09T21:00:00+08:00"))
    write(tmp_path / "aaa-newer.json", baostock_packet(opened=True, fetched_at="2026-09-10T21:00:00+08:00"))
    result = resolve_history_calendar(TARGET, tmp_path, history_days=1,
        mode="offline_test", client=NoQuery())
    assert result["verified"] is True
    assert result["trading_dates"] == [TARGET.isoformat()]
    assert {item["provider"] for item in result["source_metadata"]} == {"baostock"}


@pytest.mark.parametrize("invalid", ["mode", "hash"])
def test_invalid_local_packet_never_supplies_history(tmp_path, invalid):
    packet = local_packet()
    if invalid == "mode":
        packet["mode"] = "research"
        packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
    else:
        packet["content_hash"] = "0" * 64
    path = tmp_path / "local.json"
    write(path, packet)
    client = FailedQuery()
    result = resolve_history_calendar(TARGET, tmp_path, history_days=21,
        mode="offline_test", client=client)
    assert result["verified"] is False and result["trading_dates"] == []
    assert result["cache_rejections"][0]["path"] == str(path)
    assert len(client.calls) == 1


def test_local_calendar_does_not_extend_coverage_to_another_year(tmp_path):
    write(tmp_path / "local.json", local_packet())
    client = FailedQuery()
    result = resolve_history_calendar(date(2027, 1, 4), tmp_path, history_days=21,
        mode="offline_test", client=client)
    assert result["verified"] is False and result["trading_dates"] == []
    assert result["resolved_trade_date"] is None
    assert len(client.calls) == 1


def install_status_calendar(fixture):
    from ashare_daily.calendar import verify_calendar_packet

    _, _, _, folder, manifest, _ = fixture
    packet = local_packet()
    metadata, _ = verify_calendar_packet(packet, mode="offline_test")
    path = folder / "calendar.json"
    manifest["calendar"].update(source_path=str(path), source_file_hash=write(path, packet),
        source_raw_hash=metadata["raw_hash"])
    write(folder / "manifest.json", manifest)
    return path


def test_dated_halt_evidence_accepts_verified_local_calendar_reference(fixture, monkeypatch):
    install_status_calendar(fixture)
    root, universe, quotes, folder, _, _ = fixture
    monkeypatch.setattr(bs.BaoStockClient, "query", lambda *args, **kwargs: pytest.fail("network attempted"))
    result = sector_status.apply_status_evidence(root, universe, quotes, TARGET.isoformat(), folder / "result.json")
    assert all(row["full_day_halt_evidence"]["verified"] is True for row in result["rows"])
    assert all(row["tradestatus"] is None for row in result["rows"])
    assert quotes["rows"][0].get("full_day_halt_evidence") is None


@pytest.mark.parametrize("invalid", ["file_hash", "source_hash", "mode", "packet_hash"])
def test_dated_halt_rejects_tampered_local_calendar(fixture, invalid):
    path = install_status_calendar(fixture)
    root, universe, quotes, folder, manifest, _ = fixture
    if invalid == "file_hash":
        path.write_text("{}", encoding="utf-8")
    elif invalid == "source_hash":
        manifest["calendar"]["source_raw_hash"] = "0" * 64
    else:
        packet = json.loads(path.read_text(encoding="utf-8"))
        if invalid == "mode":
            packet["mode"] = "research"
            packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
        else:
            packet["content_hash"] = "0" * 64
        manifest["calendar"]["source_file_hash"] = write(path, packet)
    write(folder / "manifest.json", manifest)
    with pytest.raises(ValueError):
        sector_status.apply_status_evidence(root, universe, quotes, TARGET.isoformat(), folder / "result.json")
