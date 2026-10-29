"""Excel 导入：FBA 订单主表解析 + 运营代号记录解析。

口径（与浏览器端旧看板保持一致）：
    只读取订单文件中的「主表」sheet，按 商品数量 求和。
    文件名 `20261027 FBA订单.xlsx` 中的 20261027 即业务日期；
    同一天重复导入会整日覆盖，不会翻倍。
"""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
import zipfile

from .db import DATA_DIR, now_iso, write_conn

UPLOAD_DIR = DATA_DIR / "uploads"
RECYCLE_DIR = DATA_DIR / "replaced"

# 列名 -> 内部字段（容忍常见别名与空格）
COLUMN_ALIASES = {
    "order_id": ("订单编号", "订单号", "order id", "amazon-order-id"),
    "trade_id": ("交易编号", "交易号"),
    "status": ("平台订单状态", "订单状态", "状态"),
    "country": ("国家", "站点", "国家/地区"),
    "shop": ("店铺名", "店铺", "店铺名称"),
    "paid_at": ("付款时间", "支付时间", "下单时间"),
    "platform_sku": ("平台SKU", "平台sku", "平台 SKU"),
    "sku": ("SKU", "sku", "MSKU"),
    "operator": ("运营", "运营代号", "负责人"),
    "qty": ("商品数量", "数量", "件数"),
}

COUNTRY_MAP = {
    "united arab emirates": "UAE",
    "uae": "UAE",
    "ae": "UAE",
    "阿联酋": "UAE",
    "迪拜": "UAE",
    "saudi arabia": "KSA",
    "ksa": "KSA",
    "sa": "KSA",
    "沙特": "KSA",
    "沙特阿拉伯": "KSA",
}

COUNTRY_LABEL = {
    "UAE": "United Arab Emirates",
    "KSA": "Saudi Arabia",
}

DATE_IN_NAME = re.compile(r"(20\d{2})(\d{2})(\d{2})")
SHEET_MMDD = re.compile(r"^\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})\s*日?\s*$")


class ImportError_(Exception):
    """导入失败，message 直接展示给用户。"""


def _norm_col(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _resolve_header(header_row) -> dict[str, int]:
    """把表头行映射成 {内部字段: 列索引}。"""
    found: dict[str, int] = {}
    cells = [_norm_col(c) for c in header_row]
    for field, aliases in COLUMN_ALIASES.items():
        normalized_aliases = {_norm_col(a) for a in aliases}
        for idx, cell in enumerate(cells):
            if cell in normalized_aliases and field not in found:
                found[field] = idx
                break
    return found


def _pick_main_sheet(wb, filename_date: str | None):
    """挑选主表 sheet。

    规则：优先取 sheet 名形如 `10.27` 且与文件名日期吻合的表；
    否则取含关键列且行数最多的表。HWC / SFHB 等补充表一律忽略。
    """
    candidates = []
    for ws in wb.worksheets:
        header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        cols = _resolve_header(header) if header else {}
        if "order_id" not in cols or "qty" not in cols:
            continue
        candidates.append((ws, cols, ws.max_row))
    if not candidates:
        raise ImportError_("文件里没有找到包含「订单编号 / 商品数量」列的工作表，请确认是 FBA 订单导出表。")

    def score(item):
        ws, _, rows = item
        m = SHEET_MMDD.match(ws.title or "")
        matched = bool(m and filename_date and f"{filename_date[5:7]}.{str(int(filename_date[8:10]))}" == f"{int(m.group(1))}.{int(m.group(2))}")
        return (matched, rows)

    ws, cols, _ = max(candidates, key=score)
    return ws, cols


def _parse_name_date(filename: str) -> str | None:
    stem = Path(filename).stem
    m = DATE_IN_NAME.search(stem)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _sheet_date(ws_title: str, fallback_year: int) -> str | None:
    m = SHEET_MMDD.match(ws_title or "")
    if not m:
        return None
    try:
        return date(fallback_year, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def _norm_country(raw) -> str:
    key = str(raw or "").strip().lower()
    if key in COUNTRY_MAP:
        return COUNTRY_MAP[key]
    if "emirat" in key or "uae" in key:
        return "UAE"
    if "saudi" in key or "ksa" in key:
        return "KSA"
    return str(raw or "UNKNOWN").strip() or "UNKNOWN"


def _hour_of(paid_at: str) -> int | None:
    """从 'YYYY-MM-DD HH:MM:SS' 取小时；格式不对返回 None。"""
    if len(paid_at) >= 13 and paid_at[10] in " T":
        try:
            h = int(paid_at[11:13])
            return h if 0 <= h <= 23 else None
        except ValueError:
            return None
    return None


def _to_qty(value) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _open_workbook(path: Path):
    """统一把「不是有效 xlsx」的异常翻译成友好提示。"""
    try:
        return load_workbook(path, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ImportError_("文件不是有效的 .xlsx，请确认是 Excel 导出的原始文件（不要改后缀）。") from exc


def parse_orders_file(path: Path, original_name: str) -> dict:
    """解析订单 Excel，返回待写入的行数据与统计。"""
    biz_date = _parse_name_date(original_name)
    wb = _open_workbook(path)
    try:
        year = int(biz_date[:4]) if biz_date else datetime.now().year
        ws, cols = _pick_main_sheet(wb, biz_date)

        sheet_date = _sheet_date(ws.title, year)
        if not biz_date:
            biz_date = sheet_date
        if not biz_date:
            raise ImportError_(
                "无法识别业务日期：文件名需形如 `20261027 FBA订单.xlsx`，或主表 sheet 名为 `10.27`。"
            )

        rows: list[tuple] = []
        total_qty = 0
        shops: set[str] = set()
        operators: set[str] = set()
        min_paid, max_paid = None, None

        for raw in ws.iter_rows(min_row=2, values_only=True):
            if raw is None:
                continue
            def cell(field):
                idx = cols.get(field)
                if idx is None or idx >= len(raw):
                    return None
                return raw[idx]

            order_id = _to_text(cell("order_id"))
            if not order_id:
                continue
            qty = _to_qty(cell("qty"))
            paid_at = _to_text(cell("paid_at"))
            country = _norm_country(cell("country"))
            shop = _to_text(cell("shop"))
            operator = _to_text(cell("operator"))

            total_qty += qty
            if shop:
                shops.add(shop)
            if operator:
                operators.add(operator)
            if paid_at:
                if min_paid is None or paid_at < min_paid:
                    min_paid = paid_at
                if max_paid is None or paid_at > max_paid:
                    max_paid = paid_at

            rows.append(
                (
                    biz_date,
                    order_id,
                    _to_text(cell("trade_id")),
                    _to_text(cell("status")),
                    country,
                    shop,
                    paid_at,
                    _hour_of(paid_at),
                    _to_text(cell("platform_sku")),
                    _to_text(cell("sku")),
                    operator,
                    qty,
                    original_name,
                )
            )

        if not rows:
            raise ImportError_("主表里没有解析到有效数据行。")

        return {
            "biz_date": biz_date,
            "sheet": ws.title,
            "rows": rows,
            "row_count": len(rows),
            "total_qty": total_qty,
            "shops": len(shops),
            "operators": len(operators),
            "paid_range": (min_paid, max_paid),
        }
    finally:
        wb.close()


def save_upload(tmp_path: Path, original_name: str) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / original_name
    shutil.copyfile(tmp_path, target)
    return target


def import_orders(path: Path, original_name: str) -> dict:
    parsed = parse_orders_file(path, original_name)
    biz_date = parsed["biz_date"]
    ts = now_iso()

    with write_conn() as conn:
        # 整日覆盖：避免重复上传同一天造成数据翻倍
        conn.execute("DELETE FROM orders WHERE biz_date = ?", (biz_date,))
        conn.executemany(
            """INSERT INTO orders
               (biz_date, order_id, trade_id, status, country, shop, paid_at, hour,
                platform_sku, sku, operator, qty, source_file, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [row + (ts,) for row in parsed["rows"]],
        )
        # 同一事务内重算当天预聚合：要么都成功，要么都不生效
        conn.execute("DELETE FROM agg_daily WHERE biz_date = ?", (biz_date,))
        conn.execute(
            """INSERT INTO agg_daily (biz_date, country, shop, operator, hour, qty, orders)
               SELECT biz_date, country, COALESCE(shop, ''), COALESCE(operator, ''),
                      COALESCE(hour, -1), SUM(qty), COUNT(DISTINCT order_id)
               FROM orders WHERE biz_date = ?
               GROUP BY biz_date, country, shop, operator, hour""",
            (biz_date,),
        )
        conn.execute(
            """INSERT INTO import_batches
               (kind, biz_date, filename, rows, qty, shops, operators, imported_at, note)
               VALUES ('orders', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz_date,
                original_name,
                parsed["row_count"],
                parsed["total_qty"],
                parsed["shops"],
                parsed["operators"],
                ts,
                f"主表 sheet: {parsed['sheet']}",
            ),
        )

    return {
        "biz_date": biz_date,
        "sheet": parsed["sheet"],
        "row_count": parsed["row_count"],
        "total_qty": parsed["total_qty"],
        "shops": parsed["shops"],
        "operators": parsed["operators"],
        "paid_range": parsed["paid_range"],
    }


def parse_operators_file(path: Path) -> list[tuple]:
    wb = _open_workbook(path)
    try:
        ws = wb.worksheets[0]
        cols = _resolve_header(next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None) or ())
        header = [_to_text(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ()) or ()]
        # 运营代号记录表头固定为 运营代号 / 运营姓名 / 团队名 / 主管
        idx = {name: i for i, name in enumerate(header) if name}
        need = {"运营代号": "code", "运营姓名": "name", "团队名": "team", "主管": "supervisor"}
        mapping = {field: idx[cn] for cn, field in need.items() if cn in idx}
        if len(mapping) < 3:
            raise ImportError_("运营代号记录表头需包含：运营代号 / 运营姓名 / 团队名 / 主管。")

        out: list[tuple] = []
        seen: set[str] = set()
        for raw in ws.iter_rows(min_row=2, values_only=True):
            if not raw:
                continue
            def cell(field):
                i = mapping.get(field)
                return _to_text(raw[i]) if i is not None and i < len(raw) else ""

            code = cell("code")
            if not code or code in seen:
                continue
            seen.add(code)
            out.append((code, cell("name"), cell("team"), cell("supervisor")))
        if not out:
            raise ImportError_("运营代号记录里没有读到数据行。")
        return out
    finally:
        wb.close()


def import_operators(path: Path, original_name: str) -> dict:
    rows = parse_operators_file(path)
    ts = now_iso()
    with write_conn() as conn:
        conn.execute("DELETE FROM operators")
        conn.executemany(
            "INSERT INTO operators(code, name, team, supervisor, updated_at) VALUES (?,?,?,?,?)",
            [row + (ts,) for row in rows],
        )
        conn.execute(
            """INSERT INTO import_batches
               (kind, biz_date, filename, rows, qty, shops, operators, imported_at, note)
               VALUES ('operators', NULL, ?, ?, 0, 0, ?, ?, ?)""",
            (original_name, len(rows), len(rows), ts, "运营-团队映射"),
        )
    return {"operators": len(rows)}
