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
- [ ] 审阅实际接口、状态分区及失败路径，检查 启动、查询及迟到成功是否错误重建锚点或重开已终态；记录门禁证据，建议以“feat: 实现录像启动与可靠计时”形成独立提交。

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

- [ ] 编写失败用例。在 `test_capture_facts_survive_all_recovery_paths` 中各类型每个副作用边界中断，`assert current_result == expected_result` 且固定 H 报告字节不变；改变查询/停止/完成声明，公共调度仍能组合。已终态源动作后取回或清理仍取得原绑定。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_capture_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。补齐 camera-verification、camera-capture 验收及适用 R/Q/S/O 条目映射，核验采集→正式产物→取回→报告整链。
- [ ] 再运行上述命令，要求全部 PASS，并核对 没有把某个真实相机保证强加到其他能力，全部失败/恢复有归属。
- [ ] 审阅实际接口、状态分区及失败路径，检查 三种流程的未知、预算、占用、终态及迟到结果同类风险；记录门禁证据，建议以“test: 验证全部拍摄能力闭环”形成独立提交。

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

## 模块完成门禁

录像、照片及延时摄影正常、取消和重启路径通过真实软件组合；适用检查/修复完成后才最终登记。应急与普通预算分开，所有正式产物和公开结果与历史边界一致。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
