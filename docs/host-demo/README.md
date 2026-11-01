# C 接入模块与本地演示

本组件使用 C，面向 Linux，提供第三方现有主程序可以直接调用的接入模块，以及使用同一模块的终端演示程序。[源码与接入说明](../../apps/host-demo/README.md)包含构建、业务调用时机、主程序停止、配置范围和终端命令。

首先阅读[设计规格](design.md)：模块在后台管理 camctl 进程，递交计划的调用等待输入处理结果保存后返回，原计划文件随后可删除；主程序显式调用领取入口时，同步将 `ready` 文件逐个移动到 `processing`。第三方负责实际传输和后续文件处理。

[实现设计](implementation.md)整理 arm64 Ubuntu 18.04 的技术选型、公开 C 接口、电机通知回调、用户目录默认路径及部署参数、串行提交、日志、资源上限，以及源码和静态库交付方式。[验证记录](verification.md)区分开发环境已执行的测试和真实 camctl、目标主机及断电验收。

进程管理使用独立进程组、保留退出记录和原组收场后最终回收的方式。Linux 软件验证涵盖正常与异常退出、遗留线程、并行提交及共享服务端；实施和复验记录见[跨组件计划 I1/I2](../superpowers/plans/2026-09-30-camctl-integration.md#i1-独立进程组明确启动结果与主程序约定)。

- [角色与职责](../architecture/system-context.md#主程序的集成边界)
- [命令、启动及进程结果](../architecture/cli-commands.md)
- [会话错误与恢复边界](../architecture/session-errors.md)
- [文件交接](../architecture/file-handoff.md)

公共协议由以上架构专题维护；模块调用、有限自动重试和本地演示的契约由设计规格维护。业务调度与报告生成由 camctl 负责。

电机控制接入另见[主程序通知协议](../../protocol/host-notifications.md)、[电机动作](../architecture/motor-control.md)和[按消息类型注册回调](implementation.md#按消息类型注册回调)。主程序注册仅含 `int position` 参数的回调，host 负责消息解析和分发；软件接入与组合验证见[组件交付复验](verification.md#电机通知组件交付复验2026-10-08)和[真实组合复验](verification.md#电机通知真实组合复验2026-10-08)。真实电机运动及第三方主程序业务范围仍按[目标环境联调清单](../superpowers/plans/2026-10-11-camctl-target-integration-runbook.md)核验。

默认部署使用运行账户的 `$HOME/.camctl`，主程序可通过[路径补齐接口](implementation.md#用户目录与路径补齐)取得绝对路径；演示程序省略路径参数时采用相同布局。

Python CLI 的完整 TOML 字段、默认值、合法范围和自定义交接目录示例见[配置项参考](../camctl/configuration-reference.md)。其中的 C 配置对应表说明两端应如何使用同一组路径。

软件更新在已初始化实例中通过 `camctl_host_update(bundle_path, client_data_path)` 调用。模块内部停止普通工作、应用更新、处理失败恢复，并在允许启动时重建普通资源和安排 `run`；接口返回 `0` 表示新包可靠提交且普通运行已安排，返回 `-1` 表示本次更新失败。更新诊断直接进入 `processing`。普通初始化配置设置 `updater_path` 后，会先自动恢复未完成事务，再安排普通运行。主程序无需解释内部状态或手动恢复、重启。交付物和行为契约见[更新接入规格](../architecture/software-update-interface.md)及[组件版本与软件更新](../architecture/versioning-and-updates.md)，实际调用见[源码说明](../../apps/host-demo/README.md#更新-camctl)。

部署人员与主程序接入人员可按[版本发布与现场更新操作](../camctl/software-updates.md)构建和应用更新包；C 主程序按更新接口的 `0` 或 `-1` 处理结果。
