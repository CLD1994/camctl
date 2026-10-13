# SW-01：受理历史时间状态修复计划

> 供执行 Agent 使用：按本计划逐项执行，使用 `superpowers:executing-plans` 或已经明确指派的子任务组织实施。每项先保留能够证伪契约的失败测试，再修改生产代码，由复核者核验实际结果。复选框只在相应门禁通过后勾选。

**目标：** `submit` 与带输入 `run` 的受理事务保存实际墙钟读数及 `UNCHECKED`；启动检查通过后的可信下界事件使用实际通过读数及 `TRUSTED`。时间状态如实表达各入口的检查阶段，不改变旧历史、受理结果或下界更新资格。

**组织建议：** 在受理仓储共同使用的事件构造边界修正时间状态，沿真实会话入口验证检查前受理与检查后的执行相互独立。保留现有事务组织和公共接口。

**技术基础：** Python 3.11、SQLite、既有整数登记、受理仓储及会话装配；沿用现有依赖。

**设计依据：** [时钟检查与事件](../../architecture/clock-recovery.md)、[历史事务与事件字段](../../camctl/database/history-formats.md#历史事务与事件字段)、[请求受理](../../architecture/plan-acceptance.md)、[会话启动](../../architecture/cli-commands.md#run-的启动顺序)。缺陷状态由[实施路线图](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)维护。

## 当前入口与文件责任

2026-10-13 静态核对：`acceptance/service.py::accept_input` 取得 `occurred_at`，构造 `ProcessInput`；`persistence/repositories/acceptance.py::_envelope` 将各分支的事件统一写为 `clock_status=2`。`session/service.py::_submit_session` 不检查启动墙钟；`_run_session` 先受理，再调用 `check_clock` 与 `enter_execution`。因此不能在受理仓储中假设调用者已经完成检查。

下列路径均相对于 `apps/camctl`。新增测试文件名和内部组织属于建议，实施者调整时须同步命令及消费者，不改变表中的责任。

| 文件 | 实施责任 |
| --- | --- |
| `src/camctl/persistence/repositories/acceptance.py` | 修改 `_envelope(event_id, txn_id, event_type, reason, rows, occurred_at) -> EventEnvelope`；审计全部调用分支。 |
| `src/camctl/persistence/repositories/session.py`、`tests/integration/session/test_clock.py` | `_LowerBoundCommand.plan` 在检查通过后保存的下界事件采用 `TRUSTED`；验证首次建立、推进及无变更分支。 |
| `src/camctl/acceptance/service.py`、`src/camctl/session/service.py` | 核对时间读数、受理顺序与下界更新边界；预期无需改变接口和启动顺序。 |
| `tests/integration/acceptance/test_clock_status.py`（新增） | 用真实受理事务覆盖各类新增历史事件及无新增事件的幂等路径。 |
| `tests/integration/bootstrap/test_acceptance_clock_status.py`（新增） | 用默认装配验证 `submit`、检查通过及失败的带输入 `run`，以及后续运行不改写旧历史。 |
| `tests/integration/session/test_session.py`、`tests/integration/acceptance/test_acceptance.py` | 审计既有预期是否把合法枚举误当成正确语义，保留既有受理、ACK 和接管断言。 |

## 全局约束

- 历史时间状态从 `enum_for("history_events.clock_status")` 的登记读取；登记的 `UNCHECKED=1`、`TRUSTED=2`、`INVALID=3` 是测试的独立预期，不新增另一份生产编号表。
- 受理中保存实际读数，不把未经检查的时间替换为 0、可信下界或后续校时结果。`UNCHECKED` 不使已经提交的业务事实失效。
- 不将墙钟检查提前至受理之前；`submit` 不新增检查，也不依据当前时间判定动作过期。
- 可信下界只由现有启动检查通过后的短事务更新。受理事务不得更新该下界；后续检查不得修订旧事件。
- 保存下界变更时，历史事件使用实际通过检查的读数及 `TRUSTED`；不能因为受理事件使用 `UNCHECKED`，就把后续通过检查的独立事件也记成未检查。
- 不修改公共协议、历史格式或既有数据库记录；新写事件的修复不等于旧库迁移。
- 测试分类、Python 环境及执行隔离遵守[测试运行指南](../../../apps/camctl/tests/AGENTS.md)和[共享测试规则](../../../.agents/rules/testing.md)。所有命令从仓库根执行。

## 状态分类与关键复核点

本表针对受理事务产生的事件。检查后的调度、设备及报告事件仍遵守其所在会话的既有时间状态。

| 入口和结果 | 新历史与时间状态 | 下界及业务结果 |
| --- | --- | --- |
| 首次合法受理，含动作自身参数错误 | 本事务实际产生的事件均为 `UNCHECKED`，保存输入处理时取得的读数。 | 受理结果与原子性保持；下界不变。 |
| 同请求重送复用，未形成新诊断或 ACK 变化 | 不为修复时间状态制造新历史；旧事件原样保留。 | 原计划、动作及历史身份保持；下界不变。 |
| 复用输入同时形成新诊断或有效 ACK 变化 | 新事件为 `UNCHECKED`；原受理事件不重写。 | ACK 按实际覆盖吸收，不能借时钟修复改变资格。 |
| 输入拒绝或诊断登记 | 实际新增诊断事件为 `UNCHECKED`。 | 拒绝和诊断事实保留；下界不变。 |
| ACK 推进并结束适用同步责任 | ACK 与同步结束等本事务事件均为 `UNCHECKED`。 | 水位及同步结果原子保存；时间状态不改变其效力。 |
| ACK 无效 | 按现有规则决定新增诊断；有新事件时为 `UNCHECKED`。 | 不制造确认推进。 |
| ACK 有效但水位不推进，仍满足未完成同步的结束条件 | 同步结束事件为 `UNCHECKED`；不伪造水位变更事件。 | 同步结束与本次受理原子保存，不能把“不推进”误当成“无新事实”。 |
| ACK 有效但不推进，也没有适用同步或其他新事实 | 不创建新事件或空事务。 | 保持原确认状态和下界。 |
| 带输入 `run` 后续检查通过或失败 | 先前受理事件始终为 `UNCHECKED`。 | 检查通过按已有规则更新下界；检查失败保留已受理事实，普通动作不得执行。 |

重点复核：并发 `submit` 不借用另一进程的检查资格；同操作键核实不重写历史；无新增事实不创建事务；已有非空下界不被受理推进；旧事件在后续正常及受限会话中保持原值。以下任务分别覆盖这些条件。

## 任务一：受理分支的失败测试与共同修复

**消费接口：** `accept_input(source, context, key, owned) -> AcceptanceResult` 与仓储 `process_input(command, key, owned) -> DbOutcome[AcceptanceResult]`。

**交付接口：** 保持原签名；所有受理事件共同采用已登记的 `UNCHECKED`。

- [x] 在新增 `integration/acceptance/test_clock_status.py` 中使用该目录真实建库和守卫装配惯例，加入 `test_first_acceptance_events_are_unchecked`、`test_reused_request_preserves_old_clock_status`、`test_rejection_diagnostic_is_unchecked`、`test_ack_and_sync_events_are_unchecked`、`test_equal_watermark_ack_ends_sync_with_unchecked_time`、`test_nonadvancing_ack_without_new_facts_creates_no_history`。分别核对上表的动作参数错误、复用加 ACK、无效 ACK 分支，不以单个合法请求代替整个分类。同步数据依据现有 `integration/reporting/test_ack_sync.py` 的真实同步责任与报告构造，必须真正满足结束条件。
- [x] 直接读取本次事务中的 `history_events`，独立断言 `clock_status == 1` 及 `occurred_at == 注入的实际读数`；另核对事件类型集合、事务边界、受理结果与 `runtime_state.trusted_time_lower_bound`。无新增事件的路径比较前后完整事件行和事务数量。
- [x] 单独运行下方专项命令，保留“预期 1，实际 2”的失败记录；缺少守卫、测试数据无效或 ACK 未真正吸收不算有效失败证据。
- [x] 在 `_envelope` 中从既有登记取得 `UNCHECKED` 并写入事件。审计 `ProcessInputCommand` 中计划、动作、来源、诊断、ACK、同步及聚合状态的全部事件构造，防止其他分支继续固定写可信状态。
- [x] 复跑专项，再分别运行整个 `integration/acceptance` 和单元目录。确认原子提交、同请求复用、独立 ACK 判定与未知提交核实未回归。

## 任务二：默认入口、后续检查与历史保持

**消费接口：** `bootstrap/lifecycle.py` 的默认会话装配及 `session/service.py::run_session(context, source) -> SessionOutcome`。

**交付接口：** 不改变会话接口；提供从真实入口到实际历史行的组合证据。

- [x] 在新增 `integration/bootstrap/test_acceptance_clock_status.py` 中加入 `test_submit_acceptance_never_advances_clock_bound`、`test_run_accepts_before_successful_clock_check`、`test_run_acceptance_survives_invalid_clock`、`test_later_run_does_not_rewrite_acceptance_history`。使用真实数据库、默认受理仓储与可控时钟；设备交互使用受契约约束的替身。
- [x] 对无可信下界和已有下界分别验证 `submit`；后者注入更晚读数，仍断言下界未推进。在已有正常 `run` 接纳期间提交另一请求，也只保存 `UNCHECKED`。
- [x] 对检查通过的 `run`，先保存受理历史快照，再验证下界按实际通过读数建立或推进；对检查失败的 `run`，核对 `clock_invalid`、受理事实保留、零普通设备调用及下界不变。本项验证时间状态与受理顺序，不扩展启动检查机制。
- [x] 核对 `_LowerBoundCommand.plan` 的首次建立及推进事件均使用实际通过读数和 `TRUSTED == 2`。2026-10-13 实施复核发现该共同入口原先固定为 `1`；先在 `integration/session/test_clock.py` 保留有效失败，再从同一整数登记取得 `TRUSTED`。无下界变更时仍不新增事件，默认入口同时断言受理与下界事件采用各自的时间状态。
- [x] 用后续正常和受限 `run` 重读原事务，逐行比较事件内容；同请求及同操作键复用不得产生“补可信”事件。随后按现有历史查询入口恢复原边界，核对受理实体不因时间状态改变而丢失。
- [x] 先运行专项记录失败；若任务一已使全部场景通过，记录组合测试直接通过，不能伪造红阶段。只对真实遗漏修正对应最窄入口，再运行 `integration/bootstrap`、`integration/session`，各目录分开启动。

## 验收命令与收口

以下命令执行本项新增测试文件；完整目录与专项的实际结果见本页末尾验证记录。

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/acceptance/test_clock_status.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_acceptance_clock_status.py -q
```

随后按测试指南分别执行 `unit`、`integration/acceptance`、`integration/session`、`integration/bootstrap`，并单独执行 `integration/reporting/test_ack_sync.py`。要求相关范围全部通过；跳过必须解释其条件，不能跳过本计划的状态分支。

- [x] 复核全部受理事件构造和后续下界消费者；失败、未知、重用及恢复入口均符合上表。
- [x] 记录环境、解释器、命令、日志、退出码、实际通过数量和未核验范围；复核实际差异及历史读取证据，再更新路线图 SW-01 的状态。
- [x] 完成独立可审阅的修复提交；下一项计划为[SW-02 日志失败诊断](2026-10-13-camctl-sw02-log-copy-diagnostics.md)。该顺序用于组织本组工作，不构造 SW-01 与日志功能之间的业务依赖。

2026-10-13：生产修复、专项及相关目录回归已完成，根代理亲自复核代码、契约和实际日志，独立任务审查符合规格并批准质量。实际环境、红绿证据、通过数量、平台跳过和警告由[SW-01 验证记录](../../camctl/verification.md#sw-01-时间状态验证2026-10-13)维护；没有连接真实相机或 ARM 主机。
