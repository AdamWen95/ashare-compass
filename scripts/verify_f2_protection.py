"""Read-only comparison with the actual pre-F2 backup; emit hashes, not data."""
from pathlib import Path
import hashlib
import json
import sqlite3
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def table_rows(db, name):
    quoted = '"' + name.replace('"', '""') + '"'
    rows = [json.dumps(list(row), ensure_ascii=False, default=lambda value: value.hex(), separators=(",", ":"))
            for row in db.execute("SELECT * FROM " + quoted)]
    return sorted(rows)


def main():
    backup = ROOT / "backups/f2-before-20260911"
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    unchanged, changed, missing = [], [], []
    for entry in manifest["files"]:
        if entry["kind"] != "immutable_file":
            continue
        path = ROOT / entry["path"]
        if not path.is_file():
            missing.append(entry["path"])
        elif digest(path) != entry["sha256"]:
            changed.append(entry["path"])
        else:
            unchanged.append(entry["path"])
    database_checks = []
    for relative, subset in (("data/research/market.sqlite3", False), ("data/research/universe.sqlite3", True),
                              ("data/operations/runtime.sqlite3", False)):
        old = backup / "files" / relative
        current = ROOT / relative
        if not old.is_file():
            continue
        before = sqlite3.connect(old.as_uri() + "?mode=ro", uri=True)
        after = sqlite3.connect(current.as_uri() + "?mode=ro", uri=True)
        try:
            for (name,) in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                original, present = table_rows(before, name), table_rows(after, name)
                matched = set(original).issubset(present) if subset else original == present
                database_checks.append({"database": relative, "table": name, "baseline_rows": len(original),
                    "current_rows": len(present), "mode": "baseline_rows_preserved" if subset else "exact_unchanged",
                    "passed": matched, "baseline_sha256": hashlib.sha256("\n".join(original).encode()).hexdigest()})
        finally:
            before.close()
            after.close()
    source_location = json.loads((ROOT / "outputs/verification/f2/source-backup-location.json").read_text(encoding="utf-8-sig"))
    source_changes = []
    with zipfile.ZipFile(Path(source_location["path"]) / "source.zip") as archive:
        baseline = {name.replace("\\", "/"): hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist() if not name.endswith("/")}
        for name, sha in baseline.items():
            path = ROOT / name
            if not path.exists() or digest(path) != sha:
                source_changes.append({"path": name, "change": "modified" if path.exists() else "missing"})
        for directory in ("src", "tests", "config", "scripts", "docs"):
            for path in (ROOT / directory).rglob("*"):
                relative = path.relative_to(ROOT).as_posix()
                if path.is_file() and path.suffix in {".py", ".json", ".md", ".ps1", ".sh"} and relative not in baseline and "__pycache__" not in path.parts:
                    source_changes.append({"path": relative, "change": "added"})
    result = {"baseline_backup": str(backup), "immutable_files_unchanged": len(unchanged),
        "changed_existing_archive_files": changed, "missing_existing_archive_files": missing,
        "legacy_database_checks": database_checks, "legacy_databases_preserved": all(item["passed"] for item in database_checks),
        "modified_source_files": source_changes, "model_calls_this_round": 0,
        "note": "Database comparison excludes newly added F2 tables; universe comparison preserves every old immutable row."}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
