"""SQLite storage with idempotent inserts (MQTT QoS 1 may redeliver messages)."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict

from .schema import Heartbeat, Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    station_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
    test_name TEXT NOT NULL, suite TEXT NOT NULL, build TEXT NOT NULL,
    status TEXT NOT NULL, duration_ms REAL NOT NULL,
    measurement REAL, lower_limit REAL, upper_limit REAL, unit TEXT,
    PRIMARY KEY (station_id, seq)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    station_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL, build TEXT NOT NULL,
    PRIMARY KEY (station_id, seq)
);
CREATE TABLE IF NOT EXISTS rejected (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, reason TEXT, payload TEXT,
    received_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:", commit_every: int = 200) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._pending = 0
        self._commit_every = commit_every
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _after_write(self) -> None:
        self._pending += 1
        if self._pending >= self._commit_every:
            self._conn.commit()
            self._pending = 0

    def insert_result(self, r: Result) -> bool:
        """Insert a result; returns False if (station_id, seq) was already stored."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.station_id, r.seq, r.timestamp, r.test_name, r.suite, r.build, r.status,
                 r.duration_ms, r.measurement, r.lower_limit, r.upper_limit, r.unit))
            self._after_write()
            return cur.rowcount == 1

    def insert_heartbeat(self, h: Heartbeat) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO heartbeats VALUES (?,?,?,?)",
                (h.station_id, h.seq, h.timestamp, h.build))
            self._after_write()
            return cur.rowcount == 1

    def record_rejected(self, topic: str, reason: str, payload: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejected (topic, reason, payload, received_at) VALUES (?,?,?,?)",
                (topic, reason, payload[:500].decode("utf-8", "replace"), time.time()))
            self._after_write()

    def flush(self) -> None:
        with self._lock:
            self._conn.commit()
            self._pending = 0

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._conn.close()

    def results(self) -> list[Result]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM results ORDER BY station_id, ts, seq").fetchall()
        return [Result(station_id=r["station_id"], seq=r["seq"], timestamp=r["ts"],
                       test_name=r["test_name"], suite=r["suite"], build=r["build"],
                       status=r["status"], duration_ms=r["duration_ms"],
                       measurement=r["measurement"], lower_limit=r["lower_limit"],
                       upper_limit=r["upper_limit"], unit=r["unit"]) for r in rows]

    def heartbeats(self) -> dict[str, list[float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT station_id, ts FROM heartbeats ORDER BY station_id, ts").fetchall()
        out: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            out[row["station_id"]].append(row["ts"])
        return dict(out)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                name: self._conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("results", "heartbeats", "rejected")
            }

    def rejected(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT topic, reason, payload FROM rejected ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
