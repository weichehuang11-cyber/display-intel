"""資料庫（SQLite 單一檔案，存在 repo 的 data/ 資料夾，免費且不需架伺服器）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .schema import PanelRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY, title TEXT, source TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS panels (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, confidence TEXT,
    first_seen TEXT, last_updated TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,          -- panel / platform / fab / industry
    kind TEXT NOT NULL,              -- new / updated / rumor_confirmed / announcement ...
    dedup_key TEXT,
    confidence TEXT,
    source_url TEXT,
    source_name TEXT,
    payload TEXT NOT NULL,
    reported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_reported ON events(reported_at);
CREATE INDEX IF NOT EXISTS idx_events_dedup ON events(dedup_key);
CREATE TABLE IF NOT EXISTS watch_state (
    name TEXT PRIMARY KEY, last_checked TEXT, found INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    month TEXT, service TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (month, service)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, started TEXT, finished TEXT, stats TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ---------- 文章 ----------
    def seen_urls(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT url FROM articles")}

    def recent_titles(self, days: int = 3) -> list[str]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        return [r[0] for r in self.conn.execute("SELECT title FROM articles WHERE fetched_at >= ?", (since,))]

    def add_article(self, url: str, title: str, source: str):
        self.conn.execute("INSERT OR IGNORE INTO articles VALUES (?,?,?,?)", (url, title, source, utcnow().isoformat()))

    # ---------- 面板紀錄 ----------
    def get_panel(self, key: str) -> Optional[PanelRecord]:
        row = self.conn.execute("SELECT data FROM panels WHERE key = ?", (key,)).fetchone()
        return PanelRecord.model_validate_json(row[0]) if row else None

    def put_panel(self, rec: PanelRecord):
        self.conn.execute(
            "INSERT OR REPLACE INTO panels VALUES (?,?,?,?,?)",
            (rec.key, rec.model_dump_json(), rec.confidence.value, rec.first_seen.isoformat(), rec.last_updated.isoformat()))

    def all_panels(self) -> list[PanelRecord]:
        return [PanelRecord.model_validate_json(r[0]) for r in
                self.conn.execute("SELECT data FROM panels ORDER BY last_updated DESC")]

    # ---------- 事件 ----------
    def has_recent_event(self, dedup_key: str, days: int = 7) -> bool:
        since = (utcnow() - timedelta(days=days)).isoformat()
        return self.conn.execute("SELECT 1 FROM events WHERE dedup_key = ? AND created_at >= ?",
                                 (dedup_key, since)).fetchone() is not None

    def add_event(self, category: str, kind: str, payload: dict, confidence: str = "",
                  source_url: str = "", source_name: str = "", dedup_key: str = ""):
        self.conn.execute(
            "INSERT INTO events (created_at, category, kind, dedup_key, confidence, source_url, source_name, payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (utcnow().isoformat(), category, kind, dedup_key, confidence, source_url, source_name,
             json.dumps(payload, ensure_ascii=False, default=str)))

    def unreported_events(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events WHERE reported_at IS NULL ORDER BY id").fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def recent_events(self, days: int) -> list[dict]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute("SELECT * FROM events WHERE created_at >= ? ORDER BY id DESC", (since,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def mark_reported(self, ids: list[int]):
        now = utcnow().isoformat()
        self.conn.executemany("UPDATE events SET reported_at = ? WHERE id = ?", [(now, i) for i in ids])

    # ---------- 關注機型 ----------
    def watch_last_checked(self, name: str):
        row = self.conn.execute("SELECT last_checked FROM watch_state WHERE name = ?", (name,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def mark_watch_checked(self, name: str, found: int):
        self.conn.execute("INSERT INTO watch_state (name, last_checked, found) VALUES (?,?,?) "
                          "ON CONFLICT(name) DO UPDATE SET last_checked = excluded.last_checked, found = excluded.found",
                          (name, utcnow().isoformat(), found))

    # ---------- 外部服務月用量 ----------
    @staticmethod
    def _month() -> str:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m")

    def add_usage(self, service: str, n: int = 1):
        if n <= 0:
            return
        self.conn.execute("INSERT INTO usage (month, service, count) VALUES (?,?,?) "
                          "ON CONFLICT(month, service) DO UPDATE SET count = count + excluded.count",
                          (self._month(), service, n))

    def usage(self, service: str) -> int:
        row = self.conn.execute("SELECT count FROM usage WHERE month = ? AND service = ?", (self._month(), service)).fetchone()
        return row[0] if row else 0

    # ---------- 執行紀錄 ----------
    def log_run(self, stage: str, started: datetime, stats: dict):
        self.conn.execute("INSERT INTO runs (stage, started, finished, stats) VALUES (?,?,?,?)",
                          (stage, started.isoformat(), utcnow().isoformat(), json.dumps(stats, ensure_ascii=False)))
