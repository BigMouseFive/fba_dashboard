#!/bin/bash
# 前台调试运行（开发用）
cd "$(dirname "$0")"
FBA_PORT="${FBA_PORT:-8770}" exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${FBA_PORT}" --reload
