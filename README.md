# 金柝（shence-jintuo）—— 守护启动器

神策（SHENCE）项目群 P2。**独立父进程**：插件救不了死掉的宿主，金柝以启动器形态监督 DSH。

## 组件

- `jintuo.sh` —— 守护启动器（父进程）：拉起/监督 DSH web；5s 资源采样与告警水位
  （写 `$DSH_HOME/storages/jintuo-alerts.jsonl`，**只告警不代决策**）；崩溃自动拉起
  （防重启风暴退避 3/10/30s）；退出证据转储（free/meminfo/pressure/dmesg）；内核 OOM
  行逃逸副本（每分钟落普通文件，扛 VM 重启）。
  - 配置：`JINTUO_DSH_HOME / JINTUO_WORKDIR / JINTUO_PORT / JINTUO_HEAP_MB /
    JINTUO_SAMPLE_S / JINTUO_WARN_MB / JINTUO_ALERT_FILE / JINTUO_NO_OPEN`（均有默认）。
- `guard-runner.sh` —— 校场 L4 跑分进程守护（2026-09-06）：进程死亡→重拉（同一命令，
  `xiaochang_start` 按快照续跑）；心跳失联→判定假死杀重拉（卡死循环也覆盖）；内存
  超限只告警；5 连快速失败放弃。混沌测试 2/2（kill -9 重拉 + 19s stale 重拉，告警落盘）。
  - 配置：`GUARD_DSH_HOME / GUARD_CMD(必填) / GUARD_AUDIT_FILE / GUARD_STALE_S /
    GUARD_HEAP_MB / GUARD_SAMPLE_S / GUARD_ALERT_FILE / GUARD_WORKDIR / GUARD_STDOUT`。
- `plugin/` —— 告警读取插件（@shence/jintuo-alerts）：尾随告警文件，投递 `jintuo/alert`
  事件并注册 `jintuo_alerts` 工具供模型查询实例健康。
- `patches/check.sh` —— 目标 checkout 三个已知问题的存在性检查（goal-restart-disarm、
  child-interrupted-notice、堆上限声明）。
- `patches/goal-rearm.patch` —— goal-round-driver 重启重武装补丁（上游仍 PRESENT）。
- `tests/chaos.sh` —— L3 混沌测试：N 轮杀 web → 断言自动恢复 + 告警/证据记录。

## 验收记录（2026-09-05）

- ✅ L3 混沌 3/3：每轮 ~44-51s 恢复，告警 0→3 条，证据日志 103→309 行。
- ✅ 告警插件实测：模型经 jintuo_alerts 工具读到 3 条 web-exit 告警。
- 分工（章程）：金柝管进程级恢复（拉起+会话存在）；虎符管调度级恢复（心跳+未完成工作项）。

## 部署

```bash
JINTUO_WORKDIR=/path/to/deepseek-harness bash jintuo.sh
# dev 实例当前即由金柝守护（端口 3081，堆 2GB）
```

## 9/8 事故补遗：战役启动器的 home 隔离红线（2026-09-08）

校场战役启动器**必须**硬校验 DSH_HOME（run 6 事故：漏设 DSH_HOME → 新版 CLI 默认落生产
home ~/.dsh → 用新格式覆写 ~/.dsh/.credentials.yaml → 生产 web 旧 parser 崩溃）：

```bash
# 启动器第一段（campaign-launch 脚本模板）
REQUIRED_HOME=/home/zrn/.dsh-dev
if [ "${DSH_HOME:-}" != "$REQUIRED_HOME" ] || [ ! -d "$REQUIRED_HOME" ]; then
  echo "FATAL: DSH_HOME must be $REQUIRED_HOME (campaign home isolation); got '${DSH_HOME:-<unset>}'" >&2
  exit 2
fi
export DSH_HOME=$REQUIRED_HOME
```

原理：DSH 凭据文件格式存在版本错位——生产（旧 parser）只认扁平映射
`DEEPSEEK_API_KEY: sk-...`（无 version/包装层，非字符串值一律拒绝）；新版运行时会写
`{version:1, refs:{...}}`。两个 home 的凭据文件必须彻底分开。
