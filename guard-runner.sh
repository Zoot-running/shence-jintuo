#!/bin/bash
# 金柝·runner 守护：监督校场 L4 跑分进程（xiaochang_start 所在 headless CLI）。
# 与 jintuo.sh 分工：jintuo.sh 守 DSH web 实例；本脚本守"长跑工具"进程——
#   1) 进程消亡 → 重新拉起（同一命令；runner 工具按快照续跑，丢的只是在跑那一轮）；
#   2) 心跳失联（审计文件超过 STALE 秒没更新）→ 判定假死，杀掉重拉；
#   3) 内存超限 → 只告警（与金柝原则一致：告警不代决策）；
#   4) 5 连快速失败 → 放弃重启风暴，告警留证。
#
# 环境变量：
#   GUARD_DSH_HOME     DSH 数据目录（默认 /home/zrn/.dsh-dev；告警/审计都在这下面）
#   GUARD_CMD          要守护的完整命令（必填）
#   GUARD_LOGFILE      守护日志（默认 /tmp/guard-runner.log）
#   GUARD_AUDIT_FILE   心跳来源文件（默认 $GUARD_DSH_HOME/storages/xiaochang-run-audit.jsonl）
#   GUARD_STALE_S      心跳失联判定秒数（默认 600；runner 每 120s 写心跳）
#   GUARD_HEAP_MB      内存告警水位 MB（默认 3072，只告警）
#   GUARD_SAMPLE_S     采样间隔秒（默认 20）
#   GUARD_ALERT_FILE   告警文件（默认 $GUARD_DSH_HOME/storages/jintuo-alerts.jsonl）
set -u

DSH_HOME_="${GUARD_DSH_HOME:-/home/zrn/.dsh-dev}"
CMD="${GUARD_CMD:-}"
LOG="${GUARD_LOGFILE:-/tmp/guard-runner.log}"
AUDIT_FILE="${GUARD_AUDIT_FILE:-$DSH_HOME_/storages/xiaochang-run-audit.jsonl}"
STALE_S="${GUARD_STALE_S:-600}"
HEAP_MB="${GUARD_HEAP_MB:-3072}"
SAMPLE_S="${GUARD_SAMPLE_S:-20}"
ALERT_FILE="${GUARD_ALERT_FILE:-$DSH_HOME_/storages/jintuo-alerts.jsonl}"

if [ -z "$CMD" ]; then
  echo "guard-runner: GUARD_CMD is required (the headless CLI command that invokes xiaochang_start)" >&2
  exit 2
fi

alert() { # kind summary [detail]
  local kind="$1" summary="$2" detail="${3:-}"
  printf '{"at":%s,"kind":"%s","summary":"%s","detail":"%s"}\n' \
    "$(($(date +%s%N) / 1000000))" "$kind" "$summary" "$detail" >> "$ALERT_FILE"
}

log() { echo "$(date '+%F %T') guard-runner: $*" >> "$LOG"; }

launch() {
  # F16：重启后旧审计文件的 mtime 属于上一任子进程——新子进程必须重新获得
  # 完整 boot 宽限（GUARD_BOOT_S），否则 stale-kill 后每 20-30s 连杀新进程（风暴）。
  AUDIT_SEEN=0
  # F17：setsid 建独立进程组，restart_child 按组杀，不留 node 孤儿。
  setsid bash -c 'cd "${GUARD_WORKDIR:-/home/zrn/xiaochang-work}" && eval "$GUARD_CMD"' >>"${GUARD_STDOUT:-/tmp/guard-runner-cmd.log}" 2>&1 &
  CHILD_PID=$!
  CHILD_STARTED=$(date +%s)
  log "launched cmd pid=$CHILD_PID"
}

heartbeat_age_s() {
  if [ -f "$AUDIT_FILE" ]; then
    echo $(( $(date +%s) - $(stat -c %Y "$AUDIT_FILE") ))
  else
    echo 999999
  fi
}

restart_child() { # reason
  local reason="$1"
  # F17：按进程组杀（setsid 组杀）——只杀包装层会留下 node 孤儿进程，
  # 与重启后的新 agent 并发跑、互相抢会话锁/烧钱（run 6 实锤：stale-kill 后 12562 存活）。
  if [ -n "${CHILD_PID:-}" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -- -"$CHILD_PID" 2>/dev/null
    sleep 2
    kill -9 -- -"$CHILD_PID" 2>/dev/null
  fi
  FAILS=$((FAILS + 1))
  if [ "$FAILS" -ge 5 ]; then
    alert "runner-giving-up" "5 consecutive runner failures" "reason=$reason log=$LOG"
    log "5 consecutive failures; giving up (reason=$reason)"
    exit 1
  fi
  local delay=3
  [ "$FAILS" -ge 2 ] && delay=10
  [ "$FAILS" -ge 3 ] && delay=30
  alert "runner-restart" "runner restarted after: $reason" "delay=${delay}s fails=$FAILS"
  log "restarting after: $reason (delay=${delay}s, fails=$FAILS)"
  sleep "$delay"
  launch
}

FAILS=0
CHILD_PID=""
CHILD_STARTED=0
AUDIT_SEEN=0
launch

while true; do
  sleep "$SAMPLE_S"

  # 1. 进程存活
  if [ -n "$CHILD_PID" ] && ! kill -0 "$CHILD_PID" 2>/dev/null; then
    wait "$CHILD_PID" 2>/dev/null
    CODE=$?
    UPTIME=$(( $(date +%s) - CHILD_STARTED ))
    restart_child "process exited code=$CODE uptime=${UPTIME}s"
    continue
  fi

  # 2. 心跳失联（假死/卡死循环也在此覆盖：runner 每 120s 写心跳）。
  #    关键：心跳文件"存在"≠现任子进程写的——重启后旧文件 mtime 属于上一任。
  #    只有 mtime 晚于本次启动（CHILD_STARTED）才算现任写过；否则用 boot 宽限。
  if [ -f "$AUDIT_FILE" ] && [ "$(stat -c %Y "$AUDIT_FILE" 2>/dev/null || echo 0)" -ge "$CHILD_STARTED" ]; then
    AUDIT_SEEN=1
  fi
  AGE=$(heartbeat_age_s)
  UPTIME=$(( $(date +%s) - CHILD_STARTED ))
  if [ "$AUDIT_SEEN" = 1 ] && [ "$AGE" -gt "$STALE_S" ]; then
    restart_child "heartbeat stale for ${AGE}s (> ${STALE_S}s)"
    continue
  fi
  if [ "$AUDIT_SEEN" = 0 ] && [ "$UPTIME" -gt "${GUARD_BOOT_S:-600}" ]; then
    restart_child "no heartbeat file within ${GUARD_BOOT_S:-600}s (boot wedged)"
    continue
  fi

  # 3. 内存告警（只告警）
  RSS_KB=$(ps -o rss= -p "$CHILD_PID" 2>/dev/null | tr -d ' ' || echo 0)
  if [ "${RSS_KB:-0}" -gt $((HEAP_MB * 1024)) ]; then
    alert "runner-resource-pressure" "runner RSS $((RSS_KB / 1024))MB > ${HEAP_MB}MB warn threshold" "pid=$CHILD_PID rss_kb=$RSS_KB"
  fi
done
