"""统计聚合：总览、对比、团队、运营榜单、明细、趋势。"""

from __future__ import annotations

from datetime import date, timedelta

from .db import connect

COUNTRY_LABEL = {"UAE": "United Arab Emirates", "KSA": "Saudi Arabia"}
SCOPE_LABEL = {
    "all": "全部站点",
    "both": "双站点运营",
    "uae": "单站点 · 阿联酋",
    "ksa": "单站点 · 沙特",
}


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


def resolve_range(mode: str, day: str | None, month: str | None, start: str | None, end: str | None):
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
    """上一周期：等长前移一段；按月则取上一个自然月。"""
    if mode == "month":
        first = start.replace(day=1)
        last_prev = first - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    span = (end - start).days + 1
    return start - timedelta(days=span), end - timedelta(days=span)


def latest_date() -> date:
    with connect() as conn:
        row = conn.execute("SELECT MAX(biz_date) AS d FROM orders").fetchone()
    if row and row["d"]:
        return date.fromisoformat(row["d"])
    return date.today()


# --------------------------------------------------------------------------
# 基础聚合
# --------------------------------------------------------------------------
def totals(s: date, e: date) -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(qty), 0)                       AS qty,
                      COUNT(DISTINCT order_id)                   AS orders,
                      COUNT(DISTINCT shop)                       AS shops,
                      COUNT(DISTINCT operator)                   AS operators,
                      COUNT(DISTINCT biz_date)                   AS days
               FROM orders WHERE biz_date BETWEEN ? AND ?""",
            (s.isoformat(), e.isoformat()),
        ).fetchone()
    return dict(row)


def by_country(s: date, e: date) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT country, SUM(qty) AS qty, COUNT(DISTINCT order_id) AS orders
               FROM orders WHERE biz_date BETWEEN ? AND ?
               GROUP BY country ORDER BY qty DESC""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def by_team(s: date, e: date, scope: str = "all") -> list[dict]:
    """团队汇总。

    scope: all / uae / ksa（按国家截取）；both 对团队无意义，等同 all。
    """
    where = ""
    if scope == "uae":
        where = " AND r.country = 'UAE'"
    elif scope == "ksa":
        where = " AND r.country = 'KSA'"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(NULLIF(o.team, ''), '未分组') AS team,
                       SUM(r.qty) AS qty,
                       COUNT(DISTINCT r.order_id) AS orders,
                       COUNT(DISTINCT r.operator) AS operators,
                       SUM(CASE WHEN r.country = 'UAE' THEN r.qty ELSE 0 END) AS uae,
                       SUM(CASE WHEN r.country = 'KSA' THEN r.qty ELSE 0 END) AS ksa
                FROM orders r
                LEFT JOIN operators o ON o.code = r.operator
                WHERE r.biz_date BETWEEN ? AND ?{where}
                GROUP BY team
                HAVING SUM(r.qty) > 0
                ORDER BY qty DESC""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def by_operator(s: date, e: date, scope: str = "all") -> list[dict]:
    where = ""
    if scope == "uae":
        where = " AND r.country = 'UAE'"
    elif scope == "ksa":
        where = " AND r.country = 'KSA'"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT r.operator AS code,
                       COALESCE(NULLIF(o.name, ''), r.operator) AS name,
                       COALESCE(NULLIF(o.team, ''), '未分组')    AS team,
                       SUM(r.qty)                                AS qty,
                       COUNT(DISTINCT r.order_id)                AS orders,
                       SUM(CASE WHEN r.country = 'UAE' THEN r.qty ELSE 0 END) AS uae,
                       SUM(CASE WHEN r.country = 'KSA' THEN r.qty ELSE 0 END) AS ksa,
                       COUNT(DISTINCT r.biz_date)                AS active_days
                FROM orders r
                LEFT JOIN operators o ON o.code = r.operator
                WHERE r.biz_date BETWEEN ? AND ?{where}
                GROUP BY r.operator
                HAVING SUM(r.qty) > 0
                ORDER BY qty DESC""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    items = [dict(r) for r in rows]
    if scope == "both":
        # 「双站点运营」= 两个国家都有出单的人
        items = [it for it in items if it["uae"] > 0 and it["ksa"] > 0]
        items.sort(key=lambda it: -it["qty"])
    return items


def trend_series(end: date, days: int = 14) -> list[dict]:
    """以 end 结尾的近 N 天走势，供单日/短周期视图提供上下文。"""
    start = end - timedelta(days=days - 1)
    with connect() as conn:
        rows = conn.execute(
            """SELECT biz_date, SUM(qty) AS qty, COUNT(DISTINCT order_id) AS orders
               FROM orders WHERE biz_date BETWEEN ? AND ?
               GROUP BY biz_date ORDER BY biz_date""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def daily_series(s: date, e: date) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT biz_date, SUM(qty) AS qty, COUNT(DISTINCT order_id) AS orders
               FROM orders WHERE biz_date BETWEEN ? AND ?
               GROUP BY biz_date ORDER BY biz_date""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def by_hour(s: date, e: date) -> list[dict]:
    """24 小时 × 国家 的件数分布（按付款时间的小时切片）。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT substr(paid_at, 12, 2) AS hour, country, SUM(qty) AS qty
               FROM orders
               WHERE biz_date BETWEEN ? AND ? AND paid_at IS NOT NULL AND paid_at <> ''
               GROUP BY hour, country""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    grid = {f"{h:02d}": {"UAE": 0, "KSA": 0, "total": 0} for h in range(24)}
    for r in rows:
        h = r["hour"]
        if h not in grid:
            continue
        grid[h][r["country"]] = r["qty"]
        grid[h]["total"] += r["qty"]
    return [{"hour": h, **v} for h, v in sorted(grid.items())]


def by_shop(s: date, e: date, limit: int = 12) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT shop, country, SUM(qty) AS qty, COUNT(DISTINCT order_id) AS orders
               FROM orders WHERE biz_date BETWEEN ? AND ?
               GROUP BY shop ORDER BY qty DESC LIMIT ?""",
            (s.isoformat(), e.isoformat(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def accumulated() -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(qty), 0) AS qty, COUNT(DISTINCT order_id) AS orders,
                      MIN(biz_date) AS first_date, MAX(biz_date) AS last_date,
                      COUNT(DISTINCT biz_date) AS days
               FROM orders"""
        ).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------
def _pct(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def summary(mode: str, day: str | None, month: str | None, start: str | None, end: str | None,
            team_scope: str = "all") -> dict:
    s, e, label = resolve_range(mode, day, month, start, end)
    ps, pe = prev_range(s, e, mode)
    prev_label = f"{ps:%Y-%m-%d}" if ps == pe else f"{ps:%Y-%m-%d} ~ {pe:%Y-%m-%d}"

    cur = totals(s, e)
    prev = totals(ps, pe)

    countries = by_country(s, e)
    prev_countries = {r["country"]: r for r in by_country(ps, pe)}
    total_qty = cur["qty"] or 1
    for c in countries:
        c["label"] = COUNTRY_LABEL.get(c["country"], c["country"])
        c["share"] = round(c["qty"] / total_qty * 100, 1)
        p = prev_countries.get(c["country"], {})
        c["prev_qty"] = p.get("qty", 0)
        c["delta_pct"] = _pct(c["qty"], c["prev_qty"])

    teams = by_team(s, e, team_scope)
    prev_teams = {r["team"]: r["qty"] for r in by_team(ps, pe, team_scope)}
    team_total = sum(t["qty"] for t in teams) or 1
    for t in teams:
        t["prev_qty"] = prev_teams.get(t["team"], 0)
        t["delta_pct"] = _pct(t["qty"], t["prev_qty"])
        # 占比以「当前口径团队合计」为分母，"全部站点"时即等于大盘占比
        t["share"] = round(t["qty"] / team_total * 100, 1)

    # 排名变化（团队）
    teams_sorted = sorted(teams, key=lambda t: -t["qty"])
    prev_rank = {
        name: i + 1
        for i, (name, _) in enumerate(sorted(prev_teams.items(), key=lambda kv: -kv[1]))
    }
    for i, t in enumerate(teams_sorted):
        t["rank"] = i + 1
        t["rank_prev"] = prev_rank.get(t["team"])
        t["rank_delta"] = (t["rank_prev"] - t["rank"]) if t["rank_prev"] else None

    # 大盘主要变动国家：按 绝对增量 排序，找变化最大的
    mover = None
    if countries:
        mover = max(countries, key=lambda c: (c["qty"] - c["prev_qty"]))

    return {
        "range": {"start": s.isoformat(), "end": e.isoformat(), "label": label, "days": (e - s).days + 1},
        "compare": {"start": ps.isoformat(), "end": pe.isoformat(), "label": prev_label},
        "totals": cur,
        "prev": prev,
        "delta": {
            "qty": cur["qty"] - prev["qty"],
            "qty_pct": _pct(cur["qty"], prev["qty"]),
            "orders_pct": _pct(cur["orders"], prev["orders"]),
        },
        "by_country": countries,
        "by_team": teams_sorted,
        "team_scope": team_scope,
        "team_total": sum(t["qty"] for t in teams_sorted),
        "team_operators": sum(t["operators"] for t in teams_sorted),
        "daily": daily_series(s, e),
        "trend": trend_series(e, 14),
        "by_shop": by_shop(s, e, 12),
        "by_hour": by_hour(s, e),
        "accumulated": accumulated(),
        "main_mover": mover,
    }


def by_operator_country(s: date, e: date) -> dict[tuple[str, str], int]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT operator, country, SUM(qty) AS qty FROM orders
               WHERE biz_date BETWEEN ? AND ? GROUP BY operator, country""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    return {(r["operator"], r["country"]): r["qty"] for r in rows}


def operator_board(mode: str, day: str | None, month: str | None,
                   start: str | None, end: str | None, scope: str = "all") -> dict:
    s, e, label = resolve_range(mode, day, month, start, end)
    ps, pe = prev_range(s, e, mode)
    span = (e - s).days + 1

    cur = by_operator(s, e, scope)
    prev = by_operator(ps, pe, scope)
    prev_map = {it["code"]: it for it in prev}

    # 排名对比
    prev_rank = {it["code"]: i + 1 for i, it in enumerate(sorted(prev, key=lambda x: -x["qty"]))}

    # 与旧看板一致的异常阈值：当日全体运营“环比差值”绝对值的平均
    deltas = []
    for it in cur:
        p = prev_map.get(it["code"])
        if p:
            deltas.append(abs(it["qty"] - p["qty"]))
    threshold = round(sum(deltas) / len(deltas), 1) if deltas else 0.0

    items = []
    prev_split = by_operator_country(ps, pe)
    cur_split = by_operator_country(s, e)
    for i, it in enumerate(cur):
        p = prev_map.get(it["code"], {})
        prev_qty = p.get("qty", 0)
        delta = it["qty"] - prev_qty

        # 主要增减国家：取该运营两国增量里绝对值最大的那个
        country_moves = []
        for cc in ("UAE", "KSA"):
            d = cur_split.get((it["code"], cc), 0) - prev_split.get((it["code"], cc), 0)
            country_moves.append({"country": cc, "label": COUNTRY_LABEL[cc], "delta": d})
        main_move = max(country_moves, key=lambda m: abs(m["delta"]))

        items.append({
            **it,
            "rank": i + 1,
            "prev_qty": prev_qty,
            "delta": delta,
            "delta_pct": _pct(it["qty"], prev_qty),
            "rank_prev": prev_rank.get(it["code"]),
            "rank_delta": (prev_rank.get(it["code"]) - (i + 1)) if prev_rank.get(it["code"]) else None,
            "avg_per_day": round(it["qty"] / span, 1),
            "share": round(it["qty"] / (cur[0]["qty"] or 1) * 100, 1),
            "is_anomaly": bool(threshold and abs(delta) > threshold),
            "main_move": main_move,
        })

    return {
        "scope": scope,
        "scope_label": SCOPE_LABEL.get(scope, scope),
        "range": {"start": s.isoformat(), "end": e.isoformat(), "label": label, "days": span},
        "compare": {"start": ps.isoformat(), "end": pe.isoformat()},
        "threshold": threshold,
        "items": items,
        "operator_count": len(items),
    }


def operator_daily(code: str) -> list[dict]:
    """单个运营的每日走势（含国家拆分），用于明细抽屉。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT biz_date,
                      SUM(qty) AS qty,
                      SUM(CASE WHEN country = 'UAE' THEN qty ELSE 0 END) AS uae,
                      SUM(CASE WHEN country = 'KSA' THEN qty ELSE 0 END) AS ksa,
                      COUNT(DISTINCT order_id) AS orders
               FROM orders WHERE operator = ?
               GROUP BY biz_date ORDER BY biz_date""",
            (code,),
        ).fetchall()
    out = []
    prev = None
    for r in rows:
        item = dict(r)
        item["delta"] = (item["qty"] - prev) if prev is not None else None
        prev = item["qty"]
        out.append(item)
    return out


def export_orders(s: date, e: date) -> list[tuple]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT biz_date, order_id, shop, country, operator, sku, qty, paid_at, status
               FROM orders WHERE biz_date BETWEEN ? AND ?
               ORDER BY biz_date, paid_at""",
            (s.isoformat(), e.isoformat()),
        ).fetchall()
    return [tuple(r) for r in rows]


def meta() -> dict:
    with connect() as conn:
        dates = [r["biz_date"] for r in conn.execute(
            "SELECT DISTINCT biz_date FROM orders ORDER BY biz_date"
        ).fetchall()]
        months = [r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(biz_date, 1, 7) AS m FROM orders ORDER BY m"
        ).fetchall()]
        teams = [r["t"] for r in conn.execute(
            "SELECT DISTINCT team AS t FROM operators WHERE team IS NOT NULL AND team <> '' ORDER BY team"
        ).fetchall()]
        # 同一天重复导入只保留最新一条，日志更好读
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
        active_ops = conn.execute("SELECT COUNT(DISTINCT operator) AS c FROM orders").fetchone()["c"]
        active_teams = conn.execute(
            """SELECT COUNT(DISTINCT COALESCE(NULLIF(o.team, ''), '未分组')) AS c
               FROM orders r LEFT JOIN operators o ON o.code = r.operator"""
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
