#!/usr/bin/env bash
# =====================================================================
# Portable Console — 便携运维控制台安装脚本（免 root / root-free）
#
# 做四件事：
#   1. 检查 python3 >= 3.10（纯标准库，零第三方依赖）
#   2. 首次运行复制 console.config.example.json -> console.config.json（幂等）
#   3. 生成 data/console.token（优先调用 `python3 console.py token`；
#      子命令不存在/console.py 缺席时用 /dev/urandom 兜底，权限 0600）
#   4. 渲染 systemd/*.service 模板（%B_PLACEHOLDER -> bundle 绝对路径，
#      user unit 不支持 %b 故用 sed drop-in 方式）安装到
#      ~/.config/systemd/user/ 并 daemon-reload。本次安装不自动启动服务。
#
# 选项：
#   --port N        覆盖 console.config.json 里 listen.port
#   --no-systemd    跳过单元安装，打印手动运行命令
#   --uninstall     停用并移除已安装的用户单元（保留 data/ 与配置文件）
#   -h, --help      显示帮助
# =====================================================================
set -euo pipefail

BUNDLE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
EXAMPLE="$BUNDLE_DIR/console.config.example.json"
CONFIG="$BUNDLE_DIR/console.config.json"
DATA_DIR="$BUNDLE_DIR/data"
TOKEN="$DATA_DIR/console.token"
UNIT_DIR="${HOME}/.config/systemd/user"
UNITS="console-daemon.service console-server.service"

PORT=""
WITH_SYSTEMD=1
DO_UNINSTALL=0

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install][警告] %s\n' "$*" >&2; }
die()  { printf '[install][错误] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

# ------------------------- 参数解析 -------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --port)
      [ $# -ge 2 ] || die "--port 需要一个数字参数"
      PORT="$2"; shift 2 ;;
    --port=*)
      PORT="${1#*=}"; shift ;;
    --no-systemd)
      WITH_SYSTEMD=0; shift ;;
    --uninstall)
      DO_UNINSTALL=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      die "未知参数: $1（--help 查看用法）" ;;
  esac
done

# ------------------------- 环境检查 -------------------------
check_python() {
  command -v python3 >/dev/null 2>&1 \
    || die "未找到 python3。本 bundle 只依赖 Python >= 3.10 标准库，请安装 python3 后重试。"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 版本过低：当前 $(python3 -V 2>&1)，需要 >= 3.10。"
  log "python3 版本合格：$(python3 -V 2>&1)"
}

# ------------------------- 配置文件 -------------------------
ensure_config() {
  if [ ! -f "$CONFIG" ]; then
    [ -f "$EXAMPLE" ] || die "缺少示例配置 $EXAMPLE"
    cp "$EXAMPLE" "$CONFIG"
    log "已生成 console.config.json（复制自示例；cards/links 请按需编辑）"
  else
    log "console.config.json 已存在（保留现有配置，不覆盖）"
  fi
}

apply_port() {
  case "$1" in
    ''|*[!0-9]*) die "--port 需要 1-65535 的整数，收到: $1" ;;
  esac
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ] || die "端口越界: $1"
  if grep -qE '"listen".*"port"[[:space:]]*:[[:space:]]*[0-9]+' "$CONFIG"; then
    # 单行写法（示例格式）：直接 sed 替换 listen 行的 port
    sed -i -E 's/("listen".*"port"[[:space:]]*:[[:space:]]*)[0-9]+/\1'"$1"'/' "$CONFIG"
  else
    # 多行/其它排版：用 python3 标准库 json 重写，保证语义正确
    python3 - "$CONFIG" "$1" <<'PY'
import json, sys
path, port = sys.argv[1], int(sys.argv[2])
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)
cfg.setdefault("listen", {})["port"] = port
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  fi
  grep -Eq "\"port\"[[:space:]]*:[[:space:]]*$1([^0-9]|$)" "$CONFIG" \
    || die "端口写入失败，请检查 console.config.json 格式"
  log "监听端口已设为 $1"
}

current_port() {
  python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8")).get("listen",{}).get("port",8090))' \
    "$CONFIG" 2>/dev/null || echo 8090
}

# ------------------------- 令牌 -------------------------
ensure_token() {
  mkdir -p "$DATA_DIR"
  if [ -s "$TOKEN" ]; then
    chmod 600 "$TOKEN" 2>/dev/null || true
    log "data/console.token 已存在（保留现有令牌）"
    return 0
  fi
  # 路径 1：console.py 已实现 token 子命令则优先使用它。
  # stdout 捕获到临时文件；若它自己写了 data/console.token 则不重复落盘。
  if [ -f "$BUNDLE_DIR/console.py" ]; then
    local tmp
    tmp="$(mktemp)"
    if (cd "$BUNDLE_DIR" && python3 console.py token >"$tmp" 2>/dev/null); then
      if [ ! -f "$TOKEN" ] && [ -s "$tmp" ]; then
        ( umask 077; tr -d ' \t\r\n' <"$tmp" >"$TOKEN" )
        log "令牌由 console.py token 生成"
      elif [ -f "$TOKEN" ]; then
        log "令牌由 console.py token 直接写入 data/console.token"
      fi
    fi
    rm -f "$tmp"
  fi
  # 路径 2（兜底，安装不硬依赖 console.py 的 token 子命令）
  if [ ! -s "$TOKEN" ]; then
    ( umask 077; head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$TOKEN" )
    log "console.py token 不可用，已用 /dev/urandom 生成 data/console.token"
  fi
  chmod 600 "$TOKEN"
  log "令牌就绪：data/console.token（权限 $(stat -c '%a' "$TOKEN" 2>/dev/null || echo 0600)）"
}

# ------------------------- systemd 用户单元 -------------------------
sed_escape_replacement() {
  # 转义 sed 替换串中的 \ & 与分隔符 |
  printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

install_units() {
  mkdir -p "$UNIT_DIR"
  local esc name src dst
  esc="$(sed_escape_replacement "$BUNDLE_DIR")"
  for name in $UNITS; do
    src="$BUNDLE_DIR/systemd/$name"
    [ -f "$src" ] || die "缺少单元模板 $src"
    dst="$UNIT_DIR/$name"
    sed "s|%B_PLACEHOLDER|${esc}|g" "$src" >"$dst"
    log "已渲染安装用户单元：$dst"
  done
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user daemon-reload 2>/dev/null; then
      log "systemd --user 已重载单元"
    else
      warn "systemctl --user daemon-reload 失败（可能无用户 systemd 会话），请手动执行。"
    fi
  else
    warn "未找到 systemctl，跳过 daemon-reload；请以命令行方式运行（参考 --no-systemd 提示）。"
  fi
  local port
  port="$(current_port)"
  echo
  log "本次安装未自动启动服务。启动命令："
  echo "    systemctl --user enable --now console-daemon console-server"
  echo "    # 浏览器打开 http://127.0.0.1:${port}"
  echo
  log "开机常驻（无需登录会话）需一次性开启 linger："
  echo "    loginctl enable-linger $USER"
  echo "    # 说明：linger 让 user unit 在开机/未登录时也被拉起；该操作走 polkit，"
  echo "    # 可能需要一次管理员授权，属可选步骤。不开启则服务仅在你登录期间运行。"
}

manual_hint() {
  local port
  port="$(current_port)"
  echo
  log "已跳过 systemd 安装（--no-systemd）。手动运行（建议分别放后台/独立终端）："
  echo "    cd $BUNDLE_DIR"
  echo "    python3 console.py daemon run --config console.config.json   # 采集守护"
  echo "    python3 console.py serve    --config console.config.json   # Web 门户"
  echo "    # 浏览器打开 http://127.0.0.1:${port}"
}

uninstall_units() {
  log "卸载用户单元（保留 data/ 与 console.config.json）"
  if command -v systemctl >/dev/null 2>&1; then
    local name
    for name in $UNITS; do
      systemctl --user disable --now "$name" 2>/dev/null || true
    done
  fi
  local changed=0 name
  for name in $UNITS; do
    if [ -e "$UNIT_DIR/$name" ]; then
      rm -f "$UNIT_DIR/$name"
      log "已移除 $UNIT_DIR/$name"
      changed=1
    fi
  done
  if [ "$changed" = 1 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  log "完成。data/（health.json、console.token 等）与 console.config.json 均已保留。"
}

# ------------------------- 主流程 -------------------------
main() {
  check_python
  if [ "$DO_UNINSTALL" = 1 ]; then
    uninstall_units
    exit 0
  fi
  ensure_config
  if [ -n "$PORT" ]; then
    apply_port "$PORT"
  fi
  ensure_token
  if [ "$WITH_SYSTEMD" = 1 ]; then
    install_units
  else
    manual_hint
  fi
  log "安装完成。bundle: $BUNDLE_DIR"
}
main
