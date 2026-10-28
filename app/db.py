"""SQLite 存储层：库表结构、连接与基础读写。"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FBA_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("FBA_DB_PATH", DATA_DIR / "fba.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    biz_date      TEXT NOT NULL,          -- 业务日期 YYYY-MM-DD（取主表 sheet 名 / 文件名 / 付款时间）
    order_id      TEXT NOT NULL,
    trade_id      TEXT,
    status        TEXT,
    country       TEXT NOT NULL,          -- United Arab Emirates / Saudi Arabia
    shop          TEXT,
    paid_at       TEXT,
    platform_sku  TEXT,
    sku           TEXT,
    operator      TEXT,
    qty           INTEGER NOT NULL DEFAULT 0,
    source_file   TEXT,
    imported_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_date      ON orders(biz_date);
CREATE INDEX IF NOT EXISTS idx_orders_date_op   ON orders(biz_date, operator);
CREATE INDEX IF NOT EXISTS idx_orders_date_ctry ON orders(biz_date, country);

CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,           -- orders / operators
    biz_date     TEXT,                    -- 订单文件对应日期
    filename     TEXT NOT NULL,
    rows         INTEGER NOT NULL,
    qty          INTEGER NOT NULL,
    shops        INTEGER NOT NULL DEFAULT 0,
    operators    INTEGER NOT NULL DEFAULT 0,
    imported_at  TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS operators (
    code       TEXT PRIMARY KEY,
    name       TEXT,
    team       TEXT,
    supervisor TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(retries: int = 5) -> sqlite3.Connection:
    """打开 SQLite 连接。

    WAL 模式下并发打开偶发 "unable to open database file"（多进程/沙箱瞬时争用），
    这里做一次短退避重试，避免单个请求直接 500。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
            time.sleep(0.08 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def get_meta(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
