# 第一版软件验收映射

[数据库一致性与历史恢复验收](database/consistency-verification.md)定义的全部条目在此逐条映射到实际生产入口、测试与验证证据。本页是[跨组件集成计划 I6](../superpowers/plans/2026-09-30-camctl-integration.md#i6-全量契约映射软件验收与部署交接)的实施档案；映射的结构与引用由 `tests/integration/test_camctl_acceptance_map.py` 检查。一行映射不等于行为通过：行为证据以所列用例的实际通过记录为准，尚未核验的前提逐条列出并归属到模块任务或路线图行，不折叠为空白。

## 条目格式与结论分类

每条包含六项：原文、结论、归属（所属模块任务）、生产入口、测试、证据、未核验前提。结论分三类：

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
- 测试：apps/camctl/tests/integration/scheduling/test_resources.py
- 证据：资格、窗口与既定顺序的单元及集成判定已覆盖（scheduling 计划分段验证记录）；交错提交竞争组合未逐项注入。
- 未核验前提：候选取消、释放与授予的交错竞争矩阵，归路线图阶段 6 排序竞争行。

#### 验收 W-06
- 原文：[验收 W-06](database/consistency-verification.md#等待与来源结束)（归属变化中断与三路径恢复一致）。
- 结论：部分覆盖
- 归属：Q4 授予与恢复（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：恢复等价与迟到观察不覆盖新归属已覆盖（scheduling 计划分段验证记录）；中断逐点注入未建立。
- 未核验前提：归属变化各边界中断注入，归路线图阶段 3 中断行。

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
- 测试：apps/camctl/tests/integration/scheduling/test_recovery.py
- 证据：恢复先核实原责任、不重复分配已覆盖（scheduling 计划分段验证记录，17 项恢复用例）。
- 未核验前提：意图提交、派发、结果保存前后的逐点中断注入，归路线图阶段 3 中断行。

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
- 测试：apps/camctl/tests/integration/scheduling/test_resources.py
- 证据：持有者推导、交错授予与矛盾报错已覆盖（scheduling 计划分段验证记录）；仓储层查询计划已由持久化 P6 收口核对，调度侧未完成工作查询计划未单独核对。
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
- 测试：apps/camctl/tests/integration/scheduling/test_window_expiration.py
- 证据：窗口观察、窗口外未派发过期与窗口守卫已接入生产路径（scheduling 计划分段验证记录）。
- 未核验前提：窗口内发出、窗口后确认的在途确认子项，归路线图阶段 3 窗口行剩余。

#### 验收 W-19
- 原文：[验收 W-19](database/consistency-verification.md#等待与来源结束)（意图提交后派发前跨过窗口）。
- 结论：部分覆盖
- 归属：Q 窗口与派发（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_window_expiration.py
- 证据：窗口外未派发事实与过期收场已覆盖；派发前后逐点中断未建立。
- 未核验前提：中断注入与原尝试未知子项，归路线图阶段 3 窗口行剩余与中断行。

#### 验收 W-20
- 原文：[验收 W-20](database/consistency-verification.md#等待与来源结束)（调用截止与启动窗口分别触发；迟到成功不改写终态）。
- 结论：部分覆盖
- 归属：Q 窗口与 O 尝试（scheduling、operations 计划）。
- 生产入口：apps/camctl/src/camctl/persistence/repositories/scheduling.py
- 测试：apps/camctl/tests/integration/scheduling/test_window_expiration.py
- 证据：迟到结果不改写终态已覆盖（capture、operations 计划分段验证记录）。
- 未核验前提：截止与窗口分别触发的组合矩阵与三路径中断对照，归路线图阶段 3 窗口行剩余与中断行。

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
- 结论：部分覆盖
- 归属：Q3 工作发现（scheduling 计划）。
- 生产入口：apps/camctl/src/camctl/outputs/dispatch.py
- 测试：apps/camctl/tests/integration/scheduling/test_discovery.py
- 证据：候选发现、排序与重启恢复已覆盖（scheduling 计划分段验证记录）。
- 未核验前提：最优候选位于后续页、详情缓存淘汰与分批扫描，归路线图阶段 3 窗口行的分页与缓存淘汰子项。

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
- 测试：apps/camctl/tests/integration/outputs/test_device_work.py
- 证据：业务顺序资格判定已覆盖（outputs 计划分段验证记录）。
- 未核验前提：先取回后清理、先清理后取回及同时间的竞争交换，归路线图阶段 6 排序竞争行。

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
- 测试：apps/camctl/tests/integration/outputs/test_read_recovery.py
- 证据：恢复保留原身份、次数与关联已覆盖（outputs 计划分段验证记录）。
- 未核验前提：建档与准备完成各边界中断注入，归路线图阶段 6 中断行。

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
- 测试：apps/camctl/tests/integration/outputs/test_cleanup_recovery.py
- 证据：通知丢失后的启动恢复接续已覆盖（outputs 计划分段验证记录）。
- 未核验前提：源依赖解除各边界中断注入，归路线图阶段 6 中断行。

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

## 七、路径定位、运行库与编号（F-01–F-08、V-01–V-04、J-01–J-06、E-01–E-02）

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
- 结论：部分覆盖
- 归属：B4 目录切换开放项（bootstrap 计划；允许切换分支已交付）。
- 生产入口：apps/camctl/src/camctl/persistence/initialization.py、apps/camctl/src/camctl/persistence/directory_switch.py
- 测试：apps/camctl/tests/integration/bootstrap/test_directory_switch.py
- 证据：B4 开放项交付验证记录（责任全部结束且原目录只剩空目录树时 SWITCHED 并三路径共同保存；数据库身份与全部业务历史保持；原目录原样保留、新目录准备后重复 init 按已有库验证；原配置恢复正常处理；相同、互相包含、跨文件系统与 Windows 大小写别名目录拒绝；来回切换各保存一整套绑定）。
- 未核验前提：目标 Linux 环境的符号链接真实对象核对（Windows 开发环境不可创建）与主程序采用新交接路径后的恢复领取联调，归 B7 目标部署复验。

#### 验收 F-08
- 原文：[验收 F-08](database/consistency-verification.md#中间文件的路径与定位)（切换各边界中断与三路径共同更新）。
- 结论：部分覆盖
- 归属：B4 目录切换开放项（bootstrap 计划；中断矩阵主体已交付）。
- 生产入口：apps/camctl/src/camctl/persistence/initialization.py、apps/camctl/src/camctl/persistence/directory_switch.py
- 测试：apps/camctl/tests/integration/bootstrap/test_directory_switch.py、apps/camctl/tests/unit/persistence/test_directory_switch.py
- 证据：B4 开放项交付验证记录（提交注入失败回滚后重读判定 not_completed 且库值保持整套旧绑定、混合组合判 inconsistent 不混用新旧目录；提交成功后重读核实三列整体为新值；绑定保存失败时新空目录保留不构成切换成功；准备前各检查中止保持原绑定；切换保留数据库身份与全部表内容）。
- 未核验前提：带真实报告历史、累计 ACK 与文件 ID 的库切换保留验证及切换后历史回放、进程在提交后返回前被终止的字面中断模拟，归 I 跨组件复验场景群（路线图 376-377 行）。

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
- 证据：写连接 PRAGMA 核验（WAL/FULL）、STRICT/JSON 由结构 SQL 与事务测试覆盖、增量 BLOB 分段读写由快照维护覆盖（persistence、history 计划分段验证记录；检查器执行全部规格 SQL）。
- 未核验前提：目标部署环境的实际 Python 路径与构建身份记录，归 V-04/B7。

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
- 归属：真实 ARM64 硬件的构建与运行差异复验，见[部署交接与待核验项](verification.md#部署交接与待核验项)。
- 生产入口：apps/camctl/src/camctl/persistence/runtime.py
- 测试：apps/camctl/tests/integration/bootstrap/test_distribution.py、apps/camctl/tests/integration/persistence/test_runtime.py
- 证据：B7 已按部署验证裁决在 WSL x86 合规环境（cpython-3.11.17/SQLite 3.53.1）完成源码目录之外的发行物构建、按锁文件安装与 init/describe/submit/设备替身 run 验证，发行物自包含权威资源且只声明运行时依赖；Windows 开发机同样通过（[B7 验证记录](../superpowers/plans/2026-09-30-camctl-bootstrap.md#b7-验证记录2026-10-08)）。
- 未核验前提：真实 ARM64 硬件上的发行物构建（或安装）与运行差异复验，按部署交接表在目标硬件执行，不以 x86 通过代替。

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
- 结论：已覆盖
- 归属：H2 历史归属（history 计划）。
- 生产入口：apps/camctl/src/camctl/history/changes.py
- 测试：apps/camctl/tests/integration/history/test_changes.py
- 证据：history 计划分段验证记录。
- 未核验前提：无

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

[跨模块契约检查](verification.md#跨模块契约检查)的十项组合场景在此映射到实际生产入口、测试与验证证据。场景条目独立于上文验收条目编号；每条包含五项：规则、结论、生产入口、测试、证据、未核验前提，结论分类与上文一致。单个模块测试通过不代表整条业务链组合成立，本节只登记已有组合证据的边界。

#### 场景 配置变化后继续工作
- 规则：[本地配置变化与未完成工作](../architecture/configuration.md#本地配置变化与未完成工作)、[设备绑定异常的影响范围](../architecture/configuration.md#设备绑定异常的影响范围)
- 结论：部分覆盖
- 生产入口：apps/camctl/src/camctl/bootstrap/config.py、apps/camctl/src/camctl/session/service.py
- 测试：apps/camctl/tests/unit/bootstrap/test_configuration.py、apps/camctl/tests/integration/bootstrap/test_initialization.py、apps/camctl/tests/integration/outputs/test_work_files.py、apps/camctl/tests/integration/session/test_session.py
- 证据：配置加载、部分覆盖组合校验与非法值拒绝由单元配置用例覆盖；绑定异常拒绝初始化且保持原绑定；工作文件清理按剩余额度与已用次数推进；会话每轮重新驱动流程。各模块计划的分段验证记录。
- 未核验前提：同一状态库上配置变化前后连续运行、拍摄/取回/清理/取消核对原绑定且历史次数与终态保持的组合剧本；调用结束晚于流程结束。归路线图阶段 3 收口行与阶段 4 组合行。

#### 场景 延时摄影等待与完成
- 规则：[设备自行结束时的等待与完成核实](../architecture/camera-capture.md#设备自行结束时的等待与完成核实)、[相机验证](../architecture/camera-verification.md)
- 结论：部分覆盖
- 生产入口：apps/camctl/src/camctl/capture/handlers.py、apps/camctl/src/camctl/persistence/repositories/timelapse.py
- 测试：apps/camctl/tests/integration/capture/test_result_confirmation.py
- 证据：发送锚点保存、跨检查轮恢复、时间与产物双依据、不满足保存已知失败保持占用、未确认标记等分区均有失败用例并纳入回归（capture 计划分段验证记录）。
- 未核验前提：单文件、多文件、合法空结果、集合未齐、读取错误、核实耗尽及跨运行配置变化的完整结果核实矩阵，归路线图阶段 4 结果核实行。

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
- 结论：已覆盖
- 生产入口：apps/camctl/src/camctl/logging_runtime/copies.py、apps/camctl/src/camctl/session/service.py
- 测试：apps/camctl/tests/integration/logging_runtime/test_copies.py、apps/camctl/tests/integration/session/test_session.py、tests/integration/test_camctl_report_roundtrip.py
- 证据：副本包含触发错误与轮换竞争字节、标记创建失败先于复制、发布失败清理暂存、失败交付不重试、状态库不可用不阻塞交付（组件层与跨组件两处）及会话内触发链均通过（logging_runtime、session 计划与集成计划 I 系列验证记录）。
- 未核验前提：无

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

## 执行记录

- 2026-10-08（Linux x86_64，CPython 3.11.16）：I1/I2 与真实 O6 组合验证通过，验收 32、33、R-09、R-10、R-11 及遗留工具契约场景升级为已覆盖；34 增补 C 收场期间 pending 与预算的证据，R-12 分别登记 C 软件证据和部署联调前提。具体命令、环境和范围见[接入模块验证记录](../host-demo/verification.md)及[跨组件计划](../superpowers/plans/2026-09-30-camctl-integration.md#i1i2-验证记录2026-10-08linux-x86_64)。

- 2026-10-08（Windows 开发机，uv CPython 3.11）：新增[九、跨模块契约场景](#九跨模块契约场景)十项映射并扩展 `tests/integration/test_camctl_acceptance_map.py` 检查器（场景清单解析自 verification.md 表格、字段与引用逐项校验、条目解析不再跨章节读取）；验收 68、P-03、P-04、P-06 依据 history 计划 H7 规模测量记录升级为已覆盖。`tests/integration/test_camctl_acceptance_map.py` 3 项全部通过。
- 2026-10-08（Windows 开发机，uv CPython 3.11）：`tests/integration/test_camctl_acceptance_map.py` 全部通过；引用的模块测试文件与生产入口路径逐一核验存在。此前的最近全量回归见[集成计划验证记录](../superpowers/plans/2026-09-30-camctl-integration.md#i4-验证记录2026-10-07)（Windows 单元 3353、根跨组件 52+4 跳+342 子测试、bootstrap 87、session 82；WSL 单元 3352+1 跳、reporting 345 等）。
- 本映射覆盖[数据库一致性验收](database/consistency-verification.md)全部 163 条（数字条目 72 条、字母条目 91 条）与[跨模块契约检查](verification.md#跨模块契约检查)十项场景。截至本记录：验收条目已覆盖 140 条，部分覆盖 23 条，开放 0 条；契约场景已覆盖 8 项，部分覆盖 2 项。开放与部分覆盖条目的未核验前提均归属到模块任务或路线图行（B4 两项开放功能已交付：目录切换允许分支与运行库 `configuration_error` 分类，F-06/V-03 升级为已覆盖，F-07/F-08 剩余前提为目标 Linux 符号链接对象、主程序恢复领取联调与带真实报告历史的切换保留验证），不作为行为通过的依据。
