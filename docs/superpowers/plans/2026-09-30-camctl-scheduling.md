# camctl 调度与设备协调模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 按时间、可靠责任、业务顺序和设备兼容性发现并授予工作，使唤醒竞争及重启不改变执行资格。

**组织建议：** 资格和排序由纯函数计算，候选按范围从仓储加载；首次授予与意图共同提交，实际派发前再次检查资格。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 asyncio、单调钟与可信墙钟端口、分批 SQLite 查询和受约束的动作处理器目录。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [调度](../../architecture/scheduling-execution.md)、[启动与占用](../../camctl/database/waiting.md)、[实现通知](../../camctl/implementation.md#调度通知与待处理工作的交接)、[一致性验收](../../camctl/database/consistency-verification.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

拍摄派发必须满足 scheduled_at≤trusted_wall_now≤window_end；意图提交不等于派发。窗口内派发而窗口后确认可以继续原任务，未知效果不得重发。准备、重试等待、活动占用、启动保留和实际调用分别建模。唤醒必须统一重新判断资格，缓存不是授予依据。提交、发现、检查与进入等待的交接不能漏通知，重启从持久化责任重新发现。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

调度只消费公共管理、资源及任务资格，专属拍摄定义由相应处理器解释。设备活动结束、占用释放和动作终态是不同事实。

| 类型 | 字段或含义 |
| --- | --- |
| `ScheduleFacts / ScheduleDecision` | 可信时间、动作管理、窗口观察、取消、预算、准备、原调用及占用；决定等待、准备、授予候选、继续原责任或结束。 |
| `CandidateRequest / CandidatePage` | 设备或工作类别、有限范围、稳定排序游标和上限；数据库之外缓存只保留有界候选。 |
| `WakeToken / WorkNotifier` | 单调递增的检查版本及等待接口；同进程提交与外部发现转换为同一版本通知。 |
| `DispatchGrant` | 原动作/流程/活动/尝试身份、采用依据及可靠提交凭据；派发前仍需核对当前时间和取消。 |
| `ActionHandler / ExecutorSlot` | 按类型登记的处理端口及一个实际可推进责任的位置；不为全部未来或阻塞动作预建执行协程。 |

先处理事实错误、终态与生效取消，再分区决定普通启动。

| 原启动事实与时间 | 决定 |
| --- | --- |
| 未到 scheduled_at | 仅按准备提前量安排准备或等待。 |
| 窗口内，准备、预算及资源均满足 | 可以在事务内竞争授予；提交后派发前再次核对。 |
| 窗口已过，无尝试或可靠无效果且无其他核实责任 | 过期，不新增启动。 |
| 原调用仍在原期限内，或效果未知需有限核实 | 继续原责任，不因窗口结束新建尝试。 |
| 已可靠确认原启动成功 | 继续采集流程，不再竞争启动。 |
| 有限核实结束仍未知 | 失败并保留未知活动及必要独立收场。 |

资源授予再组合设备占用、输出范围、原调用、拷贝机会和驱动兼容性，全部限制通过才放行；机会释放只触发重新判断，不自动授予下一候选。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/scheduling/rules.py` | 公共资格及窗口分类。 |
| `apps/camctl/src/camctl/scheduling/order.py` | 候选排序与同时间取回优先。 |
| `apps/camctl/src/camctl/scheduling/discovery.py` | 有界工作与终态后责任发现。 |
| `apps/camctl/src/camctl/scheduling/notifications.py` | 版本通知与检查等待交接。 |
| `apps/camctl/src/camctl/scheduling/resources.py` | 占用、启动保留及读取机会。 |
| `apps/camctl/src/camctl/scheduling/service.py` | 按需创建及推进动作流程。 |
| `apps/camctl/src/camctl/persistence/repositories/scheduling.py` | 候选查询及完整授予事务。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享与持久化](2026-09-30-camctl-persistence.md) | K1—K3、P3/P5；Q4 使用完整事务。 |
| [会话](2026-09-30-camctl-session.md) | S1/S5 管理模式及实际责任。 |
| [操作](2026-09-30-camctl-operations.md) | O1/O2 的尝试身份及可靠意图。 |
| [采集与产物](2026-09-30-camctl-capture.md) | C1/C2/X3 的处理器与资格端口；先稳定接口再实现具体流程。 |

Q1—Q3 可用受约束替身先实施。Q4 与 O2、C2 联合建立首个启动原子操作；Q5 接入首个录像处理器。Q6 每引入新的资源及终态后责任就补组合，最终涵盖全部入口。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 意图提交后窗口耗尽不得派发或退次数。 | Q4，`test_grant_does_not_skip_dispatch_recheck` |
| 检查完毕但等待前到达通知仍会唤醒。 | Q3，`test_notify_between_check_and_wait` |
| 执行机会不按协程唤醒先后决定。 | Q1，`test_order_is_independent_of_wakeup` |
| 终态动作下仍发现未结束尝试。 | Q2，`test_terminal_action_does_not_hide_call` |
| 准备不得为每个未来动作创建执行协程。 | Q5，`test_blocked_actions_have_no_executor` |

## 实施任务

### Q1 时间资格与统一排序

**预计文件：** `apps/camctl/src/camctl/scheduling/rules.py`、`apps/camctl/src/camctl/scheduling/order.py`；测试为 `apps/camctl/tests/unit/scheduling/test_rules.py`。

**接口与依赖：** 提供 `decide_schedule(facts: ScheduleFacts) -> ScheduleDecision`、`order_candidates(candidates: Sequence[Candidate]) -> tuple[Candidate, ...]`；Candidate 含稳定 ID、计划时间、类型及归属。前置交付：K1—K3、S1 的执行模式。

- [ ] 编写失败用例。建立 `test_order_is_independent_of_wakeup`，打乱输入与通知顺序，`assert ordered_ids == expected_by_contract`；时间覆盖到点前、两端包含、超窗和 0 宽窗口。已有成功、原调用在途、原意图未知、核实耗尽各给独立预期，不能统一判过期。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/scheduling/test_rules.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。精确比较可信墙钟与启动窗口，等待使用单调钟；按业务规定的时间、类型及稳定身份排序，不在规则中查询外部资源。
- [ ] 再运行上述命令，要求全部 PASS，并核对 调度状态分区及候选排序与原有决策表对应。
- [ ] 审阅实际接口、状态分区及失败路径，检查 普通拍摄、恢复、准备及来源资格是否使用不同的时间规则；记录门禁证据，建议以“feat: 实现调度资格与候选顺序”形成独立提交。

### Q2 有界发现及缓存扩展

**预计文件：** `apps/camctl/src/camctl/scheduling/discovery.py`、`apps/camctl/src/camctl/persistence/repositories/scheduling.py`；测试为 `apps/camctl/tests/integration/scheduling/test_discovery.py`。

**接口与依赖：** 提供异步 `discover_work(request: CandidateRequest) -> CandidatePage`；候选结果包含原责任，分页部分遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)。候选读取与执行资格复核分别处理，Page 不授予执行机会。前置交付：P5、Q1。

- [ ] 编写失败用例。建立 `test_terminal_action_does_not_hide_call`，动作终态而尝试 RUNNING，`assert attempt_id in discovered_responsibilities`；后续页有更早合格候选、缓存淘汰、仅有未来动作、新 submit 未改变缓存父对象时都能发现。无关已完成历史增加不导致全库常驻加载。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/scheduling/test_discovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按未完成管理字段及独立责任索引分页；只保存本批和必要近期缓存，候选不足时继续持久化扩展。
- [ ] 再运行上述命令，要求全部 PASS，并核对 有限查询不漏终态后责任或缓存外工作。
- [ ] 审阅实际接口、状态分区及失败路径，检查 发现入口是否只查 actions 非终态或最新计划序列；记录门禁证据，建议以“feat: 实现有界调度工作发现”形成独立提交。

### Q3 提交与进入等待的通知交接

**预计文件：** `apps/camctl/src/camctl/scheduling/notifications.py`；测试为 `apps/camctl/tests/unit/scheduling/test_notifications.py` 和 `apps/camctl/tests/integration/scheduling/test_notifications.py`。

**接口与依赖：** 提供 `WorkNotifier.mark_changed(reason: WakeReason) -> WakeToken`、`snapshot() -> WakeToken`、异步 `wait_changed(observed: WakeToken, deadline: MonotonicDeadline | None) -> WakeToken`。前置交付：K1；WakeReason 在本模块集中定义，原因不替代持久化发现。

- [x] 编写失败用例。建立 `test_notify_between_check_and_wait`，控制通知在读版本、查工作、进入等待三个位置到达，`assert recheck_count >= 1`；取消原等待者但已提交由 S5 接手仍 mark_changed。重复通知合并但不丢最后变化，超时只触发重新判断。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/scheduling/test_notifications.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。在共享同步边界维护版本与等待登记，旧版本不可直接睡眠；外部 submit 由可靠持久化发现入口形成通知，不要求新增跨进程通知协议。
- [x] 再运行上述命令，要求全部 PASS，并核对 无检查后睡死竞争，通知只是安排再次查询。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/scheduling/test_notifications.py -q`，真实写线程与调度协程控制提交后通知、外部 submit 和退出竞争。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部提交成功、读取机会释放、活动结束及来源结束通知入口；记录门禁证据，建议以“feat: 实现可靠调度通知交接”形成独立提交。

### Q4 原子授予、启动保留与派发再检查

**预计文件：** `apps/camctl/src/camctl/scheduling/resources.py`、`apps/camctl/src/camctl/persistence/repositories/scheduling.py`；测试为 `apps/camctl/tests/integration/scheduling/test_resources.py`。

**接口与依赖：** 提供异步 `grant_start(command: StartCandidate, key: OperationKey) -> DbOutcome[DispatchGrant]` 与纯 `validate_dispatch(grant: DispatchGrant, current: DispatchFacts) -> DispatchDecision`；首次 grant 含原活动、流程、意图及次数。前置交付：Q1/Q2、O2、P3、C1 的固定执行定义；不等待 C2 的录像流程。

- [ ] 编写失败用例。建立 `test_grant_does_not_skip_dispatch_recheck`，可靠提交后时间超窗或取消生效，`assert driver_calls == 0` 且次数不退还。首次机会记录缺活动、意图或参数任一项整组拒绝；同设备两个候选竞争只能一个获准，同时间资源排序始终一致。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/scheduling/test_resources.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。完整写事务中再核对排序、准备、占用、保留、窗口及预算；可靠授予后检查派发，未发且已阻止迟到执行时由 O1/O2 保存 dispatch_prevented。
- [ ] 再运行上述命令，要求全部 PASS，并核对 W 系列首次授予及保存窗口观察对应事务全部成立。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有启动入口是否绕过 grant 或把等待保留当作实际活动；记录门禁证据，建议以“feat: 实现原子启动授予与再检查”形成独立提交。

### Q5 处理器接入与执行协程创建

**预计文件：** `apps/camctl/src/camctl/scheduling/service.py`；测试为 `apps/camctl/tests/unit/scheduling/test_executors.py` 和 `apps/camctl/tests/integration/scheduling/test_executors.py`。

**接口与依赖：** 提供 `register_handler(action_type: ActionType, handler: ActionHandler) -> None`、异步 `drive_ready(context: SchedulerContext) -> DriveResult`；目录由 B6 装配。前置交付：Q1—Q3、S5 及 ActionHandler 端口；Q4/C1 随设备处理器接入。

- [ ] 编写失败用例。建立 `test_blocked_actions_have_no_executor`，大量未来、来源未完及占用阻塞动作，`assert running_executor_ids == ready_responsibility_ids`；准备按需创建自己的责任，不创建整个动作等待协程。错误类型不默认路由录像。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/scheduling/test_executors.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。只为已具备推进步骤的动作或独立责任创建协程，结束后释放；相机专属定义交给处理器，资源限制由统一协调端口核验。
- [ ] 再运行上述命令，要求全部 PASS，并核对 无单个总控制器承载所有业务分支，模块不直接创建驱动或连接。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/scheduling/test_executors.py -q`，先组合真实调度、仓储和 report_status 处理器；录像/取回实施后再验证不同设备工作推进，阶段 3—6 收齐证据。
- [ ] 审阅实际接口、状态分区及失败路径，检查 处理器目录、未来动作及恢复责任是否重复创建执行者；记录门禁证据，建议以“feat: 接入按需动作执行器”形成独立提交。

### Q6 释放与重启的全入口验证

**预计文件：** `apps/camctl/src/camctl/scheduling/resources.py`、`apps/camctl/src/camctl/scheduling/discovery.py`；测试为 `apps/camctl/tests/integration/scheduling/test_recovery.py`。

**接口与依赖：** 使用 `reevaluate_resource(target: ResourceIdentity) -> None` 通知端口；释放决定由所属业务仓储完整提交。前置交付：C3/C7、X3/X5/X8、O5、N3。

- [ ] 编写失败用例。在 `test_release_never_erases_new_owner` 中迟到观察属于旧活动，`assert new_activity_occupancy_is_held`；组合正常停止、取消、无效果、可靠未派发、恢复、残留收场和应急补记全部入口。ENDED+HELD、UNKNOWN+适用完成依据、实际调用未完分别按 O 系列验收处理。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/scheduling/test_recovery.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。把每个释放入口接入相同占用规则，提交后统一重新判断；原观察必须核对身份，活动结束不单独证明输出范围已解除限制。
- [ ] 再运行上述命令，要求全部 PASS，并核对 W-01—W-20 与 O-01—O-06 有逐项组合用例，重启不丢候选或责任。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部释放、通知、恢复和迟到结果路径的同类风险；记录门禁证据，建议以“test: 验证资源释放与重启调度”形成独立提交。

## 模块完成门禁

Q1—Q5 首条录像和取回链运行；Q6 完成全部释放入口、通知竞争及恢复责任。所有派发均经过可靠意图和最终资格检查，缓存与协程先后不能改变业务顺序。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
