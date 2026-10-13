# 报告工作进程收场期间的状态库错误闭合计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务逐步执行。先证实失败测试，再修改生产代码；pytest 由主 Agent 前台顺序执行。

**目标：** 未决报告任务停止或超时后，主进程仍接收同任务、同数据库实例的完整状态库错误；迟到成功不恢复发布资格，收场始终确认进程与通信线程实际停止。

**组织建议：** `WorkerSupervisor` 记录当前未决任务及停止状态，生成和显式停止共享一次实际收场。通信线程在关闭端点前交出已经送达的完整帧，监督者逐条校验身份并保留有效状态库错误。已确认成功后的进程回收不重新分类该成功。

**技术基础：** Python 3.11、既有 `multiprocessing.Pipe`、`WorkerCommunicator`、`ProcessHandle`、类型化结果与 `asyncio`；不增加依赖。

**设计依据：** [结果与退出判定](../../camctl/report-runtime.md#生成结果与进程退出的判定)、[停止与结果先后](../../camctl/report-runtime.md#报告任务停止与结果先后关系)、[超时收场](../../camctl/report-runtime.md#分阶段时限与超时收场)、[通信所有权](../../camctl/report-runtime.md#通信端点与任务所有权)、[会话错误](../../architecture/session-errors.md)。

## 范围与当前入口

生产入口为 `reporting/supervisor.py::WorkerSupervisor.generate`、`stop`、`_retire` 与 `reporting/communication.py::WorkerCommunicator.close`、`_run`。`generate` 的发送失败、剩余期限耗尽、`wait_for` 超时、`ChannelClosed`、协议错误及显式 `stop` 都进入收场。当前 `_retire` 清空监督引用并请求退出、等待、升级信号、关闭通信，却不再读取结果，尚未证明已送达状态库错误不会丢失。`generate` 当前只比较 `job_id`；结果的 `instance_id` 也必须匹配。

`unit/reporting/test_worker.py` 只验证错误分类、父进程守护及使用内存锁替身的工作锁契约。`integration/reporting/test_supervisor_transport.py` 使用真实 Pipe、通信线程及替身进程验证监督者的就绪、启动失败、普通成功、即时状态库失败、退出顺序及超时收场。纯监督矩阵使用受真实接口约束的内存替身，位于单元层；真实管道、线程及子进程组合均位于 `integration/reporting`。

此次只修改 supervisor、communication 及相关测试。主 Agent 负责 `bootstrap/lifecycle.py` 两种 supervisor 的停止结果消费；其他 Agent 负责报告业务消费者。`reporting` 不导入 `session`，不得改 `bootstrap/flows.py`。

## 输入与状态矩阵

有效结果须完整通过消息解码，且 `job_id`、`instance_id` 同时匹配当前未决任务。字节不完整或身份错误不能证明当前库失效。

| 已确定状态 | 当前收到的消息或事实 | 结果与处理 |
| --- | --- | --- |
| 有效未决任务，尚未停止或超时 | 匹配身份的 SUCCESS | 保存 SUCCESS；按原健康规则复用，或实际收场后保留该成功。 |
| 有效未决任务，尚未停止或超时 | 匹配身份的 STATE 失败 | 返回 STATE_FAILURE 并保存原分类失败消息，实际结束进程和线程。 |
| 有效未决任务，尚未停止或超时 | 匹配身份的普通报告失败 | 返回 REPORT_FAILURE，保留责任并实际收场。 |
| 已停止或超时的未决任务 | 匹配身份的迟到 STATE 失败 | 保留停止／超时主事实诊断，返回 STATE_FAILURE；显式 stop 通过 WorkerShutdown.state_failure 交给调用方。 |
| 已停止或超时的未决任务 | 匹配身份的迟到 SUCCESS、普通报告失败或就绪 | 不恢复发布资格或重开任务；保留必要诊断并继续收场。 |
| 未决任务，无论是否停止或超时 | 结果的任务或数据库实例身份不匹配 | 记录协议错误，不升级为当前任务的状态库错误，不发布该成功；继续确认实际退出。 |
| 已经校验并确认 SUCCESS | 失效计时、显式停止、子进程随后退出或旧结果 | 保持该成功；不把它重新解释为未决任务，不由收场期旧消息撤回。 |
| 没有未决任务的启动／空闲进程 | 结果消息 | 不能关联为业务状态库错误，按阶段协议及停止规则处理。 |
| 未决任务，通道结束或消息不完整 | 尚无有效结果 | 保留通道／协议诊断与报告责任，实际退出前不清理或复用临时文件。 |

`ChannelClosed` 是通信终端：先前完整消息必须先于它送达，终端之后不伪造新帧。测试须分别控制管道到达、线程解码、事件循环接收及进程退出，不能用违反 FIFO（先进先出）及终端契约的替身制造通过。

## 接口与所有权

- `WorkerShutdown` 保留 `exitcode`、`forced`，增加可选 `state_failure: ResultFailureMessage | None = None`。只有当前未决或已停止／超时任务的 matching STATE 可以填该字段。
- `WorkerGeneration` 保持既有接口；收场取得 matching STATE 时返回既有 `GenerationOutcomeKind.STATE_FAILURE` 及该失败，`detail` 保留导致停止的原期限／发送／通信／协议原因。
- `generate` 与显式 `stop` 并发时只有一个结果接收者和一次进程收场。先标为停止中，再撤销当前等待的普通发布资格；等待协程取消不作为线程、进程或文件停止证据。
- 收场的身份和诊断记录不得在读取排队结果之前清空。逐条读取到已知通信终端，不组装无界结果列表，也不在超时后重新派发任务。
- 停止流程确认进程实际退出后才关闭通信所有者，线程在自己的端点关闭前处理已可读取的完整帧，再发布终端并实际退出；不能跨线程强制关闭端点来模拟可靠收场。
- 发出 `kill` 后尚未确认实际退出时，保持管理责任及不可复用状态，不能关闭端点并报告已经完成收场。该分区沿第一版实际停止契约处理，不增加持续系统失响应的自适应机制。
- 生命周期消费者完整停止两个 supervisor 并关闭日志后，将有效 `state_failure` 转成会话 `state_db_error`；原本已经是状态库错误时保留原诊断及次要错误顺序。该消费由主 Agent 实施和验收。

## 实施任务

### WL1 失败测试与判定矩阵

**文件：** `unit/reporting/test_worker.py`、`unit/reporting/test_worker_late_state.py`、`integration/reporting/test_supervisor_transport.py`、`integration/reporting/test_worker.py` 及 `integration/reporting/test_communication_shutdown.py`；必要时更新既有错误身份测试的有效预期。

- [x] 单元文件保留 `TestErrorClassification`、`TestParentGuard`、`TestWorkLock` 及内存锁替身；真实管道与通信线程文件包含 `TestSupervisorStartup`、`TestSupervisorGeneration` 及所需辅助代码。5 项局部契约和 9 项传输组合的测试内容与断言保持完整，不重复登记用例，也不让单元测试导入集成测试。
- [x] 纯单元替身使用 `create_autospec(..., spec_set=True)` 约束 WorkerCommunicator，进程替身遵守 ProcessHandle。通过受控时钟、队列与同步事件让 STATE 在超时决定或 Shutdown 后到达；分别断言发送失败、剩余期限耗尽、异步等待超时、协议错误及显式 stop 的结果。已交付于 `unit/reporting/test_worker_late_state.py`（`_Clock`／`_Channel` 替身与超时、协议错误、显式 stop 分区）。
- [x] 对同任务同实例的迟到 SUCCESS／REPORT 和两个错误身份维度分别断言不返回成功、不误判状态库失效；有效及时 SUCCESS 后的 stop 保持既有成功。`test_timeout_never_accepts_ordinary_late_result`、`test_wrong_result_identity_is_protocol_failure`、`test_late_state_from_wrong_identity_is_not_database_failure`、`test_stop_after_confirmed_success_does_not_reclassify_old_message`。
- [x] 用真实 Pipe 与通信线程验证 `close` 请求前已送达但尚未解码的完整 STATE 帧仍排在终端前，发送失败同时另一方向已有完整结果也保持该顺序；部分帧不能变成有效 STATE。已交付于 `integration/reporting/test_communication_shutdown.py` 两项。
- [x] 主 Agent 单独运行新文件并提供红证据。失败须来自缺失 STATE、错误身份未拒绝或完整帧被丢，替身接口错误和测试准备错误不算红。专项红绿日志未随检查点单独保留（交付经由 2026-10-09 检查点 `48144df`／`86ba422`）；2026-10-11 复跑两文件 33 项全部通过作为当前行为证据。

### WL2 监督收场与通信帧保留

**文件：** `supervisor.py`、`communication.py`。

- [x] 在原监督对象内明确当前未决任务、停止状态与共享收场所有权；所有 generate 失败入口经过同一收场判定，正确匹配任务与数据库实例。`reporting/supervisor.py` 的 `_retirement_task`／`_retire_current` 统一收场，`integration/reporting/test_supervisor_transport.py::TestSupervisorGeneration` 覆盖任务与实例匹配。
- [x] 通信线程实际关闭前保留已可接收的完整消息；监督者在实际退出和通信线程收场之后消费剩余有效结果，匹配 STATE 填 WorkerShutdown.state_failure。显式 stop 与 generate 同时发生时不竞争读取或重复回收。`supervisor.py` 的 `state_failure` 装配与 `test_retirement_keeps_matching_state_failure`、`test_exit_checks_delivered_result`、`test_exit_without_result_is_channel_lost` 覆盖。
- [x] 生成成功后解除未决身份，保留既有成功；停止／超时之后的普通成功无 success 载荷，也不产生发布资格。错误身份按协议失败，不能静默继续等待正确结果掩盖该错误。`test_matching_success_keeps_worker_reusable`、`test_old_task_result_rejects_current_generation`、`test_stop_after_confirmed_success_does_not_reclassify_old_message`。
- [x] 保持 join／terminate／kill 的顺序和实际退出判断；最终仍存活的进程继续由监督对象持有，不能让下一生成复用其文件。将实际无法收场的具体失败交给已有诊断范围。`test_kill_without_observed_exit_keeps_ownership`、`test_total_deadline_times_out_and_escalates`、`test_startup_timeout_retires_process`。
- [x] 主 Agent 复跑新增单元与组件集成，要求完整通过，再审计全部 _retire 入口和两种生命周期消费者。2026-10-11 全量单元 4726 passed、1 skipped；`integration/reporting` 全目录 373 passed。`_retire` 入口收场与 `bootstrap/lifecycle.py:717` 关机消费、`bootstrap/flows.py:807` 生成失败消费两种生命周期消费者均经真实组合测试覆盖。

### WL3 独立组合验证与证据

- [x] 单独执行 `unit/reporting/test_worker_late_state.py` 的失败与修复验证，再执行整个 `unit` 目录；真实管道、线程与进程验证单独执行 `integration/reporting`，其中包含 `test_supervisor_transport.py`。2026-10-11：专项 33 passed；全量 unit 4726 passed、1 skipped、2 条既有 asyncio 警告；`integration/reporting` 373 passed。
- [x] 在正常及受限会话的报告监督组合中断言迟到 STATE 进入 `state_db_error`，普通设备不继续依赖已失效前提，状态库不可用的日志副本仍独立处理。该消费者组合由主 Agent 登记。`bootstrap/flows.py:807` 将 STATE_FAILURE 转为 `StateDbFailure`；`test_capture_flow_failures.py::test_capture_state_failure_stops_later_device_flow` 核普通设备停止；`session/test_local_work_settlement.py` 的 `test_local_state_failure_preserves_exit_error_priority` 与 `test_clock_transition_stops_restricted_work_after_local_state_failure` 覆盖正常及受限组合；日志副本经 `bootstrap/test_logging_composition.py` 与 `recovery_logging.py` 独立处理。
- [x] 实际进程／线程仍活动时不能清理或复用临时文件；任务成功、停止、超时、错误身份、STATE／REPORT 分类及两种先后顺序均有独立预期。存活的未观测退出进程由监督对象继续持有（`test_kill_without_observed_exit_keeps_ownership`），staging 按 report/job 独立命名（`_temporary_staging_path`）；分类与先后顺序由迟到状态矩阵与 `test_exit_checks_delivered_result` 的两种顺序覆盖。
- [x] 记录 Python 版本、环境、命令及结果；`git diff --check` 与文档链接／锚点检查只验证文档，不代替软件门禁。2026-10-11 记录见[当前验收记录](#当前验收记录2026-10-11)。

## 验收命令与当前状态

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/reporting -q
```

2026-10-09：局部契约与真实传输组合分别位于单元和集成目录；失败用例、生产修复和消费者组合按复选框及实际验证记录跟踪。硬件、真实设备与物理断电不在本任务范围。

## 当前验收记录（2026-10-11）

Linux 开发容器、Python 3.11.16，SQLite 3.53.1。前台独占、按目录分开执行：

- `unit/reporting/test_worker_late_state.py` 加 `integration/reporting/test_supervisor_transport.py`：33 passed。
- `integration/reporting` 全目录：373 passed。
- 全量 `unit`：4726 passed、1 skipped、2 warnings（既有同步测试的 asyncio 标记）。

本计划的迟到状态矩阵、监督收场、帧保留、生命周期消费者组合及临时文件所有权均已由上述文件与 `bootstrap` 消费者组合覆盖；专项红绿过程证据随 2026-10-09 检查点提交（`48144df`、`86ba422`）交付，未单独保留日志。
