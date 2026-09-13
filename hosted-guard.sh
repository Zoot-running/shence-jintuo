#!/bin/bash
# 托管沙箱守卫（金柝 hosted-guard）：在平台一次性沙箱内守护战役 driver。
# 与本地 guard-runner 的区别：
#  - 本脚本自身是容器主进程（PID 1），driver 退出不影响容器存活；
#  - 判据不是 exit code，而是 runner 的收官标记 .campaign-finished
#    （只有 xiaochang_finish 全终态才会写）——战役没打完，driver 怎么退都重拉；
#  - 停表条款由此机制化：agent 无法提前杀死战役，只有满分 finish 或平台 6h 钟可结束。
#
# 退出码契约：
#   0  = 战役完成（标记存在）→ standing down（容器退出 → 平台判局终）
#   2  = 环境不满足（缺 DSH_HOME/凭证）→ 立即失败
set -u

MARKER="${GUARD_MARKER:-/opt/work/.campaign-finished}"
WORKDIR_HOME="${GUARD_WORKDIR:-/opt/work}"
DSH_HOME_V="${GUARD_DSH_HOME:-/opt/dsh-home}"
LOG="${GUARD_LOG:-/tmp/hosted-guard.log}"
RESTART_DELAY="${GUARD_RESTART_DELAY:-5}"
# 兜底上限（秒）：远超平台 6h 钟；平台到时自然终止沙箱。
MAX_RUNTIME="${GUARD_MAX_RUNTIME:-21600}"
START_TS=$(date +%s)

if [ ! -d "$DSH_HOME_V" ]; then
  echo "FATAL: DSH_HOME $DSH_HOME_V missing" >&2
  exit 2
fi
mkdir -p "$WORKDIR_HOME"

log() { echo "$(date '+%F %T') hosted-guard: $*" >> "$LOG"; }

# 战役 driver：与本地 launch 一致的最小形态（开战令打包在镜像内）。
DRIVER_CMD="${GUARD_CMD:-node /opt/dsh/apps/cli/lib/bin.js --profile headless \"\$(cat /opt/order.txt)\"}"

launch() {
  log "launching driver (pid=$$ attempt=$1)"
  cd "$WORKDIR_HOME"
  export DSH_HOME="$DSH_HOME_V"
  eval "$DRIVER_CMD"
}

ATTEMPT=0
while true; do
  if [ -f "$MARKER" ]; then
    log "campaign-finished marker present — standing down"
    exit 0
  fi
  NOW=$(date +%s)
  if [ $((NOW - START_TS)) -ge "$MAX_RUNTIME" ]; then
    log "MAX_RUNTIME reached — standing down"
    exit 0
  fi
  ATTEMPT=$((ATTEMPT + 1))
  launch "$ATTEMPT"
  CODE=$?
  log "driver exited code=$CODE after attempt $ATTEMPT"
  if [ -f "$MARKER" ]; then
    log "campaign-finished marker present — standing down"
    exit 0
  fi
  # 战役未完成：无论 exit code（含 0=回合结束/异常退出）都重拉。
  log "campaign not finished — relaunching in ${RESTART_DELAY}s"
  sleep "$RESTART_DELAY"
done
