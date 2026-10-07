# camctl 采集执行与恢复模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 实现录像、单张拍摄和延时摄影各自的正常、取消及恢复流程，并可靠登记其正式产物。

**组织建议：** 三种拍摄按独立用例文件组织，共用操作及占用接口；结果核实、恢复和内部媒体处理消费明确设备证据，不采用单一录像控制模型。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 asyncio、精确时间、O2/O4 操作责任、按能力驱动及 X4—X6 的可恢复拷贝；媒体能力通过 F6。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [录像](../../architecture/camera-recording.md)、[照片与延时摄影](../../architecture/camera-capture.md)、[异常录像](../../architecture/camera-recovery.md)、[设备占用](../../camctl/database/operation-fields.md#设备活动占用的释放条件)、[会话应急](../../architecture/session-errors.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

拍摄的原参数、驱动及任务定义首次受理后固定。录像从可靠启动确认取得单调锚点，数据库或日志不重置锚点；启动窗口只限制派发。照片与延时摄影按驱动返回、完成及停止能力选择路径，不要求查询。可靠采集、设备活动结束、文件归属/写完/集合齐备、内部处理和动作结果分别保存。最终动作、正式产物、适用内部文件提升及父计划同事务提交，已有终态不改写。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

三种流程只通过共同管理接口暴露推进及恢复，不共享一个含全部阶段的万能状态机。每个模型的阶段采用自身有约束类型，事实载荷沿对应数据库定义。

| 类型 | 字段或含义 |
| --- | --- |
| `CaptureDefinition / CaptureContext` | 动作类型、原生效参数、固定完成方式/stop_supported/必要余量、原绑定和本次配置；由首次受理定义恢复。 构造输入 CaptureInput 由本模块拥有，受理模块调用构造器。 |
| `RecordingState / RecordingDecision` | 启动事实、计时、停止/查询责任、文件归属、适用检查/修复及结果；分支采用录像专题。 |
| `PhotoState / TimelapseState` | 各自调用、等待、结果核实及设备占用事实；Timelapse 含可靠发送 UTC、预计检查时间及本次单调剩余等待。 |
| `CaptureFileSet / CaptureAssessment` | 分别保存归属、单文件完成、集合齐备和必要检查；结果为满足、明确不满足、尚未齐备、未知或错误。 |
| `CaptureRepository / CaptureResult` | 完整开始、等待、结果及最终登记端口；返回已保存事实和后续通知目标。 |
| `EmergencyRecord / MediaDecision` | 应急目标、本会话独立预算、实际尝试及最终停止证据；内部媒体决定含原片、检查结果、目标和处理范围，保存后固定。 |

先处理持久化错误、原终态和取消，再按能力决定等待与完成。

| 固定完成方式及返回 | 执行或完成依据 |
| --- | --- |
| 原生调用在任务完成后返回 | 异步等原调用，成功后不再等待完整目标时长；文件保证另核对。 |
| 只确认发送，设备自行结束且采用时间与产物判定 | 发送成功立即取锚点，等目标时长及适用余量，再核实完整产物。 |
| 设备自行结束且采用状态查询完成方式 | 按声明和既定查询用途核实；不为所有任务强制建立查询。 |
| 需要主机控制结束 | 业务流程负责计时和停止，驱动只执行明确操作。 |
| 发送或结果未知 | 保留未知并按原责任恢复，不能补造启动、结束或文件完成。 |

取消资格由未启动/可能启动/已启动/终态、既有取消和固定停止能力共同决定，采用 N2 的完整模型。占用释放统一核对实际调用、活动依据、文件归属及输出范围限制，不以动作终态直接释放。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/capture/models.py` | 各拍摄类型自身的事实与决定。 |
| `apps/camctl/src/camctl/capture/handlers.py` | 动作类型路由及公共处理端口。 |
| `apps/camctl/src/camctl/capture/recording.py` | 录像启动、计时与停止。 |
| `apps/camctl/src/camctl/capture/photo.py` | 单张拍摄用例。 |
| `apps/camctl/src/camctl/capture/timelapse.py` | 延时摄影能力分区及等待恢复。 |
| `apps/camctl/src/camctl/capture/results.py` | 文件归属、完整集合和结果核实。 |
| `apps/camctl/src/camctl/capture/recovery.py` | 有限安全收场和应急最终补记。 |
| `apps/camctl/src/camctl/capture/media.py` | 原片检查及修复决定。 |
| `apps/camctl/src/camctl/capture/ports.py` | 采集窄仓储与消费者接口。 |
| `apps/camctl/src/camctl/persistence/repositories/capture.py` | 采集事实、占用、正式产物和父状态原子操作。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [操作与调度](2026-09-30-camctl-operations.md) | O1—O5、Q4/Q5 提供真实调用、资格和资源。 |
| [设备](2026-09-30-camctl-devices.md) | D1/D2/D4 的能力、证据及原绑定。 |
| [产物](2026-09-30-camctl-outputs.md) | X1 提供正式登记规则；C8 等待 X4—X6 共用拷贝完成。 |
| [文件与取消](2026-09-30-camctl-host-files.md) | F6 执行媒体处理；N2/N3 定义取消资格和独立责任。 |
| [历史与报告](2026-09-30-camctl-history.md) | 每个 C 任务同步接入 H1—H3，R3 验证相应公开结果。 |

C1—C3、C6 的录像分支及 C7 随首个设备副作用一起交付；C4/C5 扩展照片与延时摄影并同步补 C6/C7。C8 依赖共用拷贝和媒体工具，属于后续内部处理阶段。C9 完成全部能力及历史组合，C3 不把异常处理尚未完成的录像提前判成功。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 保存启动事实或日志不推迟录像锚点。 | C2，`test_recording_anchor_precedes_persistence` |
| 发送成功不能被报告为设备直接完成。 | C5，`test_send_only_has_no_device_completion` |
| 多文件归属、写完与集合齐备不互相替代。 | C6，`test_written_file_does_not_complete_set` |
| 数据库失效时应急最终补记不重发停止。 | C7，`test_emergency_recording_does_not_repeat_stop` |
| 修复成功不自动代表原录像动作成功。 | C8，`test_repair_success_does_not_replace_capture_result` |

## 实施任务

### C1 专属定义及能力路由

**预计文件：** `apps/camctl/src/camctl/capture/models.py`、`apps/camctl/src/camctl/capture/handlers.py`、`apps/camctl/src/camctl/capture/ports.py`；测试为 `apps/camctl/tests/unit/capture/test_definitions.py`。

**接口与依赖：** 提供 `build_capture_definition(action: CaptureInput, capability: ParameterDefinition) -> CaptureDefinition`、`capture_handler(action_type: ActionType) -> ActionHandler`；CaptureInput 由本模块拥有，包含已校验的动作参数，A3 调用定义构造器。前置交付：D1/D2、K1/K2；定义先提供给 A3，运行处理器随后接入。

- [x] 编写失败用例。在 `test_capability_routes_own_completion` 中三种动作及各完成声明路由到对应流程，`assert chosen_mode == declared_mode`；未支持类型不能默认录像。target_duration_ms 精确且 stop_supported/必要余量组合完整，受理失败定义不得保存。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_definitions.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按动作自身定义拆分类型和处理器，全部定义由首次受理事实恢复；驱动默认值变化不影响旧动作。
- [x] 再运行上述命令，要求全部 PASS，并核对 公共管理模型设备无关，专属参数仅相应模块解释。
- [x] 审阅实际接口、状态分区及失败路径，检查 受理、执行和恢复是否重新计算固定定义；记录门禁证据，建议以“feat: 定义拍摄执行与能力路由”形成独立提交。

### C2 录像启动及计时锚点

**预计文件：** `apps/camctl/src/camctl/capture/recording.py`；测试为 `apps/camctl/tests/unit/capture/test_recording_start.py` 和 `apps/camctl/tests/integration/capture/test_recording_start.py`。

**接口与依赖：** 提供异步 `start_recording(context: CaptureContext) -> CaptureStep`、纯 `recording_stop_target(anchor: MonotonicInstant, duration: DurationMillis) -> MonotonicInstant`；CaptureStep 表达已保存事实和下步责任。前置交付：Q4、O2/O3、C1。

- [x] 编写失败用例。建立 `test_recording_anchor_precedes_persistence`，驱动可靠启动响应时钟为 t，数据库和日志随后延迟，`assert stop_target == t + duration`；意图与派发两次窗口检查，窗口内派发窗口后确认仍接受；仅发送、拒绝无效果、未知及可靠启动后调用错误分别保留。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_recording_start.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。首次授予使用 Q4 的完整事务，驱动确认时立即取锚点并保存原依据，不在查询源文件后重新计时；有限启动与核实按原身份、次数和窗口推进。
- [x] 再运行上述命令，要求全部 PASS，并核对 正常录像不主动少录，启动确认不自动提供源文件身份。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_recording_start.py -q`，真实调度、仓储和驱动替身在各启动边界中断，报告保留原事实。
- [x] 审阅实际接口、状态分区及失败路径，检查 启动、查询及迟到成功是否错误重建锚点或重开已终态；记录门禁证据，建议以“feat: 实现录像启动与可靠计时”形成独立提交。审阅结论（2026-10-07）：锚点在驱动确认接收时取得单调钟读数并由装配层登记，持久化与源文件查询延迟不移动锚点（`test_recording_anchor_precedes_persistence`）；查询流程独立计数、不为确认启动而查询（`integration/operations/test_queries.py`）；未登记锚点的动作按跨会话进入对账分区，不重建计时（`test_recording_reconcile.py`）；迟到成功不重开终态（`test_late_result_does_not_overwrite_terminal_state` 与 `test_history_bytes_stable_after_terminal_redispatch`）。

### C3 录像停止、结果登记与异常恢复

**预计文件：** `apps/camctl/src/camctl/capture/recording.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`；测试为 `apps/camctl/tests/unit/capture/test_recording_finish.py` 和 `apps/camctl/tests/integration/capture/test_recording_finish.py`。

**接口与依赖：** 提供 `decide_recording_next(state: RecordingState, facts: RecordingFacts) -> RecordingDecision`、异步 `finish_capture(command: FinishCapture, key: OperationKey) -> DbOutcome[CaptureResult]`；FinishCapture 含可靠最终事实、全部适用产物及提升。前置交付：C2、O4/O5、X1、P3；先提供 MediaProcessingPort，C8 随后接入异常媒体分支。

- [x] 编写失败用例。在 `test_stop_uses_original_budget_across_cancel_and_restart` 中普通、取消及恢复入口共用原停止次数，`assert stop_count == expected_accumulated_count`；停止成功和文件完成保证分别核对。调用尚未结束保持资源，正式产物登记与终态任一写入失败整组回滚，重启不使用旧进程单调值继续计时。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_recording_finish.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按录像成功标准、取消和恢复模型决定停止及核实，保留原片事实；正常不强制裁剪，异常需处理时保留未完成。可靠产物、适用提升及父计划与动作结果同事务登记。登记关联、文件提升及原键恢复按[正式产物登记计划](2026-10-03-camctl-output-registration-review.md)收齐证据。
- [x] 再运行上述命令，要求全部 PASS，并核对 停止、活动结束、产物和动作结果不混同，原终态不改写。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_recording_finish.py -q`，真实 SQLite、调度、报告与录像替身完成正常、停止耗尽、窗口后确认及恢复链。
- [x] 审阅实际接口、状态分区及失败路径，检查 正常停止、取消、重启及后续残留收场是否刷新预算或漏占用；记录门禁证据，建议以“feat: 实现录像结束与正式登记”形成独立提交。

### C4 单张拍摄独立流程

**预计文件：** `apps/camctl/src/camctl/capture/photo.py`；测试为 `apps/camctl/tests/unit/capture/test_photo.py` 和 `apps/camctl/tests/integration/capture/test_photo.py`。

**接口与依赖：** 提供异步 `run_photo(context: CaptureContext) -> CaptureStep`、`decide_photo(state: PhotoState, result: CaptureAssessment) -> PhotoDecision`；PhotoDecision 采用该任务的完成声明。前置交付：C1、O2/O4、C6 结果端口。

- [x] 编写失败用例。在 `test_photo_uses_declared_completion` 中完成后返回和只发送两种契约分别执行，`assert observed_completion == supplied_evidence`；单张任务不默认录像计时/停止。未启动取消、可能启动无停止拒绝、已有合法完成文件在取消后保留。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_photo.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。只实现相应照片任务规则，核实归属和必要文件条件；查询与停止仅在能力声明需要时调用，多文件产物沿 C6/X1 正式登记。
- [x] 再运行上述命令，要求全部 PASS，并核对 照片不被录像流程的默认假设决定结果。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_photo.py -q`，真实仓储、调度和报告组合照片正常、失败、取消与重启，设备用契约替身。
- [x] 审阅实际接口、状态分区及失败路径，检查 照片结果、文件检查和取消是否被统一录像分支覆盖；记录门禁证据，建议以“feat: 实现单张拍摄流程”形成独立提交。

### C5 延时摄影等待及跨重启恢复

**预计文件：** `apps/camctl/src/camctl/capture/timelapse.py`；测试为 `apps/camctl/tests/unit/capture/test_timelapse.py` 和 `apps/camctl/tests/integration/capture/test_timelapse.py`。

**接口与依赖：** 提供 `plan_capture_wait(state: TimelapseState, config: CaptureWaitConfig, now: ClockReading) -> WaitPlan`、异步 `run_timelapse(context: CaptureContext) -> CaptureStep`；WaitPlan 含原发送 UTC、预计检查时间和本次单调截止。前置交付：C1、O2/O4、C6；固定完成方式及 stop_supported。

- [x] 编写失败用例。建立 `test_send_only_has_no_device_completion`，发送成功，`assert device_state_is_observed_ended is False`；锚点取得后 DB 延迟不推迟预计检查。无查询重启计算剩余等待、默认额外等待 0、本次配置变化、原生完成后返回不再等全时长、主机负责结束四类分别验证。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_timelapse.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。分别实现原生任务、发送后等待、状态完成和主机结束路径；驱动必要余量首次固定，部署额外等待采用本次值并保存实际依据。跨重启日期时间用于剩余等待，本次进程使用单调钟，原活动限制仍保留。
- [x] 再运行上述命令，要求全部 PASS，并核对 无查询合法路径不创建查询预算，首次核实前不周期查询状态。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_timelapse.py -q`，在发送、等待登记、复检和完整结果事务各边界中断并改变本次配置，验证旧报告不变。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有完成方式和等待恢复是否补造直接设备观察；记录门禁证据，建议以“feat: 实现延时摄影等待与恢复”形成独立提交。

### C6 文件归属、集合核实及占用释放

**预计文件：** `apps/camctl/src/camctl/capture/results.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`；测试为 `apps/camctl/tests/unit/capture/test_results.py` 和 `apps/camctl/tests/integration/capture/test_results.py`。

**接口与依赖：** 提供 `assess_capture_files(files: CaptureFileSet, requirements: ProductRequirements) -> CaptureAssessment`、`decide_release(state: ActivityFacts) -> ReleaseDecision`；ProductRequirements 及 ActivityFacts 来自对应驱动声明和持久化事实。前置交付：D2、O4、X1、Q4。

- [x] 编写失败用例。建立 `test_written_file_does_not_complete_set`，一份文件已写完但集合未齐，`assert assessment.is_complete is False`；合法空、缺必需类型、明确不满足、暂未齐、读取错误、轮次耗尽分别处理。ENDED+HELD 不表示仍拍摄；文件归属未定同范围新拍摄不得放行。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_results.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。分别保存归属、文件完成、集合及必要检查依据，一轮分页只用一个核实次数；统一按 O 系列模型释放占用。预览关系依据明确配对事实，不按目录顺序。
- [x] 再运行上述命令，要求全部 PASS，并核对 每种事实与必要证据独立保存，缺任一条件不得登记成功或释放。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_results.py -q`，真实文件历史、产物登记、占用事务及三种能力替身，完成 Q/O 系列适用验收。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部正常、停止、取消、无效果、恢复、残留和应急释放入口；记录门禁证据，建议以“feat: 实现拍摄结果与占用判定”形成独立提交。

### C7 有限安全收场与应急最终补记

**预计文件：** `apps/camctl/src/camctl/capture/recovery.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`；测试为 `apps/camctl/tests/unit/capture/test_emergency.py` 和 `apps/camctl/tests/integration/capture/test_emergency.py`。

**接口与依赖：** 提供 `emergency_eligibility(facts: EmergencyFacts) -> EmergencyDecision`、异步 `emergency_stop(scope: EmergencyScope) -> EmergencyRecord`、`save_emergency(record: EmergencyRecord, key: OperationKey) -> DbOutcome[EmergencySave]`；scope 限可靠原目标及本会话预算。前置交付：S3/S5、O3、P4、C6；收尾结果随后由 S6 消费，不得通过 submit 调用。

- [x] 编写失败用例。建立 `test_emergency_recording_does_not_repeat_stop`，最终补记提交未知核实后 `assert extra_stop_calls == 0`。零尝试已知/未知配置、有尝试已知配置分别按正式组合保存；有尝试未知上限、超限、缺项、普通意图被省略、进行中补记均拒绝。错误重复不刷新本会话额度。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_emergency.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。仅在可靠归属、排他资格、安全重复停止能力及可运行条件成立时应急；目标结束且调用收场后一次补记最终流程、全部尝试和观察。记录保存 not_recorded/recorded/unknown 与实际停止结果分开，不等待数据库无限恢复。
- [x] 再运行上述命令，要求全部 PASS，并核对 S-01—S-07 全部分区有可证伪用例，应急例外不进入普通调用。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_emergency.py -q`，真实数据库失效/恢复与受约束录像替身，按补记前后 H 重建并验证旧动作终态保持。
- [x] 审阅实际接口、状态分区及失败路径，检查 受限、普通错误、报告失败及日志失败是否错误取得应急资格；记录门禁证据，建议以“feat: 实现录像有限收场与最终补记”形成独立提交。

### C8 异常原片检查及内部修复

**预计文件：** `apps/camctl/src/camctl/capture/media.py`；测试为 `apps/camctl/tests/unit/capture/test_media_processing.py` 和 `apps/camctl/tests/integration/capture/test_media_processing.py`。

**接口与依赖：** 提供 `decide_media_processing(facts: RecordingEvidence, config: MediaPolicy) -> MediaDecision`、异步 `process_recording(decision: MediaDecision, copies: CopyService, files: MediaFiles) -> MediaResult`；CopyService 采用 X4—X6，MediaFiles 采用 F6。前置交付：C3/C6、X4—X6、F6、X11。

- [x] 编写失败用例。建立 `test_repair_success_does_not_replace_capture_result`，修复成品合格但采集依据不满足，`assert capture_success is False`。按多录门槛及计时证据完整表测试恰好门槛、超过、缺证据、原片长度不足、空间错误及修复失败；内部检查不默认验证所有媒体内容。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_media_processing.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。复用正式读取资格及原片拷贝，保存固定处理决定后执行 ffprobe/ffmpeg；原片、输入副本、临时输出与成品身份分开，全部适用校验及提升与最终登记闭合。
- [x] 再运行上述命令，要求全部 PASS，并核对 可靠原片不丢失，未验证临时文件不能成为正式产物。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_media_processing.py -q`，真实最小媒体、SQLite、文件提升及历史报告，覆盖工具结束到登记和清理各中断边界。
- [x] 审阅实际接口、状态分区及失败路径，检查 内部读写是否绕过统一拷贝、占用、取消及文件生命周期；记录门禁证据，建议以“feat: 实现异常录像检查与修复”形成独立提交。

#### C8 第一段的阶段性验证（2026-10-04）

第一段完成[录像内部处理](../../camctl/database/operation-fields.md#录像内部处理)的持久化骨架，任务 checkbox 保持未勾：`capture/processing.py` 提供六个事务命令类型、公共 media 观察与检查时长三分区（`classify_check_duration`：严格短于目标、区间含端点、严格超门槛），`repositories/capture.py` 提供 RECORDING_DECIDED（检查决定/修复决定/原片关联）与 RECORDING_PROCESSED（检查结果/修复结果/取消收场）六个事务，原键重送恢复首次响应。processing 守卫补齐处理状态分支并单独注册 `recording_source`（登记声明的具名守卫）。

| 关键裁决 | 内容 |
| --- | --- |
| 决定与依据配对固定 | REQUIRED 检查决定只配 INSUFFICIENT_TIMING 依据；NOT_NEEDED 配连续控制完成或异常多录判定；PENDING 修复决定只配 THRESHOLD_REACHED，NOT_NEEDED 配 BELOW_THRESHOLD 或 NO_USABLE_INPUT。类型层固定配对，仓储与守卫复核。 |
| 阶段转换 | 修复成功只能自 RUNNING 进入（3→5 无登记转换）；PENDING 可直接失败或取消。检查 FAILED 与 UNCONFIRMED 为终态。取消收场进度按 1→2、{2,3}→{3,4,5,6} 推进。 |
| 媒体观察与阶段一致 | COMPLETED 必须携带可靠时长且无错误；FAILED/UNCONFIRMED 必须携带协议 error 结构；RUNNING 不携带结论（对协议 media allOf 的实现收严）。时长经精确 JSON 编码保持全精度。 |
| 可靠原片与修复输出 | 原片关联核对角色 ORIGINAL、写完 COMPLETE 且 source_action_id 归属本动作；修复成功核对 REPAIR_OUTPUT 用途、归属与完整字节事实（size/sha256 非空）。 |
| 幂等与冲突 | 原键重送恢复 ALREADY 只读；事实时刻不同按操作身份冲突拒绝；已固定决定不因重送或配置变化重算。 |

验证：`test_media_processing.py` 单元 41 项、集成 24 项（正常链、逐命令分区、非法前提与转换回滚、原键恢复、守卫接受真实事件并拒绝媒体结构不一致、依据缺门槛、成品缺登记）；全量回归单元 2775、集成 2935+7 skip、根 34+342、node 111、文档链接 2907 均通过（Python 3.11）。`check-event-transitions.mjs` 的 history-formats.md 区段待同步为 HEAD 既有问题，与本段无关。

第二段剩余：原片检查拷贝复用 X4—X6 读取资格与拷贝流程、probe/repair 受管执行接入 `MediaObservation` 与修复决定、修复成品提升及与 `finish_capture` 终态同事务整合、DISCARD 与中间文件清理的衔接、动作错误码消费（`recording_too_short`、`recording_processing_failed`）。M2 的可靠视频时长来源仍阻塞在设备适配证据。

#### C8 第二段的阶段性验证（2026-10-05）

第二段完成修复成品提升与 `finish_capture` 终态的同事务整合（对应媒体计划 M4b 与登记计划 R3），任务 checkbox 保持未勾：`FinishCaptureCommand` 对 REPAIRED 草稿核对修复资格后同事务保存 `INTERMEDIATE_FILE_CHANGED.LIFECYCLE`（保留状态 1→3），`output` 守卫补齐提升配对分支并复核登记资格；产物元信息从占位改为实际文件事实。关键裁决与验证见[登记计划 R3 验证记录](2026-10-03-camctl-output-registration-review.md#r3-验证记录)。

第二段仍剩余：原片检查拷贝复用 X4—X6 读取资格与拷贝流程、probe/repair 受管执行接入 `MediaObservation` 与修复决定、DISCARD 与中间文件清理的衔接、动作错误码消费（`recording_too_short`、`recording_processing_failed`）。M2 的可靠视频时长来源仍阻塞在设备适配证据。

#### C8 第二段的执行编排验证（2026-10-05）

第二段完成检查与修复的受管执行编排接入，任务 checkbox 保持未勾：`capture/media.py` 提供 `check_observation_from_probe`、`repair_decision_from_check`、`repair_error_from_artifact` 换算规则与 `execute_check`/`execute_repair` 编排（`MediaTools`/`ProcessingSaves` 双端口，适配层持有执行器、任务身份、连接与操作键）；仓储新增 `start_repair_output`（修复输出路径登记与 PENDING→RUNNING 同事务）和 `complete_repair_output`（完整字节与 RUNNING→SUCCEEDED 同事务）；`processing.py` 新增 `RepairStart`/`RepairSuccess` 事务输入与 `saved_check_duration`/`saved_target_duration_ms` 解码。

| 关键裁决 | 内容 |
| --- | --- |
| 检查终态三分 | probe 工具错误（tool_failed/tool_unavailable/tool_cancelled/output_failed）按检查 FAILED 终态保存；工具正常结束但未取得可靠时长（missing_duration/invalid_structure）按 UNCONFIRMED 终态保存，时长判定保持未知；取得时长才 COMPLETED。分类码取诊断前缀，完整消息保留在错误 details。 |
| 检查失败不派生修复决定 | 检查 FAILED/UNCONFIRMED 后 repair_state 保持 UNDETERMINED，不猜测未超门槛；与产物侧内部输入需求的 `input_need_ended` 分类一致。恢复入口只在检查完成且决定未固定时，按已保存 media_json 与 check_basis_json 补固定决定。 |
| 修复输出身份先登记后写入 | 启动事务分配中间文件身份并登记 `derived/` 正式路径（retention REQUIRED、无字节事实）与 PENDING→RUNNING；`repair_output_file_id` 只由成功事务固定。恢复入口（RUNNING）按归属动作与 REPAIR_OUTPUT 用途查唯一登记行，多个登记属于不可解释状态。 |
| 字节与成功同事务 | 完成事务先保存 INTERMEDIATE_FILE_CHANGED.LIFECYCLE（size+sha256），再保存 RECORDING_PROCESSED 修复分支（SUCCEEDED + repair_output_file_id）；守卫从同事务先行事件复核用途、归属与完整字节，与提升配对共用同一可见性机制。 |
| 编排失败分区 | 保存被拒或未知、工具任务未取得观察（撤回或执行体错误）、修复成品不完整各自成相：意图保存未确认不启动工具，任务未取得观察不保存终态结论，不完整成品保存 FAILED 终态与结构化错误（工具分类码或 `artifact_incomplete`），责任留给下一次执行。 |

验证：`test_media_execution.py` 单元 60 项（换算三分、恢复入口、逐保存失败分区、端口调用次序）、集成 11 项（真实 SQLite 与真实受管子进程：检查链完成与失败、检查完成后恢复补决定、修复登记-执行-字节-成功链、启动提交后恢复续执行、失败错误结构、命令原键重用与非法前提整组回滚）；全量回归单元 2853、集成 2957+7 skip、根 34+342、check-protocol、check-report-dependencies、check-doc-links 2912 通过（Python 3.11）。check-event-transitions 的 history-formats.md 区段仍为既有待同步问题，本轮未新增事件类型。

第二段仍剩余：原片检查拷贝复用 X4—X6 读取资格与拷贝流程（编排调用资格申请与分段拷贝）、DISCARD 与中间文件清理的衔接、动作错误码消费（`recording_too_short`、`recording_processing_failed`）。M2 的可靠视频时长来源仍阻塞在设备适配证据；编排消费 probe 现有结果，不改变时长来源的可靠性边界。

#### C8 第二段的输入取得编排验证（2026-10-05）

第二段完成原片检查拷贝复用（X4—X6 读取资格与拷贝流程的编排接入），任务 checkbox 保持未勾：`capture/input_copy.py` 提供 `obtain_recording_input` 编排与端口（`RecordingCopies` 组合资格申请、状态装载、续传准备、分段推进与完整性收尾；`ReadSessionOpener`/`RecordingSource` 承载设备读取会话）。资格申请使用内部输入候选（processing_id + 设备源），建档或复用既有准备记录由资格事务统一裁决。

| 关键裁决 | 内容 |
| --- | --- |
| 就绪重入不重拷 | 就绪判定 = 校验 MATCHED/SOURCE_CHECKSUM_UNAVAILABLE 且目标字节事实已保存；重入直接返回就绪引用，不重开设备会话、不推进段。 |
| 续传位置由准备决定 | 会话按准备决定的偏移打开：重拷重置后归零、续传取已确认进度、字节齐备只差收尾（VERIFY）不开会话直接完成。 |
| 会话生命周期 | 段循环结束无论成败都请求停止并等待实际结束；段保存 SKIPPED（发起责任取消或不在执行）停止推进并透出原因，不为待清理副本继续读取。 |
| 不一致不就地重试 | 完整性收尾登记新一轮重拷后本次返回待重拷，下一次执行经重置从零重读；完成事务优先采用已保存源摘要（跨轮一致），源摘要读取端口只在没有保存值时使用。 |
| 编排边界 | 读取尝试预算与相机读取机会由意图入口管理，本编排不代替；资格等待与最终失败的原因原样透传，各失败分区不在本次执行内重试。 |

验证：`test_input_copy.py` 单元 17 项（端口替身：重入、续传位置、跳过与失败分区、端口调用次序与会话收场）、集成 7 项（真实 SQLite、真实资格/分段/完成事务与真实 ReadSession：完整链与重入、VERIFY 重入不开会话、等待两因、段失败按已确认进度续传、损坏字节登记重拷后次轮重读成功、发起责任取消跳过不提交进度）；全量回归单元 2870、集成 2964+7 skip、根 34+342、check-protocol、check-report-dependencies、check-doc-links 2913 通过（Python 3.11）。

第二段仍剩余：DISCARD 与中间文件清理的衔接、动作错误码消费（`recording_too_short`、`recording_processing_failed`）。设备读取会话工厂（D4 绑定）与读取尝试纪律随调度接线接入；M2 的可靠视频时长来源仍阻塞在设备适配证据。

#### C8 完成验证（2026-10-05）

最后一组完成 DISCARD 衔接与动作错误码消费，C8 任务收口（各 checkbox 依据下述证据勾选）：

- `decide_recording_result`（`capture/media.py`）按录像成功标准判定动作结果：时长不足与明确媒体错误优先按失败处理（`recording_too_short`、`recording_processing_failed` 的 `invalid_media`），控制完成与时长检查是独立成功依据（修复失败不否定、修复成功不替代），必要处理未结束保持待定，检查工具失败不单独否定控制依据，无可用输入按 `source_unavailable` 失败——空间不足经拷贝写入失败分区快速失败后即落入该判定。
- `FinishCapture` 增加可选 `failure`：携带时按 `ACTION_FINISHED.FAIL` 保存执行失败终态（公共动作错误编号与协议 details 先经登记校验），产物登记、失败事实与父计划完成同事务提交；取消请求已生效的动作拒绝失败终态。
- `execute_discard`（`capture/media.py`）衔接 X11：取消决定已保存（PENDING）后先保存运行阶段，再对动作归属中间文件逐项定向清理（释放保留、删除、保存结果），任一失败保存失败终态与错误结构（携带文件身份），单项失败不中断其余；执行中恢复入口沿用既有进度。
- 动作归属清理失败错误码 `action_work_file_delete_failed`（详情携带文件身份）登记入公共错误清单，X11 的定向与历史清理对两种归属都可靠保存失败结果，未决责任留给后续运行。

验证：`test_recording_result.py` 单元 19 项（决策表全分区：待定、失败证据优先、双成功依据、修复成败不影响判定、空间不足落位）、`test_discard.py` 单元 13 项（入口状态、意图先行、失败分区、恢复）与集成 7 项（真实清理事务与真实文件：释放删除、Windows 打开句柄触发删除失败并保存登记错误详情、执行中恢复、失败终态错误编号 16/15 与 details、结构不符整组拒绝、取消动作拒绝失败终态）；`test_work_files.py` 动作归属失败用例改为保存登记错误。全量回归单元 2901、集成 2971+7 skip、根 34+342、check-protocol（含新错误码）、check-report-dependencies、check-doc-links 2913 通过（Python 3.11）。

C8 边界：设备读取会话工厂（D4 绑定）、读取尝试纪律与取消动作的完整收场接线归 C9/I5 及调度接入；完成事务原键重送归登记计划 R4；M2 的可靠视频时长来源仍阻塞在设备适配证据。

### C9 三种能力的完整链验收

**预计文件：** `apps/camctl/src/camctl/capture/handlers.py`、`apps/camctl/src/camctl/capture/recovery.py`；测试为 `apps/camctl/tests/integration/capture/test_capture_contract.py`。

**接口与依赖：** 使用三种真实处理器、仓储、历史和报告接口；设备侧保持契约替身。前置交付：C1—C8、Q6、R8、N1—N5、X1—X11；不依赖其他模块的最终验收任务。

- [x] 编写失败用例。在 `test_capture_facts_survive_all_recovery_paths` 中各类型每个副作用边界中断，`assert current_result == expected_result` 且固定 H 报告字节不变；改变查询/停止/完成声明，公共调度仍能组合。已终态源动作后取回或清理仍取得原绑定。（实施形态调整为散布覆盖：见文末 C9 验收映射档案的实施形态说明。）
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_capture_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。（各链红—绿证据在十五个分段验证记录中逐段落档。）
- [x] 实施本任务。补齐 camera-verification、camera-capture 验收及适用 R/Q/S/O 条目映射，核验采集→正式产物→取回→报告整链。（映射档案见文末；照片与延时整链已由 I5 第一链跨组件核验，录像与清理跨组件用例归 I5 剩余两项。）
- [x] 再运行上述命令，要求全部 PASS，并核对 没有把某个真实相机保证强加到其他能力，全部失败/恢复有归属。（能力未声明的设备不推进拍摄——C9 第十五段；各能力按自身确认契约处理——C4/C5 审阅行；开放边界见映射档案清单。）
- [x] 审阅实际接口、状态分区及失败路径，检查 三种流程的未知、预算、占用、终态及迟到结果同类风险；记录门禁证据，建议以“test: 验证全部拍摄能力闭环”形成独立提交。（同类风险的系统核对即映射档案的 R/Q/S/O 逐条结论：迟到结果 R-01/R-13/R-14、预算 Q-03/Q-09/Q-10、占用 O-01—O-06、终态 Q-02/Q-04。）

#### C9 第一段的阶段性验证（2026-10-05）：设备文件观察登记链

三种能力执行链的共同地基先行落地：驱动结果列举的稳定文件身份
可靠落库，供集合核实（C6）与完成登记（X1）消费。`capture/files.py`
提供登记输入与 `[设备, 驱动, 文件身份]` 的确定 JSON 身份键编码；
`CaptureRepository` 新增 `save_file_observation`（CREATE：观察者动作
必须属于拍摄类型并携带固定绑定，拍摄类型的设备与驱动绑定由表约
束物理保证；重复发现复用原行并核对定位，定位矛盾保留原依据）、
`save_file_ownership`（OWNERSHIP：来源只能从未知一次确认，预览配
对原片必须已确认归属且属于同一来源任务，配对证据固定为驱动配对
方法）与 `save_file_completion`（COMPLETE：按登记转换表 1→{2,3,4}、
2→{3,4}、4→3 推进，已完成不倒退；COMPLETE 携带非负完整大小与完成
依据，TIME_AND_OUTPUTS 依据核对 `device_activities.wait_completed_
event_id` 引用；UNCONFIRMED 携带实际失败证据；可靠观察可清除此前
错误）。三个命令的原键重送各自核实分支、时刻与输入后恢复首次响
应。正式 `device_file` 守卫按事件前事实核对全部五个分支（含无生产
命令的 CHECKSUM：支持声明只能从未判定一次决定、摘要要求已完成且
受支持、失败不能改成不支持；PRESENCE：必须是实际状态变化，与清理
成功分别保存），并随 `register_capture_guards` 注册；公开投影路由
需要的产物行由命令装配入 state_rows。

验证：`test_file_inputs.py` 单元 35 项（身份键编码、归属与配对证据
结构、形成状态分区输入规则）；`test_file_observation.py` 集成 22 项
（真实 SQLite 与事务内核：三命令正常链、定位矛盾与来源改指拒绝、
转换表分区、TIME_AND_OUTPUTS 引用核对、原键重送、写入失败回滚重
试、守卫 CHECKSUM/PRESENCE 直接事件）。既有 `test_events.py` 的"未
接入具名校验拒绝写入"改用尚未实现的 `window` 守卫（原以 device_file
为例的前提已不成立）。全量回归与 node 检查通过（Python 3.11）。

C9 后续分段：第二段三能力处理器接线（grant→设备调用→观察登记→
C6 核实→终态登记）、第三段录像媒体链调用方与 D4 读取会话工厂绑
定、第四段调度接线（Q6）与 `test_capture_contract.py` 总验收（含
R/Q/S/O 条目映射与报告字节对照）。设备观察与资格授予事件的同事务
组合验收随第二段推进（见[文件资格计划](2026-10-02-camctl-file-
qualification-review.md#首次建档资格与事件顺序的阶段验证)）。


#### C9 第二段的阶段性验证（2026-10-05）：三能力处理器执行链

`capture/handlers.py` 的三个 `NotImplementedError` 占位替换为真实
执行链。`CaptureRuntime` 装配真实授予（grant_start）、尝试结果
（finish_attempt）、文件观察（第一段三命令）、终态（finish_capture）
与等待安排（schedule_wait）仓储，并注入设备控制、结果列举、墙钟、
单调钟、启动窗口、延时等待配置与录像中段状态端口（契约替身与真
实驱动同形）。共享尾段把结果列举观察落库（发现 → TASK_SCOPE 任务
归属 → DEVICE_GUARANTEE 完成），经 C6 `assess_capture_files` 核实
集合，可判定时保存终态与正式产物；失败保留完整且归属明确的文件。

| 处理器 | 推进序列与分区 |
| --- | --- |
| 照片 | 无尝试时授予并调用 `take_photo`（`photo_taken` 确认）；调用错误同样保存尝试并进入判定——`FAILED_KEEP_FILES` 以 `capture_failed`（`device_failed`，详情携带活动身份）失败终态并保留文件；结果未齐只读等待，再次推进不重复调用；终态后幂等。 |
| 录像 | 无启动尝试时授予并调用 `start_recording`（不依赖中段端口）；有尝试后经中段状态端口 `decide_recording_next`——停止确认与文件完成分区进入尾段，装载处理行后 `decide_recording_result` 判定终态；无处理责任或处理未结束时等待媒体链；等待计时、停止推进与跨会话对账留给调度接线段。 |
| 延时摄影 | 发送（`timelapse_sent`）后消费活动观察边界保存的 `sent_at`（`DEVICE_OBSERVED` 生产者属活动持久化段，测试按该边界预置），经 `plan_capture_wait` 与真实 `schedule_wait` 安排等待；到达预计检查时间后走共享尾段。 |

关键裁决：观察身份必须等于操作目标（票据目标即动作身份）；同一
计划时间下更早动作阻塞启动授予（`not_first_candidate`），集成测试
按能力单独建立动作；设备错误进入尝试结局的 `ErrorValue`（成功结
局不得携带调用错误）；`expected_check_at` 要求 `sent_at` 与余量同
置（表约束）。`test_capture_contract.py` 的 6 项集成测试覆盖三种
能力正常路径、照片调用失败保留文件、结果未齐→迟到结果恢复、录像
启动后停止确认的续推进、处理未建立保持运行、延时发送-等待-到点
收尾。全量回归与 node 检查通过（Python 3.11）。

C9 剩余分段：第三段录像媒体链调用方与 D4 读取会话工厂绑定、设备
活动观察（`DEVICE_OBSERVED`）与录像中段事实生产者、第四段调度接
线（Q6）与 `test_capture_contract.py` 总验收扩展（各副作用边界中
断、固定 H 报告字节对照、R/Q/S/O 条目映射）。


#### C9 第三段的阶段性验证（2026-10-05）：媒体链调用方与活动观察生产者

`capture/media_flow.py` 提供录像媒体链调用方：`DriverReadSessions`
把 input_copy 的读取会话端口绑定到 D4——按设备文件的完成事实（
COMPLETE 且有完整大小）构造 `SourceFile`（身份键+驱动定位+固定长
度）并经驱动 `open_read` 打开真实可停止会话，未完成文件拒绝读取；
`RecordingInputCopies` 组合真实资格、续传、分段与完整性事务；
`CaptureProcessingSaves` 适配五个处理事务；`load_processing_status`
从当前投影装载编排事实（含修复执行中按归属查登记输出）。`run_
recording_media` 串联输入取得 → 检查执行 → （决定待执行时）修复
执行：无需修复或修复不待执行时在检查步骤收束，各保存被拒/未知分
区原样透传，重入不重复拷贝。

设备活动观察生产者补齐：`CaptureRepository.save_activity_observation`
（DEVICE_OBSERVED.OBSERVE，事件 13）保存 `sent_at`/`started_at`（只
能从空值一次保存）与 `dispatch_state`/`activity_state`（按登记转换
表推进）；活动结束不经本命令补造（须由可靠停止事实承载，由已注册
的 activity 守卫核对）；原键重送核实分支、时刻与输入后恢复首次响
应。三处理器在设备调用成功后接线：发送即保存 `sent_at`，确认启动
另存 `started_at` 与活动进行中；dispatch_state 由授予事务的尝试事
实推进，处理器不再重复保存。第二段测试中的 sent_at 预置移除，延
时链路端到端真实保存。

验证：`test_media_flow.py` 6 项（D4 绑定按完成事实打开/未完成拒绝、
完整输入-检查链（61s 落在目标加余量内→无需修复）并落真实副本与
校验、重入复用副本与检查结论、200s 多录超门槛→修复决定+输出登记+
工具未运行保留运行阶段、状态装载器解码公共 media 结构）；
`test_capture_contract.py` 6 项全部经真实活动观察通过。全量回归与
node 检查通过（Python 3.11）。

C9 剩余分段：第四段调度接线（Q6：处理器挂入调度循环、录像停止推
进与中段状态装载生产化、结果列举的驱动适配）与总验收扩展（各副
作用边界中断、固定 H 报告字节对照、R/Q/S/O 条目映射）。


#### C9 第四段的阶段性验证（2026-10-05）：调度接线与总验收扩展

`capture/dispatch.py` 提供调度侧推进循环（Q6 接线）：`ready_
capture_actions` 从当前投影取可推进的拍摄动作（执行中、未取消、
已到计划时间，按计划与数组顺序），`dispatch_ready` 把就绪描述符
按类型路由到能力处理器——未知类型不默认路由，单动作异常不阻止
其余动作，重复调度经处理器的幂等推进不产生重复副作用。

录像中段状态生产化：`SessionRecordingState`（handlers.py）从
start/stop 责任的最近尝试装载启动确认、停止确认与在途，从停止
流程行装载次数与上限；计时锚点是本进程会话的单调钟读数，由装
配层在启动确认后登记，未登记时按跨会话进入对账分区（规格：旧
读数不与新会话组合计时）。`test_capture_contract.py` 的录像用例
改用真实停止流程行与确认停止尝试，不再依赖端口替身。

总验收扩展：`TestDispatchLoop`（三能力跨计划经调度循环一次推进，
照片直达终态）；`TestInterruptionRecovery`（文件登记边界注入写
入故障→整组回滚保持运行→干净运行时重入补齐终态且不重复调用与
登记；终态后再次调度，历史事件字节完全稳定）。固定 H 报告字节
对照与 R/Q/S/O 条目映射见文末“C9 验收映射档案”；结果列举端口
的生产适配见第十五段，驱动适配归 D5 接入。

验证：`test_capture_contract.py` 9 项；全量单元 2964、集成
3039+7skip、根 34+342、check-protocol、check-report-dependencies、
check-doc-links 2916 通过（Python 3.11）。

C9 剩余的映射终验收已由文末“C9 验收映射档案（2026-10-07）”落
档：报告字节对照经 I5 第一链报告链核验，R/Q/S/O 条目逐条给结论；
结果列举端口的生产适配见第十五段，真实驱动适配消费归 D5 接入。


#### C9 第五段的阶段性验证（2026-10-05）：录像停止链与取消联动

录像到时停止与取消立即停止接入执行链。`decide_recording_next` 增
加取消分支：取消生效后不经计时立即按剩余预算停止，不依赖本会话
锚点；跨会话对账分区仅在未取消时进入。`_record_handler` 在
`READY_TO_STOP` 经停止端口发起 `stop_recording` 调用（stop 责任、
`stop_returned`/`stop_confirmed` 证据契约、共享原停止预算），可靠
确认即结束停止流程并收场设备活动，随后重入尾段；调用错误或未确
认保存结果并建立重试等待。停止确认后的取消不再核验产物：
`finish_canceled_capture` 事务按取消终态收场（动作 `canceled`、计
划完成、不登记正式产物），要求取消标记已保存。活动结束的可靠停
止事实按动作类型选择责任：录像取 `stop/{id}`、照片与延时取
`start/{id}` 的成功终态流程行（启动调用成功不证明录像已停止）。
`ready_capture_actions` 把已取消的执行中录像纳入推进（取消联动派
发），未确认启动的取消不重新启动。录像目标时长改从首次固定的执
行定义读取（`execution_spec_json.target_duration_ms`），缺失或非
法按状态库错误停止；会话锚点表可由装配层跨推进轮共享。

取消动作的逐目标收场修复：取消已生效的成员不再以目标是否终态为
前提跳过结算，一律经结算端口按目标类型推进剩余工作；全部完成后
按目标终态登记完成依据（`canceled` 证明取消达成，其他终态保持原
结果 `already_terminal`）。

验证：`test_recording_finish.py` 集成 7 项（取消终态三事务、停止预
算跨入口累计）；`test_recovery.py` 14 项（收场停止事实按动作类型）；
`test_capture_contract.py` 9 项（停止流程行改终态成功、执行定义种
子按受理约定）；新增 `bootstrap/test_recording_stop.py` 会话级 3 项
（真实 run 会话：锚点到时停止→控制完成加完整结果登记成功终态与正
式产物、停止失败一次后重试成功 `attempts_used == 2` 仍达终态、录制
中取消立即停止→取消终态→取消动作与成员收场）。全量单元 3217、集
成 15 目录与根 34+342、三个 node 检查器通过（Python 3.11）。

C9 后置：sent-only
取消核实随后续轮次接入；停止预算耗尽的残留收场已随[第十七段](#c9-第十七段的阶段性验证2026-10-07停止预算耗尽的残留收场接线)交付；等待中取消的延时收场（停止链与核实终止）
见[第八段](#c9-第八段的阶段性验证2026-10-05等待中取消的延时收场)；
异常录像媒体链接线见[第九段](#c9-第九段的阶段性验证2026-10-06异常录像媒体链接线)；
跨会话对账与恢复决定见[第十段](#c9-第十段的阶段性验证2026-10-06跨会话对账与恢复决定建立)、
媒体链会话级装配见[第十一段](#c9-第十一段的阶段性验证2026-10-06媒体链会话级装配)、
时钟异常会话的保守收场见[第十二段](#c9-第十二段的阶段性验证2026-10-06时钟异常会话的保守收场)。

#### C9 第六段的阶段性验证（2026-10-05）：结果集合核实与延时占用释放

`RESULT_SET_CONFIRMED` 四分支事务落地：BEGIN 只把未核实集合推进
到核实中；满足结论保存驱动规则标识、结构化依据与采集结果，按
`time_and_outputs` 方法判定时间与产物完成时，事务核对固定完成方
式、可靠发送、已保存的等待完成事实与一致的事件引用，采集判定从
`UNDETERMINED` 初始化为 `TIME_AND_OUTPUTS`；明确不满足保存已知失
败；无法确认保存未知采集事实且判定保持待定。`CAPTURE_WAIT_CHANGED`
完成分支保存一次引用自身事件的等待完成事实。`result_check` 守卫
核对分支、判定编码与轮次状态；`activity` 守卫新增采集判定列的适
用范围（录像保持为空）与一次判定不变量。活动创建事务同步修复：
照片与延时的采集判定从 `UNDETERMINED` 开始（此前一律写空值与转
换模型不符）。延时处理器接线完成链：等待到期先保存完成事实，再
按任务范围列举核实集合，满足即保存完成判定并解除占用（设备活动
保持未知，不补造结束观察）后登记成功终态与正式产物；必需类别缺
失保存已知失败并按失败终态收场，占用保持持有等待残留收场。

验证：新增 `test_result_confirmation.py` 集成 15 项（等待完成一次
性与前置、四分支状态与判定、时间与产物事实核对、依据引用一致、
完成方式不符拒绝、已知失败不解除占用、录像拒绝、终态后拒绝、同
键重送、输入契约）；`bootstrap/test_timelapse_finish.py` 会话级 2
项（真实 run 会话：发送后等待到期→等待完成→满足判定→占用释放→
成功终态与产物；必需类别缺失→已知失败终态、占用保持、无产物）；
`test_result_inputs.py` 单元 13 项。全量单元 3230、集成 15 目录与
根 34+342、node 检查器通过（`check-event-transitions.mjs` 的文档
生成区段失败为 HEAD 既有问题，与本轮无关）。

#### C9 第七段的阶段性验证（2026-10-05）：结果核实的轮次与预算编排

延时核实接入 `CHECK_CAPTURE_RESULTS` 责任（`results/<activity_id>`，
配置 `devices.<id>.result_check.*`，处理器端口 `check_config` 默认
3 轮、单轮 10 秒、重试间隔 3 秒）：每轮先提交意图再列举，结论（满
足或明确不满足）与尝试结束、流程收场在同一事务提交，任一侧输入
被拒整组回滚；暂不齐备或列举失败保存本轮结果并建立重试等待，下
一轮作为新尝试累计名额；预算耗尽时没有在途尝试，核实流程与无法
确认的集合结论同事务收场（流程按登记的公共错误结构置
`UNCONFIRMED`），动作以 `capture_result_unconfirmed`（reason
`outputs_unknown`）失败终态收场，占用保持等待残留收场。结论已保
存而动作未收场的中断窗口用原结果直接收尾：满足判定补释放与终
态、已知失败与无法确认按登记错误补终态，不重开核实轮次。轮次收
场依据的证据类型为 `results_returned`（operation `result`），驱动
登记接入时须声明同形契约。

验证：`test_result_confirmation.py` 集成新增 9 项（结论与尝试结
束同事务及事件形态、结论被拒不留半提交、同键重送恢复、已结束尝
试拒绝携带结论、耗尽收场流程与结论同事务、耗尽收场重送、结论保
存后中断的满足与无法确认收尾、中断遗留的在途轮次停等不提交新意
图）；`bootstrap/test_timelapse_finish.py`
会话级新增 3 项（真实 run 会话：文件写完前暂不齐备→第一轮重试等
待→下一轮尝试后结论与终态同链；单轮预算耗尽→无法确认收场→动
作按 `capture_result_unconfirmed` 失败；列举失败一轮→失败尝试加
重试等待→下一轮新尝试后成功）；既有满足、不满足会话
测试与 `test_capture_contract.py` 延时处理器测试在轮次接入后保持
通过。

#### C9 第八段的阶段性验证（2026-10-05）：等待中取消的延时收场

可停止延时（执行定义 `stop_supported: true`）在等待中取消生效后，
`_timelapse_handler` 不再等待计时，立即进入停止收场：停止决策与
录像共用 `decide_recording_next`（取消分支不经计时、共用原停止预
算），经停止端口发出 `stop_timelapse`；启动未确认的取消不重新启
动，归启动核实链；在途停止等待结果，预算耗尽等待残留收场接线。
停止确认后活动以 stop 责任成功流程行收场（等待中的活动状态为未
知，可靠停止事实直接保存 `ENDED` 并释放占用；启动效果不可能存在
的未知活动仍拒绝收场），随后收尾列举一次：已拍完、确认归属且写
入完成的文件与取消终态同事务登记为正式产物，列举失败按完成情况
未知处理不登记。`FinishCanceledCapture` 扩展可选 `drafts` 与
`catalog_facts`，与 `FinishCapture` 共用同一完成登记命令的取消分
支：取消标记必须已保存，未完成草稿整组拒绝，空草稿保持录像放弃
内容的原语义。`ready_capture_actions` 取消联动派发扩展到已取消的
执行中延时。不可停止延时已启动后取消按取消资格拒绝（成员失败
`task_cancel_unsupported`，取消动作 `cancel_items_failed`），目标
取消标记不变，原任务继续等待、核实产物并登记正常结果。

验证：`test_recording_finish.py` 新增 2 项（取消收场登记已拍完文
件与取消终态同事务、同键重送恢复、空草稿新键改写被拒；未完成草
稿整组拒绝）；`test_recovery.py` 新增 3 项（延时 start 责任不作结
束证据、被停止延时经 stop 责任从未知状态收场、启动已生效的未知
活动仍须停止事实），既有未知活动拒绝收场测试按新分区改写；
`bootstrap/test_timelapse_finish.py` 新增 2 项会话级（真实 run 会
话：可停止延时等待中取消→立即 `stop_timelapse`→活动结束释放→
已拍完文件登记为产物→取消动作与成员成功；不可停止延时取消项失
败→目标标记不变→原任务正常完成含产物）。全量单元 3230、集成 15
目录与根 34+342、协议/报告依赖/文档链接与数据库结构检查器通过
（Python 3.11）。


#### C9 第九段的阶段性验证（2026-10-06）：异常录像媒体链接线

录像尾段接入媒体链。停止确认后处理器经 `_advance_recording_outcome`
推进：控制完成（停止确认且文件完成有保证）先固定无需检查决定
（`CheckDecisionSave(NOT_NEEDED, CONTINUOUS_CONTROL_COMPLETE)`，
控制依据即成功依据），行为与既装控制链一致；处理行已固定需要检
查（INSUFFICIENT_TIMING）且检查或修复未终局时，经 `CaptureRuntime.media`
端口驱动 `run_recording_media`：首次把观察到的完整原片归属到处理
行（`SourceFileSave`，媒体读取资格的归属核对以此对齐），拷贝、检
查与修复一次串联推进，随后按已保存事实装载判定输入（检查时长、
媒体问题、输入不可用与修复进度取自处理行投影而非硬编码）。修复
成功的成品与原片同事务登记为 `REPAIRED` 正式产物（
`original_batch_file_id` 关联原片，中间文件保留为修复用途）。

媒体链在真实观察路径上补齐两项读取前提：结果列举确认文件在场
（`FilePresenceSave`，重复发现沿用已保存事实）；`MediaFlow` 携带读
取绑定的驱动摘要能力声明 `digest_supported`，`run_recording_media`
首次把来源文件的摘要能力从未判定一次固定（新增
`FileChecksumSave` 与 `CaptureRepository.save_file_checksum`，
DEVICE_FILE_OBSERVED.CHECKSUM 事件此前只有守卫没有生产写入方）。

分区：需要检查但没有可归属完整原片时按 `source_unavailable` 有
限失败收场（原片事实保留）；媒体端口未装配或处理未终局时保持执
行中等待装配会话，不提前判成功；检查时长不足目标按
`recording_too_short` 失败并保留原片。

验证：`test_recording_media_link.py` 集成 5 项（控制完成固定无需
检查决定并照常登记成功、计时证据不足一次推进拷贝-检查-修复且修
复成品与原片同事务登记（含在场与摘要能力断言）、时长不足失败保
留原片、媒体端口未装配保持运行、无源按 source_unavailable 失败）。
全量单元 3230、集成 15 目录（capture 182）与根 34+342、协议/报告
依赖/文档链接与数据库结构检查器通过（Python 3.11）。


#### C9 第十段的阶段性验证（2026-10-06）：跨会话对账与恢复决定建立

`RECONCILE_REQUIRED` 分支接入生产推进：既往会话已确认启动（锚点随
会话失效）的录像，恢复会话以已保存的启动墙钟与当前可信墙钟对账
（`decide_recording_reconciliation`：恰好到达目标即满足；启动墙钟
缺失为计时不可靠，归时钟异常会话的保守收场，不推测已经录够）。
可信计时证明已满足即按原停止预算停止，尚未满足保持执行中等待剩
余时长，不提前停止也不无界等待。

恢复停止确认后的决定建立经 `decide_recovered_control` 判定：本会
话锚点的正常停止不超门槛，按连续控制完成固定无需检查；恢复停止
以启动墙钟到停止确认墙钟的控制耗时判定，恰好达到目标加余量（含
等号）不触发修复，超过即保存无需检查（EXCESS_DURATION_CHECK，
携带控制耗时）与待执行修复（THRESHOLD_REACHED，门槛与实测时长共
同保存）两决定；计时证据缺失不折叠为连续控制完成，保持未定等待
对账。媒体链对检查不适用的处理行按待执行修复决定直接修复，不经
检查；处理行媒体责任谓词收窄为"待执行检查（仅需要检查时）或待执
行修复"，无需检查且未达门槛的行不产生媒体责任，控制完成即按结
果集合核实收场。

验证：`test_recording_reconcile.py` 集成 3 项（65 秒满足即停止并按
恢复控制完成登记成功；30 秒未满足保持运行且无停止意图，墙钟推进
后对账满足恰达门槛不触发修复；120 秒多录超 70 秒门槛不经检查直接
修复且成品与原片同事务登记）；单元 `test_recording_finish.py` 新
增 7 项（对账三分区与恢复控制四分支）。全量单元 3237、集成 15 目
录（capture 185/bootstrap 50）与根 34+342、协议/报告依赖/文档链
接与数据库结构检查器通过（Python 3.11）。


#### C9 第十一段的阶段性验证（2026-10-06）：媒体链会话级装配

`bootstrap/capture_assembly.py` 提供会话级拍摄推进工厂
`session_capture_assembly`：按设备声明与驱动登记项（DriverRegistry）
组装 `CaptureRuntime`，控制、停止、读取与源端摘要端口经 `port_for`
按静态声明取得；未声明读取能力的驱动不装配媒体端口（处理行保持
执行中等待），驱动未登记或设备未声明时该设备本轮不推进，动作保
持已保存状态等待后续会话。`capture_flow` 的工厂契约相应扩展为接
收连接与设备身份，就绪描述符按设备分组派发。

媒体链按声明构造：`MediaFlow.digest_for` 按源设备文件构造源端摘
要读取端口（`DriverDigestReader` 经驱动摘要端口调用，请求携带文
件行号、身份键与定位信息，观察按登记证据契约校验、身份用
`device_files` 行号；调用错误或观察不符契约表达为摘要获取失败，
不降级为不支持）；检查与修复经 `HostMediaTools` 走受管媒体工具
（绑定执行器、任务身份与工具参数，等待者取消后由会话 owner 接手
等待实际结束）。异常多录修复余量从 `devices.<id>.recording.repair_
margin_s` 注入（配置加载时校验为有限非负秒数并规范化为 Decimal，
默认 10 秒）。

媒体链等待轮次首次被真实会话多轮驱动，暴露并修复两项既有缺陷：
结果列举的重复观察使归属确认与完成确认幂等（同一来源任务、同一
用途、同一配对按已确认处理，保持已保存依据，不产生重复事件；已
完成文件的完整大小与既往确认不一致是设备观察矛盾，明确拒绝）。

验证：`test_media_assembly.py` 集成 3 项（跨会话异常多录经生产装
配推进对账、停止、原片拷贝、源端摘要比较与修复登记成功，门槛秒
数取自设备配置；读取未声明时媒体端口不装配、修复责任保持待执行；
驱动未登记时不派发、活动保持未启动），检查与修复工具为真实子进
程替身脚本；配置单测 3 项（余量接受数字与精确文本、拒绝非法值、
recording 必须是表）。全量单元 3240、集成 15 目录（bootstrap 53）
与根 34+342、协议/报告依赖/文档链接与数据库结构检查器通过
（Python 3.11）。


#### C9 第十二段的阶段性验证（2026-10-06）：时钟异常会话的保守收场

受限会话的保守收场接线落地。纯决策 `decide_conservative_winddown`
按 min(目标时长, `recovery_wait_cap_s`) 划定保守等待窗口，只用本
会话单调钟从首次观察起计时，窗口内返回剩余秒数、达到即可停止
（含等号），不为完整性无界等待；`RecordingFacts.timing_waived` 表
达保守收场达到上限后放弃计时判定，在途停止与预算耗尽仍照常优先，
未放弃计时的跨会话录像仍先进对账。

推进入口 `advance_winddown` 按已保存事实分区：启动未确认、在途停
止、预算耗尽及本会话锚点分别归各自责任链；停止已确认时不重复停
止——取消已生效收取消终态（放弃内容），否则保存等待阶段；未取消
的录像先按窗口等待，再按原停止预算停止，失败按重试间隔在预算内
重试。等待阶段的保存登记结果观察（归属与写完事实）、固定计时证
据不足的检查决定（REQUIRED 配 INSUFFICIENT_TIMING，此前该依据只
有测试写入方）并关联源文件；列举不可靠或没有写完的完整原片时保
持未定，不用等待阶段代替文件证据。受限会话不启动媒体链、不登记
正式产物，动作保持 running 表达“已停止，等待正常会话处理”，不
新增公共动作状态；后续取得正常执行资格的会话经既有结果推进链用
原停止结果与源文件继续后处理，不重新录像、不重置已消耗预算。

流程装配 `winddown_flow` 供受限会话单次驱动：选取执行中录像按设
备分组经拍摄推进工厂推进；`session_capture_assembly` 以
`media_enabled=False` 构造受限运行时（不装配媒体链）；
`execute_command` 增加 `restricted_flows` 注入点（生产受限装配随
D5 结果列举端口接入后一并接线，取消流程先于保守收场驱动）。

验证：`test_restricted_winddown.py` 集成 6 项（目标 6 秒上限 2 秒
时窗口取上限，停止后保存等待阶段、动作保持 running、原片不成为
正式产物且无拷贝；后续正常会话用原停止结果与源文件完成后处理并
成功终态；重复受限会话不重复等待、停止或登记；取消已生效不经计
时立即停止并取消终态；停止失败两次按重试间隔重试后确认并保存等
待阶段；预算耗尽三次后停止重试、动作保持执行中且不建等待阶段）；
单元 `test_recording_finish.py` 新增 8 项（窗口五分区与放弃计时
三分支）。全量单元 3248、集成 15 目录（bootstrap 59）与根
34+342、协议/报告依赖/文档链接与数据库结构检查器通过
（Python 3.11）。

#### C9 第十三段的阶段性验证（2026-10-06）：照片与录像列举的轮次化

照片响应确定后的产物核实、录像尾段的结果观察与等待中取消的延
时收尾列举统一接入 `results` 责任的有限轮次，共用推进入口
`_listing_round`：在途轮次跨会话恢复前停等；本轮列举失败保存实际
结果与重试等待，下一轮作为新尝试累计名额；预算耗尽交由调用方按
所属拍摄及收场规则结束；其余拒绝说明核实责任已闭合，按直接列举
回退不再保存轮次事实。照片一轮可靠返回即完成核实责任：成功或已
知失败与尝试结束、流程收场同事务，产物暂不齐备等其余分区保持动
作执行中，迟到结果经回退列举消费。录像尾段列举登记观察后按结果
标准收场：终局轮以可靠结果收场核实责任，终局依据尚不齐备时保存
成功结果与重试等待；预算耗尽时录像活动不适用集合结论
（`_ResultSetCommand` 拒绝 type 2），新增 `ResultRunClose` 输入与
`close_unconfirmed_result_run` 命令仅按公共错误结构收场核实流程
（单发 OPERATION_CONFIGURED.FINISH，含原键重送恢复），动作以
`capture_result_unconfirmed` 失败终态且不登记产物。取消收尾列举同
样按轮次重试，失败保持取消待收场事实；预算耗尽先按无法确认收场
核实责任与集合结论，取消终态优先且不登记产物。会话内共享的列举
缓存（装配工厂闭包持有，`CaptureRuntime.listing_cache`）只在完整
原片已归属且媒体责任打开时复用已保存观察，等待媒体装配的推进不
消耗核实名额；产物未齐的列举每轮重新观察文件到达。

测试装配按生产事实补齐两处前提：照片活动创建时
`completion_basis` 为 `UNDETERMINED`（授予命令的初始写入，测试
`_seed_activity` 此前置空使无法确认收场的采集事实保存被状态模型
拒绝）；录像停止与媒体装配会话测试的证据注册表补 `results_returned`
契约（录像路径此前不保存 results 轮次）。

验证：`test_listing_rounds.py` 集成 7 项（照片列举失败下一轮成功、
三轮耗尽按无法确认收场；录像尾段失败重试、耗尽仅收场核实流程且
采集判定列保持为空、会话缓存不重复列举；取消收尾失败重试后登记
产物、耗尽取消终态优先无产物）；`test_capture_contract.py` 照片断
言按轮次事实更新（首轮核实与启动调用各占一次尝试）；
`test_recording_stop.py` 与 `test_media_assembly.py` 补证据契约后
保持通过。全量单元 3248、集成 15 目录（capture 192、bootstrap
59）与根 34+342、协议/报告依赖/文档链接与数据库结构检查器通过
（Python 3.11）；event-transitions 检查器维持 HEAD 既有失败（文档
生成区段待同步）。

2026-10-06 用户确认：照片核实责任闭合后，产物未齐的等待不占核实
名额、不设次数上限，为第一版确认语义；照片因此长期保持执行中在状
态报告中可见，文件持续不出现时由部署侧或人工介入处置。

C9 后置收口：结果列举端口的生产实现与 run 会话默认流程、受限保
守收场流程的接入见[第十五段](#c9-第十五段的阶段性验证2026-10-06结果列举端口与会话装配收口)。

#### C9 第十四段的阶段性验证（2026-10-06）：重试间隔的时间强制

已存在重试语义的责任统一接入设备级间隔的时间强制：结果核实轮次
（`results/<activity_id>`）、录像停止（`stop/<action_id>`）、清理删
除与查询（`delete/<item_id>`、`exists/<item_id>`）和拷贝读取
（`media-input/<processing_id>`）。间隔未到时不发起新尝试，以返回
分区表达等待（`ListingPhase.RETRY_WAIT`、
`HandlerOutcome("stop_retry_wait")`、`CleanupStep` 的
`delete_retry_wait`/`query_retry_wait`、`InputPhase.RETRY_WAITING`），
由推进循环下一轮重新判定，同一轮内继续处理其他设备与动作的调度事
件，不阻塞调度器；等待不消耗尝试次数、上限和窗口。

门槛机制（`operations/attempts.py` 的 `RetryWaitGate` 与
`retry_wait_remaining_s`）：以会话内单调钟锚点表和流程行的权威字
段共同判定——`attempts_used`、`retry_wait_required`、
`max_attempts_used` 任一表明无需等待（无尝试、上轮未建立重试等待、
预算已耗尽）即开闸，锚点在 `finish(retry_wait=True)` 事务成功后登
记；跨会话恢复没有锚点时按本会话首次观察重新计时（本轮不立即重试，
也不无限等待）；预算耗尽预检先行，立即交回意图事务按耗尽收场，不
为等待延长资格。拷贝链无流程行锚点事实，用同门槛建立侧的
`pending` 语义：只对本会话已记录的锚点等待。受限单次驱动的保守收
场流程保留其内部有界等待（该流程无推进循环可依托），与门槛不构成
双重等待。

间隔配置接入 `devices.<id>` 五组字段（`result_check.retry_interval_s`、
`recording.stop_retry_interval_s`、`copy.retry_interval_s`、
`cleanup.delete_retry_interval_s`、`cleanup.query_retry_interval_s`，
默认 3 秒，有限非负秒数校验），装配工厂按设备声明注入各责任运行时，
媒体链落库的 `retry_interval_s_json` 与实际强制间隔取同一来源。
`recording.start_retry_interval_s` 仅接入配置校验：启动责任当前一次
用尽（错误即失败终态），启动重试机制（预算与机会保留模型）为独立
后置任务，接入时一并消费该间隔。

既有缺陷修复（本轮暴露）：清理 exists 流程中查询可靠确认仍在的轮
次原本无处置结束，使 `retry_wait_required` 保持 0，后续
`begin_attempt` 以"后续尝试要求先建立重试等待"拒绝，流程被锁死；
修正为确认仍在与无可靠事实的轮次都按重试等待结束轮次（确认缺席仍
由调用方收场终态），与第七十四段"成功但不终局的轮次必须建立重试
等待"同类。

验证：`test_retry_intervals.py` 5 项（核实与停止间隔等待、零间隔、
预算耗尽即时收场、跨会话重新计时）、`test_cleanup_intervals.py`
3 项（删除与查询间隔、零间隔）、`test_media_flow.py` 拷贝间隔
1 项、`test_attempt_inputs.py` 纯函数与门槛 9 项、
`test_configuration.py` 设备间隔配置 4 项、`test_media_assembly.py`
设备声明注入 1 项；既有测试按间隔语义修正（`test_listing_rounds.py`
零间隔注入、`test_restricted_winddown.py` 停止重试等待 3 秒、
`test_recording_stop.py`/`test_timelapse_finish.py`/`test_run_dispatch.py`
会话共享锚点表——推进循环每轮重建运行时，锚点跨轮保留）。
全量单元 3261、集成分目录独占顺序（capture 198、outputs 1784+1skip、
bootstrap 61、acceptance+cancellation+contracts+devices 344、
history+host_files+logging_runtime+operations 342+5skip、persistence
69、reporting 344、scheduling 125、session 81）与根 34+342、全部
Node/Python 检查器通过（Python 3.11）。event-transitions 检查器此前
记录的"文档生成区段待同步"确认为 Windows 检出的 CRLF 行尾与脚本
生成的 LF 区段不匹配：`--write` 重写后规范化内容与 HEAD 一致，无实
际漂移。

#### C9 第十五段的阶段性验证（2026-10-06）：结果列举端口与会话装配收口

结果列举端口获得生产实现并接入 run 会话默认流程与受限会话的保守
收场流程。`DriverResultListing` 适配（装配工厂内构造）按活动身份调
用驱动的 result 端口（`list_results`，第一版固定批量 100），观察经
登记的 `result_files_listed` v1 契约校验（身份成员 `activity_id` 与
操作目标一致）后逐条解释为候选产物文件：条目携带稳定文件身份、
定位结构、大小（未完成允许为空）、完整与否、类别、原始文件名与媒
体类型，条目自身结构作为归属与完成的结构化依据；类别未知不冒充
已知类别。调用错误在没有可靠观察时表达为异常，由核实轮次按列举
失败保存实际结果与重试等待；可靠观察与调用错误并存时观察优先；
登记证据缺少列举契约属于装配错误直接暴露。装配工厂的 results 参
数改为可选：未注入时按设备声明的 result 能力构造生产适配（能力未
声明的设备本轮不推进拍摄——列举是结果核实的必要前提），注入时整
体替换为部署端口。

进程内驱动登记点（`devices/drivers/runtime.py`）提供
`register_drivers`/`current_registry`/`reset_drivers`：部署适配（或集
成测试的受约束替身）在进程启动阶段登记驱动接入项，重复 driver_id
明确拒绝；静态能力目录与运行端口登记是两个来源，正式厂商接入时
须保证对同一驱动一致。run 会话默认流程集合加入拍摄推进
（`scheduling`）：按设备声明与登记项组装运行时，延时等待配置由
`execution_wait_config` 从首次固定的执行定义取得（目标时长与
result_wait_margin_ms；定义缺少目标时长明确拒绝），等待配置的输入
由生效参数修正为动作行——目标时长与余量不在生效参数中。受限会话
的生产装配加入保守收场（`winddown`）：额外等待上限取
`clock.recovery_wait_cap_s`，工厂以 `media_enabled=False` 构造，取消
与一次报告机会保持原有装配。

验证：`test_result_listing.py` 单元 10 项（条目转换与类别映射、调
用错误与观察并存、身份不符、外来观察类型、契约缺失、条目结构非
法、等待配置读取）；`test_production_flows.py` 集成 3 项（登记点解析
与重复拒绝；延时任务经生产默认装配真实推进到成功终态——发送经登
记驱动确认、结果列举来自 result 端口、完整集合按时间与产物完成判
定登记产物并释放占用、会话完成全部责任后正常退出；受限会话经生
产装配保守收场——按上限等待后确认停止、保存等待阶段与原片归属、
动作保持执行中不登记正式产物、按时钟异常退出）。既有测试修正一
处：`test_run_dispatch` 合并生产装配流程时排除生产 scheduling（该
测试注入自定义拍摄工厂，生产 scheduling 依赖进程驱动登记，合并覆
盖会使动作永不推进）。全量单元 3271（连续两次全绿）、集成分目录
独占顺序（bootstrap 64、capture 198、outputs 1784+1skip、
devices+acceptance 272、cancellation+contracts+history 136、
host_files+logging_runtime+operations 278+5skip、persistence 69、
scheduling 125、session 81、reporting 344）与根 34+342、全部
Node/Python 检查器通过（Python 3.11）。本轮回归期间单元与集成出
现位置漂移的偶发 setup 错误（socket/子进程资源），stash 对比确认
HEAD 同现，属环境瞬时资源压力非本轮回归；清理残留 python 进程后
全部通过。

#### C9 第十七段的阶段性验证（2026-10-07）：停止预算耗尽的残留收场接线

原停止预算耗尽后的收场责任分两层落地。第一层终态化：录像三次停
止尝试全部失败后，停止流程按未确认收场并携带 `recording_stop_failed`
（`activity_id` 与 `operation_run_id` 必填），动作以零产物登记失败
终态；设备活动缺少结束与释放依据，执行中事实与占用原样保留（取
消触发的停止耗尽仍归取消收场链，不在本分支收场）。第二层残留收
场（`capture/residual.py`）：到期拍摄动作开始前经残留门检查同设
备残留候选——无候选或已有可靠空闲判定放行；候选存在且驱动声明
查询能力时执行执行前检查（`query/preflight/<触发动作>`，观察契约
`query_returned` 与 `activity_status`，目标不限定观察身份）：可靠
空闲保存观察后放行；观察到候选活动仍在录制且声明停止与安全重复
停止时建立独立收场流程（kind 8 `STOP_RESIDUAL`，责任键
`followup/<触发>/<活动>`）并按 `devices.<id>.residual_stop.*` 预算
（默认 3 次包含第一次、间隔 3 秒、时限 10 秒）发送停止，绑定与参
数取活动归属动作；观察到其他活动或身份不可解释时等待，不推测空
闲也不授权停止。停止发出未确认且无调用错误时先由确认查询
（`query/residual/<触发>/<活动>`）核实：可靠空闲即按停止事实收场
流程与活动；仍在录制、查询失败或查询预算耗尽时按声明的安全重复
停止继续。停止可靠确认或确认查询空闲后，活动以该成功收场流程行
为停止事实（`_load_stop_fact` 扩展 kind 8）经统一释放判定收场并释
放占用，触发动作随后放行开始。收场预算耗尽按 `recording_stop_failed`
失败终态化（数据库登记约束耗尽不得以未确认收场），残留事实保留，
触发动作按窗口过期收尾。触发动作取消或过期后由 `residual_flow`
孤儿扫描接管：未发出停止的流程保存 CANCELED/EXPIRED，已发出的按
已保存意图继续使用剩余次数；触发动作终态后其执行前检查与确认查
询责任一并结束。同一目标录像同时只保持一个未收口流程（规格按触
发动作唯一约束之上更保守，避免对同一录像双重停止）；受限会话不
推进残留收场，保守收场按既有规则等待正常会话。

验证：`bootstrap/test_residual_winddown.py` 集成 8 项（真实 run 会
话：耗尽终态化后残留活动占用保持→执行前检查确认残留→收场流程
首停确认→活动收场释放并放行触发动作完成；检查失败期间取消触发
不建流程不停止、检查责任随取消结束；首停发出后取消触发改由孤儿
扫描用剩余次数完成收场；收场预算与原停止预算分别计数、耗尽保留
残留且触发动作窗口过期；重启后沿原流程累计次数继续完成并放行触
发动作；检查可靠空闲直接放行不停止、陈旧活动事实保留；观察到其
他活动只等待不建流程；首停发出未确认经确认查询空闲收场不再重复
停止）；`bootstrap/test_recording_stop.py` 新增耗尽终态化 1 项；
`unit/bootstrap/test_configuration.py` 新增 query 与 residual_stop
子表校验 4 项。全量单元 3352、集成 15 目录与根 52+342 通过
（Python 3.11）。Q-07 三类计数独立、O-04 第八释放入口随本段闭合，
scheduling 计划 Q6 已同步收口。

#### C9 验收映射档案（2026-10-07）

本节是 C9 的终验收映射：将[相机录像验收](../../architecture/camera-verification.md)的单元与集成验收段、[拍摄能力](../../architecture/camera-capture.md)验收节的八段，以及[一致性验证](../../camctl/database/consistency-verification.md)中与拍摄执行直接相关的 R/Q/S/O 共 39 条，逐一映射到本模块的测试与分段验证记录。第五段注记的“报告字节与 R/Q/S/O 映射终验收依赖阶段 2 报告链”前提已由 I5 第一链满足（`tests/integration/test_camctl_report_roundtrip.py` 六用例，含固定报告字节对照 `test_report_save_failure_retry_keeps_determined_bytes`）。结论分三类：已覆盖（列锚点）、开放（列归属边界）、联调范畴（不在软件集成测试门槛内）。

**camera-verification 单元验收段：**

- 录像输入验证：`unit/acceptance/test_validation.py` 与 `unit/acceptance/test_definitions.py`。已覆盖。
- 触发动作取消或过期（停止未发出、已发出、未知、已结束）：`unit/capture/test_recording_finish.py` 取消群、`unit/capture/test_discard.py`。已覆盖。
- 后续动作触发的残留收场（触发分区、默认 3 次可配置、重复检查不新建预算）：`integration/bootstrap/test_residual_winddown.py` 八用例（第十七段）。已覆盖。
- 启动与停止尝试上限（默认、分别配置、三入口共享计数、上限降低保留历史次数）：`unit/operations/test_attempt_inputs.py`、`integration/capture/test_retry_intervals.py`、`unit/capture/test_recording_finish.py`。已覆盖。
- 同一计划时间排序（`plans.id`、数组位置、反转加载不变）：`unit/scheduling/test_rules.py` 顺序群与 `integration/scheduling/test_discovery.py`。已覆盖。
- 等待重试保留相机（八分区）与多个待启动录像选择（零、一、多）：`unit/scheduling/test_rules.py` 责任群、`integration/scheduling/test_window_expiration.py` 与授予群。已覆盖。
- 协作者隔离（设备、时钟、产物存储、视频处理）：各单元链测试文件的替身装配。已覆盖。
- 录像动作结束时点（修复未结束不取回、取消与终态事务先后）：`unit/capture/test_media_processing.py`、`unit/capture/test_output_promotion.py`。已覆盖。
- 事实解释（启动意图、有效确认、结果未知）与启动确认锚点（单调钟、不为确认再查询、持久化延迟不移动锚点）：`unit/capture/test_recording_start.py`（`test_recording_anchor_precedes_persistence`）、`unit/operations/test_queries.py`。已覆盖。
- 源文件命名与录像关联（支持、不支持、能力未知、查询失败）：`integration/capture/test_recording_media_link.py`（第九段）。已覆盖。
- 录制 B 时读取已完成 A（执行资格、写完、来源未知）：`integration/capture/test_file_observation.py` 资格群与 host_files 文件责任群。已覆盖。
- 安全重复停止、停止成功与文件可读：`unit/capture/test_recording_finish.py`、`integration/capture/test_recording_finish.py`。已覆盖。
- 原片保留与核验结果（取消、未核验、不足、损坏、无结论、无文件、归属未知分区）：`integration/capture/test_result_confirmation.py`。已覆盖。
- 录像成功标准、异常时长成功依据、断电恢复控制完成依据：`unit/capture/test_recording_result.py`（决策表全分区）、`integration/capture/test_recording_reconcile.py`（第二、十段）。已覆盖。
- 自动修复多录门槛、余量配置缺省与非法值：`unit/capture/test_media_processing.py` 门槛群、`unit/bootstrap/test_configuration.py`。已覆盖。
- 计时证据不足的原片检查、第一版检查范围：`unit/capture/test_media_processing.py` 检查群（真实 `ffmpeg/ffprobe` 样本事实归 WSL 验证，见开放项）。逻辑已覆盖。
- 安全收场与后续处理衔接（时钟异常保持 `running`、不重复停止、恢复用原进度）：C9 第十二段保守收场、`integration/bootstrap/test_restricted_winddown.py` 群。已覆盖。

**camera-verification 集成验收段：**

- B 触发残留收场后取消 B、停止耗尽后 B 独立收场两段：`integration/bootstrap/test_residual_winddown.py` 取消两段与耗尽段（第十七段）。已覆盖。
- 上限默认与分别配置、中断恢复、配置修改后新旧动作各自采用：`integration/capture/test_retry_intervals.py`、`integration/operations` 恢复群。已覆盖。
- 同时间多录像排序与重启重建不变、A 失败重试 B 不启动、A/B 窗口交错 `window_exhausted`：`integration/scheduling/test_discovery.py`、`test_window_expiration.py`、`test_grant_reuse.py`。已覆盖。
- 正常控制完成原子提交（不取回、无空间仍可完成、三处中断、后续媒体问题只追加）：`integration/capture/test_media_flow.py`、`test_result_confirmation.py` 与 reporting 报告链。已覆盖。
- 跨进程恢复已录够即成功（修复组合、归属未知、提交前后中断）：`integration/capture/test_recording_reconcile.py` 与 C9 第四段 `TestInterruptionRecovery`。已覆盖。
- 修复门槛三时点、决定提交前后中断、配置变化语义：`integration/capture/test_media_processing.py`。已覆盖。
- 时钟异常等待阶段 `clock_invalid`（无普通接纳资格、原进度恢复、等待期取消）：C9 第十二段、`integration/bootstrap` 保守收场群。已覆盖。
- 主动拷贝 `staging` 检查（无取回请求也执行、内部不发布、中断续传、空间不足、摘要耗尽、工具失败、取消）：`integration/capture/test_media_execution.py`、`test_media_processing.py`。已覆盖。
- 主机媒体工具真实视频样本时长一致、`ffmpeg/ffprobe` 无重编码修复完整性两段：真实样本与工具行为归 WSL 部署验证（I1/I2/B7 范畴）；逻辑与命令构造已由媒体链测试覆盖。部分覆盖。
- 停止已执行响应未保存恢复、确认记录提交前后中断：`integration/capture/test_recording_reconcile.py`。已覆盖。
- 清理授权删除 A 且 B 继续录像（读取依赖等待）：outputs 清理群与 `integration/capture/test_file_observation.py`。已覆盖。
- 不足时长原片登记与取回（逐字节一致、交付摘要、重启、显式清理）、损坏与核验无结论原片取回：outputs 取回群、`test_result_confirmation.py`、`tests/integration/test_camctl_output_roundtrip.py`。已覆盖。
- 启动意图投影先于命令（三处中断、回放不发令）：`integration/capture/test_recording_start.py` 与 history 回放群。已覆盖。
- 指定路径与设备自行命名两类适配：`integration/capture/test_recording_media_link.py`。已覆盖。
- 修复成品默认取回、显式 ID 取回、修复期报告、取消标记与终态事务先后：`test_media_flow.py`、outputs 取回群、`test_discard.py`、cancellation 集成。已覆盖。

**camera-capture 验收节八段：**

- 四能力组合与参数组合、取消、断电、同请求重送不重复产物：本模块各能力链测试与 `test_capture_contract.py`；跨组件照片与延时链已由 I5 第一链核验，录像与清理跨组件终验收归 I5 剩余两用例。组件内已覆盖。
- 职责表（意图未保存不发送、驱动不重试、发送确认不代替完成、迟到完成不覆盖取消、完成但文件未核实仍 `running`）：`test_capture_contract.py`、`integration/operations/test_attempts.py` 迟到结果群、`test_file_observation.py`。已覆盖。
- 返回语义三分类（锚点保持原时点、不额外等全时长、事件循环可处理其他工作、取消与完成竞争取消优先）：`unit/capture/test_recording_start.py`、`test_media_execution.py`、cancellation 竞争群。已覆盖。
- 驱动必要余量与部署额外等待组合（相加、不互相覆盖、本次运行固定）：`unit/capture/test_timelapse.py` 余量群、`unit/bootstrap/test_configuration.py`。已覆盖。
- 状态查询定时任务（预计结束前不周期查询、到时只核实、重启三分区不重等不重启不重置）：`unit/operations/test_queries.py`、`test_retry_intervals.py`、`test_recording_reconcile.py`。已覆盖。
- 时间与产物判定（判定表分区、不调用未声明接口、中断后不隐式切换）：`test_result_confirmation.py`、`test_listing_rounds.py`、C9 第十五段（能力未声明不推进拍摄）。已覆盖。
- 产物规则（两类产物、七分区、照片完视频合成中不提前登记）：`test_result_confirmation.py` 必要文件群。已覆盖。
- 重启等待恢复（三时点、发送未知、重复重启不重等、恢复用本次单调钟、冲突受限兼容继续）：`test_timelapse.py`、`test_retry_intervals.py`、`tests/integration/test_camctl_capture_roundtrip.py` 延时恢复用例。已覆盖。

**一致性验证 R/Q/S/O 条目：**

- R-01 至 R-08、R-11、R-13、R-14：`unit/operations` 与 `integration/operations`（`test_attempts.py`、`test_recovery.py`、`test_results.py`、`test_process.py`、`test_queries.py`）。已覆盖。
- R-09、R-10：真实 Linux 进程组收场与受管工具建组，归 WSL 部署验证（I1/I2/B7）；系统接口替身分支已覆盖。部分覆盖。
- R-12：成员检查分支替身已覆盖；`ffmpeg/ffprobe` 及包装程序启动入口审计已由 host-files F6 AST 审计覆盖；第三方主程序实际回收归联调核验。部分覆盖。
- Q-01 至 Q-06、Q-08 至 Q-12：`unit/operations/test_queries.py`、`integration/operations/test_queries.py`、`test_grant_reuse.py`、`test_listing_rounds.py`、`test_retry_intervals.py`、`test_configuration.py`（bootstrap）。已覆盖。
- Q-07（B 为 A 建立残留收场、三类计数独立）：`integration/bootstrap/test_residual_winddown.py`（第十七段；执行前检查、收场停止、确认查询三类分别断言计数）。已覆盖。
- S-01 至 S-06：`integration/capture/test_emergency.py`（11 用例，含第九十六段释放组合两用例）与 bootstrap 收场群。已覆盖。
- S-07（补记前后边界从回放、快照正向、投影逆向三路径一致）：回放与快照等价性由 J/H 系列通用验证覆盖；应急补记专属冻结边界的显式三路径对照未单独建立，随 H7 规模验证与 I6 验收映射核对。部分覆盖。
- O-01 至 O-06：第九十六段已在 scheduling 计划 Q6 映射注记逐条落档；O-04 第八释放入口（残留收场）由第十七段交付，八入口齐。已覆盖（引用该注记）。

**开放项清单（均在本任务责任边界之外）：**

1. WSL 部署验证：真实媒体样本时长一致性、`ffmpeg/ffprobe` 修复完整性（R-12 尾项同类）、R-09/R-10 真实 Linux 进程组，归 I1/I2/B7 范畴；固定 ADB 版本与真实启动行为（R-08 尾句）属目标主机联调。
3. I5 剩余录像与清理跨组件用例：录像链与清理链的跨组件终验收，组件内行为已覆盖。
4. D5 结果列举的驱动适配消费：生产适配（C9 第十五段 `DriverResultListing`）已建立，真实厂商驱动接入后的消费验证归 D5。
5. 真实设备联调（camera-verification 末节四段）：明确不作为软件集成测试门槛。

C9 实施形态说明：第 272 行预估的单一汇聚用例 `test_capture_facts_survive_all_recovery_paths` 未建立，其三项目标以散布形式覆盖——各类型副作用边界中断在各链测试的恢复群与 `TestInterruptionRecovery`（文件登记故障回滚重入、终态后重派发历史字节稳定），固定报告字节对照在 I5 报告链，已终态源动作后取回原绑定在 outputs 取回群与 `test_finish_reuse.py`。本档案即为第 274 行映射责任的完成形态。

## 模块完成门禁

录像、照片及延时摄影正常、取消和重启路径通过真实软件组合；适用检查/修复完成后才最终登记。应急与普通预算分开，所有正式产物和公开结果与历史边界一致。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
