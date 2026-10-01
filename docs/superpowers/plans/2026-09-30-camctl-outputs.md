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

**预计文件：** `apps/camctl/src/camctl/outputs/catalog.py`；测试为 `apps/camctl/tests/unit/outputs/test_catalog.py` 和 `apps/camctl/tests/integration/outputs/test_catalog.py`。

**接口与依赖：** 提供 `validate_output_registration(drafts: tuple[OutputDraft, ...], facts: OutputCatalogFacts) -> RegistrationChanges`；RegistrationChanges 由 C3.finish_capture 的同一事务应用。前置交付：K1、D2、H1；不依赖取回全部实现。

- [x] 编写失败用例。在 `test_registration_preserves_device_file_identity` 中正式登记前后 `assert file_id_after == original_file_id`；没有归属或文件完成不能登记。合法登记尚无 SHA-256 可保存未知摘要，已有可靠值不得丢失；原片/修复/预览明确关联，任意目录顺序不能配对。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_catalog.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。把登记规则作为纯计算供采集完整终态事务使用；源位置、产物身份及关联固定，内部文件按适用校验提升。
- [x] 再运行上述命令，要求全部 PASS，并核对 登记与动作结果原子，摘要不是所有产物的强制前置计算。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_catalog.py -q`，真实 C3/H3 组合证明正式产物与文件历史、原片关系及终态同时成立。
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

**接口与依赖：** 提供异步 `grant_file(command: FileCandidate, key: OperationKey) -> DbOutcome[FileQualification]`；读取获准时同时建立所需依赖、delivery、copy、目标文件及读取流程。前置交付：X2、Q1/Q4、O2、P3。

- [ ] 编写失败用例。在 `test_qualification_uses_business_order` 中颠倒来源结束及协程唤醒，`assert winner == expected_by_plan_time`，同时间取回优先。已有读取保护、不可撤销清理限制、唯一删除处理者及跨设备候选分别验证；缺任一建档行整笔拒绝。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_qualification.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。事务内查全部可靠限制和业务顺序，完整授予或保存逐项拒绝；内部检查/修复共用设备单文件读取机会，不必创建 delivery。
- [ ] 再运行上述命令，要求全部 PASS，并核对 资格不是先检查后另事务抢占，等待不消耗尝试。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部普通取回、自动预览、内部处理和源删除是否经过相同资格规则；记录门禁证据，建议以“feat: 实现读取与清理原子资格”形成独立提交。

### X4 共用拷贝身份与续传准备

**预计文件：** `apps/camctl/src/camctl/outputs/copy.py`、`apps/camctl/src/camctl/outputs/ports.py`；测试为 `apps/camctl/tests/unit/outputs/test_copy_resume.py` 和 `apps/camctl/tests/integration/outputs/test_copy_resume.py`。

**接口与依赖：** 提供 `decide_resume(facts: CopyFacts) -> CopyDecision`、异步 `prepare_copy(identity: CopyIdentity, service: CopyContext) -> CopyStep`；CopyContext 含仓储、读取和文件端口，CopyStep 表达原身份及待办阶段。前置交付：X3、D4、F1/F4、O2/O5。

- [ ] 编写失败用例。按上方七个续传分区建立 `test_resume_preserves_confirmed_prefix`，C=3、L=5、N=6，`assert decision.truncate_to == 3` 且 offset=3；C=0/L缺失、L=C=N、不可靠检查及长度越界分别断言。未保存读取失败重启沿原尝试，已明确失败只能新增合法尝试。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_copy_resume.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。固定源身份、长度、原目标及处理归属；读取开始前保存意图/次数；续传截断必须同步成功才继续，不重新分配 delivery 或重拷轮次。
- [ ] 再运行上述命令，要求全部 PASS，并核对 取回、原片检查及修复可以调用同一 CopyService。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_copy_resume.py -q`，真实文件与数据库验证多次中断、可靠尾部、源身份改变和两种失败恢复。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有恢复入口是否从当前长度默认进度或补满读取次数；记录门禁证据，建议以“feat: 实现共用拷贝与续传准备”形成独立提交。

### X5 分段提交、取消和可靠进度

**预计文件：** `apps/camctl/src/camctl/outputs/copy.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_copy_segments.py` 和 `apps/camctl/tests/integration/outputs/test_copy_segments.py`。

**接口与依赖：** 提供异步 `copy_next_segment(identity: CopyIdentity, context: CopyContext) -> CopyStep`、仓储 `save_segment(command: ReliableSegment, key: OperationKey) -> DbOutcome[CopyProgress]`；ReliableSegment 含原轮次、旧 C、范围及同步证据。前置交付：X4、F2/F3、P3/P4。

- [ ] 编写失败用例。建立 `test_cancel_before_progress_prevents_new_commit`，段成功返回但普通取回已取消，`assert new_progress_writes == 0`；进度事务已开始则仍跟踪实际提交。同步失败 `assert progress_after == progress_before`；提交未知不得排下一段。改段大小后不把 C 对齐新倍数。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_copy_segments.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。一文件一次一段，段大小从本次配置取得；实际写入及同步可靠、资格仍成立才保存进度，确认提交后推进下一段。每段不新建读取尝试，不重置无数据等待或预算。
- [ ] 再运行上述命令，要求全部 PASS，并核对 可靠进度没有空洞、重复或未同步尾部，取消停止新增普通操作。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_copy_segments.py -q`，真实默认池、SQLite 与可控源流，在段内、同步和进度提交前后中断。
- [ ] 审阅实际接口、状态分区及失败路径，检查 段成功、文件长度及进度保存是否被错误视为同一事实；记录门禁证据，建议以“feat: 实现可靠拷贝段与取消收场”形成独立提交。

### X6 摘要、有限重拷与准备完成

**预计文件：** `apps/camctl/src/camctl/outputs/copy.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_copy_complete.py` 和 `apps/camctl/tests/integration/outputs/test_copy_complete.py`。

**接口与依赖：** 提供 `decide_integrity(facts: IntegrityFacts) -> IntegrityDecision`、异步 `complete_copy(identity: CopyIdentity, context: CopyContext) -> DbOutcome[PreparedCopy]`；IntegrityFacts 含固定长度、可靠读取、主机摘要及源能力/摘要结果。前置交付：X5、D2/D3、F4。

- [ ] 编写失败用例。建立 `test_prepared_commit_releases_source`，完整性及同步成功但准备事务尚未确认，`assert source_dependency_released is False`；事务提交后为 True。源摘要支持相同/不同/失败、明确不支持、能力未知五分区覆盖。读取额与重拷额独立，已用读取 3 仍可合法重拷但读取再次错误立即耗尽。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_copy_complete.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。主机 SHA-256 必须计算；源支持时必须比较，失败不能降级。摘要不一致先提交轮次消耗和进度归零，再截断/重建并记录重置完成；准备完成与解除源依赖共同保存并通知清理。
- [ ] 再运行上述命令，要求全部 PASS，并核对 两类预算及原身份保持，已准备副本不再依赖源存在。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_copy_complete.py -q`，真实字节摘要、重拷决定到文件重置各中断边界及来源清理竞争。
- [ ] 审阅实际接口、状态分区及失败路径，检查 完整性、重拷、内部输入和普通取回是否存在绕过校验；记录门禁证据，建议以“feat: 实现拷贝校验与副本准备”形成独立提交。

### X7 普通交付发布及恢复

**预计文件：** `apps/camctl/src/camctl/outputs/handoff.py`、`apps/camctl/src/camctl/outputs/service.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_delivery.py` 和 `apps/camctl/tests/integration/outputs/test_delivery.py`。

**接口与依赖：** 提供 `decide_handoff(facts: DeliveryFacts, files: DeliveryLocations) -> DeliveryDecision`、异步 `publish_delivery(identity: DeliveryIdentity, context: DeliveryContext) -> DeliveryResult`；位置观察分别分类 staging/ready/processing。前置交付：X6、F5、P3/P4。

- [ ] 编写失败用例。建立 `test_missing_all_copies_is_final_unknown_failure`，无完成事实且可靠三处缺失，`assert error.code == 'delivery_handoff_unconfirmed'` 且重投次数 0；三处检查任一失败不能归此分支。完整 staging 继续原身份，ready/processing 可补本地事实，已保存完成不因文件删除撤销。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_delivery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。整次取回满足发布条件后，先提交原 delivery 发布意图，事务外移动/同步，最后按实际证据保存结果；主程序已经领取也可保存当前可靠完成。未知最终失败保持，其他项继续。
- [ ] 再运行上述命令，要求全部 PASS，并核对 本地交付不冒充远端传输或客户端收件。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_delivery.py -q`，真实目录、SQLite 及受协议约束的领取协作者，在意图、移动、同步和结果保存前后中断；真实 C 领取归 I5。
- [ ] 审阅实际接口、状态分区及失败路径，检查 普通交付是否误用报告补投、覆盖同名文件或删除 processing；记录门禁证据，建议以“feat: 实现普通交付与未知恢复”形成独立提交。

### X8 完整源清理与独立预算

**预计文件：** `apps/camctl/src/camctl/outputs/cleanup.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_source_cleanup.py` 和 `apps/camctl/tests/integration/outputs/test_source_cleanup.py`。

**接口与依赖：** 提供 `decide_cleanup(facts: CleanupFacts) -> CleanupDecision`、异步 `advance_cleanup(item: CleanupIdentity, context: CleanupContext) -> CleanupStep`；实际文件删除/查询使用 D3 或 F2。前置交付：X2/X3、O2/O4、F1/F2、P3。

- [ ] 编写失败用例。按文件已删除/仍存在/未知与两组额度有余/耗尽建立 `test_cleanup_budgets_are_independent`，`assert delete_used == expected_delete` 且 query_used 独立。删除额耗尽仍可剩余查询，未知且查询额耗尽不能重删；可靠完成不额外查询。已准备 delivery、修复产物及历史不级联删。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_source_cleanup.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。先固定完整目标，按资格和唯一处理者调用；每次真实删除或存在性查询前保存意图及所属次数，调用结束后文件事实、产物可用性、逐项与动作汇总共同提交。
- [ ] 再运行上述命令，要求全部 PASS，并核对 未知不变删除成功，配置改变和重启不重开终态。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_source_cleanup.py -q`，真实仓储、文件/设备替身验证全部删除与查询结果及取回先后竞争。
- [ ] 审阅实际接口、状态分区及失败路径，检查 设备源与主机派生成品的全部删除/查询入口是否暗自重试；记录门禁证据，建议以“feat: 实现源产物清理与独立预算”形成独立提交。

### X9 清理取消、接手及原结果保持

**预计文件：** `apps/camctl/src/camctl/outputs/cleanup.py`、`apps/camctl/src/camctl/outputs/qualification.py`；测试为 `apps/camctl/tests/unit/outputs/test_cleanup_recovery.py` 和 `apps/camctl/tests/integration/outputs/test_cleanup_recovery.py`。

**接口与依赖：** 提供 `apply_cleanup_cancel(facts: CleanupFacts) -> CleanupCancelChanges`、`merge_cleanup_requests(facts: CleanupCoordination) -> CoordinationDecision`；Coordination 含全部有效项和原处理者。前置交付：X8、N3；已有真实删除责任。

- [ ] 编写失败用例。建立 `test_later_cleanup_preserves_old_failure`，请求 A 查询耗尽失败，请求 B 后来确认不存在，`assert a.result == original_failure` 且 B 成功/产物清理事实更新。取消未发删除解除相应限制，可能已删保持限制；在途调用实际结束前仍跟踪，取消发起者结束不丢责任。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_cleanup_recovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。目标独立收场，保留原请求结果及预算；后续有效请求可以接手仍适用工作，不能重开旧项。正常及重启用同一候选时间排序。
- [ ] 再运行上述命令，要求全部 PASS，并核对 清理取消和读取保护按完整分区恢复，所有独立结果不被后续成功覆盖。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_cleanup_recovery.py -q`，真实 SQLite、并发取回和多个清理请求，固定取消、删除、查询及结果保存的竞争位置。
- [ ] 审阅实际接口、状态分区及失败路径，检查 来源、清理处理者、取回保护和逐项汇总的共同不变量；记录门禁证据，建议以“feat: 实现清理取消与责任接手”形成独立提交。

### X10 自动预览及统一发布汇总

**预计文件：** `apps/camctl/src/camctl/outputs/previews.py`、`apps/camctl/src/camctl/outputs/service.py`；测试为 `apps/camctl/tests/unit/outputs/test_previews.py` 和 `apps/camctl/tests/integration/outputs/test_previews.py`。

**接口与依赖：** 提供 `select_previews(facts: PreviewFacts) -> SelectionSnapshot`、`decide_obtain_finish(facts: ObtainFacts) -> ObtainDecision`；facts 包含全部固定来源及逐项阶段，不能用缓存局部子集汇总。前置交付：X2—X7、C6、Q1/Q4、N1/N2。

- [ ] 编写失败用例。在 `test_publish_waits_for_complete_selection` 中一来源已准备、另一仍未选，`assert may_publish is False`；所有来源和合法项完成准备/失败后才统一发布成功项，整次有失败仍保留成功交付。自动预览已开始兼容读取可继续，不兼容时本次读取结束再优先到时拍摄；下一文件和重试等待让路。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_previews.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。用明确预览关联选择，自动与手动共用预览选择方式；取消联动只由 N1/N2 决定，直接取消取回不反向取消拍摄。汇总全部适用结果及父状态同事务保存。
- [ ] 再运行上述命令，要求全部 PASS，并核对 没有早发、扩大筛选、回退替换或按目录配对。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_previews.py -q`，真实三种拍摄、部分取回及取消联动，按领取契约控制文件位置；报告保留成功/失败项，真实 C 组合归 I5。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部来源、部分成功和自动预览的发布/取消条件；记录门禁证据，建议以“feat: 实现自动预览与取回汇总”形成独立提交。

### X11 中间文件生命周期与有限维护

**预计文件：** `apps/camctl/src/camctl/outputs/work_files.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`；测试为 `apps/camctl/tests/unit/outputs/test_work_files.py` 和 `apps/camctl/tests/integration/outputs/test_work_files.py`。

**接口与依赖：** 提供 `classify_work_file(facts: WorkFileFacts) -> WorkFileDecision`、异步 `clean_work_files(scope: WorkFileCleanupScope) -> WorkFileCleanupResult`；事实含用途、归属、可恢复责任、真实任务状态及持久化游标。前置交付：F1/F2、X4、S1；先提供中间文件用途和生命周期端口，C8 随后消费。

- [ ] 编写失败用例。在 `test_work_file_cleanup_does_not_restart_action` 中取消后完整 staging 副本删除失败，`assert action.status == original_terminal_status` 且不会单独 needs_run。可恢复输入保留、已成正式文件不清理、实际操作未结束不删；首次新文件清理与历史扫描预算分别计数。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/outputs/test_work_files.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。中间文件建立时就保存用途/归属；先可靠保存取消或失败，再等实际操作结束后清理。历史清理按批量、每 run 上限及可靠游标有限推进，失败留后续正常 run，不原地循环。
- [ ] 再运行上述命令，要求全部 PASS，并核对 历史及新文件清理遵守既定不同预算，诊断可靠保存后允许会话收尾。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_work_files.py -q`，真实文件和 SQLite 验证重启续查、额度边界、删除失败及内部修复文件提升。
- [ ] 审阅实际接口、状态分区及失败路径，检查 半成品、完整取消副本、检查输入和修复输出所有生命周期；记录门禁证据，建议以“feat: 实现中间文件有限维护”形成独立提交。

### X12 产物链及历史组合验收

**预计文件：** `apps/camctl/src/camctl/outputs/service.py`、`apps/camctl/src/camctl/outputs/cleanup.py`；测试为 `apps/camctl/tests/integration/outputs/test_outputs_contract.py`。

**接口与依赖：** 使用 X1—X11、C1—C8、N1—N5、H1—H6 和 R1—R8 的真实接口。前置交付：X1—X11、C1—C8、N1—N5、H1—H6、R1—R8；H7/N6 为同层验收，不作为前置。

- [ ] 编写失败用例。在 `test_output_lifecycles_are_independent` 中源后来清理、原 delivery 已准备，`assert delivery_can_continue is True`；同 output 两个新请求产生独立交付，同请求重送不产生新交付。所有来源/资格/拷贝/清理/交接边界中断后核对同 H 的事实及固定报告字节。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/outputs/test_outputs_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。逐项映射 output-verification 和数据库验收 01—51、69—72 的适用条目，执行真实仓储、文件、受管调用和报告组件组合；设备使用契约替身，真实 C 领取的跨组件验证由 I5 承接。
- [ ] 再运行上述命令，要求全部 PASS，并核对 所有来源和文件生命周期闭合，没有仅正常路径的通过声明。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部错误、未知、取消、预算、清理及跨请求结果保持；记录门禁证据，建议以“test: 验证产物取回清理完整闭环”形成独立提交。

## 模块完成门禁

X1—X12 各用例及真实组合通过；取回、清理和内部媒体处理共享能力而保持各自身份及结果。正式源文件、副本、delivery 与中间文件的历史、恢复和可观察结果各自有完整证据。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
