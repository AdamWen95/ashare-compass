"""The first nonempty industry run must accept a verified empty legacy cache."""
import sqlite3

import pytest

from ashare_daily import sector_history as history
from ashare_daily.storage.market import MarketStore
from test_sector_history import calendar, config, freeze, member, source_factory


def legacy(root, cfg, kind="offline_test"):
    path = root / cfg["database"]
    MarketStore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO market_metadata VALUES('verification_kind',?)", (kind,))
    return path


def test_cold_legacy_history_plan_is_readonly_and_keeps_all_members(tmp_path):
    cfg = config(); calendar(tmp_path, cfg)
    path = legacy(tmp_path, cfg)
    before = path.read_bytes()
    result = history.prepare_history(tmp_path, freeze(cfg, [member(1), member(2)]), cfg, dry_run=True)
    assert len(result["plan"]["tasks"]) == 2
    assert all(not t["raw_refs"] and t["status"] == "pending" for t in result["plan"]["tasks"])
    assert path.read_bytes() == before


def test_first_live_stage_adds_f2_only_after_valid_fixture_history(tmp_path, monkeypatch):
    cfg = config(); calendar(tmp_path, cfg)
    path = legacy(tmp_path, cfg)
    calls = []; source_factory(monkeypatch, calls)
    result = history.prepare_history(tmp_path, freeze(cfg), cfg)
    assert result["metrics"]["requests"] == 2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version,mode FROM f2_schema").fetchall() == [(1, "offline_test")]
        assert dict(db.execute("SELECT key,value FROM market_metadata"))["verification_kind"] == "offline_test"


def test_calendar_gap_produces_report_instead_of_missing_table_error(tmp_path):
    cfg = config(); path = legacy(tmp_path, cfg)
    before = path.read_bytes()
    result = history.prepare_history(tmp_path, freeze(cfg), cfg, max_seconds=0)
    assert result["status"] == "calendar_blocked"
    assert path.read_bytes() == before


def test_legacy_cache_supplies_no_qualification_proofs_without_f2_tables(tmp_path):
    from ashare_daily.sector_eligibility import _cached_bao_proofs
    cfg = {"database": "data/research/market.sqlite3"}
    path = legacy(tmp_path, cfg, kind="live_network")
    before = path.read_bytes()
    selection = {"mode": "research", "members": [member()], "target_date": "2026-09-21"}
    assert _cached_bao_proofs(tmp_path, selection, "2026-09-22T11:00:00+08:00") == []
    assert path.read_bytes() == before


def test_partial_qualification_history_schema_is_not_silently_accepted(tmp_path):
    from ashare_daily.sector_eligibility import _cached_bao_proofs
    cfg = {"database": "data/research/market.sqlite3"}
    path = legacy(tmp_path, cfg, kind="live_network")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE f2_batches(batch_id TEXT)")
    with pytest.raises(ValueError, match="schema"):
        _cached_bao_proofs(tmp_path, {"mode":"research", "members":[member()]}, "2026-09-22T11:00:00+08:00")


@pytest.mark.parametrize("mutation", ["wrong_mode", "unverified", "orphan_f2", "demo"])
def test_cold_cache_rejects_untrusted_or_partial_database(tmp_path, mutation):
    cfg = config(); path = legacy(tmp_path, cfg)
    with sqlite3.connect(path) as db:
        if mutation == "wrong_mode": db.execute("UPDATE market_metadata SET value='demo' WHERE key='mode'")
        if mutation == "unverified": db.execute("DELETE FROM market_metadata WHERE key='verification_kind'")
        if mutation == "orphan_f2": db.execute("CREATE TABLE f2_bar_current(bad TEXT)")
        if mutation == "demo": db.execute("CREATE TABLE demo_prices(value TEXT)")
    with pytest.raises(ValueError):
        history._ReadStore(path, "offline_test", root=tmp_path)
