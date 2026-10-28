"""FastAPI 入口：上传、统计接口、CSV 导出、静态看板。"""

from __future__ import annotations

import csv
import io
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, ingest
from .db import BASE_DIR, init_db

APP_TITLE = "FBA 订单数据对比看板"
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

app = FastAPI(title=APP_TITLE, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------
@app.post("/api/upload/orders")
async def upload_orders(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "只支持 .xlsx 订单文件")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(413, "文件超过 200MB")
            tmp.write(chunk)
    try:
        saved = ingest.save_upload(tmp_path, name)
        result = ingest.import_orders(saved, name)
    except ingest.ImportError_ as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # 解析期异常统一转成可读错误
        raise HTTPException(400, f"解析失败：{exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"ok": True, "kind": "orders", **result}


@app.post("/api/upload/operators")
async def upload_operators(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "只支持 .xlsx 文件")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        saved = ingest.save_upload(tmp_path, name)
        result = ingest.import_operators(saved, name)
    except ingest.ImportError_ as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"解析失败：{exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"ok": True, "kind": "operators", **result}


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def _range_args(mode: str, day: str | None, month: str | None, start: str | None, end: str | None):
    try:
        analytics.resolve_range(mode, day, month, start, end)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"日期参数不合法：{exc}") from exc


@app.get("/api/meta")
def api_meta():
    return {**analytics.meta(), "today": date.today().isoformat()}


@app.get("/api/summary")
def api_summary(
    mode: str = Query("day", pattern="^(day|week|month|custom)$"),
    day: str | None = None,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    team_scope: str = Query("all", pattern="^(all|both|uae|ksa)$"),
):
    _range_args(mode, day, month, start, end)
    return analytics.summary(mode, day, month, start, end, team_scope)


@app.get("/api/operators")
def api_operators(
    mode: str = Query("day", pattern="^(day|week|month|custom)$"),
    day: str | None = None,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    scope: str = Query("all", pattern="^(all|both|uae|ksa)$"),
):
    _range_args(mode, day, month, start, end)
    return analytics.operator_board(mode, day, month, start, end, scope)


@app.get("/api/operators/{code}/daily")
def api_operator_daily(code: str):
    rows = analytics.operator_daily(code)
    if not rows:
        raise HTTPException(404, "没有该运营的数据")
    return {"code": code, "items": rows}


@app.get("/api/export.csv")
def api_export(
    kind: str = Query("operators", pattern="^(operators|teams|daily|orders)$"),
    mode: str = Query("day", pattern="^(day|week|month|custom)$"),
    day: str | None = None,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
):
    _range_args(mode, day, month, start, end)
    s, e, label = analytics.resolve_range(mode, day, month, start, end)

    buf = io.StringIO()
    writer = csv.writer(buf)
    if kind == "orders":
        writer.writerow(["业务日期", "订单编号", "店铺", "国家", "运营", "SKU", "商品数量", "付款时间", "状态"])
        for row in analytics.export_orders(s, e):
            writer.writerow(row)
    elif kind == "teams":
        board = analytics.summary(mode, day, month, start, end)["by_team"]
        writer.writerow(["排名", "团队", "今日/区间总量", "上一周期", "环比%", "占比%", "运营人数"])
        for t in board:
            writer.writerow([t["rank"], t["team"], t["qty"], t["prev_qty"],
                             t["delta_pct"] if t["delta_pct"] is not None else "",
                             t["share"], t["operators"]])
    elif kind == "daily":
        writer.writerow(["业务日期", "商品数量", "订单数"])
        for d in analytics.daily_series(s, e):
            writer.writerow([d["biz_date"], d["qty"], d["orders"]])
    else:
        board = analytics.operator_board(mode, day, month, start, end, "all")
        writer.writerow(["排名", "运营代号", "姓名", "团队", "总量", "阿联酋", "沙特",
                         "上一周期", "环比%", "环比差值", "排名变化"])
        for it in board["items"]:
            writer.writerow([it["rank"], it["code"], it["name"], it["team"], it["qty"],
                             it["uae"], it["ksa"], it["prev_qty"],
                             it["delta_pct"] if it["delta_pct"] is not None else "",
                             it["delta"], it["rank_delta"] if it["rank_delta"] is not None else ""])

    data = "\ufeff" + buf.getvalue()  # BOM 让 Excel 正确识别 UTF-8
    filename = f"FBA_{kind}_{s:%Y%m%d}_{e:%Y%m%d}.csv"
    return StreamingResponse(
        iter([data.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# 静态资源
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "service": "fba-dashboard"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    host = os.environ.get("FBA_HOST", "0.0.0.0")
    port = int(os.environ.get("FBA_PORT", "8770"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
