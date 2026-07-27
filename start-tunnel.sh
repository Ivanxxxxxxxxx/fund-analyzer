#!/bin/bash
# ==========================================================================
#  基金组合健康度分析 —— 本机 + cloudflared 隧道 一键启动
#  作用：把本机 localhost:8000 暴露成公网 https 网址，手机/任意设备可访问
#  适用：macOS（Linux 同样可用）
#  注意：
#    - 运行本脚本时 Mac 必须保持开机且此终端不关闭
#    - 关闭脚本（Ctrl+C）后隧道断开，公网网址失效
#    - quick tunnel 网址每次启动都会变；想固定网址见文末「进阶」
# ==========================================================================
set -e

PORT=8000
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================================"
echo " 基金分析工具 · 公网隧道启动"
echo "============================================================"

# 0. 检查 app.py 存在
if [ ! -f "$SCRIPT_DIR/app.py" ]; then
  echo "✗ 错误：未在 $SCRIPT_DIR 找到 app.py，请在本项目目录内运行本脚本"
  exit 1
fi

# 1. 检查 python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ 未检测到 python3。请先安装："
  echo "    brew install python3   （或到 python.org 下载 macOS 安装包）"
  exit 1
fi

# 2. 检查 / 安装 cloudflared
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "→ 未检测到 cloudflared，尝试自动安装…"
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflared
  else
    echo "✗ 未安装 Homebrew，无法自动安装 cloudflared。请二选一："
    echo "  a) 先装 Homebrew："
    echo "     /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "     然后再运行本脚本"
    echo "  b) 手动下载（无 brew 也行）："
    echo "     打开 https://github.com/cloudflare/cloudflared/releases"
    echo "     下载 cloudflared-darwin-amd64 重命名为 cloudflared，"
    echo "     chmod +x 后放到 /usr/local/bin/"
    exit 1
  fi
fi

# 3. 启动本地 app.py（后台）
echo "→ 启动本地服务 app.py (端口 $PORT) …"
cd "$SCRIPT_DIR"
nohup python3 app.py > /tmp/fund-analyzer.log 2>&1 &
APP_PID=$!
echo "   app.py PID=$APP_PID，日志：/tmp/fund-analyzer.log"

# 等几秒让服务就绪
sleep 3
if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "✓ 本地服务已就绪 (http://localhost:$PORT/)"
else
  echo "⚠ 本地服务似乎未就绪，请检查：tail -f /tmp/fund-analyzer.log"
fi

# 4. 退出时清理 app.py 进程
cleanup() {
  kill "$APP_PID" 2>/dev/null || true
  echo ""
  echo "已关闭隧道与本地 app.py 服务。"
}
trap cleanup EXIT

# 5. 启动 cloudflared 隧道（前台阻塞，Ctrl+C 退出）
echo "------------------------------------------------------------"
echo "→ 正在建立公网隧道… 稍等几秒，下面会打印一个 https://xxxx.trycloudflare.com 网址"
echo "   手机/任意设备用该网址访问即可。"
echo "   按 Ctrl+C 关闭（关闭后公网网址失效）。"
echo "------------------------------------------------------------"
cloudflared tunnel --url "http://localhost:$PORT"
