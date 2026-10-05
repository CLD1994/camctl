# camctl 状态报告与生成进程模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 异步完成报告资格、固定历史内容、进程生成、本地发布和累计确认，失败时保留原责任。

**组织建议：** 资格、冻结、编码、工作进程、发布及维护分别组织；主进程保存业务决定和发布事实，子进程只读历史并生成同步后的 staging 文件。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 spawn 的 multiprocessing.Process/Pipe、专用通信线程、Linux 父进程死亡保护和独立 flock；分批恢复及确定性 JSON 使用 H4/K4。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [报告与 ACK](../../architecture/status-reports.md)、[状态同步](../../architecture/status-sync.md)、[维护机会](../../architecture/report-maintenance.md)、[发布与补投](../../architecture/report-publication.md)、[报告编码](../../architecture/report-encoding.md)、[报告进程](../../camctl/report-runtime.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

冻结在同事务可靠状态下取事务之前的完整 H、业务水位、ACK 和全部有效同步依据；报告身份及冻结内容不再改变。生成器不读当前时间、配置或设备，同一报告重建字节及 SHA-256 不变。成功消息只证明生成和文件同步，不证明发布。报告文件/进程失败可继续可靠普通业务，真实状态库读取或历史解释失败不能降级。ACK 按业务水位及范围，不按报告 ID 大小比较；报告管理及有效 ACK 本身不产生确认 ACK 的报告。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

逻辑 report_id 与每次 generation task_id 分开。控制消息只传标识、冻结依据引用和执行参数，不传完整对象、事件或报告正文。临时文件在实际任务停止前不得清理或复用。

| 类型 | 字段或含义 |
| --- | --- |
| `ReportOpportunity / ReportDecision` | 同一完整 H 的业务终点、累计 ACK、有效同步的最早起点及最晚开始边界；纯规则决定生成、复用或跳过。 |
| `ReportSelection` | 写事务中实际选择的分支；生成及复用提供固定报告依据，跳过不提供报告。该结果不证明文件已经发布。 |
| `FrozenReport / ReportBasisRef` | report_id、完整 H、范围/水位、冻结生成元数据和持久化依据引用；大集合留在库中分页读取，不放入控制消息。 |
| `GenerationJob / TaskId` | 数据库位置/身份、report_id、BasisRef、task_id、独占临时文件、本次读取/忙等待及日志参数；TaskId 建议 uuid4 的 32 字符十六进制值，仅用于本次进程通信。 |
| `ControlMessage / GenerationResult` | version=1，ready/job/result/shutdown 类型集中定义；成功含 task_id、原临时文件、大小和 SHA-256，失败含分类及必要诊断。 |
| `WorkerState / GenerationState` | 工作进程启动、健康空闲、生成、停止、退出确认；任务未决、成功、失败、超时、停止及有效结果确认分别表达。 |
| `ReportRepository / PublicationResult` | 冻结、发布事实、同步结束及 ACK 窄用例；实际发布来自 F5，不由文件存在猜测生成成功。 |
| `AckReport / AckFacts` | 确认使用的报告身份、覆盖起点与终点、冻结事件位置，以及累计水位和累计身份；可靠不存在与读取失败分别表达。 |
| `SyncResponsibility` | 同步身份、发起动作、固定起点与开始事务的完整末位；资格同时比较业务范围和历史顺序。 |

主进程串行确认当前任务的结果、停止和超时。

| 先确认的状态 | 随后事件 | 处理 |
| --- | --- | --- |
| 当前任务未决且未停止/超时 | 匹配的完整成功结果 | 确认生成，按本地前提发布。 |
| 当前任务未决 | 有效报告失败 | 保留责任，结束本进程，普通业务可继续。 |
| 任意适用状态 | 有效状态库或历史错误，包括停止后的迟到结果 | 按状态库错误处理，不能忽略证据。 |
| 未决 | 退出/断连，检查已送达消息仍无有效结果 | 未确认生成，不发布残留文件。 |
| 停止或超时 | 迟到 ready/成功 | 不重新激活，继续实际退出确认。 |
| 已成功 | 子进程退出或迟到原计时 | 保留生成事实；该进程不复用，计时不能改结果。 |
| 已成功 | 发布所需库、锁等前提失效 | 保存实际事实，停止依赖失效前提的后续步骤。 |
| 任意 | 消息无效或身份不匹配 | 通信错误，不能完成当前任务。 |

共享同步等待者取消不停止共享生成；真实停止资格由维护专题决定。启动、整次生成、停止各用单调期限，进度不续期；发信号不等于实际退出。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/reporting/models.py` | 冻结、机会、进程及任务结果。 |
| `apps/camctl/src/camctl/reporting/policy.py` | 资格、覆盖、替换及补投纯规则。 |
| `apps/camctl/src/camctl/reporting/encoding.py` | 固定字段、数字、字符串及流式实体输出。 |
| `apps/camctl/src/camctl/reporting/messages.py` | 控制消息的唯一版本和结构。 |
| `apps/camctl/src/camctl/reporting/communication.py` | 专用线程、端点及事件循环通知。 |
| `apps/camctl/src/camctl/reporting/worker.py` | 子进程就绪、独立只读连接与生成。 |
| `apps/camctl/src/camctl/reporting/supervisor.py` | 主进程创建、监测、时限和实际回收。 |
| `apps/camctl/src/camctl/reporting/publication.py` | 文件检查、ready 替换及发布结果。 |
| `apps/camctl/src/camctl/reporting/maintenance.py` | 独立报告机会及失败后触发。 |
| `apps/camctl/src/camctl/reporting/ack.py` | 累计 ACK 及同步资格纯计算。 |
| `apps/camctl/src/camctl/persistence/repositories/reporting.py` | 冻结、发布、同步和管理历史原子保存。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享与历史](2026-09-30-camctl-history.md) | K1—K4、H3—H5 固定 H、纯公开投影及有限读取。 |
| [持久化](2026-09-30-camctl-persistence.md) | P1/P3/P4 完整冻结、记录和错误分类。 |
| [文件](2026-09-30-camctl-host-files.md) | F1/F5 的文件位置与实际移动/同步。 |
| [会话与日志](2026-09-30-camctl-session.md) | S5/S6 管理实际结果，L5 独立故障副本。 |
| [受理与客户端](2026-09-30-camctl-acceptance.md) | R6 的纯 ACK 规则供 A4 同事务消费；消费者适配由 I3 完成。 |

R1/R2/R3 实现首批内容，R4/R5 实现真实进程，R6 的规则先支撑受理，R7/R8 完成首条链。每次新业务接入扩展同一字段/依赖用例，R9 在全部事件与同步消费者完成后验收。R5 的外部生成独立于普通调度。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 冻结后新变化不进入原报告。 | R2，`test_frozen_report_excludes_later_changes` |
| 分页、缓存和 Decimal 精度不改变字节。 | R3，`test_encoding_is_batch_invariant` |
| 先退出通知仍先检查已送达结果。 | R5，`test_exit_checks_delivered_result` |
| 同水位有效 ACK 仍可结束同步。 | R6，`test_equal_watermark_ack_can_end_sync` |
| 只剩失败报告不轮询重试或无限延长 run。 | R8，`test_failure_waits_for_new_trigger` |

## 实施任务

### R1 公开字段与报告身份模型

**预计文件：** `apps/camctl/src/camctl/reporting/models.py`、`apps/camctl/src/camctl/reporting/policy.py`；测试为 `apps/camctl/tests/unit/reporting/test_projection.py`。

**接口与依赖：** 提供 `validate_frozen_report(report: FrozenReport) -> None`；报告字段直接消费 K4.project_public，不为简单转发函数增加独立契约。前置交付：K3/K4、H1 的事实接口。

- [x] 编写失败用例。建立 `test_frozen_report_requires_complete_basis`，缺少完整 H、固定范围或必要生成元数据时，`assert validation_rejected is True`；合法初始边界、空变更范围及原始非法输入分别核对。公开字段与目录一致性的真实消费者验证由 K4/R3 承担，不重复一套字段计算测试。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_projection.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。建立不可变冻结类型及其完整性验证；实体字段复用 K4 的公开投影，子实体分页编码，不复制整棵计划。
- [x] 再运行上述命令，要求全部 PASS，并核对 公开字段有用户用途及正式依据，错误输入保留原事实。
- [x] 审阅实际接口、状态分区及失败路径，检查 事件变化比较与报告内容是否各写一份完整投影；记录门禁证据，建议以“feat: 定义报告字段与冻结模型”形成独立提交。

### R2 报告资格、覆盖与原子冻结

**预计文件：** `apps/camctl/src/camctl/reporting/policy.py`、`apps/camctl/src/camctl/persistence/repositories/reporting.py`；测试为 `apps/camctl/tests/unit/reporting/test_freeze.py` 和 `apps/camctl/tests/integration/reporting/test_freeze.py`。

**接口与依赖：** 提供 `decide_report(opportunity: ReportOpportunity, existing_reports: Iterable[AckReport]) -> ReportDecision`、异步 `freeze_report(key: OperationKey, owned: OwnedConnection) -> DbOutcome[ReportSelection]`。仓储在写事务中取得完整 H、累计 ACK、全部有效同步及一份合格候选，实际选择生成、复用或跳过；调用方不传入预先计算的范围。前置交付：R1、P3/P4、S1；同步事实来自可靠仓储。机会触发和文件责任由维护与发布流程另行判断。

- [x] 编写失败用例。建立 `test_frozen_report_excludes_later_changes`，冻结 H 后新提交，`assert later_entity not in original_report_scope`；完整同步从 0、局部同步与普通 ACK 合并、已有宽报告可满足、报告 ID 更大但覆盖不足各独立判定。冻结不取当前事务的部分事件。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_freeze.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。事务内重新取得全部范围及原完整 H，保存不可变逻辑报告；保留/替换/补投按内容责任及文件证据，不按名字或编号决定。
- [x] 再运行上述命令，要求全部 PASS，并核对 没有把业务水位当历史边界，生成期间新变化交下一轮。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_freeze.py -q`，真实 SQLite 冻结与并发受理/ACK/同步变化，核对完整事务及旧内容不变。
- [x] 审阅实际接口、状态分区及失败路径，检查所有正常/受限/显式同步机会的冻结及覆盖来源；记录门禁证据，建议以“feat: 实现报告机会与可靠冻结”形成独立提交。

**分项进度：** [累计确认与同步结束审查](2026-10-02-camctl-ack-sync-review.md)与[报告范围及事务内选择审查](2026-10-02-camctl-report-freeze-review.md)记录冻结依据、范围选择和读取不变量的门禁。

- [x] 在冻结写事务中取得完整 H 及其实际业务水位；冻结前有新提交时仍保存一致的生成依据。
- [x] 精确校验报告依据，无效起点不截断；真实登记的报告可供 ACK 查询。
- [x] 从同一 H 取得累计 ACK 与全部有效同步要求，重判数据库内容需求和起点。
- [x] 已有单份报告同时满足范围与全部开始历史时复用；没有内容需求时跳过，两者均不新增历史。
- [x] 报告两端使用完整业务事务边界；局部同步起点报告在开始边界已经登记。矛盾时统一返回状态库错误。
- [ ] 复用、替换、补投结合覆盖、历史边界及实际文件责任，并接入正常、受限及显式同步机会。正常与显式同步机会已由 run 会话的报告维护流程接入；受限会话的一次报告机会等待 S3 装配。

### R3 确定性流式编码

**预计文件：** `apps/camctl/src/camctl/reporting/encoding.py`；测试为 `apps/camctl/tests/unit/reporting/test_encoding.py` 和 `apps/camctl/tests/integration/reporting/test_encoding.py`。

**接口与依赖：** 提供 `encode_number(value: int | Decimal) -> bytes`、`iter_report_bytes(report: FrozenReport, reader: HistoricalReader) -> Iterator[bytes]`；HistoricalReader 消费 H4/H5 的范围分页，遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)，不返回完整子树。前置交付：R1/R2、H4/H5、K2/K4。

- [x] 编写失败用例。建立 `test_encoding_is_batch_invariant`，同输入改变批次、缓存、Decimal 精度和恢复方向，`assert bytes_a == bytes_b == expected_bytes`；另以受真实分页契约约束的替身分别验证空有效页继续、最后一批有数据及读取错误不被解释为结束。精确数字直接预期、字段顺序、UTF-8 无 BOM、字符串转义、实体 ID 排序和普通数组保序均按编码专题。一个动作大量项跨页也不组装整树。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_encoding.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按固定字段与实体顺序维护分隔符和父/子写入位置，数字使用 as_tuple，字符串转义复用标准库；只保留本批与当前写入上下文，不保留全报告大字符串。
- [x] 再运行上述命令，要求全部 PASS，并核对 分批影响资源但不改变报告原始字节和 SHA-256。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_encoding.py -q`，真实历史读取后编码并用公共 Schema、文件名摘要及独立字节预期验证。
- [x] 审阅实际接口、状态分区及失败路径，检查 原始输入字段存在性、实体空集省略及跨页父子补齐；记录门禁证据，建议以“feat: 实现确定性分批报告编码”形成独立提交。

**分项进度：** [报告编码与生成前置审查](2026-10-02-camctl-report-encoding-review.md)记录确定字节和编码缓冲的验证范围；固定历史读取与子集合分页由 `generation.py` 组合 H4 分页与分段流写入 staging 文件交付。

- [x] 自身字段递归排序，实体子集合使用权威登记中的明确顺序；按完整整数 ID 排序并拒绝同集合重复身份。
- [x] 精确数字按规定分区统一写法，不舍入，不受 Decimal 精度影响；字符串复用标准库转义，普通数组保序。
- [x] 提供容量明确的字节输出接口，逐个投影根实体，缓冲变化不改变字节；后续投影错误保留失败，不能发布未完成文件。
- [x] 真实受理、SQLite、冻结范围、公开投影与公共 Schema 组合后，编码符合独立字节预期；字段登记的书写顺序不改变结果。
- [x] 从固定 H 恢复分页入选事实，按子集合跨页写出；不组装完整计划或动作子树。

### R4 控制消息及通信线程所有权

**预计文件：** `apps/camctl/src/camctl/reporting/messages.py`、`apps/camctl/src/camctl/reporting/communication.py`；测试为 `apps/camctl/tests/unit/reporting/test_messages.py` 和 `apps/camctl/tests/integration/reporting/test_messages.py`。

**接口与依赖：** 提供 `encode_message(message: ControlMessage) -> bytes`、`decode_message(data: bytes) -> ControlMessage`、异步 `communicate(request: WorkerRequest) -> WorkerEvent`；WorkerRequest/WorkerEvent 为发送及已校验接收控制事件。前置交付：R1；Pipe 字节接口与线程通知端口。

- [x] 编写失败用例。在 `test_messages_reject_wrong_task_and_size` 中未知版本、额外字段、错误 task_id、部分帧、非 UTF-8 或超容量，`assert task_completed is False`；旧结果不能完成新任务。通信阻塞时事件循环及生成总时限仍推进，停止后线程实际退出才关闭责任。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_messages.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。建议控制消息 version=1、最大 64 KiB、一次最多一个待发送生成任务，集中定义并校验。每端单一收发线程/进程拥有者，未使用的 Pipe 端立即关闭；线程通过 call_soon_threadsafe 交事件，父进程监测独立于阻塞收发。结束子进程并关闭对端后使阻塞通信返回，再由拥有者关闭端点和回收线程。
- [x] 再运行上述命令，要求全部 PASS，并核对 消息容量不携带业务大集合，控制错误不静默当成功。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_messages.py -q`，真实 Pipe 覆盖部分帧、端点中断、发送阻塞、退出通知先后及线程关闭。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有重复端点、跨线程关闭和旧任务消息路径；记录门禁证据，建议以“feat: 实现报告控制通信契约”形成独立提交。

**实施状态：** `messages.py` 集中定义 version=1、64 KiB 上限的全部控制消息与严格编解码；`communication.py` 的 WorkerCommunicator 以单一通信线程拥有父进程端，先送达后关闭事件的顺序经 call_soon_threadsafe 交付。

### R5 spawn、保护、工作锁与实际回收

**预计文件：** `apps/camctl/src/camctl/reporting/worker.py`、`apps/camctl/src/camctl/reporting/supervisor.py`；测试为 `apps/camctl/tests/unit/reporting/test_worker.py` 和 `apps/camctl/tests/integration/reporting/test_worker.py`。

**接口与依赖：** 提供异步 `generate(job: GenerationJob) -> GenerationResult`、`stop_worker(reason: WorkerStopReason) -> WorkerSettlement`；Linux 适配 `install_parent_guard(expected_parent_pid: int) -> None` 与独立工作锁由子进程取得。前置交付：R3/R4、P1、S5、F1。

- [x] 编写失败用例。建立 `test_exit_checks_delivered_result`，结果已到达而退出先处理，`assert generation.is_success is True`；仅残留文件无成功消息不得发布。父保护设置前后原父退出、保护失败、旧 worker 持锁、SQLite 启动能力失败、停止/超时/成功两种先后及迟到状态库错误均覆盖。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_worker.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。由在整个 worker 生命周期内存活的主线程启动 spawn；子进程先建立 PR_SET_PDEATHSIG=SIGKILL 并核对原父，再取得独立 flock、检查实际运行库，才 ready。每任务独立只读连接，短事务读取、完成写入/摘要/同步且无活动读事务后才成功。只复用成功健康 worker，失败后确认实际退出再清理。
- [x] 再运行上述命令，要求全部 PASS，并核对 没有信号即结束或锁文件存在即占用的错误判定。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_worker.py -q`，真实 Linux spawn/Pipe/父死亡/工作锁组合；未取得锁或未确认停止前禁止临时文件清理及复用。
- [x] 审阅实际接口、状态分区及失败路径，检查 父线程寿命、句柄继承、结果与退出及全部工具资源；记录门禁证据，建议以“feat: 实现报告生成进程生命周期”形成独立提交。

**分项进度：** `worker.py` 以 spawn 启动真实子进程：父死亡保护（Linux PDEATHSIG，其余平台无操作）、有界工作锁等待、运行库检查后就绪，任务按冻结依据生成 staging 文件并改用规范文件名；`supervisor.py` 按分阶段单调时限管理启动、派发、结果判定与三段收场。Linux 父死亡保护行为无法在 Windows 开发环境验证，目标机联调时补证。

### R6 累计 ACK 及同步动作

**预计文件：** `apps/camctl/src/camctl/reporting/ack.py`、`apps/camctl/src/camctl/persistence/repositories/reporting.py`；测试为 `apps/camctl/tests/unit/reporting/test_ack_sync.py` 和 `apps/camctl/tests/integration/reporting/test_ack_sync.py`。

**接口与依赖：** 提供 `decide_ack(ack: AckInput, facts: AckFacts) -> AckDecision`、`qualifies_sync(report: AckReport, sync: SyncResponsibility) -> bool`、`decide_sync_cancel(facts: SyncFacts) -> SyncChanges`；ACK 写入由 A4 同事务消费。前置交付：R1/R2、P3；先实现纯 ACK 与同步规则，A4 随后在完整输入事务内消费。

- [x] 编写失败用例。建立 `test_equal_watermark_ack_can_end_sync`，有效 ACK 水位等当前但满足完整/局部同步，`assert sync_ended is True`；较旧报告 ID 不代表旧水位，未知报告与读取失败分别分类。未执行、运行和成功后的报告动作取消，既有待确认同步按正式规则保持/结束。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_ack_sync.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。单调累计业务水位，逐个判断报告范围和冻结历史是否满足同步；有效 ACK 无新报告循环。报告动作成功与同步等待确认是不同责任，发布结果按 R7 完成适用动作。
- [x] 再运行上述命令，要求全部 PASS，并核对 ACK 与输入原子、同水位也可结束责任，同步取消不停止共享生成。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_ack_sync.py -q`，A4/R7 实施后组合真实受理、同步、发布和 ACK 事务，核对同一事务及取消；该消费者验证归阶段 2 门禁。
- [x] 审阅实际接口、状态分区及失败路径，检查全部 ACK、重复输入、显式同步及取消入口；记录门禁证据，建议以“feat: 实现累计确认与同步规则”形成独立提交。

**分项进度：** [累计确认与同步结束审查](2026-10-02-camctl-ack-sync-review.md)记录具体矩阵与门禁。以上完整任务的勾选须包含同步开始、本地完成和取消消费者。

- [x] ACK 的精确事实模型：报告起点、终点与冻结边界；同步固定起点与开始事务末位。
- [x] 有效 ACK 在全部输入分区中原子结束合格的已有责任，同水位及较旧 ACK 仍检查资格；报告管理不增加业务水位。
- [x] 正式守卫反例、读写失败、提交结果未知、submit 接管回滚及 ACK 与同步事件的正向/逆向回放。
- [x] 同步实际开始、固定起点不存在时的动作失败及恢复。
- [x] 本地报告满足与动作成功共同保存，以及 ACK 先结束后的本地完成。
- [x] 取消消费者与纯取消规则覆盖未开始、运行和终态动作；共享生成继续执行。
- [x] CLI 对提交结果未知的完整核实与恢复装配。

### R7 发布、替换与同报告补投

**预计文件：** `apps/camctl/src/camctl/reporting/publication.py`、`apps/camctl/src/camctl/persistence/repositories/reporting.py`；测试为 `apps/camctl/tests/unit/reporting/test_publication.py` 和 `apps/camctl/tests/integration/reporting/test_publication.py`。

**接口与依赖：** 提供异步 `publish_report(report: FrozenReport, generated: GenerationResult, context: PublicationContext) -> PublicationResult`、`recover_report_files(report: FrozenReport, files: ReportLocations) -> ReportFileDecision`。前置交付：R2/R5、F1/F5、P3/P4、R6。

- [x] 编写失败用例。在 `test_processing_report_is_never_modified` 中原报告在 processing，`assert processing_mutations == 0`；满足责任的 ready 保留，新报告覆盖足够才替换。移动/同步成功后即使 C 已领取删除仍可记发布；staging 残留从头生成；两处无文件且仍需补投沿原 report_id 同字节重建。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_publication.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。完整生成及同步后按文件规则发布，目录同步可靠后记录每次发布与适用本地同步/动作结果；恢复仅用登记和符合命名的目录事实，不常规读全文比对。实际清理遵守 R5 工作锁及退出确认。
- [x] 再运行上述命令，要求全部 PASS，并核对 ready 至多一份待领取报告，processing 可与新 ready 并存。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_publication.py -q`，真实生成文件、SQLite、公共 Schema 和摘要检验，在各移动/保存边界安排协议允许的领取效果；真实 C/客户端组合由 I4/I5 验证。
- [x] 审阅实际接口、状态分区及失败路径，检查 普通 delivery 与报告补投规则是否误共用，ready 替换是否仅看 ID；记录门禁证据，建议以“feat: 实现报告发布与补投”形成独立提交。

**分项进度：** [报告字节与发布事实审查](2026-10-02-camctl-report-publication-review.md)记录下列数据库边界及验证证据；完整 R7 的勾选仍要求报告专属文件规则与真实消费者。

- [x] 首次确定字节、独立发布意图、可靠交接结果及实际失败分别保存；同一报告字节不变，成功次数及引用与最近发布历史一致。
- [x] 重复操作、读写失败、提交未知与正逆向回放保持历史及投影一致；文件已被领取删除仍可按已取得的可靠交接结果保存成功。
- [x] 报告专属目录引用、ready 替换、processing 保留、恢复观察、补投资格及同步本地完成的真实消费者。

### R8 失败触发、阶段时限与日志副本

**预计文件：** `apps/camctl/src/camctl/reporting/maintenance.py`、`apps/camctl/src/camctl/reporting/supervisor.py`；测试为 `apps/camctl/tests/unit/reporting/test_maintenance.py` 和 `apps/camctl/tests/integration/reporting/test_maintenance.py`。

**接口与依赖：** 提供异步 `maintain_reports(context: ReportMaintenanceContext) -> ReportMaintenanceResult`；context 含本次机会、时限、仓储、worker、发布及独立日志副本端口。前置交付：R2/R5/R7、S1/S5、L5；S6 随后消费报告错误和实际收场结果。

- [x] 编写失败用例。建立 `test_failure_waits_for_new_trigger`，文件/worker 失败后时间经过或重扫，`assert new_generation_calls == 0`；新业务/同步/后续正常 run 才重试。普通业务继续，状态库或历史错误则停止可靠执行；受限会话只处理一次。启动/生成总期限/停止宽限分别测试，进度不续期。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/reporting/test_maintenance.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。建议工程初值为启动 10 秒、整次生成 300 秒、停止等待 5 秒，各为有限正秒数且运行内固定；在消费代码前集中落到配置定义并同步责任文档，联调再校准。超时先失效任务、请求终止、宽限后强制终止并确认退出；停止未确认不复用文件。规定报告首次失败触发 L5，副本失败不递归。
- [x] 再运行上述命令，要求全部 PASS，并核对 失败责任保留且不自建重试循环，报告失败与状态库失败严格区分。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/reporting/test_maintenance.py -q`，真实进程、文件、数据库和日志副本组合各失败边界，其他必要设备收场并行推进。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有超时、停止、迟到错误、无新触发及会话关闭分支；记录门禁证据，建议以“feat: 实现报告失败维护与时限”形成独立提交。

### R9 完整报告与消费者验收

**预计文件：** `apps/camctl/src/camctl/reporting/encoding.py`、`apps/camctl/src/camctl/reporting/maintenance.py`；测试为 `tests/integration/test_camctl_report_contract.py`。

**接口与依赖：** 使用完整业务历史、真实报告子进程、C 领取、客户端导入与 A4 ACK。前置交付：R1—R8、C1—C8、X1—X11、N1—N5、H1—H6 及 I1—I4 的实际消费者与测试驱动；H7 提供共同审阅的独立预期。

- [x] 编写失败用例。在 `test_same_report_rebuilds_identical_bytes` 中全部动作、文件、取消与清理结果冻结后继续变化，`assert rebuilt_bytes == original_bytes` 且客户端可校验保存。不同批次/缓存/路径、同步 scope、跨计划父补齐及报告失败恢复逐项独立预期。
- [x] 运行 `uv run --project apps/camctl --group test pytest tests/integration/test_camctl_report_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。逐项映射 report-acceptance、history 查询和字段依赖条件，每种公开结果绑定正式机器 Schema 与实际客户端消费者；资源测量同时计主和报告进程。
- [x] 再运行上述命令，要求全部 PASS，并核对 报告全部字段、范围和失败恢复有真实组合证据，不以样例摘要代替生产生成。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部字段遗漏、内部事实误公开、父子关联及确定性风险；记录门禁证据，建议以“test: 验证完整状态报告闭环”形成独立提交。

## 模块完成门禁

R1—R8 首条报告链及真实 worker/通信通过；R9 全部业务及客户端消费者接入，固定报告重建字节不变。报告失败不阻止可靠普通控制，状态库错误仍按会话规则，日志副本独立交付。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
