# camctl 设计与实现资料

camctl 是嵌入式主机上的 Python CLI，负责计划受理、调度、设备控制、状态保存及文件交接，生产代码位于 `apps/camctl`。本目录说明这些职责如何在进程、线程、数据库和文件接口中实现；全局业务行为在 `docs/architecture` 定义，公共机器协议只在根目录 `protocol` 维护。

## 第一次阅读

1. 先读[设计总览](../architecture/README.md)与[概念](../architecture/concepts.md)，了解计划、动作、正式产物、交付和报告之间的关系。
2. 按[全局阅读路线](../architecture/reading-guide.md)了解受理、执行、文件交接和报告的正常流程及失败规则。
3. 阅读[实现总览](implementation.md)，理解运行环境、线程与进程分工、模块协作和接口完成含义。
4. 按下表进入自己负责的实现专题，最后按[软件验证](verification.md)核对跨模块契约及验收要求。
5. 进入实施时，按[第一版实施路线图](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)确定阶段依赖，再从[模块计划目录](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)进入所属模块的具体任务。先核对[共享实施契约](module-contracts.md)、前置交付和实际代码，再按任务的失败测试、实现步骤与门禁推进。

设计中的行为契约、不变量和失败语义必须保持。标为“建议”的内部命名、物理表和接口组织可以根据实际数据流调整。库能力核验、测试通过与真实设备联调是不同层次的证据，不能互相代替。

## 按实现职责查阅

| 要回答的问题 | 文档及范围 |
| --- | --- |
| 系统如何分工，工作在哪里执行？ | [实现总览](implementation.md)：目标环境、技术选型、协程／线程／进程、模块和调度接口 |
| 模块接口怎样组织，事务和结果由谁负责？ | [模块协作与实施契约](module-contracts.md)：类型归属、依赖方向、完整提交、取消接手和组合门禁 |
| 分批读取怎样表达继续和结束？ | [分页结果契约](module-contracts.md#分页结果契约)：批次字段、只读结束属性、空有效页、续读范围及失败语义 |
| 怎样解析和校验输入，保持数值精确？ | [输入类型与数字适配](data-types.md)：分阶段校验、内部类型、JSON 与配置数字、编码适配 |
| 数据库如何分表，字段和关联保存在哪里？ | [数据库设计入口](database-schema.md)：完整表目录，以及公共规则、字段、执行定义和关联记录的专题导航 |
| 数据库调用何时算完成，取消后怎样处理？ | [数据库执行、事务与缓存](persistence-runtime.md)：格式检查、队列、连接、事务、物理结构和缓存 |
| 历史记录怎样表达事实与顺序？ | [历史存储](history-storage.md)：事件、两类序号、事务边界、查询目录、格式及 SQLite 运行设置 |
| 怎样恢复某个历史位置并重建报告？ | [历史状态查询](historical-state-query.md)：正反向路径、一致读取、分页、分批编码和性能边界 |
| 历史快照何时生成，是否影响会话退出？ | [快照维护](history-snapshots.md)：对象范围、累计次数、候选发现、分批处理和退出 |
| 驱动返回什么证据，文件如何分段处理？ | [设备驱动与文件执行](file-runtime.md)：结果分类、线程池、流式传输、取消和文件所有权 |
| 日志满载时怎么办，怎样安全写同一文件？ | [日志执行与适配](logging-runtime.md)：入口、队列水位、采样、取消、多进程轮换及持锁复制 |
| 报告进程如何启动、复用、停止和回收？ | [报告进程与通信](report-runtime.md)：任务身份、结果确认、超时、管道和工作锁 |
| 如何证明上述软件模块能共同工作？ | [软件验证与实施顺序](verification.md)：单元测试、集成测试、契约验证和阶段方向 |

## 实施计划与验证依据

| 文档 | 用途 |
| --- | --- |
| [第一版实施路线图与模块计划目录](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口) | 引用 15 个模块计划及跨组件集成计划，按任务编排阶段、前置交付和完整业务链门禁 |
| [跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md) | C 进程启动与原组收场、客户端协议接入、真实业务闭环及全量验收映射 |
| [跨模块契约检查](verification.md#跨模块契约检查) | 配置、取消、文件、历史和报告等边界的组合验证入口 |
| [依赖能力核验与设备联调输入](integration-readiness.md) | 有日期及环境范围的库能力实验，以及真实设备联调所需证据 |

各专题的验收要求共同约束实现。[真实设备联调](integration-readiness.md#设备证据与联调输入)及硬件性能测量单独安排，不作为第一版软件集成测试的运行前提或通过门槛；软件结论向部署与联调的交接口径见[部署交接与待核验项](verification.md#部署交接与待核验项)。

## 运行与主程序对接

- [CLI 命令与主程序调用](../architecture/cli-commands.md)：命令、输入路径、进程结果与退出。
- [请求与会话](../architecture/protocol-session.md)：接纳、会话接管和退出检查。
- [本地配置](../architecture/configuration.md)与[初始化](../architecture/initialization.md)：默认值、生效范围和显式建库。
- [部署联调样例](../architecture/deployment-example.md)：串联配置、初始化、能力说明、输入和报告确认。
- [Action6 录像演示与相机资料采集](../hardware/camera-demo-validation.md)：从安装包提取配置和计划生成器，通过 C host 完成 Action6 普通录像、独立取回、文件领取及报告确认；Windows 用于资料采集，完整演示在 ARM Linux 验收。后续相机和延时接入由对应计划跟踪。
- [C 接入模块](../host-demo/design.md)：主程序管理 CLI 进程及领取文件的责任。
- [电机控制动作](../architecture/motor-control.md)与[主程序通知协议](../../protocol/host-notifications.md)：有效窗口、发送意图、单向通知及不重发的恢复责任。
