"""SQLite 存储层：库表结构、连接与基础读写。

性能设计要点
------------
* 明细表 ``orders`` 保存原始订单行，供导入去重与明细导出使用。
* 预聚合表 ``agg_daily`` 按「日 × 国家 × 店铺 × 运营 × 小时」存汇总，
  看板所有统计都读它，避免每次请求扫全表。
* 每个请求只开一个连接（``read_conn`` / ``write_conn``），
  旧实现每个查询都新建连接，13 次建连是切换变慢的主因之一。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FBA_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("FBA_DB_PATH", DATA_DIR / "fba.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    biz_date      TEXT NOT NULL,          -- 业务日期 YYYY-MM-DD
    order_id      TEXT NOT NULL,
    trade_id      TEXT,
    status        TEXT,
    country       TEXT NOT NULL,          -- UAE / KSA
    shop          TEXT,
    paid_at       TEXT,
    hour          INTEGER,                -- 付款小时 0..23，导入时解析
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

-- 日粒度预聚合：看板所有统计读这张表
CREATE TABLE IF NOT EXISTS agg_daily (
    biz_date TEXT    NOT NULL,
    country  TEXT    NOT NULL,
    shop     TEXT    NOT NULL,
    operator TEXT    NOT NULL,
    hour     INTEGER NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 0,
    orders   INTEGER NOT NULL DEFAULT 0,   -- 该维度组合内的去重订单数
    PRIMARY KEY (biz_date, country, shop, operator, hour)
) WITHOUT ROWID;

-- 日级精确去重：agg_daily 按维度拆开后同一订单会被重复计数，
-- 全局订单数必须读这张表（跨日无重复订单号，可安全相加）
CREATE TABLE IF NOT EXISTS agg_day (
    biz_date TEXT NOT NULL,
    country  TEXT NOT NULL,
    shop     TEXT NOT NULL,
    operator TEXT NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 0,
    orders   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (biz_date, country, shop, operator)
) WITHOUT ROWID;

-- 维度级精确订单数：订单可跨运营（多 SKU 不同运营），故各维度单独去重存
CREATE TABLE IF NOT EXISTS agg_dim_orders (
    biz_date TEXT NOT NULL,
    dim      TEXT NOT NULL,     -- operator / team / shop（country 口径另存）
    country  TEXT NOT NULL,     -- 'ALL' 或 UAE / KSA
    key      TEXT NOT NULL,
    orders   INTEGER NOT NULL,
    PRIMARY KEY (biz_date, dim, country, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS agg_day_total (
    biz_date TEXT PRIMARY KEY,
    qty      INTEGER NOT NULL DEFAULT 0,
    orders   INTEGER NOT NULL DEFAULT 0,
    shops    INTEGER NOT NULL DEFAULT 0,
    operators INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_agg_date      ON agg_daily(biz_date);
CREATE INDEX IF NOT EXISTS idx_agg_date_op   ON agg_daily(biz_date, operator);
CREATE INDEX IF NOT EXISTS idx_agg_date_ctry ON agg_daily(biz_date, country);
CREATE INDEX IF NOT EXISTS idx_agg_date_hour ON agg_daily(biz_date, hour);

CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    biz_date     TEXT,
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


def _open(retries: int = 5) -> sqlite3.Connection:
    """打开 SQLite 连接（带退避重试）。"""
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


def connect() -> sqlite3.Connection:
    """裸连接（脚本、一次性操作用）。调用方负责 close()。"""
    return _open()


@contextmanager
def read_conn():
    """只读请求用：一个请求一个连接，退出时关闭。"""
    conn = _open()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn():
    """写操作用：成功才提交，异常回滚，退出时关闭。"""
    conn = _open()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with write_conn() as conn:
        conn.executescript(SCHEMA)
        # 老库补列：orders.hour 是后加的
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "hour" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN hour INTEGER")

        # agg_dim_orders 增加 country 维度（老库直接重建该表，稍后由 rebuild 填充）
        dim_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agg_dim_orders)")}
        if dim_cols and "country" not in dim_cols:
            conn.execute("DROP TABLE agg_dim_orders")
            conn.executescript(SCHEMA)
    backfill_hours()
    ensure_agg()


def backfill_hours() -> int:
    """把 paid_at 里的小时回填到 orders.hour（幂等，只处理 NULL 行）。"""
    with write_conn() as conn:
        cur = conn.execute(
            """UPDATE orders SET hour = CAST(substr(paid_at, 12, 2) AS INTEGER)
               WHERE hour IS NULL AND paid_at IS NOT NULL AND length(paid_at) >= 13"""
        )
        return cur.rowcount


def rebuild_agg(biz_date: str | None = None) -> int:
    """从 orders 重算 agg_daily。

    biz_date 为空时全量重建；否则只重建那一天。
    返回重建后的行数。
    """
    with write_conn() as conn:
        targets = ["agg_daily", "agg_day", "agg_day_total"]
        if biz_date:
            where, args = "WHERE biz_date = ?", (biz_date,)
            for t in targets:
                conn.execute(f"DELETE FROM {t} WHERE biz_date = ?", (biz_date,))
        else:
            where, args = "", ()
            for t in targets:
                conn.execute(f"DELETE FROM {t}")

        # 小时级（图表用）
        conn.execute(
            f"""INSERT INTO agg_daily (biz_date, country, shop, operator, hour, qty, orders)
                SELECT biz_date, country, COALESCE(shop,''), COALESCE(operator,''),
                       COALESCE(hour,-1), SUM(qty), COUNT(DISTINCT order_id)
                FROM orders {where}
                GROUP BY biz_date, country, shop, operator, hour""", args)

        # 日级 × 维度（团队/运营分解用，orders 在此粒度内精确）
        conn.execute(
            f"""INSERT INTO agg_day (biz_date, country, shop, operator, qty, orders)
                SELECT biz_date, country, COALESCE(shop,''), COALESCE(operator,''),
                       SUM(qty), COUNT(DISTINCT order_id)
                FROM orders {where}
                GROUP BY biz_date, country, shop, operator""", args)

        # 维度级精确订单数：每个维度独立 COUNT(DISTINCT)，并按站点口径各存一份
        conn.execute(f"DELETE FROM agg_dim_orders WHERE biz_date {'= ?' if biz_date else 'IS NOT NULL'}",
                     (biz_date,) if biz_date else ())

        def ins(dim: str, select_sql: str, select_args):
            conn.execute(
                f"""INSERT INTO agg_dim_orders (biz_date, dim, country, key, orders)
                    {select_sql}""", select_args)

        for ctry in ("ALL", "UAE", "KSA"):
            cextra = "" if ctry == "ALL" else f" AND country = '{ctry}'"
            w = (where + cextra) if where else ("WHERE " + cextra[5:] if cextra else "")
            a = args
            ins("operator", f"""SELECT biz_date, 'operator', ?, COALESCE(operator,''), COUNT(DISTINCT order_id)
                                FROM orders {w} GROUP BY biz_date, operator""", (ctry,) + a)
            ins("shop", f"""SELECT biz_date, 'shop', ?, COALESCE(shop,''), COUNT(DISTINCT order_id)
                            FROM orders {w} GROUP BY biz_date, shop""", (ctry,) + a)
            rw = w.replace("biz_date", "r.biz_date")
            ins("team", f"""SELECT r.biz_date, 'team', ?, COALESCE(NULLIF(o.team,''),'未分组'), COUNT(DISTINCT r.order_id)
                            FROM orders r LEFT JOIN operators o ON o.code = r.operator
                            {rw} GROUP BY r.biz_date, 4""", (ctry,) + a)

        # 日级总计（全局订单数精确来源）
        conn.execute(
            f"""INSERT INTO agg_day_total (biz_date, qty, orders, shops, operators)
                SELECT biz_date, SUM(qty), COUNT(DISTINCT order_id),
                       COUNT(DISTINCT shop), COUNT(DISTINCT operator)
                FROM orders {where} GROUP BY biz_date""", args)

        # 更新统计信息，避免 SQLite 选错执行计划（数据量增长后影响明显）
        conn.execute("ANALYZE")
        return conn.execute("SELECT COUNT(*) AS c FROM agg_daily").fetchone()["c"]


def ensure_agg() -> int:
    """预聚合表为空但明细有数据时（老库升级），自动重建一次。"""
    with write_conn() as conn:
        agg = conn.execute("SELECT COUNT(*) AS c FROM agg_daily").fetchone()["c"]
        tot = conn.execute("SELECT COUNT(*) AS c FROM agg_day_total").fetchone()["c"]
        dim = conn.execute("SELECT COUNT(*) AS c FROM agg_dim_orders").fetchone()["c"]
        if agg and tot and dim:
            return agg
        has_orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    if has_orders:
        return rebuild_agg()
    return 0


def latest_biz_date() -> str | None:
    with read_conn() as conn:
        row = conn.execute("SELECT MAX(biz_date) AS d FROM agg_daily").fetchone()
    return row["d"] if row and row["d"] else None


def get_meta(key: str, default: str | None = None) -> str | None:
    with read_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with write_conn() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
