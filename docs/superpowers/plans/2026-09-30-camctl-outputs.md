# camctl 产物取回与清理模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 实现正式产物登记、固定来源、可恢复拷贝、独立交付、源清理和中间文件生命周期。

**组织建议：** 按登记、来源、资格、拷贝、交接、清理和预览用例拆分；取回及内部录像处理共用 CopyService，delivery 由发起流程决定是否创建。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用完整 SQLite 用例、D4 读取会话、F3 分段线程与 F5 文件交接；预算、资格及汇总规则保持业务所有。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [产物规则](../../architecture/outputs.md)、[取回](../../architecture/obtaining-outputs.md)、[拷贝与续传](../../architecture/file-copy.md)、[正式清理](../../architecture/output-cleanup.md)、[自动预览](../../architecture/preview-obtaining.md)、[文件交接](../../architecture/file-handoff.md)、[数据库协调](../../camctl/database/cleanup-coordination.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

正式产物、源文件、独立交付和中间文件是不同生命周期。来源及选中集合一次可靠固定，不因重启或后续文件出现扩大。资格按业务时间及同时间取回优先，不能按唤醒顺序抢占。副本准备完成与解除源读取依赖共同提交。读取尝试和摘要重拷分别累计，分段及重启不补满。普通交付失联但没有可靠完成事实时按未知交接失败，不自动重投；源清理不级联删除已准备交付、派生成品或历史。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

本模块对采集提供纯登记规则，对取消提供逐项独立收场端口，对内部处理提供共用拷贝用例。调用者不操作本模块私有行和文件状态。

| 类型 | 字段或含义 |
| --- | --- |
| `OutputDraft / OutputCatalogFacts` | 来源动作、设备或主机文件、产物类别、原片/修复/预览关系及必要登记事实；原设备文件身份保持。 |
| `SourceResolution / SelectionSnapshot` | 来源未解析、已固定含空集合、错误；逐来源尚未选定、合法空选择、已选定及逐项失败分别保存。 |
| `FileQualification / CopyIdentity` | 读取或删除资格、来源保护和实际占用；拷贝含处理归属、源文件、目标中间文件、读取尝试及轮次。 |
| `CopyFacts / CopyDecision` | 固定长度 N、可靠进度 C、文件观察 L、两种次数、校验及资格；决定续传、核实、重拷、取消或失败。 |
| `PreparedCopy / DeliveryFacts` | 完整同步文件及主机摘要，独立交付身份与发布/撤回阶段；不依赖源文件继续存在。 |
| `CleanupFacts / CleanupDecision` | 固定目标、唯一删除处理者、读取保护、删除与查询次数、文件事实、取消及逐项结果。 |
| `WorkFileCleanupScope / OutputRepository` | 中间文件用途、归属、操作实际结束及本次历史清理游标/额度；仓储只提供完整原子用例。 |

先处理原终态、取消和事实错误，再判断各类文件工作。续传位置不得从默认值推导。

| 可靠源长度 N、可靠进度 C 与主机文件事实 | 续传决定 |
| --- | --- |
| 文件缺失且 C=0 | 创建原身份的目标，从 0 开始。 |
| 文件存在且 L=C<N | 从 C 连续读取。 |
| 文件存在且 C<L≤N | 截断并同步至 C，再继续。 |
| 文件存在且 L=C=N | 进入完整性确认，不新增空段。 |
| 文件缺失且 C>0，或 L<C | 已确认数据缺失，报错，不跳过缺口。 |
| C 越界或 L>N | 状态矛盾，报错。 |
| 身份、进度或文件检查不可靠 | 停止依赖判断，不视为 0 或缺失。 |

删除的事实与两组预算独立组合：可靠删除/不存在直接完成；仍存在且有删除额才发删除；效果未知只能用剩余查询额核实，查询额耗尽不能用重发删除替代。取消后停止新增删除，已经发生或可能发生的效果继续保留限制。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/outputs/catalog.py` | 正式登记及原片/修复/预览关系。 |
| `apps/camctl/src/camctl/outputs/sources.py` | 来源固定及逐来源选择。 |
| `apps/camctl/src/camctl/outputs/qualification.py` | 读取、删除顺序和保护。 |
| `apps/camctl/src/camctl/outputs/copy.py` | 共用拷贝身份、尝试、段进度和校验。 |
| `apps/camctl/src/camctl/outputs/handoff.py` | 普通 delivery 发布、恢复及撤回。 |
| `apps/camctl/src/camctl/outputs/cleanup.py` | 完整源清理及独立查询/删除预算。 |
| `apps/camctl/src/camctl/outputs/previews.py` | 自动预览选择与读控协调。 |
| `apps/camctl/src/camctl/outputs/work_files.py` | 中间文件首次及历史有限清理。 |
| `apps/camctl/src/camctl/outputs/service.py` | 取回逐项准备、统一发布和最终汇总。 |
| `apps/camctl/src/camctl/outputs/ports.py` | 窄仓储、CopyService 及独立收场接口。 |
| `apps/camctl/src/camctl/persistence/repositories/outputs.py` | 集合、资格、进度、准备、交接和清理原子操作。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享与持久化](2026-09-30-camctl-persistence.md) | K1—K4、P3—P5 与 H1—H5 的历史接入。 |
| [操作与调度](2026-09-30-camctl-operations.md) | O2—O5、Q3/Q4/Q6 的尝试、次数及设备机会。 |
| [设备与文件](2026-09-30-camctl-host-files.md) | D2/D4、F1—F5 的分类证据与实际停止。 |
| [采集](2026-09-30-camctl-capture.md) | X1 先供 C3/C6 使用；C8 只依赖 X4—X6，不依赖全部取回流程。 |
| [取消与报告](2026-09-30-camctl-cancellation.md) | N3/N5 施加取消但结果由本模块继续；R3/R7 核验公开结果。 |

X1 随首次录像完成；X2—X7 实现首条取回链。X8/X9 在明确来源与资格稳定后接入源清理；X10 在三类拍摄、取回和取消组合后完成；X11 与首次中间文件同时保存生命周期，完整历史预算后续验收。X12 是整模块组合门禁。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 固定空集合与尚未选择不可混淆。 | X2，`test_empty_selection_is_fixed` |
| 副本可靠准备前不能解除源保护。 | X6，`test_prepared_commit_releases_source` |
| 取消后段成功不得新增待清理副本进度。 | X5，`test_cancel_before_progress_prevents_new_commit` |
| 普通交接未知不得按报告规则补投。 | X7，`test_missing_all_copies_is_final_unknown_failure` |
| 后续清理成功不能改写旧请求失败。 | X9，`test_later_cleanup_preserves_old_failure` |

## 实施任务

### X1 正式产物登记及文件关系

**预计文件：** `apps/camctl/src/camctl/outputs/catalog.py` 与 `apps/camctl/src/camctl/persistence/repositories/capture.py`；纯规则测试为 `apps/camctl/tests/unit/outputs/test_catalog.py`，正式守卫与真实登记测试随来源、关联和文件生命周期分别组织。

**接口与依赖：** 提供 `validate_output_registration(drafts: tuple[OutputDraft, ...], facts: OutputCatalogFacts) -> RegistrationChanges`；RegistrationChanges 由 C3.finish_capture 的同一事务应用。前置交付：K1、D2、H1；不依赖取回全部实现。

- [x] 编写失败用例。在 `test_registration_preserves_device_file_identity` 中正式登记前后 `assert file_id_after == original_file_id`；没有归属或文件完成不能登记。合法登记尚无 SHA-256 可保存未知摘要，已有可靠值不得丢失；原片/修复/预览明确关联，任意目录顺序不能配对。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_catalog.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。把登记规则作为纯计算供采集完整终态事务使用；源位置、产物身份及关联固定，内部文件按适用校验提升。派生关联、提升及可靠元信息按[正式产物登记计划](2026-10-03-camctl-output-registration-review.md)验收。
- [x] 再运行上述命令，要求全部 PASS，并核对 登记与动作结果原子，摘要不是所有产物的强制前置计算。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/capture/test_registration_relations.py apps/camctl/tests/integration/outputs/test_registration_source_sequence.py -q`，核验同批原片关联、既有原片的正式派生事件、逐种唯一性及完整回滚。文件提升和完整历史闭环另按 R3/R5 收齐证据。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部拍摄及修复登记入口是否重复文件身份或拆开终态；记录门禁证据，建议以“feat: 实现正式产物登记规则”形成独立提交。

### X2 来源固定、选择与精确 ID

**预计文件：** `apps/camctl/src/camctl/outputs/sources.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_sources.py` 和 `apps/camctl/tests/integration/outputs/test_sources.py`。

**接口与依赖：** 提供 `resolve_source(spec: SourceSpec, lookup: SourceLookup) -> SourceResolution`、`select_outputs(source: SourceResolution, facts: OutputCatalogFacts, mode: SelectionMode) -> SelectionSnapshot`；SourceSpec/SelectionMode 来自 A3 固定执行定义。前置交付：X1、K1/K2、P3；来源执行定义由本模块提供给 A3。

- [x] 编写失败用例。建立 `test_empty_selection_is_fixed`，来源完成无产物，`assert selection.is_fixed is True` 且 ids=()，与未选定不同。默认选择以修复替代原片、不含预览；修复不可用不回退。精确 ID 不存在/错误来源/已清理各逐项失败且无 delivery；可靠存在过而后记录缺失是状态库错误。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_sources.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。本计划关联使用受理保存关系，跨计划在执行资格后一次固定；逐来源完成后固定选择，保存合法空和每项最终失败，不因新文件出现重新选择。
- [x] 再运行上述命令，要求全部 PASS，并核对 来源形式及精确筛选不会扩大范围，读取失败不当空集。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_sources.py -q`，真实 SQLite 固定来源、重启及部分失败，覆盖所有六种来源形式和合法空选择。
- [x] 审阅实际接口、状态分区及失败路径，检查 同计划/跨计划/组/全计划/预览/精确 ID 入口；记录门禁证据，建议以“feat: 实现固定来源与产物选择”形成独立提交。

### X3 读取和删除资格的共同事务

**预计文件：** `apps/camctl/src/camctl/outputs/qualification.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/integration/outputs/test_qualification.py`。

**接口与依赖：** 提供异步 `grant_file(command: FileCandidate, key: OperationKey) -> DbOutcome[FileQualification]`；取回取得逐产物读取资格时同时建立源依赖、delivery、copy、目标文件及读取流程。相机读取机会由已建档拷贝另行取得，等待机会期间保留源依赖；内部处理直接引用原录像处理责任及设备原片。具体状态分区和修复门禁见[文件读取资格与拷贝责任](2026-10-02-camctl-file-qualification-review.md)。前置交付：X2、Q1/Q4、O2、P3。

- [x] 编写失败用例。在 `test_qualification_uses_business_order` 中颠倒来源结束及协程唤醒，`assert winner == expected_by_plan_time`。同一产物的取回与清理按计划时间排列，同时间取回优先；相机拷贝机会按发起动作时间、计划、数组位置及文件登记顺序排列，内部处理与取回不另设类型优先级。已有读取保护、不可撤销清理限制、唯一删除处理者及跨设备候选分别验证；缺任一建档行整笔拒绝。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_qualification.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。事务内查全部可靠限制和业务顺序，完整授予或保存逐项拒绝；内部检查/修复共用设备单文件读取机会，不必创建 delivery。
- [x] 再运行上述命令，要求全部 PASS，并核对 资格不是先检查后另事务抢占，等待不消耗尝试。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部普通取回、自动预览、内部处理和源删除是否经过相同资格规则；记录门禁证据，建议以“feat: 实现读取与清理原子资格”形成独立提交。

X3 按[文件读取资格与拷贝责任](2026-10-02-camctl-file-qualification-review.md)的 F1—F5 完整实施与验收：逐产物候选竞争由 `outputs/competition.py` 纯规则提供，仓储与正式授予守卫共用；相机机会经独立 `grant_read_slot`/`release_read_slot` 事务按统一文件顺序授予。计划中建议的 `test_qualification_uses_business_order` 名称在实施中展开为资格矩阵（`test_qualification.py` 35 项）、候选生命周期（`test_fixed_candidates.py`）、来源解析（`test_product_competition.py`）、时间门禁（`test_read_schedule.py`）与机会事务（`test_read_slot.py`），全部通过；等待出口均为只读且不增加尝试次数。2026-10-05 的 F5 总收口完成矩阵复核、两层资源责任与原键恢复的独立复核，录像处理事件组合由 `test_selection_processing_sequence.py` 闭合。

### X4 共用拷贝身份与续传准备

**预计文件：** `apps/camctl/src/camctl/outputs/copy.py`、`apps/camctl/src/camctl/outputs/ports.py`；测试为 `apps/camctl/tests/unit/outputs/test_copy_resume.py` 和 `apps/camctl/tests/integration/outputs/test_copy_resume.py`。

**接口与依赖：** 提供 `decide_resume(facts: CopyFacts) -> CopyDecision`、异步 `prepare_copy(identity: CopyIdentity, service: CopyContext) -> CopyStep`；CopyContext 含仓储、读取和文件端口，CopyStep 表达原身份及待办阶段。前置交付：X3、D4、F1/F4、O2/O5。

- [x] 编写失败用例。按上方七个续传分区建立 `test_resume_preserves_confirmed_prefix`，C=3、L=5、N=6，`assert decision.truncate_to == 3` 且 offset=3；C=0/L缺失、L=C=N、不可靠检查及长度越界分别断言。未保存读取失败重启沿原尝试，已明确失败只能新增合法尝试。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_copy_resume.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。固定源身份、长度、原目标及处理归属；读取开始前保存意图/次数；续传截断必须同步成功才继续，不重新分配 delivery 或重拷轮次。
- [x] 再运行上述命令，要求全部 PASS，并核对 取回、原片检查及修复可以调用同一 CopyService。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_copy_resume.py -q`，真实文件与数据库验证多次中断、可靠尾部、源身份改变和两种失败恢复。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有恢复入口是否从当前长度默认进度或补满读取次数；记录门禁证据，建议以“feat: 实现共用拷贝与续传准备”形成独立提交。

X4 的阶段验证：`outputs/copy.py` 提供 `decide_resume`（七分区加目标重置分区：CREATE/CONTINUE/TRUNCATE/VERIFY/RESET_TARGET）与 `decide_attempt`（在途尝试沿原尝试恢复、已明确失败只能经合法入口新增、旧轮次忽略、未来轮次拒绝）纯规则，`prepare_copy` 经仓储与文件端口编排观察、截断/创建、同步与 `COPY_CHANGED.RESET` 保存；仓储新增只读 `load_copy_state`（核对目标用途与归属一致、保存路径可定位、源文件当前长度等于建档固定长度）与 `reset_copy_target` 事务（重置意图 2→1，进度非零拒绝，原键恢复首次响应），`copy` 守卫扩展核对重置完成时当前拷贝进度已归零。单元 40 项；集成 40 项覆盖交付/录像内部输入/主机修复产物三类来源共用同一入口、截尾后续传与字节保持、同步失败阻断且可靠进度不变、设备与主机源身份改变拒绝、真实在途尝试恢复且数据库不变、已失败尝试只能新增且预算耗尽被 `begin_attempt` 拒绝、重置的截断-保存两步与截断后断电的幂等恢复、原键恢复与输入不符拒绝、目录占用目标路径不当作缺失。Python 3.11 通过组件单元 2588 项、集成 2780 项另 6 项跳过及根跨组件 34 项另 342 subtests。所有恢复入口不以当前文件长度默认进度、不补满读取次数；预算、重试等待与机会判断仍唯一由意图入口承担。SEGMENT 事件生产者属 X5，进度种子在测试中直接保存并注明。

### X5 分段提交、取消和可靠进度

**预计文件：** `apps/camctl/src/camctl/outputs/copy.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_copy_segments.py` 和 `apps/camctl/tests/integration/outputs/test_copy_segments.py`。

**接口与依赖：** 提供异步 `copy_next_segment(identity: CopyIdentity, context: CopyContext) -> CopyStep`、仓储 `save_segment(command: ReliableSegment, key: OperationKey) -> DbOutcome[CopyProgress]`；ReliableSegment 含原轮次、旧 C、范围及同步证据。前置交付：X4、F2/F3、P3/P4。

- [x] 编写失败用例。建立 `test_cancel_before_progress_prevents_new_commit`（集成名 `test_transfer_success_but_canceled_owner_skips_progress_commit`），段成功返回但普通取回已取消，`assert _segment_events(owned) == 0`；进度事务已开始则仍跟踪实际提交。同步失败 `assert progress_after == progress_before`；提交未知不得排下一段。改段大小后不把 C 对齐新倍数。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_copy_segments.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。（初始红：plan_segment、ReliableSegment、ReadResumeRequest 均不存在。）
- [x] 实施本任务。一文件一次一段，段大小从本次配置取得；实际写入及同步可靠、资格仍成立才保存进度，确认提交后推进下一段。每段不新建读取尝试，不重置无数据等待或预算。
- [x] 再运行上述命令，要求全部 PASS，并核对 可靠进度没有空洞、重复或未同步尾部，取消停止新增普通操作。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_copy_segments.py -q`，真实默认池、SQLite 与可控源流，在段内、同步和进度提交前后中断。
- [x] 审阅实际接口、状态分区及失败路径，检查 段成功、文件长度及进度保存是否被错误视为同一事实；记录门禁证据，建议以“feat: 实现可靠拷贝段与取消收场”形成独立提交。

X5 的阶段验证：`outputs/copy.py` 提供 `plan_segment`（E = C + min(S, N - C)；N-C=0 不产生空段；C 越界按一致性错误拒绝）与 `copy_next_segment` 编排（加载固定事实→计划段→线程内 `transfer_segment` 传输并同步→`save_segment` 事务保存；传输、同步或保存失败保留 `CopySegmentError` 阶段诊断，提交未知停止推进不排下一段）；`ReliableSegment` 构造强制已同步推进范围。仓储 `save_segment` 在写事务内核对轮次、旧进度、固定源长度与发起责任（取消→`cancel_requested`，动作不在执行→`owner_not_running`，均只读跳过不产生事件），保存 `COPY_CHANGED.SEGMENT`（reason 2）事件并同步投影；原键恢复首次响应，旧进度或范围不符拒绝。`_copy_guard` 扩展 SEGMENT 分支（进度必须推进且不越过源长度）。`host_files/io.py` 新增 `PositionedWriter`（定位到段起点的顺序写入与同步）与 `LocalSourceReader`（主机源顺序读取，与设备会话共用段传输接口）。补齐 `ATTEMPT_RESULT.RESUME_READ`（reason 4）生产者：`OperationsRepository.resume_read` 把本次运行采用的预算与期限写入原在途读取尝试（配置未变只读跳过，尝试已结束报告 `not_running`），`read_resume` 守卫注册并核对事件只属于读取流程。单元 35 项；集成 39 项覆盖交付/录像内部输入共用同一入口、10 字节三段推进与字节一致、无剩余不产生空段、改段大小从已确认位置继续不对齐、在途尝试数量不变、取消与终态动作跳过进度提交且物理尾部保留、同步失败进度不变、源提前结束按读取失败保留进度、停止通知在小块之间生效、提交未知不推进、原键恢复与输入不符拒绝、守卫接受真实事件并拒绝空段或越界推进、主机源分段字节一致、恢复配置三分区与原键、非读取流程被守卫拒绝。Python 3.11 通过组件单元 2623 项、集成 2840 项另 7 项跳过及根跨组件 34 项另 342 subtests；文档链接 2904 通过。

### X6 摘要、有限重拷与准备完成

**预计文件：** `apps/camctl/src/camctl/outputs/copy.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_copy_complete.py` 和 `apps/camctl/tests/integration/outputs/test_copy_complete.py`。

**接口与依赖：** 提供 `decide_integrity(facts: IntegrityFacts) -> IntegrityDecision`、异步 `complete_copy(identity: CopyIdentity, context: CopyContext) -> DbOutcome[PreparedCopy]`；IntegrityFacts 含固定长度、可靠读取、主机摘要及源能力/摘要结果。前置交付：X5、D2/D3、F4。

- [x] 编写失败用例。建立 `test_prepared_commit_releases_source`，完整性及同步成功但准备事务尚未确认，`assert source_dependency_released is False`；事务提交后为 True。源摘要支持相同/不同/失败、明确不支持、能力未知五分区覆盖。读取额与重拷额独立，已用读取 3 仍可合法重拷但读取再次错误立即耗尽。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_copy_complete.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。（初始红：IntegrityFacts、decide_integrity、VerificationSave 均不存在。）
- [x] 实施本任务。主机 SHA-256 必须计算；源支持时必须比较，失败不能降级。摘要不一致先提交轮次消耗和进度归零，再截断/重建并记录重置完成；准备完成与解除源依赖共同保存并通知清理。
- [x] 再运行上述命令，要求全部 PASS，并核对 两类预算及原身份保持，已准备副本不再依赖源存在。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_copy_complete.py -q`，真实字节摘要、重拷决定到文件重置各中断边界及来源清理竞争。
- [x] 审阅实际接口、状态分区及失败路径，检查 完整性、重拷、内部输入和普通取回是否存在绕过校验；记录门禁证据，建议以“feat: 实现拷贝校验与副本准备”形成独立提交。

X6 的阶段验证：`outputs/copy.py` 提供 `decide_integrity`（五分区：MATCHED/MISMATCHED/SOURCE_UNAVAILABLE/VERIFICATION_FAILED/CAPABILITY_UNDETERMINED；MISMATCHED 附带 REGISTER/EXHAUSTED/OWNER_NOT_ELIGIBLE 重拷子判定，只依赖重拷次数与本次上限，不接收读取尝试次数）、`SourceChecksumSupport`（设备源按 `device_files.checksum_support` 映射，主机源总是支持）与 `complete_copy` 编排（线程内 `hash_target` 计算主机摘要；源摘要按已保存值复用，未取得且支持时经 `SourceDigestReader` 端口获取；能力未知不折叠为不支持；分支保存后进入准备完成、重拷登记或保留阶段诊断）。仓储新增 `save_verification`（COPY_CHANGED.VERIFY reason 3：全部字节可靠保存后才保存终局校验状态，取消或不在执行只读跳过）、`register_recopy`（不一致事实尚未保存时在同一事务先保存 VERIFY(MISMATCHED) 再保存 RECOPY reason 4（轮次与额度各增一、进度归零、登记重置意图、清除旧目标摘要）；额度耗尽保存不一致诊断与实际判定上限（CONFIGURE reason 7），不登记新轮次；取消或不在执行不开始重拷）、`save_prepared`（校验通过后目标文件完整字节事实（INTERMEDIATE_FILE_CHANGED.LIFECYCLE）、交付进入 PREPARED（DELIVERY_CHANGED.PREPARE）与解除取回源依赖（READ_PERMISSION_CHANGED.RELEASE）共同提交；内部输入副本不解除取回源依赖；先前已提交的完整事实幂等恢复）。守卫扩展：`_copy_guard` 校验/重拷/判定上限分支（MATCHED 要求摘要一致、MISMATCHED 要求都存在且不等、重拷必须归零且恰好增加一轮）、`_read_permission_guard` RELEASE 分支（副本可靠准备前不能解除源保护）、`_delivery_guard` PREPARE 分支、`_intermediate_guard` LIFECYCLE 分支（完整字节必须同时携带长度与摘要）并注册 `processing` 守卫（处理输入副本完整字节事实以唯一拷贝校验完成为前提；处理状态分支留待媒体处理模块接入）。单元 43 项；集成 22 项覆盖交付与内部输入、校验通过准备完成并解除源依赖、准备事务提交未知不冒充解除且恢复重放幂等、明确不支持降级完成、能力未知停止、获取失败保存诊断不降级、端口取得源摘要保存并在重复收尾时复用、主机摘要计算失败不保存状态、进度未满拒绝收尾、摘要不一致双事件登记新一轮（轮次/额度/归零/重置意图/清摘要）、重拷后截断重建新一轮完整拷贝再校验通过并准备完成的端到端闭环、额度耗尽保存判定上限不登记、读取尝试耗尽不阻止重拷且新一轮新增尝试仍被原预算拒绝、取消不校验不重拷、内部输入不解除源依赖、主机派生成品源校验、三个事务入口原键恢复与输入不符拒绝、守卫接受真实事件并拒绝摘要不一致的 MATCHED、未满进度的重拷与未校验的源依赖解除。Python 3.11 通过组件单元 2666 项、集成 2864 项另 7 项跳过及根跨组件 34 项另 342 subtests；文档链接 2905 通过。清理等待者的唤醒通知属执行层调度，解除事实已由 RELEASE 事件持久化，随 C8/调度接入消费。

### X7 普通交付发布及恢复

**预计文件：** `apps/camctl/src/camctl/outputs/handoff.py`、`apps/camctl/src/camctl/outputs/service.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_delivery.py` 和 `apps/camctl/tests/integration/outputs/test_delivery.py`。

**接口与依赖：** 提供 `decide_handoff(facts: DeliveryFacts, files: DeliveryLocations) -> DeliveryDecision`、异步 `publish_delivery(identity: DeliveryIdentity, context: DeliveryContext) -> DeliveryResult`；位置观察分别分类 staging/ready/processing。前置交付：X6、F5、P3/P4。

- [x] 编写失败用例。建立 `test_missing_all_copies_is_final_unknown_failure`，无完成事实且可靠三处缺失，`assert error.code == 'delivery_handoff_unconfirmed'` 且重投次数 0；三处检查任一失败不能归此分支。完整 staging 继续原身份，ready/processing 可补本地事实，已保存完成不因文件删除撤销。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_delivery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。（初始红：handoff 模块不存在。）
- [x] 实施本任务。整次取回满足发布条件后，先提交原 delivery 发布意图，事务外移动/同步，最后按实际证据保存结果；主程序已经领取也可保存当前可靠完成。未知最终失败保持，其他项继续。
- [x] 再运行上述命令，要求全部 PASS，并核对 本地交付不冒充远端传输或客户端收件。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_delivery.py -q`，真实目录、SQLite 及受协议约束的领取协作者，在意图、移动、同步和结果保存前后中断；真实 C 领取归 I5。
- [x] 审阅实际接口、状态分区及失败路径，检查 普通交付是否误用报告补投、覆盖同名文件或删除 processing；记录门禁证据，建议以“feat: 实现普通交付与未知恢复”形成独立提交。

X7 的阶段验证：`outputs/handoff.py` 提供 `decide_handoff`（七分区：COMPLETED/DELIVERED_LOCALLY/PUBLISH/HOLD/UNCONFIRMED_FINAL/UNDECIDABLE/NOT_ACTIVE；ready/processing 副本必须按长度与摘要确认是同一完整副本，staging 副本按长度核对、观察携带摘要时一并核对；观察不可靠或多处并存归 UNDECIDABLE，不套用三处均无分支；终局未知失败构造公共登记的 `delivery_handoff_unconfirmed`，republishes 恒 0 不自动重投）与 `publish_delivery` 编排（加载交付事实→三位置观察→按分区执行：PUBLISH 先 `save_publication_intent` 再经 F5 `publish_file` 原子移动并同步目录，同步确认（含平台不支持）才 `save_publication`；同步失败保留已移动事实不保存完成；NOT_MOVED 后重新观察确认交付或保存终局失败；移动结果未知只保留诊断）。仓储新增 `save_publication_intent`（DELIVERY_CHANGED.INTENT reason 3：PREPARED→PUBLISHING 并把意图登记为本事件，取消或不在执行只读跳过，已有意图幂等 ALREADY）、`save_publication`（PUBLISH reason 4：PUBLISHING→PUBLISHED 并登记完成事件；确认已发生的外部交接结果不受发起责任取消影响；PREPARED 且副本已在交接位置时同一事务补存 INTENT 与 PUBLISH）、`save_unconfirmed_failure`（FAIL reason 5：保存公共错误对象，已 PUBLISHED 拒绝改判）与只读 `load_delivery_state`。守卫扩展 INTENT/PUBLISH（事件行必须自引用本事件并对应唯一已校验拷贝）与 FAIL（错误对象按公共登记校验且关联本次交付）。单元 33 项；集成 26 项覆盖正常发布全链、条件未满足保持、取消跳过意图、意图提交未知不移动并恢复、目录同步失败保留移动事实后按 ready 观察补存、主程序提前领取到 processing、NOT_MOVED 重新观察确认、ready 同名文件不覆盖且不投放 staging 副本、PUBLISHING 恢复继续原身份不分配新交付、副本已交接后取消仍保存事实、PREPARED 补意图组合、三处均无保存终局失败且终态不复活、观察失败不误判缺失、已保存完成不因文件删除撤销、未准备完成拒绝意图、已发布拒绝改判、四组原键恢复与输入不符拒绝、守卫接受真实/补意图事件并拒绝自引错误、未登记错误码及未校验拷贝的发布。真实 C 领取模块组合归 I5。

### X8 完整源清理与独立预算

**预计文件：** `apps/camctl/src/camctl/outputs/cleanup.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_source_cleanup.py` 和 `apps/camctl/tests/integration/outputs/test_source_cleanup.py`。

**接口与依赖：** 提供 `decide_cleanup(facts: CleanupFacts) -> CleanupDecision`、异步 `advance_cleanup(item: CleanupIdentity, context: CleanupContext) -> CleanupStep`；实际文件删除/查询使用 D3 或 F2。前置交付：X2/X3、O2/O4、F1/F2、P3。

- [x] 编写失败用例。按文件已删除/仍存在/未知与两组额度有余/耗尽建立 `test_cleanup_budgets_are_independent`，`assert delete_used == expected_delete` 且 query_used 独立。删除额耗尽仍可剩余查询，未知且查询额耗尽不能重删；可靠完成不额外查询。已准备 delivery、修复产物及历史不级联删。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_source_cleanup.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。先固定完整目标，按资格和唯一处理者调用；每次真实删除或存在性查询前保存意图及所属次数，调用结束后文件事实、产物可用性、逐项与动作汇总共同提交。
- [x] 再运行上述命令，要求全部 PASS，并核对 未知不变删除成功，配置改变和重启不重开终态。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_source_cleanup.py -q`，真实仓储、文件/设备替身验证全部删除与查询结果及取回先后竞争。
- [x] 审阅实际接口、状态分区及失败路径，检查 设备源与主机派生成品的全部删除/查询入口是否暗自重试；记录门禁证据，建议以“feat: 实现源产物清理与独立预算”形成独立提交。

#### X8 第一段的阶段性验证（2026-10-05）：清理守卫三件套

`target_set`/`cleanup_member`/`cleanup` 三个具名守卫实现并注册进
`register_outputs_guards`（此前在事件登记声明但未接入，生产内核按
未实现守卫拒绝清理事件提交）。`target_set` 核对 TARGETS_FIXED 清
理/取消/失败分支的动作类型（清理=5、取消=6、失败∈{5,6}）与目标
状态转换；清理成员初始值必须未解析且无限制、不重复；精确清理固定
全部原请求 ID 且保持顺序，范围清理的每个成员属于本动作固定来源的
已登记产物；失败分支不创建成员。`cleanup_member` 核对成员推进要
求目标集合已 FIXED、身份与原请求保持不变、产物身份只能经 RESTRICT
从空值一次确认且等于原请求、直接终态创建只属于已固定集合的事务并
携带本事件为最终事件。`cleanup` 核对终态成员的最终事件等于本事件、
成功依据按结果分类（实际删除=已完成的删除调用或文件缺席事实、已
有完成=产物已 CLEANED、存在性查询=文件缺席事实）、同一产物至多一
个删除中成员。

验证：`test_cleanup_guards.py` 12 项直接事件测试（精确集合匹配与
失配、非法初始值、范围来源成员资格、失败分支零创建、推进需固定、
RESTRICT 一次确认、终态最终事件与三类成功依据、唯一删除处理者）；
单元+outputs 集成 4718 项通过。后续分段：FixCleanupTargets 目标固
定命令、RESTRICT/删除/未知核实编排（cleanup_flow）与双预算、X9 取
消接手，及 test_source_cleanup/test_cleanup_recovery 总验收。

#### X8 第三段的阶段性验证（2026-10-05）：删除编排与独立预算

删除链完整落地：`save_file_presence`（DEVICE_FILE_OBSERVED.PRESENCE
生产者，必须是实际状态变化，原键重送恢复）；`restrict_cleanup_item`
（CLEANUP_CHANGED.RESTRICT 成员未解析→限制中并首次确认产物 + 同事
务 OUTPUT_REGISTERED.OBSERVATION 产物投影→RESTRICTED/PENDING，幂等
按成员状态判定）；`progress_cleanup_item`（限制中→删除中，限制转
不可撤销，投影→RUNNING）；`finish_cleanup_item`（文件缺席观察 +
产物→CLEANED/COMPLETED + 成员 SUCCEEDED/outcome/final_event_id=本
事件同事务提交，成功依据由 cleanup 守卫按同事务先行事件或已完成
的删除调用核对）；`fail_cleanup_item`（FAIL 终态携带公共错误）。
`cleanup_flow.delete_source_file` 串联：限制→删除意图（operations
`delete/<item>` 流程，唯一删除中成员由守卫与部分唯一索引保证）→
契约删除调用→结果事务；效果未知只能用 `exists/<item>` 查询预算核
实——确认缺席按 ABSENCE_CONFIRMED 成功，确认仍在等待预算内重试
（重试等待由结束事实置位，间隔来自尝试配置），查询也未知时等待；
删除预算耗尽按 `delete_attempts_exhausted`（详情 output_id/max/
used）终态失败。删除与查询两条流程预算独立。

验证：`test_source_cleanup.py` 5 项（确认删除全链同事务投影、未知
经查询确认缺席双流程各自一次尝试、仍在时保持删除中并预算内重试
成功、预算耗尽终态失败详情合规、终态幂等不重复副作用）；
`test_cleanup_guards.py` 17 项保持通过；全量回归通过（Python 3.11）。
X8 剩余：范围清理目标固定、动作汇总 finish（cleanup_items_failed）、
X9 取消后责任接手（test_cleanup_recovery.py）。

#### X8 第四段的阶段性验证（2026-10-05）：范围固定与动作汇总收口

X8 收口完成，任务全部勾选：`_FixCleanupTargetsCommand` 补范围清理分
支——`params.source` 请求（`_requested_cleanup_ids` 返回 None）经
`action_dependencies` 解析固定来源，全部来源终态且适用产物处理完成
（`_processing_completed`）后才按来源枚举 `outputs.source_action_id`
创建成员；来源未就绪返回只读 `WAITING`（新增 `CleanupTargetsDisposi-
tion.WAITING`），不创建成员也不写历史；来源没有可清理产物时按事务错
误拒绝（零产物动作收场归调度接线）。新增 `finish_cleanup_action`
（`FinishCleanupAction`/`CleanupActionFinished`/`_FinishCleanupAction-
Command`）：全部成员终态后保存 ACTION_FINISHED，任一 FAILED（含不可
解析目标的直接终态）按 `cleanup_items_failed`（22，详情空对象）汇总
失败，否则成功；父计划状态在全兄弟终态时同事务推进；原键重送核实事
务身份与事实时刻，终态新键按既有事实只读恢复；取消已生效拒绝普通终
态（取消收场归 N 系列）。守卫与重送核实均按固定来源成员资格闭环。

验证：`test_cleanup_guards.py` 26 项（新增范围等待三分区与汇总终态
六用例：失败汇总含父计划完成、全部成功、未终态拒绝、取消拒绝、原键
重送与时刻冲突、终态新键恢复不写新历史）；全量回归通过（Python 3.11
组件单元 2979、集成 3076 另 7 项跳过、根跨组件 34 另 342 subtests）。

#### X8 第五段的阶段性验证（2026-10-07）：清理执行链接入 run 会话

新增 `bootstrap/cleanup_assembly.py` 会话装配并经 `lifecycle._report_
assembly` 注册 `cleanup` 流程；`cleanup_flow.advance_cleanup` 每轮推
进：到时清理动作开始（`StartCleanupAction`——`outputs.py` _Start-
CleanupCommand 保存 ACTION_STARTED.START，计划待执行时同事务保存
PLAN_STATUS START，原键重送与终态/取消分区同取回开始模式）→ 目标
固定（`FixCleanupTargets`；范围来源未就绪只读等待）→ 逐未终态成员
`delete_source_file`（限制、删除意图、双预算与查询核实既有链）→ 全
部成员终态后 `finish_cleanup_action` 汇总。

接线修复四处生产契约缺口：其一，精确清理（output_ids）受理时不建
来源依赖、`source_resolution_state` 为空，而目标固定命令统一要求
FIXED——放宽为仅范围清理（params.source）要求固定来源，精确清理按
原请求 ID 逐项核实。其二，范围来源可靠确认无产物时按规格（output-
cleanup.md 来源及产物状态表）固定空集合并同事务保存动作成功终态与
父计划状态（TARGETS_FIXED 空成员 + ACTION_FINISHED 成功 + 可选
PLAN_STATUS COMPLETE），替换此前的事务错误拒绝；`target_set` 守卫
对应放宽为范围清理允许空成员。其三，删除与查询请求携带目标身份与
设备文件定位（`cleanup_item_id`、`file_id`、`identity_key`、
`locator`），驱动据此定位目标文件并回填成员身份观察；主机派生成品
成员经本地文件协作者删除 `staging/derived/` 对应成品（2026-10-08
接入，见 X8 收口记录），绑定未装配设备时成员保持等待
（target_unbound）。其四，删除收场证据由 `operation_returned` 改为操作
专属的 `delete_returned`——单相机驱动同时声明控制与删除时两者共用
(type,version) 会冲突，沿 read_returned/stop_returned 裁决补齐；查询
收场维持 file_presence。配置接入 `devices.<id>.cleanup.delete_timeout_s`
与 `query_timeout_s`（正秒数），删除/查询尝试上限取自 `cleanup.max_
delete_attempts`/`max_query_attempts`；第一版单相机假设下装配首个同
时声明删除与查询能力的设备。

| 关键裁决 | 内容 |
| --- | --- |
| 零产物范围清理按成功收场 | 来源终态且处理完成、可靠确认无正式产物时动作 `succeeded`（“无产物需要清理”），不创建清理成员；区别于指定动作类型不产生产物的受理失败。 |
| 精确清理不要求来源状态 | output_ids 模式没有来源引用，执行时按 ID 核实存在性；范围清理维持受理时 FIXED 来源。 |
| 删除调用证据专属命名 | `delete_returned`（op=delete）与控制的 `operation_returned` 区分，证据注册表 (type,version) 全局唯一。 |
| 成员推进等待不解释为失败 | 未装配设备的目标成员保持等待；主机派生成品经本地删除链路推进（与设备调用同形、双预算共用）。流程层的成员推进仅对事务未完成或事实不一致分区报错。 |

验证：会话级集成 `tests/integration/bootstrap/test_cleanup_flow.py` 4 项
（精确清理一次删除成功：成员 SUCCEEDED/DELETED、产物 CLEANED、计划完
成、请求携带成员与文件身份；范围来源失败无产物：零产物成功收场且无
清理成员、无删除调用；删除持续错误且查询确认在场：3 次删除耗尽后成员
FAILED（delete_attempts_exhausted，attempts_used=3）、动作按 cleanup_
items_failed 失败；删除效果未知经查询确认缺席：ABSENCE_CONFIRMED 成功
且设备文件缺席）；既有 `test_source_cleanup.py`/`test_cleanup_recovery.py`
证据契约同步 delete_returned。全量回归：单元 3296、apps 集成分目录
3395（outputs 1788、bootstrap 74、capture+scheduling+operations 507、
cancellation+devices+contracts 124、acceptance+history+host_files 382、
logging_runtime+persistence+reporting+session 520）、根集成混跑 3395
另 6 跳过、五检查器通过（Python 3.11）。

X8 剩余（2026-10-08 收口）：主机派生成品成员的删除链路已接入——
清理成员产物由提升中间文件承载（outputs.device_file_id 为空且
intermediate_file_id 非空）时，执行链接入本地删除协作者：装配层提供
`HostArtifacts`（staging 工作根的本地删除与存在性查询，观察与设备调
用同形、file_absent/file_presence 证据共用），`delete_source_file` 按
成员目标在设备驱动与本地协作者间分派，删除与查询双预算、重试间隔、
取消收场与汇总全部共用设备链。清理成功的文件缺席事实由中间文件
`cleanup_state` 承载：`finish_cleanup_item` 对此类成员同事务保存
`INTERMEDIATE_FILE_CHANGED.CLEANUP_RESULT`（cleanup_state→COMPLETED，
与成员终态同事务先行供成功依据核对）；配套扩展事件目录（该分支
before 允许 retention_state=3 的提升承载、新增 cleanup_state 1→4 转
换边）与中间文件表约束（`retention_state IN (2, 3) OR cleanup_state
= 1`）。X11 自动清理通道边界不变（classify 对 PROMOTED/HANDED_OFF
仍不归自动清理，历史扫描候选仍限 RELEASABLE），显式清理不占自动清
理的历史额度。

验证（先红后绿）：真实 run 会话用例两枚（`test_cleanup_flow.py` 的
`TestHostArtifactCleanup`）——本地删除成功链（成品文件从
staging/derived 消失、成员 SUCCEEDED/DELETED、中间文件 COMPLETED、
产物 CLEANED 投影、动作与计划成功、设备驱动零调用）与删除结果未知
经查询确认缺席链（ABSENCE_CONFIRMED、本地删除仅一次、中间文件
COMPLETED）。全量回归通过（单元 3353；集成 outputs 1788、bootstrap
93、cancellation/capture/persistence/history 419、其余 1155；根跨组
件 55+342 subtests；五检查器全绿含事件目录同步，Python 3.11）。

多设备清理动作仍属第一版单相机假设（装配扩展时按设备路由成员）。

### X9 清理取消、接手及原结果保持

**预计文件：** `apps/camctl/src/camctl/outputs/cleanup.py`、`apps/camctl/src/camctl/outputs/qualification.py`；测试为 `apps/camctl/tests/unit/outputs/test_cleanup_recovery.py` 和 `apps/camctl/tests/integration/outputs/test_cleanup_recovery.py`。

**接口与依赖：** 提供 `apply_cleanup_cancel(facts: CleanupFacts) -> CleanupCancelChanges`、`merge_cleanup_requests(facts: CleanupCoordination) -> CoordinationDecision`；Coordination 含全部有效项和原处理者。前置交付：X8、N3；已有真实删除责任。

- [x] 编写失败用例。建立 `test_later_cleanup_preserves_old_failure`，请求 A 查询耗尽失败，请求 B 后来确认不存在，`assert a.result == original_failure` 且 B 成功/产物清理事实更新。取消未发删除解除相应限制，可能已删保持限制；在途调用实际结束前仍跟踪，取消发起者结束不丢责任。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_cleanup_recovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。目标独立收场，保留原请求结果及预算；后续有效请求可以接手仍适用工作，不能重开旧项。正常及重启用同一候选时间排序。
- [x] 再运行上述命令，要求全部 PASS，并核对 清理取消和读取保护按完整分区恢复，所有独立结果不被后续成功覆盖。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_cleanup_recovery.py -q`，真实 SQLite、并发取回和多个清理请求，固定取消、删除、查询及结果保存的竞争位置。
- [x] 审阅实际接口、状态分区及失败路径，检查 来源、清理处理者、取回保护和逐项汇总的共同不变量；记录门禁证据，建议以“feat: 实现清理取消与责任接手”形成独立提交。

#### X9 的阶段性验证（2026-10-05）：取消收场与责任接手

X9 完成，任务全部勾选。仓储新增 `cancel_cleanup_item`（CLEANUP_CHA-
NGED.CANCEL）：未解析/待删除成员解除限制保存取消（PENDING_DELETE 的
ACTIVE→RELEASED，未解析保持 NOT_ESTABLISHED），命令核对从未发出删除
（不存在本项 DELETE_FILE 尝试）才允许解除；删除中成员的收场取消必须
携带 `delete_unconfirmed` 或 `file_delete_failed` 并保留不可撤销限制；
成员取消要求所属动作取消请求已生效。清理汇总投影改为派生
（`_CleanupItemCommandMixin._derive_projection`）：按本产物全部清理成
员自上而下判定 completed/running/pending/incomplete（错误来源按
final_event_id 降序、同号按 id 降序选择，本事务终态事件必然最晚）/
canceled/not_requested，restrict/progress/finish/fail/cancel 五个生产
者共同维护 `outputs.availability`、`cleanup_status`、`cleanup_error_json`
（结构化错误 `{code, stage, details}`），无实际变化时不生成投影事件。
`restrict_cleanup_item` 放宽接手前置：允许在受限、未完成汇总（此前失
败留下限制）或已清理产物上建立本项限制，按原请求目标装载未确认成员
的产物行；`finish_cleanup_item` 允许待删除成员在无调用依据下保存
ALREADY_CLEANED（产物已 CLEANED）或 ABSENCE_CONFIRMED（文件已缺席）。

编排（`cleanup_flow`）以两个纯决策表驱动：`decide_cleanup_cancel`（终
态保持/未发出解除/在途跟踪/收场后成功·未知·仍在）与 `decide_cleanup_
entry`（复用完成/先核实/直接删除；未决删除按尝试编号与可靠查询观察
的先后判定）。`delete_source_file` 重构：取消检查先于限制建立；接手
前先核实本产物未决的删除效果（查询确认缺席直接成功、确认仍在转入删
除、查询额耗尽按 `file_query_attempts_exhausted` 终态失败——修正此前
误用 `delete_unconfirmed` 且详情键不合规的死代码）；在途删除调用结束
后取消才生效时按实际结论收场（缺席成功、可靠在场按 `file_delete_fai-
led`、其余 `delete_unconfirmed`）；查询预算耗尽的详情按实际尝试计数。

验证：单元 `test_cleanup_recovery.py` 15 项（两张决策表全分区）；集
成 6 项——`test_later_cleanup_preserves_old_failure`（A 查询耗尽失败保
持原错误且产物进入 incomplete 保留结束原因，B 固定后接手不重开旧项、
先用自己的查询额核实确认缺席成功，产物完成且汇总错误清空，A 原失败
不变）、取消未发删除解除限制（成员 CANCELED/RELEASED、产物取消且可
用性恢复）、未解析成员取消不建限制、删除效果未知取消保留不可撤销限
制并按 delete_unconfirmed 收场（产物 incomplete 保留错误）、可靠确
认仍在按 file_delete_failed 收场、在途调用跟踪到实际结束（挂起替身
在取消到达后放行，尝试结果保存后按未知收场）。X8 语义随规格修正一
处：删除效果未知且查询也未知时，重试先核实而非直接重删（
`test_source_cleanup.py` 对应用例改为查询确认在场后删除预算耗尽）。
全量回归：组件单元 2979、集成 3076 另 7 项跳过、根跨组件 34 另 342
subtests、check-protocol、check-report-dependencies、check-doc-links
2916 通过（Python 3.11）。

X9 调度侧剩余（取消动作自身终态、跨动作候选排序与读取保护竞争）随
N1—N6 与 X10 剩余接线推进。

### X10 自动预览及统一发布汇总

**预计文件：** `apps/camctl/src/camctl/outputs/previews.py`、`apps/camctl/src/camctl/outputs/service.py`；测试为 `apps/camctl/tests/unit/outputs/test_previews.py` 和 `apps/camctl/tests/integration/outputs/test_previews.py`。

**接口与依赖：** 提供 `select_previews(facts: PreviewFacts) -> SelectionSnapshot`、`decide_obtain_finish(facts: ObtainFacts) -> ObtainDecision`；facts 包含全部固定来源及逐项阶段，不能用缓存局部子集汇总。前置交付：X2—X7、C6、Q1/Q4、N1/N2。

- [x] 编写失败用例。在 `test_publish_waits_for_complete_selection` 中一来源已准备、另一仍未选，`assert may_publish is False`；所有来源和合法项完成准备/失败后才统一发布成功项，整次有失败仍保留成功交付。自动预览已开始兼容读取可继续，不兼容时本次读取结束再优先到时拍摄；下一文件和重试等待让路。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_previews.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。用明确预览关联选择，自动与手动共用预览选择方式；取消联动只由 N1/N2 决定，直接取消取回不反向取消拍摄。汇总全部适用结果及父状态同事务保存。
- [x] 再运行上述命令，要求全部 PASS，并核对 没有早发、扩大筛选、回退替换或按目录配对。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_previews.py -q`，真实三种拍摄、部分取回及取消联动，按领取契约控制文件位置；报告保留成功/失败项，真实 C 组合归 I5。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部来源、部分成功和自动预览的发布/取消条件；记录门禁证据，建议以“feat: 实现自动预览与取回汇总”形成独立提交。

#### X10 的阶段性验证（2026-10-05）

X10 完成预览选择复用确认与统一发布汇总判定，任务 checkbox 保持未勾：预览选择由 `sources.py` 既有 `SelectionMode.PREVIEW` 路径共同实现（`select_for_original` 决策表：预览缺失逐项失败且不自动传修复成品；有修复成品时两侧完整大小必须已知，否则按不可确认逐项失败；修复成品小于或等于预览选修复成品（相等选修复成品）、大于预览选预览；未知存在性保留身份待确认）——自动与手动取回共用同一路径，不另建规则。新增 `outputs/obtain_summary.py`（命名替代建议的 previews.py/service.py：预览选择已在 sources.py，避免重复规则）提供 `obtain_item_stage`（条目与交付状态→准备阶段：等待资格或重试间隔→PENDING，准备或校验中→PROCESSING，可靠准备完成/发布中/已发布→PREPARED，最终失败含取消撤回→FAILED；待判定条目计入来源事实不进准备汇总，未知状态拒绝解释）与 `decide_obtain_finish`（开始发布条件决策表：任一来源判定未固定→WAIT_SOURCES；显式条目待判定→WAIT_ITEMS；条目等待准备、处理或重试→WAIT_PREPARATION；全部确定→READY_TO_PUBLISH 并计数成功与失败，整次有失败仍保留成功交付，无成功文件不创建交付由调用方执行；来源等待优先于条目与准备等待）。

| 关键裁决 | 内容 |
| --- | --- |
| 来源判定完成 ≡ 选择行 FIXED | 选择固定本身要求来源终态且产物判定完成（未完成来源 is_fixed=False 不保存固定）；汇总事实以固定性为来源侧输入。 |
| PREPARED 含发布中与已发布 | 发布满足条件后逐文件进行，已开始或已完成的发布不使汇总回退等待。 |
| 终态条目优先于交付观察 | 条目 FAILED/CANCELED 直接归最终失败，不再按交付状态解释。 |

验证：单元 27 项（决策表全分区含命名用例 `test_publish_waits_for_complete_selection`、映射全状态、未知拒绝、事实校验，位于 `tests/unit/outputs/test_obtain_summary.py`）；集成 3 项（`tests/integration/outputs/test_obtain_summary.py`：真实选择与准备事务后一项真实准备完成、一项最终失败→READY(1,1)，经真实 `publish_delivery` 发布成功项且重判仍 READY；来源未固定→WAIT_SOURCES；交付 PREPARING→WAIT_PREPARATION）；全量回归单元 2929、集成 2984+7 skip、根 34+342、check-protocol、check-report-dependencies、check-doc-links 2914 通过（Python 3.11）。

X10 仍剩余：取回动作终态汇总事务（发布汇总与父计划状态同事务保存）、读取与拍摄让路的调度接线（设备兼容性判定，Q6/I5）、取消联动消费（N1/N2）、真实三种拍摄与部分取回组合（I5）。

#### X10 第二段的阶段性验证（2026-10-05）

第二段完成取回动作终态汇总事务，任务 checkbox 保持未勾：`repositories/outputs.py` 新增 `FinishObtain`/`ObtainFinishResult` 与 `_FinishObtainCommand`（仓储入口 `finish_obtain`）。命令从已保存的选择、条目与交付行装载汇总事实（与调度接线前的参考装载规则一致：选择按依赖归属、待判定条目计数、已判定条目经交付状态映射阶段），按 `decide_obtain_finish` 判定后才保存终态；成功交付必须全部已发布（PREPARED/PUBLISHING 拒绝），父计划状态与动作终态同事务推进（PLAN_STATUS 仅当全部兄弟动作终态）。原键重送核实事务身份后恢复首次结果；终态后新键按既有事实恢复，取消请求已生效的动作拒绝。

| 关键裁决 | 内容 |
| --- | --- |
| 部分失败整次失败 | 任一逐项最终失败把动作置 `failed`（`obtain_items_failed`，details 为空对象，具体失败项由条目 `result.failures` 表达），成功交付保留且已发布状态不回退；全部成功才 `succeeded`。 |
| 无成功文件也失败 | 没有成功交付时不创建交付文件，动作按同一错误失败（prepared=0、failed>0 可保存终态）；既无成功也无失败条目（无可汇总条目）拒绝保存。 |
| 终态次序 | 汇总确定先于发布完成检查：决策等待分区（来源/条目/准备）逐项拒绝，发布完成（交付 PUBLISHED）是保存终态的独立前提。 |
| 恢复 | 原键重送核对首事件为完成登记、事实时刻与动作身份；恢复路径重新装载事实并要求与终态一致，不一致按身份冲突拒绝。 |

验证：`test_obtain_summary.py` 集成新增 8 项（成功终态与新键恢复、原键重送与时刻冲突、部分失败置 failed 且成功交付保留、全败无交付、未发布拒绝、汇总未定拒绝、取消拒绝、兄弟终态齐备时父计划同事务完成）；全量回归单元 2929、集成 2992+7 skip、根 34+342、check-protocol、check-report-dependencies、check-doc-links 2914 通过（Python 3.11）。

X10 仍剩余：读取与拍摄让路的调度接线（设备兼容性判定，Q6/I5）、取消联动消费（N1/N2）、真实三种拍摄与部分取回组合（I5）。

#### X10 第三段的阶段性验证（2026-10-05）：设备让路与取消联动调度

`outputs/dispatch.py` 提供统一设备工作决策层：`decide_device_work` 按
拍摄优先规则决定每设备本轮计划——持机会读取遇到到时拍摄时先让路
（本轮结束本次读取，拍摄下一轮派发）；到时拍摄优先于新读取授予；
设备被占用中拍摄活动阻塞时读取等待；空闲设备授予合格读取工作，
已在途读取继续占用机会不重复授予。第一版设备兼容性裁决：拍摄与
读取不并行（预览读取与拍摄的驱动级兼容声明待 D5 真实驱动适配）。
`plan_device_work` 从当前投影装配：到时拍摄按设备分组（未取消、
执行中、占用中活动）；在途读取按 `file_copies.slot_device_id` 归属
设备；合格读取候选按源设备文件的观察动作绑定设备，排除已取消取
回动作与已取消/撤回交付（取消联动消费：取消生效的工作不进入普通
授予，由 N1—N6 取消收场流程推进）。

验证：单元 `test_device_work.py` 5 项（空闲授予、到时拍摄优先、持
机会读取让路、拍摄占用阻塞、无拍摄时在途读取继续）；集成 2 项
（真实 SQLite 分组与让路、拍摄终态释放且让路读取取消收场后空闲
设备授予剩余候选）。全量回归通过（Python 3.11）。

X10 剩余：真实驱动兼容性声明（D5）、三种拍摄与部分取回的跨组件
组合（I5）；统一计划与执行协程的完整 run 循环装配归阶段 2/I3。

#### X10 第四段的阶段性验证（2026-10-07）：取回执行链接入 run 会话

新增 `outputs/obtain_flow.py` 取回执行编排并经 `bootstrap/obtain_assembly.py`
接入生产 run 会话（`lifecycle._report_assembly` 注册 `obtain` 流程）。
会话每轮 `advance_obtain` 依次推进：到时取回动作开始（`start_obtain_action`
——受理时来源已固定的依赖在 ACTION_STARTED 事务内初始化 PENDING 选
择行，`selection_initialization` 守卫强制每个依赖恰好一条）→ 来源解
析（`ResolveSources`）→ 选择计算与固定（`read_selection_request`+
`select_outputs`+`fix_selection`，来源未完成保持等待）→ 逐项建档
（`grant_file`：候选资格、目标中间文件与 delivery 同事务）→ 读取推
进（`_advance_reads` 消费 `plan_device_work`：设备有到时拍摄或让路工
作时本轮不推进；新授予与在途读取经真实机会事务与 READ_FILE 尝试预算
推进）→ 尝试执行（prepare、open_session、分段 transfer、complete_copy
内部完整性校验后 PREPARED）→ 尝试收场（成功释放读取机会并清等待；
预算耗尽先以 `read_attempts_exhausted` 闭合流程行再保存交付终局失败
`fail_read_delivery`——要求流程 FAILED 且无在途尝试——并释放机会）→
统一发布（`decide_obtain_finish` 判定后逐 PREPARED 交付 `publish_delivery`
再 `finish_obtain` 与父计划同事务终态）。

`DeviceWork` 补 `resume_reads` 字段修复契约缺口：此前“无拍摄工作的在
途读取继续占用机会”只以空计划表达，消费方无从发现需继续推进的读取；
现在 `decide_device_work` 对无拍摄工作的在途读取显式返回 resume 计划，
拍摄占用分支仍为全空（读取等待）。错误码登记 `read_attempts_exhausted`
（stage=source_read，details 携带 delivery_id、max_attempts、
attempts_used，从已保存流程行读取保证与事实一致）。

本段顺带修复同类生产缺陷“动作身份冒充设备活动主键”共九处：既有测
试每库首个拍摄动作恰好活动主键等于动作主键而从未暴露，第二个拍摄
动作的活动观察、占用释放、活动收场、结果核实、等待安排、停止与核
实意图全部装载失败且异常被 `dispatch_ready` 记录吞掉、动作永卡执行
中。统一改为按 `action_id` 查询唯一活动行（`load_activity_of_action`），
真实主键参与行更新、所有权映射、状态行与原键重送校验。

| 关键裁决 | 内容 |
| --- | --- |
| 在途读取的继续推进由决策层显式表达 | `resume_reads` 让消费方无歧义发现持机会读取；不改动 grantable 与让路分支语义。 |
| 预算耗尽的两步闭合 | 最后一次失败尝试先以 run_finish=FAILED 闭合流程行，再按流程事实保存交付终局失败并释放机会；中间态（流程 FAILED 但交付未失败）由下一轮幂等补齐。 |
| 读取收场证据类型 `read_returned` | 沿 stop_returned/results_returned 惯例按操作专属命名，避免与 control 的 operation_returned 重复登记。 |
| 交付展示名按产物来源命名 | `<原文件主干>-<来源计划名>-<来源动作名>.<原扩展名>`；修复成品加 repaired 前缀；扩展名取 original_name 合法后缀，不合规兜底 bin。 |

验证：会话级集成 `tests/integration/bootstrap/test_obtain_flow.py` 4 项
（正常链：跨计划取回发布到 ready 且字节一致、display_name 正确；显式
实例不存在：来源解析失败保存动作终态且无交付；耗尽链：3 次尝试全部
失败后交付 FAILED、错误详情 attempts_used=3、机会释放；让路链：录像执
行期间零读取会话，录像终态后读取继续推进到发布，录像原片媒体链拷贝
与取回读取共用端口）；单元 `test_device_work.py` 6 项覆盖 resume 分支；
全量回归单元 3296、apps 集成分目录 3391（outputs 1788、capture 199、
bootstrap 70、scheduling+operations 308、cancellation+devices+contracts
124、acceptance+history+host_files 382、logging+persistence+reporting+
session 520）、根集成混跑 3391 全绿（Python 3.11）。

X10 剩余已于 2026-10-08 全部收口（见下方 X10 收口记录）；取消收场
消费（N 链）与清理动作执行入口分别于 2026-10-08、2026-10-07 接线完成。

#### X10 收口（2026-10-08）：驱动兼容性声明与三种拍摄部分取回组合

设备兼容性判定从硬编码裁决改为驱动声明：`DriverDeclaration` 新增
`capture_read_parallel_supported`（缺省 False——未验证并行控制的驱动按
拍摄与读取不并行的保守方式让路，与第一版裁决一致），`outputs/dispatch.py`
的 `DeviceFacts`/`DeviceWork` 增加 `capture_read_parallel` 事实与回显、
`decide_device_work` 按声明分支、`plan_device_work` 接受声明并行的设备集
合；装配链 `session_obtain_assembly` 从登记声明解析进 `DeviceReadAssembly`，
`_advance_reads` 以设备工作计划的自述并行标志判断读取推进条件。

| 关键裁决 | 内容 |
| --- | --- |
| 并行时到时拍摄与新授予同轮共存 | “拍摄优先于尚未开始的冲突读取”表达为同轮内拍摄先派发的顺序，不推迟读取授予——录像执行中活动持续出现在到时集合，若同轮不授予新读取，兼容设备上整段录像期间读取永远无法开始，与规格“当前相机支持拍摄期间读取其他已经完成的文件”矛盾。 |
| 一次一份拷贝互斥不因并行声明改变 | 在途读取继续占用机会期间不重复授予（file-copy.md 第一版同一相机一次一份拷贝），并行只解除拍摄与读取之间的阻塞。 |
| 未声明并行保持让路 | 缺省 False 是保守方向合并：未验证并行控制的驱动按不支持并行处理，让路、占用阻塞与重试等待规则保持原样。 |

三种拍摄与部分取回的跨组件组合（X10 集成验收、I5 组合面）由
`tests/integration/test_camctl_output_roundtrip.py` 的
`test_three_capture_kinds_partial_obtain` 覆盖：照片、录像与延时同计划
顺序执行（单设备互斥），照片与延时产物被两个取回动作分别取回交付
（ready 两份字节与各自来源一致），录像产物保持登记不建交付，报告表
达五个动作全部成功。

组合用例首次运行暴露并修复一项既有生产缺陷（I5 第四条链同族）：
`_stop_call` 把驱动停止确认观察直接交 `runtime.finish`，观察身份与操作
目标（设备活动主键）不符时 `OutcomeValidationError` 上抛、停止流程与在
途尝试遗留执行中、run 会话永不退出——启动调用路径（`_control_call`）
已于 I5 第四条链接 invalid_device_result 收场，停止调用路径漏网。修复
同模式：校验拒绝按调用失败保存尝试终局并结束停止流程；契约测试新增
`test_invalid_stop_observation_settles_stop_attempt`（停止观察身份不符
→ 尝试终局失败、流程 FAILED、无遗留）。跨组件替身的停止观察身份同步
按执行中活动行查库对齐（与启动调用的已派发待响应对齐同一策略，停止
请求同样不携带任务身份，属 D5 已记录的下发通道接缝）；替身另增环境
开关的端口调用日志（`CAMCTL_TEST_TRACE_CALLS`，默认关闭）作为跨组件
诊断基建。

验证：单元 `test_device_work.py` 9 项（并行四分支：到时与在途共存、占
用中授予、到时与新授予同轮、在途互斥保持）；调度集成 3 项（真实投影
下并行共存与未声明对照）；会话级 `test_obtain_flow.py` 5 项新增并行链
（照片完成后读取首败进入重试等待、等待期间录像到时启动、重试在录像
执行中并行授予并推进到发布、动作成功且录像仍执行中、第二会话完成录
像）；capture 契约 15 项含停止收场新用例；跨组件 `test_camctl_output_
roundtrip.py` 5 项。全量回归（2026-10-08，Windows 开发机，uv CPython
3.11）：单元 3357、apps 集成分目录 3458+6 skip（capture/outputs/
bootstrap 2094+1、其余 1364+5）、根 `tests/integration` 56+4 skip+342
子测试、五项仓库检查全部通过。


### X11 中间文件生命周期与有限维护

**预计文件：** `apps/camctl/src/camctl/outputs/work_files.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_work_files.py` 和 `apps/camctl/tests/integration/outputs/test_work_files.py`。

**接口与依赖：** 提供 `classify_work_file(facts: WorkFileFacts) -> WorkFileDecision`、异步 `clean_work_files(scope: WorkFileCleanupScope) -> WorkFileCleanupResult`；事实含用途、归属、可恢复责任、真实任务状态及持久化游标。前置交付：F1/F2、X4、S1；先提供中间文件用途和生命周期端口，C8 随后消费。

- [x] 编写失败用例。在 `test_work_file_cleanup_does_not_restart_action` 中取消后完整 staging 副本删除失败，`assert action.status == original_terminal_status` 且不会单独 needs_run。可恢复输入保留、已成正式文件不清理、实际操作未结束不删；首次新文件清理与历史扫描预算分别计数。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_work_files.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。（初始红：work_files 模块不存在。）
- [x] 实施本任务。中间文件建立时就保存用途/归属；先可靠保存取消或失败，再等实际操作结束后清理。历史清理按批量、每 run 上限及可靠游标有限推进，失败留后续正常 run，不原地循环。
- [x] 再运行上述命令，要求全部 PASS，并核对 历史及新文件清理遵守既定不同预算，诊断可靠保存后允许会话收尾。
- [x] 审阅实际接口、状态分区及失败路径，检查 半成品、完整取消副本、检查输入和修复输出所有生命周期；记录门禁证据，建议以”feat: 实现中间文件有限维护”形成独立提交。

X11 的阶段验证：`outputs/work_files.py` 提供 `classify_work_file`（决策表：PROMOTED/HANDED_OFF 不归自动清理；归属交付或动作未终态保留；归属终态但目标拷贝仍有 RUNNING 尝试时先收场；归属终态且操作停止才可清理，REQUIRED 由编排先释放；RELEASABLE+COMPLETED 不重复）、`WorkFileLimits`（`cleanup.work_file_batch_size`/`cleanup.work_file_limit_per_run` 均 ≥1 整数、布尔拒绝）、`CleanupScan`（固定上界、剩余额度、单轮绕回：绕回段上界为本次起始继续位置，批上限不越过剩余额度）与 `clean_work_files`/`clean_one_work_file` 编排（首次入口不推游标不占历史额度；同一文件被两个入口发现时经会话登记复用本次实际结果；意图→观察→删除→结果顺序，观察不可靠或删除失败按公共错误保存后继续其余记录）。仓储新增 `save_retention_release`（LIFECYCLE：REQUIRED→RELEASABLE 核对归属交付 PUBLISHED/FAILED/CANCELED 留存或失败取消、动作终态及无 RUNNING 尝试；HANDED_OFF→RELEASABLE 核对交付 PUBLISHED/WITHDRAWN；已 RELEASABLE 幂等只读）、`save_cleanup_intent`（CLEANUP_INTENT：PENDING/FAILED/UNKNOWN→RUNNING，RUNNING 或 COMPLETED 拒绝）、`save_cleanup_result`（CLEANUP_RESULT：进入 COMPLETED 清除错误、FAILED 保存 `work_file_delete_failed` 并关联交付；与 CLEANUP_CURSOR_MOVED 游标推进同事务）、`save_cleanup_checked`（仅游标推进）与只读 `load_work_file_state`/`next_cleanup_candidates`/`max_cleanup_candidate_id`/`load_cleanup_cursor`。`save_publication` 补齐同事务 HANDED_OFF（发布确认后副本所有权归交接位置；留存工作副本经 4→2 释放清理）。守卫扩展 CLEANUP_INTENT（只推进清理状态且要求已释放）、CLEANUP_RESULT（完成不带错误、失败错误经公共登记校验）并注册 cursor 守卫。删除失败按归属保存公共错误：交付副本保存 `work_file_delete_failed` 并关联交付，动作归属文件（录像输入/处理临时/修复输出）保存 `action_work_file_delete_failed` 并携带文件身份（2026-10-05 登记）；两种归属的失败结果都可靠保存，未决责任留给后续正常运行重新核实。单元 48 项；集成 21 项覆盖核心不重开用例（删除失败动作与交付终态保持、无新动作收场事件、失败后重试成功）、归属活跃与未停尝试释放拒绝、PROMOTED 不清理、外力改变归属保留、文件已删补记完成、观察失败不冒充、额度先尽续查、绕回单轮、保留候选占额推进、批上限不越剩余、首次清理不占历史额度、同 run 复用不二次尝试、动作归属失败保留责任且其余记录继续、发布 HANDED_OFF、PUBLISHED 留存副本释放清理、四组原键恢复与输入不符拒绝、守卫接受真实事件并拒绝缺错误/带错误完成/意图改保留状态。

### X12 产物链及历史组合验收

**预计文件：** `apps/camctl/src/camctl/outputs/service.py`、`apps/camctl/src/camctl/outputs/cleanup.py`；测试为 `apps/camctl/tests/integration/outputs/test_outputs_contract.py`。

**接口与依赖：** 使用 X1—X11、C1—C8、N1—N5、H1—H6 和 R1—R8 的真实接口。前置交付：X1—X11、C1—C8、N1—N5、H1—H6、R1—R8；H7/N6 为同层验收，不作为前置。

- [x] 编写失败用例。在 `test_output_lifecycles_are_independent` 中源后来清理、原 delivery 已准备，`assert delivery_can_continue is True`；同 output 两个新请求产生独立交付，同请求重送不产生新交付。所有来源/资格/拷贝/清理/交接边界中断后核对同 H 的事实及固定报告字节。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_outputs_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。（初始红：冻结报告用例在 `public_projection.py` 的 `failure_union` 占位处失败——取回失败汇总投影未实施，属组合暴露的真缺口；跨窗口用例随后在报告 2 缺失败条目处失败——交付事实装配不受增量子集限制的规则未落实。）
- [x] 实施本任务。逐项映射 output-verification 和数据库验收 01—51、69—72 的适用条目，执行真实仓储、文件、受管调用和报告组件组合；设备使用契约替身，真实 C 领取的跨组件验证由 I5 承接。
- [x] 再运行上述命令，要求全部 PASS，并核对 所有来源和文件生命周期闭合，没有仅正常路径的通过声明。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部错误、未知、取消、预算、清理及跨请求结果保持；记录门禁证据，建议以“test: 验证产物取回清理完整闭环”形成独立提交。

X12 验证记录（2026-10-06）：

**组合形态。** 单计划四动作（拍摄→两个取回→范围清理）经真实受理、调度、拷贝、交付、清理与报告组件闭环运行：受理 `accept_input` 建立计划；`start_action`+`dispatch_ready` 用契约驱动替身（`_ActivityDriver`+`ResultsDouble`）完成拍摄并登记正式产物；两个取回各自经 `resolve_sources`（执行期固定来源）、`select_outputs`+`fix_selection`、`grant_file`（共同建档）、分段拷贝与 `complete_copy` 到 PREPARED；范围清理经 `fix_cleanup_targets`+`delete_source_file`（契约删除替身）真实删除源文件；交付经 `publish_delivery` 发布；`finish_obtain` 保存取回终态。设备交互全部使用受接口契约约束的替身；普通动作（取回/清理）的 PENDING→RUNNING 开始命令不存在于生产入口（`StartActionCommand` 只适用拍摄），该前提与 X2—X11 一致以投影事实表达，命令装配归阶段 2/I3/I5。清理来源采用与拍摄同计划的 `action_name` 引用（受理即时 FIXED）；精确 `output_ids` 清理与跨计划清理的执行期来源固定同为 run 循环装配范围，组合中不伪造命令。

**组合暴露的缺口与实施。** ① `failure_union`（取回失败汇总）公开投影未实施（原占位直接抛规则错误）：按登记实施于 `contracts/public_projection.py`——分支沿登记关系链正向展开并保留到达路径（`_relation_paths`），分支投影按自身 `when` 过滤（不满足跳过），条目按（固定依赖 ID、分支序、分支身份）排序，同一分支重复返回同一身份按 `state_database_error` 暴露不静默去重，空集输出空数组。② 跨表列解析原只支持"根行沿声明关系正向一跳"，而 `source_failure`/`item_failure` 需要从子行沿外键反向（及多跳）读父表列：`_related_column_source` 改为在投影声明关系图上枚举双向路径，可选外键为空按 SQL NULL 参与条件与取值，锚点存在而行缺失仍按关联事实缺失报错；既有正向单跳投影行为不变（delivery/automation/device_execution 等全部回归通过）。③ 报告事实装配缺口：取回失败汇总的查询范围是"该取回在 H 的全部固定来源、明细及关联交付，不受本次报告的交付增量子集合限制"，而交付行此前仅随窗口入选合并——前窗建档（或失败）的交付在后窗报告中会缺行：`reporting/generation._plan_facts` 对动作恢复行中 `obtain_items.delivery_id` 引用的交付逐个补齐恢复（含承载产物）。④ 测试辅助教训：`_mark_running` 裸 UPDATE 不写历史事件，在冻结边界之后使用会污染旧边界恢复（逆向恢复无法撤销无事件的投影变化，冻结字节用例曾因此泄漏 clean 动作 running 状态）；改为幂等且只在相关固定边界读取之前表达。

**测试证据。** 投影级（`tests/integration/contracts/test_public_projection.py` 新增 6 用例：无失败空数组、来源失败读来源身份与登记错误、条目失败读产物关联与错误、交付失败读交付/产物/错误、依赖→分支→身份排序、重复交付身份规则错误；先红后绿）。组合级（`tests/integration/outputs/test_outputs_contract.py` 4 用例）：独立生命周期（两个新请求独立交付、同请求重送复用不新建、源清理后两个已准备交付继续发布 PUBLISHED、全部发布后两个取回保存成功终态）；清理阻止新取回而历史保持（固定 H 的 OUTPUT_FILE 在场事实不变、当前边界转缺席、新请求选择保存 output_unavailable 失败条目、建档 REJECTED_FINAL 不新建交付）；冻结报告字节（清理、发布、重送推进后按同一冻结依据重建字节完全一致）；跨窗口失败汇总（前窗终局失败的交付经发布失败链真实落库，报告 1 窗口内含失败条目，报告 2 窗口外交付不入增量子集而失败汇总仍完整、交付字段省略）。全量回归（Python 3.11）：单元全量 3292 通过（连续两轮）；集成 contracts+reporting 378、outputs 1788+1 skipped、bootstrap 64（复跑全绿）、acceptance 226；根 tests/integration 34 项+342 子测试；客户端单元 488 与 typecheck 通过；五检查器（database-spec、report-dependencies 23 投影 99 字段映射、protocol、doc-links 2930 链接、event-transitions）全部通过。环境漂移按既定协议定性（位置漂移+涉事测试单独通过+复跑全绿，无代码问题）。

**验收条目映射。** 数据库验收 29—36 属 ADB 调用收场区域（host-demo/接入模块边界），由 R 轮次与 I5 承接，不在 X12 适用范围。其余适用条目的归属：01—10（等待与来源结束）由来源等待与选择轮次的 `test_sources.py`、`test_selection_*` 系列验证，组合覆盖主路径（执行期固定来源、PENDING→FIXED、取消保持）；11—16（读取流程、共同建档、竞争顺序、依赖解除、提交中断、三路径恢复）由 `test_copy_segments.py`、`test_copy_resume.py`、`test_copy_creation_slot.py`、`test_read_recovery*.py`、`test_read_associations.py`、`test_grant_reuse.py`、`test_product_competition.py` 验证，组合覆盖真实建档→拷贝→PREPARED→依赖解除链与冻结字节；17—28、37—39、44—45、47（清理限制、依赖触发、间隔预算、接手、取消收场）由 `test_cleanup_guards.py`、`test_cleanup_intervals.py`、`test_cleanup_recovery.py`、`test_source_cleanup.py`、`test_member_guard.py`、`test_member_lookup_evidence.py` 验证，组合覆盖范围清理固定→真实删除→产物不可用→阻止新取回；40—43、46、48—50（产物汇总状态表、错误选择、`final_event_id` 规则）由 `test_output_family.py`、`test_saved_errors.py`、`test_product_event_sequence.py`、`test_catalog_integrity.py` 验证；51 与 69（取回项与交付职责、取回字段组合）由 `test_obtain_summary.py`、`test_selection_guard*.py`、`test_catalog_integrity.py` 验证，组合覆盖 DELIVERY_CREATED 保持、重送复用与失败汇总；70—72（清理字段矩阵、三种 outcome、产物字段矩阵）由 `test_cleanup_guards.py`、`test_source_cleanup.py`、`test_output_family.py` 验证。output-verification 行为覆盖清单中"每个新请求产生独立 delivery、同一产物两次独立取回不同文件名、清理不级联、已获资格取回优先（business_order 严格早于才阻塞）、新取回被阻止"等由组合与上述文件共同覆盖；"取消撤回 ready、processing 不可撤回"由取消收场轮次（N1—N6，cancellation 目录）验证；真实设备联调与物理断电验证按规格单独安排（B7/部署验证），不属于本轮门槛。

**后置事项。** 普通（取回/清理）动作开始命令、精确清理与跨计划清理的执行期来源固定、统一 run 循环装配归阶段 2/I3/I5；真实 C 领取的跨组件验证归 I5；H7/N6 同层验收另行安排。

## 模块完成门禁

X1—X12 各用例及真实组合通过；取回、清理和内部媒体处理共享能力而保持各自身份及结果。正式源文件、副本、delivery 与中间文件的历史、恢复和可观察结果各自有完整证据。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
