# 第一版软件验收映射

[数据库一致性与历史恢复验收](database/consistency-verification.md)定义的全部条目在此逐条映射到实际生产入口、测试与验证证据。本页是[跨组件集成计划 I6](../superpowers/plans/2026-09-30-camctl-integration.md#i6-全量契约映射软件验收与部署交接)的实施档案；映射的结构与引用由 `tests/integration/test_camctl_acceptance_map.py` 检查。一行映射不等于行为通过：行为证据以所列用例的实际通过记录为准，尚未核验的前提逐条列出并归属到模块任务或路线图行，不折叠为空白。

## 条目格式与结论分类

每条包含七项：原文、结论、归属（所属模块任务）、生产入口、测试、证据、未核验前提。结论分三类：

- **已覆盖**：条目全部要求有可证伪用例且已有通过记录。
- **部分覆盖**：主体行为已验证；中断注入矩阵、排序竞争组合、跨组件或规模测量等子场景未核验，未核验前提列出归属。
- **开放**：关键分支尚无生产实现或覆盖用例；未核验前提列出所属模块任务。

路径约定：生产入口与测试以仓库根相对路径书写；测试引用允许 `*` 通配，检查器按 glob 展开并要求至少命中一个真实文件。验证证据统一指向各模块计划的分段验证记录与本文[执行记录](#执行记录)，不复制完整计数。

## 一、等待与来源结束（验收 01–10、W-01–W-06、W-13–W-20）

来源解析与选择的生产入口为 `apps/camctl/src/camctl/outputs/sources.py` 与 `apps/camctl/src/camctl/persistence/repositories/outputs.py`（来源解析、选择固定与等待命令）；启动保留与首次启动为 `apps/camctl/src/camctl/persistence/repositories/scheduling.py` 与 `apps/camctl/src/camctl/scheduling/`。

#### 验收 01
- 原文：[验收 01](database/consistency-verification.md#等待与来源结束)（来源成功、失败、过期和取消覆盖有产物与无产物）。
- 结论：已覆盖
- 归属：X2/X3 来源解析与选择（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/sources.py
- 测试：apps/camctl/tests/integration/outputs/test_sources.py
- 证据：outputs 计划分段验证记录（六形态解析、失败终态与空集合固定用例）。
- 未核验前提：无

#### 验收 02
- 原文：[验收 02](database/consistency-verification.md#等待与来源结束)（同一来源被多个取回与范围清理引用，分批通知全部相关未完成责任）。
- 结论：已覆盖
- 归属：X2 来源等待（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/sources.py
- 测试：apps/camctl/tests/integration/outputs/test_sources.py
- 证据：outputs 计划分段验证记录（多引用来源的通知与逐来源推进）。
- 未核验前提：无

#### 验收 03
- 原文：[验收 03](database/consistency-verification.md#等待与来源结束)（交错提交来源结束、依赖建立和动作首次执行）。
- 结论：已覆盖
- 归属：X2/X3 来源与选择（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_selection_derived_sequence.py
- 证据：outputs 计划分段验证记录（来源结束与选择事件序列群）。
- 未核验前提：无

#### 验收 04
- 原文：[验收 04](database/consistency-verification.md#等待与来源结束)（取消先提交、来源结束先提交、选择先提交及依赖方已有终态）。
- 结论：已覆盖
- 归属：X3 选择与 N1—N6 取消联动（outputs、cancellation 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/sources.py
- 测试：apps/camctl/tests/integration/outputs/test_selection_reuse_boundaries.py
- 证据：outputs 计划分段验证记录与 cancellation 计划验证记录。
- 未核验前提：无

#### 验收 05
- 原文：[验收 05](database/consistency-verification.md#等待与来源结束)（共同提交并覆盖提交前中断、明确回滚、提交未知及提交后通知前退出）。
- 结论：部分覆盖
- 归属：X2/X3（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_selection_reuse.py
- 证据：原键重送核实与恢复等价已覆盖（outputs 计划分段验证记录）；中断逐点注入矩阵未建立。
- 未核验前提：来源固定与选择事务各边界（提交前、回滚、提交未知、提交后通知前）的注入矩阵，归路线图阶段 3 中断行。

#### 验收 06
- 原文：[验收 06](database/consistency-verification.md#等待与来源结束)（完整边界查询与固定 H 报告一致）。
- 结论：已覆盖
- 归属：H 系列历史查询与 I5 报告链（history、integration 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_queries.py
- 证据：history 计划分段验证记录与 I5 报告链固定字节对照（集成计划 I4 验证记录）。
- 未核验前提：无

#### 验收 07
- 原文：[验收 07](database/consistency-verification.md#等待与来源结束)（回放、快照正向与投影逆向恢复一致）。
- 结论：已覆盖
- 归属：H1—H3 历史恢复（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_replay_continuity.py
- 证据：history 计划分段验证记录（三路径等价群）。
- 未核验前提：无

#### 验收 08
- 原文：[验收 08](database/consistency-verification.md#等待与来源结束)（依赖方自身记录未变时不增加历史计数）。
- 结论：已覆盖
- 归属：H2 历史归属与计数（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_changes.py
- 证据：history 计划分段验证记录（计数与关联群）。
- 未核验前提：无

#### 验收 09
- 原文：[验收 09](database/consistency-verification.md#等待与来源结束)（同一动作两项工作保存不同重试配置，彼此不覆盖）。
- 结论：已覆盖
- 归属：尝试重试间隔的时间强制（capture、outputs 计划）。
- 生产入口：apps/camctl/src/camctl/operations/attempts.py
- 测试：apps/camctl/tests/integration/capture/test_retry_intervals.py
- 证据：capture 计划分段验证记录（重试间隔群）。
- 未核验前提：无

#### 验收 10
- 原文：[验收 10](database/consistency-verification.md#等待与来源结束)（依赖的类型、范围、成员唯一性及归属覆盖合法与非法情况）。
- 结论：已覆盖
- 归属：X 系列守卫与成员（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/work_files.py
- 测试：apps/camctl/tests/integration/outputs/test_member_guard.py
- 证据：outputs 计划分段验证记录（成员守卫群）。
- 未核验前提：无

#### 验收 W-01
- 原文：[验收 W-01](database/consistency-verification.md#等待与来源结束)（来源处理分类表及两种来源初始化时点）。
- 结论：已覆盖
- 归属：X2 来源处理分类（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/sources.py
- 测试：apps/camctl/tests/integration/outputs/test_sources.py
- 证据：outputs 计划分段验证记录（六来源形态、PENDING 继续与 FIXED 不重选）。
- 未核验前提：无

#### 验收 W-02
- 原文：[验收 W-02](database/consistency-verification.md#等待与来源结束)（唯一来源选择记录；回滚、提交未知、重送及恢复）。
- 结论：已覆盖
- 归属：X3 选择重用（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_selection_reuse.py
- 证据：outputs 计划分段验证记录（原键恢复群）。
- 未核验前提：无

#### 验收 W-03
- 原文：[验收 W-03](database/consistency-verification.md#等待与来源结束)（重试转换表与两份交付各自计时）。
- 结论：已覆盖
- 归属：X 系列交付等待（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/handoff.py
- 测试：apps/camctl/tests/integration/outputs/test_delivery.py
- 证据：outputs 计划分段验证记录（交付等待与重试群）。
- 未核验前提：无

#### 验收 W-04
- 原文：[验收 W-04](database/consistency-verification.md#等待与来源结束)（启动保留阻挡与持有者变化历史）。
- 结论：已覆盖
- 归属：Q1/Q5 调度资源与保留（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_resources.py
- 证据：scheduling 计划分段验证记录（保留与释放群）。
- 未核验前提：无

#### 验收 W-05
- 原文：[验收 W-05](database/consistency-verification.md#等待与来源结束)（交错提交候选取消、占用释放、保留结束及下一候选授予）。
- 结论：部分覆盖
- 归属：Q4/Q5 授予顺序（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_resources.py、apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：`test_resources.py::test_grant_does_not_skip_dispatch_recheck`、`test_two_candidates_same_device_only_one_granted` 与 `test_earlier_candidate_wins_consistently` 覆盖候选复查、同设备互斥和排序；`test_recovery.py::test_not_dispatched_activity_releases`、`test_ended_activity_releases`、`test_rejected_without_effect_releases`、`test_late_observation_keeps_new_owner_occupancy` 覆盖可靠释放及迟到旧观察不覆盖新归属。路线图阶段 3 也记录同会话释放后下一动作推进。上述分区尚未把候选取消、活动释放、保留结束、下一候选授予及提前／重复／迟到通知组合为同一竞争序列。
- 未核验前提：W-05 所列候选取消、释放、保留结束与下一候选授予之间的组合竞争及通知交接仍待联合注入；路线图阶段 3 调度竞争项跟踪该启动责任范围。

#### 验收 W-06
- 原文：[验收 W-06](database/consistency-verification.md#等待与来源结束)（归属变化中断与三路径恢复一致）。
- 结论：部分覆盖
- 归属：Q4 授予与恢复（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_grant_reuse.py、apps/camctl/tests/integration/scheduling/test_recovery.py、apps/camctl/tests/integration/capture/test_recording_start_save_boundary.py
- 证据：`scheduling/test_grant_reuse.py::test_grant_write_failure_rolls_back_all_joint_facts` 注入 attempt、run、activity 和 link 写失败并核对整体回滚；`test_grant_unknown_commit_is_resolved_by_original_key` 覆盖提交前／后结果未知后的原键只读核实。`scheduling/test_recovery.py::test_late_observation_keeps_new_owner_occupancy` 验证迟到观察不抹去新归属。录像启动结果保存边界另由 `capture/test_recording_start_save_boundary.py::test_start_grant_state_failure_stops_before_device_call` 与 `test_actual_start_result_is_verified_and_resaved_with_same_key_without_resend` 覆盖授予未可靠保存时不派发、实际结果同键复验后保存。
- 未核验前提：不同阶段的故障用例尚未合成为同一历史边界：授予后通知丢失、下一候选获得机会并重启后，仍需在初始回放、快照正向恢复和当前投影逆向恢复中对照原保留、活动、收场与下一候选。对应路线图阶段 3 的跨阶段接缝。

#### 验收 W-13
- 原文：[验收 W-13](database/consistency-verification.md#等待与来源结束)（首次启动共同提交分类表）。
- 结论：已覆盖
- 归属：Q4 首次授予（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_grant_reuse.py
- 证据：scheduling 计划分段验证记录（首次授予分类与重用群）。
- 未核验前提：无

#### 验收 W-14
- 原文：[验收 W-14](database/consistency-verification.md#等待与来源结束)（首次及后续启动的回滚、提交未知与派发前后中断）。
- 结论：部分覆盖
- 归属：Q4 授予恢复（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_grant_reuse.py、apps/camctl/tests/integration/capture/test_recording_start_save_boundary.py
- 证据：`scheduling/test_grant_reuse.py::test_grant_write_failure_rolls_back_all_joint_facts` 覆盖共同授予事实的写入回滚，`test_grant_unknown_commit_is_resolved_by_original_key` 覆盖提交未知后的原键核实；`capture/test_recording_start_save_boundary.py::test_start_grant_state_failure_stops_before_device_call` 验证授予状态未可靠保存时不调用设备，`test_actual_start_result_is_verified_and_resaved_with_same_key_without_resend` 覆盖实际启动结果经原键核实并保存且不重发。路线图阶段 3 另记录调用结果保存及窗口结束的回滚、提交前／后与恢复用例。
- 未核验前提：尚缺同一历史边界把授予后通知丢失、下一候选推进、启动尝试／活动收场及三路径恢复串联核对；这不是缺少所有阶段的中断注入，余项归路线图阶段 3 的跨阶段接缝。

#### 验收 W-15
- 原文：[验收 W-15](database/consistency-verification.md#等待与来源结束)（录像启动状态表全部合法与非法组合）。
- 结论：已覆盖
- 归属：C 录像启动（capture 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/capture/test_recording_start.py
- 证据：capture 计划分段验证记录（启动状态表群）。
- 未核验前提：无

#### 验收 W-16
- 原文：[验收 W-16](database/consistency-verification.md#等待与来源结束)（启动持有者推导与至多一个持有者）。
- 结论：部分覆盖
- 归属：Q5 资源判定（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_resources.py、apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：`test_resources.py::test_grant_does_not_skip_dispatch_recheck`、`test_two_candidates_same_device_only_one_granted`、`test_earlier_candidate_wins_consistently` 覆盖派发前复查、同设备互斥和排序；`test_recovery.py::test_not_dispatched_activity_releases`、`test_ended_activity_releases`、`test_rejected_without_effect_releases` 与 `test_late_observation_keeps_new_owner_occupancy` 覆盖可靠释放和迟到旧观察不覆盖新归属。仓储层查询计划已由持久化 P6 收口核对，调度侧未完成工作查询计划未单独核对。
- 未核验前提：调度查询的实际索引与查询计划核对，归路线图阶段 7 规模验证行。

#### 验收 W-17
- 原文：[验收 W-17](database/consistency-verification.md#等待与来源结束)（启动流程已结束但尝试仍 RUNNING 的合法组合）。
- 结论：已覆盖
- 归属：Q4 授予与迟到结果（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_grant_reuse.py
- 证据：scheduling 计划分段验证记录（迟到结果不覆盖终态群）。
- 未核验前提：无

#### 验收 W-18
- 原文：[验收 W-18](database/consistency-verification.md#等待与来源结束)（窗口结束与在途启动的全部组合）。
- 结论：部分覆盖
- 归属：Q 窗口观察与过期（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_window_expiration.py、apps/camctl/tests/integration/capture/test_recording_start.py、apps/camctl/tests/integration/capture/test_recording_start_runtime.py、apps/camctl/tests/integration/capture/test_stop_confirmed_with_error.py、apps/camctl/tests/integration/capture/test_recording_start_query_cancel.py、apps/camctl/tests/integration/capture/test_recording_queries.py
- 证据：窗口观察、窗口外未派发过期和窗口守卫已覆盖；`capture/test_recording_start.py::test_late_confirmation_after_window_is_accepted` 验证录像在窗口内派发、窗口后确认仍沿原责任继续，`scheduling/test_window_expiration.py::test_granted_intent_is_not_expired_by_window` 验证已授予意图不因窗口结束过期。未知启动先核实、不重发及核实耗尽时保留未知占用由 `capture/test_recording_start_runtime.py::test_unknown_start_uses_start_confirmation_before_any_resend`、`test_start_confirmation_exhaustion_keeps_unknown_occupancy_and_original_start`、`test_original_start_stays_in_flight_without_second_dispatch_or_early_terminal` 覆盖；窗口后不得派发且保留已用次数由 `test_window_end_after_intent_prevents_dispatch_and_keeps_consumed_attempt` 覆盖。窗口观察、可靠未派发、无效果等待、在途和迟到结果均有相应分区用例，见路线图阶段 3 的 2026-10-11 核对记录。
- 未核验前提：单张拍摄、录像和延时摄影三种动作的启动、停止及结果核实，尚未与窗口边界、取消／有限核实及各结果分支组合到同一个历史边界，并共同完成三路径恢复对照；剩余项是统一交叉矩阵，不是上述分项行为缺少验证。

#### 验收 W-19
- 原文：[验收 W-19](database/consistency-verification.md#等待与来源结束)（意图提交后派发前跨过窗口）。
- 结论：部分覆盖
- 归属：Q 窗口与派发（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/capture/test_recording_start_runtime.py、apps/camctl/tests/integration/operations/test_attempts.py、apps/camctl/tests/integration/operations/test_recovery.py、apps/camctl/tests/integration/capture/test_recording_start_save_boundary.py、apps/camctl/tests/integration/capture/test_call_result_save.py、apps/camctl/tests/integration/scheduling/test_window_expiration.py
- 证据：`capture/test_recording_start_runtime.py::test_window_end_after_intent_prevents_dispatch_and_keeps_consumed_attempt` 覆盖意图已保存、窗口结束后不派发且不退还次数；同文件的未知启动先核实、不重发、耗尽后保留未知占用及原启动仍在途用例覆盖原责任保持。`operations/test_attempts.py::test_first_intent_creates_run_and_attempt_together` 与 `operations/test_recovery.py::test_intent_only_attempt_recovers_without_redispatch` 覆盖意图共同提交和只有意图时不重派发；`capture/test_recording_start_save_boundary.py`、`capture/test_call_result_save.py` 覆盖结果读取／投影／提交边界，`scheduling/test_window_expiration.py::test_unstarted_expiration_recovers_atomic_transaction` 覆盖 rollback、commit 前／后及三路径恢复。
- 未核验前提：各中断点和结果分支已有分项证据；尚未将窗口外的意图、派发、响应与结果提交前后切点，以及可靠未派发、有／无效果和仍未知，组合成同一历史边界的完整对照与三路径恢复。

#### 验收 W-20
- 原文：[验收 W-20](database/consistency-verification.md#等待与来源结束)（调用截止与启动窗口分别触发；迟到成功不改写终态）。
- 结论：部分覆盖
- 归属：Q 窗口与 O 尝试（scheduling、operations 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_window_expiration.py、apps/camctl/tests/integration/capture/test_stop_confirmed_with_error.py、apps/camctl/tests/integration/capture/test_recording_start_query_cancel.py、apps/camctl/tests/integration/capture/test_recording_queries.py、apps/camctl/tests/integration/capture/test_recording_start_save_boundary.py、apps/camctl/tests/integration/capture/test_call_result_save.py
- 证据：调用窗口后确认、已授予意图在窗口后保留、未知启动核实及结果保存边界见 W-18／W-19 用例。迟到停止调用在取消后仍等待实际结果并按真实停止事实收场由 `capture/test_stop_confirmed_with_error.py::test_cancel_during_in_flight_stop_waits_for_actual_result` 覆盖；取消后的迟到确认保留实际观察且不改写原动作与尝试终态由 `capture/test_recording_start_query_cancel.py::test_canceled_confirmation_keeps_actual_observation_and_original_terminals` 覆盖；原停止／活动查询结束后迟到核实由 `capture/test_recording_queries.py::test_orphan_stop_confirmation_settled_after_stop_succeeds`、`test_orphan_activity_observation_settled_after_cancel` 覆盖。`capture/test_call_result_save.py` 覆盖调用结果读取、投影及提交阶段的故障注入。
- 未核验前提：现有分项证据尚未在同一历史 H 上交叉覆盖原调用截止与启动窗口、核实期间取消、迟到成功／错误及终态，并对初始回放、快照正向和当前投影逆向作整链对照；还须验证回放不读取当前时钟或调用设备。

## 二、相机读取机会与调度交接（W-07–W-12）

读取机会与槽位的生产入口为 `apps/camctl/src/camctl/outputs/slots.py` 与 `apps/camctl/src/camctl/persistence/repositories/outputs.py`；读取会话结算为 `apps/camctl/src/camctl/devices/` 读取会话端口。

#### 验收 W-07
- 原文：[验收 W-07](database/consistency-verification.md#相机读取机会与调度交接)（同相机多个未完成拷贝共用唯一机会）。
- 结论：已覆盖
- 归属：X 槽位与机会（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/slots.py
- 测试：apps/camctl/tests/integration/outputs/test_read_slot.py
- 证据：outputs 计划分段验证记录（机会授予与释放群）。
- 未核验前提：无

#### 验收 W-08
- 原文：[验收 W-08](database/consistency-verification.md#相机读取机会与调度交接)（读取机会分类表与交错保持既定顺序）。
- 结论：已覆盖
- 归属：X 槽位（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/slots.py
- 测试：apps/camctl/tests/integration/outputs/test_read_rejections.py
- 证据：outputs 计划分段验证记录（机会拒绝分类与迟到通知群）。
- 未核验前提：无

#### 验收 W-09
- 原文：[验收 W-09](database/consistency-verification.md#相机读取机会与调度交接)（触发路径表逐项覆盖）。
- 结论：已覆盖
- 归属：X 读取调度（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/dispatch.py
- 测试：apps/camctl/tests/integration/outputs/test_read_schedule.py
- 证据：outputs 计划分段验证记录（触发路径群）。
- 未核验前提：无

#### 验收 W-10
- 原文：[验收 W-10](database/consistency-verification.md#相机读取机会与调度交接)（原协程取消时数据库操作的四种结果）。
- 结论：已覆盖
- 归属：D 读取会话结算（devices 计划）与 X 读取恢复（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/devices/read_session.py
- 测试：apps/camctl/tests/integration/devices/test_read_session_settlement.py
- 证据：devices、outputs 计划分段验证记录（结算与恢复群）。
- 未核验前提：无

#### 验收 W-11
- 原文：[验收 W-11](database/consistency-verification.md#相机读取机会与调度交接)（启动扫描和按设备候选查询分批进行）。
- 结论：已覆盖
- 归属：默认读取发现（SW-06）与 Q3 工作发现（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/read_discovery.py、apps/camctl/src/camctl/outputs/obtain_flow.py、apps/camctl/src/camctl/bootstrap/flows.py
- 测试：apps/camctl/tests/integration/outputs/test_read_discovery.py、apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py、apps/camctl/tests/integration/bootstrap/test_read_default_consumers.py、apps/camctl/tests/integration/session/test_session.py、apps/camctl/tests/integration/session/test_submit_boundary.py、apps/camctl/tests/integration/session/test_work_facts.py
- 证据：默认 `build_runtime`／`execute_command` 在 B=1、2 下推进多个取回和独立副本，实际返回量不超过 B，下一页查询前上一页候选的弱引用全部释放。真实 SQL 验证登记较晚但业务更早的候选、普通与内部读取的共同顺序及原持有者优先；详情淘汰并重开数据库后，原持有者、READ 身份和已用次数保持。默认首次读取期间受理更早的新计划，后续轮次完成其全部副本；取消后实际读取仍独立收场。大量终态动作不扩大未完成候选详情，实际结果的正常、残留及受限消费者保持原保存责任。30秒轮询条件下，流程内下一页通知及时继续；这些默认用例均在全部持久化责任完成后正常退出，关闭接纳仍在原短写事务内重查工作。完整目录、查询计划及容量证据见[SW-06 验证记录](verification.md#sw-06-默认读取分批发现验证2026-10-15)。
- 未核验前提：无

#### 验收 W-12
- 原文：[验收 W-12](database/consistency-verification.md#相机读取机会与调度交接)（授予及释放的中断与三路径恢复一致）。
- 结论：部分覆盖
- 归属：X 槽位与 H 恢复（outputs、history 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/slots.py
- 测试：apps/camctl/tests/integration/outputs/test_read_recovery.py
- 证据：原键恢复与恢复等价已覆盖（outputs 计划分段验证记录）。
- 未核验前提：授予、释放各边界中断注入，归路线图阶段 3 中断行。

## 三、取回、读取保护与清理（验收 11–28、37–51、69–72）

取回建档与资格为 `apps/camctl/src/camctl/outputs/qualification.py`；拷贝推进为 `apps/camctl/src/camctl/outputs/copy.py`；清理编排为 `apps/camctl/src/camctl/outputs/cleanup_flow.py` 与 `apps/camctl/src/camctl/outputs/work_files.py`；设备工作候选为 `apps/camctl/src/camctl/outputs/dispatch.py`。

#### 验收 11
- 原文：[验收 11](database/consistency-verification.md#取回读取保护与清理)（唯一读取流程、错误归属与恢复）。
- 结论：已覆盖
- 归属：X 取回拷贝链（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/copy.py
- 测试：apps/camctl/tests/integration/outputs/test_copy_resume.py
- 证据：outputs 计划分段验证记录（建档、续传、重拷与恢复群）。
- 未核验前提：无

#### 验收 12
- 原文：[验收 12](database/consistency-verification.md#取回读取保护与清理)（共同建档与无资格拒绝）。
- 结论：已覆盖
- 归属：X4/X5 资格与建档（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/qualification.py
- 测试：apps/camctl/tests/integration/outputs/test_qualification.py
- 证据：outputs 计划分段验证记录（资格矩阵与整体提交群）。
- 未核验前提：无

#### 验收 13
- 原文：[验收 13](database/consistency-verification.md#取回读取保护与清理)（取回与清理候选唤醒及选择保存顺序）。
- 结论：部分覆盖
- 归属：X 排序与清理协调（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/dispatch.py
- 测试：apps/camctl/tests/integration/outputs/test_qualification.py、apps/camctl/tests/integration/outputs/test_outputs_contract.py、apps/camctl/tests/integration/outputs/test_read_slot.py
- 证据：`test_qualification.py::test_earlier_obtain_participates_before_its_selection_is_saved` 与 `test_earlier_cleanup_participates_before_restriction_is_saved` 覆盖选择／限制尚未保存时更早候选参与资格；`test_different_products_prepare_independently_after_their_due_time` 覆盖不同产物分别等待自己的计划时间，不是同产物的竞争排序测试。`test_existing_read_protection_preserves_original_copy` 验证两份取回独立建档且两条源依赖保持，`test_delete_in_progress_rejects_with_dedicated_code` 覆盖删除进行中拒绝。`test_outputs_contract.py` 覆盖两份副本均准备完成后清理源产物、继续发布及保留历史；`test_read_slot.py` 验证同时间读取候选按动作身份排序。它们证明这些分项，不覆盖本条全部交错顺序。
- 未核验前提：尚须组合验证取消与建档分别先提交、建档后新增删除限制，以及同产物两份取回中一份完成解除依赖、另一份仍排队时继续受保护；不能以两份副本均准备完成后的清理代替非最后一条依赖解除的边界。归路线图阶段 6 的排序与清理协调验收。

#### 验收 14
- 原文：[验收 14](database/consistency-verification.md#取回读取保护与清理)（读取各期间失败或取消与依赖解除边界）。
- 结论：已覆盖
- 归属：X 读取保护（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/copy.py
- 测试：apps/camctl/tests/integration/outputs/test_read_recovery.py
- 证据：outputs 计划分段验证记录（依赖与保护群）。
- 未核验前提：无

#### 验收 15
- 原文：[验收 15](database/consistency-verification.md#取回读取保护与清理)（建档及准备完成事务各边界中断）。
- 结论：部分覆盖
- 归属：X 拷贝恢复（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/copy.py
- 测试：apps/camctl/tests/integration/outputs/test_copy_creation_slot.py、apps/camctl/tests/integration/outputs/test_copy_complete.py、apps/camctl/tests/integration/outputs/test_read_recovery.py
- 证据：`test_copy_creation_slot.py` 覆盖建档时相机槽位资格与拒绝；`test_copy_complete.py::test_prepared_commit_releases_source` 注入准备事务提交结果未知，断言源依赖仍保留且副本未标为已准备，再次进入后以原键幂等完成准备并解除依赖。续传和重拷恢复由 `test_read_recovery.py` 与 outputs 计划分段记录覆盖。
- 未核验前提：共同建档事务的完整中断矩阵仍需覆盖回滚、提交未知与通知丢失；准备完成已有未知结果后的重放证据，但真实 SQL 提交前／后、明确回滚与通知丢失的完整故障组合尚未在同一历史边界统一核对。归路线图阶段 6 事务接缝。

#### 验收 16
- 原文：[验收 16](database/consistency-verification.md#取回读取保护与清理)（三路径恢复取得相同资格和依赖事实）。
- 结论：已覆盖
- 归属：H 历史恢复（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_replay_continuity.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 17
- 原文：[验收 17](database/consistency-verification.md#取回读取保护与清理)（清理限制与 PENDING_DELETE 共同保存）。
- 结论：已覆盖
- 归属：X8 清理固定（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_guards.py
- 证据：outputs 计划分段验证记录（限制建立群）。
- 未核验前提：无

#### 验收 18
- 原文：[验收 18](database/consistency-verification.md#取回读取保护与清理)（多源依赖解除按受影响产物触发检查）。
- 结论：已覆盖
- 归属：X8/X9 清理协调（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录（依赖解除群）。
- 未核验前提：无

#### 验收 19
- 原文：[验收 19](database/consistency-verification.md#取回读取保护与清理)（清理取消、源依赖解除及处理归属取得的提交顺序）。
- 结论：已覆盖
- 归属：X9 清理取消（outputs 计划）与 N 取消联动（cancellation 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs、cancellation 计划分段验证记录。
- 未核验前提：无

#### 验收 20
- 原文：[验收 20](database/consistency-verification.md#取回读取保护与清理)（源依赖解除事务各边界中断）。
- 结论：部分覆盖
- 归属：X9 清理恢复（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_recovery.py、apps/camctl/tests/integration/outputs/test_copy_complete.py
- 证据：`test_cleanup_recovery.py` 覆盖通知丢失后的启动恢复接续；`test_copy_complete.py::test_prepared_commit_releases_source` 将准备保存返回 `UNKNOWN`，断言源依赖仍为 1、尚未进入准备状态，再次进入后完成准备并解除源依赖，覆盖未知结果后幂等重入。
- 未核验前提：现有用例覆盖恢复通知和准备提交未知，但未组成源依赖解除真实 SQL 提交前／后、明确回滚及提交后通知丢失的同一历史边界三路径核对；归路线图阶段 6 事务接缝。

#### 验收 21
- 原文：[验收 21](database/consistency-verification.md#取回读取保护与清理)（多清理请求排队与跨重启预算）。
- 结论：已覆盖
- 归属：X9 清理（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录（排队与预算群）。
- 未核验前提：无

#### 验收 22
- 原文：[验收 22](database/consistency-verification.md#取回读取保护与清理)（当前清理项取消、超时与收场依据）。
- 结论：已覆盖
- 归属：X9 清理收场（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录（收场群）。
- 未核验前提：无

#### 验收 23
- 原文：[验收 23](database/consistency-verification.md#取回读取保护与清理)（接手决策表与 absence_confirmed/already_cleaned）。
- 结论：已覆盖
- 归属：X9 清理接手（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录（接手决策表群）。
- 未核验前提：无

#### 验收 24
- 原文：[验收 24](database/consistency-verification.md#取回读取保护与清理)（C1 失败或取消后 C2 按自己的授权继续）。
- 结论：已覆盖
- 归属：X9 清理（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 25
- 原文：[验收 25](database/consistency-verification.md#取回读取保护与清理)（同一产物最多一个 DELETING 项与归属保持）。
- 结论：已覆盖
- 归属：X9 清理恢复（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_recovery.py
- 证据：outputs 计划分段验证记录（恢复与接手群）。
- 未核验前提：无

#### 验收 26
- 原文：[验收 26](database/consistency-verification.md#取回读取保护与清理)（删除和查询流程仅通过 cleanup_item_id 关联）。
- 结论：已覆盖
- 归属：X8 守卫（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_guards.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 27
- 原文：[验收 27](database/consistency-verification.md#取回读取保护与清理)（首次进入 DELETING 与首次尝试整体提交）。
- 结论：已覆盖
- 归属：X8/X9（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_guards.py
- 证据：outputs 计划分段验证记录（整体提交群）。
- 未核验前提：无

#### 验收 28
- 原文：[验收 28](database/consistency-verification.md#取回读取保护与清理)（查询后删除、删除未知后查询沿原流程累计）。
- 结论：已覆盖
- 归属：X9 清理（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录（重试间隔与预算群）。
- 未核验前提：无

#### 验收 37
- 原文：[验收 37](database/consistency-verification.md#取回读取保护与清理)（多待处理清理项的计划时间排序）。
- 结论：已覆盖
- 归属：X 设备工作决策（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/dispatch.py
- 测试：apps/camctl/tests/integration/outputs/test_device_work.py
- 证据：outputs 计划分段验证记录（候选排序群）。
- 未核验前提：无

#### 验收 38
- 原文：[验收 38](database/consistency-verification.md#取回读取保护与清理)（候选分类表）。
- 结论：已覆盖
- 归属：X 设备工作决策（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/dispatch.py
- 测试：apps/camctl/tests/integration/outputs/test_device_work.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 39
- 原文：[验收 39](database/consistency-verification.md#取回读取保护与清理)（接手中断与重启恢复保持原处理者）。
- 结论：部分覆盖
- 归属：X9 清理恢复（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_recovery.py
- 证据：跨重启沿用原处理归属与预算已覆盖（outputs 计划分段验证记录）。
- 未核验前提：前一项终态提交前后、下一项建档前后及两事务之间的逐点中断，归路线图阶段 6 中断行。

#### 验收 40
- 原文：[验收 40](database/consistency-verification.md#取回读取保护与清理)（产物清理汇总表六种状态及优先级）。
- 结论：已覆盖
- 归属：X 汇总投影（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_errors.py
- 证据：outputs 计划分段验证记录（汇总投影群）。
- 未核验前提：无

#### 验收 41
- 原文：[验收 41](database/consistency-verification.md#取回读取保护与清理)（清理状态变化与产物报告字段共同提交）。
- 结论：已覆盖
- 归属：X 汇总与报告目录（outputs、reporting 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_product_event_sequence.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 42
- 原文：[验收 42](database/consistency-verification.md#取回读取保护与清理)（以最终结果事件顺序选择产物清理错误）。
- 结论：已覆盖
- 归属：X 汇总（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_errors.py
- 证据：outputs 计划分段验证记录（错误选择群）。
- 未核验前提：无

#### 验收 43
- 原文：[验收 43](database/consistency-verification.md#取回读取保护与清理)（候选最终结果与产物错误共同提交）。
- 结论：已覆盖
- 归属：X 汇总（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_errors.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 44
- 原文：[验收 44](database/consistency-verification.md#取回读取保护与清理)（取消已生效后的五种事实分支）。
- 结论：已覆盖
- 归属：X9 取消收场（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录（取消未知分支群）。
- 未核验前提：无

#### 验收 45
- 原文：[验收 45](database/consistency-verification.md#取回读取保护与清理)（取消项终态、错误、限制与汇总同事务保存）。
- 结论：已覆盖
- 归属：X9 取消收场（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划取消收场群验证记录。
- 未核验前提：无

#### 验收 46
- 原文：[验收 46](database/consistency-verification.md#取回读取保护与清理)（incomplete 进入 pending 时清空 cleanup_error_json）。
- 结论：已覆盖
- 归属：X 汇总（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_errors.py
- 证据：outputs 计划分段验证记录（错误清空群）。
- 未核验前提：无

#### 验收 47
- 原文：[验收 47](database/consistency-verification.md#取回读取保护与清理)（取消后可靠确认删除失败且文件仍在）。
- 结论：已覆盖
- 归属：X9 取消收场（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录；真实文件删除失败注入另见 capture 计划 test_discard 的 POSIX 用例。
- 未核验前提：无

#### 验收 48
- 原文：[验收 48](database/consistency-verification.md#取回读取保护与清理)（final_event_id 的必填与不可改写规则）。
- 结论：已覆盖
- 归属：X 最终引用（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_output_references.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 49
- 原文：[验收 49](database/consistency-verification.md#取回读取保护与清理)（最终结果、final_event_id、历史正文和汇总共同提交）。
- 结论：已覆盖
- 归属：X 最终引用与 H 历史正文（outputs、history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_output_references.py
- 证据：outputs、history 计划分段验证记录。
- 未核验前提：无

#### 验收 50
- 原文：[验收 50](database/consistency-verification.md#取回读取保护与清理)（相同 final_event_id 时固定选择清理项 ID 最大者）。
- 结论：已覆盖
- 归属：X 汇总（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/outputs.py
- 测试：apps/camctl/tests/integration/outputs/test_saved_errors.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 51
- 原文：[验收 51](database/consistency-verification.md#取回读取保护与清理)（取回项与交付职责表）。
- 结论：已覆盖
- 归属：X10 取回汇总（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/obtain_summary.py
- 测试：apps/camctl/tests/integration/outputs/test_obtain_summary.py
- 证据：outputs 计划分段验证记录（取回完成判定群）。
- 未核验前提：无

#### 验收 69
- 原文：[验收 69](database/consistency-verification.md#取回读取保护与清理)（取回字段表合法与非法组合）。
- 结论：已覆盖
- 归属：X 契约（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/definitions.py
- 测试：apps/camctl/tests/integration/outputs/test_outputs_contract.py
- 证据：outputs 计划分段验证记录（字段矩阵群）。
- 未核验前提：无

#### 验收 70
- 原文：[验收 70](database/consistency-verification.md#取回读取保护与清理)（清理字段矩阵）。
- 结论：已覆盖
- 归属：X 契约（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/definitions.py
- 测试：apps/camctl/tests/integration/outputs/test_outputs_contract.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 71
- 原文：[验收 71](database/consistency-verification.md#取回读取保护与清理)（三种 outcome 与复用完成记录）。
- 结论：已覆盖
- 归属：X9 清理（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_source_cleanup.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

#### 验收 72
- 原文：[验收 72](database/consistency-verification.md#取回读取保护与清理)（产物字段矩阵六种清理状态）。
- 结论：已覆盖
- 归属：X 契约（outputs 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/definitions.py
- 测试：apps/camctl/tests/integration/outputs/test_outputs_contract.py
- 证据：outputs 计划分段验证记录。
- 未核验前提：无

## 四、调用结果与中断恢复（验收 29–36、R-01–R-14）

尝试与进程的生产入口为 `apps/camctl/src/camctl/operations/process.py` 与 `apps/camctl/src/camctl/operations/recovery.py`；结果分类为 `apps/camctl/src/camctl/operations/models.py`。R/Q/S/O 系列的映射结论与[capture 计划 C9 验收映射档案](../superpowers/plans/2026-09-30-camctl-capture.md#c9-验收映射档案2026-10-07)一致，此处逐条独立列出。

#### 验收 29
- 原文：[验收 29](database/consistency-verification.md#调用结果与中断恢复)（删除超时后的决策表处理）。
- 结论：已覆盖
- 归属：O 受管进程（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/process.py
- 测试：apps/camctl/tests/integration/operations/test_process.py
- 证据：operations 计划分段验证记录；capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 30
- 原文：[验收 30](database/consistency-verification.md#调用结果与中断恢复)（响应保证范围与证据先后到达）。
- 结论：已覆盖
- 归属：O 尝试结果（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/models.py
- 测试：apps/camctl/tests/integration/operations/test_attempts.py
- 证据：operations 计划分段验证记录。
- 未核验前提：无

#### 验收 31
- 原文：[验收 31](database/consistency-verification.md#调用结果与中断恢复)（退出证据与假设收场共同保存）。
- 结论：已覆盖
- 归属：O 恢复（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/recovery.py
- 测试：apps/camctl/tests/integration/operations/test_recovery.py
- 证据：operations 计划分段验证记录。
- 未核验前提：无

#### 验收 32
- 原文：[验收 32](database/consistency-verification.md#调用结果与中断恢复)（camctl 退出后遗留执行进程的终止与放行）。
- 结论：已覆盖
- 归属：I1/I2 与 O6（integration、operations 计划）。
- 生产入口：apps/host-demo/src/process.c、apps/host-demo/src/process_group.c
- 测试：tests/integration/test_camctl_process_recovery.py、apps/host-demo/tests/integration/test_process.c
- 证据：I1/I2 的 2026-10-08 Linux 验证记录：零个、一个和多个遗留成员，正常与异常退出，信号已发出但尚未停止、已经停止但核验未完时均不放行；并行 submit 与主程序保持运行。
- 未核验前提：无

#### 验收 33
- 原文：[验收 33](database/consistency-verification.md#调用结果与中断恢复)（退出信息取得失败与回收顺序）。
- 结论：已覆盖
- 归属：I1/I2（integration 计划）。
- 生产入口：apps/host-demo/src/process.c、apps/host-demo/src/process_group.c
- 测试：apps/host-demo/tests/unit/test_process_unit.c、apps/host-demo/tests/unit/test_process_group.c、apps/host-demo/tests/integration/test_process.c、tests/integration/test_camctl_process_recovery.py
- 证据：I1/I2 的 2026-10-08 Linux 验证记录：保留退出记录、成员检查与组信号失败、主线程僵尸而工作线程仍活、扫描中成员消失、最终回收失败与提前回收分别覆盖；具体 PID 回收与其他子进程互不干扰。
- 未核验前提：无

#### 验收 34
- 原文：[验收 34](database/consistency-verification.md#调用结果与中断恢复)（收场期间 needs_run 与预算保持）。
- 结论：已覆盖
- 归属：I2/O6 主机收场与 S/O 会话、尝试恢复（integration、operations、session 计划）。
- 生产入口：apps/host-demo/src/host.c、apps/host-demo/src/scheduler.c、apps/camctl/src/camctl/session/service.py、apps/camctl/src/camctl/operations/recovery.py
- 测试：tests/integration/test_camctl_process_recovery.py、apps/camctl/tests/integration/session/test_handoff.py、apps/camctl/tests/integration/operations/test_recovery.py
- 证据：I1/I2 的 2026-10-08 Linux 记录验证收场期间保留 pending、异常重启合并、预算耗尽后新提交只触发一次正常启动且不重置计数；业务身份与预算恢复继续由 session、operations 计划分段记录证明。
- 未核验前提：无

#### 验收 35
- 原文：[验收 35](database/consistency-verification.md#调用结果与中断恢复)（超时收场前冻结报告与重建字节保持）。
- 结论：已覆盖
- 归属：R 报告重建与 H 恢复（reporting、history 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/generation.py
- 测试：tests/integration/test_camctl_report_roundtrip.py
- 证据：集成计划 I4 验证记录（固定字节对照用例）。
- 未核验前提：无

#### 验收 36
- 原文：[验收 36](database/consistency-verification.md#调用结果与中断恢复)（发现超时、收场、提交各边界中断）。
- 结论：已覆盖
- 归属：O 恢复与 C 中断恢复（operations、capture 计划）。
- 生产入口：apps/camctl/src/camctl/operations/recovery.py
- 测试：apps/camctl/tests/integration/capture/test_recording_reconcile.py
- 证据：operations、capture 计划分段验证记录（TestInterruptionRecovery 与恢复群）。
- 未核验前提：无

#### 验收 R-01
- 原文：[验收 R-01](database/consistency-verification.md#调用结果与中断恢复)（尝试字段表与结果引用校验）。
- 结论：已覆盖
- 归属：O 尝试结果（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/validation.py
- 测试：apps/camctl/tests/unit/operations/test_results.py
- 证据：capture 计划 C9 档案 R 群；operations 计划分段验证记录。
- 未核验前提：无

#### 验收 R-02
- 原文：[验收 R-02](database/consistency-verification.md#调用结果与中断恢复)（调用失败与效果组合）。
- 结论：已覆盖
- 归属：O 尝试结果（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/models.py
- 测试：apps/camctl/tests/unit/operations/test_results.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-03
- 原文：[验收 R-03](database/consistency-verification.md#调用结果与中断恢复)（本地与远端退出结果记录）。
- 结论：已覆盖
- 归属：O 结果记录（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/models.py
- 测试：apps/camctl/tests/integration/operations/test_attempts.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-04
- 原文：[验收 R-04](database/consistency-verification.md#调用结果与中断恢复)（结束依据三类与运行假设记录）。
- 结论：已覆盖
- 归属：O 结果记录（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/models.py
- 测试：apps/camctl/tests/integration/operations/test_attempts.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-05
- 原文：[验收 R-05](database/consistency-verification.md#调用结果与中断恢复)（各边界完整结果保存与 ADB 恢复记录）。
- 结论：已覆盖
- 归属：O 恢复（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/recovery.py
- 测试：apps/camctl/tests/integration/operations/test_recovery.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-06
- 原文：[验收 R-06](database/consistency-verification.md#调用结果与中断恢复)（终止宽限配置与升级）。
- 结论：已覆盖
- 归属：O 进程收场（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/process.py
- 测试：apps/camctl/tests/integration/operations/test_process.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-07
- 原文：[验收 R-07](database/consistency-verification.md#调用结果与中断恢复)（三种超时触发本地终止）。
- 结论：已覆盖
- 归属：O 进程收场（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/process.py
- 测试：apps/camctl/tests/integration/operations/test_process.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-08
- 原文：[验收 R-08](database/consistency-verification.md#调用结果与中断恢复)（共享服务端生命周期）。
- 结论：部分覆盖
- 归属：O 进程（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/process.py
- 测试：apps/camctl/tests/integration/operations/test_process.py
- 证据：启动与连接契约受约束替身的分支已覆盖（capture 计划 C9 档案）。
- 未核验前提：固定 ADB 版本与真实启动行为，归目标主机联调（不作为软件集成门槛）。

#### 验收 R-09
- 原文：[验收 R-09](database/consistency-verification.md#调用结果与中断恢复)（服务端脱离原组的收场范围）。
- 结论：已覆盖
- 归属：I2/O6（integration、operations 计划）。
- 生产入口：apps/host-demo/src/process.c、apps/host-demo/src/process_group.c、apps/camctl/src/camctl/operations/process.py
- 测试：tests/integration/test_camctl_process_recovery.py
- 证据：I1/I2 的 2026-10-08 Linux 验证记录：模拟服务端未脱离、已脱离及脱离与终止并发的三种真实进程组合；原组客户端停止后放行，独立服务端继续运行，并行 submit 不被终止，测试自身回收服务端。
- 未核验前提：无

#### 验收 R-10
- 原文：[验收 R-10](database/consistency-verification.md#调用结果与中断恢复)（独立组建组与组归属保持）。
- 结论：已覆盖
- 归属：I1（integration 计划）。
- 生产入口：apps/host-demo/src/process.c、apps/camctl/src/camctl/operations/process.py
- 测试：apps/host-demo/tests/unit/test_spawn.c、apps/host-demo/tests/integration/test_process.c、tests/integration/test_camctl_process_recovery.py
- 证据：I1/I2 的 2026-10-08 Linux 验证记录：exec 前独立组，主程序、run 和 submit 的组相互独立，普通后代及生产入口启动的工具继承调用组；组建立失败不执行，程序不存在与已执行后退出 127 分别判定。
- 未核验前提：无

#### 验收 R-11
- 原文：[验收 R-11](database/consistency-verification.md#调用结果与中断恢复)（SIGCHLD 处置方式与各自回收）。
- 结论：已覆盖
- 归属：I1/I2（integration 计划）。
- 生产入口：apps/host-demo/src/process.c、apps/host-demo/src/host.c
- 测试：apps/host-demo/tests/unit/test_spawn.c、apps/host-demo/tests/integration/test_init_failure.c、apps/host-demo/tests/integration/test_process.c、apps/host-demo/tests/integration/test_host.py、tests/integration/test_camctl_process_recovery.py
- 证据：I1/I2 的 2026-10-08 Linux 验证记录：SIG_DFL 与合法处理器，初始化拒绝显式忽略和 SA_NOCLDWAIT，运行期间设置变化阻止下一启动，提前回收后保留责任且不再发送旧组信号；模块不修改全局处置，各方具体 PID 回收互不干扰。
- 未核验前提：无

#### 验收 R-12
- 原文：[验收 R-12](database/consistency-verification.md#调用结果与中断恢复)（成员检查不可读等分支与启动入口审计）。
- 结论：部分覆盖
- 归属：I1/I2/O6 与 F6 审计（integration、operations、host-files 计划）。
- 生产入口：apps/host-demo/src/process.c、apps/host-demo/src/process_group.c、apps/camctl/src/camctl/operations/process.py
- 测试：apps/host-demo/tests/unit/test_process_group.c、apps/host-demo/tests/unit/test_process_unit.c、tests/integration/test_camctl_process_recovery.py、apps/camctl/tests/integration/contracts/test_external_boundaries.py
- 证据：I1/I2 的 2026-10-08 Linux 记录覆盖读取权限、格式与线程数量异常、归属未知、终止失败和扫描期间消失；生产工具创建点继续由真实源码 AST 边界检查证明，C 启动、退出、扫描、信号和回收入口已审计。
- 未核验前提：第三方主程序实际回收逻辑、目标工具及包装程序的归属和查询、终止权限，由目标主机联调核验。

#### 验收 R-13
- 原文：[验收 R-13](database/consistency-verification.md#调用结果与中断恢复)（明确返回的记录）。
- 结论：已覆盖
- 归属：O 结果记录（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/models.py
- 测试：apps/camctl/tests/integration/operations/test_attempts.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

#### 验收 R-14
- 原文：[验收 R-14](database/consistency-verification.md#调用结果与中断恢复)（可靠未派发的记录）。
- 结论：已覆盖
- 归属：O 结果记录（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/models.py
- 测试：apps/camctl/tests/integration/operations/test_attempts.py
- 证据：capture 计划 C9 档案 R 群。
- 未核验前提：无

## 五、活动查询的责任与计数（Q-01–Q-12）

查询用途与计数的生产入口为 `apps/camctl/src/camctl/operations/queries.py` 与 `apps/camctl/src/camctl/persistence/repositories/operations.py`。

#### 验收 Q-01
- 原文：[验收 Q-01](database/consistency-verification.md#活动查询的责任与计数)（B、C 为各自启动检查同一相机，观察归 A、计数归各自）。
- 结论：已覆盖
- 归属：O 查询责任（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/queries.py
- 测试：apps/camctl/tests/integration/operations/test_queries.py
- 证据：capture 计划 C9 档案 Q 群；operations 计划分段验证记录。
- 未核验前提：无

#### 验收 Q-02
- 原文：[验收 Q-02](database/consistency-verification.md#活动查询的责任与计数)（同一责任保持流程身份和累计次数）。
- 结论：已覆盖
- 归属：O 查询责任（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/queries.py
- 测试：apps/camctl/tests/integration/operations/test_queries.py
- 证据：capture 计划 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-03
- 原文：[验收 Q-03](database/consistency-verification.md#活动查询的责任与计数)（启动核实与停止核实独立计数）。
- 结论：已覆盖
- 归属：O 查询责任（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/queries.py
- 测试：apps/camctl/tests/unit/operations/test_queries.py
- 证据：capture 计划 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-04
- 原文：[验收 Q-04](database/consistency-verification.md#活动查询的责任与计数)（按查询目的解释可靠观察）。
- 结论：已覆盖
- 归属：O 查询责任（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/queries.py
- 测试：apps/camctl/tests/integration/operations/test_queries.py
- 证据：capture 计划 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-05
- 原文：[验收 Q-05](database/consistency-verification.md#活动查询的责任与计数)（执行前检查覆盖五种相机状态）。
- 结论：已覆盖
- 归属：O 查询与 C 残留收场（operations、capture 计划）。
- 生产入口：apps/camctl/src/camctl/capture/residual.py
- 测试：apps/camctl/tests/integration/bootstrap/test_residual_winddown.py
- 证据：capture 计划第十七段验证记录（preflight 四分区）与 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-06
- 原文：[验收 Q-06](database/consistency-verification.md#活动查询的责任与计数)（源文件关联查询、对账及延时结束后的查询）。
- 结论：已覆盖
- 归属：O 查询与 C 对账（operations、capture 计划）。
- 生产入口：apps/camctl/src/camctl/capture/recovery.py
- 测试：apps/camctl/tests/integration/capture/test_recording_reconcile.py
- 证据：capture 计划分段验证记录（对账群）与 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-07
- 原文：[验收 Q-07](database/consistency-verification.md#活动查询的责任与计数)（B 为 A 建立残留收场，三类计数独立）。
- 结论：已覆盖
- 归属：C 残留收场（capture 计划）。
- 生产入口：apps/camctl/src/camctl/capture/residual.py
- 测试：apps/camctl/tests/integration/bootstrap/test_residual_winddown.py
- 证据：capture 计划第十七段验证记录（执行前检查、收场停止、确认查询三类分别断言计数）。
- 未核验前提：无

#### 验收 Q-08
- 原文：[验收 Q-08](database/consistency-verification.md#活动查询的责任与计数)（产物结果核实覆盖各集合形态，一轮只消耗一次尝试）。
- 结论：已覆盖
- 归属：C 结果核实（capture 计划）。
- 生产入口：apps/camctl/src/camctl/capture/handlers.py
- 测试：apps/camctl/tests/integration/capture/test_listing_rounds.py
- 证据：capture 计划分段验证记录（轮次化列举群）与 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-09
- 原文：[验收 Q-09](database/consistency-verification.md#活动查询的责任与计数)（查询与核实配置六项默认值与逐字段覆盖）。
- 结论：已覆盖
- 归属：O 配置（operations 计划）与 B 配置装配（bootstrap 计划）。
- 生产入口：apps/camctl/src/camctl/bootstrap/config.py
- 测试：apps/camctl/tests/integration/bootstrap/test_configuration.py
- 证据：capture 计划第十七段验证记录（query/residual_stop 子表用例）与 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-10
- 原文：[验收 Q-10](database/consistency-verification.md#活动查询的责任与计数)（可控单调钟下的检查间隔与调用期限）。
- 结论：已覆盖
- 归属：O 查询与重试间隔（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/attempts.py
- 测试：apps/camctl/tests/integration/capture/test_retry_intervals.py
- 证据：capture 计划分段验证记录（重试间隔群）与 C9 档案 Q 群。
- 未核验前提：无

#### 验收 Q-11
- 原文：[验收 Q-11](database/consistency-verification.md#活动查询的责任与计数)（五种用途、责任键与目标字段组合）。
- 结论：已覆盖
- 归属：O 查询（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/queries.py
- 测试：apps/camctl/tests/unit/operations/test_queries.py
- 证据：capture 计划 C9 档案 Q 群（含跨表归属拒绝断言）。
- 未核验前提：无

#### 验收 Q-12
- 原文：[验收 Q-12](database/consistency-verification.md#活动查询的责任与计数)（配置采用目标设备与流程结束分类）。
- 结论：已覆盖
- 归属：O 查询（operations 计划）。
- 生产入口：apps/camctl/src/camctl/operations/queries.py
- 测试：apps/camctl/tests/integration/operations/test_queries.py
- 证据：capture 计划 C9 档案 Q 群。
- 未核验前提：无

## 六、应急停止与活动占用释放（S-01–S-07、O-01–O-06）

应急补记的生产入口为 `apps/camctl/src/camctl/persistence/repositories/capture.py`（应急流程与尝试命令）与接入模块协议；占用释放为 `apps/camctl/src/camctl/persistence/repositories/capture.py` 的统一释放判定。

#### 验收 S-01
- 原文：[验收 S-01](database/consistency-verification.md#应急停止的最终补记)（应急资格条件组合）。
- 结论：已覆盖
- 归属：C 应急停止（capture 计划）。
- 生产入口：apps/camctl/src/camctl/capture/results.py
- 测试：apps/camctl/tests/integration/capture/test_emergency.py
- 证据：capture 计划 C9 档案 S 群（11 用例含释放组合）。
- 未核验前提：无

#### 验收 S-02
- 原文：[验收 S-02](database/consistency-verification.md#应急停止的最终补记)（应急记录组合四分区）。
- 结论：已覆盖
- 归属：C 应急停止（capture 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/capture/test_emergency.py
- 证据：capture 计划 C9 档案 S 群。
- 未核验前提：无

#### 验收 S-03
- 原文：[验收 S-03](database/consistency-verification.md#应急停止的最终补记)（多录像分别收场的共同补记）。
- 结论：已覆盖
- 归属：C 应急停止（capture 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/capture/test_emergency.py
- 证据：capture 计划 C9 档案 S 群。
- 未核验前提：无

#### 验收 S-04
- 原文：[验收 S-04](database/consistency-verification.md#应急停止的最终补记)（真实写入事务中的非法组合拒绝）。
- 结论：已覆盖
- 归属：C 应急停止（capture 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/unit/capture/test_emergency_guard.py
- 证据：capture 计划 E5 段验证记录（守卫直测反例群）。
- 未核验前提：无

#### 验收 S-05
- 原文：[验收 S-05](database/consistency-verification.md#应急停止的最终补记)（补记诊断三分支）。
- 结论：已覆盖
- 归属：C 应急停止（capture 计划）。
- 生产入口：apps/camctl/src/camctl/capture/results.py
- 测试：apps/camctl/tests/integration/capture/test_emergency.py
- 证据：capture 计划 C9 档案 S 群。
- 未核验前提：无

#### 验收 S-06
- 原文：[验收 S-06](database/consistency-verification.md#应急停止的最终补记)（尝试之间与补记期间中断）。
- 结论：已覆盖
- 归属：C 应急停止（capture 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/capture/test_emergency.py
- 证据：capture 计划 C9 档案 S 群（中断恢复用例）。
- 未核验前提：无

#### 验收 S-07
- 原文：[验收 S-07](database/consistency-verification.md#应急停止的最终补记)（补记前后冻结边界三路径一致）。
- 结论：部分覆盖
- 归属：C 应急停止与 H 历史恢复（capture、history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/capture/test_emergency.py
- 证据：回放与快照等价性由 J/H 系列通用验证覆盖；capture 计划 C9 档案 S-07 行。
- 未核验前提：应急补记专属冻结边界的显式三路径对照，归路线图阶段 7 历史与规模验证行核对。

#### 验收 O-01
- 原文：[验收 O-01](database/consistency-verification.md#设备活动占用释放)（释放必要条件的行列组合）。
- 结论：已覆盖
- 归属：Q6 统一释放判定（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：scheduling 计划 Q6 映射注记（第九十六段落档）；capture 计划 C9 档案 O 群。
- 未核验前提：无

#### 验收 O-02
- 原文：[验收 O-02](database/consistency-verification.md#设备活动占用释放)（时间与产物分支的固定完成方式）。
- 结论：已覆盖
- 归属：Q6 统一释放判定与 C 结果确认（scheduling、capture 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/capture/test_result_confirmation.py
- 证据：capture 计划第六十七段验证记录（结果确认四分支事务）。
- 未核验前提：无

#### 验收 O-03
- 原文：[验收 O-03](database/consistency-verification.md#设备活动占用释放)（归属未固定时 B 不执行冲突拍摄）。
- 结论：已覆盖
- 归属：Q6 输出范围限制（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：scheduling 计划分段验证记录（`test_baseline_scope_limited_rejects_release` 等范围群）。
- 未核验前提：无

#### 验收 O-04
- 原文：[验收 O-04](database/consistency-verification.md#设备活动占用释放)（八类释放入口的统一条件与事件顺序）。
- 结论：已覆盖
- 归属：Q6 统一释放入口（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：scheduling 计划 Q6 行注记（八释放入口齐，含第十七段残留收场入口）。
- 未核验前提：无

#### 验收 O-05
- 原文：[验收 O-05](database/consistency-verification.md#设备活动占用释放)（交错结束、归属、释放与下一候选授予）。
- 结论：部分覆盖
- 归属：Q6 与 Q4（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：迟到观察不清除新占用、同设备推进已覆盖（`test_late_observation_keeps_new_owner_occupancy` 等）。
- 未核验前提：活动结束、归属、释放、收场与授予的完整交错竞争矩阵，归路线图阶段 6 排序竞争行。

#### 验收 O-06
- 原文：[验收 O-06](database/consistency-verification.md#设备活动占用释放)（结束依据与占用变化共同提交的中断与三路径恢复）。
- 结论：部分覆盖
- 归属：Q6 与 H（scheduling、history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/capture.py
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：恢复等价与回放不操作设备已覆盖（history 计划分段验证记录）。
- 未核验前提：共同提交前后逐点中断注入，归路线图阶段 3 中断行。

## 七、路径定位、主机拍摄、运行库与编号（F-01–F-08、HBF-01–HBF-02、V-01–V-04、J-01–J-06、E-01–E-02）

路径与文件操作的生产入口为 `apps/camctl/src/camctl/host_files/paths.py`、`apps/camctl/src/camctl/host_files/io.py` 与 `apps/camctl/src/camctl/host_files/handoff.py`；运行库条件为 `apps/camctl/src/camctl/persistence/runtime.py`；初始化为 `apps/camctl/src/camctl/persistence/initialization.py`。

#### 验收 F-01
- 原文：[验收 F-01](database/consistency-verification.md#中间文件的路径与定位)（四种用途的根目录、子目录与文件名校验）。
- 结论：已覆盖
- 归属：F1 路径定位（host-files 计划）。
- 生产入口：apps/camctl/src/camctl/host_files/paths.py
- 测试：apps/camctl/tests/integration/host_files/test_paths.py
- 证据：host-files 计划分段验证记录。
- 未核验前提：无

#### 验收 F-02
- 原文：[验收 F-02](database/consistency-verification.md#中间文件的路径与定位)（先登记再创建与修复目标提升）。
- 结论：已覆盖
- 归属：F 线程任务与 X 提升（host-files、outputs 计划）。
- 生产入口：apps/camctl/src/camctl/host_files/tasks.py
- 测试：apps/camctl/tests/integration/host_files/test_media_tasks.py
- 证据：host-files 计划分段验证记录；修复目标提升见 capture 计划（test_output_promotion.py）。
- 未核验前提：无

#### 验收 F-03
- 原文：[验收 F-03](database/consistency-verification.md#中间文件的路径与定位)（文件存在、缺失、权限错误与移动恢复）。
- 结论：已覆盖
- 归属：F7 文件契约（host-files 计划）。
- 生产入口：apps/camctl/src/camctl/host_files/io.py
- 测试：apps/camctl/tests/integration/host_files/test_file_contract.py
- 证据：host-files 计划 F6/F7 收口验证记录（四边界注入记录==观察）。
- 未核验前提：无

#### 验收 F-04
- 原文：[验收 F-04](database/consistency-verification.md#中间文件的路径与定位)（历史恢复不访问、不移动、不重建文件）。
- 结论：已覆盖
- 归属：H 文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/queries.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划 H5 收口验证记录。
- 未核验前提：无

#### 验收 F-05
- 原文：[验收 F-05](database/consistency-verification.md#中间文件的路径与定位)（三处目录绑定共同保存与重复初始化）。
- 结论：已覆盖
- 归属：B1 初始化与 P 运行库（bootstrap、persistence 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/initialization.py
- 测试：apps/camctl/tests/integration/bootstrap/test_initialization.py
- 证据：bootstrap 计划分段验证记录（重复初始化保留身份、绑定不一致拒绝、无效文件不重建）。
- 未核验前提：无

#### 验收 F-06
- 原文：[验收 F-06](database/consistency-verification.md#中间文件的路径与定位)（阻止切换目录的完整分类）。
- 结论：已覆盖
- 归属：B4 目录切换开放项（bootstrap 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/directory_switch.py、apps/camctl/src/camctl/persistence/initialization.py
- 测试：apps/camctl/tests/integration/bootstrap/test_directory_switch.py、apps/camctl/tests/unit/persistence/test_directory_switch.py
- 证据：B4 开放项交付验证记录（REQUIRED、未完成清理、已提升未清理成品、未结束运行、未完成交付与撤回、本地报告责任七类分别计数并按检查顺序给出首个阻止诊断；已结束清理、已 CLEANED 成品、CANCELED 交付、PUBLISHED 报告与终态运行不构成旧路径责任；切换事务内重新核对发现责任保持原绑定；目录不可靠检查的受控错误注入阻止切换）。
- 未核验前提：无

#### 验收 F-07
- 原文：[验收 F-07](database/consistency-verification.md#中间文件的路径与定位)（责任全部结束后的允许切换）。
- 结论：已覆盖
- 归属：B4 目录切换与主机交接（bootstrap 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/initialization.py、apps/camctl/src/camctl/persistence/directory_switch.py
- 测试：apps/camctl/tests/integration/bootstrap/test_directory_switch.py、tests/integration/test_camctl_directory_switch.py
- 证据：`test_directory_switch.py::test_configured_root_symlink_is_rejected` 在 Linux 上实际创建 staging、ready、processing 三处的有效与悬空符号链接，并分别验证新库初始化拒绝且不创建状态库、既有库切换拒绝且保持原绑定（DS2 记录 39 passed、1 skipped，Windows 仅跳过真实链接对象测试）。责任全部结束且原目录只剩空目录树时，切换共同保存三路径、保留数据库身份及全部业务历史；其他路径边界、原目录保留和重复初始化亦有覆盖。`tests/integration/test_camctl_directory_switch.py::test_new_binding_publishes_and_claims_after_explicit_init` 以真实 CLI 和 C 接入模块在新绑定发布并领取报告，核对 processing 字节和旧目录无残留。
- 未核验前提：无

#### 验收 F-08
- 原文：[验收 F-08](database/consistency-verification.md#中间文件的路径与定位)（切换各边界中断与三路径共同更新）。
- 结论：已覆盖
- 归属：B4 目录切换的历史保留与进程恢复（bootstrap 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/initialization.py、apps/camctl/src/camctl/persistence/directory_switch.py
- 测试：apps/camctl/tests/integration/bootstrap/test_directory_switch.py、apps/camctl/tests/integration/bootstrap/test_directory_switch_history.py、apps/camctl/tests/unit/persistence/test_directory_switch.py、tests/integration/test_camctl_directory_switch.py
- 证据：提交边界注入后重读整组三路径并分类完成、未完成或不一致；`test_directory_switch_history.py::test_switch_preserves_report_bytes_ack_and_file_history` 使用真实报告、文件 ID 与累计 ACK，切换后逐表核对业务记录并重建旧报告，确认原字节不变。`test_process_exit_preserves_whole_binding_and_business` 分别在提交前和实际提交后返回前终止子进程，再由新进程核对完整旧／新绑定和业务历史，并验证显式初始化可继续。跨组件测试另以真实 CLI 和 C 模块验证新目录发布与领取（DS3 记录 Linux 通过）。
- 未核验前提：无

#### 验收 HBF-01
- 原文：[主机单张拍摄的成对文件责任](database/consistency-verification.md#主机单张拍摄的成对文件责任)（原图和预览共同登记、保存完整字节并恢复原身份）。
- 结论：部分覆盖
- 归属：Hasselblad T1/T3（主机文件责任和单张拍摄编排）。
- 生产入口：apps/camctl/src/camctl/capture/host_photo.py、apps/camctl/src/camctl/persistence/repositories/host_capture.py、apps/camctl/src/camctl/persistence/repositories/capture.py、apps/camctl/src/camctl/history/host_photo_request.py
- 测试：apps/camctl/tests/integration/capture/test_host_photo_storage.py、apps/camctl/tests/integration/capture/test_host_photo_flow.py、apps/camctl/tests/integration/capture/test_host_photo_finish_pair_boundary.py、apps/camctl/tests/integration/capture/test_host_photo_complete_history_recovery.py、apps/camctl/tests/integration/capture/test_host_photo_malformed_history_entries.py、apps/camctl/tests/integration/host_files/test_capture_sources.py
- 证据：`test_host_photo_storage.py::test_prepare_reuses_action_files_across_operation_keys_without_physical_files` 核对两份固定用途路径和稳定文件身份；`test_host_original_preview_register_promote_and_recover_without_new_outputs` 核对同一事务中的 `CREATE`、`HOST_PHOTO_COMPLETE`、原图与预览关联以及同键／新键恢复。`test_host_photo_flow.py::test_host_photo_resumes_after_original_archive_and_ignores_later_binding_failure` 在原图已归档后注入中断，恢复时复用原图并完成预览，仅派发一次 START；`test_complete_receipt_unknown_verifies_same_request_and_key` 覆盖完整字节已提交但回执未知的独立只读核实。`test_capture_sources.py` 分别验证原图和 JPEG 的大小、摘要与文件同步，以及移动后重试复用匹配目标；历史测试覆盖准备、完整字节、成品与清理边界的恢复。ARM 记录另有真实 RAW 与配对 JPEG 的拍摄、独立取回及报告证据（[软件验证](verification.md#hasselblad-主机单张接入的软件验证2026-10-12)、[ARM 验收](verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)）。
- 未核验前提：现有流程中断用例在同一个 runtime 实例中恢复；没有用新装配的进程级运行时分别恢复“START 结果已保存但尚未归档”及“两份源文件均归档但完整字节事件尚未保存”的组合。该范围是恢复演练覆盖边界，不否定已经验证的成对持久化、原图归档后恢复或 ARM 真实拍摄链。

#### 验收 HBF-02
- 原文：[主机单张拍摄的成对文件责任](database/consistency-verification.md#主机单张拍摄的成对文件责任)（提交结果未知时使用独立只读连接核实原请求与键，格式 1/2 保持各自边界）。
- 结论：部分覆盖
- 归属：Hasselblad T3（原请求核实）与状态库格式 P1（persistence 计划）。
- 生产入口：apps/camctl/src/camctl/capture/host_photo.py、apps/camctl/src/camctl/persistence/repositories/host_capture.py、apps/camctl/src/camctl/persistence/runtime.py
- 测试：apps/camctl/tests/integration/capture/test_host_photo_flow.py、apps/camctl/tests/integration/capture/test_host_photo_storage.py、apps/camctl/tests/integration/capture/test_host_photo_complete_history_recovery.py、apps/camctl/tests/integration/capture/test_host_photo_malformed_history_entries.py、apps/camctl/tests/integration/persistence/test_format_admission.py、apps/camctl/tests/integration/bootstrap/test_initialization.py、apps/camctl/tests/integration/bootstrap/test_distribution.py
- 证据：`test_host_photo_flow.py::test_complete_receipt_unknown_verifies_same_request_and_key` 覆盖完整字节事务已提交但调用方收到未知时，先结束原写事务，再用另一条 `query_only` 连接核实相同请求和键，且拍摄只执行一次；`test_unknown_host_photo_receipt_without_verifier_retains_request` 覆盖核实端口缺失或抛错时原请求仍在运行时待处理集合中，且不重复拍摄。`test_host_photo_storage.py::test_original_complete_request_can_be_verified_after_promotion_with_new_connection` 覆盖新连接重建已提交准备／完整字节申请、修改申请返回未知、未登记键返回可靠未提交；畸形事件测试覆盖核实读取拒绝坏申请。格式 1 的初始化保持历史和版本（`test_initialization.py::test_init_preserves_legacy_v1_sql_metadata_and_history`），受理及仓储入口均拒绝新增主机照片责任且无历史副作用；安装包测试确认格式 2 新库和主机照片计划。ARM 格式 2 初始化及真实拍摄记录见[ARM 验收](verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)。
- 未核验前提：仓储测试已分别验证核实结果为 `ROLLED_BACK`、`UNKNOWN` 和 `COMPLETED`，生产流程代码也按原请求与键分支处理；尚缺一项流程集成组合，注入前次保存回执未知后依次让独立核实返回“可靠未提交”或“仍未知”，断言同键重试或保留待处理申请。当前已验证的 ARM 拍摄、配对、取回与报告不依赖该模拟分支。

#### 验收 V-01
- 原文：[验收 V-01](database/consistency-verification.md#sqlite-运行库与部署版本)（统一条件文件的版本判定）。
- 结论：已覆盖
- 归属：P1 运行库条件（persistence 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/runtime.py
- 测试：apps/camctl/tests/integration/persistence/test_runtime.py
- 证据：persistence 计划分段验证记录（允许/拒绝/格式非法版本群；权威资源同源见 bootstrap 计划 test_package.py）。
- 未核验前提：无

#### 验收 V-02
- 原文：[验收 V-02](database/consistency-verification.md#sqlite-运行库与部署版本)（记录实际版本并验证 STRICT、JSON、增量 BLOB 与 WAL/FULL）。
- 结论：部分覆盖
- 归属：P1 运行库（persistence 计划）与 H 快照（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/runtime.py
- 测试：apps/camctl/tests/integration/persistence/test_runtime.py
- 证据：写连接 PRAGMA 核验（WAL/FULL）、STRICT/JSON 由结构 SQL 与事务测试覆盖、增量 BLOB 分段读写由快照维护覆盖（persistence、history 计划分段验证记录；检查器执行全部规格 SQL）。ARM64 Hasselblad 验收实际记录 CPython 3.11.17、SQLite 3.53.1、独立虚拟环境和安装发行物，并完成格式 2 初始化、拍摄、取回及报告（见[ARM 真实拍摄与取回验收](verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)）；Action6 普通录像也在同一目标环境完成现场验收（见[现场验收记录](verification.md#action6-普通录像演示的现场验收2026-10-11)）。
- 未核验前提：目标进程的 `sqlite_source_id()` 与 SQL `sqlite_version()` 对照，以及在 ARM64 上单独执行的 JSON、增量 BLOB、WAL/FULL 能力检查未见记录；归 V-04/B7。

#### 验收 V-03
- 原文：[验收 V-03](database/consistency-verification.md#sqlite-运行库与部署版本)（init、run、submit 与报告子进程的兼容分支）。
- 结论：已覆盖
- 归属：B4 开放项（bootstrap 计划）与 B1 入口（bootstrap 计划）。
- 生产入口：apps/camctl/src/camctl/session/service.py、apps/camctl/src/camctl/persistence/initialization.py、apps/camctl/src/camctl/reporting/worker.py
- 测试：apps/camctl/tests/integration/session/test_session.py、apps/camctl/tests/integration/bootstrap/test_initialization.py、apps/camctl/tests/integration/reporting/test_worker.py
- 证据：B4 开放项交付验证记录（run/submit 注入运行库不兼容返回 `configuration_error` 且拒绝先于业务库打开、不创建空库；运行库合格时同一缺失目标仍按 `state_db_error`；init 不兼容在任何文件创建前失败；报告子进程 RUNTIME_CHECK 阶段启动失败发送 `StartupFailedMessage` 并以退出码 3 结束，报告责任由主进程按消息保留、普通设备工作继续）。
- 未核验前提：无

#### 验收 V-04
- 原文：[验收 V-04](database/consistency-verification.md#sqlite-运行库与部署版本)（目标 Python 3.11/ARM64 发行物固定构建验证）。
- 结论：部分覆盖
- 归属：P1 运行库条件（persistence 计划）与 B7 发行物及部署验证（bootstrap 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/runtime.py
- 测试：apps/camctl/tests/integration/bootstrap/test_distribution.py、apps/camctl/tests/integration/persistence/test_runtime.py
- 证据：B7 已在 WSL x86 合规环境（CPython 3.11.17、SQLite 3.53.1）完成源码目录外构建、按锁文件安装、`init`/`describe`/`submit`/设备替身 `run`，并核对权威资源及运行依赖（[B7 验证记录](../superpowers/plans/2026-09-30-camctl-bootstrap.md#b7-验证记录2026-10-08)）。目标 ARM64 已安装发行物并在真实工作流中运行：Hasselblad 使用独立环境和数据目录完成格式 2 初始化、真实单张拍摄、原图与预览配对、独立取回及报告；Action6 普通录像在 ARM 安装更新包后由用户确认演示及独立取回正常（[Hasselblad ARM 验收](verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)、[Action6 现场验收](verification.md#action6-普通录像演示的现场验收2026-10-11)）。
- 未核验前提：尚无 ARM64 上直接执行统一运行库能力检查，并组合验证真实 WAL 多连接、读视图、提交、历史恢复及报告流程的专项记录；这项 SQLite 目标运行库验证仍归 P1/B7。它不影响已完成的 Action6 普通录像和 Hasselblad 单张现场验收，也不表示 ARM64 安装与运行尚未进行。

#### 验收 J-01
- 原文：[验收 J-01](database/consistency-verification.md#历史引用与事务边界)（规则文件逐列核对引用分类）。
- 结论：已覆盖
- 归属：K/H 规格检查（contracts、history 计划）。
- 生产入口：docs/camctl/database/history-formats.md
- 测试：scripts/check-database-spec.py
- 证据：根检查器逐列核对规则文件、正文、投影和目录，历轮全量通过（路线图各阶段验证记录）。
- 未核验前提：无

#### 验收 J-02
- 原文：[验收 J-02](database/consistency-verification.md#历史引用与事务边界)（事务末位引用与分批读取）。
- 结论：已覆盖
- 归属：P3 事务（persistence 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/persistence/test_transactions.py
- 证据：persistence 计划分段验证记录（末位引用群）。
- 未核验前提：无

#### 验收 J-03
- 原文：[验收 J-03](database/consistency-verification.md#历史引用与事务边界)（首次事实不被后续观察覆盖）。
- 结论：部分覆盖
- 归属：H2 历史归属（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/changes.py、apps/camctl/src/camctl/persistence/repositories/acceptance.py、apps/camctl/src/camctl/session/service.py
- 测试：apps/camctl/tests/integration/history/test_changes.py、apps/camctl/tests/integration/acceptance/test_acceptance.py、apps/camctl/tests/integration/session/test_session.py
- 证据：`history/test_changes.py::test_internal_event_has_no_watermark_and_consumes_none` 覆盖可信时间首次建立的事件引用与业务水位边界，历史计划分段验证记录覆盖 J-03 的首次事实与既有结果保持。受理测试覆盖事件构造及复用流程，但当前没有断言受理事件的 `clock_status` 与调用模式一致。
- 未核验前提：受理仓储的 `_envelope` 将所有受理事件固定记为 `TRUSTED`；`submit` 和带计划 `run` 均在启动墙钟检查前完成受理，因此这些事件需要表示尚未检查的 `UNCHECKED`。该缺口由[SW-01](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)跟踪；它不否定本条其他首次写入及历史恢复覆盖。

#### 验收 J-04
- 原文：[验收 J-04](database/consistency-verification.md#历史引用与事务边界)（交错保存与逆向撤回的计数）。
- 结论：已覆盖
- 归属：H1/H3 事件校验与恢复（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/replay.py
- 测试：apps/camctl/tests/integration/history/test_replay_continuity.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 J-05
- 原文：[验收 J-05](database/consistency-verification.md#历史引用与事务边界)（冻结依据早于本次事务首条事件）。
- 结论：已覆盖
- 归属：R2 冻结（reporting 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/policy.py
- 测试：apps/camctl/tests/integration/reporting/test_freeze.py
- 证据：reporting 计划分段验证记录（冻结边界群）。
- 未核验前提：无

#### 验收 J-06
- 原文：[验收 J-06](database/consistency-verification.md#历史引用与事务边界)（编码失败重做或共同回滚）。
- 结论：已覆盖
- 归属：P3 事务编码（persistence 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/persistence/test_commit_recovery.py
- 证据：persistence 计划分段验证记录（提交未知核实与回滚群）。
- 未核验前提：无

#### 验收 E-01
- 原文：[验收 E-01](database/consistency-verification.md#整数编号与字段约束)（枚举定义格式与编号唯一性）。
- 结论：已覆盖
- 归属：K1 枚举（contracts 计划）。
- 生产入口：scripts/check-database-spec.py
- 测试：apps/camctl/tests/integration/contracts/test_enums.py
- 证据：contracts 计划分段验证记录；enum-registry 同源由 bootstrap 计划 test_package.py 强制。
- 未核验前提：无

#### 验收 E-02
- 原文：[验收 E-02](database/consistency-verification.md#整数编号与字段约束)（SQL 接受集合与程序枚举核对）。
- 结论：已覆盖
- 归属：K1 枚举与规格检查（contracts 计划）。
- 生产入口：scripts/check-event-transitions.mjs
- 测试：apps/camctl/tests/integration/contracts/test_enums.py
- 证据：根检查器核对事件转换与 SQL 约束；入口拒绝布尔/字符串冒充编号由 contracts 计划（test_json_values）与各守卫测试覆盖。
- 未核验前提：无

## 八、独立历史与报告重建（验收 52–68、H-01–H-06、P-01–P-06）

历史查询的生产入口为 `apps/camctl/src/camctl/history/queries.py` 与 `apps/camctl/src/camctl/persistence/repositories/history.py`；报告生成为 `apps/camctl/src/camctl/reporting/generation.py` 与 `apps/camctl/src/camctl/reporting/encoding.py`。

#### 验收 52
- 原文：[验收 52](database/consistency-verification.md#独立历史与报告重建)（交付关联保持与固定边界报告字节）。
- 结论：已覆盖
- 归属：H 同边界查询与 R 报告链（history、reporting 计划）。
- 生产入口：apps/camctl/src/camctl/history/queries.py
- 测试：apps/camctl/tests/integration/history/test_queries.py
- 证据：history 计划分段验证记录；I5 报告链固定字节对照（集成计划 I4 验证记录）。
- 未核验前提：无

#### 验收 53
- 原文：[验收 53](database/consistency-verification.md#独立历史与报告重建)（交付关联的四类历史状态与非法关联）。
- 结论：已覆盖
- 归属：H5 独立文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划 H5 收口验证记录（引用类与候选类恢复）。
- 未核验前提：无

#### 验收 54
- 原文：[验收 54](database/consistency-verification.md#独立历史与报告重建)（取回完整失败列表取自全部相关交付）。
- 结论：已覆盖
- 归属：H 历史查询（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/queries.py
- 测试：apps/camctl/tests/integration/history/test_queries.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 55
- 原文：[验收 55](database/consistency-verification.md#独立历史与报告重建)（仅交付变化时的报告目录与父对象补齐）。
- 结论：已覆盖
- 归属：H 报告范围（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/changes.py
- 测试：apps/camctl/tests/integration/history/test_report_scope.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 56
- 原文：[验收 56](database/consistency-verification.md#独立历史与报告重建)（对象关联、计数、业务序号及快照维护核对）。
- 结论：已覆盖
- 归属：H 报告范围（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/changes.py
- 测试：apps/camctl/tests/integration/history/test_report_scope.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 57
- 原文：[验收 57](database/consistency-verification.md#独立历史与报告重建)（缓存不得仅凭计数复用旧失败列表）。
- 结论：已覆盖
- 归属：R 生成缓存（reporting 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/generation.py
- 测试：apps/camctl/tests/integration/reporting/test_generation.py
- 证据：reporting 计划分段验证记录（重建与缓存群）。
- 未核验前提：无

#### 验收 58
- 原文：[验收 58](database/consistency-verification.md#独立历史与报告重建)（设备文件独立历史与身份保持）。
- 结论：已覆盖
- 归属：H5 文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划 H5 收口验证记录。
- 未核验前提：无

#### 验收 59
- 原文：[验收 59](database/consistency-verification.md#独立历史与报告重建)（源文件与目标中间文件分别独立历史）。
- 结论：已覆盖
- 归属：H5 文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划 H5 收口验证记录（七种 FileHistoryKind）。
- 未核验前提：无

#### 验收 60
- 原文：[验收 60](database/consistency-verification.md#独立历史与报告重建)（文件关联的分类与共同保存中断）。
- 结论：已覆盖
- 归属：H5 文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划 H5 收口验证记录（恢复拒绝与完整事务群）。
- 未核验前提：无

#### 验收 61
- 原文：[验收 61](database/consistency-verification.md#独立历史与报告重建)（SHA-256 分类与源端校验值组合）。
- 结论：已覆盖
- 归属：X 校验与 H 文件历史（outputs、history 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/copy.py
- 测试：apps/camctl/tests/integration/outputs/test_copy_complete.py
- 证据：outputs 计划分段验证记录（摘要校验群）。
- 未核验前提：无

#### 验收 62
- 原文：[验收 62](database/consistency-verification.md#独立历史与报告重建)（仅文件取得摘要时的目录登记与类型隔离）。
- 结论：已覆盖
- 归属：H 报告目录（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/changes.py
- 测试：apps/camctl/tests/integration/history/test_changes.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 63
- 原文：[验收 63](database/consistency-verification.md#独立历史与报告重建)（文件报告依赖表的四类路径）。
- 结论：已覆盖
- 归属：R 报告依赖（reporting 计划）。
- 生产入口：docs/camctl/database/report-dependencies.json
- 测试：apps/camctl/tests/integration/reporting/test_encoding.py
- 证据：reporting 计划分段验证记录；依赖表由根检查器与生成测试共同核对。
- 未核验前提：无

#### 验收 64
- 原文：[验收 64](database/consistency-verification.md#独立历史与报告重建)（文件事实与关联建立的先后关系）。
- 结论：已覆盖
- 归属：H 报告目录（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/changes.py
- 测试：apps/camctl/tests/integration/history/test_report_scope.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 65
- 原文：[验收 65](database/consistency-verification.md#独立历史与报告重建)（文件后续观察不批量改写关联交付）。
- 结论：已覆盖
- 归属：H 文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 66
- 原文：[验收 66](database/consistency-verification.md#独立历史与报告重建)（目录与投影共同保存及非法组合拒绝）。
- 结论：已覆盖
- 归属：H 事件校验（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/events.py
- 测试：apps/camctl/tests/integration/history/test_events.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

#### 验收 67
- 原文：[验收 67](database/consistency-verification.md#独立历史与报告重建)（三种同步方式的入选集合、顺序和字节一致）。
- 结论：已覆盖
- 归属：R 编码（reporting 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/encoding.py
- 测试：apps/camctl/tests/integration/reporting/test_encoding.py
- 证据：reporting 计划分段验证记录（同步方式群）。
- 未核验前提：无

#### 验收 68
- 原文：[验收 68](database/consistency-verification.md#独立历史与报告重建)（带游标分页与分批处理的额外读取成本）。
- 结论：已覆盖
- 归属：R 分页、H7 规模测量（reporting 计划、history 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/encoding.py
- 测试：apps/camctl/tests/integration/reporting/test_encoding.py、apps/camctl/tests/integration/history/test_complete_history.py
- 证据：分页等价（对象集合、顺序、字节不变）已覆盖（reporting 计划分段验证记录）；带统计信息的代表性样本上分页选择与候选扫描命中实际索引、无临时排序，页大小 100 与 7 取得相同升序集合，恢复批次 128 与 3 结果一致（history 计划 H7 验证记录，Windows 开发机）。
- 未核验前提：无

#### 验收 H-01
- 原文：[验收 H-01](database/consistency-verification.md#快照成员与历史文件集合)（逐表自身记录归属）。
- 结论：已覆盖
- 归属：H6 快照成员（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/snapshots.py
- 测试：apps/camctl/tests/integration/history/test_snapshots.py
- 证据：history 计划分段验证记录（六类对象落快照与独立恢复比对）。
- 未核验前提：无

#### 验收 H-02
- 原文：[验收 H-02](database/consistency-verification.md#快照成员与历史文件集合)（同一事件多行只计一次与漏行识别）。
- 结论：已覆盖
- 归属：H6 快照成员（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/snapshots.py
- 测试：apps/camctl/tests/integration/history/test_snapshots.py
- 证据：history 计划分段验证记录（以独立回放行集合识别漏行、重行）。
- 未核验前提：无

#### 验收 H-03
- 原文：[验收 H-03](database/consistency-verification.md#快照成员与历史文件集合)（未知来源到确认来源的完整历史）。
- 结论：已覆盖
- 归属：H5 文件历史（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/queries.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划 H5 收口验证记录。
- 未核验前提：无

#### 验收 H-04
- 原文：[验收 H-04](database/consistency-verification.md#快照成员与历史文件集合)（批次上限 1、2 与默认值的多页查询）。
- 结论：已覆盖
- 归属：H4 固定 H 分批（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划分段验证记录（空有效页不结束扫描、继续位置取最后候选）。
- 未核验前提：无

#### 验收 H-05
- 原文：[验收 H-05](database/consistency-verification.md#快照成员与历史文件集合)（读取连接间交错分页与提交）。
- 结论：已覆盖
- 归属：H4 分批查询（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_file_history.py
- 证据：history 计划分段验证记录（真实交错 5/8/3 批次群）。
- 未核验前提：无

#### 验收 H-06
- 原文：[验收 H-06](database/consistency-verification.md#快照成员与历史文件集合)（五类冻结边界的三路径恢复与报告字节）。
- 结论：已覆盖
- 归属：H6 快照维护（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/snapshots.py
- 测试：apps/camctl/tests/integration/history/test_snapshots.py
- 证据：history 计划 H6 收口验证记录。
- 未核验前提：无

#### 验收 P-01
- 原文：[验收 P-01](database/consistency-verification.md#报告对象分页的具体查询验证)（类型登记与容量边界的续读切换）。
- 结论：已覆盖
- 归属：R 编码分页（reporting 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/encoding.py
- 测试：apps/camctl/tests/integration/reporting/test_encoding.py
- 证据：reporting 计划分段验证记录（有界与续读路径切换群）。
- 未核验前提：无

#### 验收 P-02
- 原文：[验收 P-02](database/consistency-verification.md#报告对象分页的具体查询验证)（各页无重复无遗漏按升序，无 OFFSET）。
- 结论：已覆盖
- 归属：R 编码分页（reporting 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/encoding.py
- 测试：apps/camctl/tests/integration/reporting/test_encoding.py
- 证据：reporting 计划分段验证记录（分页群）。
- 未核验前提：无

#### 验收 P-03
- 原文：[验收 P-03](database/consistency-verification.md#报告对象分页的具体查询验证)（真实首批与后续游标查询的覆盖索引）。
- 结论：已覆盖
- 归属：P6 索引成本与 H7 规模测量（persistence 计划、history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/persistence/test_cache_queries.py、apps/camctl/tests/integration/history/test_complete_history.py
- 证据：persistence 计划 P6 收口验证记录（EXPLAIN QUERY PLAN 断言五张体量表无 SCAN）；ANALYZE 后的代表性样本上首批与续读经 `report_changes_by_entity` 覆盖索引检索、无临时排序（history 计划 H7 验证记录，Windows 开发机）。
- 未核验前提：无

#### 验收 P-04
- 原文：[验收 P-04](database/consistency-verification.md#报告对象分页的具体查询验证)（数据形态组合下的扫描增长）。
- 结论：已覆盖
- 归属：H7 规模测量（history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/history/test_complete_history.py
- 证据：均匀（对象与目录行等量）、高重复（少数对象大量行）与稀疏（窗口外）三种形态混合样本上分页读完整、升序无重复，页大小 100 与 7 取得相同集合，高重复对象只贡献一次身份，计划不退化为全表扫描（history 计划 H7 验证记录，Windows 开发机）。
- 未核验前提：无

#### 验收 P-05
- 原文：[验收 P-05](database/consistency-verification.md#报告对象分页的具体查询验证)（分页期间提交新事件的冻结范围不变）。
- 结论：已覆盖
- 归属：R 生成（reporting 计划）。
- 生产入口：apps/camctl/src/camctl/reporting/generation.py
- 测试：apps/camctl/tests/integration/reporting/test_generation.py
- 证据：reporting 计划分段验证记录（冻结范围与重建群）；I5 报告链固定字节。
- 未核验前提：无

#### 验收 P-06
- 原文：[验收 P-06](database/consistency-verification.md#报告对象分页的具体查询验证)（候选峰值与完整选择工作量）。
- 结论：已覆盖
- 归属：P5 批分离与 H7 规模测量（persistence 计划、history 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py
- 测试：apps/camctl/tests/integration/persistence/test_repositories.py、apps/camctl/tests/integration/history/test_complete_history.py
- 证据：批分离与不可变批次已覆盖（persistence 计划 P5 收口验证记录）；代表性样本上候选扫描每批不超过固定批量、分页取得的候选集合与独立全量选择一致、半数晚于边界的候选被窗口过滤（history 计划 H7 验证记录，Windows 开发机）。
- 未核验前提：无

## 九、跨模块契约场景

[跨模块契约检查](verification.md#跨模块契约检查)的十项组合场景在此映射到实际生产入口、测试与验证证据。场景条目独立于上文验收条目编号；每条包含六项：规则、结论、生产入口、测试、证据、未核验前提，结论分类与上文一致。单个模块测试通过不代表整条业务链组合成立，本节只登记已有组合证据的边界。

#### 场景 配置变化后继续工作
- 规则：[本地配置变化与未完成工作](../architecture/configuration.md#本地配置变化与未完成工作)、[设备绑定异常的影响范围](../architecture/configuration.md#设备绑定异常的影响范围)
- 结论：部分覆盖
- 生产入口：apps/camctl/src/camctl/bootstrap/config.py、apps/camctl/src/camctl/session/service.py
- 测试：apps/camctl/tests/unit/bootstrap/test_configuration.py、apps/camctl/tests/integration/bootstrap/test_initialization.py、apps/camctl/tests/integration/outputs/test_work_files.py、apps/camctl/tests/integration/session/test_session.py
- 证据：配置加载、部分覆盖组合校验与非法值拒绝由单元配置用例覆盖；绑定异常拒绝初始化且保持原绑定；工作文件清理按剩余额度与已用次数推进；会话每轮重新驱动流程。各模块计划的分段验证记录。
- 未核验前提：同一状态库上配置变化前后连续运行、拍摄/取回/清理/取消核对原绑定且历史次数与终态保持的组合剧本；调用结束晚于流程结束。归路线图阶段 3 收口行与阶段 4 组合行。

#### 场景 延时摄影等待与完成
- 规则：[设备自行结束时的等待与完成核实](../architecture/camera-capture.md#设备自行结束时的等待与完成核实)、[相机验证](../architecture/camera-verification.md)
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/capture/handlers.py、apps/camctl/src/camctl/persistence/repositories/timelapse.py
- 测试：apps/camctl/tests/integration/bootstrap/test_timelapse_finish.py、apps/camctl/tests/integration/capture/test_result_confirmation.py、apps/camctl/tests/integration/capture/test_timelapse_wait_runtime.py
- 证据：RC4 的真实 run 装配覆盖单文件、多文件、合法空列举但必需类型缺失、未写完文件、集合尚未齐备、列举错误、轮数耗尽及跨运行预算上调／下调；分别核对完整结果、已知失败或未确认结论、产物登记与占用收场。`bootstrap/test_timelapse_finish.py` 对原子结论保存、同键重入和等待配置重送亦有测试；`capture/test_result_confirmation.py` 覆盖文件归属、结果结论与原子结束，结果与等待分区见 RC4 的 2026-10-11 记录。
- 未核验前提：无

#### 场景 普通交付移入 ready 后，结果保存前中断
- 规则：[普通交付的保存顺序与中断恢复](../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)、[产物验证](../architecture/output-verification.md)
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/outputs/handoff.py、apps/camctl/src/camctl/outputs/copy.py
- 测试：apps/camctl/tests/integration/outputs/test_delivery.py、apps/camctl/tests/integration/host_files/test_handoff.py
- 证据：交付恢复决策表全部分区（意图保存未知、目录同步失败、主机先领取后保存、move 未落重新观察、发布恢复延续身份、取消后观察仍保存、既有 ready 不覆盖、已领取文件撤回不改写）与真实目录原子移动用例通过（outputs 与 host_files 计划分段验证记录）。
- 未核验前提：无

#### 场景 来源结束后，取回与清理同时具备条件
- 规则：[延后执行时的取回与清理顺序](../architecture/outputs.md#延后执行时的取回与清理顺序)、[取回读取保护与清理](database/consistency-verification.md#取回读取保护与清理)
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/outputs/qualification.py、apps/camctl/src/camctl/outputs/cleanup_flow.py
- 测试：apps/camctl/tests/integration/outputs/test_qualification.py、apps/camctl/tests/integration/outputs/test_cleanup_guards.py
- 证据：不同产物独立到期、内部与交付准备不竞争相机槽、既有读取保护保持原拷贝、清理限制拒绝并记录、删除进行中专用错误、跨设备候选不阻塞及清理守卫成员终态分区均通过（outputs 计划分段验证记录）。
- 未核验前提：无

#### 场景 取消动作自身被取消
- 规则：[取消阶段与目标范围](../architecture/task-cancellation.md#取消动作自身被取消)、[取消计划](../superpowers/plans/2026-09-30-camctl-cancellation.md)
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/cancellation/service.py、apps/camctl/src/camctl/cancellation/targets.py
- 测试：apps/camctl/tests/integration/cancellation/test_controller_cancel.py、apps/camctl/tests/integration/cancellation/test_targets.py
- 证据：后一次取消不扩大范围直接包含原目标、完整目标集合包含自身时动作失败且无任何取消项、取消动作自身成功后原目标继续、中断边界幂等重入及原键恢复不产生部分取消均通过（cancellation 计划分段验证记录）。
- 未核验前提：无

#### 场景 取消后的完整副本与录像处理中间文件
- 规则：[取回中间文件的保留与清理](../architecture/obtaining-outputs.md#取回中间文件的保留与清理)、[内部中间文件的保留与清理](../architecture/camera-recovery.md#内部中间文件的保留与清理)、[中间文件清理的运行预算](../architecture/file-handoff.md#中间文件清理的运行预算)
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/outputs/copy.py、apps/camctl/src/camctl/outputs/work_files.py
- 测试：apps/camctl/tests/integration/outputs/test_copy_resume.py、apps/camctl/tests/integration/outputs/test_work_files.py、apps/camctl/tests/integration/capture/test_media_processing.py
- 证据：拷贝续传中断矩阵（未确认尾截断、同步失败不推进、目标缺失保持事实、设备源身份变化拒绝）、历史扫描固定上界与跨轮游标、单轮绕回、删除失败保留责任及录像处理门槛三时点与决定提交前后中断均通过（outputs 与 capture 计划分段验证记录）。
- 未核验前提：无

#### 场景 日志错误触发副本交付
- 规则：[日志适配与写入完成通知](logging-runtime.md#日志适配与写入完成通知)、[日志交付](../architecture/log-delivery.md)、[故障标记](../architecture/log-failure-marker.md)
- 结论：部分覆盖
- 生产入口：apps/camctl/src/camctl/logging_runtime/copies.py、apps/camctl/src/camctl/session/service.py
- 测试：apps/camctl/tests/unit/logging_runtime/test_copies.py、apps/camctl/tests/integration/logging_runtime/test_copies.py、apps/camctl/tests/integration/session/test_session.py、tests/integration/test_camctl_report_roundtrip.py
- 证据：副本包含触发错误与轮换竞争字节、标记创建失败先于复制、发布失败清理暂存、失败交付不重试、状态库不可用不阻塞交付（组件层与跨组件两处）及会话内触发链均通过（logging_runtime、session 计划与集成计划 I 系列验证记录）。现有进度写入失败测试确认仍继续发布，但没有验证 `MarkerSave.error` 进入诊断；保存错误诊断与本次临时文件清理、生产启动清理接线仍未覆盖完整。
- 未核验前提：日志副本与标记失败的会话诊断、每次失败保存对本次临时文件的清理，以及默认 `run` 对旧日志副本和标记临时文件的启动扫描接线分别由[SW-02、SW-03、SW-04](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)跟踪。副本自身条件满足时继续交付的现有行为保持已验证结论。

#### 场景 业务事实保存后生成历史报告
- 规则：[独立历史与报告重建](database/consistency-verification.md#独立历史与报告重建)、[报告验证](../architecture/report-acceptance.md)
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/persistence/repositories/history.py、apps/camctl/src/camctl/reporting/generation.py
- 测试：apps/camctl/tests/integration/history/test_complete_history.py、apps/camctl/tests/integration/reporting/test_generation.py
- 证据：综合剧本全部对象按初始回放、快照正向与当前投影逆向三路径恢复并与独立事件推导映像逐行核对；读取批次与恢复方向变化不改结果；同一报告重建字节不变。见 history 计划 H7 验证记录与 reporting 计划分段记录。
- 未核验前提：无

#### 场景 公共协议跨组件读写
- 规则：[客户端适配计划](../superpowers/plans/2026-09-30-report-client-adaptation.md)、[跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md)
- 结论：已覆盖
- 生产入口：protocol/schemas
- 测试：tests/integration/test_camctl_cancellation_roundtrip.py、tests/integration/test_camctl_capture_roundtrip.py、tests/integration/test_camctl_media_roundtrip.py、tests/integration/test_camctl_output_roundtrip.py、tests/integration/test_camctl_report_roundtrip.py、tests/integration/test_camctl_session_recovery.py、apps/client/tests/integration
- 证据：能力导出、计划生成、run/submit 受理、报告生成、导入及 ACK 消费同一协议样例；精确 ID、请求复用、时间、关联与部分结果用例在 Windows 全量通过（集成计划 I4/I5 验证记录）。
- 未核验前提：无

#### 场景 camctl 退出后仍有工具进程
- 规则：[接入模块的本地进程收场责任](../host-demo/design.md#接入模块的本地进程收场责任)、[I2 保留退出记录、分批检查原组与最终回收](../superpowers/plans/2026-09-30-camctl-integration.md#i2-保留退出记录分批检查原组与最终回收)
- 结论：已覆盖
- 生产入口：apps/host-demo/src/process.c、apps/host-demo/src/process_group.c、apps/host-demo/src/host.c
- 测试：tests/integration/test_camctl_process_recovery.py、apps/host-demo/tests/integration/test_process.c、apps/host-demo/tests/unit/test_process_group.c、apps/host-demo/tests/unit/test_process_unit.c
- 证据：I1/I2 的 2026-10-08 Linux 验证记录：真实 C 模块与生产 CLI、工具启动组合验证保留退出记录、分批核验、组终止、线程实际停止、最终回收及下一调用；扫描未知和提前回收均保留管理责任。8 项 O6 全部通过。
- 未核验前提：无

## 十、电机控制与主程序通知增量

本表映射[电机动作](../architecture/motor-control.md)、[通知协议](../../protocol/host-notifications.md)和[回调注册](../host-demo/implementation.md#按消息类型注册回调)的软件行为，不改变前述数据库验收编号。实施归属为 M1—M6、HN1—HN6 和路线图的客户端接入项；实际命令、环境和结果由[电机验证记录](verification.md#电机控制与单向通知验证2026-10-08)保存。

| 契约范围 | 生产入口 | 可证伪测试入口 |
| --- | --- | --- |
| 精确位置、窗口和共同机器格式 | `protocol/schemas/host-notification.schema.json`、`camctl.acceptance`、`shared/protocol-validation.ts` | Python `contracts/test_motor_schema.py`、客户端 `motor-control.test.ts`、C `test_notification.c` 共用原数夹具；覆盖数学整数、边界、长尾精度和非法参数。 |
| 唯一意图与原事务核实 | `camctl.persistence.repositories.motor`、`camctl.motor.service` | `persistence/test_motor_transactions.py`、`unit/motor/test_service.py`：原请求／键及完整历史边界一致，核实只读且不重授许可。 |
| 取消与最后资格检查 | `camctl.cancellation`、`camctl.motor.rules`、`camctl.bootstrap.motor_assembly` | `cancellation/test_motor_cancel.py`、`unit/motor/test_decisions.py`、根 `test_camctl_motor_recovery.py`：四种寻址、可靠未发送原子取消、未知意图拒绝撤回、窗口两端和时钟失信。 |
| 单次写入与描述符所有权 | `camctl.motor.notification`、`camctl.cli` | `unit/motor/test_notification.py`、`integration/motor/test_notification_pipe.py`：完整／失败／短写分区、后续通道停用、真实工具及报告进程不继承写端、提前退出关闭。 |
| host 接收、背压、回调及交付 | `apps/host-demo/src/notification.c`、`callback_queue.c`、`host.c` | host 单元及 `test_notifications.c`、`test_delivery.py`：解析边界、真正满队列时继续收集标准流与回收 PID、共享 FIFO、安装接口与源码包。 |
| 不重发与用户可见结果 | Python 电机流程、C 公共接口、客户端报告导入 | 根 `test_camctl_motor_recovery.py`、`test_camctl_motor_notifications.py`：进程及提交边界故障、host 自动重启、客户端真实导出／导入／ACK、慢回调与相机共存。 |
| 历史、报告及发行物 | `camctl.history`、`camctl.reporting`、客户端 `server/database.ts` | `integration/motor/test_history_reports.py`、`bootstrap/test_distribution.py`、客户端 `motor-exact-json.test.ts`：同 H 正逆恢复、无副作用、仓库外 wheel、失败输入原数、派生重建与回滚。 |

Python 组件测试路径相对 `apps/camctl/tests/`；前两行未标出分类的 Python 测试属于 `integration/`。客户端测试分别位于 `apps/client/tests/unit/` 和 `integration/`，host 测试位于 `apps/host-demo/tests/`，根测试位于 `tests/integration/`。这些软件验证不覆盖 TX2、电机实际执行或物理断电，具体部署输入见[部署交接](verification.md#部署交接与待核验项)。

## ARM 目标运行与真实相机验收范围（截至 2026-10-13）

本节补充软件映射之外的 ARM 安装和真实相机证据，不改变前述 165 条软件验收及十项跨模块场景的结论统计。目标 Python 3.11 发行物已在 ARM64 上安装并用于以下两项已完成的相机演示；SQLite 专项能力检查、其他设备场景和部署方验收分别保留其自身边界。

| 验收范围 | 当前结论 | 证据与未覆盖范围 |
| --- | --- | --- |
| ARM64 上的 camctl 安装与运行 | Action6 普通录像和 Hasselblad 单张拍摄均已在目标 ARM64 环境运行。 | 现场记录的解释器为 CPython 3.11.17，SQLite 为 3.53.1。Hasselblad 使用独立虚拟环境、独立数据目录和已安装发行物；Action6 使用 ARM 更新后的 camctl 与 C host。SQLite 版本与工作流证据见[V-02](#验收-v-02)、[V-04](#验收-v-04)。目标运行库的完整 WAL 多连接、读视图、提交和历史恢复专项仍未执行，不能把这一项未完成扩大解释为 ARM 安装或两项现场工作流未完成。 |
| Action6 普通录像 | 2026-10-11 的 ARM 演示按用户现场确认验收通过。 | 用户确认演示正常，并明确回传客户端生成取回计划和成功完成独立取回。原始计划、正式报告、媒体文件和确认水位没有归档；不补写媒体属性、摘要、实测码率或逐项响应。详见[Action6 普通录像现场记录](verification.md#action6-普通录像演示的现场验收2026-10-11)。 |
| Hasselblad CFV 100C 单张拍摄 | 2026-10-12 在 ARM64 上完成真实 RAW 拍摄、主机原图与配对 JPEG 预览登记、取回及报告生成。 | 新库以格式 2 初始化；格式 1 的既有业务保持兼容，不隐式升级，格式 1 新增 Hasselblad 拍摄仍受拒绝规则保护。正式源文件保留在主机；删除两份交付副本后再次取回，产生新的独立交付且未再次拍摄。取回文件的领取和删除由验收脚本模拟，不代表第三方传输；原 RAW 未纳入归档或解码，相机端是否同时保留原片仍未确认。本次没有物理断电。详见[Hasselblad ARM 验收记录](verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)。 |
| Action6 原生延时摄影 | 尚未完成真实设备验收。 | 当前没有已核实的设备停止接口和完整结束、文件写完及产物集合证据；诊断和驱动状态见[原生延时记录](verification.md#action6-的-windows-原生延时采样2026-10-10)及接入计划。 |
| OSMO 360 II 完整接入 | 已完成 10/18 完成设计所列 TX2 正式业务验收场景。 | 在原账户、配置及状态库上完成安装和正式 describe；Auto EV 0 与 Manual ISO 800 普通录像、Auto 1 秒／300 秒原生延时、30 秒／300 秒低功耗恢复及有限取消均通过各自规定的业务链。有限取消的目标 action 为 `CANCELED`、cancel action 为 `FAILED/cancel_items_failed`，原 STOP 失败保留。仅 Auto 1 秒／300 秒样片没有实际间隔独立读回；省略 `-o` 的工具默认响应上限仍未知。详见[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)及[TX2 验证记录](verification.md#tx2-正式业务验收2026-10-18-起)。Action6 仍在固件修复门槛下暂停，不能从 OSMO 结果推及 Action6。 |
| 第三方接入、目标资源与物理断电 | 尚无通过证据。 | Action6 演示使用 ARM64 上构建安装的 C host；ARM64 上尚未运行 host-demo 组件 CTest，也没有实际第三方主程序或传输程序接入证据。Hasselblad 验收中的领取由脚本模拟。本次没有电机实测、目标资源测量或物理断电验收。部署方输入及通过条件见[目标联调记录](verification.md#部署交接与待核验项)。 |

## 执行记录

- 2026-10-08（Linux x86_64，CPython 3.11.16）：I1/I2 与真实 O6 组合验证通过，验收 32、33、R-09、R-10、R-11 及遗留工具契约场景升级为已覆盖；34 增补 C 收场期间 pending 与预算的证据，R-12 分别登记 C 软件证据和部署联调前提。具体命令、环境和范围见[接入模块验证记录](../host-demo/verification.md)及[跨组件计划](../superpowers/plans/2026-09-30-camctl-integration.md#i1i2-验证记录2026-10-08linux-x86_64)。

- 2026-10-08（Windows 开发机，uv CPython 3.11）：新增[九、跨模块契约场景](#九跨模块契约场景)十项映射并扩展 `tests/integration/test_camctl_acceptance_map.py` 检查器（场景清单解析自 verification.md 表格、字段与引用逐项校验、条目解析不再跨章节读取）；验收 68、P-03、P-04、P-06 依据 history 计划 H7 规模测量记录升级为已覆盖。`tests/integration/test_camctl_acceptance_map.py` 3 项全部通过。
- 2026-10-08（Windows 开发机，uv CPython 3.11）：`tests/integration/test_camctl_acceptance_map.py` 全部通过；引用的模块测试文件与生产入口路径逐一核验存在。此前的最近全量回归见[集成计划验证记录](../superpowers/plans/2026-09-30-camctl-integration.md#i4-验证记录2026-10-07)（Windows 单元 3353、根跨组件 52+4 跳+342 子测试、bootstrap 87、session 82；WSL 单元 3352+1 跳、reporting 345 等）。
- 2026-10-13（验收映射与当前 ARM 证据核对）：逐条复核 165 项的权威编号、结论、生产入口、测试入口和未核验前提，并复核十项跨模块场景；全部入口路径均存在。2026-10-12 的最新全量软件回归记录为单元测试 `5076 passed, 1 skipped`、16 个集成目录合计 `6209 passed, 3 skipped`；之后另有 capture 完整目录 `1088 passed`、主机照片专项 `19 passed`、收场恢复专项 `93 passed` 及安装版专项 `1 passed`（本次审阅未运行测试）。现场结果见[Hasselblad 软件及 ARM 验收](verification.md#hasselblad-主机单张接入的软件验证2026-10-12)和[ARM 真实拍摄与取回验收](verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)，以及[Action6 普通录像现场确认](verification.md#action6-普通录像演示的现场验收2026-10-11)。Action6 普通录像和 Hasselblad 单张已通过各自记录的现场范围；Action6 原生延时、OSMO、V-02/V-04 所列的目标 SQLite 专项、第三方传输、目标资源及物理断电仍分别按上表和对应责任入口跟踪。
- 本映射覆盖[数据库一致性验收](database/consistency-verification.md)全部165条（数字条目72条、字母条目93条）与[跨模块契约检查](verification.md#跨模块契约检查)十项场景。截至2026-10-15，验收条目已覆盖 142 条、部分覆盖 23 条、开放 0 条；契约场景已覆盖8项、部分覆盖2项。SW-06 在真实默认消费者、SQL 授予及恢复中补齐 W-11 的逐项软件证据，因此将该项升级为已覆盖；其余条目的结论保持。各部分覆盖条目的未核验前提仍由各自映射及路线图跟踪。上述统计描述软件验收映射，目标设备工作流、目标性能及物理断电分别记录。
