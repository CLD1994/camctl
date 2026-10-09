# 显式同步取消与本地完成执行计划

> 供执行 Agent 使用：按 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 逐项执行。复选框只记录实际完成；先建立能证伪行为契约的测试，再修改生产实现。

**目标：** 显式同步动作的取消、客户端确认和本地成功分别按已提交事实推进，所有执行与恢复入口保留相同的同步责任、动作终态和报告依据。

**实现建议：** 在现有取消生效事务中保存适用的同步结束，在报告动作的本地收场事务中保存动作终态及计划聚合。报告维护与会话责任查询共同消费尚未保存的本地结果；继续复用现有事务内核、事件登记、报告发布及累计 ACK，不增加状态、依赖或设备操作。

**技术基础：** Python 3.11、SQLite、现有不可变历史与当前投影；真实数据库、文件和进程组合属于组件集成测试。内部接口和文件划分是实施建议，正式契约与项目规则是硬性要求。

**规格：** [同步开始、本地成功、确认与取消](../../architecture/status-sync.md)、[同步持久化职责](../../camctl/database/reports-runtime.md#状态同步记录)、[动作状态与取消标记](../../camctl/database/plans-actions.md#计划与动作的运行状态)、[取消动作](../../architecture/task-cancellation.md)、[报告继续与停止](../../architecture/report-maintenance.md#报告生成的继续与停止)。

[路线图审查入口](2026-09-30-camctl-implementation-roadmap.md#分模块审查进度) · [报告模块计划](2026-09-30-camctl-reporting.md) · [取消模块计划](2026-09-30-camctl-cancellation.md)

## 范围与代码起点

`report_status` 动作通过 `bootstrap.flows._start_due_report_actions` 调用 `reporting.policy.start_sync` 开始。开始事务同时保存动作进入 `RUNNING`、执行标记及唯一的 `state_syncs` 记录。生成和发布独立推进；`record_local_report` 在同一事务中填写 `local_report_id`、保存动作 `SUCCEEDED`，必要时完成计划。客户端 ACK 可以先于本地结果保存结束同步责任，不能替代这项本地结果保存。

取消通过 `bootstrap.flows.cancel_flow`、`cancellation.service.apply_cancel`、`CancellationRepository.apply_cancel_target` 和 `TargetSettlement.settle` 推进。当前报告动作分支直接返回收场完成；取消生效事务只保存目标取消标记，尚需接入同步结束与动作取消终态。`reporting.policy.cancel_sync` 提供单独的同步结束接口，但真实取消链没有调用它；不能将新增一次独立调用视为满足同事务要求。

`bootstrap.flows._settle_covered_local_syncs`、`_covered_local_actions` 及 `bootstrap.application.query_work_facts` 的本地完成查询只选择 `OUTSTANDING` 同步。`record_local_report` 和 `SYNC_CHANGED.LOCAL` 已允许 ACK 结束后的本地完成，因此这些消费者也需要覆盖 `ACKNOWLEDGED`、尚未保存本地结果且动作仍正常运行的分区。`_settle_covered_local_syncs` 必须检查每次仓储结果，不能把回滚或提交未知解释为完成。

本计划限定在取消仓储、报告同步用例、报告动作收场端口、`bootstrap.flows` 和会话工作事实查询。初始化、历史读取仓储、报告字节生成及报告进程停止状态机分别由其他任务维护。

## 全局约束

- 行为以写事务实际读取和提交的顺序决定。事务外读取的动作、ACK 或报告状态不能授予永久许可。
- 同一动作只保留一个同步身份、固定起点和开始边界；终态、已结束责任、原本地报告身份及已登记报告的冻结依据不改写。
- 取消生效、适用的同步结束、取消明细进度及对应历史必须共同提交；报告动作收场的终态与受影响计划聚合必须共同提交。
- `RUNNING` 取消先保存 `cancel_requested=1`，保留执行标记和运行状态；本地有限收场完成后保存 `CANCELED`。`SUCCEEDED` 等已有终态及两个标记保持原事实。
- 同步管理事件不增加业务水位；动作及计划的公开变化按现有登记产生报告责任。
- 同步取消不取消共享报告进程，不删除 `processing` 文件，不改写旧报告，也不将主程序领取当作 ACK。
- 查询、保存或提交结果无法可靠解释时，按状态库错误停止依赖它的执行；不能默认不存在、完成、取消或成功。
- 按 [测试运行指南](../../../apps/camctl/tests/AGENTS.md)使用 Python 3.11；所有 pytest 前台独占、顺序执行，组件集成目录分别启动。

## 状态分类与结果

判定依次使用动作状态与执行标记、已提交取消标记、同步结束状态、`local_report_id`，以及报告的可靠发布事实。下表中的“已发布”仅表示存在一份满足固定起点及开始历史的报告；文件移动而未保存可靠事实时，先按报告发布恢复规则核实，不能直接视为动作成功。

| 动作和同步的可靠事实 | 本地报告与 ACK | 取消和后续本地处理 |
| --- | --- | --- |
| 动作 `PENDING`，执行标记为 0，没有同步记录 | 不适用 | 取消生效事务直接保存动作 `CANCELED`、取消标记及成员结果；不建立同步；适用时共同完成计划。 |
| 动作 `RUNNING`、执行标记为 1、未取消；同步 `OUTSTANDING`、本地结果为空 | 尚未发布，或已发布但动作成功尚未提交；尚未获得合格 ACK | 取消生效事务保存取消标记并将本动作同步改为 `CANCELED`，结束依据指向本事务的 `SYNC_CHANGED.CANCEL` 事件；动作暂留 `RUNNING`，随后完成本地取消收场。 |
| 同上，同步已为 `ACKNOWLEDGED`、本地结果仍为空 | 合格 ACK 已先提交；本地动作成功尚未提交 | 取消保存动作标记并继续本地取消收场；保持 ACK 的原结束原因、报告身份及事件，不追加同步取消，不把动作改为成功。 |
| 动作 `RUNNING`、取消标记为 1，同步已为 `CANCELED` 或 `ACKNOWLEDGED` | 共享生成可能未结束，也可能已经发布 | 本地收场不等待共享生成或远端确认；保存动作 `CANCELED`，执行标记保持 1，必要时同事务完成计划，然后成员结果表达 `canceled`。 |
| 动作 `SUCCEEDED`，已有固定 `local_report_id` | 同步仍 `OUTSTANDING`，没有合格 ACK | 取消保持动作成功及标记；同步继续等待合格 ACK，不能终止责任。 |
| 动作 `SUCCEEDED`，同步已 `ACKNOWLEDGED` | 本地成功和 ACK 均已提交，顺序任意 | 取消保持动作、原本地报告及 ACK 结束事实。 |
| 动作已有其他合法终态 | 同步无记录，或其状态符合原终态契约 | 保持终态及原事实；不重新建立同步、不强行改为取消。 |
| 动作 `RUNNING`、未取消，本地结果为空，同步为 `OUTSTANDING` 或 `ACKNOWLEDGED` | 有满足固定起点和开始历史、可靠保存发布事实的报告 | 本地成功入口保存原报告身份及动作成功，适用时共同完成计划；ACK 已结束时保留原结束事实。 |
| 动作已取消生效或已 `CANCELED` | 迟到普通生成成功、发布恢复或本地成功申请 | 保留真实报告文件及发布事实；不填入该动作的成功结果，不恢复同步、不改变动作终态。 |
| 动作与同步的必要事实缺失、矛盾或不可读 | 任意 | 状态库错误；不得把缺失的同步记录当作运行中动作已完成，也不得把未终态目标记为 `already_terminal`。 |

同一运行中动作最多有一项同步责任。`RUNNING` 且执行标记为 1 的报告动作必须能可靠读取其原同步记录；合法执行失败、受理失败和执行前取消另按其自身分区判断。已结束记录永久保存，`local_report_id` 为空并不使 ACK 结束无效。

### 原操作键与输入身份

原操作键只核实原 `item_id`、`mode` 和 `occurred_at`。后续动作终态、成员结果或 ACK 可以已经提交，但不能据此把另一种模式当成原输入。适用的同步结束与计划完成也必须属于原事务和原对象。

| 原申请模式 | 核实完整事务时必须保持的依据 |
| --- | --- |
| `PRE_START` | 原目标可靠未开始；适用的动作取消终态、成员结果、启动责任及计划完成共同保存。非拍摄目标的原动作变化包含待执行到取消终态及取消标记。 |
| `WITH_STOP` | 原目标取消标记从 0 变为 1；适用的普通启动责任及报告同步共同结束；后续本地收场可以已保存，但不得追加第二次生效。 |
| `ALREADY` | 原事务复用已生效的取消标记，成员效果为 `APPLIED`；不重新建立标记或结束已经结束的责任。 |
| `TERMINAL` | 原目标已有合法终态，成员效果为 `NOT_REQUIRED`；原终态和标记保持，适用交付撤回另按其原责任核实。 |

拍摄目标的部分原状态不能仅凭行变化区分 `PRE_START` 与 `WITH_STOP`。非电机目标用正式登记的 `cancel_request` 保存并核对完整原输入；电机目标沿用 `motor_request`。

### 提交竞争

| 先提交的完整事务 | 后提交的事务 | 必须保持的结果 |
| --- | --- | --- |
| 本地成功 | 取消 | 动作保持 `SUCCEEDED`；未获 ACK 的同步仍 `OUTSTANDING`。 |
| 取消生效并结束尚未结束同步 | 本地成功申请或报告发布 | 动作按本地取消收场进入 `CANCELED`；共享报告实际事实保留，本地成功事务不得提交。 |
| 合格 ACK | 取消生效 | 同步保持 `ACKNOWLEDGED`；若动作尚未终态，取消仍生效并完成动作取消收场。 |
| 取消结束同步 | 合格 ACK | 同步保持 `CANCELED`；累计 ACK 仍按原独立规则处理。 |
| 合格 ACK，动作尚未保存本地结果 | 重启后的正常报告维护 | 恢复本地结果责任，使用满足要求且可靠发布的报告保存动作成功；不重新开始同步。 |

### 原子失败与恢复

| 执行阶段与结果 | 恢复依据和动作 |
| --- | --- |
| 取消生效事务确认回滚 | 取消标记、同步结束和成员进度均未生效；后续从原可靠状态重判，不能保存成员成功。 |
| 取消生效事务提交结果未知 | 按原操作键核实完整事务及对应目标、同步和成员；本会话停止依赖未确认结果的后续操作，不用新键重新施加取消。 |
| 取消生效已完整提交，动作取消收场尚未提交 | 从持久化取消标记发现本地收场责任；沿原同步结束事实完成动作终态，不需要原取消发起者继续等待。 |
| 动作取消收场事务确认回滚 | 动作和计划保持原状态，成员保持待收场；后续继续原本地收场。 |
| 动作取消收场已提交，成员结果尚未提交 | 从目标 `CANCELED` 保存原成员结果；不再结束同步或追加动作终态。 |
| 报告发布已可靠保存，本地成功事务回滚或结果未知 | 保留发布事实和本地结果责任；结果未知先核实原完整事务，后续按原动作、取消和同步事实选择成功或取消。 |
| 历史回放与固定旧 `H` 报告重建 | 只恢复动作、同步、成员及计划事实，不执行取消、发布或设备操作；旧报告身份、字节和摘要保持。 |

若执行者发现规格未覆盖的合法组合，或需要扩大第一版故障范围，停止相关任务并报告具体事实。不得自行用新结束原因、额外重试循环或空值归一化填补语义。

## 审阅重点

| 条件 | 测试归属与预期 |
| --- | --- |
| 报告已发布，但动作成功尚未提交时取消 | SC1/SC2：按动作仍 `RUNNING` 取消，保留报告发布事实，不将目标误判为成功或已终态。 |
| ACK 先结束同步，本地动作仍运行 | SC2/SC3：正常维护仍完成本地结果；取消仍能终态化动作，ACK 结束原因保持。 |
| 取消生效提交后，原取消动作自身结束等待 | SC2：从目标自身取消标记继续本地收场，不能遗留无人推进的 `RUNNING` 动作。 |
| 多项同步共享一份冻结报告，只有一项目标被取消 | SC1/SC3：只结束目标责任；共享生成继续，其他同步仍须满足固定范围与开始历史。 |
| 取消生效、本地终态或本地成功提交失败 | SC1—SC3：不留部分事实，提交未知不降为成功，重启沿完整已提交事实恢复。 |

## SC1：在取消生效事务中闭合同步责任

**建议文件：** `persistence/repositories/cancellation.py` 的 `_ApplyCancelTargetCommand.plan/_reuse` 与相关取消守卫，`reporting/policy.py` 的同步结束规则；测试新建 `tests/integration/cancellation/test_report_sync_lifecycle.py`。路径均相对于 `apps/camctl`。

**接口：** 保持 `CancellationRepository.apply_cancel_target(command: ApplyCancelTarget, key: OperationKey, owned: OwnedConnection)`。命令内部读取原同步记录并按上表分区，在同一个 `CommandPlan` 中保存必要的 `SYNC_CHANGED.CANCEL`。`cancel_sync` 的独立接口可以继续用于既有责任，但生产取消链必须使用同事务组合，不能嵌套提交。

- [ ] 先写 `test_apply_cancel_ends_running_sync_in_same_transaction`：通过真实受理和 `start_sync` 建立报告动作，固定真实取消成员，执行取消生效。断言目标 `(status, execution_started, cancel_requested) == (2, 1, 1)`，原同步状态为 `CANCELED`；标记、成员进度和同步结束事件属于同一完整事务，原同步定义及其他同步不变。
- [ ] 分别覆盖报告未发布、已发布但动作仍运行、ACK 已先结束，以及动作已成功四个分区；独立断言结束原因、`ack_report_id`、`local_report_id` 和动作标记。已有报告经真实冻结/发布入口准备，不用虚构的水位或不完整历史作为查询依据。
- [ ] 由主 Agent 顺序运行新文件，确认目标反例因同步责任未结束而失败；依赖、守卫注册或场景准备错误不算有效失败。
- [ ] 在现有生效事务统一读取、判断、登记拥有者、分配事件与提交；同步事件的 `ended_event_id` 必须指向它自身。原键重送读取完整原事务，核对目标、输入、同步身份和结束事实，不重新分配身份或追加结束。
- [ ] 用真实事务失败注入覆盖同步写入前后回滚与提交未知；验证标记、成员、同步与历史共同回滚，未知结果不推进成员成功，恢复核实原键。
- [ ] 审计 PRE_START、WITH_STOP、ALREADY、TERMINAL 四个入口及其他目标类型，保持它们现有责任边界。先运行新文件，再运行 cancellation 目录门禁。

**阶段门禁：** 尚未结束的运行中同步与取消标记共同提交；已 ACK 或已成功分区保持原事实；事件登记、原键核实、回放与其他目标类型均能解释实际事务。

## SC2：完成报告动作本地取消收场

**建议文件：** `reporting/policy.py`、`cancellation/settlement.py`、`bootstrap/flows.py`；SC1 的真实取消测试文件及 `tests/integration/bootstrap/test_report_flow.py`。

**接口：** `TargetSettlement.settle(target_action_id: int) -> SettlementOutcome` 消费真实取消事实。建议新增窄用例 `finish_canceled_sync_action(key: OperationKey, owned: OwnedConnection, *, action_id: int, occurred_at: int) -> DbOutcome[SyncSaved]`，保持现有结果类型或使用等价有约束类型；用例只保存目标本地收场及适用计划完成，不触碰共享生成和报告文件。调用方必须检查 `DbOutcomeKind`。

- [ ] 先写 `test_running_sync_cancel_settles_target_before_member_success`，组合 `apply_cancel` 与真实 `TargetSettlement`。断言目标最终 `(6, 1, 1)`，取消成员为成功且 `outcome=canceled`，同步保留 SC1 的结束事实；不能只断言收场端口返回 `complete=True`。
- [ ] 写 ACK 已先结束后的取消反例、最后一个动作取消时计划共同完成、目标已有终态保持，以及取消生效提交后中断再重建收场端口的反例。
- [ ] 建立原取消发起者自身结束等待后的恢复反例：只留下目标已取消生效的持久化事实，由目标本地责任发现入口完成动作收场；取消成员和发起者已保存结果保持原事实。恢复不能依赖原发起者仍 `RUNNING`。
- [ ] 由主 Agent运行上述反例确认红灯，再实施本地收场事务和报告目标分派；取消结果保存前核实目标已经取得契约规定的终态，禁止把 `RUNNING` 记为 `already_terminal`。
- [ ] 将已生效且尚未终态的报告动作收场接入正常及受限运行的责任发现；先处理本地取消，再考虑普通本地成功。重入沿原同步、动作和计划事实，只追加尚缺的事实。
- [ ] 覆盖动作/计划终态写入失败、原键重送、回放及成员结果保存前中断；运行新 cancellation 测试文件和 bootstrap 报告窄门禁。

**阶段门禁：** 取消成员成功时目标已可靠终态；报告动作本地收场不等待共享生成或 ACK；已保存标记的收场责任在原发起者退出后仍可发现和完成。

## SC3：闭合 ACK 先结束后的本地成功消费者

**建议文件：** `bootstrap/flows.py` 的 `_settle_covered_local_syncs`、`_covered_local_actions`、报告维护流程，`bootstrap/application.py` 的 `query_work_facts`；必要时完善 `reporting/policy.py` 的本地结果状态检查。测试使用 `tests/integration/bootstrap/test_report_flow.py`，并保留 reporting 生命周期用例作为仓储门禁。

**接口：** 沿用 `record_local_report(key, owned, *, action_id, local_report_id, occurred_at)` 与 `report_flow`。责任发现必须根据动作仍正常运行、本地结果尚未保存和可靠合格发布报告判断；同步为 `OUTSTANDING` 或 `ACKNOWLEDGED` 均可承担尚未结束的本地工作。

- [ ] 先写 `test_acknowledged_sync_recovers_local_action_success`：真实开始同步、冻结和发布报告，真实 ACK 先结束同步但尚未保存本地结果；重新建立生产 `report_flow`。断言原动作 `SUCCEEDED`、原报告 ID 成为 `local_report_id`、ACK 的结束字段保持，最后一个动作适用时计划完成，不建立第二项同步。
- [ ] 写 `test_work_facts_keeps_acknowledged_local_result_pending`，断言上述中间状态在真实会话查询中仍有本地待处理责任；正常维护成功后只剩合法远端或保留责任，不为等待 ACK 延长运行。
- [ ] 写取消已经生效后的本地成功拒绝反例及 `_settle_covered_local_syncs` 保存回滚/未知反例；断言生产维护抛出状态库错误，动作和计划不被声明完成。
- [ ] 由主 Agent 顺序运行反例确认红灯，再修改全部实际消费者的过滤与结果处理。查询不选择 `CANCELED` 同步、不选择目标已有终态或取消标记已生效的动作；发布流程可以保存共享报告实际事实，不能恢复被取消目标成功。
- [ ] 组合正常会话、受限会话、ACK 与取消两种提交顺序和报告生成期间新变化；报告固定 `H`、字节和其他同步要求保持。运行 reporting、cancellation、bootstrap 各目录门禁，彼此分开启动。
- [ ] 独立审阅新事务、全部责任查询及实际报告内容，按本计划矩阵逐行对照证据；更新路线图对应入口与模块计划。只有具备真实消费者证据的条目才勾选完成。

**阶段门禁：** ACK 结束不丢失本地动作结果责任，取消不被迟到成功覆盖，任何本地结果保存失败都保留可诊断事实并进入状态库错误处理。

## 验收命令与证据

在仓库根目录执行。先确认解释器为 Python 3.11，再依次运行，每条结束后才启动下一条：

```bash
apps/camctl/.venv311/bin/python --version
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/cancellation/test_report_sync_lifecycle.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_report_flow.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/reporting -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/cancellation -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap -q
git diff --check
```

证据记录须注明日期、环境、实际执行命令、目标反例的失败原因、修复后结果及未核验范围。软件测试不连接实际设备，不形成 TX2 性能或物理断电结论。

2026-10-09，开发容器 Python 3.11.16、`.venv311`：主 Agent 按上述新 cancellation 文件命令独占执行，初始 6 项反例全部失败，原因分别为同步责任未结束、取消目标仍运行、ACK 后本地动作结果及会话责任遗漏。补充原键模式反例得到 `1 failed, 15 passed`，变化的 `mode` 被错误复用；补充两类查询在正常／受限报告入口的反例得到 4 项失败，SQLite 错误没有转换为 `StateDbFailure`。生产路径实施后，扩展文件的业务字段与各对象正式声明的派生元数据分别核对，得到 `31 passed`。随后补充完整输入身份门禁六项均失败：拍摄 `PRE_START`／`WITH_STOP` 的原键歧义，以及守卫对缺失、其他成员、其他模式、其他时间和额外字段的证据未拒绝。完整证据写入、守卫及原键核实完成后，该文件 `37 passed`。

扩展门禁覆盖同事务取消、报告已发布／未发布、ACK 两种顺序、原操作键四种模式、明确回滚与提交未知、本地收场和成员保存之间的恢复、原发起者取消后的责任发现、固定 `H` 的共享报告、初始回放与当前投影逆向恢复，以及真实会话驱动在状态库失败后停止后续流程。reporting 单目录独立回归 352 项通过；完整 cancellation、bootstrap 目录门禁仍待完成。

## 输入身份记录与验证边界

### 取消输入身份记录

取消操作使用原操作键核实时，必须确认 `ApplyCancelTarget` 的 `item_id`、`mode` 与 `occurred_at` 均为原输入。部分尚未可靠启动的拍摄动作在 `PRE_START` 与 `WITH_STOP` 两种输入下产生相同的行变化，不能通过当前投影或事件组合反推原输入。

非电机目标的 `CANCEL_CHANGED.APPLY` 登记 `evidence.cancel_request`，内容为 `{"item_id": 正整数, "mode": CancelApplyMode 的协议值, "occurred_at": UTC 微秒整数}`；守卫核对完整结构、合法值及本事件实际目标与时间，原键重送逐字段精确核对。电机目标沿用已经登记的 `motor_request`。该记录只用于内部事务身份核实，不改变取消状态转换或对外报告。

`history-formats.md` 定义证据职责，分支专属成员以 `event-transitions.json` 的登记为权威。现有公共成员与电机证据保留原契约。守卫与拍摄歧义分区的六项失败反例已确认；完整非电机输入证据写入、原键逐字段核实和事件守卫已实施，窄门禁得到 `37 passed`。

报告生成的全量集合和子集合跨页资源边界、控制消息、迟到状态库错误及实际工作进程停止仍由报告生成链审查负责。本计划不以报告仓储局部测试替代这条链的组合证据，也不替代跨组件客户端导入与后续 ACK 验收。
