import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


DEFAULT_SETTINGS = {
    "poll_seconds": 60,
    "market_only": True,
    "completed_only": True,
    "kdj_n": 9,
    "kdj_m1": 3,
    "kdj_m2": 3,
    "cooldown_minutes": 30,
    "provider": "auto",
    "browser_enabled": False,
    "browser_template_mode": "rule",
    "browser_title": "{name} · {rule}",
    "browser_body": "{symbol} · {period}\nJ={J} K={K} D={D}\n价格：{close}\nK线：{bar_time}",
}
DEFAULT_BODY = "{name} ({symbol}) · {period}\nJ={J} K={K} D={D}\n价格：{close}\nK线时间：{bar_time}\n条件：{rule}"


@contextmanager
def connect(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            periods TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            kdj_n INTEGER, kdj_m1 INTEGER, kdj_m2 INTEGER
        );
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, server TEXT NOT NULL,
            token TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            sound TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '股票提醒',
            level TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            stock_ids TEXT NOT NULL, periods TEXT NOT NULL, condition TEXT NOT NULL,
            mode TEXT NOT NULL, cooldown_minutes INTEGER,
            title TEXT NOT NULL, body TEXT NOT NULL, target_ids TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            stock_id INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
            period TEXT NOT NULL, data TEXT, error TEXT, checked_at TEXT NOT NULL,
            PRIMARY KEY(stock_id,period)
        );
        CREATE TABLE IF NOT EXISTS rule_state (
            rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
            stock_id INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
            period TEXT NOT NULL, matched INTEGER NOT NULL, last_sent REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(rule_id,stock_id,period)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, rule_name TEXT NOT NULL,
            symbol TEXT NOT NULL, stock_name TEXT NOT NULL, period TEXT NOT NULL,
            bar_time TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            values_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(rule_id,symbol,period,bar_time)
        );
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
            target_id INTEGER, target_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, next_retry REAL NOT NULL DEFAULT 0, sent_at TEXT,
            UNIQUE(alert_id,target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries(status,next_retry);
        """)
        # Existing installations retain their settings, records, and Bark behavior.
        alert_columns = {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
        for column in ("browser_title", "browser_body"):
            if column not in alert_columns:
                c.execute(f"ALTER TABLE alerts ADD COLUMN {column} TEXT")
        # Seed only on first initialization; intentionally deleted examples stay deleted.
        if c.execute("SELECT 1 FROM settings WHERE id=1").fetchone() is None:
            c.execute("INSERT INTO settings VALUES (1,?)", (json.dumps(DEFAULT_SETTINGS),))
            c.execute("INSERT INTO stocks(symbol,name,periods) VALUES (?,?,?)", ("sh688605", "先锋精科", json.dumps(["day", "5", "15"])))
            for name, threshold, op, relation in [("J 高位提醒", 98, ">", ">"), ("J 低位提醒", 2, "<", "<")]:
                condition = {"logic": "all", "items": [{"left": "J", "op": op, "right": threshold}, {"left": "K", "op": relation, "right": "D"}]}
                c.execute("""INSERT INTO rules(name,stock_ids,periods,condition,mode,title,body,target_ids)
                    VALUES (?,?,?,?,?,?,?,?)""", (name, "[]", '["day","5","15"]', json.dumps(condition), "on_enter", "{name} · {rule}", DEFAULT_BODY, "[]"))


def settings(c):
    return {**DEFAULT_SETTINGS, **json.loads(c.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])}


def decode(row, fields=()):
    obj = dict(row)
    for key in fields:
        obj[key] = json.loads(obj[key]) if obj[key] is not None else None
    if "enabled" in obj:
        obj["enabled"] = bool(obj["enabled"])
    return obj
