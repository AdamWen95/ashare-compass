"""F1 daily discovery ledger; deliberately contains no quote or screening code.

Adapters supply *evidenced* normalized metadata.  This module never infers a
security's exchange, board, share class, or risk state from its code/name.
The f1_ tables are additive and may live alongside a legacy SQLite database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


BOARDS = ("sse_main", "szse_main", "chinext", "star", "bse")
SCOPE_BOARDS = {"all_a": BOARDS, "sse_szse_a": BOARDS[:-1]}
BOARD_EXCHANGE = {"sse_main": "SSE", "star": "SSE", "szse_main": "SZSE", "chinext": "SZSE", "bse": "BSE"}
RISK_STATES = ("st", "suspended", "delisting_period")
KNOWN_OTHER_TYPES = {"b_share", "etf", "fund", "bond", "convertible_bond", "index", "preferred", "cdr", "h_share", "neeq"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
NAMESPACE = uuid.UUID("28138e3b-fde1-4b27-8bf3-1c207ec78b95")


def scope_boards(scope: str = "all_a") -> tuple[str, ...]:
    """Explicit product scopes, never a caller-selected subset of failed boards."""
    try:
        return SCOPE_BOARDS[scope]
    except (KeyError, TypeError) as exc:
        raise ValueError("scope must be all_a or sse_szse_a") from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _day(value: str) -> date:
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("dates must use YYYY-MM-DD")
    return result


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must contain a timezone")
    return result.astimezone(SHANGHAI)


def _in_range(day: str, start: str | None, end: str | None) -> bool:
    return bool(start and _day(start) <= _day(day) and (not end or _day(day) <= _day(end)))


class UniverseStore:
    """Additive, versioned SQLite ledger with immutable snapshot payloads."""

    def __init__(self, path: str | Path, *, mode: str = "research") -> None:
        if mode not in {"research", "offline_test"}:
            raise ValueError("universe mode must be research or offline_test")
        self.path = Path(path).resolve()
        self.mode = mode
        if "demo" in {part.lower() for part in self.path.parts} or self.path.name.lower() == "demo.sqlite3":
            raise ValueError("universe cannot write a DEMO path")
        if mode == "offline_test" and "research" in {part.lower() for part in self.path.parts}:
            raise ValueError("offline_test must not write a research directory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name.lower().startswith("demo_") for name in tables):
            self.connection.close()
            raise ValueError("universe cannot write a DEMO database")
        if tables and "f1_schema" not in tables and "market_metadata" not in tables:
            self.connection.close()
            raise ValueError("existing database is not a managed universe or market database")
        if "market_metadata" in tables:
            metadata = dict(self.connection.execute("SELECT key,value FROM market_metadata"))
            legacy_kind = metadata.get("verification_kind", "").lower()
            if metadata.get("mode") != "research" or (mode == "research" and legacy_kind in {"offline_test", "demo", "synthetic"}) or (mode == "offline_test" and legacy_kind != "offline_test"):
                self.connection.close()
                raise ValueError("legacy database provenance mode mismatch")
        existing = "f1_schema" in tables
        if existing:
            saved = self.connection.execute("SELECT version, mode FROM f1_schema").fetchone()
            if not saved or saved["version"] != 1 or saved["mode"] != mode:
                self.connection.close()
                raise ValueError("universe schema version or provenance mode mismatch")
        with self.connection:
            self.connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS f1_schema(version INTEGER NOT NULL, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS f1_securities(
                    security_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
                    first_seen_at TEXT NOT NULL, first_source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS f1_security_aliases(
                    alias_id INTEGER PRIMARY KEY, security_id TEXT NOT NULL REFERENCES f1_securities(security_id),
                    provider TEXT NOT NULL, exchange TEXT NOT NULL, code TEXT NOT NULL,
                    valid_from TEXT NOT NULL, valid_to TEXT,
                    evidence_source TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    UNIQUE(provider, exchange, code, valid_from)
                );
                CREATE INDEX IF NOT EXISTS f1_alias_lookup ON f1_security_aliases(provider, exchange, code, valid_from, valid_to);
                CREATE INDEX IF NOT EXISTS f1_exchange_code ON f1_security_aliases(exchange, code, valid_from, valid_to);
                CREATE TABLE IF NOT EXISTS f1_universe_snapshots(
                    snapshot_id TEXT PRIMARY KEY, requested_date TEXT NOT NULL,
                    resolved_trade_date TEXT, cutoff_at TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS f1_snapshot_date ON f1_universe_snapshots(requested_date, first_observed_at);
                CREATE TRIGGER IF NOT EXISTS f1_snapshot_no_update BEFORE UPDATE ON f1_universe_snapshots
                    BEGIN SELECT RAISE(ABORT, 'universe snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS f1_snapshot_no_delete BEFORE DELETE ON f1_universe_snapshots
                    BEGIN SELECT RAISE(ABORT, 'universe snapshots are immutable'); END;
            """)
            if not existing:
                self.connection.execute("INSERT INTO f1_schema VALUES(1, ?)", (mode,))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> UniverseStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM f1_universe_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        return self._decode_snapshot(row) if row else None

    @staticmethod
    def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        unhashed = {key: value for key, value in payload.items() if key not in {"snapshot_id", "content_hash"}}
        digest = hashlib.sha256(_json(unhashed).encode("utf-8")).hexdigest()
        if digest != payload.get("content_hash") or digest != row["content_hash"] or payload.get("snapshot_id") != row["snapshot_id"] or payload.get("snapshot_id") != "universe-" + row["requested_date"] + "-" + digest[:20]:
            raise ValueError("universe snapshot content hash mismatch")
        for field in ("requested_date", "resolved_trade_date", "cutoff_at", "status"):
            if payload.get(field) != row[field]:
                raise ValueError("universe snapshot metadata mismatch")
        if payload.get("observed_at") != row["first_observed_at"]:
            raise ValueError("universe snapshot observation mismatch")
        return payload

    def latest_snapshot(self, before_date: str | None = None, *, scope: str | None = None) -> dict[str, Any] | None:
        conditions, values = [], []
        if before_date:
            conditions.append("requested_date<?")
            values.append(before_date)
        if scope is not None:
            scope_boards(scope)
            conditions.append("COALESCE(json_extract(payload,'$.scope'),'all_a')=?")
            values.append(scope)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        row = self.connection.execute("SELECT * FROM f1_universe_snapshots" + where +
            " ORDER BY requested_date DESC, first_observed_at DESC, rowid DESC LIMIT 1", values).fetchone()
        return self._decode_snapshot(row) if row else None

    def latest_verified_snapshot(self, before_date: str, *, scope: str | None = None) -> dict[str, Any] | None:
        condition, values = "", [before_date]
        if scope is not None:
            scope_boards(scope)
            condition = " AND COALESCE(json_extract(payload,'$.scope'),'all_a')=?"
            values.append(scope)
        row = self.connection.execute("SELECT * FROM f1_universe_snapshots WHERE requested_date<? AND json_extract(payload,'$.structural_verified')=1" +
            condition + " ORDER BY requested_date DESC, first_observed_at DESC, rowid DESC LIMIT 1", values).fetchone()
        return self._decode_snapshot(row) if row else None

    def latest_observed_snapshot(self, *, on_or_before_date: str, observed_before: str) -> dict[str, Any] | None:
        """Scope-transition context only; never used as another scope's baseline."""
        row = self.connection.execute("SELECT * FROM f1_universe_snapshots WHERE requested_date<=? AND first_observed_at<? ORDER BY first_observed_at DESC, rowid DESC LIMIT 1",
            (on_or_before_date, _time(observed_before).isoformat())).fetchone()
        return self._decode_snapshot(row) if row else None

    def resolve_alias(self, provider: str, exchange: str, code: str, on_date: str) -> str | None:
        _day(on_date)
        rows = self.connection.execute("SELECT DISTINCT security_id FROM f1_security_aliases WHERE provider=? AND exchange=? AND code=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (provider, exchange, code, on_date, on_date)).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous security alias")
        return rows[0][0] if rows else None

    def _register(self, row: dict[str, Any], day: str, observed_at: str) -> str:
        provider, exchange, code = row["provider"], row["exchange"], row["code"]
        known = self.resolve_alias(provider, exchange, code, day)
        if known:
            return known
        provisional = self.connection.execute("SELECT DISTINCT security_id FROM f1_security_aliases WHERE provider=? AND code=? AND exchange='UNKNOWN' AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (provider, code, day, day)).fetchall()
        if exchange == "UNKNOWN":
            candidates = self.connection.execute("SELECT DISTINCT security_id FROM f1_security_aliases WHERE provider=? AND code=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (provider, code, day, day)).fetchall()
            if len(candidates) > 1:
                raise ValueError("unknown exchange has ambiguous known identities")
            if candidates:
                return candidates[0][0]
        elif provisional:
            if len(provisional) != 1:
                raise ValueError("ambiguous provisional identity")
            known_exchanges = {entry[0] for entry in self.connection.execute("SELECT DISTINCT exchange FROM f1_security_aliases WHERE security_id=? AND exchange!='UNKNOWN' AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (provisional[0][0], day, day))}
            if known_exchanges - {exchange}:
                raise ValueError("provisional code maps to conflicting exchanges")
            self.connection.execute("INSERT INTO f1_security_aliases(security_id,provider,exchange,code,valid_from,valid_to,evidence_source,evidence_id) VALUES(?,?,?,?,?,?,?,?)", (provisional[0][0], provider, exchange, code, day, None, row["metadata_source"], row.get("evidence_id") or "observed:" + observed_at))
            return provisional[0][0]
        expired = self.connection.execute("SELECT MAX(valid_to) FROM f1_security_aliases WHERE provider=? AND exchange=? AND code=? AND valid_to<?", (provider, exchange, code, day)).fetchone()[0]
        if expired and (not row.get("listing_date") or row["listing_date"] <= expired):
            raise ValueError("expired alias cannot be reopened without new listing evidence")
        # Another provider's current alias can link the *same exchange and code*;
        # cross-exchange codes and names are never identity evidence.
        others = self.connection.execute("SELECT DISTINCT security_id FROM f1_security_aliases WHERE exchange=? AND code=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (exchange, code, day, day)).fetchall() if exchange != "UNKNOWN" and row["metadata_verified"] else []
        if len(others) > 1:
            raise ValueError("conflicting provider mappings")
        identity_key = f"{provider}|{exchange}|{row.get('native_security_id') or code}|{row.get('listing_date') or day}"
        security_id = others[0][0] if others else str(uuid.uuid5(NAMESPACE, identity_key))
        self.connection.execute("INSERT OR IGNORE INTO f1_securities VALUES(?,?,?,?)", (security_id, identity_key, observed_at, row["metadata_source"]))
        # Discovery is evidence of alias validity only from this date; earlier
        # validity requires explicit alias evidence instead of a guessed history.
        self.connection.execute("INSERT INTO f1_security_aliases(security_id,provider,exchange,code,valid_from,valid_to,evidence_source,evidence_id) VALUES(?,?,?,?,?,?,?,?)", (security_id, provider, exchange, code, day, None, row["metadata_source"], row.get("evidence_id") or "observed:" + observed_at))
        return security_id

    def _apply_alias_event(self, event: dict[str, Any], day: str, cutoff_at: str, observed_at: str) -> None:
        required = ("provider", "exchange", "old_code", "new_code", "effective_date", "evidence_source", "evidence_id", "observed_at")
        if event.get("verified") is not True or any(not event.get(key) for key in required):
            raise ValueError("alias event requires verified mapping evidence")
        effective = event["effective_date"]
        if _day(effective) > _day(day) or _time(event["observed_at"]) > _time(observed_at):
            raise ValueError("alias event is not yet effective or observed")
        if event.get("published_at") and _time(event["published_at"]) > _time(cutoff_at):
            raise ValueError("alias mapping published after cutoff")
        if event["exchange"] not in {"SSE", "SZSE", "BSE"} or event["old_code"] == event["new_code"]:
            raise ValueError("invalid alias event exchange or code")
        provider, exchange = event["provider"], event["exchange"]
        old_day = (_day(effective) - timedelta(days=1)).isoformat()
        old_id = self.resolve_alias(provider, exchange, event["old_code"], old_day)
        # A freshly fetched historical mapping may be registered before either
        # code has ever been observed; the source event supplies the identity.
        new_ids = self.connection.execute("SELECT DISTINCT security_id FROM f1_security_aliases WHERE provider=? AND exchange=? AND code=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (provider, exchange, event["new_code"], day, effective)).fetchall()
        if len(new_ids) > 1:
            raise ValueError("alias mapping conflicts with overlapping new-code identities")
        new_id = new_ids[0][0] if new_ids else None
        if old_id and new_id and old_id != new_id:
            raise ValueError("alias mapping conflicts with existing identities")
        chosen = old_id or new_id
        if not chosen:
            identity_key = f"mapping|{provider}|{exchange}|{event['old_code']}|{effective}"
            chosen = str(uuid.uuid5(NAMESPACE, identity_key))
            self.connection.execute("INSERT OR IGNORE INTO f1_securities VALUES(?,?,?,?)", (chosen, identity_key, observed_at, event["evidence_source"]))
        if old_id:
            self.connection.execute("UPDATE f1_security_aliases SET valid_to=?,evidence_source=?,evidence_id=? WHERE provider=? AND exchange=? AND code=? AND security_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", (old_day, event["evidence_source"], event["evidence_id"], provider, exchange, event["old_code"], chosen, old_day, effective))
        elif event.get("old_valid_from"):
            if _day(event["old_valid_from"]) > _day(old_day):
                raise ValueError("invalid historical alias interval")
            self.connection.execute("INSERT OR IGNORE INTO f1_security_aliases(security_id,provider,exchange,code,valid_from,valid_to,evidence_source,evidence_id) VALUES(?,?,?,?,?,?,?,?)", (chosen, provider, exchange, event["old_code"], event["old_valid_from"], old_day, event["evidence_source"], event["evidence_id"]))
        if not new_id:
            self.connection.execute("INSERT INTO f1_security_aliases(security_id,provider,exchange,code,valid_from,valid_to,evidence_source,evidence_id) VALUES(?,?,?,?,?,?,?,?)", (chosen, provider, exchange, event["new_code"], effective, None, event["evidence_source"], event["evidence_id"]))
        elif self.resolve_alias(provider, exchange, event["new_code"], effective) is None:
            first_known = self.connection.execute("SELECT MIN(valid_from) FROM f1_security_aliases WHERE provider=? AND exchange=? AND code=? AND security_id=? AND valid_from>?", (provider, exchange, event["new_code"], chosen, effective)).fetchone()[0]
            if not first_known:
                raise ValueError("mapping has no verifiable new-code interval")
            # Late evidence may extend a known identity backwards to its actual
            # effective date. Keep the original observation and alias intact.
            until = (_day(first_known) - timedelta(days=1)).isoformat()
            self.connection.execute("INSERT INTO f1_security_aliases(security_id,provider,exchange,code,valid_from,valid_to,evidence_source,evidence_id) VALUES(?,?,?,?,?,?,?,?)", (chosen, provider, exchange, event["new_code"], effective, until, event["evidence_source"], event["evidence_id"]))


def _status_state(raw: dict[str, Any] | None, day: str, cutoff_at: str, observed_at: str) -> dict[str, Any]:
    raw = dict(raw or {})
    value = raw.get("value")
    reason = None
    if value is not True and value is not False:
        reason = "not_reported"
    elif raw.get("verified") is not True or not raw.get("source") or not raw.get("evidence_id"):
        reason = "unverified_evidence"
    else:
        try:
            if not _in_range(day, raw.get("effective_from"), raw.get("effective_to")):
                reason = "outside_effective_interval"
            elif not raw.get("effective_to") and raw.get("as_of_date") != day:
                reason = "status_freshness_unverified"
            elif not raw.get("observed_at") or _time(raw["observed_at"]) > _time(observed_at):
                reason = "unverified_observation_time"
            elif raw.get("published_at") and _time(raw["published_at"]) > _time(cutoff_at):
                reason = "published_after_cutoff"
            elif raw.get("derived_from_absence") and not (raw.get("complete_list_verified") is True and raw.get("list_as_of_date") == day):
                reason = "absence_not_evidence"
        except (ValueError, TypeError):
            reason = "invalid_evidence_date"
    return {**raw, "value": None if reason else value, "unknown_reason": reason}


def _validate_sources(pages: list[dict[str, Any]], manifests: list[dict[str, Any]], day: str, observed_at: str,
                      required_boards: tuple[str, ...] = BOARDS) -> list[str]:
    blockers: list[str] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        grouped[(page["provider"], page["dataset"])].append(page)
    seen: set[tuple[str, str]] = set()
    covered: set[str] = set()
    for manifest in manifests:
        key = (manifest["provider"], manifest["dataset"])
        label = ":".join(key)
        if key in seen:
            blockers.append("duplicate_manifest:" + label)
        seen.add(key)
        if manifest.get("permission_status") != "approved":
            blockers.append("permission_unconfirmed:" + label)
        if manifest.get("as_of_date") != day:
            blockers.append("source_date_mismatch:" + label)
        try:
            if not manifest.get("observed_at") or _time(manifest["observed_at"]) > _time(observed_at):
                blockers.append("source_observation_unverified:" + label)
        except (ValueError, TypeError):
            blockers.append("source_observation_unverified:" + label)
        if manifest.get("errors"):
            blockers.append("source_failed:" + label)
        parts = grouped.get(key, [])
        numbers = [part.get("page_number") for part in parts]
        if not numbers or any(type(number) is not int for number in numbers) or sorted(numbers) != list(range(1, len(numbers) + 1)):
            blockers.append("pagination_gap_or_duplicate:" + label)
        terminal_pages = [part.get("page_number") for part in parts if part.get("terminal") is True]
        if terminal_pages != [len(parts)]:
            blockers.append("pagination_terminal_unverified:" + label)
        if manifest.get("expected_pages") is not None and manifest["expected_pages"] != len(parts):
            blockers.append("pagination_truncated:" + label)
        count = sum(len(part.get("records", [])) for part in parts)
        if manifest.get("expected_records") is not None and count != manifest["expected_records"]:
            blockers.append("record_count_mismatch:" + label)
        if manifest.get("complete") is not True:
            blockers.append("source_completeness_unverified:" + label)
        if manifest.get("authoritative") is not True:
            blockers.append("source_authority_unverified:" + label)
        if not manifest.get("lineage_id"):
            blockers.append("source_lineage_unknown:" + label)
        covered.update(manifest.get("coverage_boards", []))
    for key in grouped.keys() - seen:
        blockers.append("source_manifest_missing:" + ":".join(key))
    for board in required_boards:
        if board not in covered:
            blockers.append("board_source_unverified:" + board)
    return blockers


def _normalize(raw: dict[str, Any], page: dict[str, Any], day: str, cutoff_at: str, observed_at: str) -> dict[str, Any]:
    row = dict(raw)
    row["provider"] = page["provider"]
    row["code"] = str(row.get("code") or "").strip()
    if not row["code"]:
        raise ValueError("discovery row missing code")
    row.setdefault("name", "")
    row.setdefault("exchange", "UNKNOWN")
    row.setdefault("board", "unknown")
    row.setdefault("security_type", "unknown")
    row.setdefault("listing_status", "unknown")
    row["metadata_source"] = row.get("metadata_source") or page["provider"] + ":" + page["dataset"]
    row["metadata_verified"] = row.get("metadata_verified") is True
    if not row["metadata_verified"]:
        # Retain claimed values for inspection, never use them as classification.
        row["unverified_metadata"] = {key: row[key] for key in ("exchange", "board", "security_type", "listing_status")}
        row.update(exchange="UNKNOWN", board="unknown", security_type="unknown", listing_status="unknown")
    elif row["exchange"] not in {"SSE", "SZSE", "BSE"} or (row["board"] in BOARDS and BOARD_EXCHANGE[row["board"]] != row["exchange"]):
        row["metadata_conflict"] = True
    row["statuses"] = {key: _status_state(row.get("statuses", {}).get(key), day, cutoff_at, observed_at) for key in RISK_STATES}
    row["classification_reasons"] = []
    if row["security_type"] in KNOWN_OTHER_TYPES and row["metadata_verified"]:
        row["discovery_classification"] = "outside_ordinary_a"
    elif row["security_type"] != "ordinary_a" or row["board"] not in BOARDS or row["exchange"] == "UNKNOWN" or row.get("metadata_conflict"):
        row["discovery_classification"] = "metadata_unknown"
    else:
        row["discovery_classification"] = "ordinary_a"
    try:
        if row["metadata_verified"] and row.get("listing_date") and _day(row["listing_date"]) > _day(day):
            row["discovery_classification"] = "not_yet_listed"
        if row.get("delisting_date") and _day(row["delisting_date"]) <= _day(day):
            if row["metadata_verified"]:
                row["discovery_classification"] = "delisted_with_evidence"
        if row["listing_status"] == "delisted" and not row.get("delisting_date"):
            row["classification_reasons"].append("delisting_date_unknown")
    except (ValueError, TypeError):
        row["classification_reasons"].append("invalid_listing_date")
    if row["listing_status"] not in {"listed", "delisted"}:
        row["classification_reasons"].append("listing_status_unknown")
    if not row.get("listing_date"):
        row["classification_reasons"].append("listing_date_unknown")
    for key, status in row["statuses"].items():
        if status["value"] is True:
            row["classification_reasons"].append(key)
        elif status["value"] is None:
            row["classification_reasons"].append(key + "_unknown")
    # F1 does not fetch history. A listing today is discovered; history cannot
    # be made sufficient using elapsed calendar days or synthetic bars.
    row["newly_listed"] = row.get("listing_date") == day
    row["history_status"] = "not_assessed_f1"
    row["research_eligibility"] = "excluded_by_verified_risk" if any(row["statuses"][key]["value"] is True for key in RISK_STATES) else "pending_history_f2"
    if row["discovery_classification"] != "ordinary_a" or row["classification_reasons"]:
        if row["research_eligibility"] != "excluded_by_verified_risk":
            row["research_eligibility"] = "pending_metadata_or_status"
    return row


def sync_universe(
    store: UniverseStore, *, requested_date: str, resolved_trade_date: str | None,
    cutoff_at: str, observed_at: str, pages: Iterable[dict[str, Any]],
    manifests: Iterable[dict[str, Any]], calendar_verified: bool = False,
    alias_events: Iterable[dict[str, Any]] = (), reconciliations: Iterable[dict[str, Any]] = (),
    scope: str = "all_a", config_version: str | None = None,
) -> dict[str, Any]:
    """Freeze full discovery and explicit gaps; no source failure shrinks scope.

    A complete listing requires authoritative metadata and proven terminal
    pagination for every board. Independent reconciliation is optional for a
    single authoritative source, but an asserted reconciliation must be valid.
    Status completeness is recorded separately from listing completeness.
    """
    required_boards = scope_boards(scope)
    if config_version is not None and (not isinstance(config_version, str) or not config_version.strip()):
        raise ValueError("config_version must be a nonempty version string")
    required_exchanges = tuple(exchange for exchange in ("SSE", "SZSE", "BSE")
                               if exchange in {BOARD_EXCHANGE[board] for board in required_boards})
    _day(requested_date)
    day = resolved_trade_date or requested_date
    _day(day)
    cutoff = _time(cutoff_at)
    observed = _time(observed_at)
    if cutoff.date().isoformat() < requested_date:
        raise ValueError("cutoff local date cannot precede requested_date")
    cutoff_at, observed_at = cutoff.isoformat(), observed.isoformat()
    pages, manifests, alias_events, reconciliations = list(pages), list(manifests), list(alias_events), list(reconciliations)
    # No fixture can be accepted by a research database, including fixtures
    # whose adapter accidentally labels a page as a successful response.
    provenance = [m.get("provenance_mode") for m in manifests]
    if store.mode == "research" and any(mode != "online" for mode in provenance):
        raise ValueError("research universe only accepts online source manifests")
    if store.mode == "research" and any(p.get("provenance_mode") == "offline_test" or any(r.get("provenance_mode") == "offline_test" for r in p.get("records", [])) for p in pages):
        raise ValueError("synthetic records cannot enter research")
    previous = store.latest_snapshot(before_date=requested_date, scope=scope)
    baseline = store.latest_verified_snapshot(before_date=requested_date, scope=scope) or previous
    observed_previous = store.latest_observed_snapshot(on_or_before_date=requested_date, observed_before=observed_at)
    scope_change = None
    if observed_previous and observed_previous.get("scope", "all_a") != scope:
        old_scope = observed_previous.get("scope", "all_a")
        scope_change = {"from_scope": old_scope, "to_scope": scope,
                        "previous_snapshot_id": observed_previous["snapshot_id"],
                        "excluded_boards": sorted(set(scope_boards(old_scope)) - set(required_boards)),
                        "included_boards": sorted(set(required_boards) - set(scope_boards(old_scope))),
                        "classification": "scope_change_not_delisting"}
    blockers = _validate_sources(pages, manifests, day, observed_at, required_boards)
    if calendar_verified is not True or resolved_trade_date is None:
        blockers.append("calendar_unverified")
    if resolved_trade_date and resolved_trade_date != requested_date:
        blockers.append("requested_trade_date_mismatch")
    if observed < cutoff:
        blockers.append("cutoff_not_reached")
    members: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    security_rows: dict[str, dict[str, Any]] = {}
    with store.connection:
        for index, event in enumerate(alias_events):
            store.connection.execute("SAVEPOINT alias_event")
            try:
                store._apply_alias_event(event, day, cutoff_at, observed_at)
            except (ValueError, sqlite3.IntegrityError) as exc:
                store.connection.execute("ROLLBACK TO alias_event")
                blockers.append(f"alias_conflict:{index}:{exc}")
            finally:
                store.connection.execute("RELEASE alias_event")
        for page in pages:
            for raw in page.get("records", []):
                row = _normalize(raw, page, day, cutoff_at, observed_at)
                if row["metadata_verified"] and row["board"] in BOARDS and row["board"] not in required_boards:
                    row["discovery_classification"] = "outside_requested_scope"
                    row["classification_reasons"].append("outside_requested_scope")
                key = (row["provider"], row["exchange"], row["code"])
                if key in seen:
                    blockers.append("duplicate_source_code:" + ":".join(key))
                    continue
                seen.add(key)
                try:
                    row["security_id"] = store._register(row, day, observed_at)
                    row["first_seen_at"] = store.connection.execute("SELECT first_seen_at FROM f1_securities WHERE security_id=?", (row["security_id"],)).fetchone()[0]
                except (ValueError, sqlite3.IntegrityError) as exc:
                    blockers.append("identity_conflict:" + ":".join(key) + ":" + str(exc))
                    row["security_id"] = "unresolved:" + str(uuid.uuid5(NAMESPACE, _json(key)))
                prior = security_rows.get(row["security_id"])
                if prior:
                    compare = ("code", "exchange", "board", "security_type", "name", "listing_status", "listing_date", "delisting_date")
                    if any(prior.get(field) != row.get(field) for field in compare):
                        blockers.append("conflicting_security_metadata:" + row["security_id"])
                    for state in RISK_STATES:
                        old_state, new_state = prior["statuses"][state], row["statuses"][state]
                        values = {old_state["value"], new_state["value"]} - {None}
                        if len(values) > 1 or old_state.get("unknown_reason") == "conflicting_verified_sources":
                            prior["statuses"][state] = {"value": None, "unknown_reason": "conflicting_verified_sources", "conflicting_evidence": [old_state, new_state]}
                            prior["research_eligibility"] = "pending_metadata_or_status"
                            prior["classification_reasons"] = sorted(set(prior["classification_reasons"] + [state + "_unknown"]))
                            blockers.append("conflicting_security_status:" + row["security_id"] + ":" + state)
                        elif old_state["value"] is None and new_state["value"] is not None:
                            prior["statuses"][state] = new_state
                            prior["classification_reasons"] = [reason for reason in prior["classification_reasons"] if reason != state + "_unknown"]
                            if new_state["value"] is True:
                                prior["classification_reasons"].append(state)
                                prior["research_eligibility"] = "excluded_by_verified_risk"
                    prior.setdefault("additional_source_records", []).append(row)
                    continue
                security_rows[row["security_id"]] = row
                members.append(row)
        members.sort(key=lambda member: member["security_id"])
        active = [row for row in members if row["discovery_classification"] == "ordinary_a"]
        board_counts = {board: sum(row["board"] == board for row in active) for board in required_boards}
        exchange_counts = {exchange: sum(row["exchange"] == exchange for row in active) for exchange in required_exchanges}
        unknown_count = sum(row["discovery_classification"] == "metadata_unknown" for row in members)
        if unknown_count:
            blockers.append("unclassified_metadata:" + str(unknown_count))
        invalid_listing = sum(bool(set(row["classification_reasons"]) & {"listing_date_unknown", "listing_status_unknown", "delisting_date_unknown", "invalid_listing_date"}) for row in active)
        if invalid_listing:
            blockers.append("listing_validity_unverified:" + str(invalid_listing))
        for board, count in board_counts.items():
            if count == 0:
                blockers.append("board_empty:" + board)
            if baseline and count < baseline["board_counts"].get(board, 0):
                # A verified delisting date can explain removals; missing rows
                # are always gaps, never an implicit delisting event.
                now = {row["security_id"]: row for row in members}
                removed = [row for row in baseline["members"] if row["board"] == board and row["discovery_classification"] == "ordinary_a" and (row["security_id"] not in now or now[row["security_id"]]["discovery_classification"] != "delisted_with_evidence")]
                present_ids = {row["security_id"] for row in active}
                if any(row["security_id"] not in present_ids for row in removed):
                    blockers.append("board_count_drop_unexplained:" + board)
        source_lineages = {manifest.get("lineage_id") for manifest in manifests}
        reconciliation_results = []
        for check in reconciliations:
            reasons = []
            if check.get("permission_status") != "approved" or check.get("verified") is not True:
                reasons.append("reconciliation_unverified")
            if not check.get("lineage_id") or check["lineage_id"] in source_lineages:
                reasons.append("reconciliation_not_independent")
            if check.get("as_of_date") != day:
                reasons.append("reconciliation_date_mismatch")
            for board in required_boards:
                if check.get("board_counts", {}).get(board) != board_counts[board]:
                    reasons.append("reconciliation_board_mismatch:" + board)
            blockers.extend(reasons)
            reconciliation_results.append({**check, "passed": not reasons, "reasons": reasons})
        old = {row["security_id"]: row for row in previous["members"]} if previous else {}
        current = {row["security_id"]: row for row in members}
        required_classifications = {"ordinary_a", "metadata_unknown"}
        old_required = {key for key, row in old.items() if row["discovery_classification"] in required_classifications}
        baseline_ids = {row["security_id"] for row in baseline["members"] if row["discovery_classification"] in required_classifications} if baseline else set()
        inherited_missing = set(previous["changes"]["missing_unexplained"]) if previous else set()
        explained_retired = {key for key, row in old.items() if row["discovery_classification"] == "delisted_with_evidence"}
        missing = sorted((old_required | baseline_ids | inherited_missing) - current.keys() - explained_retired)
        if missing:
            blockers.append("previous_members_missing_unexplained:" + str(len(missing)))
        changes = {
            "previous_snapshot_id": previous["snapshot_id"] if previous else None,
            "coverage_baseline_snapshot_id": baseline["snapshot_id"] if baseline else None,
            "change_kind": "scope_change" if scope_change else "within_scope_observation",
            "scope_change": scope_change,
            "added": sorted(current.keys() - old.keys()),
            "missing_unexplained": missing,
            "delisted_with_evidence": [key for key, row in current.items() if row["discovery_classification"] == "delisted_with_evidence" and (key not in old or old[key]["discovery_classification"] != "delisted_with_evidence")],
            "changed": [{"security_id": key, "fields": {field: {"before": old[key].get(field), "after": current[key].get(field)} for field in ("code", "name", "exchange", "board", "listing_status") if old[key].get(field) != current[key].get(field)}} for key in sorted(old.keys() & current.keys()) if any(old[key].get(field) != current[key].get(field) for field in ("code", "name", "exchange", "board", "listing_status"))],
        }
        blockers = sorted(set(blockers))
        status_scope = [row for row in members if row["discovery_classification"] in required_classifications]
        unknown_status = sum(any(row["statuses"][key]["value"] is None for key in RISK_STATES) or bool(set(row["classification_reasons"]) & {"listing_date_unknown", "listing_status_unknown", "delisting_date_unknown", "invalid_listing_date"}) for row in status_scope)
        structural_verified = not blockers
        collection_ready = structural_verified and store.mode == "research"
        for row in members:
            row["collection_ready"] = collection_ready and row["discovery_classification"] == "ordinary_a"
            # F1 has no verified history/adjustment window. A known risk flag or
            # its absence cannot make a security ready for research at this stage.
            row["research_ready"] = False
            row["research_readiness_reasons"] = sorted(set(row["classification_reasons"] + ["history_not_verified_F2"]))
        payload = {
            "schema_version": 1, "scope": scope, "mode": store.mode,
            "required_boards": list(required_boards), "excluded_boards": sorted(set(BOARDS) - set(required_boards)),
            "requested_date": requested_date, "resolved_trade_date": resolved_trade_date,
            "cutoff_at": cutoff_at, "observed_at": observed_at,
            "is_backfill": observed.date() > _day(requested_date),
            "historical_reconstruction": observed.date() > _day(requested_date),
            "calendar_verified": calendar_verified is True,
            "status": "blocked" if blockers else ("sample" if store.mode == "offline_test" else "partial" if unknown_status else "complete"),
            "coverage_dimension": "universe_only", "capability_stage": "f1_discovery_only",
            "universe_verified": structural_verified and store.mode == "research",
            "collection_ready": collection_ready, "research_ready": False,
            "research_readiness_reasons": ["history_not_verified_F2"] + (["risk_status_unknown"] if unknown_status else []),
            "structural_verified": structural_verified,
            "status_verified": unknown_status == 0 and bool(status_scope),
            "status_denominator": len(status_scope),
            "formal_candidates_blocked": True, "next_stage": "F2",
            "discovered_records": sum(len(page.get("records", [])) for page in pages),
            "discovered_unique": len(members), "ordinary_a_count": len(active),
            "board_counts": board_counts, "exchange_counts": exchange_counts,
            "unknown_metadata_count": unknown_count, "unknown_status_count": unknown_status,
            "classification_counts": dict(Counter(row["discovery_classification"] for row in members)),
            "risk_counts": {key: sum(row["statuses"][key]["value"] is True for row in active) for key in RISK_STATES},
            "newly_listed_count": sum(row["newly_listed"] for row in active),
            "blockers": blockers, "changes": changes, "members": members,
            "source_manifests": manifests,
            "page_manifest": [{key: value for key, value in page.items() if key != "records"} | {"record_count": len(page.get("records", []))} for page in pages],
            "alias_events": alias_events, "reconciliations": reconciliation_results,
        }
        # Optional for direct legacy callers. Existing frozen payloads are read
        # unchanged; an explicitly supplied version belongs inside the hash.
        if config_version is not None:
            payload["config_version"] = config_version
        content_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        payload["content_hash"] = content_hash
        payload["snapshot_id"] = "universe-" + requested_date + "-" + content_hash[:20]
        store.connection.execute("INSERT OR IGNORE INTO f1_universe_snapshots VALUES(?,?,?,?,?,?,?,?)", (payload["snapshot_id"], requested_date, resolved_trade_date, cutoff_at, observed_at, content_hash, payload["status"], _json(payload)))
    return payload
