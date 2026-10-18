"""SQLite persistence.

Two data tables, both with rolling-window retention:

  observation  ARPAE observations, indexed on the measurement instant.
               Retention: ts >= now - retention_h.

  forecast     every downloaded forecast snapshot, indexed on
               (fetched_at, valid_ts). We don't keep just the latest
               forecast: keeping the snapshots lets us see how the forecast
               for a given instant changed over time.
               Retention: fetched_at >= now - retention_h.

The PRIMARY KEYs make writes idempotent (INSERT OR REPLACE): re-running a
cycle duplicates nothing, and ARPAE corrections overwrite the old value as
their model expects ("last received value wins").
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observation (
    ts      INTEGER NOT NULL,   -- UTC epoch of the measurement instant
    source  TEXT    NOT NULL,   -- e.g. 'arpae:simnbo:1134677,4449420'
    var     TEXT    NOT NULL,   -- canonical name (see vars.py)
    value   REAL,
    PRIMARY KEY (ts, source, var)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS forecast (
    fetched_at INTEGER NOT NULL,  -- when we downloaded the snapshot
    source     TEXT    NOT NULL,  -- e.g. 'open-meteo:icon_seamless'
    valid_ts   INTEGER NOT NULL,  -- instant the forecast is valid for
    var        TEXT    NOT NULL,
    value      REAL,
    PRIMARY KEY (fetched_at, source, valid_ts, var)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_forecast_valid ON forecast (source, valid_ts);

CREATE TABLE IF NOT EXISTS rule_state (
    rule       TEXT PRIMARY KEY,
    last_value INTEGER NOT NULL DEFAULT 0,  -- last outcome (0/1), for edge detection
    last_fired INTEGER                      -- epoch of the last notification sent
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS notification (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    rule      TEXT    NOT NULL,
    text      TEXT    NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_notification_ts ON notification (ts);
"""

# Time series: list of (timestamp, {variable: value}), ordered by timestamp.
Series = list[tuple[int, dict[str, float]]]


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # WAL: concurrent reads (e.g. a manual query while the agent writes)
        # without blocking. NORMAL is enough: worst case we lose the last cycle.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ writes

    def save_observations(self, source: str, rows: Iterable[tuple[int, str, float]]) -> int:
        payload = [(ts, source, var, value) for ts, var, value in rows]
        if not payload:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO observation (ts, source, var, value) VALUES (?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    def save_forecast(
        self, source: str, fetched_at: int, rows: Iterable[tuple[int, str, float]]
    ) -> int:
        payload = [(fetched_at, source, valid_ts, var, value) for valid_ts, var, value in rows]
        if not payload:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO forecast (fetched_at, source, valid_ts, var, value) "
                "VALUES (?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # ------------------------------------------------------------------ reads

    def observation_series(self, source: str | None, start_ts: int, end_ts: int) -> Series:
        if source is None:
            rows = self.conn.execute(
                "SELECT ts, var, value FROM observation WHERE ts BETWEEN ? AND ? ORDER BY ts",
                (start_ts, end_ts),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ts, var, value FROM observation "
                "WHERE source = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (source, start_ts, end_ts),
            ).fetchall()
        return _to_series(rows, "ts")

    def latest_fetch_time(self, source: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(fetched_at) AS t FROM forecast WHERE source = ?", (source,)
        ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    def latest_forecast_series(self, source: str, start_ts: int, end_ts: int) -> Series:
        """Latest available snapshot, restricted to the requested validity window."""
        fetched_at = self.latest_fetch_time(source)
        if fetched_at is None:
            return []
        rows = self.conn.execute(
            "SELECT valid_ts, var, value FROM forecast "
            "WHERE source = ? AND fetched_at = ? AND valid_ts BETWEEN ? AND ? "
            "ORDER BY valid_ts",
            (source, fetched_at, start_ts, end_ts),
        ).fetchall()
        return _to_series(rows, "valid_ts")

    def forecast_sources(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT source FROM forecast ORDER BY source").fetchall()
        return [r["source"] for r in rows]

    def observation_sources(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT source FROM observation ORDER BY source"
        ).fetchall()
        return [r["source"] for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("observation", "forecast", "notification"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        out["forecast_snapshots"] = self.conn.execute(
            "SELECT COUNT(DISTINCT fetched_at) AS n FROM forecast"
        ).fetchone()["n"]
        return out

    # ------------------------------------------------------------------ rule state

    def get_rule_state(self, rule: str) -> tuple[int, int | None]:
        row = self.conn.execute(
            "SELECT last_value, last_fired FROM rule_state WHERE rule = ?", (rule,)
        ).fetchone()
        if row is None:
            return 0, None
        return int(row["last_value"]), row["last_fired"]

    def set_rule_state(self, rule: str, last_value: int, last_fired: int | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO rule_state (rule, last_value, last_fired) VALUES (?, ?, ?) "
                "ON CONFLICT(rule) DO UPDATE SET last_value = excluded.last_value, "
                "last_fired = excluded.last_fired",
                (rule, last_value, last_fired),
            )

    def log_notification(self, ts: int, rule: str, text: str, delivered: bool) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO notification (ts, rule, text, delivered) VALUES (?, ?, ?, ?)",
                (ts, rule, text, 1 if delivered else 0),
            )

    # ------------------------------------------------------------------ retention

    def purge(self, now_ts: int, retention_h: int) -> dict[str, int]:
        """Applies the rolling window. Returns the number of rows deleted per table."""
        cutoff = now_ts - retention_h * 3600
        deleted = {}
        with self.conn:
            cur = self.conn.execute("DELETE FROM observation WHERE ts < ?", (cutoff,))
            deleted["observation"] = cur.rowcount
            # For forecasts the window slides on fetched_at, not valid_ts:
            # this way we keep 72h of snapshots, each complete with its own horizon.
            cur = self.conn.execute("DELETE FROM forecast WHERE fetched_at < ?", (cutoff,))
            deleted["forecast"] = cur.rowcount
            cur = self.conn.execute("DELETE FROM notification WHERE ts < ?", (cutoff,))
            deleted["notification"] = cur.rowcount
        if any(deleted.values()):
            log.debug("purge < %d: %s", cutoff, deleted)
        return deleted

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")


def _to_series(rows: Sequence[sqlite3.Row], ts_field: str) -> Series:
    grouped: dict[int, dict[str, float]] = {}
    for row in rows:
        grouped.setdefault(row[ts_field], {})[row["var"]] = row["value"]
    return [(ts, grouped[ts]) for ts in sorted(grouped)]
