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
#
# v0.2.0 packaging: 上面 1-4 的全部逻辑已移植到 python
# （`portable-console install` / `uninstall` 子命令，见 src/portable_console/
# cli.py）。本脚本退化为兼容垫片，保持 v0.1.0 的调用方式不变：
#   - PATH 上已有 portable-console（pipx/pip 安装）-> 直接转发；
#   - 否则用 PYTHONPATH=<本目录>/src python3 -m portable_console 转发（源码树）。
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"

log()  { printf '[install] %s\n' "$*"; }
die()  { printf '[install][错误] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
}

# ------------------------- 参数解析 -------------------------
MODE="install"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --port)
      [ $# -ge 2 ] || die "--port 需要一个数字参数"
      ARGS+=(--port "$2"); shift 2 ;;
    --port=*)
      ARGS+=(--port "${1#*=}"); shift ;;
    --no-systemd)
      ARGS+=(--no-systemd); shift ;;
    --uninstall)
      MODE="uninstall"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      die "未知参数: $1（--help 查看用法）" ;;
  esac
done

# ------------------------- 转发 -------------------------
# v0.1.0 源码树兼容：bundle 里已有 console.config.json 则沿用（不迁移到
# XDG 默认路径）；用户显式 --config 优先。
has_config=0
for a in ${ARGS[@]+"${ARGS[@]}"}; do
  [ "$a" = "--config" ] && has_config=1
done
if [ "$MODE" = "install" ] && [ "$has_config" = 0 ] && [ -f "$SCRIPT_DIR/console.config.json" ]; then
  ARGS+=(--config "$SCRIPT_DIR/console.config.json")
fi

if command -v portable-console >/dev/null 2>&1; then
  log "转发至 portable-console ${MODE}（PATH 上的已安装版本）"
  exec portable-console "$MODE" ${ARGS[@]+"${ARGS[@]}"}
fi
[ -d "$SCRIPT_DIR/src/portable_console" ] \
  || die "找不到 portable-console 命令，且 $SCRIPT_DIR/src 下没有包源码"
log "使用源码树：python3 -m portable_console ${MODE}"
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m portable_console "$MODE" ${ARGS[@]+"${ARGS[@]}"}
