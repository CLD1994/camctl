# 拍摄终态完整申请的保存恢复

> 执行者使用 `superpowers:executing-plans` 按任务落实；下列步骤跟踪已有第一版保存契约，不增加业务范围。

**目标：** 拍摄终态提交结果未知或可靠回滚后，会话继续持有完整申请、原操作键与事实时刻，并在后续业务判断前核实原申请。

**正式依据：** [持久化接口契约](../../camctl/persistence-runtime.md#持久化接口契约)、[已提交结果与当前状态的边界](../../camctl/persistence-runtime.md#已提交结果与当前状态的边界)、[原子操作与副作用交接](../../camctl/module-contracts.md#原子操作与副作用交接)。文件与终态必须共同提交，原键核实不重新取得设备资格。

**实现起点：** `73f474f`。录像收场的 `PendingRecordingResults` 已持有完整申请；普通完成、非录像取消、取消延时摄影及没有内部 READ 的绑定失败仍直接提交。内部 READ 的绑定失败已经由 `PendingReadBusiness` 持有。

## 状态与责任

完整申请在首次保存前确定；草稿、配对、错误、配置、责任集合、事实时刻和操作键均属于这份申请。恢复期间不重新构造其中任何一项。申请只在当前会话拥有的集合内保持；进程退出后，没有旧申请时按可靠数据库事实恢复，不能声称内存申请已持久保存。

| 原申请与数据库结果 | 行为 |
| --- | --- |
| 没有待保存申请，业务前置成立 | 构造一次完整申请，先交给会话持有，再提交。 |
| 原申请尚在集合中 | 先按原键及完整输入核实；禁止用新申请替换。 |
| `COMPLETED` 且处理结果完整 | 按已保存事实释放对应的保存责任；随后才允许依赖步骤。 |
| `UNKNOWN`、`ROLLED_BACK` 或没有完整处理结果 | 保留原申请，报告可诊断错误并停止依赖步骤。 |
| 动作已终态，但原申请仍持有 | 仍核实原申请；终态早退不能跳过该责任。 |
| 当前绑定或配置变化，原申请仍持有 | 核原申请，不调用设备、不改原配置或事实时刻。 |
| 主申请可靠完成，原内部读取调用已返回但流程仍等待重试 | 固定附属 `StaleRunFinish` 及原键，以主申请的事实时刻按业务终态结束原读取流程；原实际读取尝试及其失败不改写。 |
| 附属读取收尾提交未知或回滚 | 主申请保持已可靠完成；继续持有附属完整申请，并按其原键和事实时刻核实，禁止重新构造。 |
| 原内部读取仍有未结束的实际尝试 | 不按业务终态补造读取结束，保持申请和原读取责任，交回既有实际读取结束或恢复机制。 |
| 原申请不存在于历史，当前已出现不兼容业务事实 | 保留申请并报告仓储的诊断；不自行增加通用退役语义。录像已有的明确退役规则保持。 |

内部 READ 的完整申请继续由原读取拥有者保存，不再同时登记到拍摄集合。包含实际调用结果的 `finish_start_result` 复合申请由 `PendingCallResult` 保存；没有新实际结果的 `close_start` 外层完整申请由共有集合持有 `StartCloseRequest`，实现与验证见[任务四](#task-4-启动流程外层复合申请的保存恢复)。其中嵌套的 `FinishCapture` 不能单独提交。

## 建议实现边界与横切检查

内部命名建议为 `PendingCaptureCompletion`、`pending_capture_completions` 和 `resume_capture_completions`；这些名称及文件划分不是正式协议约束。复用既有录像集合的会话生命周期，显式分派 `FinishCapture`、`FinishCanceledCapture`、`FinishBindingFailure` 和 `FinishRecordingResults`，不采用默认分支将未知类型当录像收场。

生产修改预计涉及 `capture/handlers.py`、`bootstrap/capture_assembly.py` 和 `bootstrap/lifecycle.py`。正常、残留、受限工厂共享同一集合；默认 scheduling、residual、winddown 及正常／受限 cancel 在候选查询前恢复。photo、timelapse 与 record 的处理器也须在终态早退、绑定核对和新业务前恢复。保存后的输入读取收尾责任不得因原调用点退出而遗漏。

## Task 1: 四类完整申请与处理器恢复

**消费：** 既有四种仓储请求和 `DbOutcomeKind`。**产出：** 一个会话共有的完整申请集合及显式类型分派，三个拍摄处理器先核申请。

- [x] 在 `apps/camctl/tests/integration/capture/test_capture_completion_save_recovery.py` 建立真实照片、延时摄影、取消延时摄影和绑定失败前置。在真实 COMMIT 前／后注入错误并拒绝回滚，确认回执确为 UNKNOWN，关闭原连接后重新打开同一库；验证原 request/key/T1 保持、唯一事务组、完整文件与终态共同提交及无新设备调用。
- [x] 根 Agent 用 Python 3.11 前台独占运行该文件并记录有效红；fixture 错误先修正，不能作为产品红。
- [x] 实现共同持有和恢复，所有首次提交先登记申请。只在可靠完成且结果完整后移除；未知、回滚及复验失败保持责任。
- [x] 补齐非录像无草稿取消、重复未知／可靠回滚及无完整结果的边界。原内部 READ 集合和录像明确退役保持。
- [x] 使用真实已返回的失败 READ 及未耗尽预算，补取消 primary 未知后的五入口收尾和附属读取收尾 COMMIT 前／后 UNKNOWN。先运行 `test_capture_completion_read_settlement.py` 取得有效红，再把附属完整申请接到共同 holder；不得结束在途实际尝试。
- [x] 顺序运行新文件和既有拍摄原申请测试。限定新增范围全部通过；既有未决失败逐项登记。

## Task 2: 默认会话前置恢复

**消费：** 任务一的共有申请与恢复函数。**产出：** 三个真实工厂和五种默认 flow 在候选之前沿 fresh Owned 核原申请。

- [x] 在 `apps/camctl/tests/integration/bootstrap/test_capture_completion_default_recovery.py` 使用实际 lifecycle 装配，隔离主会话循环及设备接口。至少覆盖三种普通消费者及绑定失败、五个默认入口、真实 COMMIT 前／后错误；没有手工向第二个工厂复制申请。
- [x] 在候选查询边界检查原键已可靠保存、原集合已释放、原实际结果和文件保持；提前读取候选、使用新 key/T1、终态后不核原键均须使测试失败。
- [x] 根核任务一有效红，并复用已有录像前置接线使三个工厂共享共有集合；保持必要保存失败被映射为 `StateDbFailure`，不进入设备操作。该接线裁决与证据边界见下文。
- [x] 顺序复验新 bootstrap 文件、既有录像默认恢复和内部 READ 默认恢复。受影响恢复用例通过，目录中的既有集合判定失败另行登记。

## Task 3: 阶段门禁与提交

- [x] 根顺序运行 capture 组件集成目录、bootstrap 受影响恢复文件及全量单元测试。集成目录不合并，pytest 不并行；记录日期、环境、实际命令、结果及所有失败。
- [x] 独立只读审查四种申请、全部提交入口、候选前置、终态早退及输入读取后续责任。修复重要问题时先取得失败测试；不宣称覆盖没有执行的竞争分区。
- [x] 更新本计划与原缺口记录，执行 diff 检查并按用户持续授权整体提交阶段变更；提交后报告 commit 和工作区状态，不推送。

## 审查重点与完成边界

1. 类型未知时明确诊断；任何类型不得错误路由到录像仓储。
2. 普通申请失败后再进入 handler，以及已提交后终态不再被候选选中，两种情况均保留原申请核验。
3. 含预览配对的绑定失败和取消延时摄影必须保持完整草稿，不能降为无产物取消。
4. 内部 READ 已拥有的绑定失败申请不得由两个集合重复掌权；原录像输入读取收尾继续完成。
5. 原键可靠未提交、同时出现不兼容取消或终态时，不照搬录像退役规则。

本阶段不改变持久化格式，不为未决 RESULTS v2、应急错误身份或通用退役选择语义。仓储完整输入核实的额外缺口须按实际证据处理；阶段通过不能替代全 app 的软件验收。

## 实施与验证记录（2026-10-09）

环境为容器内 Python 3.11.16，使用已有 `apps/camctl/.venv`，与部署版本一致。根 Agent 前台独占、按目录顺序执行 pytest；设备接口由受真实契约约束的替身提供，不连接相机。此处只记录已有第一版保存契约的实施与证据，不增加业务范围。

共同持有者为 `PendingCaptureCompletion`。普通完成、非录像取消、取消延时摄影和无内部 READ 的绑定失败均先登记完整申请，再沿原 key 保存。三个工厂及五种默认入口共用集合，候选读取和 handler 终态早退之前核实原申请。内部 READ 的绑定失败仍由 `PendingReadBusiness` 单独持有。

主申请可靠完成后，如果原内部 READ 调用已经返回、流程仍开放，会话继续持有附属 `StaleRunFinish`。附属提交未知或回滚时保持其原完整输入、key 和主申请事实时刻，不改写原实际读取尝试，也不再次提交已可靠完成的主申请。有 RUNNING 实际尝试时诊断并保留责任，不补造读取结束。

实际执行命令采用以下前缀，后接表中的测试路径及 `-q`：

```bash
PYTHONPATH=/workspaces/camctl/apps/camctl/src apps/camctl/.venv/bin/python -m pytest
```

| 验证范围 | 实际结果 | 日志 |
| --- | --- | --- |
| `integration/capture/test_capture_completion_save_recovery.py` | 8 failed → 8 passed；四个入口分别在真实 COMMIT 前／后出现 UNKNOWN。 | `/tmp/camctl-goal-capture-completion-red.log`、`/tmp/camctl-goal-capture-completion-green.log` |
| `integration/bootstrap/test_capture_completion_default_recovery.py` | 56 passed；覆盖五入口、配置变化、原键复验再次 UNKNOWN／ROLLED_BACK。 | `/tmp/camctl-goal-capture-completion-bootstrap.log` |
| `integration/bootstrap/test_capture_completion_read_settlement.py` | 12 failed → 12 passed；实际 READ 已失败且调用返回、预算未耗尽，再通过公开事务取消。 | `/tmp/camctl-goal-capture-completion-read-red.log`、`/tmp/camctl-goal-capture-completion-read-green.log` |
| `unit/capture/test_completion_save_responsibility.py` 与 `unit/capture/test_start_gate.py` | 初次局部门禁 45 passed；全量门禁另行记录。 | `/tmp/camctl-goal-capture-completion-unit-scoped.log` |
| `integration/bootstrap` 全目录 | 791 passed、6 failed、1 skipped，599.21s，exit 1；新增两个文件的 68 项全部通过。 | `/tmp/camctl-goal-capture-completion-bootstrap-all.log` |
| `integration/capture` 全目录 | 613 passed、11 failed，167.86s，exit 1；新增 8 项及本阶段受影响的既有保存恢复用例通过。 | `/tmp/camctl-goal-capture-completion-capture-final.log` |
| `unit` 全目录 | 3920 passed、1 skipped、2 warnings，9.24s，exit 0；含新增 23 项共同申请与实际 RUNNING 守卫。 | `/tmp/camctl-goal-capture-completion-unit-final.log` |

两个 warning 来自既有同步测试的 asyncio 标记：`unit/devices/test_read_session.py::test_source_stream_protocol_shape` 与 `unit/operations/test_process.py::test_local_exit_is_exclusive`。目录门禁实际执行顺序为 bootstrap、capture、unit；没有合并集成目录或并行 pytest。新增集成测试共 76 项全部通过，新增单元测试 23 项通过；此结论不等于两个集成目录全绿。

四类申请的单元分区覆盖 UNKNOWN、ROLLED_BACK、NOT_EXECUTED、缺少完整业务结果、可靠完成释放、未知请求类型及动作身份错配。另有 RUNNING 实际读取阻止流程结束的守卫用例。非录像无草稿取消通过既有公开取消单元入口和共同请求分区验证，没有把有草稿的 UNKNOWN 组合测试计作无草稿独立组合证据。

执行裁决与代价如下。

| 决定 | 依据与可能代价 |
| --- | --- |
| 继续实施已有契约，不重新询问设计审批及执行方法。 | 用户持续授权优先；未定义业务分区仍停止。若授权理解有误，本地可逆变更需要调整。 |
| 在当前 `implement_camctl` 共用分支工作，整体提交本阶段。 | 用户已授权整体阶段提交，包括准确登记的已知失败。代价是历史粒度较粗。 |
| pytest 按目录前台独占，协作者使用受约束替身。 | 仓库测试规范优先；没有新变化或疑问时不重复已通过门禁。代价是不能以单个合并命令展示全套结果。 |
| 复用已有录像前置接线，共同类型迁移时同步改名。 | 主缺陷有 8 项有效红；不撤回正确接线制造任务二独立红。代价是没有单独的“新接线失败”日志。 |
| 独立审查从阶段起点核实际工作区及新增文件。 | 用户要求降低整理历史成本；此次不重复审查此前阶段的全部提交。代价是结论仅适用于本阶段。 |

独立只读审查实际核查生产差异、四个新增测试文件及正式契约，没有确认本阶段新增的 Critical、Important 或 Minor 代码缺陷。审查未运行 pytest，也不将未结束的目录门禁称为通过。下列边界仍分别保留。

| 考虑的分区 | 结论及未核验时的代价 |
| --- | --- |
| 带预览配对的 UNKNOWN 恢复 | 原完整 request 不被拆解；新增 fixture 只有一个原文件，没有专门的配对组合证据。若该组合有遗漏，需要补真实原片、预览及配对的 COMMIT 前后恢复测试。 |
| 两种启动复合申请 | 包含 `AttemptFinish` 的 `finish_start_result` 由 `PendingCallResult` 持有；没有新实际结果的 `close_start` 由共有集合持有完整 `StartCloseRequest`，见[任务四](#task-4-启动流程外层复合申请的保存恢复)。两者都不能单独提交嵌套的动作结果。 |
| 进程退出后的原内存申请 | 会话内持有不等于申请已持久化；退出后按可靠数据库事实恢复。代价是本阶段不证明内存申请可以跨进程重建。 |
| 原键未提交且出现不兼容取消或终态 | 仓储诊断后保留申请；普通请求不采用录像退役规则。未决分区需要确定语义后才能继续业务。 |
| RESULTS v2、UNSATISFIED reason 与应急错误详情 | 保留原有失败与各自未决事项，不新增语义。代价是当前不能宣布全 app 验收通过。 |
| 通用仓储重送的额外输入核验 | 本阶段保持原对象，不修改全部重送核验规则。完整输入变体的覆盖仍需按实际仓储缺口核验。 |

### bootstrap 全目录的失败归属

下列六项在 `/tmp/camctl-goal-combined-bootstrap-current.log` 已有相同失败表现。本次没有修改延时摄影集合结束判定；v1 条目没有独立集合结束依据。五个预期成功的入口实际保存 FAILED；缺少必需类别的入口实际保存 UNCONFIRMED 集合，而非测试预期的 UNSATISFIED。既有未决格式及集合判定继续由[产物核实计划](2026-10-09-camctl-result-round-runtime.md#分页格式的待定事项)跟踪，不改状态预期或补造 `set_finalized`。

| 文件 | 实际失败用例 |
| --- | --- |
| `test_production_flows.py` | `TestRunSessionProductionScheduling::test_timelapse_reaches_success_through_default_flows` |
| `test_timelapse_finish.py` | `TestTimelapseSatisfiedFinish::test_wait_completes_and_confirms_time_and_outputs` |
| `test_timelapse_finish.py` | `TestTimelapseUnmetFinish::test_missing_required_kind_saves_known_failure` |
| `test_timelapse_finish.py` | `TestTimelapseCheckRounds::test_incomplete_file_retries_next_round_then_succeeds` |
| `test_timelapse_finish.py` | `TestTimelapseCheckRounds::test_listing_failure_consumes_round_then_recovers` |
| `test_timelapse_finish.py` | `TestTimelapseCancel::test_unstoppable_cancel_fails_and_task_completes` |

### capture 全目录的失败归属

实际失败与阶段起点的[十一项失败记录](2026-10-09-camctl-binding-failure-retained-files.md#实施与验证记录2026-10-09)一致。中途目录门禁另有两项取消延时摄影的旧异常类预期，与共同保存边界现在使用的 `ConsistencyError` 不符；测试精确要求该异常后，真实投影故障、原回滚错误、outputs/action 共同回滚和恢复断言全部保持，最终目录门禁的这两项通过。

| 原有分区 | 最终失败用例及原因 |
| --- | --- |
| v1 缺少集合结束依据 | `test_capture_contract::TestTimelapseHandler::test_send_wait_then_finish`；`test_timelapse_wait_runtime::test_backward_wall_clock_change_does_not_extend_current_session_wait`。测试预期 SUCCEEDED，实际仍 RUNNING。 |
| UNSATISFIED 输入错误缺少 `stage` | `test_capture_failure_activity_identity::test_closed_unsatisfied_timelapse_reports_actual_activity_without_query`；`test_result_confirmation::TestResultSetConfirmation::test_unsatisfied_saves_known_failure_and_keeps_occupancy`；`test_result_consumer_saves::test_closed_result_consumers_use_saved_input_without_device_query[timelapse]`；`test_result_file_recovery::test_closed_latest_error_keeps_previously_registered_file_input[timelapse]`。公共结构校验拒绝输入；正式错误 reason 尚未确定。 |
| 应急生产错误缺少 `details` | `test_emergency::test_zero_attempts_unknown_config_saves_not_attempted`；`test_later_session_preserves_exact_old_error_and_omits_unchanged_activity[EmergencyOutcome.NOT_ATTEMPTED]`；同入口 `[EmergencyOutcome.UNCONFIRMED]`；`test_unconfirmed_with_attempts_saves_unconfirmed`；`test_unrecorded_emergency_does_not_release`。活动结果守卫拒绝错误；身份与详情语义仍由结果错误计划跟踪。 |

## Task 4: 启动流程外层复合申请的保存恢复

**起点与消费：** `5fc94b3`；已实现的共有 `PendingCaptureCompletion` 及三个工厂、五种前置。`CaptureRepository.close_start(finish, action_finish, key, owned)` 没有新 `AttemptFinish`，以原 `StaleRunFinish` 和 `FinishCapture` 共同保存；正式成功返回 `DbOutcome[None]`，`COMPLETED/value=None` 合法。普通四类终态申请仍要求完整 `CaptureResult`，不得放宽其校验。

**已确定的契约：** 在第一次提交前固定两个完整子请求、责任集合、错误及决定依据、共同事实时刻和 key，交给会话持有。未知或回滚后先核原外层请求；已有终态、当前绑定和配置变化均不能跳过。共同提交包含适用 START／QUERY、动作和父计划结果，以及可靠无效果分区的占用释放。没有实际调用在途时才形成该请求，不改变原调用尝试。

已知生产入口的分类如下；此处不新增启动失败或释放占用的语义。

| 业务前置 | 外层收场结果 |
| --- | --- |
| 所有原 START 尝试可靠无效果，动作未取消／过期，当前上限已经耗尽 | START FAILED、动作失败、适用占用释放共同保存。 |
| 原 START 效果未知，设备没有查询能力或端口 | 原 START 和固定查询责任按 UNCONFIRMED 收场，动作失败，未知占用保持。 |
| 原 START 效果未知，查询已经可靠结束或当前查询预算耗尽 | 与上一行相同，固定原查询责任与原尝试，禁止再次启动。 |

每个业务分区分别应用以下保存规则。

| 保存及恢复状态 | 会话责任与后续动作 |
| --- | --- |
| 首次外层申请尚未提交 | 先登记完整外层申请，再调用 `close_start`。 |
| COMPLETED，包含该接口的合法空返回值 | 清除原请求的重试等待及持有责任；后续业务可以继续。 |
| UNKNOWN、ROLLED_BACK、NOT_EXECUTED、原键读取失败或再次核实未知 | 保持同一申请及 key，诊断并停止依赖业务；不重新取得时钟、上限或设备端口来构造申请。 |
| 数据库已保存动作终态，但会话还持有原请求 | 五个默认 flow 候选之前及直接 handler 终态早退之前仍核原请求。 |
| 可靠确认原请求缺失，但取消或其他终态使原业务资格不兼容 | 保留原请求及仓储诊断，停止该分区；不增加退役、取消替换或新键重做规则。 |

**建议实现：** 用内部冻结类型 `StartCloseRequest(finish, action_finish)` 表达完整外层请求，加入共有 holder 的显式分派。调用原 `close_start`，不修改仓储事务、格式或四类请求的成功判定。对该类型跳过输入 READ 的附属收尾，因为 START 尚未确认，不授予内部读取资格。命名和函数划分可以按实际数据流调整。

**预估文件：** `capture/handlers.py`；新 `integration/capture/test_start_close_save_recovery.py` 和 `integration/bootstrap/test_start_close_default_recovery.py`；现有 `unit/capture/test_completion_save_responsibility.py`。现有工厂共用集合无需重新创建一套持有机制。

- [x] 只写真实 capture 反例：三种业务前置分别覆盖真实 COMMIT 前／后 UNKNOWN，可靠无效果及未知两种前置另覆盖真实投影错误和可靠回滚。根独占运行新文件，预期失败于缺少原外层持有者；fixture 错误不算产品红。
- [x] 实现完整外层登记和显式分派，再运行同一文件。预期原两个子请求对象、key、T1保持；终态与适用流程／占用同组保存、原尝试与历史前缀保持、无新增设备调用。
- [x] 添加局部单元用例，验证原四类请求缺少 `CaptureResult` 仍被拒绝；新外层合法 `COMPLETED/value=None` 才释放，失败结果保持。同一动作不得替换原外层请求。
- [x] 用真实 lifecycle 装配三个工厂，通过默认 scheduling、residual、winddown及正常／受限 cancel 五个入口核原申请。覆盖 COMMIT 前后和原键复验再次失败，改变当前配置与绑定，候选、时钟及设备不能先于核实被消费；手工把申请复制进第二工厂不算共享证明。
- [x] 根顺序复验新增 capture、全部 START 已有门禁、共有终态门禁及受影响 bootstrap 恢复文件，再跑全 unit；目录分开、pytest独占。按真实改动选择更宽目录；已有十七项失败及先前环境范围继续准确保留。
- [x] 独立只读审查本阶段差异和三个已知生产入口，重要修复取得有效红绿；更新实际证据，整体提交阶段变更并报告工作区状态。

技能要求新设计或执行方法再次审批，与用户的持续完成目标及整体阶段提交授权冲突；依据更高优先级的持续授权继续实施已有保存契约，代价仅为可逆本地调整。此任务记录第一版已有责任的缺口，不作为新增业务范围或完成率分母。

任务四审查重点：外层两个子请求不能拆交不同事务；合法空返回值不得放宽原四类请求；原查询责任、当前额度下降及无查询能力三入口均审计；已提交终态不能绕过核原键；可靠回滚后出现不兼容取消／终态时保留诊断，不自行选择退役规则。

### 任务四实施与验证记录（2026-10-09）

环境为容器内 Python 3.11.16，使用已有 `apps/camctl/.venv`。生产实现使用冻结的 `StartCloseRequest`，在首次提交前持有原两个子请求、key 和事实时刻；共有恢复入口将它们共同交给 `close_start`。该接口的合法空成功值只释放本申请的等待和保存责任，原四类请求仍要求完整处理结果。三个原生产入口的预算、错误和占用判定保持原契约。

所有 pytest 都由根 Agent 前台独占运行。命令前缀为 `PYTHONPATH=/workspaces/camctl/apps/camctl/src apps/camctl/.venv/bin/python -m pytest`，下表范围后接 `-q`，完整输出重定向至所列日志。

| 范围 | 实际结果 | 日志 |
| --- | --- | --- |
| `apps/camctl/tests/integration/capture/test_start_close_save_recovery.py`，实现前 | 8 failed，2.19s，exit 1；真实 COMMIT 前后未知及投影回滚前置成立，全部失败于缺少原外层持有者。 | `/tmp/camctl-goal-start-close-red.log` |
| 同一 capture 文件，实现后 | 8 passed，1.57s，exit 0；原两个子请求对象、key、事实时刻及唯一完整事务保持，没有新设备调用。 | `/tmp/camctl-goal-start-close-green.log` |
| `apps/camctl/tests/integration/bootstrap/test_start_close_default_recovery.py` | 42 passed，3.35s，exit 0；含三种实际前置、COMMIT 前后、五入口，以及原键核实再次 UNKNOWN／ROLLED_BACK。 | `/tmp/camctl-goal-start-close-bootstrap-new.log` |
| 下列六个受影响 bootstrap 文件 | 137 passed，73.22s，exit 0；共有四类申请、录像收场及内部 READ 后续责任均通过。 | `/tmp/camctl-goal-start-close-bootstrap-regression.log` |
| `apps/camctl/tests/integration/capture` 全目录 | 621 passed、11 failed，167.83s，exit 1；失败用例集合与任务三记录完全相同，错误仍属于已登记的三个未决分区。 | `/tmp/camctl-goal-start-close-capture-final.log` |
| `apps/camctl/tests/unit` 全目录 | 3925 passed、1 skipped、2 warnings，9.22s，exit 0；新增 5 项外层保存责任分区通过。 | `/tmp/camctl-goal-start-close-unit-final.log` |

受影响 bootstrap 命令逐项选择同一目录内的 `test_capture_completion_default_recovery.py`、`test_capture_completion_read_settlement.py`、`test_recording_results_cancellation.py`、`test_recording_results_saved_consumers.py`、`test_record_result_run_close_recovery.py` 和 `test_read_default_consumers.py`。本任务没有重跑约十分钟的 bootstrap 全目录；其上一阶段六项未决失败继续保留，不把局部回归称为目录全绿。全 unit 的两个 warning 仍是上文列出的既有同步测试 asyncio 标记。

新增 capture 反例先实际失败，再实施生产逻辑。新增 bootstrap 验证沿用已经接通的共有前置，不移除有效接线来制造额外失败；新增单元边界没有各自独立红日志。局部单元测试最初的 `RunOutcome` 导入错误属于测试输入问题，不计作产品反例；最终全量已验证正式错误的 `execution` 阶段。

实际 lifecycle 测试采用缺少设备声明的装配，三个真实工厂逐一检查共有集合及等待门的对象身份。原 START／QUERY 已由真实 handler 保存；原生产者在形成申请前接入空共有集合并交接已有等待锚点，五个实际 flow 以新可靠连接核实。此证据证明共有责任接手，不证明缺少绑定的工厂可以新建 START 请求。配置目录与原库登记身份匹配，没有改写数据库业务状态。

独立只读审查实际读取本阶段差异、两个新增集成文件、单元文件、仓储及三个原生产入口，没有确认 Critical 或 Important 代码问题。计划中的保存责任说明按当前两个接口分别登记。审查没有运行 pytest；根独立执行上述门禁。

| 审查考虑的边界 | 有效结论与未核验时的代价 |
| --- | --- |
| 可靠回滚后出现不兼容取消或终态 | 静态路径由仓储拒绝并保留原 holder；没有该竞争组合的独立实跑证据。不能据此宣布该组合完整验收。 |
| 查询自然耗尽的最后一次返回 | 含实际返回的申请由 `finish_start_result` 持有；外层测试使用原额度为 2 时的一次真实失败查询，随后本次额度降为 1。不能混算两个入口。 |
| 附属 READ 与进程退出 | START 未确认时没有内部 READ 收尾资格；本任务证明会话内持有及新连接核实，不证明进程退出后重建原内存申请。 |
| 仓储重送输入变体及原未决失败 | 没有改变全部仓储核实规则，也未选择 RESULTS v2、UNSATISFIED reason 或应急错误详情。相关缺口和十七项既有失败仍保留。 |

任务四继续采用用户已授权的阶段整体本地提交；提交粒度较粗，可逆调整仍在当前特性分支进行。根负责生产实现，同一明确指派的协作者仅编写独立测试文件；根实际读取并独占运行。单元测试按仓库规则隔离真实 IO，重要协作边界由真实集成测试验证。实际门禁作为完成证据，不重复没有新增变化的测试，也不合并不同集成目录。审查范围使用 `5fc94b3` 至阶段工作区差异及新增文件，不用空提交范围代替实际审查；代价是证据必须逐项注明范围。
