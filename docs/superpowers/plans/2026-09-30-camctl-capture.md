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

- [ ] 编写失败用例。在 `test_capability_routes_own_completion` 中三种动作及各完成声明路由到对应流程，`assert chosen_mode == declared_mode`；未支持类型不能默认录像。target_duration_ms 精确且 stop_supported/必要余量组合完整，受理失败定义不得保存。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_definitions.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按动作自身定义拆分类型和处理器，全部定义由首次受理事实恢复；驱动默认值变化不影响旧动作。
- [ ] 再运行上述命令，要求全部 PASS，并核对 公共管理模型设备无关，专属参数仅相应模块解释。
- [ ] 审阅实际接口、状态分区及失败路径，检查 受理、执行和恢复是否重新计算固定定义；记录门禁证据，建议以“feat: 定义拍摄执行与能力路由”形成独立提交。

### C2 录像启动及计时锚点

**预计文件：** `apps/camctl/src/camctl/capture/recording.py`；测试为 `apps/camctl/tests/unit/capture/test_recording_start.py` 和 `apps/camctl/tests/integration/capture/test_recording_start.py`。

**接口与依赖：** 提供异步 `start_recording(context: CaptureContext) -> CaptureStep`、纯 `recording_stop_target(anchor: MonotonicInstant, duration: DurationMillis) -> MonotonicInstant`；CaptureStep 表达已保存事实和下步责任。前置交付：Q4、O2/O3、C1。

- [ ] 编写失败用例。建立 `test_recording_anchor_precedes_persistence`，驱动可靠启动响应时钟为 t，数据库和日志随后延迟，`assert stop_target == t + duration`；意图与派发两次窗口检查，窗口内派发窗口后确认仍接受；仅发送、拒绝无效果、未知及可靠启动后调用错误分别保留。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_recording_start.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。首次授予使用 Q4 的完整事务，驱动确认时立即取锚点并保存原依据，不在查询源文件后重新计时；有限启动与核实按原身份、次数和窗口推进。
- [ ] 再运行上述命令，要求全部 PASS，并核对 正常录像不主动少录，启动确认不自动提供源文件身份。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_recording_start.py -q`，真实调度、仓储和驱动替身在各启动边界中断，报告保留原事实。
- [ ] 审阅实际接口、状态分区及失败路径，检查 启动、查询及迟到成功是否错误重建锚点或重开已终态；记录门禁证据，建议以“feat: 实现录像启动与可靠计时”形成独立提交。

### C3 录像停止、结果登记与异常恢复

**预计文件：** `apps/camctl/src/camctl/capture/recording.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`；测试为 `apps/camctl/tests/unit/capture/test_recording_finish.py` 和 `apps/camctl/tests/integration/capture/test_recording_finish.py`。

**接口与依赖：** 提供 `decide_recording_next(state: RecordingState, facts: RecordingFacts) -> RecordingDecision`、异步 `finish_capture(command: FinishCapture, key: OperationKey) -> DbOutcome[CaptureResult]`；FinishCapture 含可靠最终事实、全部适用产物及提升。前置交付：C2、O4/O5、X1、P3；先提供 MediaProcessingPort，C8 随后接入异常媒体分支。

- [ ] 编写失败用例。在 `test_stop_uses_original_budget_across_cancel_and_restart` 中普通、取消及恢复入口共用原停止次数，`assert stop_count == expected_accumulated_count`；停止成功和文件完成保证分别核对。调用尚未结束保持资源，正式产物登记与终态任一写入失败整组回滚，重启不使用旧进程单调值继续计时。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_recording_finish.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按录像成功标准、取消和恢复模型决定停止及核实，保留原片事实；正常不强制裁剪，异常需处理时保留未完成。可靠产物、适用提升及父计划与动作结果同事务登记。
- [ ] 再运行上述命令，要求全部 PASS，并核对 停止、活动结束、产物和动作结果不混同，原终态不改写。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_recording_finish.py -q`，真实 SQLite、调度、报告与录像替身完成正常、停止耗尽、窗口后确认及恢复链。
- [ ] 审阅实际接口、状态分区及失败路径，检查 正常停止、取消、重启及后续残留收场是否刷新预算或漏占用；记录门禁证据，建议以“feat: 实现录像结束与正式登记”形成独立提交。

### C4 单张拍摄独立流程

**预计文件：** `apps/camctl/src/camctl/capture/photo.py`；测试为 `apps/camctl/tests/unit/capture/test_photo.py` 和 `apps/camctl/tests/integration/capture/test_photo.py`。

**接口与依赖：** 提供异步 `run_photo(context: CaptureContext) -> CaptureStep`、`decide_photo(state: PhotoState, result: CaptureAssessment) -> PhotoDecision`；PhotoDecision 采用该任务的完成声明。前置交付：C1、O2/O4、C6 结果端口。

- [ ] 编写失败用例。在 `test_photo_uses_declared_completion` 中完成后返回和只发送两种契约分别执行，`assert observed_completion == supplied_evidence`；单张任务不默认录像计时/停止。未启动取消、可能启动无停止拒绝、已有合法完成文件在取消后保留。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_photo.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。只实现相应照片任务规则，核实归属和必要文件条件；查询与停止仅在能力声明需要时调用，多文件产物沿 C6/X1 正式登记。
- [ ] 再运行上述命令，要求全部 PASS，并核对 照片不被录像流程的默认假设决定结果。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_photo.py -q`，真实仓储、调度和报告组合照片正常、失败、取消与重启，设备用契约替身。
- [ ] 审阅实际接口、状态分区及失败路径，检查 照片结果、文件检查和取消是否被统一录像分支覆盖；记录门禁证据，建议以“feat: 实现单张拍摄流程”形成独立提交。

### C5 延时摄影等待及跨重启恢复

**预计文件：** `apps/camctl/src/camctl/capture/timelapse.py`；测试为 `apps/camctl/tests/unit/capture/test_timelapse.py` 和 `apps/camctl/tests/integration/capture/test_timelapse.py`。

**接口与依赖：** 提供 `plan_capture_wait(state: TimelapseState, config: CaptureWaitConfig, now: ClockReading) -> WaitPlan`、异步 `run_timelapse(context: CaptureContext) -> CaptureStep`；WaitPlan 含原发送 UTC、预计检查时间和本次单调截止。前置交付：C1、O2/O4、C6；固定完成方式及 stop_supported。

- [ ] 编写失败用例。建立 `test_send_only_has_no_device_completion`，发送成功，`assert device_state_is_observed_ended is False`；锚点取得后 DB 延迟不推迟预计检查。无查询重启计算剩余等待、默认额外等待 0、本次配置变化、原生完成后返回不再等全时长、主机负责结束四类分别验证。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_timelapse.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。分别实现原生任务、发送后等待、状态完成和主机结束路径；驱动必要余量首次固定，部署额外等待采用本次值并保存实际依据。跨重启日期时间用于剩余等待，本次进程使用单调钟，原活动限制仍保留。
- [ ] 再运行上述命令，要求全部 PASS，并核对 无查询合法路径不创建查询预算，首次核实前不周期查询状态。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_timelapse.py -q`，在发送、等待登记、复检和完整结果事务各边界中断并改变本次配置，验证旧报告不变。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有完成方式和等待恢复是否补造直接设备观察；记录门禁证据，建议以“feat: 实现延时摄影等待与恢复”形成独立提交。

### C6 文件归属、集合核实及占用释放

**预计文件：** `apps/camctl/src/camctl/capture/results.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`；测试为 `apps/camctl/tests/unit/capture/test_results.py` 和 `apps/camctl/tests/integration/capture/test_results.py`。

**接口与依赖：** 提供 `assess_capture_files(files: CaptureFileSet, requirements: ProductRequirements) -> CaptureAssessment`、`decide_release(state: ActivityFacts) -> ReleaseDecision`；ProductRequirements 及 ActivityFacts 来自对应驱动声明和持久化事实。前置交付：D2、O4、X1、Q4。

- [ ] 编写失败用例。建立 `test_written_file_does_not_complete_set`，一份文件已写完但集合未齐，`assert assessment.is_complete is False`；合法空、缺必需类型、明确不满足、暂未齐、读取错误、轮次耗尽分别处理。ENDED+HELD 不表示仍拍摄；文件归属未定同范围新拍摄不得放行。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_results.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。分别保存归属、文件完成、集合及必要检查依据，一轮分页只用一个核实次数；统一按 O 系列模型释放占用。预览关系依据明确配对事实，不按目录顺序。
- [ ] 再运行上述命令，要求全部 PASS，并核对 每种事实与必要证据独立保存，缺任一条件不得登记成功或释放。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_results.py -q`，真实文件历史、产物登记、占用事务及三种能力替身，完成 Q/O 系列适用验收。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部正常、停止、取消、无效果、恢复、残留和应急释放入口；记录门禁证据，建议以“feat: 实现拍摄结果与占用判定”形成独立提交。

### C7 有限安全收场与应急最终补记

**预计文件：** `apps/camctl/src/camctl/capture/recovery.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`；测试为 `apps/camctl/tests/unit/capture/test_emergency.py` 和 `apps/camctl/tests/integration/capture/test_emergency.py`。

**接口与依赖：** 提供 `emergency_eligibility(facts: EmergencyFacts) -> EmergencyDecision`、异步 `emergency_stop(scope: EmergencyScope) -> EmergencyRecord`、`save_emergency(record: EmergencyRecord, key: OperationKey) -> DbOutcome[EmergencySave]`；scope 限可靠原目标及本会话预算。前置交付：S3/S5、O3、P4、C6；收尾结果随后由 S6 消费，不得通过 submit 调用。

- [ ] 编写失败用例。建立 `test_emergency_recording_does_not_repeat_stop`，最终补记提交未知核实后 `assert extra_stop_calls == 0`。零尝试已知/未知配置、有尝试已知配置分别按正式组合保存；有尝试未知上限、超限、缺项、普通意图被省略、进行中补记均拒绝。错误重复不刷新本会话额度。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_emergency.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。仅在可靠归属、排他资格、安全重复停止能力及可运行条件成立时应急；目标结束且调用收场后一次补记最终流程、全部尝试和观察。记录保存 not_recorded/recorded/unknown 与实际停止结果分开，不等待数据库无限恢复。
- [ ] 再运行上述命令，要求全部 PASS，并核对 S-01—S-07 全部分区有可证伪用例，应急例外不进入普通调用。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_emergency.py -q`，真实数据库失效/恢复与受约束录像替身，按补记前后 H 重建并验证旧动作终态保持。
- [ ] 审阅实际接口、状态分区及失败路径，检查 受限、普通错误、报告失败及日志失败是否错误取得应急资格；记录门禁证据，建议以“feat: 实现录像有限收场与最终补记”形成独立提交。

### C8 异常原片检查及内部修复

**预计文件：** `apps/camctl/src/camctl/capture/media.py`；测试为 `apps/camctl/tests/unit/capture/test_media_processing.py` 和 `apps/camctl/tests/integration/capture/test_media_processing.py`。

**接口与依赖：** 提供 `decide_media_processing(facts: RecordingEvidence, config: MediaPolicy) -> MediaDecision`、异步 `process_recording(decision: MediaDecision, copies: CopyService, files: MediaFiles) -> MediaResult`；CopyService 采用 X4—X6，MediaFiles 采用 F6。前置交付：C3/C6、X4—X6、F6、X11。

- [ ] 编写失败用例。建立 `test_repair_success_does_not_replace_capture_result`，修复成品合格但采集依据不满足，`assert capture_success is False`。按多录门槛及计时证据完整表测试恰好门槛、超过、缺证据、原片长度不足、空间错误及修复失败；内部检查不默认验证所有媒体内容。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/capture/test_media_processing.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。复用正式读取资格及原片拷贝，保存固定处理决定后执行 ffprobe/ffmpeg；原片、输入副本、临时输出与成品身份分开，全部适用校验及提升与最终登记闭合。
- [ ] 再运行上述命令，要求全部 PASS，并核对 可靠原片不丢失，未验证临时文件不能成为正式产物。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_media_processing.py -q`，真实最小媒体、SQLite、文件提升及历史报告，覆盖工具结束到登记和清理各中断边界。
- [ ] 审阅实际接口、状态分区及失败路径，检查 内部读写是否绕过统一拷贝、占用、取消及文件生命周期；记录门禁证据，建议以“feat: 实现异常录像检查与修复”形成独立提交。

### C9 三种能力的完整链验收

**预计文件：** `apps/camctl/src/camctl/capture/handlers.py`、`apps/camctl/src/camctl/capture/recovery.py`；测试为 `apps/camctl/tests/integration/capture/test_capture_contract.py`。

**接口与依赖：** 使用三种真实处理器、仓储、历史和报告接口；设备侧保持契约替身。前置交付：C1—C8、Q6、R8、N1—N5、X1—X11；不依赖其他模块的最终验收任务。

- [ ] 编写失败用例。在 `test_capture_facts_survive_all_recovery_paths` 中各类型每个副作用边界中断，`assert current_result == expected_result` 且固定 H 报告字节不变；改变查询/停止/完成声明，公共调度仍能组合。已终态源动作后取回或清理仍取得原绑定。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_capture_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。补齐 camera-verification、camera-capture 验收及适用 R/Q/S/O 条目映射，核验采集→正式产物→取回→报告整链。
- [ ] 再运行上述命令，要求全部 PASS，并核对 没有把某个真实相机保证强加到其他能力，全部失败/恢复有归属。
- [ ] 审阅实际接口、状态分区及失败路径，检查 三种流程的未知、预算、占用、终态及迟到结果同类风险；记录门禁证据，建议以“test: 验证全部拍摄能力闭环”形成独立提交。

## 模块完成门禁

录像、照片及延时摄影正常、取消和重启路径通过真实软件组合；适用检查/修复完成后才最终登记。应急与普通预算分开，所有正式产物和公开结果与历史边界一致。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
