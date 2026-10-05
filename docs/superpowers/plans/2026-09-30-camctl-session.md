# camctl 会话接管与责任监督模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 建立可靠执行资格、事务内接管、时钟受限路径和实际任务监督，使会话退出不遗漏工作。

**组织建议：** 会话只管理资格和责任生命周期，通过窄接口调用业务流程；待处理分类采用同一纯规则，submit 交接与关闭接纳在写事务内组织。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 Linux flock、asyncio、带单位的时钟端口及 P2 数据库执行器；不建立持久化会话协调表。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [会话协议](../../architecture/protocol-session.md)、[CLI 启动](../../architecture/cli-commands.md)、[时钟恢复](../../architecture/clock-recovery.md)、[会话错误](../../architecture/session-errors.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

run 持有会话锁直到资源清理，只有普通执行资格成立才持有接纳锁；submit 不取得会话锁。锁文件稳定，非冲突错误不能解释为有/无接纳者。needs_run 与正常退出用同一责任分类，未来动作是工作，单独快照、可延后半成品清理、仅等待 ACK 和无主动收场的残留设备事实不是工作。未知不能判无工作。接纳关闭后保持关闭，不因失败重新恢复资格。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

责任监督器跟踪实际工作及结果，不能只跟踪等待协程。错误的主次排序沿用现有会话错误规则，日志副本独立于数据库。

| 类型 | 字段或含义 |
| --- | --- |
| `WorkFacts / WorkDecision` | 可靠动作、必要收场、报告责任、可延后维护和本次报告机会状态；决策为需驱动、可成功退出、报告失败退出或事实错误。 |
| `SessionLocks / AdmissionProbe` | 稳定会话与接纳句柄，探测结果为冲突、取得并释放、错误；句柄不可继承给工具。 |
| `ClockCheck / ExecutionMode` | 可靠下界、当前 UTC 和单调读数及有限复检结果；正常、受限或致命模式分别表达。 |
| `ResponsibilityOwner / OwnedTask` | 拥有者提供异步接手结果与实际结束凭据；任务登记身份、阶段、资源和所属业务。 |
| `SessionOutcome / CloseDecision` | 机器会话成功或既有 reason/details 错误；关闭决定包含是否保持接纳及仍需推进的责任。 |

submit 交接由可靠事务状态、待处理责任及接纳观察共同决定。

| 事务及事实 | 有待处理工作 | 接纳探测 | 判定 |
| --- | --- | --- | --- |
| 可靠且提交成功 | 否 | 不需要 | needs_run=False。 |
| 可靠且提交成功 | 是 | 真正冲突 | needs_run=False，由接纳者负责。 |
| 可靠且提交成功 | 是 | 临时取得并可靠释放 | needs_run=True。 |
| 状态、锁或提交失败/未知 | 任意 | 任意 | 会话错误，不输出成功判断。 |

正常关闭在同一写事务内重新检查工作并释放接纳；有新工作就保持接纳。报告失败关闭还须核对失败机会边界与最新变化，只剩该次已尝试报告责任时保留责任并返回 report_error。状态库失效允许错误路径释放锁，不伪装正常关闭。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/session/work.py` | 统一责任分类。 |
| `apps/camctl/src/camctl/session/locks.py` | 稳定 flock 句柄与探测。 |
| `apps/camctl/src/camctl/session/clock.py` | 启动时钟资格及下界更新。 |
| `apps/camctl/src/camctl/session/handoff.py` | 事务内接纳和 submit 交接规则。 |
| `apps/camctl/src/camctl/session/supervision.py` | 实际任务拥有者及取消接手。 |
| `apps/camctl/src/camctl/session/service.py` | 正常与受限 run 的生命周期。 |
| `apps/camctl/src/camctl/persistence/repositories/session.py` | 工作检查、可信下界及关闭事务。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [持久化](2026-09-30-camctl-persistence.md) | P2—P4 返回实际结果；S4 使用完整事务。 |
| [受理](2026-09-30-camctl-acceptance.md) | A4 共用输入，S4 的探测在同一输入事务内完成。 |
| [调度](2026-09-30-camctl-scheduling.md) | Q3/Q5 发现和推进正常工作。 |
| [采集](2026-09-30-camctl-capture.md) | C7 提供既定有限及应急停止；不存在设备责任时不用等待该能力。 |
| [报告与日志](2026-09-30-camctl-reporting.md) | R7/R8 及 L5/L6 给出独立完成证据。 |

S1、S2、S4 的端口先固定，允许受理首阶段组合；S3、S5、S6 实现首条报告链。设备责任接入时扩展同一监督及错误路径，不另建录像会话管理器。受限路径的软件门禁随 C7 完整验证。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 只剩未来动作仍需当前 run 驱动。 | S1，`test_future_pending_is_work` |
| 锁操作错误不能当成没有接纳者。 | S2，`test_lock_error_is_not_free` |
| 受理成功但时钟失败时保留计划。 | S3，`test_clock_failure_preserves_acceptance` |
| 关闭与 submit 竞争不能漏交接。 | S4，`test_submit_races_admission_close` |
| 等待者取消不会释放实际资源。 | S5，`test_cancel_keeps_actual_owner` |

## 实施任务

### S1 统一待处理责任分类

**预计文件：** `apps/camctl/src/camctl/session/work.py`；测试为 `apps/camctl/tests/unit/session/test_work.py`。

**接口与依赖：** 提供 `classify_work(facts: WorkFacts) -> WorkDecision`。前置交付：K1；已有责任与报告机会契约。

- [x] 编写失败用例。建立 `test_future_pending_is_work`，未来 pending 仍 `assert decision.needs_driver is True`；分别仅剩快照、ACK 等待、可延后清理及无主动收场残留时为 False。新诊断报告与残留并存仍有责任，未知事实必须错误；报告失败但存在新变化要再次处理。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/session/test_work.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。把各责任维度显式建模并按存在任意必要工作判断；不依据内存队列为空或 latest_plan_id 未变决定。
- [x] 再运行上述命令，要求全部 PASS，并核对 submit 和退出消费者使用同一分类及例外语义。
- [x] 审阅实际接口、状态分区及失败路径，检查 终态动作下的独立调用、设备、交付和报告是否漏纳入；记录门禁证据，建议以“feat: 定义会话待处理责任”形成独立提交。

### S2 稳定锁与启动前提

**预计文件：** `apps/camctl/src/camctl/session/locks.py`；测试为 `apps/camctl/tests/unit/session/test_locks.py` 和 `apps/camctl/tests/integration/session/test_locks.py`。

**接口与依赖：** 提供 `acquire_session() -> SessionLease`、`probe_admission() -> AdmissionProbe`、`release_admission(lease: AdmissionLease) -> None`；SessionLease/AdmissionLease 表达真实持有句柄。前置交付：B2 的定位配置；系统调用端口替身。

- [x] 编写失败用例。建立 `test_lock_error_is_not_free`，非冲突 errno 抛锁错误，`assert probe.is_free is False`；取得探测锁后释放失败不产生成功判断。锁文件已存在但无人持锁仍能取得，子进程不能继承句柄。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/session/test_locks.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。集中适配 flock 与非继承句柄，保持文件稳定；run/init 使用会话锁，submit 只在事务内探测接纳锁。
- [x] 再运行上述命令，要求全部 PASS，并核对 只有真正冲突代表已有接纳者。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/session/test_locks.py -q`，真实多进程 flock 验证互斥、释放、异常退出及句柄隔离。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部锁路径、文件清理及 finally 是否删除锁文件或误释放他人资格；记录门禁证据，建议以“feat: 实现稳定会话与接纳锁”形成独立提交。

### S3 受理后时钟资格与受限执行

**预计文件：** `apps/camctl/src/camctl/session/clock.py`、`apps/camctl/src/camctl/session/service.py`；测试为 `apps/camctl/tests/unit/session/test_clock.py` 和 `apps/camctl/tests/integration/session/test_clock.py`。

**接口与依赖：** 提供 `check_clock(input: ClockCheckInput, clock: ClockPort) -> ClockCheck`、异步 `enter_execution(check: ClockCheck, context: SessionContext) -> ExecutionMode`；输入含可靠历史下界及已有时间策略。前置交付：A4、P3；ClockPort 同时提供精确 UTC 与单调读数。

- [x] 编写失败用例。建立 `test_clock_failure_preserves_acceptance`，输入已提交而复检仍失败，`assert accepted_plan_unchanged`，普通设备启动次数为 0；未定时取消、必要停止和一次报告按受限规则可进行。可信下界每会话最多更新一次，受理前不可信读数不得推进。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/session/test_clock.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。遵守启动顺序：验证库、受理、查下界、检查/有限复检、正常时可靠保存下界，再取得接纳；失败则只执行规定范围并返回 clock_invalid。
- [x] 再运行上述命令，要求全部 PASS，并核对 等待复检不占写事务，受限会话不持普通接纳资格。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/session/test_clock.py -q`，真实 SQLite 与时钟替身覆盖受理、下界提交、复检、报告及重启，结合 C7 验证录像安全收场。
- [x] 审阅实际接口、状态分区及失败路径，检查 普通执行和受限执行的全部入口是否绕过时钟资格；记录门禁证据，建议以“feat: 实现时钟资格与受限会话”形成独立提交。

### S4 事务内接管与接纳关闭

**预计文件：** `apps/camctl/src/camctl/session/handoff.py`、`apps/camctl/src/camctl/persistence/repositories/session.py`；测试为 `apps/camctl/tests/integration/session/test_handoff.py` 和 `apps/camctl/tests/integration/session/test_handoff.py`。

**接口与依赖：** 提供纯 `decide_handoff(facts: WorkFacts, probe: AdmissionProbe) -> HandoffDecision`；仓储 `close_admission(command: CloseAdmission, key: OperationKey) -> DbOutcome[CloseDecision]`；submit 的同一规则嵌入 A4。前置交付：S1/S2、P3/P4；输入结果及接管命令类型先固定，A4 随后消费。

- [x] 编写失败用例。建立 `test_submit_races_admission_close`，用同步点固定两种事务先后，`assert current_handles_work or response.needs_run`；多个 submit 探测互不误认。报告失败后新变化先提交则继续，关闭先结束后新提交则请求后续 run。锁释放后提交错误保持接纳关闭。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/session/test_handoff.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。BEGIN IMMEDIATE 中查最新工作再进行非阻塞锁操作；设备、文件和报告生成不得放入交接事务。只在可靠提交后返回成功 needs_run。
- [x] 再运行上述命令，要求全部 PASS，并核对 输入与探测同事务，关闭前没有遗漏已接纳工作。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/session/test_handoff.py -q`，A4 实施后组合真实受理、锁及关闭事务，交错最后一次检查与并发提交；该消费者验证归阶段 1/2 门禁。
- [x] 审阅实际接口、状态分区及失败路径，检查 正常退出、报告失败退出、时钟受限和致命退出的资格区别；记录门禁证据，建议以“feat: 实现原子会话接管与关闭”形成独立提交。

### S5 实际任务监督与取消接手

**预计文件：** `apps/camctl/src/camctl/session/supervision.py`；测试为 `apps/camctl/tests/unit/session/test_supervision.py` 和 `apps/camctl/tests/integration/session/test_supervision.py`。

**接口与依赖：** 提供 `register(owner: ResponsibilityOwner) -> OwnerToken`、`handoff(token: OwnerToken, pending: OwnedTask) -> None`、异步 `drain_required() -> SupervisionResult`；先登记责任再解除原等待。前置交付：K1/K3 及本模块的 ResponsibilityOwner、OwnedTask 契约；具体协作者随后接入。

- [x] 编写失败用例。建立 `test_cancel_keeps_actual_owner`，线程或调用已开始且等待者取消，`assert resource_released is False`，直到实际结果及保存/通知结束才释放。覆盖排队撤回竞争、提交未知、目标终态但调用在途、接手异常；每项 `assert final_consumptions == 1`。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/session/test_supervision.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。只跟踪独立语义责任，把结果消费交给原业务拥有者；接手方沿同一身份、预算及允许步骤完成，不创建脱离会话监督的临时后台任务。
- [x] 再运行上述命令，要求全部 PASS，并核对 取消后结果、通知和实际占用仍闭合。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/session/test_supervision.py -q`，先组合真实事件循环与可控任务，验证唯一接手和通知；P2、O3、F2、R5、L3 实施后分别在消费者集成中核验实际完成。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有 shield、create_task 和取消异常分支是否丢失结果或提前关闭句柄；记录门禁证据，建议以“feat: 实现实际任务监督与取消接手”形成独立提交。

### S6 错误顺序及完整会话收尾

**预计文件：** `apps/camctl/src/camctl/session/service.py`、`apps/camctl/src/camctl/persistence/repositories/session.py`；测试为 `apps/camctl/tests/integration/session/test_session.py`。

**接口与依赖：** 提供异步 `run_session(context: SessionContext, input: ParsedInput | InputDiagnostic | None) -> SessionOutcome`；各业务能力通过注册的流程端口接入。前置交付：S1—S5、B6、Q5、R7/R8、L5/L6；设备链再接 C7。

- [x] 编写失败用例。在 `test_report_failure_keeps_device_work_running` 中报告文件失败、状态库可靠，`assert ordinary_work_continued is True`；读取历史错误则停止普通工作。组合主错误与次要停止/日志错误核对 reason/details；没有新触发的失败报告不无限延长 run，状态库失效日志副本仍可交付。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/session/test_session.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。正常关闭、report_error 关闭及致命收场分别组织；停止新增维护，确认已开始任务结果，保持必要设备和文件责任。普通业务失败不转会话错误，应急调用只由 C7 执行并按实际结果诊断。
- [x] 再运行上述命令，要求全部 PASS，并核对 所有资源实际结束及责任保存后才完成允许的会话退出。
- [x] 审阅实际接口、状态分区及失败路径，检查 每个错误、默认值、跳过及清理分支的责任和实际完成证据；记录门禁证据，建议以“feat: 完成会话错误与有序收尾”形成独立提交。

## 模块完成门禁

两把锁、事务交接、时钟资格和监督器通过真实组合；所有责任类型接入同一退出分类。会话成功、业务失败、报告失败和状态库错误分别表达，等待取消不抹去实际结果。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。

## 实施进度与验证记录

[接管边界修复计划](2026-10-02-camctl-session-handoff-review.md)记录 `submit` 真实入口的事务、锁、ACK、重送、失败与竞争验证，以及报告失败机会结束后的关闭规则。

- [x] `submit` 在输入投影保存后、同一写事务内判断接管；响应只消费可靠完成的结果。
- [x] 关闭仓储在没有新工作的报告失败分区释放接纳，保留未完成责任。
- [x] 工作事实值类型统一拒绝非法数量及非布尔标志。
- [x] S1 的生产查询接入全部持久化责任，区分本地报告完成与等待 ACK。
- [x] S3 的受限入口接入规定的取消、必要收场和一次报告机会。生产装配见路线图"会话工作事实与实际 run 装配"行的完成注记：墙钟检查失败的 run 驱动已受理未排期取消动作全链并处理一次报告责任；取消目标的实际停止链归取消联动轮次。
- [x] S4 的实际 `run` 入口在事务内取得和关闭接纳，并依据最新责任继续执行。
- [x] S6 的完整装配、错误收场与资源生命周期通过真实消费者验证。默认 run 装配生产报告流程与生成子进程监督方：报告责任清空后正常退出，只剩失败报告时按 `report_error` 释放接纳退出，状态库与历史错误按 `state_db_error` 收场，监督方在会话结束后收场。

默认 `run` 已装配会话推进循环、拍摄调度流程与生产报告维护流程：每轮驱动已注册流程，无进展时按待执行截止、进程内唤醒或轮询上限等待，到期拍摄动作经开始事务转入执行并派发，报告流程开始到期的同步动作、冻结报告机会、派发独立子进程生成并发布。运行中的报告动作由报告维度推进：等待本地报告处理的同步不作为普通未完成动作无限延长会话，已发布覆盖但未保存的本地完成继续构成待处理报告变化。S3 的受限入口已接入生产装配：墙钟检查失败时驱动规定取消流程与一次报告机会后按时钟异常退出，不取得接纳资格，不用不可信墙钟推进可信时间下界。
