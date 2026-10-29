"""预聚合维护脚本。

用法::

    python -m app.rebuild_agg              # 全量重建 agg_daily
    python -m app.rebuild_agg 2026-10-27   # 只重建某一天
    python -m app.rebuild_agg --check      # 校验预聚合与明细是否一致

导入订单时会自动重算当天，日常无需手动执行；仅在排查问题或迁移老库时用。
"""

from __future__ import annotations

import sys
import time

from . import db


def check() -> bool:
    """逐日对比 agg_daily 与 orders 的合计，返回是否一致。"""
    with db.read_conn() as conn:
        rows = conn.execute(
            """SELECT a.biz_date,
                      a.sq AS agg_qty, o.sq AS raw_qty,
                      a.cnt AS agg_rows, o.cnt AS raw_rows
               FROM (SELECT biz_date, SUM(qty) sq, COUNT(*) cnt FROM agg_daily GROUP BY biz_date) a
               JOIN (SELECT biz_date, SUM(qty) sq, COUNT(*) cnt FROM orders GROUP BY biz_date) o
                 USING (biz_date)
               ORDER BY a.biz_date"""
        ).fetchall()

    ok = True
    if not rows:
        print("两个表都没有数据")
        return True

    print(f"{'日期':<12}{'agg件数':>10}{'明细件数':>10}{'一致':>8}")
    for r in rows:
        same = r["agg_qty"] == r["raw_qty"]
        ok = ok and same
        print(f"{r['biz_date']:<12}{r['agg_qty']:>10}{r['raw_qty']:>10}{'✓' if same else '✗':>8}")

    with db.read_conn() as conn:
        # 明细里有、聚合里没有的日期
        missing = [r["biz_date"] for r in conn.execute(
            """SELECT DISTINCT biz_date FROM orders
               WHERE biz_date NOT IN (SELECT DISTINCT biz_date FROM agg_daily)"""
        ).fetchall()]
    if missing:
        ok = False
        print(f"\n缺少聚合的日期: {', '.join(missing)}")

    print("\n结果:", "一致" if ok else "不一致，请执行 python -m app.rebuild_agg")
    return ok


def main(argv: list[str]) -> int:
    db.init_db()   # 顺带补列与初始化

    if "--check" in argv:
        return 0 if check() else 1

    target = next((a for a in argv if not a.startswith("-")), None)
    t0 = time.perf_counter()
    total = db.rebuild_agg(target)
    ms = (time.perf_counter() - t0) * 1000

    scope = f"日期 {target}" if target else "全量"
    print(f"已重建 agg_daily（{scope}），当前共 {total} 行，耗时 {ms:.0f} ms")
    check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
