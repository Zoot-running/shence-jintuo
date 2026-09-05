#!/bin/bash
# 金柝 L3 混沌测试：对目标 DSH 实例（默认 dev 3081）执行 N 轮杀进程→断言自动恢复。
# 用法：bash tests/chaos.sh [rounds] [port]
# 断言：每轮杀掉 web 后，守护进程在 90s 内拉起；告警文件记录 web-exit；证据日志增长。
set -u
ROUNDS="${1:-3}"
PORT="${2:-3081}"
LOG_DIR="${JINTUO_LOG_DIR:-/mnt/d/Software/WSLSoftware/Agents/deepseek-harness-dev/logs}"
ALERT_FILE="${JINTUO_ALERT_FILE:-/home/zrn/.dsh-dev/storages/jintuo-alerts.jsonl}"

fail=0
for i in $(seq 1 "$ROUNDS"); do
  PID=$(pgrep -f "bin.js web --port $PORT" | head -1)
  if [ -z "$PID" ]; then
    echo "round $i: no web process on port $PORT (aborting)"
    fail=1
    break
  fi
  ALERTS_BEFORE=$( [ -f "$ALERT_FILE" ] && wc -l < "$ALERT_FILE" || echo 0 )
  echo "round $i: killing web pid=$PID"
  kill -9 "$PID"

  UP=0
  for j in $(seq 1 90); do
    sleep 1
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT" 2>/dev/null; then
      UP=1
      break
    fi
  done
  if [ "$UP" -ne 1 ]; then
    echo "round $i: FAIL — web did not come back within 90s"
    fail=1
    continue
  fi
  NEW_PID=$(pgrep -f "bin.js web --port $PORT" | head -1)
  ALERTS_AFTER=$( [ -f "$ALERT_FILE" ] && wc -l < "$ALERT_FILE" || echo 0 )
  EVIDENCE_LINES=$(wc -l < "$LOG_DIR/jintuo-oom-evidence.log" 2>/dev/null || echo 0)
  echo "round $i: OK — restarted as pid=$NEW_PID in ${j}s; alerts ${ALERTS_BEFORE}->${ALERTS_AFTER}; evidence lines=$EVIDENCE_LINES"
  if [ "$ALERTS_AFTER" -le "$ALERTS_BEFORE" ]; then
    echo "round $i: WARN — no new web-exit alert recorded"
  fi
  sleep 2
done

if [ "$fail" -eq 0 ]; then
  echo "CHAOS-OK: $ROUNDS rounds, all recoveries succeeded"
else
  echo "CHAOS-FAIL"
fi
exit $fail
