# 主程序 demo 对接资料

主程序 demo 使用 C，面向 Linux，提供可供对接方参考的调用示例。实现位于 `apps/host-demo`。

- [角色与职责](../architecture/system-context.md#主程序的集成边界)
- [命令、启动及进程结果](../architecture/cli-commands.md)
- [会话错误与恢复边界](../architecture/session-errors.md)
- [文件交接](../architecture/file-handoff.md)

示例按上述契约管理进程和文件，业务调度与报告生成由 camctl 负责。实际对外传输由对接方接入；不因示例存在而推定传输系统的失败恢复行为。
