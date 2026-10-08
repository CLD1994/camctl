# camctl 操作尝试与受管调用模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 统一尝试身份、意图及真实结束结果，管理工具生命周期并保留调用错误与可靠效果的独立含义。

**组织建议：** 公共操作模块负责结果契约、实际调用及接手；业务流程拥有预算用途、重试和终态。工具保持所属 camctl 进程组，C 模块拥有 camctl 退出后的原组回收责任。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 asyncio 子进程、精确时间与标准信号；ADB、ffprobe、ffmpeg 经过同一个受管启动边界。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [操作字段](../../camctl/database/operation-fields.md)、[ADB 执行](../../architecture/adb-execution.md)、[本地主机契约](../../host-demo/design.md#受管工具的启动与主程序回收约定)、[事务](../../camctl/database/transactions.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

普通尝试意图、身份、次数及采用配置可靠提交后才派发。结果外层 format_version=1，收场 basis 为 observed、assumed 或 not_dispatched；实际收场未完成不能保存结束结果。调用失败可与确认效果并存，本地退出不能推出远端退出或业务成功。不同查询用途、读取、删除、重拷和应急预算不互相补满。等待取消不结束实际调用，发出信号不等于退出。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

只定义跨操作共用的字段；具体 observations 类型及版本由 D2/F1 声明，业务校验必须核对适用操作、绑定和身份。

| 类型 | 字段或含义 |
| --- | --- |
| `AttemptTicket / AttemptIntent` | 原流程、活动或文件、尝试 ID、操作种类、查询用途、实际配置、责任键及可靠意图引用。 |
| `CallOutcome` | 调用状态、错误、效果分类、settlement、observations、已知 local_exit/remote_exit_code；均按正式结果结构。 |
| `ToolSpec / ManagedCall` | argv、绑定及期限、受约束输出解析端口；实际 PID、归属、停止阶段和退出结果由调用拥有者持有。 |
| `QueryResponsibility / ResultCheckRound` | 固定用途与目标、累计次数和本次依据；产物轮内分页沿用同一轮次。 |
| `OperationRepository / RecoveryFacts` | 完整意图/结果保存与原尝试读取；恢复事实含原历史及本地主机收场前提，不包含从日志猜测的数据。 |

先确认适用收场完成，再组合调用与效果。

| 实际情况 | 尝试记录 |
| --- | --- |
| 可靠未派发且不会迟到执行 | FAILED、NO_EFFECT，dispatch_prevented/1，已用次数保持。 |
| 操作成功且效果可靠 | SUCCEEDED、CONFIRMED，实际返回或结束证据。 |
| 只确认发令成功 | SUCCEEDED，效果按真实已知状态表达。 |
| 明确调用错误、没有可靠效果 | FAILED，NO_EFFECT 或 UNKNOWN 取决于证据。 |
| 可靠效果先到、调用随后错误 | FAILED 与 CONFIRMED 共同保留。 |
| 原调用结果缺失且中断恢复 | 原身份下保存恢复的未知结果及对应依据，不补造旧超时/响应/宽限。 |
| 尚未满足实际收场 | 保持 RUNNING，不写结束结果。 |

终止依次请求、有限宽限、必要时强制终止、确认退出；共享 ADB 服务端按独立生命周期处理，不使用全局 kill-server。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/operations/models.py` | 统一意图及结束结果。 |
| `apps/camctl/src/camctl/operations/validation.py` | 收场、退出、效果及证据校验。 |
| `apps/camctl/src/camctl/operations/attempts.py` | 意图与结果保存组织。 |
| `apps/camctl/src/camctl/operations/process.py` | 受管工具启动、期限及实际回收。 |
| `apps/camctl/src/camctl/operations/queries.py` | 查询用途与整轮核实。 |
| `apps/camctl/src/camctl/operations/recovery.py` | 原尝试恢复及取消接手。 |
| `apps/camctl/src/camctl/persistence/repositories/operations.py` | 完整操作事务及实际目标归属校验。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享与持久化](2026-09-30-camctl-persistence.md) | K1—K3、P3/P4 的原子保存及核实。 |
| [设备与文件](2026-09-30-camctl-devices.md) | D2、F1 的证据登记及结果，不消费任意厂商文本。 |
| [会话与调度](2026-09-30-camctl-session.md) | S5 接手实际结果；Q4 授予先固定接口。 |
| [跨组件集成](2026-09-30-camctl-integration.md) | I1/I2 实施 C 组身份、退出观察及最后回收前提。 |

O1/O2 的端口与类型在首个设备操作前稳定；O3 接入真实工具后才能使用结束结果。O4、O5 随每种业务责任接入，O6 核验真实主机组合；不能只通过进程替身声明系统保证成立。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 本地退出 255 不伪造远端 255。 | O1，`test_local_255_has_no_remote_result` |
| 意图已提交但未派发仍保留次数。 | O2，`test_prevented_dispatch_keeps_attempt_count` |
| 发送 SIGKILL 后仍不能立即结束尝试。 | O3，`test_signal_is_not_exit` |
| 轮内分页不新增核实轮次。 | O4，`test_pages_share_result_check_round` |
| 旧结果未知时不能以新预算重复副作用。 | O5，`test_unknown_attempt_does_not_redispatch` |

## 实施任务

### O1 完整结束结果及证据校验

**预计文件：** `apps/camctl/src/camctl/operations/models.py`、`apps/camctl/src/camctl/operations/validation.py`；测试为 `apps/camctl/tests/unit/operations/test_results.py`。

**接口与依赖：** 提供 `validate_outcome(ticket: AttemptTicket, outcome: CallOutcome, evidence: EvidenceRegistry) -> ValidatedOutcome`。前置交付：K1/K2、D2/F1 的证据接口定义。

`ValidatedOutcome` 保留校验时不可变的完整票据上下文。首次保存、迟到结果和原键重送均须核对该上下文与当前票据精确相符；无观察或无身份观察也不能省略此检查。同票据重新校验的等价结果允许使用，运行时上下文不另行持久化。具体门禁见[普通结果复用](2026-10-02-camctl-attempt-result-reuse.md)。

- [x] 编写失败用例。建立 `test_local_255_has_no_remote_result`，只有本地 exit_code=255，`assert 'remote_exit_code' not in result.call_info`；有可信远端 255 才保存。缺 settlement、未知证据版本、观察与操作不匹配、空 observations 与 SQL NULL、成功与错误组合逐项验证。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/operations/test_results.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按 format_version=1 及正式类型化对象验证全部字段和保证范围；保留真实成功观察与调用错误，原始输出不进入无限观察列表。
- [x] 再运行上述命令，要求全部 PASS，并核对 R-01—R-05、R-13/R-14 的结果分区可以独立证伪。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有调用结果编码、历史恢复和报告消费者是否误混调用/效果；记录门禁证据，建议以“feat: 定义与验证统一操作结果”形成独立提交。

### O2 意图、预算与完整结果事务

**预计文件：** `apps/camctl/src/camctl/operations/attempts.py`、`apps/camctl/src/camctl/persistence/repositories/operations.py`；测试为 `apps/camctl/tests/integration/operations/test_attempts.py`。

**接口与依赖：** 提供异步 `begin_attempt(command: AttemptIntent, key: OperationKey) -> DbOutcome[AttemptTicket]`、`finish_attempt(ticket: AttemptTicket, result: ValidatedOutcome, key: OperationKey) -> DbOutcome[OperationResult]`；OperationResult 含共同提交事实及通知目标。前置交付：P3/P4、H1/H2、O1；首次录像使用 Q4 组合操作。

- [x] 编写失败用例。建立 `test_prevented_dispatch_keeps_attempt_count`，意图提交后取消，`assert used_attempts == 1` 且 driver 未调用；意图失败则不派发。结果与设备/文件事实任一保存错误整组回滚，提交未知先核实；已有终态不会被迟到结果覆盖。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/operations/test_attempts.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。在原责任下占用预算及保存意图，再提交真实结果和适用等待/活动/文件事实；业务重试及动作完成由所属纯规则决定。
- [x] 再运行上述命令，要求全部 PASS，并核对 各操作只取得一份真实结束记录，普通尝试意图引用不可省略。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部业务入口是否绕过先提交或拆开效果与尝试结果；记录门禁证据，建议以“feat: 实现操作意图及结果事务”形成独立提交。

### O3 受管工具期限、停止与实际退出

**预计文件：** `apps/camctl/src/camctl/operations/process.py`；测试为 `apps/camctl/tests/unit/operations/test_process.py` 和 `apps/camctl/tests/integration/operations/test_process.py`。

**接口与依赖：** 提供异步 `execute_tool(spec: ToolSpec, stop: StopSignal) -> RawToolOutcome`；RawToolOutcome 含实际退出、受约束输出及错误，设备解释由 D3 完成。前置交付：S5、系统调用/时钟端口及固定终止宽限配置。

- [x] 编写失败用例。建立 `test_signal_is_not_exit`，信号已发送但 wait 未返回，`assert call.is_finished is False`；已退出、宽限内退出、到期升级、发送时恰好退出、重复取消不续期分别覆盖。工具 stdout 不进入 CLI 结果，部分输出不延长调用总期限。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/operations/test_process.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。异步启动并捕获输出，不创建新进程组或转交组外执行；在所属目标范围终止并确认 actual exit/资源关闭，工具与包装程序全部使用统一启动路径。普通 ADB 使用实际精确 terminate_grace_s，恢复无原值时不补造。
- [x] 再运行上述命令，要求全部 PASS，并核对 本地实际收场才允许结束结果或冲突新操作。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/operations/test_process.py -q`，真实 Linux 工具、包装后代和输出管道，覆盖正常、超时及取消收场；真实 C 原组收场由根 O6 用例核验。
- [x] 审阅实际接口、状态分区及失败路径，检查 ADB、ffmpeg、ffprobe 及包装程序所有启动入口和句柄继承；记录门禁证据，建议以“feat: 实现受管工具生命周期”形成独立提交。

- [x] 按[媒体结果计划的进程异常收场契约](2026-10-03-camctl-media-results-review.md#m3b-的进程异常收场契约)验证启动分类、发送信号时恰好退出、两次信号发送失败、输出读取失败及组合错误。输出失败触发原终止流程；辅助观察任务完成收场，媒体文件占用持续到实际工作结束，ADB 保留传输与响应错误及可靠观察。

### O4 独立查询责任与产物核实轮次

**预计文件：** `apps/camctl/src/camctl/operations/queries.py`；测试为 `apps/camctl/tests/unit/operations/test_queries.py` 和 `apps/camctl/tests/integration/operations/test_queries.py`。

**接口与依赖：** 提供 `validate_query_scope(scope: QueryResponsibility) -> None`、`decide_query_next(facts: QueryFacts) -> QueryDecision`、`finish_check_round(round: ResultCheckRound, observations: CheckSet) -> CheckDecision`；这些类型按所属正式五种查询用途及核实结果定义。前置交付：O1/O2，目标活动的原设备配置。

- [x] 编写失败用例。建立 `test_pages_share_result_check_round`，一轮三批文件查询，`assert rounds_used == 1`；重启未完整结束的轮次按规则重新检查计新轮次，完整结果提交未知先核实。正常查询响应仍可能需要继续责任，不支持状态查询的任务 `assert query_attempts == 0`。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/operations/test_queries.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。用途创建后不可改变，责任键、目标组合和配置完整保存；轮内失败不隐藏重发调用，状态查询与文件结果核实分别预算。可靠停止响应可满足已有判断时不为流程形式重复查询。
- [x] 再运行上述命令，要求全部 PASS，并核对 Q-01—Q-12 逐项有用途、次数、间隔和归属断言。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/operations/test_queries.py -q`，真实仓储与拍摄/残留收场替身组合，五种用途与跨设备配置保持独立。
- [x] 审阅实际接口、状态分区及失败路径，检查 切换查询用途、轮内分页、恢复及配置改变是否刷新原预算；记录门禁证据，建议以“feat: 实现独立查询与核实责任”形成独立提交。

### O5 原尝试恢复及取消结果接手

**预计文件：** `apps/camctl/src/camctl/operations/recovery.py`；测试为 `apps/camctl/tests/unit/operations/test_recovery.py` 和 `apps/camctl/tests/integration/operations/test_recovery.py`。

**接口与依赖：** 提供 `recover_attempt(facts: RecoveryFacts) -> AttemptRecoveryDecision`、异步 `settle_owned_call(call: ManagedCall, owner: ResponsibilityOwner) -> None`。前置交付：O1—O4、P4、S5。

- [x] 编写失败用例。建立 `test_unknown_attempt_does_not_redispatch`，只有意图且主机收场已可靠完成，`assert decision.redispatch is False`；保存对应恢复依据但不补造原超时、退出、时刻或配置。已确认读取错误与未保存读取失败分开，取消后可靠结果仍保存。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/operations/test_recovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按原身份和运行假设适用范围核实；本地收场完成不生成拍摄结束或删除成功，实际新观察另保存。接手结果前原拥有者及资源不释放。
- [x] 再运行上述命令，要求全部 PASS，并核对 未知、已失败、可续传和可靠未派发没有混用。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/operations/test_recovery.py -q`，调用返回、收场、结果回滚/未知及重启各边界验证历史和当前事实。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有副作用恢复是否根据剩余额度直接发令；记录门禁证据，建议以“feat: 实现原尝试恢复与接手”形成独立提交。

### O6 主机收场与下一调用组合

**预计文件：** `apps/camctl/src/camctl/operations/process.py`；测试为 `tests/integration/test_camctl_process_recovery.py`、`tests/integration/_host_recovery_entry.py`，C 同步驱动位于 `apps/host-demo/tests/integration/host_driver.c`。

**接口与依赖：** 使用真实 CLI/C 启动与 operations 接口；C 修改由 I1/I2 拥有，不在 Python 写主机回收逻辑。前置交付：I1/I2、O3/O5。

- [x] 编写失败用例。在 `test_next_run_waits_for_old_group_settlement` 中旧 camctl 已退出而工具或线程仍活，`assert next_run_started is False`；实际原组全部停止及最终回收后才按原 pending-start/预算放行。覆盖共享模拟服务端脱离前后、并行 submit 与其他组不受影响。
- [x] 运行 `uv run --project apps/camctl --group test pytest tests/integration/test_camctl_process_recovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。用真实本地进程验证既定主机前提及 Python 启动归属，进程输出诊断不作为业务历史恢复证据；目标实际工具与主程序回收协作另列联调。
- [x] 再运行上述命令，要求全部 PASS，并核对 R-08—R-12 与验收 32—36 的软件组合有证据。
- [x] 审阅实际接口、状态分区及失败路径，检查 系统级放行与业务层资格是否互相代替；记录门禁证据，建议以“test: 验证受管调用与主机收场”形成独立提交。

## 模块完成门禁

O6 的真实 C/CLI 组合记录见[跨组件计划 I1/I2 验证记录](2026-09-30-camctl-integration.md#i1i2-验证记录2026-10-08linux-x86_64)：2026-10-08 Linux x86_64、CPython 3.11.16，8 项通过。生产工具启动沿用 `operations.process`，C 模块完成原组收场及最终回收；测试没有在 Python 实现主机放行。固定 ADB 版本、真实服务端行为和第三方实际回收仍按各自部署验收执行。

统一意图/结果、真实工具收场和各查询用途均通过；普通及恢复结果保留原证据和版本。设备效果与本地生命周期各由自己的证据证明，C 主机组合有独立软件验收。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
