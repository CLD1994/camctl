# camctl 持久化与事务执行模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 提供专用数据库线程、完整事务、可靠结果确认和窄仓储实现，确保副作用只依赖已提交事实。

**组织建议：** 数据库连接由所属线程创建和使用；私有执行器处理有界队列和结果。业务仓储以完整用例提交，事务内调用纯规则和事件应用器。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 sqlite3、线程、asyncio 通知和既定 WAL/FULL 配置；复用 SQLite 忙等待，不增加应用层锁忙重试。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [数据库执行](../../camctl/persistence-runtime.md)、[完整事务](../../camctl/database/transactions.md)、[历史与 SQLite](../../camctl/history-storage.md)、[运行库条件](../../camctl/sqlite-runtime.json)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

数据库历史、投影、历史对象目录、实际报告变化及快照进度共同提交。等待容量默认 64 个完整操作，业务与快照合计计量；入队最多等待 9 秒，成功入队后无排队超时。业务优先选择下一操作但不打断已开始操作。只在所属线程控制连接，日常 mode=rw、报告 mode=ro，禁止隐式建库及报告 immutable=1。提交未知先核实原操作，不能再次派发外部效果。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

执行器是适配器内部边界，业务流程使用各模块 Repository。读错误显式返回错误或抛出 DbReadError，不能返回有效空批次。

| 类型 | 字段或含义 |
| --- | --- |
| `DbJob[T] / DbPriority` | job 含完整执行函数、操作身份、输入与目标摘要、读写种类及业务/快照优先级；只有适配器可创建。 |
| `DbOutcome[T]` | kind 为 COMPLETED、NOT_EXECUTED、ROLLED_BACK、UNKNOWN；成功值、错误、完整提交边界及实际阶段按分区适用。读接口使用 ReadReceipt[T]，读取错误单独表达。 |
| `CommitLookup[T]` | 已存在且一致、可靠不存在且原操作不可能迟到、仍在途、查询错误、输入不一致五种核实结果。 |
| `WriteReceipt / ReadReceipt[T]` | 写结果及本事务完整边界；读结果及读取绑定边界，跨线程对象具有独立所有权。 |
| `EventBatch / ChangeSet` | 由 H1/H2 产生的完整事件与关联变更，包含最终事务首尾范围及适用目录和维护事实。 |

队列占位、实际执行和结果完成分别计量。

| 操作实际状态 | 取消或时限结果 |
| --- | --- |
| 入队前截止且以后不会入队 | NOT_EXECUTED，释放等待关系，不撤销已有设备事实。 |
| 已入队尚未开始，确认撤回 | NOT_EXECUTED，只释放一次等待条目。 |
| 撤回与派发竞争尚未解决 | 保留责任，不返回未执行。 |
| 已开始并提交成功 | COMPLETED，接手方消费提交结果并通知。 |
| 提交前错误且确认回滚 | ROLLED_BACK，没有本次成功事实；保留错误。 |
| 提交、回滚或连接结果未知 | UNKNOWN，停用连接并核实原 operation_key。 |

读操作失败保留实际错误；没有写事务时不伪造回滚事实。业务入队超时进入 state_db_error；仅快照确认未入队超时由 H6 停用本次维护。已入队的迟到入队计时通知无效。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/persistence/runtime.py` | 运行库、存在性、模式及格式检查。 |
| `apps/camctl/src/camctl/persistence/executor.py` | 队列、优先选择、派发撤回与线程通知。 |
| `apps/camctl/src/camctl/persistence/models.py` | 结果、任务及核实分类。 |
| `apps/camctl/src/camctl/persistence/transaction.py` | 显式事务、编号范围和完整校验。 |
| `apps/camctl/src/camctl/persistence/recovery.py` | 按操作身份核实提交结果。 |
| `apps/camctl/src/camctl/persistence/repositories/` | 各业务端口的 SQLite 实现；随所属任务增加。 |
| `apps/camctl/src/camctl/persistence/cache.py` | 有界读取缓存及可靠失效。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享类型](2026-09-30-camctl-contracts.md) | K1—K3 的值、边界及精确文本。 |
| [历史](2026-09-30-camctl-history.md) | P3 使用 H1/H2 的事件应用与目录规则；先固定接口再分别实现。 |
| [各业务计划](2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口) | 仓储由对应业务任务定义完整命令与原子结果，P5 提供组合边界。 |
| [会话](2026-09-30-camctl-session.md) | S5 接手取消等待后的实际结果。 |

P1、P2 在首阶段完成；P3 与 H1/H2 联合建立首批事件事务；P4 在首次未知提交路径前完成；P5 随业务增加窄仓储，P6 随历史规模验收完成。P3 不依赖历史查询或完整快照模块。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 排队超过 9 秒不会变成排队超时。 | P2，`test_admitted_job_has_no_queue_timeout` |
| 取消等待后实际提交仍有人接手。 | P2，`test_started_job_survives_waiter_cancel` |
| 缓存旧行不能覆盖独立 ACK。 | P3，`test_write_uses_current_transaction_state` |
| 未知且仍可能迟到提交时不能重做。 | P4，`test_inflight_unknown_cannot_retry` |
| 日常缺失库不会被自动创建。 | P1，`test_missing_database_is_not_created` |

## 实施任务

### P1 既有库打开与运行条件核验

**预计文件：** `apps/camctl/src/camctl/persistence/runtime.py`；测试为 `apps/camctl/tests/integration/persistence/test_runtime.py`。

**接口与依赖：** 提供 `open_existing(path: Path, mode: DbOpenMode, config: DbConfig) -> OwnedConnection`，只能在所属线程调用；DbOpenMode 只有既有读写与既有只读。前置交付：B1、K1、K2，权威运行库资源。

- [x] 编写失败用例。建立 `test_missing_database_is_not_created`，不存在的路径打开失败，`assert not path.exists()`；覆盖目录类型、无效格式、不同库版本、特殊 URI 路径、只读写入拒绝、WAL/FULL 及 busy_timeout 的实际值。用权威运行条件的允许和拒绝分区检查运行库，而不复制完整版本清单。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/persistence/test_runtime.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。检查实际 sqlite3 链接库能力，再以正确 URI 打开既有库；验证整体格式和元信息，不隐式升级、修复 journal_mode 或创建运行状态。
- [x] 再运行上述命令，要求全部 PASS，并核对 日常入口与报告子进程都使用同一运行条件检查。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有连接创建是否绕过 mode、格式、忙等待或线程所有权；记录门禁证据，建议以“feat: 实现既有状态库打开与校验”形成独立提交。

### P2 有界队列、优先派发及结果接手

**预计文件：** `apps/camctl/src/camctl/persistence/executor.py`、`apps/camctl/src/camctl/persistence/models.py`；测试为 `apps/camctl/tests/unit/persistence/test_executor.py` 和 `apps/camctl/tests/integration/persistence/test_executor.py`。

**接口与依赖：** 提供 `DbExecutor.submit_write(job: DbJob[T]) -> DbOutcome[T]`、`submit_read(job: DbJob[T]) -> ReadReceipt[T]`、`withdraw(job_id: OperationKey) -> WithdrawalResult`、`close() -> None`；均为异步入口，WithdrawalResult 表达撤回确认或仍在执行。前置交付：K3、S5 的接手端口契约；连接执行替身。

- [ ] 编写失败用例。建立 `test_admitted_job_has_no_queue_timeout`，已入队等待超过 9 秒仍只执行一次；建立 `test_started_job_survives_waiter_cancel`，取消后结果交给责任拥有者，`assert deliveries == 1`。容量 1、恰好满载、业务与维护竞争、空位与截止竞争、撤回与派发竞争逐项验证。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/persistence/test_executor.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。在同一短临界区决定入队截止、派发或撤回，队列取出时释放容量，实际结束时才完成结果；业务优先，不持锁等待，线程以事件循环安全通知交回独立结果。
- [ ] 再运行上述命令，要求全部 PASS，并核对 没有超时后迟到入队、双执行、双释放或结果丢失。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/persistence/test_executor.py -q`，真实专用线程与 SQLite 组合，并保持事件循环能推进；关闭等待已开始任务实际结束。
- [ ] 审阅实际接口、状态分区及失败路径，检查 入队、排队和实际执行三种等待是否被同一个 timeout 混合；记录门禁证据，建议以“feat: 实现数据库队列与取消接手”形成独立提交。

### P3 完整事件事务与编号范围

**预计文件：** `apps/camctl/src/camctl/persistence/transaction.py`；测试为 `apps/camctl/tests/integration/persistence/test_transactions.py`。

**接口与依赖：** 提供私有 `commit_operation(command: AtomicCommand, key: OperationKey, rules: WriteRules) -> WriteReceipt`；AtomicCommand/WriteRules 由所属仓储的具体类型实现，不对业务暴露表操作。前置交付：H1/H2、K3、P1/P2。

- [ ] 编写失败用例。建立 `test_write_uses_current_transaction_state`，并发 ACK 与动作结果提交，`assert ack_after >= ack_before` 且动作事实保留；逐个中断事件、投影、两类目录和进度保存，断言整组回滚。最终事件数变化时核对引用 L 及首尾，事务内中间边界不得冻结报告。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/persistence/test_transactions.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。显式 BEGIN IMMEDIATE；重新读取可靠旧状态，确定完整事件数量和最终 F～L，再校验并保存全部关联。数量改变须重新形成受影响引用或整笔回滚，不能继续使用旧 L。
- [ ] 再运行上述命令，要求全部 PASS，并核对 事务、编号、各对象历史和公开变化对应同一最终范围。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有仓储是否用旧缓存整行覆盖或自行拆分提交；记录门禁证据，建议以“feat: 实现完整历史与投影事务”形成独立提交。

### P4 提交未知的身份核实

**预计文件：** `apps/camctl/src/camctl/persistence/recovery.py`；测试为 `apps/camctl/tests/unit/persistence/test_commit_recovery.py` 和 `apps/camctl/tests/integration/persistence/test_commit_recovery.py`。

**接口与依赖：** 提供 `resolve_commit(key: OperationKey, expected: OperationIdentity, lookup: CommitLookup[T]) -> RecoveryDecision[T]`；OperationIdentity 含输入、阶段和目标，RecoveryDecision 为复用、原资格下可重做、等待或错误。前置交付：P3 的完整操作记录。

- [ ] 编写失败用例。建立 `test_inflight_unknown_cannot_retry`，查询暂时不存在但原线程仍可能提交，`assert decision.can_retry is False`；已存在一致返回原结果，输入不一致拒绝，查询失败保留未知，确认不存在且原事务不能迟到才允许检查原资格。成功副作用后保存未知不得自动再次调用。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/persistence/test_commit_recovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。停用失效连接，在新可靠连接上核实完整操作；取消不会更换 key，内存分配的事件 ID 不作为提交证明。
- [ ] 再运行上述命令，要求全部 PASS，并核对 每个核实分区具有唯一结果，错误和未知不解释为空。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/persistence/test_commit_recovery.py -q`，在真实事务提交前后与响应交付前中断，验证恢复使用原结果及原预算。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有写结果恢复入口是否存在先重做再查询的路径；记录门禁证据，建议以“feat: 实现提交未知的可靠核实”形成独立提交。

### P5 窄仓储及短读事务

**预计文件：** `apps/camctl/src/camctl/persistence/repositories/acceptance.py`、`apps/camctl/src/camctl/persistence/repositories/session.py`、`apps/camctl/src/camctl/persistence/repositories/scheduling.py`、`apps/camctl/src/camctl/persistence/repositories/operations.py`、`apps/camctl/src/camctl/persistence/repositories/capture.py`、`apps/camctl/src/camctl/persistence/repositories/outputs.py`、`apps/camctl/src/camctl/persistence/repositories/cancellation.py`、`apps/camctl/src/camctl/persistence/repositories/reporting.py`、`apps/camctl/src/camctl/persistence/repositories/history.py`；测试为 `apps/camctl/tests/integration/persistence/test_repositories.py`。

**接口与依赖：** 实际实现 A4、S4、Q4、O2、C3、X3/X5/X7/X8、N3、R2/R7 和 H6 拥有的 Repository 端口；本任务不新增表级公共接口。前置交付：对应业务命令及纯规则已在各计划的接口任务定义。

- [ ] 编写失败用例。建立 `test_repository_returns_detached_batch`，本批返回后 `assert active_cursors == 0` 且无读事务；错误不得返回空批。每个新仓储调用组合真实事务，断言意图先于外部调用、同事务必要记录齐全以及跨线程集合不会被另一方修改。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/persistence/test_repositories.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。逐用例增加适配器，用 P3 统一提交；查询按固定范围和索引分页，分页结果遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)，转换为业务拥有的类型后结束游标和事务再返回。
- [ ] 再运行上述命令，要求全部 PASS，并核对 业务流程没有 SQLite 连接，完整用例没有多个自提交函数。
- [ ] 审阅实际接口、状态分区及失败路径，检查 新建仓储是否只验证 SQL 行内约束而遗漏具名业务校验；记录门禁证据，建议以“feat: 接入完整用例的 SQLite 仓储”形成独立提交。

### P6 缓存与实际读取成本

**预计文件：** `apps/camctl/src/camctl/persistence/cache.py`；测试为 `apps/camctl/tests/integration/persistence/test_cache_queries.py`。

**接口与依赖：** 提供 `cache_get(key: StateCacheKey) -> CacheLookup[T]`、`cache_put(key: StateCacheKey, value: T) -> None`；StateCacheKey 含数据库身份、H/C、对象身份与全部字段依赖版本，CacheLookup 明确命中或未命中。前置交付：H4/H5、R3；本批实际仓储。

- [ ] 编写失败用例。在 `test_dependency_change_invalidates_parent_cache` 中仅改变关联 delivery/file，`assert public_result == expected_changed_result`，即使父对象自身计数未变也不能命中旧结果。改变容量与命中率后固定 H 字节相同；真实代表性数据 ANALYZE 后检查索引与读取量。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/persistence/test_cache_queries.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。只缓存读取结果，按容量释放终态对象；提交结果用于本次通知不直接充当永久最新状态。容量是工程建议，实施时以实际结构明确上限并记录测量，不引入推测性资源协调器。
- [ ] 再运行上述命令，要求全部 PASS，并核对 缓存及分页不会改变固定历史结果，候选扩展来自持久化查询。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有父对象和关联缓存是否只依赖自身计数或 latest_plan_id；记录门禁证据，建议以“perf: 实现有界状态缓存与查询验证”形成独立提交。

## 模块完成门禁

P1—P4 的真实线程及 SQLite 组合通过；各业务仓储都在所属模块门禁中证明完整原子操作。V、E、J 的适用验收及每个业务的提交前中断、确认回滚、成功未通知和未知提交有明确用例；P6 以真实负载证据完成。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
