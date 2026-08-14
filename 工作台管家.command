#!/bin/bash
# ============================================================
#  剧多多文生图工作台 · 管家（双击运行）
#  一个入口包含全部功能：启动 / 关闭 / 打开页面 / 登录 / 打包 / 首次安装
#  首次运行（没装过环境）会自动进入安装向导。
# ============================================================
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.cherrystudio/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
VER=$(tr -d '[:space:]' < VERSION 2>/dev/null || echo "?")
ADDR="http://127.0.0.1:8321"

is_running() { lsof -nP -iTCP:8321 -sTCP:LISTEN >/dev/null 2>&1; }

has_uv() { command -v uv >/dev/null 2>&1; }

# ---------- ① 启动 ----------
do_start() {
  if is_running; then
    echo "✅ 工作台已在运行"
  else
    has_uv || { echo "❌ 找不到运行环境（uv），请先选 6 做首次安装"; return 1; }
    echo "🚀 正在后台启动工作台…"
    nohup uv run uvicorn app.main:app --host 127.0.0.1 --port 8321 > server.log 2>&1 &
    for _ in $(seq 1 40); do
      sleep 0.5
      curl -s -o /dev/null "$ADDR/api/config" 2>/dev/null && break
    done
    if is_running; then
      echo "✅ 启动成功（日志在 server.log）"
    else
      echo "❌ 启动失败，请把 server.log 的内容发给技术同学排查"
      return 1
    fi
  fi
  open "$ADDR"
  echo "👉 浏览器已打开工作台页面，本窗口可以直接关闭。"
}

# ---------- ② 关闭 ----------
do_stop() {
  local stopped=0
  pkill -f "uvicorn app.main" 2>/dev/null && stopped=1
  # 顺带关掉它拉起的后台 Chrome（按本项目专用 profile 路径匹配，不误伤日常浏览器）
  pkill -f "$PWD/chrome-profile" 2>/dev/null && stopped=1
  if [ "$stopped" = "1" ]; then
    sleep 1  # 等端口真正释放，让菜单状态显示准确
    echo "✅ 工作台已关闭"
  else
    echo "ℹ️  工作台本来就没在运行"
  fi
}

# ---------- ③ 打开页面 ----------
do_open() {
  if is_running; then
    open "$ADDR"
    echo "✅ 已在浏览器打开 $ADDR"
  else
    echo "⭕ 工作台没在运行，先帮你启动…"
    do_start
  fi
}

# ---------- ④ 登录剧多多 ----------
do_login() {
  has_uv || { echo "❌ 找不到运行环境（uv），请先选 6 做首次安装"; return 1; }
  uv run python -m app.login
}

# ---------- ⑤ 打包安装包 ----------
do_pack() {
  local PKG="jdd-studio-$VER"
  local OUT="$HOME/Desktop/$PKG.zip"
  local STAGE_ROOT STAGE
  STAGE_ROOT=$(mktemp -d)
  STAGE="$STAGE_ROOT/$PKG"
  mkdir -p "$STAGE"
  rsync -a \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.git' \
    --exclude '.DS_Store' \
    --exclude 'chrome-profile' \
    --exclude 'candidates' \
    --exclude 'saved_images' \
    --exclude 'review_pending' \
    --exclude 'review_done' \
    --exclude 'refs' \
    --exclude 'debug' \
    --exclude 'tasks.db' \
    --exclude 'tasks.db-shm' \
    --exclude 'tasks.db-wal' \
    --exclude 'save_dirs.json' \
    --exclude 'server.log' \
    --exclude 'nohup.out' \
    --exclude '*.zip' \
    ./ "$STAGE/"
  # 保留空目录占位（程序运行时也会自建，双保险）
  mkdir -p "$STAGE/candidates" "$STAGE/saved_images" "$STAGE/debug" "$STAGE/review_pending" "$STAGE/review_done" "$STAGE/refs"
  rm -f "$OUT"
  # 用 Python zipfile 打包（正确设置 UTF-8 标志，中文文件名跨平台不乱码）
  uv run python - "$STAGE_ROOT" "$PKG" "$OUT" <<'PYEOF'
import sys, zipfile
from pathlib import Path
root, pkg, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for fp in sorted((root / pkg).rglob("*")):
        z.write(fp, str(fp.relative_to(root)))
PYEOF
  rm -rf "$STAGE_ROOT"
  echo "✅ 安装包已生成：$OUT"
  du -h "$OUT" | awk '{print "   大小："$1}'
  echo "   迁移方法：拷到另一台 Mac → 解压 → 双击里面的「工作台管家.command」"
}

# ---------- ⑥ 首次安装 ----------
do_install() {
  echo ""
  echo "========== 首次安装向导 =========="

  # 1. uv
  if ! has_uv; then
    echo "① 安装运行环境管理器 uv（约 10 秒）…"
    curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "❌ uv 安装失败，请检查网络"; return 1; }
    export PATH="$HOME/.local/bin:$PATH"
    hash -r
  fi
  has_uv || { echo "❌ uv 安装后仍不可用，请关闭本窗口重试"; return 1; }
  echo "① uv 就绪（$(uv --version)）"

  # 2. Chrome
  if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo ""
    echo "❌ 未检测到 Google Chrome 浏览器（网页自动化需要它）。"
    echo "   请先安装：https://www.google.cn/chrome/"
    echo "   装好后重新双击本脚本即可继续。"
    return 1
  fi
  echo "② Google Chrome 已安装"

  # 3. 依赖
  echo ""
  echo "③ 安装运行依赖（首次约 1-2 分钟，之后秒过）…"
  uv sync || { echo "❌ 依赖安装失败，请检查网络"; return 1; }
  echo "③ 依赖就绪"

  # 4. AIOnly key（先自动找，找不到再请用户粘贴）
  echo ""
  if uv run python -c "
from app import config
import sys
sys.exit(0 if config.llm_config()['api_key'] else 1)
" 2>/dev/null; then
    echo "④ 已自动找到 AIOnly API key（来自本机 Cherry Studio 配置）"
  else
    echo "④ 这台电脑上没有可自动读取的 AIOnly key"
    echo "   请到 AIOnly 后台复制 API key（形如 sk-...）"
    printf "   粘贴 key 后回车："
    read -r KEY
    KEY="$(echo "$KEY" | tr -d '[:space:]')"
    if [ -z "$KEY" ]; then
      echo "❌ key 不能为空。拿到 key 后重新运行安装即可。"
      return 1
    fi
    uv run python - "$KEY" <<'PYEOF'
import sys, pathlib
key = sys.argv[1]
p = pathlib.Path("config.yaml")
t = p.read_text(encoding="utf-8")
old = '# api_key: "sk-..."'
if old in t:
    t = t.replace(old, f'api_key: "{key}"', 1)
elif "llm:\n" in t:
    t = t.replace("llm:\n", f'llm:\n  api_key: "{key}"\n', 1)
else:
    t = f'llm:\n  api_key: "{key}"\n' + t
p.write_text(t, encoding="utf-8")
print("   ✅ key 已写入 config.yaml")
PYEOF
  fi

  # 5. 登录
  echo ""
  printf "⑤ 现在打开浏览器登录剧多多吗？（只需登录一次）[Y/n] "
  read -r ANS
  if [ "$ANS" != "n" ] && [ "$ANS" != "N" ]; then
    uv run python -m app.login
  else
    echo "   跳过。之后在菜单里选 4 可随时登录。"
  fi

  # 6. 桌面图标
  cat > "$HOME/Desktop/工作台管家.command" <<EOF
#!/bin/bash
exec "$PWD/工作台管家.command"
EOF
  chmod +x "$HOME/Desktop/工作台管家.command"
  echo "⑥ 已在桌面放好「工作台管家」图标"

  echo ""
  echo "🎉 安装完成！正在启动工作台…"
  do_start
}

# ============================================================
#  入口：首次运行（无环境且未在运行）直接进安装向导
# ============================================================
if [ ! -d .venv ] && ! is_running; then
  echo "============================================================"
  echo "  🎬 剧多多文生图工作台 v$VER"
  echo "  检测到本机首次运行，自动进入安装向导"
  echo "============================================================"
  do_install
  exit $?
fi

# ---------- 菜单 ----------
while true; do
  echo ""
  echo "============================================================"
  echo "  🎬 剧多多文生图工作台 · 管家  v$VER"
  if is_running; then
    echo "  当前状态：✅ 运行中（$ADDR）"
    DEF=3
  else
    echo "  当前状态：⭕ 未运行"
    DEF=1
  fi
  echo "============================================================"
  echo "   1) 🟢 启动工作台（并打开页面）"
  echo "   2) 🔴 关闭工作台"
  echo "   3) 🌐 打开工作台页面"
  echo "   4) 🔐 登录剧多多（登录失效时用）"
  echo "   5) 📦 打包安装包（迁移到其他电脑）"
  echo "   6) 🛠  首次安装（新电脑向导）"
  echo "   0) 退出"
  printf "请输入编号 [直接回车 = %s]: " "$DEF"
  read -r CHOICE
  CHOICE=${CHOICE:-$DEF}
  case "$CHOICE" in
    1) do_start ;;
    2) do_stop ;;
    3) do_open ;;
    4) do_login ;;
    5) do_pack ;;
    6) do_install ;;
    0) echo "👋 再见"; exit 0 ;;
    *) echo "⚠️  无效输入，请输入 0-6" ;;
  esac
done
