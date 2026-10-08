# C 接入模块与本地演示

本组件使用 C，面向 Linux，提供第三方现有主程序可以直接调用的接入模块，以及使用同一模块的终端演示程序。[源码与接入说明](../../apps/host-demo/README.md)包含构建、三个调用时机、配置范围和终端命令。

首先阅读[设计规格](design.md)：模块异步管理 camctl 进程，接收已保存的计划路径，并在主程序显式调用时同步将 `ready` 文件逐个移动到 `processing`。第三方负责实际传输和后续文件处理。

[实现设计](implementation.md)整理 arm64 Ubuntu 18.04 的技术选型、公开 C 接口、人工部署参数、串行提交、日志、资源上限，以及源码和静态库交付方式。[验证记录](verification.md)区分开发环境已执行的测试和真实 camctl、目标主机及断电验收。

进程管理使用独立进程组、保留退出记录和原组收场后最终回收的方式。Linux 软件验证涵盖正常与异常退出、遗留线程、并行提交及共享服务端；实施和复验记录见[跨组件计划 I1/I2](../superpowers/plans/2026-09-30-camctl-integration.md#i1-独立进程组明确启动结果与主程序约定)。

- [角色与职责](../architecture/system-context.md#主程序的集成边界)
- [命令、启动及进程结果](../architecture/cli-commands.md)
- [会话错误与恢复边界](../architecture/session-errors.md)
- [文件交接](../architecture/file-handoff.md)

公共协议由以上架构专题维护；模块调用、有限自动重试和本地演示的契约由设计规格维护。业务调度与报告生成由 camctl 负责。
