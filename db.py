"""SQLite access layer.

The runner is stateless between GitHub Actions runs, so everything that must
survive lives in this file and is committed back to the repo.
"""
import os
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    pair            TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    alert_type      TEXT    NOT NULL,   -- choch_up | choch_down | zone
    price           REAL,               -- CHoCH level
    price_low       REAL,               -- zone lower bound
    price_high      REAL,               -- zone upper bound
    side            TEXT,               -- buy | sell | NULL (zone wording only)
    status          TEXT    NOT NULL DEFAULT 'active',  -- active|triggered|cancelled
    armed           INTEGER NOT NULL DEFAULT 0,  -- zone: price seen outside zone
    last_bar_ts     TEXT,               -- newest closed bar already evaluated
    created_at      TEXT    NOT NULL,
    next_check_at   TEXT    NOT NULL,
    last_checked_at TEXT,
    triggered_at    TEXT,
    fail_count      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alerts_due ON alerts(status, next_check_at);

CREATE TABLE IF NOT EXISTS telegram_offset (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    last_update_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    day     TEXT PRIMARY KEY,   -- UTC date, YYYY-MM-DD
    credits INTEGER NOT NULL
);

-- A half-parsed alert waiting on one clarifying reply, e.g. above or below.
CREATE TABLE IF NOT EXISTS pending_alerts (
    chat_id    INTEGER PRIMARY KEY,
    payload    TEXT NOT NULL,   -- JSON of the fields understood so far
    missing    TEXT NOT NULL,   -- comma separated field names still needed
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trigger_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id     INTEGER,
    chat_id      INTEGER,
    pair         TEXT,
    timeframe    TEXT,
    alert_type   TEXT,
    message      TEXT,
    price        REAL,
    triggered_at TEXT
);
"""


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text):
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def commit(self):
        self.conn.commit()

    # ---------- telegram offset ----------

    def get_offset(self):
        row = self.conn.execute(
            "SELECT last_update_id FROM telegram_offset WHERE id = 1").fetchone()
        return row["last_update_id"] if row else 0

    def set_offset(self, update_id):
        self.conn.execute(
            "INSERT INTO telegram_offset (id, last_update_id) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_update_id = excluded.last_update_id",
            (update_id,))
        self.conn.commit()

    # ---------- api credit accounting ----------

    def credits_used_today(self):
        day = utcnow().strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT credits FROM api_usage WHERE day = ?", (day,)).fetchone()
        return row["credits"] if row else 0

    def add_credits(self, n=1):
        day = utcnow().strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO api_usage (day, credits) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET credits = credits + excluded.credits",
            (day, n))
        self.conn.commit()

    # ---------- alerts ----------

    def add_alert(self, chat_id, pair, timeframe, alert_type, price=None,
                  price_low=None, price_high=None, side=None, next_check_at=None):
        now = utcnow()
        cur = self.conn.execute(
            "INSERT INTO alerts (chat_id, pair, timeframe, alert_type, price, "
            "price_low, price_high, side, status, created_at, next_check_at) "
            "VALUES (?,?,?,?,?,?,?,?,'active',?,?)",
            (chat_id, pair, timeframe, alert_type, price, price_low, price_high,
             side, iso(now), iso(next_check_at or now)))
        self.conn.commit()
        return cur.lastrowid

    def get_alert(self, alert_id, chat_id=None):
        if chat_id is None:
            return self.conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return self.conn.execute(
            "SELECT * FROM alerts WHERE id = ? AND chat_id = ?",
            (alert_id, chat_id)).fetchone()

    def list_alerts(self, chat_id, status="active"):
        if status is None:
            return self.conn.execute(
                "SELECT * FROM alerts WHERE chat_id = ? ORDER BY id",
                (chat_id,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM alerts WHERE chat_id = ? AND status = ? ORDER BY id",
            (chat_id, status)).fetchall()

    def due_alerts(self, now=None):
        now = now or utcnow()
        return self.conn.execute(
            "SELECT * FROM alerts WHERE status = 'active' AND next_check_at <= ? "
            "ORDER BY next_check_at", (iso(now),)).fetchall()

    def active_alerts(self):
        return self.conn.execute(
            "SELECT * FROM alerts WHERE status = 'active' ORDER BY id").fetchall()

    def update_alert(self, alert_id, **fields):
        if not fields:
            return
        cols = ", ".join("%s = ?" % k for k in fields)
        self.conn.execute("UPDATE alerts SET %s WHERE id = ?" % cols,
                          tuple(fields.values()) + (alert_id,))
        self.conn.commit()

    # ---------- pending clarifications ----------

    def set_pending(self, chat_id, payload_json, missing):
        self.conn.execute(
            "INSERT INTO pending_alerts (chat_id, payload, missing, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
            "payload = excluded.payload, missing = excluded.missing, "
            "created_at = excluded.created_at",
            (chat_id, payload_json, ",".join(missing), iso(utcnow())))
        self.conn.commit()

    def get_pending(self, chat_id):
        return self.conn.execute(
            "SELECT * FROM pending_alerts WHERE chat_id = ?", (chat_id,)).fetchone()

    def clear_pending(self, chat_id):
        self.conn.execute("DELETE FROM pending_alerts WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    # ---------- trigger log ----------

    def log_trigger(self, alert, message, price):
        self.conn.execute(
            "INSERT INTO trigger_log (alert_id, chat_id, pair, timeframe, "
            "alert_type, message, price, triggered_at) VALUES (?,?,?,?,?,?,?,?)",
            (alert["id"], alert["chat_id"], alert["pair"], alert["timeframe"],
             alert["alert_type"], message, price, iso(utcnow())))
        self.conn.commit()
        log("TRIGGER id=%s %s %s %s price=%s :: %s" % (
            alert["id"], alert["pair"], alert["timeframe"],
            alert["alert_type"], price, message))


def _append_log(line):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(config.LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (iso(utcnow()), line))


def log(line):
    """Print for the Actions console and persist for later review."""
    print(line, flush=True)
    _append_log(line)
