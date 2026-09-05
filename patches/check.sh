#!/bin/bash
# 金柝补丁检查：检测目标 checkout 中三个已知问题是否仍存在。
# 用法：bash patches/check.sh <checkout路径>
# 输出每个问题的状态：PRESENT（需要补丁）/ FIXED（上游已修）。
set -u
CHECKOUT="${1:?usage: check.sh <checkout>}"

echo "== 金柝补丁检查：$CHECKOUT"

# 1. goal-round-driver 重启后无条件 disarm（00:37 事故直接诱因）
if grep -q "process restart silently disarms" "$CHECKOUT/packages/goal/goal-round-driver/src/index.ts" 2>/dev/null; then
  echo "[1] goal-restart-disarm        PATCHED   （活跃 goal 在驱动装载时重新武装）"
elif grep -q "never inherits hidden" "$CHECKOUT/packages/goal/goal-round-driver/src/index.ts" 2>/dev/null; then
  echo "[1] goal-restart-disarm        PRESENT   （重启后 goal 被 disarm，自动轮不再触发）"
else
  echo "[1] goal-restart-disarm        FIXED"
fi

# 2. 子代理中断无父会话通知
if ! grep -rq "child interrupted\|CHILD_INTERRUPTED" "$CHECKOUT/packages/subagent/subagent/src/" 2>/dev/null; then
  echo "[2] child-interrupted-notice    PRESENT   （在途子代理随进程消亡时静默变 ready，无事件）"
else
  echo "[2] child-interrupted-notice    FIXED"
fi

# 3. 内存治理（heap 上限/预警/证据转储由金柝启动器提供，此条记录宿主配置建议）
grep -q "max-old-space-size" "$CHECKOUT/package.json" 2>/dev/null \
  && echo "[3] heap-cap-declared          PRESENT   （宿主 package.json 声明了堆上限，请勿双写）" \
  || echo "[3] heap-cap-declared          ABSENT    （由金柝启动器 JINTUO_HEAP_MB 管控）"

echo "== 完成"
