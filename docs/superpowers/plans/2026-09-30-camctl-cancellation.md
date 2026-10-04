# camctl 目标取消与独立收场模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 完整解析并固定取消目标，可靠保存生效事实和逐项结果，使目标收场独立于取消发起者。

**组织建议：** 寻址、资格及汇总是纯规则，完整目标与取消效果由窄仓储保存；设备停止、读取停止、交付撤回及清理由目标模块拥有。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用原子 SQLite 用例、类型化目标与完成端口、S5 实际责任监督及既有有限预算。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [取消契约](../../architecture/task-cancellation.md)、[自动预览联动](../../architecture/preview-obtaining.md#取消联动)、[清理取消](../../architecture/output-cleanup.md#清理取消)、[同步取消](../../architecture/status-sync.md#取消与失败)、[事务](../../camctl/database/transactions.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

完整寻址范围及自动预览候选先去重检查，包含发起取消动作自身则整个取消失败，任何目标都不生效。未启动任务可取消，已启动或可能启动且不支持停止的任务逐项拒绝并继续原流程。取消标记不是停止、读取结束或撤回完成。已经终态不改写，已生效目标责任不随取消发起者结束而撤销。取消动作自身被取消时停止新增影响并完成自身收场，不沿其原目标递归扩大后一个取消动作范围。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

参数形式在受理时校验，目标是否存在与资格在执行时判断。目标错误、读取失败及数据库未知分别表达，不能一律返回 target_not_found。

| 类型 | 字段或含义 |
| --- | --- |
| `CancelTarget / TargetResolution` | request_id、plan_instance_id、action_instance_id 或 plan_instance_id+group 四种有约束目标；可靠存在、可靠不存在或查询错误。 |
| `ResolvedTargets / FixedCancelSet` | 完整直接目标、自动关联候选和去重集合；自包含检查通过后，固定实际处理集合及拒绝明细。 |
| `CancelEligibility` | 按目标阶段、原取消、终态、固定停止能力及可能副作用判断的允许/拒绝/独立后处理。 |
| `CancelProgress / CancelItemResult` | 每目标生效事实、必要有限收场及本次最终成功/失败/不可撤回结果；不是目标自身终态。 |
| `CancelRepository / CancellationResult` | 完整固定、生效与汇总窄端口，保持已知集合及独立责任。 |
| `TargetSettlementPort` | 各目标类型提供实际有限收场结果；不能取消该端口拥有的真实任务来伪装完成。 |

先核实数据库、原终态及已有取消，再对新取消判断。

| 目标进度 | 停止能力 | 本次处理 |
| --- | --- | --- |
| 已终态 | 任意 | 保留终态，处理适用关联取回或交付。 |
| 取消已生效 | 任意 | 复用原责任及预算，继续实际有限收场。 |
| 可靠未启动且无可能生效调用 | 任意 | 允许取消，阻止普通启动。 |
| 已启动或可能启动，尚未终态 | 不支持 | 拒绝本项，不修改原取消事实，原任务继续；不等待自然结束。 |
| 已启动或可能启动，尚未终态 | 支持 | 允许，按原归属与预算停止并实际收场。 |
| 事实或提交不可靠 | 任意 | 错误或未知核实，不猜测未启动或取消成功。 |

取消动作的结果另分类：仍有适用有限工作则 running；全部结束且无拒绝/失败则 succeeded；全部结束且任一拒绝/失败/未确认则 failed；自身取消先可靠生效则 canceled，目标真实结果仍保存。processing 不可撤回是独立事实，不等于本次取消必然失败。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/cancellation/models.py` | 目标、资格和逐项进度类型。 |
| `apps/camctl/src/camctl/cancellation/targets.py` | 完整寻址、自动关联、去重与自包含检查。 |
| `apps/camctl/src/camctl/cancellation/rules.py` | 资格、发起者取消和汇总规则。 |
| `apps/camctl/src/camctl/cancellation/service.py` | 保存生效、组织独立有限处理。 |
| `apps/camctl/src/camctl/cancellation/ports.py` | 目标完成与窄仓储接口。 |
| `apps/camctl/src/camctl/persistence/repositories/cancellation.py` | 固定集合、取消事实、明细和结果共同保存。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [受理与持久化](2026-09-30-camctl-acceptance.md) | A3 保存合法参数，P3/P4 核实实际提交。 |
| [会话与调度](2026-09-30-camctl-session.md) | S5/Q4 协调启动与取消边界。 |
| [采集](2026-09-30-camctl-capture.md) | C3/C4/C5/C7 负责设备有限收场。 |
| [产物与报告](2026-09-30-camctl-outputs.md) | X7/X9/X10/X11 负责文件责任，R6 负责同步结束语义。 |

N2/N3 的本地资格和独立责任端口随首次副作用接入，不能等完整取消阶段才处理取消竞争。完整 N1 目标解析、N4 发起者竞争、N5 文件/同步组合及 N6 全入口在阶段 6 完成。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 组、计划和请求目标包含自身时无任何目标生效。 | N1，`test_self_target_has_no_partial_effect` |
| 可能启动且无停止能力不得按未启动取消。 | N2，`test_unknown_start_without_stop_is_rejected` |
| 取消标记已保存但停止未完成仍 running。 | N3，`test_cancel_mark_is_not_settlement` |
| C2 只取消 C1 不等待 C1 的目标 A。 | N4，`test_second_cancel_does_not_expand_scope` |
| 目标已终态仍需处理可撤回交付。 | N5，`test_terminal_obtain_still_withdraws_ready` |

## 实施任务

### N1 完整目标解析及自身检查

**预计文件：** `apps/camctl/src/camctl/cancellation/models.py`、`apps/camctl/src/camctl/cancellation/targets.py`；测试为 `apps/camctl/tests/unit/cancellation/test_targets.py` 和 `apps/camctl/tests/integration/cancellation/test_targets.py`。

**接口与依赖：** 提供 `resolve_cancel_target(target: CancelTarget, facts: CancelLookup) -> TargetResolution`、`prepare_cancel_set(origin: ObjectId, resolved: ResolvedTargets) -> FixedCancelSet | CancelTargetError`。前置交付：A3、K1；自动关联从首次受理事实读取，X10 随后提供真实消费者验证。

- [x] 编写失败用例。建立 `test_self_target_has_no_partial_effect`，直接自身、所属计划、包含自身的组及 request_id 各入口，`assert target_effects == ()` 且错误 cancel_self_target；其他目标也不能部分生效。可靠不存在使用错误登记的 cancel_target_not_found，查询错误不归不存在；自动候选去重但保留拒绝直接目标。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/cancellation/test_targets.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。先取得完整直接及自动关联候选，再检查自身，资格通过后固定实际集合和拒绝明细；不依据逐页半份结果先施加取消。
- [x] 再运行上述命令，要求全部 PASS，并核对 完整寻址范围不受动作当前可取消性提前缩小。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/cancellation/test_targets.py -q`，真实 SQLite 固定目标及自动关联，集合保存前后中断不产生部分取消。
- [x] 审阅实际接口、状态分区及失败路径，检查 四种寻址入口与预览候选是否都经过自包含检查；记录门禁证据，建议以“feat: 实现完整取消目标检查”形成独立提交。


#### N1 的阶段性验证（2026-10-05）

N1 完成，任务全部勾选：`cancellation/models.py` 提供 `CancelTarget`
（四种有约束组合在构造时校验互斥）、`TargetResolution`（可靠存在/
可靠不存在/查询错误三分，`lookup_failed` 不折叠为 missing）、
`TargetFacts`/`ResolvedTargets`/`FixedCancelSet`（`SelectionBasis` 与
`CancellationEffect` 与登记整数一致）及仓储输入
`FixCancelTargets`/`FailCancelTargets`（后者仅接受 cancel_self_target
与 cancel_target_not_found）。`cancellation/targets.py` 提供
`resolve_cancel_target`（四入口经 `CancelLookup` 端口查询，request_id
按内部整数身份寻址）与 `prepare_cancel_set`：自身检查使用完整集合
（直接目标∪全部自动关联候选），包含自身（含联动来源拒绝取消、联动
本不会生效的候选）时返回 `CancelTargetError("cancel_self_target")`；
通过后直接目标全部保留（终态目标初始 NOT_REQUIRED，其余 NOT_APPLIED），
拍摄终态或允许取消才联动其自动预览取回（重叠记 BOTH，去重），拒绝
取消的拍摄保留直接目标。`missing_target_error` 构造登记错误，详情
保留原请求目标对象。

仓储 `persistence/repositories/cancellation.py`：
`fix_cancel_targets`（TARGETS_FIXED.CANCEL 单事务创建全部 cancel_items
行，命令拒绝包含自身或重复目标、空集合按解析失败处理；原键重送核
实分支/时刻/成员集合后只读恢复）与 `fail_cancel_targets`
（TARGETS_FIXED.FAIL 以 24/25 结束动作且零成员；终态新键按原错误只
读恢复、错误不符拒绝）。`SqliteCancelLookup`/`sqlite_auto_candidates`
提供执行期查询（auto_preview_links is_valid=1）。守卫按登记名
`cancel` 注册：成员初始值必须待处理无结果、依据与初始效果取值合法、
联动成员须有有效自动关联且来源属于同事务直接目标、失败分支零成员
且错误码限于登记集合；动作类型与目标状态转换仍由既有 target_set 与
事件登记核对。

验证：单元 `test_targets.py` 15 项（四入口寻址与各入口可靠不存在、
查询错误不归不存在、自身检查四入口无部分效果、联动候选包含自身仍
按完整集合失败、终态效果分区、联动条件、BOTH 去重、联动来源不在直
接范围不联动）；集成 `test_targets.py` 10 项（真实 SQLite 四入口与
missing、有效关联候选读取、固定集合事务含依据与初始效果、自身范围
以 24 失败且零成员、不存在以 25 失败详情保留目标、原键重送/时刻冲
突/终态新键不产生部分取消、空集合拒绝不落任何行、守卫正反例三组：
联动无依据拒绝/非法初始值拒绝/失败分支成员与外错拒绝）。全量回归：
组件单元 2994、集成 3083 另 7 项跳过（含取消集成；守卫用例后补单目
录通过）、根跨组件 34 另 342 subtests、check-protocol、
check-report-dependencies、check-doc-links 2917 通过（Python 3.11）。

N2 起接入取消资格与启动竞争；N3 的生效事务消费本任务的固定集合。

### N2 取消资格及启动竞争

**预计文件：** `apps/camctl/src/camctl/cancellation/rules.py`；测试为 `apps/camctl/tests/unit/cancellation/test_eligibility.py` 和 `apps/camctl/tests/integration/cancellation/test_eligibility.py`。

**接口与依赖：** 提供 `decide_cancel_eligibility(target: TargetFacts) -> CancelEligibility`；TargetFacts 含原终态/取消、真实派发阶段、可能效果和固定 stop_supported。前置交付：Q4、C1/O1 的目标事实。

- [ ] 编写失败用例。建立 `test_unknown_start_without_stop_is_rejected`，原启动在途或发送未知且 stop_supported=False，`assert may_apply_cancel is False`，原等待和核实继续；未启动允许，支持停止但实际失败与拒绝分开。拍摄拒绝时不间接取消自动取回，直接目标取回仍独立处理。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/cancellation/test_eligibility.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按表完整判断，并在目标同一执行协调边界与启动派发确定顺序；使用原任务固定能力，不读取新默认值改变原保证。
- [ ] 再运行上述命令，要求全部 PASS，并核对 所有未启动/可能启动/已启动/终态×停止能力分区明确。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/cancellation/test_eligibility.py -q`，真实意图、派发与取消提交同步点竞争，核对取消先成立则无启动，启动先成立按对应分区。
- [ ] 审阅实际接口、状态分区及失败路径，检查 录像、照片、延时摄影和自动关联全部资格入口；记录门禁证据，建议以“feat: 实现取消资格与启动协调”形成独立提交。

### N3 取消生效与逐目标独立收场

**预计文件：** `apps/camctl/src/camctl/cancellation/service.py`、`apps/camctl/src/camctl/cancellation/ports.py`、`apps/camctl/src/camctl/persistence/repositories/cancellation.py`；测试为 `apps/camctl/tests/unit/cancellation/test_effects.py` 和 `apps/camctl/tests/integration/cancellation/test_effects.py`。

**接口与依赖：** 提供异步 `apply_cancel(command: ApplyCancel, key: OperationKey) -> DbOutcome[CancelProgress]`、`summarize_cancel(progress: CancelProgress) -> CancellationResult`；ApplyCancel 含固定集合及原发起者身份。前置交付：N1/N2、P3/P4、S5、目标模块的 TargetSettlementPort。

- [ ] 编写失败用例。建立 `test_cancel_mark_is_not_settlement`，标记提交而停止仍在途，`assert cancel_status is ActionState.RUNNING`；全部有限处理结束才汇总。任一失败不放弃其他有限处理，最终机器错误 cancel_items_failed/execution/details={}，具体原因在 items。目标终态与取消项结果分别保持。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/cancellation/test_effects.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。保存目标取消事实和独立必要责任，目标流程实际推进；取消动作只等待自身固定范围，原停止/读取/撤回预算复用，不创建第二套补偿。
- [ ] 再运行上述命令，要求全部 PASS，并核对 目标责任可在发起者不等待时继续发现和保存。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/cancellation/test_effects.py -q`，真实仓储、调度和各目标替身，验证取消、迟到结果、提交未知及重启后汇总。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有结果接手、目标完成与发起者等待是否被一个 Future 混同；记录门禁证据，建议以“feat: 实现取消效果与独立收场”形成独立提交。

### N4 取消动作自身被取消

**预计文件：** `apps/camctl/src/camctl/cancellation/rules.py`、`apps/camctl/src/camctl/cancellation/service.py`；测试为 `apps/camctl/tests/unit/cancellation/test_controller_cancel.py` 和 `apps/camctl/tests/integration/cancellation/test_controller_cancel.py`。

**接口与依赖：** 提供 `decide_origin_cancel(facts: CancelOriginFacts) -> OriginCancelDecision`；facts 含自身终态/取消、已生效目标、尚未确认事务及自身实际责任。前置交付：N3、S5、L3 的自身重要日志责任。

- [ ] 编写失败用例。建立 `test_second_cancel_does_not_expand_scope`，C1 取消 A 后 C2 只取消 C1，`assert c2.wait_targets == {c1_id}`，A 继续原停止；C2 同时包含 A 时复用 A 原流程。覆盖未生效、部分/全部生效、目标结果已完成但 C1 最终未提交、自身取消与未知事务组合。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/cancellation/test_controller_cancel.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。先核实已提交和未知操作，自身取消成立后不新增目标影响、停止等待目标完成，但保留自身数据库/日志及实际任务收场；已生效目标独立继续，C1 最终 canceled。
- [ ] 再运行上述命令，要求全部 PASS，并核对 后一个取消的等待范围只由自身直接及有效关联目标决定。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/cancellation/test_controller_cancel.py -q`，SQLite 在各生效与最终结果边界中断，验证 C1 canceled、C2 succeeded 与 A 仍停止可同时成立。
- [ ] 审阅实际接口、状态分区及失败路径，检查 设备停止、读取、交付撤回和清理四种独立收场的发起者取消；记录门禁证据，建议以“feat: 实现取消发起者的取消语义”形成独立提交。

### N5 文件撤回、清理及同步组合

**预计文件：** `apps/camctl/src/camctl/cancellation/service.py`；测试为 `apps/camctl/tests/unit/cancellation/test_target_types.py` 和 `apps/camctl/tests/integration/cancellation/test_target_types.py`。

**接口与依赖：** 通过目标拥有者 `settle_cancel(target: CancelItemIdentity) -> CancelItemResult` 端口组织，实际实现分别由 capture、outputs、reporting 提供。前置交付：X7/X9/X11、R6、C7、N3/N4。

- [ ] 编写失败用例。建立 `test_terminal_obtain_still_withdraws_ready`，原取回 succeeded 而 ready 可撤，`assert old_action_status is SUCCEEDED` 且交付撤回事实单独更新；processing 不删除。显式报告未开始/运行/成功后取消按原同步责任分类，不能取消共享生成任务。清理已删项不阻止其他项取消。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/cancellation/test_target_types.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按真实目标类型调用窄端口，固定本次有限范围；时钟异常只接纳规定的未定时取消和安全收场，不扩张到普通取回或清理。
- [ ] 再运行上述命令，要求全部 PASS，并核对 取消生效、实际停止、不可撤回及同步责任的完成含义各自独立。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/cancellation/test_target_types.py -q`，真实文件、数据库、报告进程及目标流程验证撤回竞争、取消同步与清理未知。
- [ ] 审阅实际接口、状态分区及失败路径，检查 各目标的错误、终态后责任、有限预算及 processing 所有权；记录门禁证据，建议以“feat: 接入各目标取消与收场”形成独立提交。

### N6 所有目标入口与恢复验收

**预计文件：** `apps/camctl/src/camctl/cancellation/service.py`；测试为 `apps/camctl/tests/integration/cancellation/test_cancellation_contract.py`。

**接口与依赖：** 使用真实 run/submit、取消仓储、调度、报告和全部目标流程。前置交付：N1—N5、C1—C8、X1—X11、R1—R8；C9/X12 为同层验收，不作为前置。

- [ ] 编写失败用例。在 `test_cancel_results_survive_restart` 中对动作/组/计划/请求四类目标施加同一事实，`assert item_results == independent_expected_results`；已拒绝任务后来自然结束不能改旧取消失败，同请求重送不重复效果。固定 H 后继续收场，旧报告字节保持。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/cancellation/test_cancellation_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。将取消专题的全部目标、资格、发起者进度和等待范围矩阵映射到实际用例，核验关联预览、文件及同步责任的端到端结果。
- [ ] 再运行上述命令，要求全部 PASS，并核对 全部取消结果有可复查事实，取消不会无限等待设备或客户端。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有入口共享的自身检查、资格、提交未知与迟到结果不变量；记录门禁证据，建议以“test: 验证完整取消与恢复闭环”形成独立提交。

## 模块完成门禁

完整目标、自包含、资格、独立生效、发起者再取消及所有目标类型通过真实软件组合。取消动作结果与目标事实分别保存，已有终态及原预算保持。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
