"""Per-call relocation reuse keeps full source integrity and hash conflicts."""
from hashlib import sha256
from pathlib import Path
import pytest
from ashare_daily.sector_pipeline import verify_http_evidence


def test_many_shared_membership_refs_resolve_once_per_frozen_file(tmp_path, monkeypatch):
    paths = [tmp_path / "offline-one.json", tmp_path / "offline-two.json"]
    for path in paths:
        path.write_bytes(b'{"mode":"offline_test"}')
    refs = [{"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()} for path in paths]
    calls = []
    def resolve(value, *, anchor):
        calls.append(value)
        return Path(value)
    monkeypatch.setattr("ashare_daily.operations.paths.resolve_archived_path", resolve)
    assert verify_http_evidence(tmp_path, [{"file_refs": refs} for _ in range(1019)]) == 2
    assert calls == [str(path) for path in paths]
    # Reuse ends with this call; later content mutation is never accepted.
    paths[0].write_bytes(b'{"mode":"tampered"}')
    with pytest.raises(ValueError, match="file_hash_mismatch"):
        verify_http_evidence(tmp_path, [{"file_refs": refs}])


def test_repeated_source_with_conflicting_expected_hash_remains_blocked(tmp_path):
    path = tmp_path / "offline.json"
    path.write_bytes(b"offline")
    first = {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match="source_file_reference_hash_conflict"):
        verify_http_evidence(tmp_path, [{"file_refs": [first]}, {"file_refs": [{**first, "sha256": "f" * 64}]}])
