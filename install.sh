#!/bin/bash
# =============================================================
#  FBA 订单数据对比看板 · macOS 安装脚本
#
#  用 uv 创建独立的 Python 虚拟环境（.venv），并注册为 launchd
#  开机自启服务（崩溃会自动拉起）。
#
#  用法：
#    ./install.sh               安装并启动（默认端口 8770）
#    ./install.sh --status      查看运行状态
#    ./install.sh --restart     重启服务
#    ./install.sh --logs        实时查看日志
#    ./install.sh --uninstall   停止并卸载开机自启（保留数据）
#
#  可选环境变量：
#    FBA_PORT=8770              监听端口
#    FBA_LABEL=com.wenwendemac.fba-dashboard   launchd 服务标识
# =============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
DATA_DIR="$PROJECT_DIR/data"
PORT="${FBA_PORT:-8770}"
LABEL="${FBA_LABEL:-com.wenwendemac.fba-dashboard}"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_NOW="$(id -u)"

# 若已安装，以 plist 里记录的端口为准（避免 --status 与实际不符）
if [ -z "${FBA_PORT:-}" ] && [ -f "$PLIST_PATH" ]; then
  _p="$(awk '/--port/{getline; gsub(/<\/?string>|[[:space:]]/,""); print; exit}' "$PLIST_PATH" 2>/dev/null || true)"
  [ -n "${_p:-}" ] && PORT="$_p"
fi

c_green() { printf "\033[32m%s\033[0m\n" "$1"; }
c_blue()  { printf "\033[34m%s\033[0m\n" "$1"; }
c_warn()  { printf "\033[33m%s\033[0m\n" "$1"; }
c_err()   { printf "\033[31m%s\033[0m\n" "$1" >&2; }

# ---------- 子命令 ----------
case "${1:-}" in
  --uninstall)
    echo "→ 停止并卸载 ${LABEL}"
    launchctl bootout "gui/${UID_NOW}/${LABEL}" 2>/dev/null \
      || launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    c_green "✓ 已卸载开机自启（数据仍保留在 ${DATA_DIR}）"
    echo "  如需彻底删除，直接删掉项目目录即可"
    exit 0
    ;;
  --status)
    echo "服务标识: ${LABEL}"
    echo "端口:     ${PORT}"
    if launchctl print "gui/${UID_NOW}/${LABEL}" >/dev/null 2>&1; then
      state="$(launchctl print "gui/${UID_NOW}/${LABEL}" 2>/dev/null | awk -F'= ' '/^\tstate/{print $2; exit}')"
      pid="$(launchctl print "gui/${UID_NOW}/${LABEL}" 2>/dev/null | awk -F'= ' '/^\tpid/{print $2; exit}')"
      c_green "✓ launchd 状态: ${state:-unknown}${pid:+ (pid ${pid})}"
      code="$(curl -s -o /dev/null -w '%{http_code}' -m 4 "http://127.0.0.1:${PORT}/healthz" || true)"
      if [ "$code" = "200" ]; then c_green "✓ 健康检查: HTTP 200"; else c_warn "! 健康检查: ${code:-无响应}"; fi
      ip="$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1)"
      echo "  本机:   http://127.0.0.1:${PORT}"
      echo "  局域网: http://${ip}:${PORT}"
    else
      c_warn "! 未安装为 launchd 服务"
      echo "  运行 ./install.sh 安装"
    fi
    exit 0
    ;;
  --restart)
    [ -f "$PLIST_PATH" ] || { c_err "错误：还没安装，先运行 ./install.sh"; exit 1; }
    echo "→ 重启 ${LABEL}"
    launchctl kickstart -k "gui/${UID_NOW}/${LABEL}"
    sleep 2
    c_green "✓ 已重启"
    exec "$0" --status
    ;;
  --logs)
    [ -f "${LOG_DIR}/stderr.log" ] || { c_err "错误：日志还不存在，先运行 ./install.sh"; exit 1; }
    exec tail -f "${LOG_DIR}/stderr.log"
    ;;
esac

echo "=========================================="
echo "  FBA 订单数据对比看板 · 安装"
echo "=========================================="
echo "项目目录: ${PROJECT_DIR}"
echo "端口:     ${PORT}"
echo ""

# ---------- 1. 前置检查 ----------
if [ "$(uname -s)" != "Darwin" ]; then
  c_err "错误：此脚本仅适用于 macOS（依赖 launchd 开机自启）"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  c_err "错误：未找到 uv"
  echo ""
  echo "请先安装 uv（推荐）："
  echo "  brew install uv"
  echo "或："
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
c_green "✓ uv $(uv --version | awk '{print $2}')"

if [ -e "/tmp/.fba-port-check" ]; then rm -f "/tmp/.fba-port-check"; fi
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  # 已安装时端口被自己占用是正常的，重启流程不受影响
  if ! launchctl print "gui/${UID_NOW}/${LABEL}" >/dev/null 2>&1; then
    c_err "错误：端口 ${PORT} 已被其他程序占用"
    echo "用 FBA_PORT=8771 ./install.sh 换一个端口"
    exit 1
  fi
fi

# ---------- 2. uv 虚拟环境 ----------
cd "$PROJECT_DIR"

if [ -d "$VENV_DIR" ]; then
  c_green "✓ 虚拟环境已存在：.venv"
else
  c_blue "→ 创建虚拟环境（uv venv）"
  uv venv --python 3.13 "$VENV_DIR"
fi

c_blue "→ 同步依赖（uv sync，按 uv.lock 精确安装）"
uv sync --frozen 2>/dev/null || uv sync

if [ ! -x "$PYTHON_BIN" ]; then
  c_err "错误：虚拟环境创建失败，${PYTHON_BIN} 不存在"
  exit 1
fi
c_green "✓ Python $("$PYTHON_BIN" -V 2>&1 | awk '{print $2}')"

# 依赖自检，避免服务起来才发现缺包
if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, openpyxl, multipart" >/dev/null 2>&1; then
  c_err "错误：依赖不完整，请手动执行：uv sync"
  exit 1
fi
c_green "✓ 依赖自检通过"

# ---------- 3. 目录 ----------
mkdir -p "$LOG_DIR" "$DATA_DIR"
c_green "✓ 数据目录：${DATA_DIR}"

# ---------- 4. LaunchAgent ----------
c_blue "→ 写入 LaunchAgent：${PLIST_PATH}"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

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
        <string>--no-access-log</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>FBA_DATA_DIR</key>
        <string>${DATA_DIR}</string>
        <key>FBA_PORT</key>
        <string>${PORT}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF
c_green "✓ LaunchAgent 已写入"

# ---------- 5. 启动 ----------
c_blue "→ 启动服务"

# 先彻底停掉旧实例：bootout 是异步的，必须等它真正退出，
# 否则紧接着的 bootstrap 会因为"服务已存在"而静默失败。
launchctl bootout "gui/${UID_NOW}/${LABEL}" 2>/dev/null || true
for i in $(seq 1 20); do
  launchctl print "gui/${UID_NOW}/${LABEL}" >/dev/null 2>&1 || break
  sleep 0.5
done

boot_out="$(launchctl bootstrap "gui/${UID_NOW}" "$PLIST_PATH" 2>&1 || true)"
if [ -n "$boot_out" ]; then
  c_warn "! bootstrap 返回：${boot_out}"
  launchctl load -w "$PLIST_PATH" 2>&1 || true
fi

# 最多等 20 秒，轮询健康检查
ok=0
for i in $(seq 1 20); do
  sleep 1
  if curl -s -m 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then ok=1; break; fi
done

IP="$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1)"
echo ""
echo "=========================================="
if [ "$ok" = "1" ]; then
  c_green "  安装完成，服务已在运行"
else
  c_warn "  服务已注册，但健康检查未通过"
  echo "  查看日志：./install.sh --logs"
fi
echo "=========================================="
echo "  本机:   http://127.0.0.1:${PORT}"
echo "  局域网: http://${IP}:${PORT}"
echo ""
echo "  状态: ./install.sh --status"
echo "  日志: ./install.sh --logs"
echo "  重启: ./install.sh --restart"
echo "  卸载: ./install.sh --uninstall"
echo "=========================================="
echo ""

[ "$ok" = "1" ] || exit 1
