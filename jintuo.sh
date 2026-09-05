#!/bin/bash
# 金柝（shence-jintuo）—— 神策 P2：DSH 守护启动器（父进程）
# 职责：拉起/监督 DSH web；资源采样与告警（写 jintuo-alerts.jsonl，只告警不代决策）；
#       崩溃自动拉起（防重启风暴退避）；退出证据转储；内核 OOM 行逃逸副本。
#
# 配置（环境变量，均有默认）：
#   JINTUO_DSH_HOME    DSH 数据目录（默认 /home/zrn/.dsh-dev）
#   JINTUO_WORKDIR     DSH checkout（默认脚本所在目录的上级 dsh 目录：<repo>/../）
#   JINTUO_PORT        web 端口（默认 3081）
#   JINTUO_HEAP_MB     V8 堆上限 MB（默认 2048）
#   JINTUO_SAMPLE_S    采样间隔秒（默认 5）
#   JINTUO_WARN_MB     告警水位 MB（默认堆上限的 75%）
#   JINTUO_LOG_DIR     日志目录（默认 <workdir>/logs）
#   JINTUO_ALERT_FILE  告警文件（默认 $JINTUO_DSH_HOME/storages/jintuo-alerts.jsonl）
#   JINTUO_NO_OPEN     置 1 禁止自动开浏览器（默认 1）

set -u

DSH_HOME_="${JINTUO_DSH_HOME:-/home/zrn/.dsh-dev}"
export DSH_HOME="$DSH_HOME_"
WORKDIR="${JINTUO_WORKDIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PORT="${JINTUO_PORT:-3081}"
HEAP_MB="${JINTUO_HEAP_MB:-2048}"
SAMPLE_S="${JINTUO_SAMPLE_S:-5}"
WARN_MB="${JINTUO_WARN_MB:-$((HEAP_MB * 75 / 100))}"
LOG_DIR="${JINTUO_LOG_DIR:-$WORKDIR/logs}"
ALERT_FILE="${JINTUO_ALERT_FILE:-$DSH_HOME_/storages/jintuo-alerts.jsonl}"
NO_OPEN="${JINTUO_NO_OPEN:-1}"

export NODE_OPTIONS="--max-old-space-size=$((HEAP_MB))"
export NVM_DIR="/mnt/d/Software/WSLSoftware/ProgramLanguages/nodejs/NVM"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd "$WORKDIR" || exit 1
mkdir -p "$LOG_DIR" "$(dirname "$ALERT_FILE")"

LOG="$LOG_DIR/jintuo-web.log"
MEMLOG="$LOG_DIR/jintuo-mem.log"
OOMLOG="$LOG_DIR/jintuo-oom-evidence.log"
KERNELLOG="$LOG_DIR/jintuo-kernel-oom.log"
PIDFILE="$LOG_DIR/jintuo-web.pid"

RESTART_DELAY=3
FAIL_STREAK=0
MIN_UPTIME=60

alert() { # kind summary [detail]
  printf '{"at":%s,"kind":"%s","summary":"%s","detail":"%s"}\n' \
    "$(date +%s000)" "$1" "$2" "${3:-}" >> "$ALERT_FILE"
}

echo "$(date '+%F %T') jintuo daemon started (pid $$, port=$PORT, heap=${HEAP_MB}MB)" >> "$LOG"

( while true; do
    dmesg -T 2>/dev/null | grep -iE "out of memory|oom-kill|killed process" >> "$KERNELLOG"
    sleep 60
  done ) &

while true; do
  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT" 2>/dev/null; then
    echo "$(date '+%F %T') jintuo: port $PORT already serving; giving up" >> "$LOG"
    exit 0
  fi

  echo "$(date '+%F %T') jintuo: starting web (NODE_OPTIONS=$NODE_OPTIONS)" >> "$LOG"
  START_TS=$(date +%s)
  ARGS="web --port $PORT"
  [ "$NO_OPEN" = "1" ] && ARGS="$ARGS --no-open"
  node apps/cli/lib/bin.js $ARGS >> "$LOG" 2>&1 &
  WEB_PID=$!
  echo $WEB_PID > "$PIDFILE"
  echo "$(date '+%F %T') jintuo: web pid=$WEB_PID" >> "$LOG"

  while kill -0 "$WEB_PID" 2>/dev/null; do
    sleep "$SAMPLE_S"
    RSS=$(ps -o rss= -p "$WEB_PID" 2>/dev/null | tr -d ' ')
    if [ -n "$RSS" ]; then
      echo "$(date '+%F %T') pid=$WEB_PID rss_kb=$RSS" >> "$MEMLOG"
      if [ "$RSS" -gt "$((WARN_MB * 1024))" ] 2>/dev/null; then
        echo "$(date '+%F %T') WARN pid=$WEB_PID rss_kb=$RSS approaching heap limit" >> "$LOG"
        alert "resource-pressure" "web RSS $((RSS / 1024))MB > ${WARN_MB}MB warn threshold" "pid=$WEB_PID rss_kb=$RSS"
      fi
    fi
  done

  wait "$WEB_PID"
  CODE=$?
  UPTIME=$(( $(date +%s) - START_TS ))
  echo "$(date '+%F %T') jintuo: web exited code=$CODE uptime=${UPTIME}s; restarting in ${RESTART_DELAY}s" >> "$LOG"
  alert "web-exit" "web exited code=$CODE uptime=${UPTIME}s" "restarting in ${RESTART_DELAY}s"

  {
    echo "===== $(date '+%F %T') web exited code=$CODE uptime=${UPTIME}s"
    echo "--- free -m"
    free -m
    echo "--- /proc/meminfo (head)"
    head -12 /proc/meminfo
    echo "--- /proc/pressure/memory"
    cat /proc/pressure/memory 2>/dev/null || echo "(PSI unavailable)"
    echo "--- dmesg tail"
    dmesg -T 2>/dev/null | tail -80
    echo
  } >> "$OOMLOG"

  if [ "$UPTIME" -lt "$MIN_UPTIME" ]; then
    FAIL_STREAK=$((FAIL_STREAK + 1))
    case "$FAIL_STREAK" in
      2) RESTART_DELAY=10 ;;
      3) RESTART_DELAY=30 ;;
    esac
    if [ "$FAIL_STREAK" -ge 5 ]; then
      echo "$(date '+%F %T') jintuo: 5 consecutive fast failures; giving up to avoid restart storm" >> "$LOG"
      alert "giving-up" "5 consecutive fast failures" "see $OOMLOG"
      exit 1
    fi
  else
    FAIL_STREAK=0
    RESTART_DELAY=3
  fi
  sleep "$RESTART_DELAY"
done
