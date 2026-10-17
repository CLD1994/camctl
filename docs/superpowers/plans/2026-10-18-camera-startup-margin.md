# 相机启动余量实施计划

> **执行方式：** 使用 `superpowers:subagent-driven-development` 按任务执行。用户已授权计划完成后直接实施，并使用持续在线的 OSMO 360 II 做实际验证。

**目标：** 两款相机的新普通录像及适用原生延时采用独立的 1000 毫秒启动余量，OSMO 新延时请求最小间隔为 2 秒，并保持既有任务和历史可恢复。

**架构：** 型号参数由设备参数定义派生到 Schema、能力和安装资源。启动余量作为首次受理的执行定义，与可靠命令确认语义一起保存；正常计时、恢复、自然等待和持久化守卫共用精确计算。目标时长、实际设备开始和实际媒体时长分别表达。

**技术栈：** Python 3.11、现有 SQLite 事件与投影、JSON Schema、TypeScript 客户端、C host，以及 TX2 上的正式安装物和 ADB 驱动。

**规格：** [相机启动余量设计](../specs/2026-10-18-camera-startup-margin-design.md)。本页中的内部模块划分、签名和提交拆分是依据当前源码提出的实施建议；调整时须记录原因并保持规格契约。

## 全局约束

- 新适用任务保存 `startup_margin_ms = 1000`；缺字段表示旧规则，不在读取、恢复或重放时补默认值。新普通录像的 SENT 与余量须共同冻结，半套或非法定义拒绝。
- 普通录像新 START 的完整正常响应仅证明可靠命令接受，保存 `sent_at`，不生成实际 `started_at`。取消可在余量期间立即 STOP，设备占用和停止预算不刷新。
- 普通录像正常 STOP 不早于 `A + S + D`；有限原生延时自然核实不早于 `A + S + D + R + E`。用户目标、设备时间负载和媒体修复目标仍为原值。
- `S` 冻结；未完成等待采用新配置 `E` 时按原锚点更新预计时点并保存实际采用值。已经完成的等待不重新打开。可靠 STOP 后保留原独立文件等待，不叠加 `S`。
- 控制经过时间、录制时间下界和实际媒体时长不能混用。下界未超阈值不能证明实际未超，实际媒体不足不能因余量而通过。
- OSMO 新间隔从 2 秒起；Action6 保留 1 秒。旧 OSMO 1 秒任务通过原生效参数恢复，字节编码不改写。客户端旧草稿保留原值并提示修正。
- 生产实现任务顺序集成；每项在独立工作树修改、测试和提交。只读调查与评审可并行，测试资源按组件指南隔离。
- Action6 只做不访问设备的软件验证。真实相机只由协调者操作 OSMO；保留原片、状态库、任务身份和原确认，不重新初始化，不自动重发未知 START。

## 评审重点

1. 旧记录缺字段和新记录只保存一半字段容易混淆：任务 2 的执行定义测试分别验证兼容读取和拒绝。
2. 余量期间取消容易被当成“尚未开始”而释放：任务 2 验证占用保持、立即 STOP 和唯一 START。
3. 未完成等待改变 `E` 与恢复重复加 `S` 容易改变原责任：任务 2 验证更新、完成后复用和只等剩余时间。
4. 下界被写成实际媒体时长会误判过录或不足：任务 2 验证未知保持、实际短片失败和原裁剪目标。
5. 源码参数已变而安装资源或旧任务恢复未变：任务 1、3 验证真实客户端、安装版和旧负载，而不是另维护枚举。

## 任务关系与文件责任

顺序为任务 1 → 任务 2 → 任务 3 → 任务 4。任务 2 中的冻结定义、普通录像和自然等待属于同一核心交付，按三段提交审阅；两款工厂在消费者闭合后才启用新定义。任务 1 拥有时间参数、参数生成脚本和相关样例；任务 2 拥有任务工厂、执行与持久化；任务 3 拥有跨组件安装链。共享能力说明由定义的拥有者修改，后续任务通过已集成基线消费。

### Task 1：按型号派生延时时间参数

**预计文件：**

- 修改 `apps/camctl/src/camctl/devices/drivers/adb_cameras/timelapse_parameters.py`、`definitions.py`、`catalog.py`。
- 修改 `apps/camctl/scripts/render_timelapse_time_parameters.py`、受其资源格式影响的 `render_action6_diagnostic_candidate.py` 和 `docs/hardware/action6_record_probe.py`。
- 生成 `docs/hardware/timelapse-time-parameters.json` 和 `docs/hardware/camera-control-handoff.md` 的参数区；修改 OSMO 延时示例。
- 测试 `apps/camctl/tests/unit/devices/test_adb_camera_parameters.py`、`test_adb_camera_commands.py`，`apps/camctl/tests/integration/bootstrap/test_adb_camera_dispatch.py`，`apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py`，`tests/integration/test_timelapse_time_parameters.py` 和 `docs/hardware/tests` 的诊断测试。

**接口建议：** `timelapse_intervals_s(model: str) -> tuple[int, ...]`、`time_properties(model: str) -> dict`、`time_conditions(model: str) -> list[dict]` 由一处权威型号定义派生，未知型号明确拒绝。参数模块不反向导入 `commands.py`；调用者可以传现有字符串枚举值。时长域共享，组合数由关系计算，不手工保存第二份允许值。生成 JSON 按型号提供视图，所有读取者同步改用型号条目。

- [ ] **步骤 1：写失败测试。** 新 OSMO 输入 1 秒拒绝、2 秒接受；Action6 1 秒保留；OSMO 合法时间组合 126、Action6 134；小数、布尔、超域及 `duration <= interval` 拒绝。新请求拒绝 1 秒的同时，已受理旧 OSMO 1 秒恢复仍生成原时间字节；客户端旧输入不被改成 2 秒。
- [ ] **步骤 2：运行目标测试确认失败。** 使用 `.venv311/bin/python -m pytest` 和工作树自身的 `PYTHONPATH=apps/camctl/src`；组件集成与根集成分开运行。记录失败原因，不能以资源未同步替代目标契约的失败。
- [ ] **步骤 3：实施参数派生和消费。** 更新真实 Schema、能力说明、生成资源、诊断读取及 OSMO 样例。客户端已有按 Schema 校验和保留无效值的行为能满足时，补行为验证即可。
- [ ] **步骤 4：运行门禁。** 上述单元文件、bootstrap 恢复测试和 `tests/integration/test_timelapse_time_parameters.py` 通过；两个参数／诊断生成器的 `--check` 通过。验证当前源码和安装参数视图同源。
- [ ] **步骤 5：审查完整 diff 后提交。** 不包含任务工厂启动语义或等待算法变更，交付提交、日志和剩余接口说明。

### Task 2：闭合启动余量的执行、恢复和证据

**预计文件：**

- 新建 `apps/camctl/src/camctl/devices/capture_timing.py`；修改 `devices/tasks.py`、`capture/models.py`、`capture/recording.py`、`capture/timelapse.py`、`capture/handlers.py`。
- 修改 `bootstrap/capture_assembly.py`、两款 ADB 相机任务工厂及响应解释器。
- 修改 `devices/capture_completion.py`、`persistence/repositories/capture.py`、`finite_cancel.py` 及其历史／重放消费者。
- 测试 `apps/camctl/tests/unit/capture/test_execution_specs.py`、`test_recording_start.py`、`test_recording_finish.py`、`test_timelapse.py`，以及新的纯时序单元测试。
- 集成测试覆盖 `capture/test_recording_reconcile.py`、`test_recording_start_runtime.py`、`test_recording_start_save_boundary.py`、`test_native_completion_context.py`、`test_timelapse_wait_runtime.py`、`test_finite_cancel.py` 和各自持久化／历史入口。

**接口建议：** 纯模块 `CaptureTiming(duration_ms: int, startup_margin_ms: int, result_margin_ms: int = 0, extra_wait_ms: int = 0)`，提供 `deadline_us(anchor_us: int) -> int`、`deadline_ns(anchor_ns: int) -> int` 和 `recording_lower_bound_ns(control_elapsed_ns: int) -> int`。精确计算使用项目既有整数边界；旧定义在明确分支传入零余量。字段验证和纯计算分别承担结构与时间职责；不引入新依赖。

**跨任务接口：** 本任务消费任务 1 已生效的型号 Schema。产出两款新普通录像 `StartReturn.SENT`、冻结 `startup_margin_ms`、保留未知 `started_at` 和统一等待依据；任务 3 从真实受理定义与事件读取这些事实，不复制生产公式构造预期。

- [ ] **步骤 1：写定义与精确算术失败测试。** 全旧普通录像仍为原 STARTED 语义；新 SENT+1000 成对接受；半套、布尔、零、负数、分数毫秒拒绝。原生延时旧定义不补 S，新定义合法。安全整数边界在设备调用前拒绝无法表示的请求，未派发 START。
- [ ] **步骤 2：实施纯时序与冻结读取，运行单元目标。** 新普通录像活动能力不得再按动作类型固定 STARTED。保存、历史、重放、提交未知采用同一冻结定义，完整正常响应与未知结果分开处理。先提交可兼容读取的基础，不提前启用工厂。
- [ ] **步骤 3：写普通录像失败测试。** 目标 10 秒在 ACK 后 10 秒不 STOP、11 秒可 STOP，响应接收锚点不被事务耗时重置；只写 `sent_at`。ACK 不可靠无新锚点。余量内取消立即 STOP、保留占用与原预算。跨会话保留原确认，只等剩余，不跨单调钟比较。
- [ ] **步骤 4：实施录像正常、恢复与媒体消费。** 控制原始经过时间与录制下界分开，下界截断到零但不写成实际媒体时长；不能用“下界未超门槛”推出实际未超。实际媒体短于 D 仍失败，轻微正常多录不新增强制拷贝，异常裁剪目标保持 D。运行相关单元及 capture 录像集成后提交。
- [ ] **步骤 5：写延时和独立守卫失败测试。** 同一可靠 SENT 下 330 秒拒绝自然核实、331 秒允许；设备收到原 300 秒。正常完成上下文、结果页、历史及有限取消均比对原 S。错活动、错 S、未来页、未实际等待、重复加 S 拒绝。未完成等待改变 E 后按原 A/S/D/R 保存新预计时点；已完成等待不重开。
- [ ] **步骤 6：实施自然等待与有限取消闭合。** 可靠 STOP 后仍只用原 STOP 文件等待；STOP 失败的有限取消只尝试一次，等待原自然时点，保留失败 attempt，目标 canceled、取消 failed，仅结束活动和释放占用。恢复不新建 START 或更改身份；最后才启用两款工厂的 SENT+1000 和适用原生延时余量。
- [ ] **步骤 7：运行核心门禁并提交。** unit 全量；组件 capture、cancellation、session、persistence、history、devices、host_files 按责任目录分别运行，bootstrap 先跑受影响文件；保留新定义、旧定义和反例的实际日志。审查上下文／守卫／重放使用相同 S 和来源后提交完整核心。

#### 自然等待的时钟映射与保存状态

本节细化步骤 5—7 的实现边界。截止时点仍为 `Q = A + (S + D + R + E) × 1000`，单位为微秒。正常、有限取消和恢复共用同一活动的等待状态，不能分别创建另一份锚点或安排责任。

建议使用不可变 `WaitClockReference(utc_us: int, monotonic_ns: int)`，提供纯计算 `logical_deadline_ns(expected_check_at_us: int) -> int`。每个原活动在一个会话内只建立一次参考对 `(U, M)`：本会话可靠 ACK 使用原响应 UTC 和单调钟；恢复缺少本会话 ACK 时使用同次可信 UTC／单调钟读数。候选逻辑截止为 `L = M + (Q - U) × 1000`。之后改变 `E` 或墙钟读数不改变参考对，也不从当前时刻或可变截止反推 ACK。

逻辑截止 `L` 使用内存中的精确有符号整数，可以为负或早于当前单调钟；实际剩余等待才取 `max(0, L - now_monotonic_ns)`。持久化 `Q` 和各时长仍分别检查原整数边界；不能把内存中过去的逻辑截止套用到要求非负的持久字段或 `CaptureTiming.deadline_ns`，也不能因已到期而丢弃原映射。`plan_capture_wait` 的已到期分支也须保留逻辑截止。时间不可信时沿原降级处理，不建立替代锚点。

映射、候选安排、可靠生效的安排、未决保存申请分别表达。映射不依赖安排写入结果，可以保留；生效缓存必须在可靠提交或原申请核实成功后更新。安排或完成保存结果未知时，持有原操作键和完整申请，先结束未决事务并核实该键的保存事件及完整持久输入。当前仓储入口为 `persistence/repositories/timelapse.py`：`ScheduleWaitCommand` 须补原键复用核验；`WaitCompletedCommand._reuse` 的既有核验可用于同一完成申请。底层 `saved_transaction_events(connection, key)` 必须在合法事务内读取，不能用同连接中尚未提交的投影代替可靠结果。不同动作、活动、分支、时刻或等待规则的同键申请应拒绝。

| 当前状态 | 保存与缓存 | 后续推进 |
| --- | --- | --- |
| 新 ACK，尚无安排 | 保留原响应参考对，按原 A/S/D/R 与当前 E 计算候选 Q/L；保存首次安排 | 可靠保存后才按 L 等待或完成，不把事务耗时作为新起点。 |
| 恢复，原 Q 在未来、等于现在或已过去 | 只建立一次恢复参考对，三类都保留有符号 L | 截止已过只提供到期资格，不自动生成完成事实。 |
| 未完成，E 增大、减小或不变 | 从原参考对重新计算 Q/L；变动先保存新 Q/E，未变不新写事件 | 增减可跨过当前时点；可靠保存后才使用新安排判断到期。 |
| 安排保存可靠回滚 | 保留映射，生效缓存不变，候选不成为已采用事实 | 下次可按当前 E 重新计算，不在原截止上累计偏移。 |
| 安排保存 UNKNOWN | 保留原键和申请；原键核实成功后发布原安排，核实未提交后才放弃候选或原样重送 | 未决期间不改 E、不完成等待、不读设备；原安排确认后，若最新 E 不同，再用新申请重配。 |
| 完成保存可靠回滚 | 尚无完成事实，保留原映射 | 下次重新采用当前 E；E 增大时可以恢复等待。 |
| 完成保存 UNKNOWN | 先核实原完成键；已提交则复用原事件；可靠确认未提交后才重新判断 E | 不先重配或创建另一份完成申请；重送必须沿原键和原完整输入。 |
| 已可靠保存完成，随后 E 改变 | 保留原 Q/E、完成事件及原结果页来源 | 不重配、不重开，正常和有限取消均复用该依据。 |
| 缺少可信时间、可靠 ACK 或完整历史 | 保留原未知及必要占用 | 继续原有限收场，不能以当前时刻补成可靠依据。 |

验收矩阵须同时覆盖旧 `S=0`、新 `S=1000`、E 连续增减及墙钟前后跳，检查保存行、缓存、事件次数和零新增设备调用。新增反例包括：`A=NOW、S=1000ms、D=1000ms、R=200ms`，恢复读数为 `(A+3s, 10s)`，完成保存回滚后 E 从 0 改为 2000ms，正确逻辑截止为 11.2s；再覆盖负逻辑截止、回滚后减 E、安排及完成 UNKNOWN 在提交前／后的原键核实、同键改变输入拒绝，以及完成后修改 E 不重开。主机 STOP、有限取消和自然结果消费者须一起审计，避免把只用于等待的映射当作实际采集开始或媒体时长。

### Task 3：安装版客户端与 C host 联合链

**预计文件：** `tests/integration/test_osmo_builtin_timelapse_roundtrip.py`、`tests/integration/_osmo_builtin_clock_entry.py`、必要的同源 ADB 替身和 `tests/integration/README.md`；沿用真实客户端、C host、安装版 CLI、SQLite、Files 导入和 ACK。

**接口：** 消费任务 2 的真实受理定义、响应与等待事件；受控时钟只加速等待，不替换执行、持久化或媒体交付。ADB 替身遵守实际完整响应合同，合成媒体不作为实机证据。

- [ ] **步骤 1：写失败断言。** 源码外安装版与源码能力一致，新 OSMO 样例 2 秒、1 秒新请求拒绝；普通录像原目标 10、冻结余量 1000、语义 SENT、实际开始未知，正常 STOP 至少经过 11 秒；原生延时负载仍 300、自然核实至少 331 秒。
- [ ] **步骤 2：更新真实联合链及替身。** 每个拍摄唯一 START；普通一次 STOP、自然延时零 STOP；STOP 后 30 秒原文件等待独立。保留原任务、原片／可选预览各自取回、真实客户端 Files 导入和累计 ACK；受控时钟不使结果事件越过尚未完成的等待。
- [ ] **步骤 3：运行安装链与回归。** 顺序运行本文件、`test_timelapse_time_parameters.py`、既有 `test_camctl_c_module_roundtrip.py`、`test_real_camera_demo_roundtrip.py` 和 `test_action6_builtin_demo_roundtrip.py`。运行源码／安装资源和协议检查；组件 bootstrap 全量在冻结组合快照上单独运行。
- [ ] **步骤 4：提交并交独立整体评审。** 对任务 1—3 全部变更检查规格符合性和代码质量；只对新修改或未决问题追加必要测试，不重复已通过的同一范围。

### Task 4：TX2 部署、360 定点补测与记录

**负责者：** 协调者独占设备。消费已通过软件门禁的 wheel 和原 ARM C host；以新验收目录记录本次提交、命令、时间、输出、退出码和媒体摘要。

- [ ] **步骤 1：只读前置核对并备份。** 核实 USB 型号、固件、boot ID、供电／存储可用性、原状态库完整性、原任务终态和占用；保存原配置及数据库备份。核对 Python 3.11、SQLite 和现有安装 provenance，不运行 init。
- [ ] **步骤 2：安装冻结快照。** 构建 wheel，核对本地／TX2 SHA-256，再离线替换包，不改变锁定依赖。核对安装字节、正式 describe 和 OSMO 2 秒能力；C 未改时复用已核实的 ARM 安装物。
- [ ] **步骤 3：普通录像补测。** Auto 和 Manual 各一轮目标 10 秒；从正式客户端和 C host 派发，核实原目标、SENT、未知实际开始、1000 毫秒余量、ACK 到正常 STOP 至少 11 秒和独立 STOP 文件等待。独立取回并全流检查必要双视频、音频和实际时长；实际短片不宣告通过。
- [ ] **步骤 4：最短间隔补测。** OSMO Auto 2 秒／300 秒自然延时，唯一 START、零 STOP，设备负载 300 秒，自然结果核实至少 331 秒；保留完整目录差集、产物及逐流事实，不由预计帧数补造参数回读。
- [ ] **步骤 5：恢复与有限取消补测。** 30 秒／300 秒分别验证低功耗期间受管进程中断后同任务恢复，以及离线 STOP 失败的有限取消。保留 A/S/D/R、采用的 E、任务／attempt 身份和唯一 START；核实剩余等待、取消双终态和占用释放，任何结果未知先对账原任务。
- [ ] **步骤 6：交付与记录。** 各必要原片及实际存在的可选预览分别取回、源／副本摘要比对、全流解码、真实 Files 导入和累计 ACK。更新 `docs/camctl/verification.md`、本页和原后续修订入口，明确软件／安装／实机边界。运行文档链接、变更锚点和 diff 检查后提交，报告工作区状态。

## 执行状态

计划已按规格逐节自查：参数入口、冻结定义、控制响应、正常与取消、恢复、媒体、自然上下文、有限取消、历史、安装物和实机均有对应任务。已核对跨任务共享接口；具体测试、提交和现场事实在执行后补入本节及组件验证记录。
