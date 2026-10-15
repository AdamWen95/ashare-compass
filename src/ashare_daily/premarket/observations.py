"""Preserve first observation by source, original URL and content version."""
import re
import sqlite3

from .contracts import aware


def record_observation(database, source_id, url, content_hash, observed_at):
    stamp = aware(observed_at).isoformat()
    if not re.fullmatch(r"[a-f0-9]{64}", content_hash):
        raise ValueError("observation_hash_invalid")
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(database, timeout=1) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS source_observations (source_id TEXT NOT NULL, original_url TEXT NOT NULL, content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY(source_id,original_url,content_hash))")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT first_seen_at FROM source_observations WHERE source_id=? AND original_url=? AND content_hash=?", (source_id, url, content_hash)).fetchone()
            if existing and aware(existing[0]) > aware(stamp):
                raise ValueError("observation_clock_regressed")
            if not existing:
                connection.execute("INSERT INTO source_observations VALUES (?,?,?,?)", (source_id, url, content_hash, stamp))
            return {"first_seen_at": existing[0] if existing else stamp, "previously_observed": existing is not None}
    except sqlite3.Error as exc:
        raise RuntimeError("observation_store_unavailable") from exc
