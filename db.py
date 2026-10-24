"""Tiny SQLite layer. One connection per call keeps it safe across threads."""
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    price_usd REAL,
    payment_id TEXT,
    terac_opportunity_id TEXT,
    report TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    launched_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(email, status);
CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    option_index INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, submission_id)
);
CREATE TABLE IF NOT EXISTS webhook_events (
    id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _order(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["options"] = json.loads(d["options"])
    d["report"] = json.loads(d["report"]) if d["report"] else None
    return d


# ---------- orders ----------

def create_order(email: str, question: str, options: list[str], status: str,
                 price_usd: float, reason: str | None = None) -> dict:
    oid = "ord_" + secrets.token_urlsafe(9)
    with connect() as c:
        c.execute(
            "INSERT INTO orders (id, email, question, options, status, status_reason, price_usd, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (oid, email.strip().lower(), question, json.dumps(options), status, reason, price_usd, now()),
        )
    return get_order(oid)


def get_order(order_id: str) -> dict | None:
    with connect() as c:
        return _order(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())


def latest_pending_order_for(email: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM orders WHERE email=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (email.strip().lower(),),
        ).fetchone()
        return _order(row)


def orders_with_status(*statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with connect() as c:
        rows = c.execute(f"SELECT * FROM orders WHERE status IN ({marks})", statuses).fetchall()
        return [_order(r) for r in rows]


def update_order(order_id: str, **fields) -> None:
    if "report" in fields and fields["report"] is not None:
        fields["report"] = json.dumps(fields["report"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), order_id))


# ---------- votes ----------

def add_vote(order_id: str, submission_id: str, option_index: int, reason: str) -> bool:
    """Returns False if this submission already voted."""
    try:
        with connect() as c:
            c.execute(
                "INSERT INTO votes (order_id, submission_id, option_index, reason, created_at) VALUES (?,?,?,?,?)",
                (order_id, submission_id, option_index, reason, now()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def votes_for(order_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM votes WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------- webhooks + settings ----------

def mark_webhook_seen(event_id: str) -> bool:
    """Returns False if we've already processed this webhook id."""
    try:
        with connect() as c:
            c.execute("INSERT INTO webhook_events (id, received_at) VALUES (?,?)", (event_id, now()))
        return True
    except sqlite3.IntegrityError:
        return False


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def claim(order_id: str, from_status: str, to_status: str) -> bool:
    """Atomically move an order between statuses. False if another worker got there first."""
    with connect() as c:
        cur = c.execute("UPDATE orders SET status=? WHERE id=? AND status=?", (to_status, order_id, from_status))
        return cur.rowcount == 1
