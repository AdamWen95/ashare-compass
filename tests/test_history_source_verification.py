"""Deduplicate repeated source reads without weakening frozen fact checks."""
import json
from pathlib import Path
import sqlite3

import pytest

from ashare_daily import sector_history as history
from test_sector_history import DAYS, calendar, config, freeze, member, source_factory


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    cfg = config()
    calendar(tmp_path, cfg)
    source_factory(monkeypatch, [])
    selection = freeze(cfg)
    history.prepare_history(tmp_path, selection, cfg)
    database = tmp_path / cfg["database"]
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        batch = dict(db.execute("SELECT * FROM f2_batches WHERE adjustment_mode='unadjusted'").fetchone())
    return tmp_path, cfg, selection, database, batch


def test_many_frozen_dates_verify_shared_source_once(prepared, monkeypatch):
    root, cfg, selection, _, batch = prepared
    source = Path(batch["source_response_path"])
    original = Path.read_bytes
    reads = []
    def counted(path):
        if path.samefile(history._io(source)):
            reads.append(path)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", counted)
    packet = history.screening_history_inputs(root, selection, cfg)
    rows = packet["securities"][member()["security_id"]]["raw_records"]
    assert [row["trade_date"] for row in rows] == list(DAYS)
    assert len(reads) == 1
    assert {"path": str(source), "sha256": batch["source_file_hash"]} in packet["file_refs"]


def _split_second_observation(database, batch, **changes):
    """Create an inconsistent second observation only in an offline test DB."""
    replacement = {**batch, "batch_id": batch["batch_id"] + "-second", **changes}
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO f2_batches (" + ",".join(replacement) + ") VALUES (" +
            ",".join("?" for _ in replacement) + ")", list(replacement.values()))
        db.execute("UPDATE f2_bar_observations SET batch_id=? WHERE batch_id=? AND trade_date=?",
            (replacement["batch_id"], batch["batch_id"], DAYS[1]))


def test_shared_source_with_conflicting_hashes_is_rejected(prepared):
    root, cfg, selection, database, batch = prepared
    _split_second_observation(database, batch, source_file_hash="0" * 64)
    with pytest.raises(ValueError, match="conflicting frozen hashes"):
        history.screening_history_inputs(root, selection, cfg)


def test_distinct_source_paths_with_same_hash_are_each_verified(prepared, monkeypatch):
    root, cfg, selection, database, batch = prepared
    source = Path(batch["source_response_path"])
    duplicate = source.with_name("second-" + source.name)
    history._io(duplicate).write_bytes(history._io(source).read_bytes())
    _split_second_observation(database, batch, source_response_path=str(duplicate))
    original = Path.read_bytes
    reads = {source: 0, duplicate: 0}
    def counted(path):
        for target in reads:
            if path.samefile(history._io(target)):
                reads[target] += 1
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", counted)
    packet = history.screening_history_inputs(root, selection, cfg)
    assert list(reads.values()) == [1, 1]
    assert {str(source), str(duplicate)} <= {ref["path"] for ref in packet["file_refs"]}


def test_shared_source_bytes_are_still_verified_each_export(prepared):
    root, cfg, selection, _, batch = prepared
    history.screening_history_inputs(root, selection, cfg)
    with history._io(Path(batch["source_response_path"])).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="source file hash differs"):
        history.screening_history_inputs(root, selection, cfg)


def test_every_selected_observation_keeps_its_provenance_check(prepared):
    root, cfg, selection, database, batch = prepared
    provenance = json.loads(batch["provenance_json"])
    provenance["mode"] = "research"
    _split_second_observation(database, batch, provenance_json=json.dumps(provenance))
    with pytest.raises(ValueError, match="raw source provenance differs"):
        history.screening_history_inputs(root, selection, cfg)


def test_shared_file_does_not_replace_missing_fact_observation(prepared):
    root, cfg, selection, database, batch = prepared
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM f2_bar_observations WHERE batch_id=? AND trade_date=?", (batch["batch_id"], DAYS[1]))
    with pytest.raises(ValueError, match="frozen raw observation reference missing"):
        history.screening_history_inputs(root, selection, cfg)
