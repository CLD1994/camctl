# camctl 日志执行与故障副本模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 实现分级有界日志接纳、多进程轮换和包含触发错误的独立日志副本交付。

**组织建议：** 各进程独立创建 Janus 队列和监听线程，文件锁由 CLH 协调；入口负责原子水位与来源路由，副本凭据随触发记录入队前绑定。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 复用 logging、QueueHandler/QueueListener、janus 和 concurrent-log-handler；内部扩展点集中适配并锁定已验证版本。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [日志执行](../../camctl/logging-runtime.md)、[日志规则](../../architecture/logging.md)、[日志副本](../../architecture/log-delivery.md)、[故障标记](../../architecture/log-failure-marker.md)、[依赖核验](../../camctl/integration-readiness.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

普通接纳不等于写出。已接纳记录按 FIFO 顺序，不因高级别新记录删除或重排。0<L<H<C；低水位下正常，中间区 DEBUG 丢弃/INFO 独立采样，高水位以上 DEBUG/INFO 丢弃，满载自身 ERROR/CRITICAL 异步等待而其他新记录按规则丢弃。必要设备停止、取消保存和清理不以自身重要日志接纳为前提。副本写入触发错误和复制在同一共享锁内完成，交付不依赖状态库，不递归生成副本。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

同步来源自身 ERROR 不从同步入口投递，数据库及其他执行线程通过结果交回主协程记录；第三方同步日志保持不阻塞。通道禁用、队列接纳丢弃和副本失败是三类结果。

| 类型 | 字段或含义 |
| --- | --- |
| `LogSource / AdmissionDecision` | 自身与第三方来源、级别、水位、一次采样结果；接纳、丢弃或自身异步等待。 |
| `LogReceipt / PendingLog` | 本条是否已接纳及取消后的所属责任；原记录只交接一次，不因取消重建副本。 |
| `ChannelState / WriteResult` | 可用、轮换故障但追加可用、必须禁用；实际追加和副本结果分开。 |
| `CopyRequest / CopyReceipt` | 故障身份、触发记录、一次副本位置和写入完成凭据；包含标记取得、锁内复制和独立文件发布结果。 |
| `DropCounters / CloseResult` | 按级别本进程计数及一次退出摘要/真实线程关闭；不写数据库、不跨进程或启动累加。 |

水位策略采用完整分类。

| 队列 q | DEBUG | INFO | WARNING | 自身 ERROR/CRITICAL | 第三方 ERROR/CRITICAL |
| --- | --- | --- | --- | --- | --- |
| q<L | 接纳 | 接纳 | 接纳 | 接纳 | 接纳 |
| L≤q<H | 丢弃 | 一次 u<p 才接纳 | 接纳 | 接纳 | 接纳 |
| H≤q<C | 丢弃 | 丢弃 | 接纳 | 接纳 | 接纳 |
| q=C | 丢弃 | 丢弃 | 丢弃 | 异步等待 | 丢弃 |

先过滤配置级别，再原子检查水位和入队；竞争重新检查不能重新抽样。关闭先停止生产并处理已接纳记录，再在线程最多写一次丢弃摘要，然后关闭监听器、处理器和 Janus 通知任务。通道故障唤醒等待者，不能继续等已禁用消费者。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/logging_runtime/models.py` | 接纳、写入、副本和关闭结果。 |
| `apps/camctl/src/camctl/logging_runtime/admission.py` | 纯水位、采样及计数规则。 |
| `apps/camctl/src/camctl/logging_runtime/janus_adapter.py` | 原子入队和私有扩展点隔离。 |
| `apps/camctl/src/camctl/logging_runtime/service.py` | 异步/同步来源路由及重要日志接手。 |
| `apps/camctl/src/camctl/logging_runtime/clh_adapter.py` | 实际轮换、追加及共享锁内复制。 |
| `apps/camctl/src/camctl/logging_runtime/copies.py` | 独立故障标记及一次副本交付。 |
| `apps/camctl/src/camctl/logging_runtime/lifecycle.py` | 有序关闭及一次摘要。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [装配与共享类型](2026-09-30-camctl-bootstrap.md) | B2/B6 配置与进程内实例，K1/K2 精确类型。 |
| [会话](2026-09-30-camctl-session.md) | S5 接手待投递重要记录，但业务必要收场先独立推进。 |
| [文件](2026-09-30-camctl-host-files.md) | F5 为副本提供 DB 独立发布事实；普通日志写入不使用默认线程池。 |
| [报告](2026-09-30-camctl-reporting.md) | R8 在规定首次报告失败时产生触发记录及 CopyRequest。 |

L1—L4 和 L6 随首阶段日志基础实施；L5 随首条报告失败链完成。副本与业务数据库没有依赖边，真实多进程轮换门禁不能由处理器 mock 代替。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 并发水位判断不能先 qsize 后无锁入队。 | L2，`test_concurrent_admission_is_atomic` |
| 一条 INFO 竞争重查只抽样一次。 | L2，`test_info_samples_once` |
| 重要日志等待不能挡必要停止。 | L3，`test_log_wait_does_not_block_stop` |
| 另一进程轮换后副本仍含触发错误。 | L5，`test_copy_contains_trigger_before_rotation` |
| 重复关闭不重复生成丢弃摘要。 | L6，`test_close_summary_is_once` |

## 实施任务

### L1 纯水位、采样和丢弃计数

**预计文件：** `apps/camctl/src/camctl/logging_runtime/models.py`、`apps/camctl/src/camctl/logging_runtime/admission.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_admission.py`。

**接口与依赖：** 提供 `decide_admission(input: AdmissionInput) -> AdmissionDecision`、`record_drop(counters: DropCounters, decision: AdmissionDecision) -> DropCounters`；input 含级别、来源、q/C/L/H、过滤结果和一次 u。前置交付：B2、K1。

- [x] 编写失败用例。建立 `test_info_sampling_uses_supplied_value`，中间区 u<p/u=p/u>p 及 p=0/1 分区，`assert decision.kind == expected_kind`；L-1/L/H-1/H/C-1/C、所有级别及双方来源各有独立预期。过滤与通道故障不记接纳丢弃，`assert drop_count == expected_once_count`。纯函数消费已有 u，不调用随机源。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_admission.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。用一次独立随机采样事实参与决定，纯规则不访问队列；按级别保存计数，不保存被丢消息内容或逐条产生新日志。
- [x] 再运行上述命令，要求全部 PASS，并核对 完整表和边界均有独立预期，不使用真实随机条数作验收。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有来源及过滤分支是否重复计数或重新抽样；记录门禁证据，建议以“feat: 实现日志水位及采样规则”形成独立提交。

### L2 Janus 原子接纳与监听线程

**预计文件：** `apps/camctl/src/camctl/logging_runtime/janus_adapter.py`、`apps/camctl/src/camctl/logging_runtime/service.py`；测试为 `apps/camctl/tests/integration/logging_runtime/test_queue.py`。

**接口与依赖：** 提供异步 `alog(record: LogRecord, receipt: LogReceipt | None) -> AdmissionResult`、同步 `slog(record: LogRecord) -> AdmissionResult`；两入口共享同一队列和接纳策略。前置交付：L1、B1 锁定依赖；只有内部适配器使用已核验 Janus 扩展点。

- [x] 编写失败用例。建立 `test_concurrent_admission_is_atomic`，真实同步/异步生产者竞争，`assert queued <= capacity` 且跨水位接纳正确；拒绝不增加未完成任务数，已接纳 FIFO 不重排。建立 `test_info_samples_once`，替换随机源并在竞争重查时 `assert random_calls == 1`，非采样区为 0；满载自身错误等待，警告和第三方错误丢弃，消费腾空后继续。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_queue.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。复用 Janus 同步锁与等待/通知，集中封装小范围原子策略；QueueListener 专用线程顺序消费，不另建替代队列或条件变量体系。
- [x] 再运行上述命令，要求全部 PASS，并核对 真实版本的队列计数、join/aclose 与监听生命周期正确。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有入口是否有不受协调 qsize 判断或直接 queue.put 绕过策略；记录门禁证据，建议以“feat: 实现原子日志接纳与线程消费”形成独立提交。

### L3 重要日志取消与通道禁用

**预计文件：** `apps/camctl/src/camctl/logging_runtime/service.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_pending_log.py` 和 `apps/camctl/tests/integration/logging_runtime/test_pending_log.py`。

**接口与依赖：** 提供异步 `deliver_important(pending: PendingLog, owner: ResponsibilityOwner) -> LogReceipt`、`disable_channel(error: ChannelError) -> None`；PendingLog 在创建后只保留一份。前置交付：L2、S5。

- [x] 编写失败用例。建立 `test_log_wait_does_not_block_stop`，队列满且协程取消，`assert necessary_stop_started is True` 而重要记录仍由自身责任等待。记录已接纳不重复入队；文件通道禁用则所有等待者退出，不能把未写记录标写入成功。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_pending_log.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。把必要业务收场与日志接纳等待拆开，在允许最终结束前确认原重要记录交接；禁用通知使用独立语义结果，不以取消等待抹去责任。
- [x] 再运行上述命令，要求全部 PASS，并核对 不会因日志堵塞阻止取消事实、设备停止及适用文件处理。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_pending_log.py -q`，真实满队列、监听错误和会话取消组合，验证消费者关闭后无永远等待的生产者。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有自身 ERROR、finally 和异常处理是否重新生成同一记录；记录门禁证据，建议以“feat: 实现重要日志取消接手”形成独立提交。

### L4 CLH 轮换、追加与故障分类

**预计文件：** `apps/camctl/src/camctl/logging_runtime/clh_adapter.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_handler.py` 和 `apps/camctl/tests/integration/logging_runtime/test_handler.py`。

**接口与依赖：** 提供 `write_record(record: LogRecord, copy: CopyRequest | None) -> WriteResult`；只由日志线程调用，实际阶段由集中 CLH 适配器取得。前置交付：L1/L2；既定 CLH 及传递依赖版本。

- [x] 编写失败用例。在 `test_rotation_failure_does_not_hide_append` 中轮换失败但追加成功，`assert write_result.appended is True`；追加失败禁用，handleError 吞内部错误不能被正常函数返回掩盖。file_count=1 对应 backupCount=0，保留总量包含活动文件；调小保留数按原规则清理。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_handler.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。复用库文件锁和轮换，适配错误阶段及一次 stderr 诊断；日志同步在专用线程，不引入新的通用轮换实现。
- [x] 再运行上述命令，要求全部 PASS，并核对 追加结果、通道状态和副本错误不合并。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_handler.py -q`，两个独立进程真实写入/轮换及不同保留参数，核对记录及保留上限。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有库内部异常扩展点和版本升级兼容性；记录门禁证据，建议以“feat: 适配多进程日志写入与故障”形成独立提交。

### L5 独立故障标记与锁内副本

**预计文件：** `apps/camctl/src/camctl/logging_runtime/copies.py`、`apps/camctl/src/camctl/logging_runtime/clh_adapter.py`；测试为 `apps/camctl/tests/integration/logging_runtime/test_copies.py`。

**接口与依赖：** 提供异步 `copy_failure_log(request: CopyRequest) -> CopyReceipt`；请求在触发记录入队前绑定，日志线程在同一共享锁内追加触发记录并复制。前置交付：L3/L4、F5；不依赖 P 或业务状态库。

- [ ] 编写失败用例。建立 `test_copy_contains_trigger_before_rotation`，触发写入后另一进程竞争轮换，`assert trigger_record in copied_bytes`；状态库不可用仍可发布。标记缺失/有效/无效/读错、并发第一次失败、复制与发布失败分别按专题处理；副本失败不递归复制。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_copies.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按现有独立故障标记契约争取一次副本责任，触发追加与复制锁内协调，释放锁后使用本地交接接口。凭据分别确认原日志写入、复制与发布；失败清理按日志用途，不创建 delivery。
- [ ] 再运行上述命令，要求全部 PASS，并核对 每次规定责任最多一次，副本包含触发记录且不被 DB 故障阻挡。
- [ ] 审阅实际接口、状态分区及失败路径，检查 标记、复制、入队取消及文件发布的全部未知/失败分支；记录门禁证据，建议以“feat: 实现独立故障日志副本”形成独立提交。

## 行为契约与实施边界

普通接纳不等于写出。已接纳记录按 FIFO 顺序，不因高级别新记录删除或重排。0<L<H<C；低水位下正常，中间区 DEBUG 丢弃/INFO 独立采样，高水位以上 DEBUG/INFO 丢弃，满载自身 ERROR/CRITICAL 异步等待而其他新记录按规则丢弃。必要设备停止、取消保存和清理不以自身重要日志接纳为前提。副本写入触发错误和复制在同一共享锁内完成，交付不依赖状态库，不递归生成副本。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

同步来源自身 ERROR 不从同步入口投递，数据库及其他执行线程通过结果交回主协程记录；第三方同步日志保持不阻塞。通道禁用、队列接纳丢弃和副本失败是三类结果。

| 类型 | 字段或含义 |
| --- | --- |
| `LogSource / AdmissionDecision` | 自身与第三方来源、级别、水位、一次采样结果；接纳、丢弃或自身异步等待。 |
| `LogReceipt / PendingLog` | 本条是否已接纳及取消后的所属责任；原记录只交接一次，不因取消重建副本。 |
| `ChannelState / WriteResult` | 可用、轮换故障但追加可用、必须禁用；实际追加和副本结果分开。 |
| `CopyRequest / CopyReceipt` | 故障身份、触发记录、一次副本位置和写入完成凭据；包含标记取得、锁内复制和独立文件发布结果。 |
| `DropCounters / CloseResult` | 按级别本进程计数及一次退出摘要/真实线程关闭；不写数据库、不跨进程或启动累加。 |

水位策略采用完整分类。

| 队列 q | DEBUG | INFO | WARNING | 自身 ERROR/CRITICAL | 第三方 ERROR/CRITICAL |
| --- | --- | --- | --- | --- | --- |
| q<L | 接纳 | 接纳 | 接纳 | 接纳 | 接纳 |
| L≤q<H | 丢弃 | 一次 u<p 才接纳 | 接纳 | 接纳 | 接纳 |
| H≤q<C | 丢弃 | 丢弃 | 接纳 | 接纳 | 接纳 |
| q=C | 丢弃 | 丢弃 | 丢弃 | 异步等待 | 丢弃 |

先过滤配置级别，再原子检查水位和入队；竞争重新检查不能重新抽样。关闭先停止生产并处理已接纳记录，再在线程最多写一次丢弃摘要，然后关闭监听器、处理器和 Janus 通知任务。通道故障唤醒等待者，不能继续等已禁用消费者。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/logging_runtime/models.py` | 接纳、写入、副本和关闭结果。 |
| `apps/camctl/src/camctl/logging_runtime/admission.py` | 纯水位、采样及计数规则。 |
| `apps/camctl/src/camctl/logging_runtime/janus_adapter.py` | 原子入队和私有扩展点隔离。 |
| `apps/camctl/src/camctl/logging_runtime/service.py` | 异步/同步来源路由及重要日志接手。 |
| `apps/camctl/src/camctl/logging_runtime/clh_adapter.py` | 实际轮换、追加及共享锁内复制。 |
| `apps/camctl/src/camctl/logging_runtime/copies.py` | 独立故障标记及一次副本交付。 |
| `apps/camctl/src/camctl/logging_runtime/lifecycle.py` | 有序关闭及一次摘要。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [装配与共享类型](2026-09-30-camctl-bootstrap.md) | B2/B6 配置与进程内实例，K1/K2 精确类型。 |
| [会话](2026-09-30-camctl-session.md) | S5 接手待投递重要记录，但业务必要收场先独立推进。 |
| [文件](2026-09-30-camctl-host-files.md) | F5 为副本提供 DB 独立发布事实；普通日志写入不使用默认线程池。 |
| [报告](2026-09-30-camctl-reporting.md) | R8 在规定首次报告失败时产生触发记录及 CopyRequest。 |

L1—L4 和 L6 随首阶段日志基础实施；L5 随首条报告失败链完成。副本与业务数据库没有依赖边，真实多进程轮换门禁不能由处理器 mock 代替。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 并发水位判断不能先 qsize 后无锁入队。 | L2，`test_concurrent_admission_is_atomic` |
| 一条 INFO 竞争重查只抽样一次。 | L2，`test_info_samples_once` |
| 重要日志等待不能挡必要停止。 | L3，`test_log_wait_does_not_block_stop` |
| 另一进程轮换后副本仍含触发错误。 | L5，`test_copy_contains_trigger_before_rotation` |
| 重复关闭不重复生成丢弃摘要。 | L6，`test_close_summary_is_once` |

## 实施任务

### L1 纯水位、采样和丢弃计数

**预计文件：** `apps/camctl/src/camctl/logging_runtime/models.py`、`apps/camctl/src/camctl/logging_runtime/admission.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_admission.py`。

**接口与依赖：** 提供 `decide_admission(input: AdmissionInput) -> AdmissionDecision`、`record_drop(counters: DropCounters, decision: AdmissionDecision) -> DropCounters`；input 含级别、来源、q/C/L/H、过滤结果和一次 u。前置交付：B2、K1。

- [x] 编写失败用例。建立 `test_info_sampling_uses_supplied_value`，中间区 u<p/u=p/u>p 及 p=0/1 分区，`assert decision.kind == expected_kind`；L-1/L/H-1/H/C-1/C、所有级别及双方来源各有独立预期。过滤与通道故障不记接纳丢弃，`assert drop_count == expected_once_count`。纯函数消费已有 u，不调用随机源。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_admission.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。用一次独立随机采样事实参与决定，纯规则不访问队列；按级别保存计数，不保存被丢消息内容或逐条产生新日志。
- [x] 再运行上述命令，要求全部 PASS，并核对 完整表和边界均有独立预期，不使用真实随机条数作验收。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有来源及过滤分支是否重复计数或重新抽样；记录门禁证据，建议以“feat: 实现日志水位及采样规则”形成独立提交。

### L2 Janus 原子接纳与监听线程

**预计文件：** `apps/camctl/src/camctl/logging_runtime/janus_adapter.py`、`apps/camctl/src/camctl/logging_runtime/service.py`；测试为 `apps/camctl/tests/integration/logging_runtime/test_queue.py`。

**接口与依赖：** 提供异步 `alog(record: LogRecord, receipt: LogReceipt | None) -> AdmissionResult`、同步 `slog(record: LogRecord) -> AdmissionResult`；两入口共享同一队列和接纳策略。前置交付：L1、B1 锁定依赖；只有内部适配器使用已核验 Janus 扩展点。

- [x] 编写失败用例。建立 `test_concurrent_admission_is_atomic`，真实同步/异步生产者竞争，`assert queued <= capacity` 且跨水位接纳正确；拒绝不增加未完成任务数，已接纳 FIFO 不重排。建立 `test_info_samples_once`，替换随机源并在竞争重查时 `assert random_calls == 1`，非采样区为 0；满载自身错误等待，警告和第三方错误丢弃，消费腾空后继续。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_queue.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。复用 Janus 同步锁与等待/通知，集中封装小范围原子策略；QueueListener 专用线程顺序消费，不另建替代队列或条件变量体系。
- [x] 再运行上述命令，要求全部 PASS，并核对 真实版本的队列计数、join/aclose 与监听生命周期正确。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有入口是否有不受协调 qsize 判断或直接 queue.put 绕过策略；记录门禁证据，建议以“feat: 实现原子日志接纳与线程消费”形成独立提交。

### L3 重要日志取消与通道禁用

**预计文件：** `apps/camctl/src/camctl/logging_runtime/service.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_pending_log.py` 和 `apps/camctl/tests/integration/logging_runtime/test_pending_log.py`。

**接口与依赖：** 提供异步 `deliver_important(pending: PendingLog, owner: ResponsibilityOwner) -> LogReceipt`、`disable_channel(error: ChannelError) -> None`；PendingLog 在创建后只保留一份。前置交付：L2、S5。

- [x] 编写失败用例。建立 `test_log_wait_does_not_block_stop`，队列满且协程取消，`assert necessary_stop_started is True` 而重要记录仍由自身责任等待。记录已接纳不重复入队；文件通道禁用则所有等待者退出，不能把未写记录标写入成功。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_pending_log.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。把必要业务收场与日志接纳等待拆开，在允许最终结束前确认原重要记录交接；禁用通知使用独立语义结果，不以取消等待抹去责任。
- [x] 再运行上述命令，要求全部 PASS，并核对 不会因日志堵塞阻止取消事实、设备停止及适用文件处理。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_pending_log.py -q`，真实满队列、监听错误和会话取消组合，验证消费者关闭后无永远等待的生产者。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有自身 ERROR、finally 和异常处理是否重新生成同一记录；记录门禁证据，建议以“feat: 实现重要日志取消接手”形成独立提交。

### L4 CLH 轮换、追加与故障分类

**预计文件：** `apps/camctl/src/camctl/logging_runtime/clh_adapter.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_handler.py` 和 `apps/camctl/tests/integration/logging_runtime/test_handler.py`。

**接口与依赖：** 提供 `write_record(record: LogRecord, copy: CopyRequest | None) -> WriteResult`；只由日志线程调用，实际阶段由集中 CLH 适配器取得。前置交付：L1/L2；既定 CLH 及传递依赖版本。

- [x] 编写失败用例。在 `test_rotation_failure_does_not_hide_append` 中轮换失败但追加成功，`assert write_result.appended is True`；追加失败禁用，handleError 吞内部错误不能被正常函数返回掩盖。file_count=1 对应 backupCount=0，保留总量包含活动文件；调小保留数按原规则清理。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_handler.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。复用库文件锁和轮换，适配错误阶段及一次 stderr 诊断；日志同步在专用线程，不引入新的通用轮换实现。
- [x] 再运行上述命令，要求全部 PASS，并核对 追加结果、通道状态和副本错误不合并。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_handler.py -q`，两个独立进程真实写入/轮换及不同保留参数，核对记录及保留上限。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有库内部异常扩展点和版本升级兼容性；记录门禁证据，建议以“feat: 适配多进程日志写入与故障”形成独立提交。

### L5 独立故障标记与锁内副本

**预计文件：** `apps/camctl/src/camctl/logging_runtime/copies.py`、`apps/camctl/src/camctl/logging_runtime/clh_adapter.py`；测试为 `apps/camctl/tests/integration/logging_runtime/test_copies.py`。

**接口与依赖：** 提供异步 `copy_failure_log(request: CopyRequest) -> CopyReceipt`；请求在触发记录入队前绑定，日志线程在同一共享锁内追加触发记录并复制。前置交付：L3/L4、F5；不依赖 P 或业务状态库。

- [ ] 编写失败用例。建立 `test_copy_contains_trigger_before_rotation`，触发写入后另一进程竞争轮换，`assert trigger_record in copied_bytes`；状态库不可用仍可发布。标记缺失/有效/无效/读错、并发第一次失败、复制与发布失败分别按专题处理；副本失败不递归复制。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_copies.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按现有独立故障标记契约争取一次副本责任，触发追加与复制锁内协调，释放锁后使用本地交接接口。凭据分别确认原日志写入、复制与发布；失败清理按日志用途，不创建 delivery。
- [ ] 再运行上述命令，要求全部 PASS，并核对 每次规定责任最多一次，副本包含触发记录且不被 DB 故障阻挡。
- [ ] 审阅实际接口、状态分区及失败路径，检查 标记、复制、入队取消及文件发布的全部未知/失败分支；记录门禁证据，建议以“feat: 实现独立故障日志副本”形成独立提交。

### L6 有序关闭和一次丢弃摘要

**预计文件：** `apps/camctl/src/camctl/logging_runtime/lifecycle.py`；测试为 `apps/camctl/tests/unit/logging_runtime/test_lifecycle.py` 和 `apps/camctl/tests/integration/logging_runtime/test_lifecycle.py`。

**接口与依赖：** 提供异步 `close_logging(runtime: LogRuntime) -> CloseResult`；LogRuntime 含生产状态、未完成记录、通道、计数及实际线程句柄。前置交付：L1—L4、S5。

- [ ] 编写失败用例。建立 `test_close_summary_is_once`，重复关闭 `assert summary_attempts == 1`，全部零或级别过滤/通道禁用为 0。摘要由日志线程直接写，`assert summary_queue_puts == 0`；队列满时停止标记不能丢弃已接纳记录或直接抛 Full。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/logging_runtime/test_lifecycle.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。先停止新生产并处理原重要等待/已接纳记录，再按最终计数最多尝试一条 WARNING，停止监听、关闭处理器及 Janus；线程等待异步组织，不阻塞事件循环。
- [ ] 再运行上述命令，要求全部 PASS，并核对 正常错误收场也能有序关闭，突然终止不补造摘要。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/logging_runtime/test_lifecycle.py -q`，真实生产者、监听线程、满队列、通道禁用及多个进程分别汇总，核对任务/线程实际关闭。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有退出路径、重复关闭和摘要失败是否递归写日志；记录门禁证据，建议以“feat: 完成日志组件有序关闭”形成独立提交。

## 模块完成门禁

真实 Janus、CLH、同步/异步生产者和独立进程组合通过；重要日志与必要业务收场保持独立，副本包含触发记录且状态库不可用仍可交付。关闭实际完成而非只取消队列等待。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
