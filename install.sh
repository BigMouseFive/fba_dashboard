#!/bin/bash
# =============================================================
# FBA 订单数据对比看板 · macOS 一键安装
#   依赖：uv（推荐）或 python3.11+；使用 launchd 开机自启
# 用法：./install.sh            安装并启动
#       ./install.sh --uninstall 停止并卸载自启
# =============================================================
set -euo pipefail

APP_NAME="fba-dashboard"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
PLIST_LABEL="com.wenwendemac.fba-dashboard"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PORT="${FBA_PORT:-8770}"
UV_CACHE_DIR="${UV_CACHE_DIR:-$PROJECT_DIR/../.uvcache}"

uninstall() {
  echo "→ 停止并卸载 ${PLIST_LABEL}"
  launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || launchctl unload "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "✓ 已卸载（数据仍保留在 $PROJECT_DIR/data）"
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

echo "=========================================="
echo " FBA 订单数据对比看板 · 安装"
echo "=========================================="

# 1. Python 环境（优先 uv）
if command -v uv >/dev/null 2>&1; then
  echo "✓ 检测到 uv: $(uv --version)"
  mkdir -p "$UV_CACHE_DIR"
  cd "$PROJECT_DIR"
  if [ ! -d "$VENV_DIR" ]; then
    echo "→ 创建虚拟环境 (.venv)"
    UV_CACHE_DIR="$UV_CACHE_DIR" uv venv --python 3.13 "$VENV_DIR"
  fi
  echo "→ 同步依赖 (uv sync)"
  UV_CACHE_DIR="$UV_CACHE_DIR" uv sync
elif command -v python3 >/dev/null 2>&1; then
  echo "! 未找到 uv，退回到 venv + pip"
  if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install -q --upgrade pip
  "$VENV_DIR/bin/pip" install -q fastapi "uvicorn[standard]" openpyxl python-multipart
else
  echo "错误：需要 uv 或 python3.11+，请先安装：brew install uv"
  exit 1
fi

# 2. 目录
mkdir -p "$LOG_DIR" "$PROJECT_DIR/data"
echo "✓ 数据目录: $PROJECT_DIR/data"

# 3. LaunchAgent（开机自启 + 崩溃自动拉起）
echo "→ 写入 LaunchAgent: $PLIST_PATH"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>app.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>${PORT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>FBA_DATA_DIR</key>
        <string>${PROJECT_DIR}/data</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>
</dict>
</plist>
EOF

# 4. 启动
launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH"
sleep 2

IP="$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1)"
echo ""
echo "=========================================="
echo " 安装完成"
echo "   本机:   http://127.0.0.1:${PORT}"
echo "   局域网: http://${IP}:${PORT}"
echo "   日志:   ${LOG_DIR}/stderr.log"
echo "   卸载:   ./install.sh --uninstall"
echo "=========================================="
curl -s -m 3 "http://127.0.0.1:${PORT}/healthz" || echo "(服务启动中，稍等几秒再刷新)"
echo ""
