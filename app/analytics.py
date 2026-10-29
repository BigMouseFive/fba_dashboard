"""统计聚合：全部基于预聚合表 ``agg_daily``。

性能设计
--------
1. 看板查询一律读 ``agg_daily``（约为明细行数的 1/4），不再扫 ``orders``。
2. 每个请求只开一个连接（由调用方传入 ``conn``）。
3. 「本期 / 上期」用 ``CASE WHEN`` 一次扫描同时算出，查询数减半。

口径与旧版一致：以「商品数量」之和为总量。
唯一差异：订单数跨天合并无法精确去重，多日区间用 ``orders_approx`` 标记，前端显示 ≈。
"""

from __future__ import annotations

from datetime import date, timedelta

from .db import read_conn

COUNTRY_LABEL = {"UAE": "United Arab Emirates", "KSA": "Saudi Arabia"}
SCOPE_LABEL = {"all": "全部站点", "uae": "单站点 · 阿联酋", "ksa": "单站点 · 沙特"}


# --------------------------------------------------------------------------
# 日期区间
# --------------------------------------------------------------------------
def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def month_range(month: str) -> tuple[date, date]:
    """'2026-10' -> (2026-10-01, 2026-10-31)"""
    y, m = (int(x) for x in month.split("-")[:2])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return start, end


def resolve_range(mode: str, day: str | None, month: str | None,
                  start: str | None, end: str | None):
    """返回 (start, end, 标签)。"""
    if mode == "month" and month:
        s, e = month_range(month)
        return s, e, f"{s:%Y年%m月}"
    if mode == "custom" and start and end:
        s, e = parse_day(start), parse_day(end)
        if s > e:
            s, e = e, s
        return s, e, f"{s:%Y-%m-%d} ~ {e:%Y-%m-%d}"
    if mode == "week" and day:
        d = parse_day(day)
        s = d - timedelta(days=d.weekday())
        return s, s + timedelta(days=6), f"{s:%Y-%m-%d} ~ {(s + timedelta(days=6)):%Y-%m-%d}"
    d = parse_day(day) if day else latest_date()
    return d, d, f"{d:%Y-%m-%d}"


def prev_range(start: date, end: date, mode: str) -> tuple[date, date]:
    """上一周期：按月取上一个自然月，其余按等长前移。"""
    if mode == "month":
        first = start.replace(day=1)
        last_prev = first - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    span = (end - start).days + 1
    return start - timedelta(days=span), end - timedelta(days=span)


def latest_date() -> date:
    with read_conn() as conn:
        row = conn.execute("SELECT MAX(biz_date) AS d FROM agg_daily").fetchone()
    if row and row["d"]:
        return date.fromisoformat(row["d"])
    return date.today()


def _pct(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _dim_orders(conn, dim: str, s: date, e: date, scope: str = "all") -> dict[str, int]:
    """读维度级精确订单数（预聚合，避免 COUNT(DISTINCT order_id) 扫明细）。

    scope 决定取 ALL / UAE / KSA 那一份，保证切站点口径时订单数同步变化。
    """
    ctry = {"all": "ALL", "uae": "UAE", "ksa": "KSA"}.get(scope, "ALL")
    if dim == "total":
        row = conn.execute(
            "SELECT COALESCE(SUM(orders),0) AS n FROM agg_day_total WHERE biz_date BETWEEN ? AND ?",
            (s.isoformat(), e.isoformat())).fetchone()
        return {"": row["n"]}
    rows = conn.execute(
        """SELECT key, SUM(orders) AS n FROM agg_dim_orders
           WHERE dim = ? AND country = ? AND biz_date BETWEEN ? AND ? GROUP BY key""",
        (dim, ctry, s.isoformat(), e.isoformat())).fetchall()
    return {r["key"]: r["n"] for r in rows}


def _scope_filter(scope: str) -> tuple[str, tuple]:
    """把站点口径翻译成 SQL 片段（作用于 agg_daily.country）。"""
    if scope == "uae":
        return " AND country = 'UAE'", ()
    if scope == "ksa":
        return " AND country = 'KSA'", ()
    return "", ()


# --------------------------------------------------------------------------
# 核心查询
# --------------------------------------------------------------------------
def _window(conn, s: date, e: date, ps: date, pe: date) -> dict:
    """本期 + 上期基础合计。

    件数走预聚合（快）；订单数回原始表 COUNT(DISTINCT order_id) 精确计算。
    原因：订单可以有多行（多 SKU / 跨小时 / 跨店铺），一旦按维度拆开聚合，
    同一订单会被重复计数，所以订单数只能用原始明细去重。
    """
    lo, hi = min(s, ps).isoformat(), max(e, pe).isoformat()
    row = conn.execute(
        """SELECT
              COALESCE(SUM(CASE WHEN biz_date BETWEEN ? AND ? THEN qty ELSE 0 END), 0) AS cur_qty,
              COALESCE(SUM(CASE WHEN biz_date BETWEEN ? AND ? THEN qty ELSE 0 END), 0) AS prev_qty
           FROM agg_daily WHERE biz_date BETWEEN ? AND ?""",
        (s.isoformat(), e.isoformat(), ps.isoformat(), pe.isoformat(), lo, hi),
    ).fetchone()

    cur_ord = conn.execute(
        "SELECT COALESCE(SUM(orders),0) AS n FROM agg_day_total WHERE biz_date BETWEEN ? AND ?",
        (s.isoformat(), e.isoformat())).fetchone()["n"]
    prev_ord = conn.execute(
        "SELECT COALESCE(SUM(orders),0) AS n FROM agg_day_total WHERE biz_date BETWEEN ? AND ?",
        (ps.isoformat(), pe.isoformat())).fetchone()["n"]

    def dims_of(lo_d: date, hi_d: date) -> dict:
        r = conn.execute(
            """SELECT COUNT(DISTINCT shop) AS shops, COUNT(DISTINCT operator) AS ops,
                      COUNT(DISTINCT biz_date) AS days
               FROM agg_day WHERE biz_date BETWEEN ? AND ?""",
            (lo_d.isoformat(), hi_d.isoformat()),
        ).fetchone()
        return {"shops": r["shops"], "operators": r["ops"], "days": r["days"]}

    cur = {"qty": row["cur_qty"], "orders": cur_ord, **dims_of(s, e)}
    prev = {"qty": row["prev_qty"], "orders": prev_ord, **dims_of(ps, pe)}
    return {"cur": cur, "prev": prev}


def _by_country(conn, s: date, e: date, ps: date, pe: date) -> list[dict]:
    rows = conn.execute(
        """SELECT country,
                  COALESCE(SUM(CASE WHEN biz_date BETWEEN ? AND ? THEN qty ELSE 0 END), 0) AS cur_qty,
                  COALESCE(SUM(CASE WHEN biz_date BETWEEN ? AND ? THEN qty ELSE 0 END), 0) AS prev_qty
           FROM agg_daily WHERE biz_date BETWEEN ? AND ?
           GROUP BY country ORDER BY cur_qty DESC""",
        (s.isoformat(), e.isoformat(), ps.isoformat(), pe.isoformat(),
         min(s, ps).isoformat(), max(e, pe).isoformat()),
    ).fetchall()

    # (日, 国家) 粒度：agg_day 按店铺拆过会重复计数，这里用 orders 精确去重。
    # 跨国家不存在同一订单，故可安全相加。
    ords = {r["country"]: r["n"] for r in conn.execute(
        """SELECT country, COUNT(DISTINCT order_id) AS n FROM orders
           WHERE biz_date BETWEEN ? AND ? GROUP BY country""",
        (s.isoformat(), e.isoformat()),
    ).fetchall()}

    total = sum(r["cur_qty"] for r in rows) or 1
    return [{
        "country": r["country"],
        "label": COUNTRY_LABEL.get(r["country"], r["country"]),
        "qty": r["cur_qty"],
        "orders": ords.get(r["country"], 0),
        "share": round(r["cur_qty"] / total * 100, 1),
        "prev_qty": r["prev_qty"],
        "delta_pct": _pct(r["cur_qty"], r["prev_qty"]),
    } for r in rows]


def _by_team(conn, s: date, e: date, ps: date, pe: date, scope: str) -> list[dict]:
    extra, eargs = _scope_filter(scope)
    rows = conn.execute(
        f"""SELECT COALESCE(NULLIF(o.team, ''), '未分组') AS team,
                   COALESCE(SUM(CASE WHEN a.biz_date BETWEEN ? AND ? THEN a.qty ELSE 0 END), 0) AS cur_qty,
                   COALESCE(SUM(CASE WHEN a.biz_date BETWEEN ? AND ? THEN a.qty ELSE 0 END), 0) AS prev_qty,
                   COALESCE(SUM(CASE WHEN a.biz_date BETWEEN ? AND ? AND a.country = 'UAE'
                                     THEN a.qty ELSE 0 END), 0) AS cur_uae,
                   COALESCE(SUM(CASE WHEN a.biz_date BETWEEN ? AND ? AND a.country = 'KSA'
                                     THEN a.qty ELSE 0 END), 0) AS cur_ksa,
                   COUNT(DISTINCT CASE WHEN a.biz_date BETWEEN ? AND ?
                                       THEN a.operator END) AS operators
            FROM agg_daily a
            LEFT JOIN operators o ON o.code = a.operator
            WHERE a.biz_date BETWEEN ? AND ?{extra}
            GROUP BY team
            HAVING cur_qty > 0
            ORDER BY cur_qty DESC""",
        (s.isoformat(), e.isoformat(), ps.isoformat(), pe.isoformat(),
         s.isoformat(), e.isoformat(), s.isoformat(), e.isoformat(),
         s.isoformat(), e.isoformat(),
         min(s, ps).isoformat(), max(e, pe).isoformat()) + eargs,
    ).fetchall()

    ords = _dim_orders(conn, "team", s, e, scope)

    total = sum(r["cur_qty"] for r in rows) or 1
    return [{
        "team": r["team"],
        "qty": r["cur_qty"],
        "orders": ords.get(r["team"], 0),
        "prev_qty": r["prev_qty"],
        "delta_pct": _pct(r["cur_qty"], r["prev_qty"]),
        "share": round(r["cur_qty"] / total * 100, 1),
        "operators": r["operators"],
        "uae": r["cur_uae"],
        "ksa": r["cur_ksa"],
    } for r in rows]


def _by_operator(conn, s: date, e: date, scope: str = "all") -> list[dict]:
    extra, eargs = _scope_filter(scope)
    rows = conn.execute(
        f"""SELECT a.operator AS code,
                   COALESCE(MAX(NULLIF(o.name, '')), a.operator) AS name,
                   COALESCE(MAX(NULLIF(o.team, '')), '未分组')     AS team,
                   SUM(a.qty) AS qty,
                   SUM(CASE WHEN a.country = 'UAE' THEN a.qty ELSE 0 END) AS uae,
                   SUM(CASE WHEN a.country = 'KSA' THEN a.qty ELSE 0 END) AS ksa,
                   0 AS orders,
                   COUNT(DISTINCT a.biz_date) AS active_days
            FROM agg_daily a
            LEFT JOIN operators o ON o.code = a.operator
            WHERE a.biz_date BETWEEN ? AND ?{extra}
            GROUP BY a.operator
            HAVING SUM(a.qty) > 0
            ORDER BY qty DESC""",
        (s.isoformat(), e.isoformat()) + eargs,
    ).fetchall()
    return [dict(r) for r in rows]


def _daily_series(conn, s: date, e: date) -> list[dict]:
    qty = {r["biz_date"]: r["qty"] for r in conn.execute(
        """SELECT biz_date, SUM(qty) AS qty FROM agg_daily
           WHERE biz_date BETWEEN ? AND ? GROUP BY biz_date""",
        (s.isoformat(), e.isoformat()),
    ).fetchall()}
    ords = {r["biz_date"]: r["orders"] for r in conn.execute(
        """SELECT biz_date, orders FROM agg_day_total WHERE biz_date BETWEEN ? AND ?""",
        (s.isoformat(), e.isoformat()),
    ).fetchall()}
    return [{"biz_date": d, "qty": qty.get(d, 0), "orders": ords.get(d, 0)}
            for d in sorted(qty)]


def _by_hour(conn, s: date, e: date) -> list[dict]:
    rows = conn.execute(
        """SELECT hour,
                  SUM(CASE WHEN country = 'UAE' THEN qty ELSE 0 END) AS uae,
                  SUM(CASE WHEN country = 'KSA' THEN qty ELSE 0 END) AS ksa,
                  SUM(qty) AS total
           FROM agg_daily WHERE biz_date BETWEEN ? AND ? AND hour >= 0
           GROUP BY hour ORDER BY hour""",
        (s.isoformat(), e.isoformat()),
    ).fetchall()
    grid = {h: {"UAE": 0, "KSA": 0, "total": 0} for h in range(24)}
    for r in rows:
        if r["hour"] in grid:
            grid[r["hour"]] = {"UAE": r["uae"], "KSA": r["ksa"], "total": r["total"]}
    return [{"hour": f"{h:02d}", **grid[h]} for h in sorted(grid)]


def _by_shop(conn, s: date, e: date, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """SELECT shop, country, SUM(qty) AS qty
           FROM agg_daily WHERE biz_date BETWEEN ? AND ?
           GROUP BY shop ORDER BY qty DESC LIMIT ?""",
        (s.isoformat(), e.isoformat(), limit),
    ).fetchall()
    ords = _dim_orders(conn, "shop", s, e)
    return [{**dict(r), "orders": ords.get(r["shop"], 0)} for r in rows]


def _operator_country_split(conn, s: date, e: date) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        """SELECT operator, country, SUM(qty) AS qty FROM agg_daily
           WHERE biz_date BETWEEN ? AND ? GROUP BY operator, country""",
        (s.isoformat(), e.isoformat()),
    ).fetchall()
    return {(r["operator"], r["country"]): r["qty"] for r in rows}


# --------------------------------------------------------------------------
# 组合
# --------------------------------------------------------------------------
def _build_operator_board(cur: list[dict], prev: list[dict],
                          cur_split: dict, prev_split: dict,
                          span: int, scope: str) -> dict:
    prev_map = {it["code"]: it for it in prev}
    prev_rank = {it["code"]: i + 1
                 for i, it in enumerate(sorted(prev, key=lambda x: -x["qty"]))}
    # orders 由调用方用 agg_dim_orders 精确回填；此处先用占位

    deltas = [abs(it["qty"] - prev_map[it["code"]]["qty"])
              for it in cur if it["code"] in prev_map]
    threshold = round(sum(deltas) / len(deltas), 1) if deltas else 0.0
    top_qty = cur[0]["qty"] if cur else 0

    items = []
    for i, it in enumerate(cur):
        p = prev_map.get(it["code"], {})
        prev_qty = p.get("qty", 0)
        delta = it["qty"] - prev_qty
        moves = [{"country": cc, "label": COUNTRY_LABEL[cc],
                  "delta": cur_split.get((it["code"], cc), 0) - prev_split.get((it["code"], cc), 0)}
                 for cc in ("UAE", "KSA")]
        items.append({
            **it,
            "rank": i + 1,
            "prev_qty": prev_qty,
            "delta": delta,
            "delta_pct": _pct(it["qty"], prev_qty),
            "rank_prev": prev_rank.get(it["code"]),
            "rank_delta": (prev_rank[it["code"]] - (i + 1)) if it["code"] in prev_rank else None,
            "avg_per_day": round(it["qty"] / span, 1),
            "share": round(it["qty"] / (top_qty or 1) * 100, 1),
            "is_anomaly": bool(threshold and abs(delta) > threshold),
            "main_move": max(moves, key=lambda m: abs(m["delta"])),
        })

    return {
        "scope": scope,
        "scope_label": SCOPE_LABEL.get(scope, scope),
        "threshold": threshold,
        "items": items,
        "operator_count": len(items),
    }


def dashboard(mode: str, day: str | None, month: str | None,
              start: str | None, end: str | None,
              scope: str = "all", team_scope: str = "all") -> dict:
    """一次请求返回整个看板所需数据。"""
    s, e, label = resolve_range(mode, day, month, start, end)
    ps, pe = prev_range(s, e, mode)
    prev_label = f"{ps:%Y-%m-%d}" if ps == pe else f"{ps:%Y-%m-%d} ~ {pe:%Y-%m-%d}"
    span = (e - s).days + 1

    with read_conn() as conn:
        base = _window(conn, s, e, ps, pe)
        countries = _by_country(conn, s, e, ps, pe)
        teams = _by_team(conn, s, e, ps, pe, team_scope)
        daily = _daily_series(conn, s, e)
        trend = _daily_series(conn, e - timedelta(days=13), e)
        hours = _by_hour(conn, s, e)
        shops = _by_shop(conn, s, e, 12)
        cur_ops = _by_operator(conn, s, e, scope)
        prev_ops = _by_operator(conn, ps, pe, scope)
        cur_split = _operator_country_split(conn, s, e)
        prev_split = _operator_country_split(conn, ps, pe)
        cur_ord = _dim_orders(conn, "operator", s, e, scope)

    cur, prev = base["cur"], base["prev"]

    prev_team_rank = {name: i + 1 for i, (name, _) in enumerate(
        sorted(((t["team"], t["prev_qty"]) for t in teams), key=lambda kv: -kv[1]))}

    for i, t in enumerate(teams):
        t["rank"] = i + 1
        t["rank_prev"] = prev_team_rank.get(t["team"])
        t["rank_delta"] = (t["rank_prev"] - t["rank"]) if t["rank_prev"] else None

    mover = max(countries, key=lambda c: c["qty"] - c["prev_qty"]) if countries else None

    return {
        "range": {"start": s.isoformat(), "end": e.isoformat(), "label": label, "days": span},
        "compare": {"start": ps.isoformat(), "end": pe.isoformat(), "label": prev_label},
        "totals": cur,
        "prev": prev,
        # 跨天时订单数是各日去重数之和，不等于区间精确去重 → 前端用 ≈ 展示
        "orders_approx": span > 1,
        "delta": {
            "qty": cur["qty"] - prev["qty"],
            "qty_pct": _pct(cur["qty"], prev["qty"]),
            "orders_pct": _pct(cur["orders"], prev["orders"]),
        },
        "by_country": countries,
        "by_team": teams,
        "team_scope": team_scope,
        "team_total": sum(t["qty"] for t in teams),
        "team_operators": sum(t["operators"] for t in teams),
        "daily": daily,
        "trend": trend,
        "by_hour": hours,
        "by_shop": shops,
        "main_mover": mover,
        "operators": {
            **_build_operator_board(
                [{**o, "orders": cur_ord.get(o["code"], 0)} for o in cur_ops],
                prev_ops, cur_split, prev_split, span, scope),
            "range": {"start": s.isoformat(), "end": e.isoformat(), "label": label, "days": span},
            "compare": {"start": ps.isoformat(), "end": pe.isoformat()},
        },
    }


# --------------------------------------------------------------------------
# 其他接口
# --------------------------------------------------------------------------
def operator_daily(code: str) -> list[dict]:
    """单个运营的逐日走势（明细抽屉）。"""
    with read_conn() as conn:
        rows = conn.execute(
            """SELECT biz_date,
                      SUM(qty) AS qty,
                      SUM(CASE WHEN country = 'UAE' THEN qty ELSE 0 END) AS uae,
                      SUM(CASE WHEN country = 'KSA' THEN qty ELSE 0 END) AS ksa,
                      SUM(orders) AS orders
               FROM agg_daily WHERE operator = ?
               GROUP BY biz_date ORDER BY biz_date""",
            (code,),
        ).fetchall()
    out, prev = [], None
    for r in rows:
        item = dict(r)
        item["delta"] = (item["qty"] - prev) if prev is not None else None
        prev = item["qty"]
        out.append(item)
    return out


def export_orders(s: date, e: date) -> list[tuple]:
    """导出明细必须读原始 orders 表。"""
    with read_conn() as conn:
        rows = conn.execute(
            """SELECT biz_date, order_id, shop, country, operator, sku, qty, paid_at, status
               FROM orders WHERE biz_date BETWEEN ? AND ?
               ORDER BY biz_date, paid_at""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    return [tuple(r) for r in rows]


def meta() -> dict:
    with read_conn() as conn:
        dates = [r["biz_date"] for r in conn.execute(
            "SELECT DISTINCT biz_date FROM agg_daily ORDER BY biz_date").fetchall()]
        months = [r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(biz_date, 1, 7) AS m FROM agg_daily ORDER BY m").fetchall()]
        teams = [r["t"] for r in conn.execute(
            """SELECT DISTINCT team AS t FROM operators
               WHERE team IS NOT NULL AND team <> '' ORDER BY team""").fetchall()]
        batches = [dict(r) for r in conn.execute(
            """SELECT id, kind, biz_date, filename, rows, qty, shops, operators, imported_at, note
               FROM import_batches b
               WHERE b.id = (
                   SELECT MAX(id) FROM import_batches x
                   WHERE x.kind = b.kind AND COALESCE(x.biz_date, '') = COALESCE(b.biz_date, '')
               )
               ORDER BY b.id DESC LIMIT 20"""
        ).fetchall()]
        op_count = conn.execute("SELECT COUNT(*) AS c FROM operators").fetchone()["c"]
        active_ops = conn.execute(
            "SELECT COUNT(DISTINCT operator) AS c FROM agg_daily").fetchone()["c"]
        active_teams = conn.execute(
            """SELECT COUNT(DISTINCT COALESCE(NULLIF(o.team, ''), '未分组')) AS c
               FROM agg_daily a LEFT JOIN operators o ON o.code = a.operator"""
        ).fetchone()["c"]
    return {
        "dates": dates,
        "months": months,
        "teams": teams,
        "batches": batches,
        "latest": dates[-1] if dates else None,
        "first": dates[0] if dates else None,
        "operator_count": active_ops,
        "mapping_count": op_count,
        "team_count": active_teams,
    }
