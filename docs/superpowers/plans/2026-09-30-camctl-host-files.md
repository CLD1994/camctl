# camctl 主机文件执行模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 提供可核实的文件检查、分段写入、同步、移动及删除事实，保持实际文件所有权直到操作结束。

**组织建议：** 业务流程提供已保存文件身份和资格，本模块只执行文件动作并返回实际阶段；普通文件和分段传输使用同一个默认线程池。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 pathlib/os、asyncio.to_thread、线程安全停止通知和 hashlib；媒体子进程通过 O3 管理。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [文件执行](../../camctl/file-runtime.md)、[路径字段](../../camctl/database/file-fields.md)、[文件交接](../../architecture/file-handoff.md)、[拷贝](../../architecture/file-copy.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

本地主机路径只从已保存身份、用途和相对路径取得，不能由设备定位或用户名称拼出。文件不存在、类型不符、目录不可读及操作错误分别表达。每个文件最多一个未完成段，实际线程结束前不关闭、移动或删除其文件。段成功仅证明实际字节及同步，可靠进度由业务事务确认。本地文件调用一旦开始就等待真实结果，不新增应用层完成时限或统一提交上限。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

默认线程池限制实际线程，第一版不宣称内部等待队列有界。通过按需创建、单文件逐段和设备并发限制控制提前积压。日志副本及报告子进程使用各自规定的执行资源。

| 类型 | 字段或含义 |
| --- | --- |
| `FileRef / FileObservation` | 文件 ID、用途、所属处理、相对路径和对应根目录；观察为有效对象、缺失、类型不符、检查错误或未知。 |
| `FileTask / FileLease` | 排队、撤回竞争、已开始、已结束阶段及实际唯一修改资格；不能由等待协程生命周期代替。 |
| `SegmentSpec / SegmentResult` | 原读取尝试、轮次、目标文件、[C,E) 范围、缓冲和停止通知；结果含已处理范围、同步阶段、错误及源读取是否实际结束。 |
| `PublishResult / RemoveResult` | 移动未发生、已移动同步未确认、已移动且同步、移动未知；删除的可靠完成、失败和未知分别表达。 |
| `MediaProbe / MediaArtifact` | ffprobe 取得的必要媒体事实，ffmpeg 实际成品及其大小/摘要/同步证据；不决定拍摄成功。 |

文件任务和发布分别建模。

| 实际阶段 | 取消或失败处理 |
| --- | --- |
| 尚未开始且确认撤回 | 返回未执行，不取得修改资格。 |
| 已开始、停止通知到达 | 小块之间和段尾同步前停止新增普通操作，等待已经开始的调用。 |
| 写入完成且必要同步成功 | 返回实际范围；尚不证明进度入库。 |
| 写入、读取或同步失败 | 保留失败阶段及已知字节，不能推进可靠进度。 |
| 移动成功但目录同步失败 | 文件已可被领取，不能解释为没有发布。 |
| 移动和目录同步可靠成功 | 返回发布事实，即使主程序已取走文件也成立。 |
| 操作或存在性无法可靠确认 | 返回实际错误或未知，不猜测缺失和成功。 |

底层文件调用持续不返回属于既定部署处置范围，本计划不设计应用内强制限时关闭仍在使用的文件。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/host_files/models.py` | 实际文件与任务结果类型。 |
| `apps/camctl/src/camctl/host_files/paths.py` | 用途目录、归属及对象类型检查。 |
| `apps/camctl/src/camctl/host_files/tasks.py` | 默认池任务、撤回及实际结果接手。 |
| `apps/camctl/src/camctl/host_files/segments.py` | 小块传输、停止、同步与段结果。 |
| `apps/camctl/src/camctl/host_files/io.py` | 创建、截断、同步及摘要。 |
| `apps/camctl/src/camctl/host_files/handoff.py` | 原子移动、目录同步及可撤回位置。 |
| `apps/camctl/src/camctl/host_files/media.py` | 受管媒体工具与结构化事实。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享类型](2026-09-30-camctl-contracts.md) | K1—K3 身份、精确范围及分类。 |
| [设备](2026-09-30-camctl-devices.md) | D4 提供 ReadSession 和独立控制。 |
| [会话与操作](2026-09-30-camctl-operations.md) | S5 接手实际任务；O3 管理媒体子进程。 |
| [业务消费者](2026-09-30-camctl-outputs.md) | X4—X9 消费实际证据；R5/R7 与 L5 分别消费自己的文件规则。 |

F1 的检查和 F5 的发布先支撑首条报告链。F2—F4 在首个拷贝前完成；F6 在异常录像处理前完成；F7 组合全部消费者和实际文件错误。报告进程自身完成文件写入，不通过主进程默认池中转内容。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 检查失败不能当作文件不存在。 | F1，`test_permission_error_is_not_missing` |
| 等待者取消后线程仍持有文件。 | F2，`test_cancel_does_not_release_started_file` |
| 取消不要求完成整个进度段。 | F3，`test_cancel_stops_between_chunks` |
| 目录同步失败仍保留已移动事实。 | F5，`test_move_success_sync_failure_is_visible` |
| 部分媒体成品不能作为正式产物。 | F6，`test_failed_media_output_is_not_complete` |

## 实施任务

### F1 文件定位与分类观察

**预计文件：** `apps/camctl/src/camctl/host_files/models.py`、`apps/camctl/src/camctl/host_files/paths.py`；测试为 `apps/camctl/tests/unit/host_files/test_paths.py` 和 `apps/camctl/tests/integration/host_files/test_paths.py`。

**接口与依赖：** 提供 `resolve_file(ref: FileRef, roots: BoundDirectories) -> HostPath`、异步 `inspect_file(ref: FileRef) -> FileObservation`；BoundDirectories 来自已验证目录绑定，HostPath 是本地定位值。前置交付：K1、B2。

- [x] 编写失败用例。建立 `test_permission_error_is_not_missing`，目录权限检查失败，`assert observation.kind is FileObservationKind.ERROR`；真实缺失另为 MISSING。设备定位、用户名称、用途与根目录不匹配、目标类型错误及非法相对路径分别拒绝。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/host_files/test_paths.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。只按正式用途和保存路径定位，实际文件访问在执行适配器；返回实际错误及阶段，纯路径规则不访问真实文件。
- [x] 再运行上述命令，要求全部 PASS，并核对 F-01—F-08 的路径与类型分类覆盖。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_paths.py -q`，临时真实目录覆盖缺失、目录对象、不可访问和移动后的定位，不修改用户全局路径。
- [x] 审阅实际接口、状态分区及失败路径，检查 输入、普通交付、内部处理、报告和日志各文件入口是否混用定位；记录门禁证据，建议以“feat: 实现主机文件定位与观察”形成独立提交。

### F2 默认线程池任务与唯一修改权

**预计文件：** `apps/camctl/src/camctl/host_files/tasks.py`；测试为 `apps/camctl/tests/unit/host_files/test_tasks.py` 和 `apps/camctl/tests/integration/host_files/test_tasks.py`。

**接口与依赖：** 提供异步 `run_file_task(task: FileTask, owner: ResponsibilityOwner) -> FileTaskResult`、`request_stop(task_id: FileTaskId) -> None`；FileTaskResult 显式给出实际未执行或已结束结果。前置交付：S5、F1；执行器端口替身。

- [x] 编写失败用例。建立 `test_cancel_does_not_release_started_file`，排队或已开始分别取消，`assert lease.released is False` 直到实际结束；撤回与开始竞争只有一个结果。重复停止不重复释放，不对同文件安排第二段。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/host_files/test_tasks.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。通过 asyncio.to_thread 共享默认池，停止通知与任务状态形成唯一判定；已开始任务受监督跟踪，不以 Future 被取消作为真实结束。
- [x] 再运行上述命令，要求全部 PASS，并核对 同文件最多一个未结束任务，关闭遵守实际执行状态。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_tasks.py -q`，真实默认线程池排队、跨段由不同线程执行及取消后结果组合。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有线程池调用与文件句柄关闭是否有一致拥有者；记录门禁证据，建议以“feat: 实现文件任务所有权与接手”形成独立提交。

### F3 段内小块传输与源无数据观察

**预计文件：** `apps/camctl/src/camctl/host_files/segments.py`；测试为 `apps/camctl/tests/unit/host_files/test_segments.py` 和 `apps/camctl/tests/integration/host_files/test_segments.py`。

**接口与依赖：** 提供同步 `transfer_segment(spec: SegmentSpec, source: ReadSession, target: WritableFile, clock: MonotonicClock) -> SegmentResult`；由 F2 在线程调用，WritableFile 是受约束写入/同步端口。前置交付：D4、F1/F2。

- [x] 编写失败用例。建立 `test_cancel_stops_between_chunks`，第一块完成后停止，`assert bytes_written < segment_end` 且没有新增同步；正在进行的同步返回后保留实际结果。覆盖剩余 0、小于/等于/大于段、跨边界块截取、提前 EOF、持续数据、设备日志、目标同步不计无数据等待。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/host_files/test_segments.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按 [C,E) 顺序读写，复用有界缓冲；块大小为独立工程参数，建议初值 1 MiB、合法范围为 1—4194304 字节的整数，单次请求不超过段剩余量。默认值和范围在消费前集中定义，后续按测量调整。源数据在线程及时观察，段大小读取既有配置，不把整段攒入内存。
- [x] 再运行上述命令，要求全部 PASS，并核对 事件循环不逐块转发内容，数据到达与可靠进度分开。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_segments.py -q`，真实受控读取流和文件验证不同小块/段大小产生相同字节，停止控制可在段内到达。
- [x] 审阅实际接口、状态分区及失败路径，检查 无数据计时是否跨本地写入、同步或数据库保存阶段累计；记录门禁证据，建议以“feat: 实现分段线程传输”形成独立提交。

### F4 创建截断同步与摘要

**预计文件：** `apps/camctl/src/camctl/host_files/io.py`；测试为 `apps/camctl/tests/unit/host_files/test_file_io.py` 和 `apps/camctl/tests/integration/host_files/test_file_io.py`。

**接口与依赖：** 提供 `prepare_target(ref: FileRef, desired_length: int) -> FileMutationResult`、`sync_target(ref: FileRef) -> SyncResult`、`hash_target(ref: FileRef) -> HashResult`；异步包装使用 F2。前置交付：F1/F2；调用方已给恢复或重拷资格。

- [x] 编写失败用例。在 `test_truncate_failure_blocks_continue` 中截断失败，`assert can_continue is False`；文件同步成功但目录同步失败返回分阶段事实。最终 SHA-256 用固定独立字节预期，`assert digest == expected_digest`；读取错误不能当空文件摘要。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/host_files/test_file_io.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。用标准文件操作及 hashlib 流式处理，遵守必要目录同步；不在本模块重置数据库进度、预算或自动重拷。
- [x] 再运行上述命令，要求全部 PASS，并核对 创建、截断、同步和摘要的实际阶段可恢复。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_file_io.py -q`，真实文件验证截断尾部、完整摘要及同步/空间错误；系统调用故障用窄注入点提供确定错误。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部 sync/flush/close 顺序是否把关闭等同落盘；记录门禁证据，建议以“feat: 实现文件同步与摘要事实”形成独立提交。

### F5 原子交接与撤回事实

**预计文件：** `apps/camctl/src/camctl/host_files/handoff.py`；测试为 `apps/camctl/tests/unit/host_files/test_handoff.py` 和 `apps/camctl/tests/integration/host_files/test_handoff.py`。

**接口与依赖：** 提供异步 `publish_file(ref: FileRef, target: ReadyName) -> PublishResult`、`withdraw_file(identity: HandoffIdentity) -> WithdrawResult`；名称和身份由业务消费者提供。前置交付：F1/F2/F4；不依赖业务 DB。

- [x] 编写失败用例。建立 `test_move_success_sync_failure_is_visible`，移动成功后目录同步失败，`assert result.moved is True` 且 durable 未确认。移动前后领取、ENOENT 竞争及同名普通文件不覆盖分别处理；processing 对象 `assert remove_calls == 0`。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/host_files/test_handoff.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。先核对完整文件资格，原子移动再同步涉及目录，保留阶段；撤回仅操作 camctl 仍拥有的位置，主程序领取后不可修改。普通、报告、日志的恢复由各自流程决定。
- [x] 再运行上述命令，要求全部 PASS，并核对 发布结果不要求文件持续留在 ready，日志无需状态库。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_handoff.py -q`，真实目录与受 C 领取契约约束的协作者交错移动和删除，核对同步及撤回；真实 C 组合由 I4/I5 验证。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有发布类型是否误用相同补投或覆盖规则；记录门禁证据，建议以“feat: 实现原子文件交接事实”形成独立提交。

### F6 媒体工具的受管执行

**预计文件：** `apps/camctl/src/camctl/host_files/media.py`；测试为 `apps/camctl/tests/unit/host_files/test_media.py` 和 `apps/camctl/tests/integration/host_files/test_media.py`。

**接口与依赖：** 提供异步 `probe_media(input: FileRef, request: ProbeRequest) -> MediaProbe`、`repair_media(input: FileRef, output: FileRef, request: RepairRequest) -> MediaArtifact`；请求由 C8 固定处理决定。前置交付：O3、F1/F4。

- [x] 编写失败用例。建立 `test_failed_media_output_is_not_complete`，工具失败但文件存在，`assert artifact.complete is False`；精确媒体时长不舍入到目标毫秒，非法结构与读取错误单独分类。probe/repair 不改变动作终态或源文件。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/host_files/test_media.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。复用 ffprobe/ffmpeg，只解析业务需要字段并控制输出容量；实际退出、必要成品校验、同步及摘要分别确认，厂商/工具参数由任务已保存决定取得。
- [x] 再运行上述命令，要求全部 PASS，并核对 媒体进程保持 camctl 组且实际收场后才释放文件。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_media.py -q`，真实最小媒体文件与工具验证成功/失败及取消，未安装工具按实际配置或处理错误分类。
- [x] 审阅实际接口、状态分区及失败路径，检查 媒体包装和工具入口是否绕过 O3，是否把遗留文件当成功；记录门禁证据，建议以“feat: 接入受管媒体文件处理”形成独立提交。

### F7 文件消费者及失败边界组合

**预计文件：** `apps/camctl/src/camctl/host_files/tasks.py`、`apps/camctl/src/camctl/host_files/handoff.py`；测试为 `apps/camctl/tests/integration/host_files/test_file_contract.py`。

**接口与依赖：** 使用 F1—F6 与 X5/X7、R5/R7、L5 实际接口。前置交付：对应消费者已实现。

- [ ] 编写失败用例。在 `test_file_contracts_keep_actual_side_effects` 中对写入、同步、移动、删除各边界注入错误或中断，`assert recorded_effect == observed_effect`；取消后调用延迟返回成功/错误，结束前禁止发布、删除或关闭。报告和日志规则分别核对，不要求未知普通交付重投。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/host_files/test_file_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。补齐真实文件、默认线程池及各业务消费者的组合测试；进程中断仅验证软件恢复，不宣称物理断电保证。
- [ ] 再运行上述命令，要求全部 PASS，并核对 所有文件边界有分类和拥有者，合计积压测量不被当作固定内存保证。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部异常捕获、默认值和 finally 是否掩盖已发生效果；记录门禁证据，建议以“test: 验证真实文件执行边界”形成独立提交。

## 模块完成门禁

F1—F5 在真实文件及线程组合通过；F6 媒体工具采用统一生命周期；F7 证明各消费者按实际效果和自己的恢复规则推进。文件事实与数据库事实、实际完成与等待结束始终分开。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
