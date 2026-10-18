# 设备文件清理与通用执行恢复实施计划

> **执行方式：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 按任务执行；复选框只在实际实施与验证后勾选。

**目标：** 闭合正式产物清理的文件查询、调用、保存、取消和恢复责任，在共用 ADB 文件适配层接入真实删除，以 OSMO 360 II 作为首个目标设备验收。

**架构：** 独立文件查询端口服务清理，拍摄状态查询保持原责任。清理框架持有原意图、实际结果与唯一保存申请，通过持久化发现和启动恢复完成交接；设备使用受管调用，主机正式源复用既有文件执行器。

**技术栈：** Python 3.11、SQLite 事务与不可变历史、ADB、现有文件任务执行器、CLI 与 C host 发行链。

**规格：** [正式产物](../../architecture/outputs.md)、[独立清理](../../architecture/output-cleanup.md)、[预览取回](../../architecture/preview-obtaining.md)、[设备驱动边界](../../architecture/camera-capabilities.md)、[文件运行接口](../../camctl/file-runtime.md)、[ADB 执行](../../architecture/adb-execution.md)、[清理协调](../../camctl/database/cleanup-coordination.md)、[清理及取消记录](../../camctl/database/cleanup-cancellation.md)、[清理汇总](../../camctl/database/output-cleanup-state.md)、[操作字段](../../camctl/database/operation-fields.md)、[事务回执](../../camctl/database/transactions.md)、[会话协议](../../architecture/protocol-session.md)。内部签名、模块拆分及提交边界是实施建议；下文新增契约由 R0 先补入责任专题。

## 范围与独立交付

本计划负责实际文件操作和通用清理责任，不修改客户端入口策略。[设备能力声明计划](2026-10-18-device-capability-declarations.md)负责公共导出与前端；两份计划互不作为前置。本计划通过内部 `DriverDeclaration`、正式端口与 CLI 请求独立验收。公共导出若已集成，按当前配置选定的正式源选择主机或设备能力；内部设备文件声明不直接等于公共值，不另设设备清单。

删除继续使用独立的 `delete_action_outputs`，没有 `device_id`，也不进入拍摄 `actions[]`。目标来自明确产物 ID 或已有来源范围；OSV、LRF、主机 original／preview／repaired 各自独立，不因清理一份而级联删除关联文件。读取保护、同产物处理归属、原绑定与逐项结果继续按现行规格执行。计划输入、报告字段和数据库操作类别保持现有契约；历史采用责任专题登记的事件及来源引用。确需存储增量时先证明现有事实不足并修正规格，不预设数据库迁移。

修复作用于所有驱动和主机正式源。ADB 文件命令放共用模块，具体型号在合同中声明完整支持；不能按型号在清理框架中分支。Action6 可复用适配，但是否启用须独立满足工具、证据及恢复契约，其真机暂停安排保持；Hasselblad 没有设备删除命令时，内部设备文件删除仍为 false，主机正式源照常清理。配置决定新拍摄选用的源，实际清理始终按保存的 device_file_id／intermediate_file_id 及原绑定分流，不按当前公共值重新解释历史位置。

普通录像启动余量、原生延时参数域、结果等待、拍摄恢复、自动预览及大小估算由原专题维护，本计划不调整这些行为。现场验收不得自动初始化、重发 START 或在物理效果未知时重试。

## 已核实的通用缺口

以下是 2026-10-18 对当前生产路径的审查起点，不是实施完成记录。

| 路径 | 缺口与影响 | 任务 |
| --- | --- | --- |
| `cleanup_assembly.py`、驱动端口 | 清理依赖 `query_state/query_supported`，文件查询会误授拍摄状态能力。 | R1 |
| `cleanup_flow.py` 的意图、DELETE 和成员保存 | 临时申请及 key 没有覆盖全部持有阶段；返回后的故障可能丢失原结果或保存责任。 | R2 |
| `advance_cleanup`、`cleanup_results.py` | 主要从运行中动作发现，已保存 DELETE 没有与 QUERY 对等的原历史消费入口；终态残留可能漏掉。 | R3 |
| `lifecycle.py` 的清理装配 | 未接入启动恢复边界、固定历史上界和原操作恢复证据，旧 RUNNING 尝试缺少合法收场路径。 | R3 |
| `HostArtifacts` | async 方法直接执行 unlink／stat，未复用文件线程任务；还套用了设备调用期限。 | R4 |
| ADB 正式合同 | 当前没有完整 DELETE／FILE_QUERY 工厂；候选 rm 辅助函数不等于正式清理支持。 | R5 |

## 状态模型与交接规则

R0 将以下增量写入各责任专题。清理应同时判断五个独立维度，不能只看成员或动作是否终态。

| 维度 | 必须区分的状态 | 判定责任 |
| --- | --- | --- |
| 业务资格 | 普通推进、取消收场、成员终态、动作终态；以及来源、依赖、归属、绑定和预算 | 决定能否发起新的调用；既有终态不重开。 |
| 意图事务 | 原申请待提交、确认回滚、提交未知、可靠 ticket、可靠拒绝且无尝试 | 不可靠时只核实原 key，不派发、不另扣额度。 |
| 物理调用 | 本会话可靠未派发、在途或收场中、实际已返回、重启后派发未知 | 只有可靠未派发且资格仍成立时，原票据可调用一次；重启后的意图不产生补发许可。 |
| 本地保存 | 原返回已持有、验证诊断已固定、finish 待存、结果已存、成员待存、伴随流程待存、交接完成 | 每阶段唯一原申请、key 和时刻；等待取消不消灭责任。 |
| 文件事实 | 可靠缺席、最新有效在场、删除效果未知 | 可靠观察与调用错误可同时保存；后续可能删除使旧在场观察失效。 |

会话责任至少绑定 database_id、item_id、原票据／尝试、消费用途、完整原申请、原 key、原时刻及原证据。新 runtime／factory 接手同一会话集合，不持有旧连接或旧运行时回调。进程结束丢失的返回不能重构，只能依据持久化事实恢复。

### 派发、保存与重启

| 当前事实 | 下一步 |
| --- | --- |
| 原意图提交未知 | 核实原 key；仍无法核实则保留责任并按状态库错误处理。 |
| 原意图确认回滚 | 资格仍成立时重送原申请和 key；资格已失效则完成该申请的可靠处置，不构造新调用许可。 |
| 可靠 ticket，本会话可靠未派发 | 再检查取消和对应调用资格；允许时派发一次，否则沿原票据保存真实 `not_dispatched`，次数不退还。 |
| 调用在途，等待者取消或成员状态变化 | 原拥有者继续接回实际结果；不释放归属，不发冲突调用。 |
| 实际返回或保存回执未知 | 先持有原返回，再验证和保存；沿原申请核实，不重复物理调用。 |
| 原结果可靠保存，尚未交接 | 按原证据类型、版本及事务恢复并消费；无需当前驱动重新验证。 |
| 重启后旧尝试 RUNNING，缺少合法本地收场依据 | 保留 RUNNING 与诊断，阻止依赖该收场的新调用。 |
| 重启后具备旧本地收场、启动固定历史边界及原操作恢复支持 | 沿原尝试保存 UNKNOWN 恢复结果，再按原资格和剩余预算核实；不重放旧票据。 |

ADB 恢复使用原 `adb_foreground_recovery/v1` 与 `settlement.assumed`，不补造退出码、超时、宽限结果或丢失的原发生时刻。主机文件恢复使用自己的本地执行与进程重启依据，不能套用 ADB 假设。当前会话晚于启动历史上界的尝试不走旧尝试恢复。

### QUERY 与取消交错

以下有限核实边界需在 R0 明确写入清理专题：已派发查询接回后不再追加取消查询；尚无在途或可消费查询、自身可能 DELETE 且调用已收场时，最多一次必要查询，仍受原剩余额度约束。它只核实取消收场，不恢复普通 DELETE 重试。

| 查询及本成员事实 | 处理 |
| --- | --- |
| 已授 ticket，未派发，未取消且核实资格成立 | 原 ticket 派发一次。 |
| 未派发且取消已生效，本成员从未可能 DELETE | 阻止查询并保存原未派发结果；本项按未删除取消，仅解除自己的可撤销限制。其他成员的 DELETE 不构成本项删除错误。 |
| 未派发且已取消，本成员可能 DELETE、旧调用已收场且结果未知 | 若仍需一次取消核实，沿原流程和剩余 QUERY 额度执行；不创建新的普通重试。 |
| 查询在途或已返回后取消 | 保存实际结果。缺席则本项成功；在场且自身可能 DELETE 则 `canceled + file_delete_failed`；未知则 `canceled + delete_unconfirmed`。动作保持取消。 |
| 取消核实申请或结果保存失败／未知 | 保留原保存责任，按状态库错误处理，不伪造业务上的 `delete_unconfirmed` 终态。 |

已保存成员终态始终保持。没有自身 DELETE 的已派发查询仍接回实际观察：可靠缺席按上表成功；仅在场或未知时按未删除取消，不附加其他成员的删除错误，并仅解除本项可撤销限制。

### 文件事实与独立预算

先完成原调用收场和保存，再按下表判断普通推进；取消使用上表，终态仅处理合法残留责任。

| 最新有效事实 | 额度 | 普通推进 |
| --- | --- | --- |
| 可靠缺席／已有清理完成事实 | 任意 | 本地完成交接，不再 QUERY 或 DELETE。 |
| 在场，且之后无可能删除 | 删除有余 | 具备资格时按适用间隔发起新 DELETE。 |
| 在场，且之后无可能删除 | 删除耗尽 | 本项清理失败。 |
| 删除效果未知 | 查询有余 | 用同一成员的 QUERY 流程核实，不能借 DELETE 代替。 |
| 删除效果未知 | 查询耗尽 | 保存未知清理失败及限制，不以剩余 DELETE 额度继续。 |

计数跨 run 累计，新 DELETE 不刷新 QUERY；配置增加不重开终态，降低到已用次数以下合法但不再授予新尝试。正式产物清理的预算由 `output-cleanup.md` 维护：设备源和主机 original／preview／repaired 都使用已有独立清理预算。`operations-devices.md` 明确将预算交给所属业务契约；R0 据此收窄 `operation-fields.md` 中本地上限 1 的泛称，消除现有文档冲突，不新增主机配置。主机调用不设应用层完成期限，不填设备默认 timeout 或重试间隔。

## 任务顺序与实施步骤

R0 → R1 → R2 → R3 → R4 → R5 → R6 顺序集成，R7 使用 R6 已验证发行物。各阶段先运行最窄失败用例，再实施、验证、独立复核和阶段提交；R5 的完整支持声明最后启用。各阶段都不依赖另一份计划的公共字段或 UI 决策。

### R0：先补齐责任专题的契约

**预计文件：** `docs/architecture/output-cleanup.md`、`camera-capabilities.md`、`docs/camctl/file-runtime.md`、`database/operation-fields.md`、`cleanup-coordination.md` 及必要的会话工作判定说明。

- [x] **步骤 1：逐条核对状态矩阵与现行契约。** 固定上述五维状态、原申请持有、QUERY 取消核实边界、终态残留发现及主机预算归属。明确哪些规则是既有契约、哪些是本次补齐，不以实现现状替代设计依据。
- [x] **步骤 2：定义完整设备文件清理能力。** 内部 `DriverDeclaration.delete_supported` 表示驱动提供完整设备文件清理协作，需 DELETE、独立文件查询、匹配证据及适用恢复齐备；原始 rm 能力不等于此声明。文件查询 true／删除 false 合法，删除 true／文件查询 false 为定义错误。false 不阻止已保存结果本地交接或主机文件清理。公共字段按当前配置选定正式源派生，由能力声明专题定义；不能将内部值直接透传或拿公共值阻断历史文件的实际协作者。
- [x] **步骤 3：明确文件调用计数与恢复证据。** 一次 DELETE 最多一次物理删除和一次用于确认该调用完成的后置观察；该观察属于 DELETE，不是适配器内部重试。后续框架 `query_file` 各占 QUERY 额度，适配器不循环 rm 或查询。证据登记按类型与版本唯一、操作单值：DELETE 后置观察使用独立 `file_delete_presence/v1`，字段为 cleanup_item_id、present，保留既有 `file_absent/v1`；FILE_QUERY 沿用 `file_presence/v1`。主机重启恢复定义独立 `local_file_recovery/v1` 空正文依据，用于主机 DELETE／QUERY 的 UNKNOWN 与 assumed 收场，不虚构实际返回。
- [x] **步骤 4：独立复核并提交文档。** 消除主机预算冲突；责任专题维护有效规则，计划和其他页面引用它们。检查新增锚点与链接后，才交接依赖这些规则的实现。

### R1：分离文件查询端口与拍摄状态查询

**预计文件：** `apps/camctl/src/camctl/devices/ports.py`、`managed_port.py`、`drivers/registry.py`、ADB `contracts.py`／`driver.py`、`bootstrap/cleanup_assembly.py`、`outputs/cleanup_flow.py`。后续源码简称均相对该组件的 `src/camctl`。

**接口建议：** 在默认字段尾部增加 `DriverDeclaration.file_query_supported=False`；新增 `FileQueryDriver.query_file(ControlRequest) -> DeviceCallResult`、`port_for(entry, "file_query")` 与 `CameraCall.FILE_QUERY`。持久化仍用 `operation="query"`、`CHECK_FILE_EXISTS`、`exists/<item_id>`、`file_presence/v1`；原 `query_supported/query_state` 保持拍摄语义。

- [x] **步骤 1：写失败测试并运行。** `integration/devices/test_driver_contract.py`、`test_adb_camera_registration.py`、`integration/bootstrap/test_cleanup_flow.py` 覆盖能力分区、清理误调用 `query_state` 即失败、真实拍摄装配仍无状态查询。拒绝不完整的删除声明。
- [x] **步骤 2：实施独立路由。** 静态合同和运行登记同步检查完整性；受管端口按原 QUERY 票据、目标、cleanup item 和绑定路由 FILE_QUERY。清理无回退拍摄查询的分支；已保存结果消费不要求当前端口。
- [x] **步骤 3：迁移全部生产者与替身。** 核对 devices、bootstrap、outputs 清理测试及根 `tests/integration/camctl_fixtures.py`，只迁移文件查询；审计 `capture_assembly.py`、capture handlers／residual 与 `operations/queries.py` 未误获文件能力。保留原保存、取消及预算断言。
- [x] **步骤 4：验证并提交。** 跑窄用例和 devices 目录；bootstrap 的 `test_cleanup_query_default_recovery.py`、`test_output_binding_changes.py`、`test_closed_result_binding_recovery.py` 验证真实装配和恢复接缝。

### R2：持有原意图、实际结果与全部终态保存申请

**预计文件：** `outputs/cleanup_flow.py`、清理责任模型、`bootstrap/cleanup_assembly.py`；必要时调整既有仓储接口。建议新增 `apps/camctl/tests/integration/outputs/test_cleanup_delete_save_recovery.py`，扩展已有 QUERY 保存测试。

**接口建议：** `PendingCleanupIntent` 在 begin 前持有原申请和 key；`PendingCleanupDelete` 与已有 `PendingCleanupQuery` 对称，在返回后验证前接住原结果。共用成员保存边界覆盖 Finish／Cancel／Fail／绑定失败，伴随责任持有原请求；具体类名可合并，阶段与责任不能省略。

- [x] **步骤 1：写故障矩阵并确认失败。** DELETE／QUERY begin、返回后验证、finish、成员及伴随保存各注入确认回滚、实际提交但回执未知、暂时无法核实。断言原请求、key、时刻、票据和计数不变；无可靠票据时调用为零，实际返回后 DELETE 始终为一次。
- [x] **步骤 2：实施持有和派发检查。** 意图先持有再提交，回滚与未知沿原请求处理；原 ticket 只在本会话可靠未派发且当前资格成立时使用一次。取消后不迟发 DELETE，QUERY 采用完整矩阵；begin 保存故障不能变成业务取消错误。
- [x] **步骤 3：闭合全部结果路径。** 直接成功、未派发、已清理复用、取消、绑定失败、预算耗尽等均先持有唯一终态申请。ALREADY 回执读取实际可靠结果，伴随按实际终态收场。完整保存错误 code／stage／details、call_info、effect、settlement、观察与原时刻；兼容结果也不能改写为泛化错误。DELETE 可靠后置观察按 R0 的类型消费，明确在场可以用于后续资格判断，无需为取得同一事实追加 QUERY。对原绑定失败等申请，验证确认未生效且后来取消使原分支失效时的可靠处置：原调用已结束且原 key 可靠不存在后，结束旧申请继续保存的可能，再形成当前取消阶段的新申请；提交未知或无法核实期间保持原申请。实际已提交的原终态保持，普通保存错误不触发替换；原返回、票据、次数与归属继续保持。
- [x] **步骤 4：验证交接并提交。** 同会话新 factory、取消等待及当前驱动移除／变更时，仍先完成本地责任。覆盖错 database／ticket／consumer 拒绝接手、验证错误保留原返回。运行 `test_cleanup_query_{save,commit}_recovery.py`、`test_cleanup_query_cancel_before_delete.py`、`test_cleanup_retry_phases.py` 和 unit/outputs 的保存责任用例。

### R3：补齐持久化发现、原 DELETE 消费和启动恢复

**预计文件：** `outputs/cleanup_flow.py`、`cleanup_results.py`、`bootstrap/cleanup_assembly.py`、`lifecycle.py`、`session/work.py`／`supervision.py`；必要的通用恢复类型与仓储查询。建议新增 `integration/outputs/test_cleanup_residual_recovery.py`。

**接口建议：** 新增与 `saved_cleanup_query` 对等的 DELETE 消费入口，或共用 `SavedCleanupCall`；从原完整事务恢复 ticket、key、结果事件、状态、效果、错误、证据类型版本及返回时刻。清理装配接收启动固定历史上界、本地收场边界和原操作恢复登记；复用现有通用恢复判定，必要时将通用类型移到 operations，避免 outputs 依赖 capture 内部状态机。

- [x] **步骤 1：写真实仓储失败测试。** 清空所有内存后重建生产 factory，覆盖仅有旧 DELETE／QUERY 意图、结果已存但成员未存、成员／动作终态且伴随未结束。组合当前绑定变更、驱动移除／false、预算降低、取消及启动历史上界，断言原事实保留、无重复副作用。按当前所需操作覆盖 DELETE／FILE_QUERY 的三种合法能力组合，以及原可靠未派票据与无票据的[能力缺失处理](../../architecture/output-cleanup.md#再次清理已完成的产物)；非法声明仍由登记拒绝。
- [x] **步骤 2：实施有界独立发现。** 不依赖动作或成员非终态，分别发现 RUNNING 尝试、已保存未交接结果、终态成员开放伴随及本会话待存责任。分批读取、去重，先本地保存和消费，再解析当前设备端口。纳入 `needs_run`、取消等待与会话退出事实，不能只以 pending 集合为空判断完成。
- [x] **步骤 3：消费原结果并诊断矛盾。** 已保存 DELETE 的可靠缺席无需当前驱动及额外 QUERY 即可完成合法交接，真实调用错误同时保留。既有成员及动作终态保持；终态成员仍有未收场物理尝试属于记录矛盾，诊断并阻止依赖推进，不能悄悄重开或释放归属。仅伴随元数据未完成时沿实际终态本地闭合。
- [x] **步骤 4：实施旧尝试恢复。** 组合旧本地已收场、意图不晚于启动上界、原操作支持恢复三项资格，沿原 ticket 保存 UNKNOWN。DELETE 经 `finish_cleanup_attempt` 同时保存不可撤销事实；旧 QUERY 结束后必要核实用新尝试与剩余额度。资格不足且没有本会话拥有者可接回实际结果时，保留原记录与诊断，按[清理恢复错误规则](../../camctl/database/cleanup-coordination.md#旧-running-尝试的恢复资格)进入既有状态库错误退出；验证不会空转、正常成功或伪造取消完成。主机恢复使用 R0 的独立依据。
- [x] **步骤 5：验证并提交。** 覆盖可靠缺席和调用错误并存、两种预算均低于已用次数仍可本地交接；更晚 DELETE 使旧在场观察失效。按[有限取消核实](../../architecture/output-cleanup.md#文件查询与取消的有限核实)分别验证同会话消费身份跨 factory 保留、真正重启缺少消费依据时先消费原有效结果、失效旧查询不阻断必要核实，以及取消后授票在未派发、失败、恢复未知及多次取消下不重授机会。运行 `test_cleanup_query_{durable,local}_consumption.py`、`test_cleanup_recovery.py` 及新残留测试；按同 H 历史验证旧失败／取消请求与冻结报告不被后续成功改写。

#### 终态成员的原可靠缺席交接

本分区属于 R3 的原结果消费。历史流程可能先保存 DELETE 缺席，退出后又因当前绑定异常保存成员失败及流程结束，文件与产物却尚未保存清理完成。真实仓储序列已经证明该状态可形成；不能只关闭伴随流程后遗漏原文件责任。行为由[终态成员交接](../../camctl/database/cleanup-coordination.md#终态成员的可靠缺席交接)和[历史来源格式](../../camctl/database/history-formats.md#事件正文版本-1)定义。

**预计新增范围：** 清理结果保存仓储、事件守卫、清理汇总及独立发现；`event-transitions.json` 登记和生成资源同步；原结果读取、待存申请和 SQLite／历史集成测试。内部 API 名称与文件拆分由实际最窄责任边界确定，不新增 SQL 列、事件类型、操作类别或公共字段。

1. 使用公开仓储生成原 DELETE 缺席结果，令成员成功保存确认回滚，再以真实绑定失败事务和动作汇总形成全部终态；清空内存，从生产 factory 证明文件交接遗漏。保留原成员、动作、流程、尝试、错误、限制及计数快照。
2. 建立仅交接原已保存可靠缺席的内部申请。绑定原票据、结果事件及完整事务和唯一承载文件；沿 R2 规则固定新交接的完整申请、key 和形成时刻。保存未知后的迟到 raw 缺席不进入该入口；成员终态且物理尝试未结束仍诊断矛盾。
3. 在同一事务保存文件完成及产物完成和报告变化；`OUTPUT_REGISTERED.OBSERVATION` 携带已批准的 `cleanup_result_source`，主机文件保留既有 `cleanup_request`。保存命令严格限定完整事件组，产物观察守卫核对精确来源和先前文件事实，原键核实核对原完整事务、时刻及目标；共同拒绝不匹配、拆分或未持久依据。文件已缺席时只写产物实际变化，产物已完成而文件事实不符则报一致性错误；两者已完成时只读，不增加空事件或历史。
4. 将独立可靠清理完成纳入现有汇总最高优先级，避免后续成员事务将产物改回未完成；普通文件缺失不能替代清理完成依据。两个原结果消费者均先交接适用文件事实，再按原成员终态闭合伴随流程，不改写任何旧终态。
5. 有界发现与 `needs_run` 同时覆盖全部动作、成员和流程都终态、但文件事实尚未交接的责任；交接完成后不永久重复发现。验证设备与主机正式源、原错误与缺席并存、当前端口移除／false、预算变化、保存回滚／未知／取消重建及精确不级联。
6. 验证新交接前后同一历史边界的正向、逆向、快照及冻结报告保持；运行受影响仓储、守卫、history、outputs 与 bootstrap 窄回归，再纳入 R3 整体独立审查。软件结果不替代 R7。

### R4：将主机正式源接入既有文件任务执行器

**预计文件：** `bootstrap/cleanup_assembly.py`、`lifecycle.py`、`outputs/cleanup_flow.py`；复用 `host_files/tasks.py` 的 `FileTaskExecutor`、现有路径／身份检查与监督器。建议新增 `apps/camctl/tests/integration/bootstrap/test_cleanup_host_file_tasks.py`。

**接口建议：** 将 `deps.work_files.executor` 传给清理。内部本地请求携带保存的 intermediate_file_id、原票据及必要路径身份；按真实存储引用分流，不按 kind 或相机删除布尔值判断。本地端口提供完整实际 outcome。

- [x] **步骤 1：写可控线程任务失败测试。** stat／unlink 暂停期间其他动作与取消仍可推进；分别覆盖排队撤回、已开始后取消、unlink 成功但等待者取消。断言只有确认任务不会执行才保存未派发，已开始任务必须接回实际结果后再结束成员和释放归属。
- [x] **步骤 2：实施执行器接入。** 使用默认线程池及既有任务句柄，排队取消与已开始责任分开；不只用 `to_thread` 丢弃取消后的返回。保持路径边界与原文件身份校验，本地无应用层完成期限，尝试不填设备 timeout／间隔。
- [x] **步骤 3：验证预算和正式源。** 主机 original／preview／repaired 使用 R0 的正式清理预算，错误与可靠观察同时保存。设备 false 不影响主机项；Hasselblad 两种源模式、原图／预览的双向精确清理及修复成品均有用例。
- [x] **步骤 4：验证并提交。** 跑新 bootstrap 用例、integration/host_files、capture 的 `test_device_photo_flow.py`／`test_host_photo_storage.py` 及 outputs 精确清理用例，确认未改变内部临时文件与日志清理的专属生命周期。

### R5：在共用 ADB 适配层接入正式文件调用

**预计文件：** ADB `filesystem.py`、`driver.py`、`contracts.py`、`osmo360ii_capture.py`；建议新增共用 `file_commands.py`，不以型号命名。扩展 unit/devices 的 `test_adb_camera_driver.py`、`test_osmo360ii_capture.py` 及 integration/devices 的传输夹具与测试。

**接口：** 从原 `identity_key + locator` 恢复 `FileIdentity`；正式工厂生成精确单文件 DELETE／FILE_QUERY，解释器输出 `DeviceCallResult.from_outcome`，按 R0 的操作边界使用文件观察证据。两者均走 `_invoke → invoke`，登记适用 query／delete 的证据和前台恢复范围；R2／R3 同步消费及恢复 DELETE 后置观察与既有缺席证据。

- [x] **步骤 1：用正式合同写失败测试。** 直接用 `osmo360ii_contract()`，不在测试替换生产工厂。覆盖零字节在场、精确缺席、原绑定及身份非法、空格／引号／换行路径、目录／介质／权限／工具故障、超时、输出截断／重复／非法帧、观察后调用错误。
- [x] **步骤 2：实施有界文件协议。** 一次 DELETE 只 rm 原精确路径，并至多取得一次后置观察；FILE_QUERY 对原目录完成可靠的精确名称查询。完整成功且无目标才证明缺席，零大小仍在场；stat 失败或 rm 退出码不能推出缺席。严格校验唯一完整帧及对应目标，任一输出流截断不信任保留前缀，仍完成原调用收场。
- [x] **步骤 3：核对工具与恢复前提。** 复用现有目录／引用工具，明确正式合同依赖及受管前台范围，不能藏查询重试或关联删除。补齐 query／delete 的实际返回、调用收场与文件观察证据；另在合同证据登记中提供 `adb_foreground_recovery/v1` 空正文模板，并分别在原持久化 query／delete 的恢复声明中授权。现有带 `terminate_grace_s` 的各操作前台收场证据不能替代该模板。工具及响应前提按每个型号合同核对，共用实现不自动授予所有型号能力。
- [x] **步骤 4：从真实装配验收后启用。** 未知 DELETE 后分别核实缺席、在场和预算耗尽；OSV、LRF 双向不级联，设备文件与主机 MP4 双向独立。通用责任与工厂均通过后，OSMO 派生删除 true、文件查询 true，拍摄 `query_supported` 仍 false。其他型号按其完整合同与独立证据声明，不维护额外型号布尔表。
- [x] **步骤 5：复核并提交。** 从正式 `osmo360ii_contract()` 装配的 entry 调用 R3 复用的 `recovery_registry(entry, operation)`，断言 query／delete 取得非空且绑定正确的原登记；缺声明、缺模板或模板正文非法时仍阻塞恢复。devices、outputs、bootstrap 目标回归，审查三类事实、错误与观察保存及恢复范围；声明不能早于实际生产协作启用。

### R6：完成安装版 CLI 与 host 的独立软件验收

**预计文件：** 新增 `tests/integration/test_device_file_cleanup_roundtrip.py`，复用现有 wheel／CLI／host 与受限 ADB 文件替身；扩展 `test_osmo_builtin_timelapse_roundtrip.py` 的拍摄回归。媒体阶段交接涉及 `capture/media.py` 及 capture／bootstrap 的共享执行器用例，接口责任在 `docs/camctl/file-runtime.md` 维护。清理调用的独立推进涉及 `bootstrap/cleanup_assembly.py`、会话退出边界及相关 bootstrap／session 用例，遵守[动作执行协程的创建时机](../../camctl/implementation.md#动作执行协程的创建时机)。更新 `tests/integration/README.md`、`docs/camctl/verification.md`。

#### 软件联测前置：OSV 准备与修复任务交接

修复成品由正式媒体流程生成。准备任务只占用输入文件；完整修复同时占用输入和已登记输出，使用同一个共享执行器。准备阶段的实际结果及原登记申请先完成保存交接，之后由正常处理轮次重新检查取消、终态和执行资格，再沿原输出身份继续；不新增持久状态或改变文件互斥规则。内部返回阶段及函数拆分属于实施建议。

- [x] **前置 1：固定真实共享执行器反例。** 用同一执行器、真实仓储和工具端口验证准备阶段完成、原输出唯一登记及后轮完整修复的顺序；保留同文件真实冲突仍被拒绝的反例。
- [x] **前置 2：实施阶段交接并保留原保存责任。** OSV 准备可靠保存探测事实与输出登记后返回内部中间结果；后轮从已保存 RUNNING 装载原输出及探测事实，不重复工具或输出登记。回滚、提交未知和换连接继续原完整申请、key 与时刻。
- [x] **前置 3：覆盖中断、取消与恢复。** 分别验证准备未开始、已开始、已登记尚未修复、修复在途、结果待存及已有终态；等待者取消保留实际结果，业务取消阻止后续普通修复，中间文件沿原规则收场。
- [x] **前置 4：独立复核并集成。** 运行共享执行器、媒体、取消及恢复的相关回归，审查后交付新稳定基线；安装版联合链重新构建 wheel，验证真实双流修复成品及后续精确清理。

#### 软件联测前置：清理调用与中央调度

设备删除、文件查询或主机文件任务等待实际结果时，中央调度仍须发现到期取消并保存目标取消事实。清理拥有者独立推进同一轮次，持有原调用、连接、完整结果及待存申请，交付完成后才允许后轮。实际责任和保存责任共同参与会话收场及正常退出判断，具体实现位置按既有责任边界确定。

- [x] **调度 1：固定在途取消反例。** 使用真实会话、仓储和受控调用，验证设备 DELETE／FILE_QUERY 及主机 stat／unlink 等待期间，目标取消在调用结束前可靠生效；保留原唯一调用及其实际结果。
- [x] **调度 2：闭合独立推进与保存责任。** 复用既有后台任务和本地收场机制，确保重复调度不重入，完成及原异常准确交付，原 pending 集合、key 与时刻跨轮保留。默认装配、自定义流程和直接会话上下文使用相同责任契约。
- [x] **调度 3：验证停止、错误和退出。** 覆盖任务未开始、调用在途、实际返回待存、提交未知、成员终态及伴随未收场。业务取消、等待者取消和会话停止分别判断；会话认定致命错误后先阻止新增普通工作，再等待原调用、连接及保存责任收场。
- [x] **调度 4：独立审查并复验安装链。** 完成相关组件门禁，审查集成后交付稳定基线。重建 wheel，在真实 host／CLI 中先确认目标取消已保存且旧 DELETE 仍在途，再结束旧组并由新进程恢复 UNKNOWN、有限查询及原终态，随后完成全部联合验收。

#### 联合验收步骤

- [x] **步骤 1：写联合失败测试并运行。** 仓库外安装 wheel，经内部正式登记验证能力与工厂，通过根协议校验的独立清理请求进入真实 host／CLI／SQLite／文件／报告及 ACK。替身只提供传输和文件系统，不替换生产文件工厂、解释器或清理状态机；不要求客户端 UI 新策略已交付。
- [x] **步骤 2：覆盖组合恢复。** 精确 OSV／LRF／主机成品不级联、设备 false 与主机混合、已清理重请求无调用；DELETE 后中断、保存回执未知、已保存结果未消费、终态伴随残留及取消交错分别重建进程。核对原 key、累计次数、调用次数、成员／伴随、限制、产物汇总与冻结历史，而非只检查文件不存在。
- [x] **步骤 3：运行整合门禁。** 按下列命令及各任务窄目标执行；如移动通用恢复类型，再核对其受影响的拍摄／取回消费者。另一份计划已集成时增加声明一致性检查，不能使其成为本计划通过前提。
- [x] **步骤 4：独立端到端复核并提交。** 沿精确身份、意图、调用、原结果保存、取消／恢复、成员及报告闭合责任。记录命令、环境、日志、退出码及未验范围；软件、ARM 与真机分开，不以此勾选 R7。

### R7：用已验发行物完成 OSMO 目标设备验收

**责任与前提：** 协调者操作设备，先完成 R6。复用[现有验收事实](../../camctl/verification.md)，核对连接、状态库及已有产物。现场选择已有可靠副本的正式测试产物，协调者在物理删除前逐文件核验并保存具体清单，依据验收授权执行和记录选择理由。

- [x] **步骤 1：准备具体操作清单。** 列出可处置测试 output_id、精确远端路径、大小、保留副本、关联 OSV／LRF／MP4 与不得触碰的文件。优先用已有可处置产物，新增拍摄另行明确目的；物理删除前核对并保存具体清单，记录验收选择及依据。
- [x] **步骤 2：执行正式独立请求。** 使用已验 CLI／host 及精确 ID，归档前后目录、意图、调用、必要查询、逐项结果和报告；确认只删除清单中的文件，关联文件与副本保留。客户端若已接入可参与，但不成为设备执行验收前置。
- [x] **步骤 3：核对并归档。** 确认 ARM 实际工具支持协议与可靠缺席观察，重复请求不重复删除。物理效果未知时停止并保留证据，不自动重试、取回或删除；故障恢复主要由软件注入证明。分别记录实测结果与未验边界。

## 实施阶段的验证命令

从任务工作树根运行，先核对 Python 3.11 及目标环境。目标失败／通过循环使用各任务最窄文件；下列整合门禁各目录分进程、同一工作树串行执行，避免生成资源冲突。

```bash
node scripts/check-protocol.mjs
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/devices -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/outputs -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/host_files -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/bootstrap -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/operations -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/session -q
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_file_cleanup_roundtrip.py tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
node scripts/check-doc-links.mjs
git diff --check
```

环境缺失时按[组件测试指南](../../../apps/camctl/tests/AGENTS.md)准备；显式使用本工作树源码与资源。根联合用例自行构建并安装 wheel；不存在的新测试先按任务创建。首次整合跑完整相关范围，之后只对修改、失败及新风险扩展复测。实施遵守[工作区隔离与阶段提交](../../../.agents/rules/implementation.md#git-协作与工作区隔离)。

## 完成判据

- [x] 文件查询与拍摄状态查询独立，所有实际清理消费者迁移，完整删除声明与生产协作一致。
- [x] 原意图、实际结果及全部终态申请都有唯一保存责任；取消、回执未知和 runtime 重建不重复副作用。
- [x] 旧 RUNNING、原 DELETE／QUERY 结果和终态残留可有界发现，恢复资格和会话退出不漏项；旧终态及冻结历史保持。
- [x] 主机正式源走既有文件任务执行器，队列撤回与已开始收场分别验证，无设备调用期限及型号条件误用。
- [x] 共用 ADB 文件协议提供可靠在场／缺席／未知，精确清理不级联；OSMO 支持值在完整协作通过后启用。
- [x] R6 软件及发行链有独立证据；R7 目标设备另行记录，未完成时不宣称真实设备删除已验收。

## 实施决策与归档

本节记录实施时选择的责任边界、取舍及验收方式。行为契约仍以责任专题为准；具体命令、结果和未验范围由[软件与 TX2 验证记录](../../camctl/verification.md#设备文件清理的-tx2-实机验收2026-10-18-utc)保存。

| 决策 | 理由及影响 | 验证依据 |
| --- | --- | --- |
| 通用登记验证文件端口及标准观察，具体驱动负责自身返回和恢复证据。 | 保持完整清理能力与具体传输实现的责任边界，新增驱动无需复制厂商证据名称。 | R1／R5 验证合法能力组合、定义错误、正式工厂与恢复消费者。 |
| 沿原申请、票据、key 和时刻接回结果；原尝试配置从可靠历史读取。 | 保存未知不产生新副作用许可，配置变化不重写旧尝试；数值 0 与缺值分别处理。 | R2／R3 原保存矩阵、R5 原配置与新配置分区、R6 安装版恢复。 |
| OSV 准备可靠登记原输出后返回内部 PREPARED，后轮再取得完整文件资格。 | 复用共享执行器互斥规则，只增加一次正常调度交接；持久状态及输出身份保持。失败的媒体探测沿既有具体错误保存，不扩大永久字段契约。 | 真实共享执行器矩阵、会话自动推进及安装版双主视频修复。 |
| CleanupFlow 组合既有后台任务与完整保存拥有者，致命错误先停止新增工作再等待收场。 | 中央继续处理到期取消，原 task、连接、结果及保存责任持续有人接手；复合停止、观察者取消与父任务取消分别保留。 | 调度反例、完整拥有者／会话矩阵、整体评审及安装版在途取消窗口。 |
| 正式主机源复用 FileTaskExecutor，设备调用复用共用单文件 ADB 工厂。 | 队列撤回与已开始结果分开，主机不套设备期限；具体型号按完整合同启用。 | 主机原图／预览／修复成品和 OSV／LRF 的独立清理及混合能力验收。 |
| 组件结果按实际输入与消费者沿用，安装链在最终测试字节上完整重建。 | 分版本结果可追踪；既有 35 项 outputs 失败和两条测试标记告警保持披露，不将局部门禁相加为一次全目录通过。 | 独立整体审查及各阶段命令、日志、SHA 和退出码。 |
| TX2 选择有独立可靠副本的 output 9 OSV 与另一组 output 20 LRF。 | 以既有小型测试产物验证双向不级联，保留各自配对源及全部交付副本，无需新增拍摄。验收删除和查询预算各为 1，原配置保持。 | 逐文件清单、设备及副本摘要、目录精确差集与正式请求。 |
| 现场辅助启动器的编译失败保留为原失败请求，新请求继续消费原文件事实。 | 先证实实际 ADB 未执行，再编译检查并重新核对文件；旧错误、未知效果、次数及终态保持。后续所需 QUERY 不被误算成重复删除。 | 原 stderr、新请求在场／缺席结果及数据库逐字段比较。 |
| 报告按实际覆盖与同步责任领取，ACK 通过原请求重新下载后重送。 | ready 中的新完整报告可替换旧文件，旧逻辑记录和已领取字节保持；ACK 不创建新业务动作。 | 报告 164 的完整字节、客户端导入、ACK 164／541 及计数不变。 |
| 后续开发串行收敛到主工作区，五个任务工作树在完整归档后移除。 | 分支历史、原未提交改动和各版本证据可恢复，主工作区承担后续修改。 | `.local/verification/worktree-archives/manifest.json` 保存旧路径及归档摘要。 |

当前交付保留两项非阻断测试标记整理：`test_read_session.py` 与 `test_process.py` 的同步测试受模块 asyncio 标记影响。后续可仅收窄各文件标记范围并保留原断言；本计划不修改全局告警配置。真实物理断电与 Action6 删除启用不在本次设备验收结论内。
