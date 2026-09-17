# C 接入模块与本地演示

本组件使用 C，面向 Linux，提供第三方现有主程序可以直接调用的接入模块，以及使用同一模块的终端演示程序。实现位于 `apps/host-demo`，目前处于规格阶段。

首先阅读[设计规格](design.md)：模块异步管理 camctl 进程，接收已保存的计划路径，并在主程序显式调用时同步将 `ready` 文件逐个移动到 `processing`。第三方负责实际传输和后续文件处理。

- [角色与职责](../architecture/system-context.md#主程序的集成边界)
- [命令、启动及进程结果](../architecture/cli-commands.md)
- [会话错误与恢复边界](../architecture/session-errors.md)
- [文件交接](../architecture/file-handoff.md)

公共协议由以上架构专题维护；模块调用、有限自动重试和本地演示的契约由设计规格维护。业务调度与报告生成由 camctl 负责。
