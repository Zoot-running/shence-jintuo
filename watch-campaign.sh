#!/bin/bash
# 金柝·战役监视器：常驻轮询 run 战役的完整观测面，写结构化采样日志 + 越界告警。
# 观测面：平台得分/容器、进度账、审计心跳、进程存活、供应商余额、花费 sidecar。
# 告警（写 $DSH_HOME/storages/jintuo-alerts.jsonl）：得分停滞、余额告急、进程死亡、心跳失联。
#
# 环境变量：
#   WATCH_DSH_HOME    DSH 数据目录（默认 /home/zrn/.dsh-dev）
#   WATCH_TOKEN       BENCHMARK_TOKEN（必填；用于平台得分轮询）
#   WATCH_BASE_URL    平台地址（默认 https://tsecbench.zc.tencent.com）
#   WATCH_AUDIT_FILE  审计文件（默认 $WATCH_DSH_HOME/storages/xiaochang-run-audit.jsonl）
#   WATCH_SAMPLE_S    采样间隔秒（默认 120）
#   WATCH_STALL_S     得分停滞告警阈值秒（默认 1500=25min）
#   WATCH_BALANCE_WARN 余额告警阈值 ¥（默认 40）
#   WATCH_LOG         采样日志（默认 $WATCH_DSH_HOME/storages/campaign-watch.jsonl）
#   WATCH_ALERT_FILE  告警文件（默认 $WATCH_DSH_HOME/storages/jintuo-alerts.jsonl）
#   WATCH_KIMI_KEY / WATCH_DS_KEY  余额轮询的 key（可选；缺省跳过对应供应商）
set -u

DSH_HOME_="${WATCH_DSH_HOME:-/home/zrn/.dsh-dev}"
TOKEN="${WATCH_TOKEN:-}"
BASE_URL="${WATCH_BASE_URL:-https://tsecbench.zc.tencent.com}"
AUDIT_FILE="${WATCH_AUDIT_FILE:-$DSH_HOME_/storages/xiaochang-run-audit.jsonl}"
SAMPLE_S="${WATCH_SAMPLE_S:-120}"
STALL_S="${WATCH_STALL_S:-1500}"
BALANCE_WARN="${WATCH_BALANCE_WARN:-40}"
LOG="${WATCH_LOG:-$DSH_HOME_/storages/campaign-watch.jsonl}"
ALERT_FILE="${WATCH_ALERT_FILE:-$DSH_HOME_/storages/jintuo-alerts.jsonl}"

if [ -z "$TOKEN" ]; then
  echo "watch-campaign: WATCH_TOKEN is required" >&2
  exit 2
fi
mkdir -p "$(dirname "$LOG")"

alert() { # kind summary [detail]
  printf '{"at":%s,"kind":"%s","summary":"%s","detail":"%s"}\n' \
    "$(($(date +%s%N) / 1000000))" "$1" "$2" "${3:-}" >> "$ALERT_FILE"
}

LAST_SCORE=""
LAST_SCORE_AT=0
STALL_ALERTED=0

platform() { # → "completed score containers"
  curl -s -m 15 -H "BENCHMARK_TOKEN: $TOKEN" "$BASE_URL/openapi/v1/challenges" \
    | python3 -c "
import json, sys
try:
    cs = json.load(sys.stdin)
except Exception:
    print('-1 -1 -1'); raise SystemExit
done = [c for c in cs if c.get('is_completed')]
open_ = sum(1 for c in cs if c.get('container_status') in ('available', 'pending'))
print(len(done), sum(c.get('total_score', 0) for c in done), open_)
"
}

kimi_balance() { # → 数字 或 -1
  [ -z "${WATCH_KIMI_KEY:-}" ] && echo -1 && return
  curl -s -m 15 -H "Authorization: Bearer $WATCH_KIMI_KEY" "https://api.moonshot.cn/v1/users/me/balance" \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('data', {}).get('available_balance', -1))
except Exception:
    print(-1)
"
}

ds_balance() { # → 数字 或 -1
  [ -z "${WATCH_DS_KEY:-}" ] && echo -1 && return
  curl -s -m 15 -H "Authorization: Bearer $WATCH_DS_KEY" "https://api.deepseek.com/user/balance" \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('balance_infos', [{}])[0].get('total_balance', -1))
except Exception:
    print(-1)
"
}

while true; do
  NOW_MS=$(($(date +%s%N) / 1000000))
  P=$(platform)
  SCORE=$(echo "$P" | awk '{print $2}')
  COMPLETED=$(echo "$P" | awk '{print $1}')
  CONTAINERS=$(echo "$P" | awk '{print $3}')

  AUDIT_AGE=$(( $(date +%s) - $(stat -c %Y "$AUDIT_FILE" 2>/dev/null || echo 0) ))
  [ ! -f "$AUDIT_FILE" ] && AUDIT_AGE=999999

  KIMI=$(kimi_balance)
  DS=$(ds_balance)
  USAGE_LINES=$(wc -l < "$DSH_HOME_/storages/llm-usage.jsonl" 2>/dev/null || echo 0)

  # 进程存活：找 profile=headless 的战役主进程
  PROC_N=$(pgrep -f "profile=headless" | wc -l | tr -d " ")

  printf '{"at":%s,"completed":%s,"score":%s,"containers":%s,"auditAgeS":%s,"procs":%s,"kimiBalance":%s,"dsBalance":%s,"usageLines":%s}\n' \
    "$NOW_MS" "$COMPLETED" "$SCORE" "$CONTAINERS" "$AUDIT_AGE" "$PROC_N" "$KIMI" "$DS" "$USAGE_LINES" >> "$LOG"

  # 告警判定
  if [ "$SCORE" != "-1" ]; then
    if [ "$LAST_SCORE" != "" ] && [ "$SCORE" = "$LAST_SCORE" ]; then
      STALL=$(( $(date +%s%N) / 1000000 - LAST_SCORE_AT ))
      if [ "$STALL" -gt $((STALL_S * 1000)) ] && [ "$STALL_ALERTED" = 0 ]; then
        alert "campaign-stall" "platform score stalled at $SCORE for $((STALL/60000))min" "procs=$PROC_N auditAgeS=$AUDIT_AGE"
        STALL_ALERTED=1
      fi
    else
      LAST_SCORE="$SCORE"
      LAST_SCORE_AT=$(($(date +%s%N) / 1000000))
      STALL_ALERTED=0
    fi
  fi
  [ "$PROC_N" -eq 0 ] && alert "campaign-driver-down" "no campaign process (profile=headless)" "score=$SCORE"
  [ "$AUDIT_AGE" -gt "$STALL_S" ] && alert "campaign-audit-stale" "audit file stale ${AUDIT_AGE}s" "score=$SCORE procs=$PROC_N"
  if [ "$KIMI" != "-1" ] && awk "BEGIN{exit !($KIMI < $BALANCE_WARN)}"; then
    alert "balance-low-kimi" "kimi balance ¥$KIMI below ¥$BALANCE_WARN"
  fi
  if [ "$DS" != "-1" ] && awk "BEGIN{exit !($DS < $BALANCE_WARN)}"; then
    alert "balance-low-deepseek" "deepseek balance ¥$DS below ¥$BALANCE_WARN"
  fi

  sleep "$SAMPLE_S"
done
