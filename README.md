# 金柝（shence-jintuo）—— 守护启动器

神策（SHENCE）项目群 P2。**独立父进程**（非插件）：插件救不了死掉的宿主，金柝以启动器/父进程形态监督 DSH。

## 功能

- 启动 / 监督 DSH web 进程（崩溃自动拉起，防重启风暴退避）；
- 资源监控：内存/堆/并发水位、进程存活；
- **只告警不代决策**：向运行中的主 agent 发资源压力事件（内存水位、崩溃、恢复完成），由主 agent 自行调整；
- 崩溃后拉起 DSH 并恢复会话（依赖 DSH 会话落盘）。

## 分工（与虎符）

- 金柝管**进程级恢复**：拉起 + 会话存在；
- 虎符管**调度级恢复**：醒来后重挂心跳 + 按账本恢复未完成工作项。

## 附带：DSH 补丁 overlay

- 重启后 goal 重新武装（goal-round-driver 无条件 disarm 修复）；
- 子代理崩溃/中断向父会话投递通知；
- 内存治理（heap 上限、预警线、优雅重启）；
- 取证增强：5s 采样、退出证据转储（free/meminfo/pressure/dmesg）、dmesg 逃逸副本。

## 关联

- 文档：[shence-docs](https://github.com/Zoot-running/shence-docs)（含 00:37 事故复盘 RECOVERY.md）
