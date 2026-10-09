# 文件读取的运行配置与原尝试恢复

本计划闭合普通取回和录像内部输入两个读取入口。每份设备文件采用本次运行的读取上限、无数据阈值及重拷上限；源身份、已用次数、可靠进度和原尝试保持。计划属于[运行配置执行闭合](2026-10-09-camctl-runtime-configuration-closure.md)的 RC3 子任务，进度回到[分模块审查进度](2026-09-30-camctl-implementation-roadmap.md#分模块审查进度)。

## 已确定的契约与责任边界

读取尝试表示取得同一文件内容的责任，可以跨主机重启及摘要不一致后的重拷继续。设备读取会话表示本次打开的连接及本地资源；会话关闭不自动结束整个读取尝试。新读取错误重试才增加读取次数，重拷只增加独立重拷次数。普通取回的责任属于其独立交付；异常原片检查和修复输入属于原录像处理，不创建普通交付。

权威规则为 [file-copy.md](../../architecture/file-copy.md) 的无数据超时、按文件累计读取尝试及有限重拷，[configuration.md](../../architecture/configuration.md#本地配置变化与未完成工作) 的配置生效范围，以及 [operation-fields.md](../../camctl/database/operation-fields.md#正常返回恢复与历史保存) 的原尝试恢复。项目测试和事务规则见根 `AGENTS.md` 与 `apps/camctl/tests/AGENTS.md`。配置合法性已经由 `bootstrap/config.py` 负责，本任务不复制其完整字段清单或修改该文件。

| 本次输入 | 合法值与实际用途 |
| --- | --- |
| `devices.<id>.copy.max_read_attempts` | 正整数，默认 3；对需要新增尝试的原责任，以本次上限减原已用次数决定剩余额度，不修改已用次数。 |
| `devices.<id>.copy.read_idle_timeout_s` | 精确有限正秒数，默认 10；只计量实际等待源文件新内容的连续无数据时长。持续收到内容不因整份文件耗时较长而失败。 |
| `devices.<id>.copy.max_recopies` | 非负整数，默认额外 1 次；仅在保存新的摘要不一致重拷决定时比较，不增加读取次数或撤回已登记轮次。 |
| 本次 `copy.segment_size` | 可靠进度的保存粒度；普通取回与内部输入共同采用，不能把分段大小当作无数据计时或尝试次数。 |
| 原设备绑定与本次驱动声明 | 原设备文件观察者提供设备及驱动；端口必须匹配原绑定，当前配置不能重写原设备身份或历史证据。 |

一次运行内配置固定。后续运行可以采用新值，但已结束的尝试、流程、交付、处理决定及校验结果保持。未结束读取尝试的恢复沿原 ticket 和可靠进度，读取重试等待及新会话的无数据计时重新采用本次配置，不累计断电时长。

本任务不修改报告外部格式、用户计划参数或摘要算法。`work_files`、共享本地文件执行器和其会话接线由工作文件清理分工维护；读取适配器须接纳其执行器与实际任务拥有者，不另建一套拥有者。元数据查询和 RESULTS 分页由相应分工维护。

普通交付的摘要不一致终局由[有限重拷的公开错误](../../architecture/file-copy.md#摘要不一致后的有限重拷)定义，并在公共错误登记中使用 `checksum_mismatch`。错误详情仅记录本次重拷上限及原已用次数，不改变报告结构。此登记由根 Agent 维护；读取实施分工在有效失败测试后完成所属业务收场，不借读取错误预算或交接错误。

## 生产数据流与必须闭合的接缝

普通取回为 `session_obtain_assembly → DeviceReadAssembly → FileCandidate → READ_FILE → DriverReadSessions → RecordingInputCopies → CompletionContext → delivery`。当前 `obtain_flow` 有新尝试事务和结果事务，但默认常量代替设备的读取上限及阈值，拷贝端口的重拷上限也使用默认值。

内部输入为 `session_capture_assembly::_media_flow_with → MediaFlow → obtain_recording_input → RecordingInputCopies`。当前媒体链将 `ticket=None` 的 `DriverReadSessions` 传到实际读取，资格事务只建立零尝试流程；必须补原尝试取得、恢复、新重试及结果保存，不能仅将配置字段加到模型上。

`AttemptTicket` 只含身份，不含配置。读取端口采用 `ReadDriver.open_read(source, offset, ticket, *, idle_timeout_s: Decimal)`；该参数表示本次等待源新内容的无数据阈值，不是整份文件或分段的截止时间。低层 `devices.read_session.open_read` 已接受 `no_data_timeout_s`，驱动须把实际传入值交给该会话。两个正式消费者与受真实接口约束的替身共同采用此端口，原 ticket 保持。不得修改 ticket 的身份或借当前数据库值让驱动猜配置。

`ReadResumeRequest`、`OperationRepository.resume_read` 和正式 `ATTEMPT_RESULT.RESUME_READ`（reason 4）已经负责同一 RUNNING READ 尝试采用本次配置。`OPERATION_CONFIGURED.CONFIGURE`（reason 2）同时允许原未结束流程保存实际采用的 `max_attempts_used`、`timeout_s_json` 和 `retry_interval_s_json`。复用原恢复入口，在同一事务保存原 run 与 RUNNING attempt 各自实际采用的配置；不新增尝试，不覆写已结束尝试，不建立另一个重复恢复接口。原 copy、run、attempt、归属、采用配置及发生时刻共同核实；提交未知按原完整组核实。当前恢复入口只更新 attempt，原键只检查事件类别和时刻，须先以 run 配置和改变原输入的反例证伪，再修复完整责任。

## 读取状态矩阵

下面的“已结束”要求原尝试有正式结束结果；流程业务终态不单独证明实际调用已停止。原实际结果保存失败时保留该结果及原操作键，先查清提交，再继续依赖处理。

| 原责任与本次情况 | 必须执行的行为 |
| --- | --- |
| 尚未取得来源、原处理未来或在排队 | 保持等待，不发调用，不消耗读取或重拷次数。 |
| 原处理取消或终态 | 不新增普通读取及重拷；已有真实读取由实际拥有者停止并确认结束，适用保护和独立收场继续按原契约保存。 |
| 副本已完整核实 | 使用原副本完成本地发布或内部处理，不重复读取、摘要比较或新建尝试。 |
| 原 READ 未终态，尚无尝试，已取得读取资格 | 保存第一个真实尝试和本次配置，提交后以非空 ticket 打开会话，已用从 0 变 1。 |
| 原尝试 RUNNING，原续传资格仍成立，原本地工作已可靠结束 | 沿原 ticket、原轮次和可靠进度打开本次会话；保存本次实际配置采用，不增加次数，不先结束原尝试。 |
| 原尝试 RUNNING，本会话仍有实际读取拥有者 | 跟踪原实际读取；零重复打开，不改变其进行中的阈值，不提前释放机会或源保护。 |
| 原尝试已可靠失败，仍有读取重试资格且本次上限大于已用 | 按本次间隔等待；到时保存新的连续 attempt_no，已用增加 1，从可靠进度继续。 |
| 原尝试已可靠失败，本次上限小于或等于已用 | 零新调用，不退次数；确认实际读取及重试结束后保存读取耗尽的所属业务失败及解除适用机会／源依赖。 |
| 未结束原尝试的已用次数已达到或超过本次上限，但原续传资格仍成立 | 继续该原尝试；新增尝试额度为 0 不禁止已登记尝试续传。 |
| 原 READ 已有最终结果 | 保持其原错误和配置依据；尚未保存的本地业务收场沿原结果完成，不以提高配置重开流程。 |
| 取得完整内容，但摘要不一致且尚可新增重拷 | 同事务保存本次采用上限、原副本无效、重拷已用加 1、新轮次与从零进度；提交成功后才重置文件并继续同一读取尝试。 |
| 取得完整内容，摘要不一致且本次重拷上限小于或等于已用 | 零新轮次；保存摘要不一致的所属业务失败，不借读取剩余额度重拷。 |
| 原重拷决定已提交但该轮未结束，本次重拷上限降低 | 恢复原已登记轮次和进度，不撤回决定或再次计数；只有下一次新增决定才比较新上限。 |
| 读取明确错误发生在重拷轮次中 | 只按读取错误预算判定下一次尝试；原重拷已用和当前轮次保持，不以剩余重拷额度绕过读取耗尽。 |
| 调用或历史身份、进度、次数、结果不可可靠解释 | 按状态库或相应执行前提错误停止依赖处理；不默认 0、成功或已结束。 |

### 绑定异常时的原读取恢复

原读取不再具备续传资格时，按[第一版 ADB 恢复记录](../../camctl/database/operation-fields.md#第一版-adb-未完成调用的恢复记录)判断。读取正常续传分支优先；不能把所有 READ RUNNING 一律结束为 ADB 未知。

| 原结果、绑定与恢复依据 | 必须执行的行为 |
| --- | --- |
| 原尝试已有可靠结果 | 保持原结果；尚需异常源设备的工作使用原局部绑定失败事务。 |
| matched 且原续传资格成立 | 恢复原读取尝试，不创建 UNKNOWN 恢复结果或新尝试。 |
| missing/mismatch，旧本地工作可靠收场，固定 H 包含原意图，原驱动明确声明 read 适用普通前台假设且恢复证据可用 | 以原 ticket 保存 `UNKNOWN`、`result_not_saved`、未知效果、空观察和 `assumed/adb_foreground_recovery/v1/{}`；随后同事务责任收场按原局部绑定失败规则完成。不得保存 FAILED、read_returned、虚构退出信息或新尝试。 |
| 无可靠旧工作边界、无固定 H、原意图晚于 H、没有原驱动声明或没有适用恢复证据 | 保留原尝试、流程、slot、源保护及进度，零新调用，并保留原 run/attempt 与阻止恢复原因的 `RecoveryDiagnostic`。不能当作库损坏、完成或可释放。 |
| 原驱动不适用普通前台假设但有其自己的恢复契约 | 按该契约处理，不借其他驱动或操作的 ADB 声明。 |

恢复输入来自本次会话启动边界、一次固定的 H 和原驱动登记。不得在每轮推进重新选择 H，也不得对本会话新增的未结束意图使用旧进程恢复假设。恢复结果提交未知仍先核实原键，再执行绑定失败或后续读取。

无可用恢复依据的分支仍使 `needs_run` 为真。根会话装配须消费已有恢复诊断，按正式调用边界停止无进展的依赖处理，不无限重扫且不改成 `needs_run: false`；其生产消费与机器错误分类需要沿 CLI 原驱动支持核验，不能由局部返回等待代替会话门禁。

## 无数据计时与真实调用矩阵

| 实际阶段或事件 | 本次阈值的应用 |
| --- | --- |
| 等待第一批源文件字节 | 使用本次阈值开始计时；达到阈值形成明确读取错误。 |
| 取得非空文件内容但尚未完成一段 | 立即重新计时，不等分段落盘或数据库保存。 |
| 只取得日志、控制响应或空读取 | 不重置计时。 |
| 正在落盘、保存进度、摘要或媒体处理 | 不计源无数据时间；下一次等待新内容重新计时。 |
| 同一次运行的会话仍在读取 | 配置固定，不能热改其阈值。 |
| 后续运行恢复原尝试 | 新会话使用新阈值及新单调钟，原 attempt_no 和已用保持；不累计断电时长。 |
| 读取超时或异常与停止／关闭 | 等待本次真实结束再提交明确失败及后续重试决定；等待协程取消不证明资源已关闭。 |

测试替身必须采用真实读取端口形状，并把实际请求的非空原 ticket 与本次阈值交给真实 `ReadSession`。局部模型或数据库字段通过不能代替实际调用证据；无数据计时测试使用可控数据和时钟，不以很长的真实睡眠作为门禁。

## 建议实施顺序与门禁

### RD0 原读取恢复分类

- [ ] 根 Agent 独占确认 `test_output_read_recovery.py` 的两项无边界保护：严格仓储拒绝，消费者保留责任与零新调用，实际 `needs_run` 仍为真。
- [ ] 在同一文件补固定 H／原驱动 read 声明／可靠 host 边界的 missing/mismatch，以及边界、H、声明各自缺失、意图晚于 H 的保护矩阵；同时核 UNKNOWN 完整结果、零新尝试和之后原局部绑定失败。
- [ ] 生产装配传递固定 H、边界及原 registry 恢复查找；诊断使用既有 `RecoveryDiagnostic`，不得只在局部循环中 `continue` 而丢弃原因。
- [ ] 对恢复结果的投影故障及 COMMIT 前后 UNKNOWN 用原键关闭重开核实，保证恢复结果没有可靠提交时不释放保护或执行下一步。

### RD1 两入口的真实配置与 ticket

- [ ] 用纯装配单元门禁验证设备配置的精确值分别进入普通取回 `DeviceReadAssembly` 与内部 `MediaFlow`；空主机源不借设备配置。
- [ ] 以真实 SQLite 和匹配的读取替身，验证普通取回首次和内部输入首次都先保存真实尝试，再将实际 ticket 和本次 idle 阈值传到 `open_read`；两个入口的历史配置和值必须来自对应原设备。
- [ ] 根 Agent 确认红后，再在协调的端口、装配及适配器窄边界接线。不得通过替换实际调用为模型断言绕过端口缺失。

### RD2 原尝试、预算变化及有限重拷

- [ ] 实现两个入口共用的原 READ ticket 恢复／新尝试选择，原 attempt 不能因断电或重拷变成新尝试。
- [ ] 真实第一运行保存 2 次失败后，第二运行上限提高至 5，最多允许新增 3 次；降低至 1，不新增尝试且原已用仍为 2。只对未终态责任采用新配置，已终态保持。
- [ ] 真实第 3 次 RUNNING 断电恢复后，上限改为 1 仍继续原第 3 次；本次新的 idle 阈值确实生效，结束后保存同一原结果身份，不重复计数。
- [ ] 摘要不一致采用 `max_recopies=0/1/2`；新的决定按本次上限比较，读取计数保持。已登记重拷降低上限仍继续，之后新增决定被正确拒绝；读取耗尽和重拷耗尽相互不能替代。
- [ ] 内部检查和修复输入均走这一责任，验证原动作结果、检查／修复依据和 source_dependency 释放；不只跑普通 delivery。

### RD3 失败原子与整体复核

- [ ] 对配置采用、读取意图、结果、重拷决定和失败／保护收场注入投影错误与 COMMIT 前后丢失。确认回滚无部分事实；UNKNOWN 关闭重开按原键核实完整事件组，改变输入须拒绝。
- [ ] 历史回放和固定 H 报告不加载新配置、不开设备、不执行文件副作用，原 bytes、已用、attempt_no 和失败理由保持。
- [ ] 根 Agent 顺序执行 bootstrap、capture、outputs、operations、devices、history 相关目录。每次 pytest 前台独占；不合并集成目录或并行运行。
- [ ] 独立审阅普通取回、内部异常原片检查、内部修复、重拷、取消、断电续传、绑定异常恢复及会话退出接缝，再更新路线图范围。窄装配或某一个入口通过不足以完成本任务。

## 禁止的捷径与未核验事项

禁止改写已结束 attempt、裁剪或退还次数、恢复终态、把整份文件的 60 秒截止当无数据阈值、仅改本地模型不传实际端口、把打开连接算成新 READ 重试、把重拷算成读取错误重试、默认原 RUNNING 已结束、将异常绑定等待变成无诊断无限轮询，或捕获所有 `ConsistencyError` 后跳过。

函数拆分及新增测试文件属于建议；状态矩阵、权威输入与失败／恢复规则是实施门槛。读取分工维护 `devices/ports.py` 的读取端口、`repositories/operations.py` 的原 READ `CONFIGURE` 窄段、`capture/media_flow.py` 的读取责任和 `_media_flow_with` 的相应装配；普通结果复合事务与共享文件执行器分别由其责任分工维护，开始修改相邻段前协调。具体设备联调、性能与物理断电不由本计划的软件门禁替代。

2026-10-09，Linux 容器、Python 3.11.16：本计划已追踪现有生产数据流和正式状态空间。RC2 的逐项绑定 13 项及读取事务 10 项已经独立通过；这些证据不覆盖本计划的读取配置、内部 ticket、原尝试续传或所有恢复分支。后续门禁由根 Agent 独占运行并按实际范围记录。

### 实际 End 与默认工厂交接的后续范围

2026-10-09，root 核实读取输入与装配测试后，内部 VERIFY 重入使用原 `wait_stopped()` 实际返回的 clean `ReadEnd`。测试检查 stopped、实际完整字节及原对象身份，再证明不重开源会话和本地完整内容。普通读取让路与并行用例按原录像 action 关联实际 activity，再提供对应 RESULTS 输入，动作和活动主键不相等。让路边界由实际 STOP、活动 ENDED、占用 RELEASED、读取意图及结果的原历史证明；同一排期下的较早取回计划先于录像内部拷贝，内部用途不取得额外优先级。排序规则仍由[多个候选文件的选择顺序](../../architecture/file-copy.md#多个候选文件的选择顺序)完整定义。

root 独占 `/tmp/camctl-goal-recovery-phase-bootstrap.log` 为 108 passed、57.69s，包含上述普通读取两项、原实际读取结束／摘要／绑定十五项，以及文件、媒体、取消和目录装配分区。内部输入模块另包含在最终 capture 定向 107 项中。两组绿色不证明默认三工厂已经交接所有原 READ 持有物。

`session_capture_assembly` 的 `continuing_read_tickets`、`pending_read_results`、`pending_read_business` 和 `pending_read_ends` 在独立使用工厂时由该工厂拥有；默认装配从同一 `RuntimeDeps` 会话注入。普通、残留和受限入口共享原事实及完整申请，但共享持有集合不自动授予相应入口新的设备或媒体执行资格。首批完整申请保存责任已经接线并通过根 15 项门禁；持续保存失败、raw End 技术校验及其他未完成分区见 RD4。

后续门禁须证明普通入口已实际取得原 End、结果及原业务申请后，后继入口用 fresh Owned 接手原保存责任。原 ticket、完整申请、key、已用次数、轮次、进度和源保护保持；不允许新读取、摘要获取、媒体检查／修复或凭字节数补造 End。业务主动停止仍使用已登记的 UNKNOWN/read_stopped 与 CANCELED 组合；完整实际返回但缺必要源摘要且绑定异常，仍使用实际 SUCCEEDED 与所属 binding failure 组合。原子提交前后、原结果可靠但业务未保存、业务终态及迟到取消须分别建模。仅验证四个字典对象相同不能替代真实消费者验收。

### RD4 默认入口交接原 READ 保存责任

普通 `capture_flow`、残留 `residual_flow` 和受限 `winddown_flow` 都会取得本轮独立连接。它们先保存同一会话已经取得的 READ 事实，再筛选当前业务候选；是否仍有可推进的录像、设备绑定是否匹配、当前驱动是否仍提供媒体能力，不决定原申请是否需要核实。原事实来自实际任务拥有者，持有物只能在同一个 `RuntimeDeps` 会话内交接；新进程没有这些持有物时，仍按原固定 H、host 边界和驱动恢复契约处理。

本阶段的正式规则来自本计划上述原结果优先矩阵及 [file-copy.md 的实际读取结束分区](../../architecture/file-copy.md)。下表分开表达实际源结束、完整结果申请和后续独立业务申请。原 `ReadEnd` 的实际时刻与完整 `AttemptFinish` 首次确定时的 `occurred_at` 各自保留；重送不能重新取钟或把后来的业务取消回填到较早的实际结束时刻。

| 持有物与可靠历史 | 默认入口的原责任 | 后续资格与本阶段边界 |
| --- | --- | --- |
| 没有原 READ 持有物 | 不创建 READ 保存范围，不为了准备阶段新增设备、copy 或 operations 依赖。 | 普通、残留和受限各自按原流程推进。 |
| 实际源任务仍未结束 | 保留原实际拥有者、ticket、slot 和源保护，等待真实结束。 | 不重开 source，不按等待取消制造业务取消。 |
| 实际主动停止且字节不完整，尚待核对业务取消 | 保留原 `PendingStoppedRead`、原 ticket 和原 key；按原可靠取消事实准备结果。 | 业务取消已生效时 UNKNOWN/read_stopped + 原 run CANCELED；没有业务取消时保留原尝试，不补造 SUCCEEDED。 |
| 已持有 clean 完整 End，但完整 Finish 尚未确定，仍缺必要源 SHA | 保留原 End、原设备声明及未取得摘要事实；不能从 C=N 形成成功副本。 | 绑定 missing/mismatch 时按已登记矩阵取得 actual SUCCEEDED + binding FAILED；匹配时是否继续获取输入仍受该入口原执行资格约束。受限入口不新增 source/digest/check/repair。 |
| 已持有 clean 完整 End，必要源摘要已可靠取得或原声明 UNSUPPORTED，技术校验未结束 | 保留原本地续接责任、End 与输入，不先制造完整 Finish。 | 原技术校验与后续媒体检查／修复分开；受限技术校验资格需要独立核定，不以本阶段 prepared-Finish 门禁授予。 |
| 已形成完整 Finish，第一次保存回滚或 COMMIT 未知，尚无可靠 F | 在 fresh Owned 用同一完整 Finish、原 key、原 occurred_at 核实或提交原组；失败仍持有它并停止业务筛选。 | 不以 UNKNOWN 恢复覆盖原实际结果，不新增 source/digest/check/repair；原结果可靠前不提交依赖 child。 |
| 已形成完整 Finish，原 F 已 COMMIT 但响应 UNKNOWN | 先核实同一原组；原 run 和 attempt 的已有终态保持，之后才处理原独立收场责任。 | 不因当前投影已终态或普通 candidate 消失而跳过原 key 核实。 |
| 原 Finish 已可靠保存，独立 slot 释放申请首次保存回滚或 COMMIT 未知 | fresh Owned 重送 `PendingReadBusiness` 的原完整申请与 key；同一原 F 的结果／次数／字节不变。 | 不重新形成 slot 申请，不依赖媒体链是否装配，不新增媒体任务。 |
| 原 Finish 和独立申请均已可靠 COMMIT，响应 UNKNOWN 后业务公开取消并进入终态 | 核实已提交的原 key，保留已经生效的取消和所有既有终态。 | 没有新的业务候选也须消费原保存责任；零新调用、零重写事实和零重新计数。 |
| 完整 child 在新取消生效前已经形成，但其 F 尚不存在或仍未知，原请求与新取消约束冲突 | 停止该分区并保留完整原申请和原 key。 | 本阶段不决定如何退役或重形成申请；不得更换 key、改 canceled 输入或放宽 terminal guard。 |
| 字节完整但没有可靠原 End，或原 ticket／所属申请／历史无法解释 | 按既有恢复资格或诊断保留责任。 | 不把 C=N、MATCHED 或业务终态当成实际 clean End，不选择 RESULTS v2 未决语义。 |

装配涉及两个责任：四个 READ 集合以同一会话为权威来源；默认流程在业务候选筛选前核实已经形成的原申请，不依赖是否构造原 READ runtime 或当前设备端口是否可用。字段名、是否采用统一 holder 类型及具体 helper 属于实现建议，完整输入与原组核实是硬性要求。`media_flow.py`／`outputs.read_attempts.py` 的共同保存责任由读取分工提出候选；`capture_assembly.py`、`lifecycle.py`、`flows.py` 和残留入口的接线由根 Agent 协调，不允许独立覆盖相邻分工。

第一阶段组件门禁使用真实受理、START／STOP、活动结束、文件归属与写完历史，真实 `ReadSession` 返回实际 clean End；由普通默认工厂取得第一个原 ticket 和完整结果。只隔离主会话循环及设备／工具边界，保留 `execute_command` 的三个默认 factory 和实际 flow。COMMIT 故障必须落到原目标事务：提交前应无该原 key 历史，提交后 UNKNOWN 应已有完整原组，之后关闭连接并由另一个默认入口取得 fresh Owned。

- [x] `Finish` COMMIT 前／后 UNKNOWN × 普通／残留／受限入口：同一原 Finish、key、occurred_at 可靠保存；原 ticket、次数、字节、轮次及业务处理状态保持，零新 source/digest/check/repair。普通和受限 ACTIVE 分区在原保存之后、业务候选筛选之前用协作者边界探针截停；不把此截停解释为普通入口以后也不能推进合格工作。
- [x] 原 Finish 已 reliable，slot child COMMIT 前／后 UNKNOWN × 三入口：重送原完整 `SlotRequest`／key，不重复 Finish，不新建 slot 责任，原机会正确释放。
- [x] slot child 已 COMMIT 但响应 UNKNOWN，随后通过公开取消与收场使原动作终态 × 三入口：原 key 仍核实，实际 READ 和业务终态均保持，所有候选已消失也不得跳过保存。COMMIT 前 child 后新取消分区暂不实现。
- [x] 再次保存仍 UNKNOWN／回滚时，不执行候选筛选或其他设备／媒体步骤，原 holder 留待下一轮；故障源在真实 repository 端口，不用字典同一性代替消费者行为。首批六项分别验证原 Finish 核实门与 slot child 保存门，不覆盖全部错误与持有物的交叉组合。
- [x] 根 Agent 独占确认九项有效行为红后实施共同 helper 与默认接线；修正六项提交前夹具后，十五项恢复与六项保存门验收通过。bootstrap、capture 分进程回归，范围见下述记录。

上述前三矩阵共 15 项是首批候选，不覆盖 raw End 的技术校验、未结束实际拥有者、continuing ticket 的零新次数续传、普通主动取消停止及 missingSHA 组合在跨工厂下的全部恢复。后续须沿已批准原状态表补这些独立分区；需要新增入口资格时先给完整证据，不能由 15 项绿色推导 RD4 全部完成。原键核实后 holder 的释放由原保存责任负责；不能因工厂返回 None 或进度投影已经终局就丢弃它。

2026-10-09，Linux 容器、Python 3.11.16：`/tmp/camctl-goal-read-default-consumers-red.log` 的 9 项 COMMIT 后 UNKNOWN 行为红覆盖三个入口的完整 Finish、slot 申请和已取消终态；真实公开前置及实际 ReadEnd 均成立。另 6 项提交前夹具需要在关闭 UNKNOWN 原连接后，用 fresh Owned 判断原 key 是否存在，不能把原连接可见的未提交历史当成可靠 F。真实 COMMIT 命中计数、原连接事务状态及新连接的原 attempt／slot 状态共同验证该故障分区，不删减前置断言。

`resume_prepared_internal_reads` 仅消费完整 `PendingReadResult` 和已形成的 `PendingReadBusiness`，四个集合由默认三工厂共享。三个流程先保存原申请，保存失败按 `StateDbFailure` 停止，不执行业务候选筛选；原 Finish 首次确定的时刻用于其 slot 释放申请，不读取当前墙钟。根 `/tmp/camctl-goal-read-default-consumers-green-1.log` 的上述 9 项通过；关闭 UNKNOWN 原连接再观测可靠 F 后，根 `/tmp/camctl-goal-read-default-consumers-green-2.log` 全部 15 passed、8.40s。6 项提交前分区是修正夹具后的首次验收覆盖，不将原可见性失败计作有效业务红。部署解释器编译和补丁格式检查通过。

`test_read_default_save_gate.py` 的后续 6 项通过最终组合门禁：三个入口各覆盖已提交原 Finish 的完整组查询失败且回滚响应丢失（实际 `OperationRepository` 返回 UNKNOWN），以及原 slot 投影失败并可靠回滚（实际 `OutputsRepository` 返回已确认回滚的保存失败结果）。两者均在 fresh Owned 查询可靠 F，重送原完整 request／key，保留原 holder，原源读取及媒体调用不增加，也不进入候选查询、工厂或业务墙钟。这 6 项没有把两个错误和两种 holder 的全部交叉组合都覆盖；它们分别证伪完整 Finish 核实门和独立 child 保存门的停止责任。取消入口接入、raw End 技术校验及迟到取消冲突不在此生产范围。

2026-10-09，Linux 容器、Python 3.11.16：根 Agent 的 `/tmp/camctl-goal-read-recording-bootstrap-final.log` 为 155 passed、92.60s，包含上述二十一项默认入口反例，以及原读取执行、必要摘要与原 End 绑定、文件前置保存、媒体保存／取消／本地收场、正常录像停止和受限默认装配。独立只读审查核对三个入口的原申请优先、失败停止、slot 原时刻及原键核实；提交前夹具问题经真实关闭重开修正后复验。capture 相关定向门禁为 138 passed，新增活动身份结论恢复与原媒体接线另有 26 passed；全单元为 3687 passed、1 skipped、2 个既有 warning。上述软件证据不证明 raw End 技术校验、continuing ticket、missingSHA 在跨工厂下的完整恢复，不决定晚取消预成 child 的状态语义，也不覆盖取消入口和真实设备验收。
