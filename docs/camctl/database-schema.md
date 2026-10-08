# 数据库结构与字段归属

[设计入口](README.md) · [数据库执行与事务](persistence-runtime.md) · [历史存储](history-storage.md)

状态库的物理设计按职责分为以下专题。本页提供表目录和阅读路线；字段、约束及存储判定在对应专题定义，业务行为由架构文档定义。物理上拆分记录不改变业务对象的身份、事务边界或恢复责任。

## 按问题查阅

等待条件的事实归属与事务见[等待专题](database/waiting.md)，清理接手见[清理协调](database/cleanup-coordination.md)，汇总见[产物清理状态](database/output-cleanup-state.md)，历史报告字段影响见[报告目录](database/report-changes.md)。联合验证见[一致性验收](database/consistency-verification.md)。

| 要回答的问题 | 定义位置 |
| --- | --- |
| 怎样检查结构、事件规则、报告依赖及公共格式？ | [检查命令与覆盖范围](../../scripts/README.md) |
| 完整建表语句和索引在哪里？ | [结构定义](database/schema/README.md) |
| 保存事件时允许修改哪些字段，怎样验证状态转换？ | [事件转换规则](database/event-transitions.md) |
| 报告字段依赖哪些历史事实，哪些变化需要报告？ | [字段依赖说明](database/report-dependencies.md)与[报告变化目录](database/report-changes.md) |
| 部署使用哪个 SQLite 运行库，怎样检查其能力？ | [运行库与部署要求](sqlite-runtime.md) |
| 哪些生产工作尚未完成，如何验收？ | [模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)与[一致性验收](database/consistency-verification.md) |
| 哪些事实必须共同提交，提交未知时如何恢复？ | [事务接口](database/transactions.md) |
| 各表如何表示身份、时间、空值和枚举？怎样选择列、子表或 JSON？ | [公共存储规则](database/common.md) |
| 计划和动作如何保存原输入、受理依据、运行状态及最终错误？ | [计划与动作](database/plans-actions.md) |
| 各类动作的固定执行定义包含哪些 JSON 字段？ | [动作执行定义](database/execution-definitions.md) |
| 如何保存自动预览关联、固定来源和逐项取回状态？ | [来源关联与取回明细](database/sources-obtaining.md) |
| 如何保存清理目标、删除限制、取消目标及逐交付处理？ | [清理与取消明细](database/cleanup-cancellation.md) |
| 正式产物、普通交付、设备文件、拷贝和中间文件如何关联？ | [产物、交付与文件记录](database/outputs-files.md) |
| 如何保存操作尝试、设备活动及录像内部检查与修复？ | [操作尝试与设备执行记录](database/operations-devices.md) |
| 如何保存诊断、报告、同步、ACK、可信时间和数据库身份？ | [报告、运行状态与数据库元信息](database/reports-runtime.md) |
| 历史事件、事务、对象关联、快照及维护进度如何组织？ | [历史基础表与快照表](database/history.md) |

首次阅读先了解[公共存储规则](database/common.md)，再按负责的业务进入对应表专题。查询历史状态时，结合[历史状态查询](historical-state-query.md)；设计写事务时，结合[数据库执行与事务](persistence-runtime.md)。

保存与恢复事件时，结合[事件转换规则](database/event-transitions.md)和[历史格式](database/history-formats.md)；生成报告时，结合[公共字段契约](../architecture/report-format.md)和[字段依赖说明](database/report-dependencies.md)。这些专题定义有效规则，实施进度由[模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)跟踪。

## 主要业务对象与主表边界

计划、动作、正式产物、普通交付、输入文件诊断和状态报告分别建立主表。产物之间的来源关联由 `output_origins` 表保存，自动预览取回与来源拍摄的关联由 `auto_preview_links` 表保存，取回和范围清理的来源成员由 `action_dependencies` 表保存。取回的逐来源选择与逐目标处理分别由 `obtain_source_selections`、`obtain_items` 保存，清理的逐目标处理由 `cleanup_items` 保存。公共操作流程及其尝试分别由 `operation_runs`、`operation_attempts` 保存，文件拷贝专属状态由 `file_copies` 保存。

设备文件的身份、定位信息、业务归属及可靠取得的文件事实由 `device_files` 保存；主机中间文件的归属、用途及清理状态由 `intermediate_files` 保存。取消动作的逐目标处理由 `cancel_items` 保存，针对目标取回中各份交付的处理由 `cancel_delivery_items` 保存。录像内部检查与修复的决定、进度和结果由 `recording_processing` 保存，动作在设备上启动的持续活动及其已知状态由 `device_activities` 保存。

显式状态同步的固定起点、开始依据和责任结束情况由 `state_syncs` 保存，客户端累计确认位置、可信历史时间下界及中间文件清理继续位置由 `runtime_state` 保存。业务投影按下表职责组织；实施进度由对应模块计划跟踪。

历史事实及提交分组分别由 `history_events`、`history_transactions` 保存，对象自身历史关联由 `entity_event_links` 保存，报告选择目录由 `report_entity_changes` 保存。对象快照及其维护进度分别由 `entity_snapshots`、`entity_snapshot_progress` 保存，数据库元信息由 `database_metadata` 保存。跨表提交与恢复边界见[事务接口](database/transactions.md)。

| 主表 | 一条记录表示什么 | 归属与生命周期 |
| --- | --- | --- |
| [plans](database/plans-actions.md) | 一个成功受理的计划实例 | 保存首次成功受理的请求关联和计划自身状态；动作分别存储。计划结束后仍可被后续请求引用 |
| [actions](database/plans-actions.md) | 一个已受理计划中的动作实例 | 每个动作属于一个计划；各动作类型共用身份与公共执行状态，专属处理记录按各自职责组织 |
| [outputs](database/outputs-files.md#正式产物与普通交付) | 一份已登记的正式产物，对应一份文件 | 属于产生它的动作；原文件、预览和修复成品分别有自己的产物记录。文件清理改变可用性，记录永久保留 |
| [deliveries](database/outputs-files.md#正式产物与普通交付) | 一个取回动作针对一份正式产物创建的普通交付 | 同时关联取回动作和实际产物；同一产物可被不同取回交付，跨会话恢复沿用原交付 |
| [plan_file_diagnostics](database/reports-runtime.md#输入诊断与状态报告) | 一次输入处理中形成的完整文件诊断 | 不要求已有计划或合法请求身份；提交后内容固定。另一次输入再次失败时形成新的诊断，历史重建复用原记录 |
| [reports](database/reports-runtime.md#输入诊断与状态报告) | 一份已登记的逻辑状态报告 | 保存固定身份和生成依据，并关联后续文件处理事实；可以覆盖多个计划及输入诊断，不属于某个单独计划 |

这些主表保存可直接查询的业务记录，权威业务事实仍由不可变历史保存。历史与相关主表、子表和查询目录按同一业务事务更新；输入诊断等内容固定的记录也遵守这一原子保存规则。

## 关联表、处理记录与基础表目录

下表与上面的六张主表共同构成完整表目录。历史对象与快照范围按业务责任明确划分，见[对象归属](database/history-formats.md#对象目录与归属)。

| 表 | 记录职责与定义位置 |
| --- | --- |
| `output_origins` | [预览或修复成品与原产物的关系](database/outputs-files.md#产物来源关系) |
| `motor_notifications` | [电机发送意图与结果](database/workflow-fields.md#电机发送事实) |
| `auto_preview_links` | [自动取回与来源拍摄的关联及能力依据](database/sources-obtaining.md#自动预览取回关联) |
| `action_dependencies` | [取回或范围清理的固定来源成员](database/sources-obtaining.md#取回与范围清理的来源成员) |
| `obtain_source_selections` | [取回的逐来源选择进度](database/sources-obtaining.md#取回的来源选择与目标明细) |
| `obtain_items` | [取回的逐目标处理、读取资格及依赖](database/sources-obtaining.md#取回的来源选择与目标明细) |
| `cleanup_items` | [清理的逐目标处理及删除限制](database/cleanup-cancellation.md#清理目标明细) |
| `cancel_items` | [取消的逐目标处理](database/cleanup-cancellation.md#取消目标明细) |
| `cancel_delivery_items` | [取消目标中的逐交付处理](database/cleanup-cancellation.md#取消动作的交付处理明细) |
| `operation_runs` | [具体操作责任及整体进度](database/operations-devices.md#操作流程与尝试的公共模型) |
| `operation_attempts` | [操作流程的一次业务尝试](database/operations-devices.md#操作流程与尝试的公共模型) |
| `device_activities` | [设备持续活动及可靠观察](database/operations-devices.md#设备活动记录) |
| `recording_processing` | [录像内部检查和修复的决定、进度与结果](database/operations-devices.md#录像内部检查与修复记录) |
| `device_files` | [设备文件身份、归属及文件事实](database/outputs-files.md#设备文件记录) |
| `file_copies` | [拷贝进度、重拷轮次与校验](database/outputs-files.md#文件拷贝记录) |
| `intermediate_files` | [主机中间文件用途、保留及清理](database/outputs-files.md#中间文件记录) |
| `state_syncs` | [显式状态同步责任](database/reports-runtime.md#状态同步记录) |
| `runtime_state` | [累计确认、可信时间及清理继续位置](database/reports-runtime.md#全局运行状态) |
| `history_events` | [权威历史事件](database/history.md#历史事件与事务分组) |
| `history_transactions` | [历史事件的完整提交分组](database/history.md#历史事件与事务分组) |
| `entity_event_links` | [实际变化对象的事件关联及计数](database/history.md#对象与事件关联) |
| `report_entity_changes` | [按事件公开影响登记的报告对象目录](database/report-changes.md) |
| `entity_snapshots` | [完整历史边界处的对象快照](database/history.md#对象快照与维护进度) |
| `entity_snapshot_progress` | [对象尚未被快照包含的变化次数](database/history.md#对象快照与维护进度) |
| `database_metadata` | [数据库标识、实例身份、格式及目录绑定](database/reports-runtime.md#数据库元信息) |

## 数据库之外的状态

| 状态或数据 | 保存与恢复依据 |
| --- | --- |
| 普通日志、日志副本和跨会话故障轮次 | 普通日志及[独立故障标记文件](../architecture/log-failure-marker.md#跨-run-的独立状态文件)；状态库不可用时仍须保留相应诊断与交付能力 |
| 会话接纳资格、报告工作资格 | 内核锁及实际进程状态；数据库记录不能代替持锁事实 |
| 调度缓存、已吸收计划序号、单调钟计时及本次队列等待 | 会话内存；重启根据可靠业务记录重新建立，不能回放旧会话的锁、协程或单调钟值 |
| 报告生成任务、分页位置与本次尝试观察边界 | 活跃任务与会话处理状态；重启通过冻结报告、业务变化、同步责任和实际文件恢复，新会话按规则重新取得处理机会 |
| 设备绑定、驱动能力及部署配置 | 本地配置与驱动定义；用户要求、设备身份、驱动选择及生效拍摄参数随动作保存，本地执行配置在每次运行加载，实际判断依据随对应历史事实保存 |
| 输入计划文件、正式文件、交付文件和报告 JSON | 文件系统及各自所有权规则；数据库保存身份、事实和恢复依据，完整报告 JSON 按冻结依据重建 |

这些边界分别见[会话协议](../architecture/protocol-session.md)、[报告维护](../architecture/report-maintenance.md)、[报告进程](report-runtime.md)及[文件交接](../architecture/file-handoff.md)。报告的每次成功发布仍属于持久化历史，不能与生成进程的临时状态混为一谈。

## 规格维护与实施边界

字段和约束在所属专题维护；各表共用的表示与事务原则只在[公共存储规则](database/common.md)定义。字段验证要求随所属定义保存，跨流程协作检查见[一致性验收](database/consistency-verification.md)。新增字段按职责加入对应专题，不在本入口扩展详细定义。

目标字段、状态组合及跨表契约由对应专题定义；字段专题与 SQL 的对应关系见[结构目录](database/schema/README.md)，检查命令见[脚本说明](../../scripts/README.md#数据库结构与运行库)。生产实现遵守[事务接口的验证门槛](database/transactions.md#实现与验证门槛)，进度由模块计划跟踪。规格检查不代替生产持久化实现、软件集成或设备验证。
