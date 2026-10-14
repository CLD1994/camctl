# SW-04：日志文件责任与启动清理实施计划

> 执行方式：由明确指派的实施代理按任务串行执行，Root 负责设计、逐文件上下文审查、完整门禁批准与提交。设计和计划核对完成后，Root 下达同一份实施说明；遇到新的设计缺口时先暂停相关实施，修订本计划后再继续。

**目标：** 每次取得会话锁的 `run` 安排一次旧日志文件检查，同时跟踪本次副本与标记操作的实际结果；必要业务停止可以先推进，文件结果、适用清理及诊断完成后再释放会话锁。

**组织方式：** 日志责任独立于业务数据库身份，复用默认线程池、`Supervisor` 和本地责任组合。副本流程保存已知阶段事实，在统一边界决定一次清理资格；会话分别处理正常早退、真正取消及普通模式转入受限模式。

**技术基础：** Python 3.11、现有 `MarkerStore`、`FileChannel`、文件交接同步核心、`SessionContext` 与 `CombinedLocalWork`。

**规格依据：** [日志交付与失败清理](../../architecture/log-delivery.md)、[标记及临时文件](../../architecture/log-failure-marker.md)、[实际文件完成](../../camctl/file-runtime.md#本地文件操作的完成与故障边界)、[重要日志取消](../../camctl/logging-runtime.md#重要日志等待期间的取消与退出)、[启动顺序](../../architecture/cli-commands.md#run-的启动顺序)、[时钟受限模式](../../architecture/clock-recovery.md)、[报告恢复依据](../../architecture/report-maintenance.md#与会话收尾的协作)。本计划承接[路线图 SW-04](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)、[SW-02](2026-10-13-camctl-sw02-log-copy-diagnostics.md)及[SW-03](2026-10-13-camctl-sw03-marker-temp-cleanup.md)。

## 实施状态与恢复前提

本项软件实现、专项、四目录门禁及逐文件个人／独立复核已完成，基线为 SW-03 的 `bfde76625d48a18af99401757d08a5e436263e3c`。实际环境、最终结果和首轮测试装配错误的复验边界见[验证记录](../../camctl/verification.md#sw-04-日志责任与启动检查验证2026-10-14)。

本页维护本项设计、实施步骤和验收入口；原始输出与执行索引保存在本地证据目录，环境、命令和最终结果由组件验证记录维护。后续修改沿实际生产者与消费者核对，并仅复验受影响范围。

## 范围与不变量

1. 只有取得会话锁的 `run` 安排本次启动检查。检查只处理实际配置的 `staging/logs`，每轮检查一次、每个合格旧对象删除一次。
2. 正式标记的存在性仍是故障持续状态的权威来源。启动检查保留正式标记；报告可靠恢复后的原消费者负责删除它。
3. 本次副本及每次标记临时文件在创建路径前分配归属身份。启动检查排除本次曾分配的文件，即使其句柄已经关闭。
4. 旧操作结束依据使用 `RecoveryBoundary`：正式 CLI 的 `HOST_LOCAL_SETTLED` 可以证明前任本地操作已收场；直接装配的缺省 `UNCONFIRMED` 保留旧对象。
5. 等待协程取消与实际文件操作结束分别判断。已开始的操作交付真实结果，尚未开始的普通操作可撤回；有资格的必要清理仍执行一次。
6. 日志责任不分配业务文件身份，不改变报告水位、报告机会、设备结果、时钟或主退出分类。原日志是否停用仍只由 `WriteResult.disables_file_logging` 判定。
7. 会话锁保留到实际日志责任与必要诊断完成。必要设备或源读取停止先推进；正常结束没有应用层文件完成超时。
8. SW-05 负责通用锁错误分类和各资源独立释放，SW-06 负责有界业务发现。本项不修改这些行为契约，不需要真实相机、ARM 或物理断电。

## 启动资格与会话阶段

预检查保留状态库实际存在性及文件类型，不根据最终错误文案反推启动阶段。

| 启动事实 | 扫描与会话路径 |
| --- | --- |
| 锁前资格检查已发现配置、目录绑定、驱动或运行库前提非法 | 按原入口错误退出，不为日志检查取得会话锁。 |
| 状态库缺失、存在性未知，或目标不是普通文件 | 锁前退出；不建库，不扫描。 |
| 状态库可靠存在且为普通文件，但预检查无法使用 | 同一会话锁内登记一次检查，保留原错误；不重开坏库，不装配普通及报告流程。 |
| 正常预检查通过，锁内打开、恢复或受理失败 | 已登记检查按真实结果在锁内收场，保留原业务错误；锁内重复核对的 `DirectoryBindingError`／`RuntimeLibraryError` 仍保留原 `configuration_error`。 |
| 正常或初始受限会话 | 锁后立即登记检查，数据库和业务推进不等待扫描线程取得位置或完成。 |
| 会话锁没有取得 | 零扫描、零删除。 |
| `init`、`submit`、`describe` | 保持原命令职责，不安排本任务检查。 |

锁内启动回调只登记任务；它即使采用异步接口，也不等待线程池或目录检查。正常早错不会撤回本次检查，锁后错误分类不反向替代锁前资格事实。只有真正取消设置独立的启动停止事实，排队中的扫描撤回，已经开始的枚举或单次删除继续返回实际结果。

会话收场按下表组织。业务与日志拥有者来自同一份组合清单，阶段消费通过排除指定拥有者完成。

| 触发与前提 | 先推进的责任 | 随后推进的责任与结果 |
| --- | --- | --- |
| 正常退出或普通业务错误 | 停止新增普通工作，完成已有业务责任及适用必要停止。 | 完成本次检查和日志收场；保留原结果，再按既有会话协议释放资格。 |
| 真正取消，包括原来允许新增工作的收场等待、有限受限流程或应急停止期间的新取消 | 同步调用本地责任组合的幂等停止入口并停止启动检查的下一普通步骤；已建立的必要业务收场保留同一任务，日志等待者交接同一任务并立即传播。 | 必要停止的实际结果及适用保存先完成，再由同一最终收场路径消费日志及诊断；重复取消不重建任务，最后传播首次取消。 |
| 初始时钟检查进入受限模式 | 沿原受限取消、录像保守收场流程推进。 | 原一次报告机会仍可使用日志责任；最终锁内收场检查及日志，保留时钟结果。 |
| 普通阶段时钟变为不可信，业务本地收场失败 | 停止普通工作，实际消费业务责任及保存失败。 | 按原前提失效规则退出，不进入依赖该前提的受限停止或报告；日志仍独立收场。 |
| 普通阶段时钟变为不可信，业务本地收场成功 | 先消费业务拥有者，暂不等待日志；按原时点释放普通接纳资格，再推进受限取消和必要录像停止。 | 必要停止完成后锁内排空旧日志责任，再恢复既有一次受限报告允许的新日志责任；检查不重启，失效原日志路径及故障暂停不恢复。 |
| 受限停止本身失败 | 保留实际受限错误，不开始依赖失效条件的报告。 | 最终仍消费日志责任及诊断，保留原错误分类。 |

### 会话持有必要业务收场

原有有限受限流程及非时钟致命错误后的应急停止，是会话持有的必要业务责任。它们不属于日志拥有者，也不能只在 `drive()` 周围捕获异常后，另行等待一个可直接越过最终本地消费的停止回调。默认 `winddown_flow` 的短期运行时不在本地责任组合内，保护边界必须覆盖其驱动调用、结果保存和独立连接关闭；仅保护 `_stop_call` 的外层等待不够。

在 `session/service.py` 内用一个私有的必要收场等待器复用以下规则，不新增公共协议或另建设备停止算法。每次合格阶段只创建一个任务；内部把正常结果与真实 `BaseException` 保存为返回事实，保留异常对象，避免把内部 `CancelledError` 与父等待者取消混为一类。会话另行保存本次首次父取消，两项事实互不替代。等待器完成同一任务后交付实际阶段结果，不能立即抛出父取消而丢掉结果及其下一阶段资格。会话持有该任务到结果消费完毕，不把它登记成日志副本责任。

| 阶段及实际前提 | 父等待及收场规则 |
| --- | --- |
| 尚未取得原有受限或应急资格 | 不创建必要阶段，不借取消新建停止资格。 |
| 原有条件允许进入有限受限取消及 winddown 流程 | 持有原整段有限流程，包括原等待、停止调用、实际结果、保存及连接关闭；不装配普通设备工作。 |
| 普通阶段已确认墙钟失信，进入普通转受限交接 | 同一必要任务先实际消费业务本地责任，排除日志拥有者；成功后按原时点释放普通接纳资格，再推进有限受限流程。父取消不会把实际成功改成前提失败，也不能在业务消费结束后漏掉原必要停止。实际业务消费失败仍沿原 clock_invalid 及本地错误出口，不进入依赖该前提的受限流程。 |
| 原有非时钟致命错误及应急回调资格成立 | 持有同一次 `_merge_emergency_stops`；停止、预算和补记仍由原 `EmergencySettlement` 负责。 |
| 必要前提成立、没有待处理父取消，进入既有唯一受限报告机会 | 从建立该次报告任务起，由同一等待器持有完整 `_restricted_once_report`，直到原生成、文件交接／保留、适用保存和独立连接关闭完成。期间父取消不传给 `WorkerSupervisor.generate`，不能仅因父等待取消就触发 `_retire`；实际报告失败、超时或前提失效仍按原报告停止协议处理。不得为已有父取消再建立报告任务。 |
| 必要阶段已建立，但尚未执行或尚未发 STOP，父等待者被取消 | 同步停止普通本地工作与下一启动候选；已建立的必要阶段仍按原目标资格、等待和预算推进一次，不将其当作可撤回的普通拍摄调用。 |
| 必要阶段已发 STOP，父等待者首次或重复取消 | 取消只影响父等待；不取消该必要任务或内部工具任务，不重发 STOP、不重置预算。按原驱动契约消费实际结果并完成适用保存及关闭。 |
| 必要阶段正常结束，父等待者未取消 | 沿原结果及阶段顺序继续；合格受限阶段仍只有原一次报告机会。 |
| 必要阶段正常结束，父等待者已有取消 | 保留实际成功事实，不进入尚未开始的受限报告或普通业务；先完成最终本地及日志收场，再传播首次父取消。 |
| 受限阶段返回 state_db_error／configuration_error，原应急回调资格成立 | 无论此前是否已有父取消，实际结果仍按原条件交给唯一应急必要阶段；完成有限应急停止及适用补记后再进入最终本地及日志收场，不进入受限报告。父取消只决定最终传播，不抹掉致命结果产生的必要责任。 |
| 已建立的一次受限报告返回 state_db_error／configuration_error，原应急回调资格成立 | 保留原错误及报告文件事实，同样按原资格完成必要应急阶段后再最终收场；不因父取消或报告结束重开报告机会。 |
| 必要阶段发生其他原有普通错误 | 保持 `_run_restricted_flows`／`_merge_emergency_stops` 的原分类及诊断；失效前提不继续依赖工作，最终本地及日志责任仍消费。 |
| 必要任务交付内部控制异常，父等待者未取消 | 持有该实际原对象；不伪造正常停止结果，完成最终本地及日志责任后传播。 |
| 必要任务交付内部控制异常，父等待者此前已取消 | 保留首次父取消为传播对象，内部实际异常及已有次因附属交付；最终本地及日志责任仍消费。 |

首次父取消同步调用幂等停止入口，后续取消沿同一等待，不改变已登记的停止事实。多个必要阶段共用本次父取消事实，受限阶段转应急阶段不重置它。阶段实际异常和实际致命结果保留为次因；没有实际致命结果时，不将内部控制异常或父取消本身伪造成应急资格。此保护不改变受限等待上限、应急与普通停止预算、驱动资格、业务取消事实或结果保存前提；既有条件不成立时仍不发 STOP。父协程取消后的报告边界由[时钟异常期间的报告](../../architecture/clock-recovery.md#时钟异常期间的报告)维护，业务 cancel_task 仍拥有原一次报告机会。

没有父取消时，普通转受限成功后仍先消费旧日志责任，再允许既有的一次受限报告使用新日志责任。有父取消时不恢复普通或报告入口，实际受限致命结果仍按上表衔接必要应急阶段，随后进入最终收场。不要把普通转受限的前置业务消费拆成一个抛出父取消的等待，再把必要 winddown 留给永远不会进入的后继代码。

会话最终消费覆盖所有等待阶段：锁内登记、受理和时钟判断、普通推进、普通转受限、初始受限、已建立的一次受限报告、应急停止、分阶段本地等待及最终日志等待。控制异常从其中任何阶段返回都先归入统一收场，再释放锁；应急回调位于 `drive()` 之外时也必须进入这一捕获路径。一次报告实际工作完成后，健康空闲子进程仍由原生命周期退出路径关闭；因真实失败而进入停止的生成任务须按原协议确认实际退出，不能把会话锁外的一次 `worker.stop()` 当作报告文件结果的消费者。通用释放错误的分类仍由 SW-05 实现，本项只保证释放前的实际责任已经消费。

日志工作线程及已拥有的服务任务可以与必要停止并行交付实际事实。本规则要求必要停止不能被日志等待拖延，以及必要业务阶段结束后才进入会话的日志排空等待；不建立所有日志文件 I/O 必须排在 STOP 之后的额外顺序。

### 本地责任组合的实际控制结果

业务文件执行体也可能交付内部 `CancelledError`。现有文件执行器会把这个实际错误附到原业务等待取消的原因中；`BackgroundFlow` 自行停止后继续消费该原因，因此它的 `settle()` 可以在实际文件结果及连接已经交付后抛出这个原控制对象。组合不能因为它不是普通 `Exception` 而跳过后面的文件或日志拥有者。

`CombinedLocalWork` 继续维护唯一拥有者清单和原顺序。每次完整或排除日志的消费阶段，用一个组合持有的任务逐个等待适用拥有者；该任务在每个拥有者的实际消费边界捕获原 `BaseException`，把全部成功或错误作为正常返回事实。外部等待者通过 `shield` 等同一个组合任务，取消只在该等待分支记录，不传进正在消费的拥有者。也可以逐个使用捕获任务，但同一时刻仅持有一个当前实际等待，结果及顺序须相同。禁止只在会话最外层捕获控制异常，因为那不能补回尚未消费的后续拥有者。

同任务保护只适用于当前消费阶段。阶段实际结果已经交付后释放其登记；正常关闭事务发现新工作并再次推进时，同一组合的下一阶段必须消费新的责任。不能按排除对象缓存整个运行的首份已完成结果。已经等待原阶段的其他观察者仍可消费其持有的任务，清除登记不取消或改写该任务。

| 组合收到的实际事实 | 后续消费与传播 |
| --- | --- |
| 一个拥有者成功或交付普通异常 | 继续原顺序消费其他适用拥有者；成功不新增错误，异常保留原对象。 |
| 一个拥有者交付内部控制异常 | 保存实际原对象及已有附注，继续其他独立拥有者，包括后续日志任务与诊断；不把该对象登记成外部等待取消。 |
| 外部首次或重复取消组合的等待 | 只保存首次外部取消，继续等同一个任务及原拥有者，不提前标记实际责任结束；会话层仍立即登记停止新增工作及启动检查。组合观察本身不建立新的设备停止资格。 |
| 全部适用拥有者已交付，没有控制异常或外部取消 | 保持既有普通出口：零错误成功，一个异常原样传播，多个普通异常按原顺序形成 ExceptionGroup。 |
| 全部已交付，有实际控制异常而没有外部取消 | 传播首个实际控制异常的同一对象，将其他实际错误及其已有附注附给它；控制异常不转换为普通错误组或成功。 |
| 全部已交付，已有外部取消 | 传播首次外部取消的同一对象，附属交付所有实际错误及已有附注；重复取消不替换对象或重做文件操作。 |

附属消费先快照来源附注，并避免把目标对象再次当作自己的次因；同一实际对象重复交付也不能导致循环追加。这项规则同时适用于组合与既有 `_RequiredPhaseWaiter.attach_exception_notes`，不能只在新增组合出口处理。排除日志的前置阶段仍排除它；该阶段实际失败后，最终完整消费独立处理剩余责任，不能因为错误已经被保存就跳过日志。此规则不重试实际文件工作、不改变原清理资格、普通异常分类或 SW-05 的通用锁释放设计。

结果还要穿过 `_settle_local_work` 的中间任务。该任务同样捕获实际 `BaseException` 并正常返回原结果事实；它的 `shield` 分支只处理外部等待取消，随后消费事实中的原控制对象。不能让中间任务直接因内部 `CancelledError` 变成 canceled，再把 shield 产生的另一个取消对象当成原错误；也不能让内部 `KeyboardInterrupt`／`SystemExit` 先越过任务执行边界。会话必要阶段及组合的事实封装可以复用已有私有类型，所有中间消费者都须保留实际对象。

## 日志操作拥有者

采用 `logging_runtime/ownership.py::LoggingOwner` 作为本次运行的薄适配；它不重新实现文件算法，也不使用要求业务数据库身份的文件执行器。

| 接口 | 责任 |
| --- | --- |
| `start_session() -> Awaitable[None]` | 同轮登记唯一启动任务，立即返回；不等工作线程。 |
| `owned_call(stage, operation, *, required=False)` | 默认线程池执行同步核心，在与停止请求共用的短锁内裁决是否开始；线程只返回值、实际异常及开始事实。 |
| `run_service_operation(stage, operation)` | 创建服务拥有者任务并保护它；父等待者取消时先登记同一任务，再立即传播取消。 |
| `stop_new_work()` 与 `stop_requested()` | 共同表示普通日志步骤停止；不替代启动检查的取消事实。 |
| `cancel_startup_sweep()` 与 `startup_stop_requested()` | 表示真正取消对本次检查的影响；同一门锁内阻止未开始调用及下一候选删除。 |
| `resume_restricted_logging()` | 只在旧业务责任、必要受限停止及旧日志责任已收场后允许新受限日志责任。 |
| `required_settlements()`、`settle()`、`take_over()` | 向既有本地责任及监督器交付任务；已完成实际工作和诊断的记录不使正常会话永久保持忙碌。 |

数量有限且决定分支的文件阶段由一个 `LogFileStage` 枚举维护，建议放在现有 `logging_runtime/models.py`。生产者、线程入口、撤回异常和参数化测试引用它；阶段文案从枚举值生成。内部类和方法划分可以调整，但上述停止维度、交接顺序及消费接口须保持一致。

枚举对应已有的 `startup_sweep`、`startup_sweep_unlink`、`marker_check`、`marker_create`、`copy_write`、`marker_progress_copied`、`copy_publish`、`marker_progress_published`、`copy_cleanup` 和 `marker_recovery_delete` 阶段。枚举自身提供启动阶段的分类；实现和测试不另外维护一份完整字符串清单。

服务任务取得原 `FailureLogService` 协调锁后执行步骤、消费结果及必要诊断，完成后再释放它。父等待者取消不会取消该任务。内部文件调用抛出的 `CancelledError` 是实际操作的异常结果；封装为任务返回事实，防止它取消监督器收场。前台服务 API 需要传播该控制异常时保留原对象；后台启动检查将其作为附属操作错误诊断。工作线程不直接写自身 ERROR 日志。

| 实际阶段 | 收到停止时的处理 | 资格释放 |
| --- | --- | --- |
| 已提交但尚未开始的普通调用 | 在入口门锁确认撤回，无本次文件副作用，不开始下一普通步骤。 | 交付撤回事实后结束该责任。 |
| 已开始的调用 | 原调用继续返回成功、失败或未知；后继普通步骤停止。 | 实际调用及结果消费完成前保持责任。 |
| 必要清理尚未开始或仍在池中排队 | 按已经确定的清理资格执行一次，不因普通停止撤回。 | 等实际清理结果及诊断。 |
| 结果已返回、诊断尚未结束 | 在主事件循环继续交付诊断；通道故障按既有 stderr 回退规则处理。 | 服务协调锁及会话锁仍保持。 |
| 调用已返回关闭错误或关闭结果未知 | 保存该事实，保留不能清理的文件，不重试关闭；责任可以交付失败并结束。 | 不把结果已交付说成句柄已关闭；后续旧文件清理仍须前任操作结束证据。 |
| 同一等待再次被取消 | 复用原任务、结果与诊断，不重复提交或清理。 | 同一最终收场路径继续持有资格。 |

## 副本阶段事实与统一清理

`WriteResult` 保留原位置参数，在尾部提供 `copy_handles_closed: bool | None = None`、`copy_source_close_error`、`copy_target_close_error`。只有关闭明确为 `True` 且两侧没有关闭错误，才有发布或清理资格；缺省 `None` 是未交付事实。每个已拥有快照句柄独立尝试一次关闭。

副本流程用一个内部事实对象保留标记检查、创建、两次进度、写入结果、发布结果、当前准备位置和清理是否已尝试。建议命名为 `_CopyDeliveryFacts`。它跟随整个交付流程，不能只在复制异常的局部捕获中决定清理。

位置判断由 `copies.py` 的 `CopyLocation` 枚举分别表达：副本操作尚未开始；任何本次副本仍在已知准备位置；移动已开始且结果未确认；已确认进入 ready；移动位置未知；已经可靠确认没有本次准备副本（`ABSENT`）。“准备位置”只表示任何残留的可清理位置和归属，不声明文件一定存在。一次清理成功或可靠返回 `FileNotFoundError` 后，位置成为 `ABSENT`；它不表示副本曾交接或远端已收到。未开始的发布被可靠撤回时仍为准备位置；发布实际开始后抛出异常而没有移动结果时为位置未知。

| 交付终止时的实际事实 | 本次副本处理 |
| --- | --- |
| 副本操作没有开始 | 不提交本次副本清理；标记临时文件由原保存流程处理。 |
| 副本操作仍在进行，或副本关闭未确认 | 继续消费实际调用结果；关闭失败或未知时保留路径。 |
| 副本关闭明确、归属明确，尚未开始移动且仍在本次准备位置 | 一次必要清理，包括读写失败、成功复制后的取消，以及 COPIED 进度保存抛出异常而终止交付。 |
| 发布在池中排队时被可靠撤回 | 仍为已知准备位置，按同一关闭与归属资格一次清理。 |
| 发布返回 NOT_MOVED，准备位置可确认 | 一次清理，不重试发布。 |
| 发布已开始后没有可靠移动结果，或返回 UNKNOWN | 保留实际已知位置和原因，不推测可清理。 |
| 已确认进入 ready，包括目录同步或 PUBLISHED 进度失败 | 保持 ready 或 processing 的既有所有权，不撤回或删除已交接副本。 |
| 必要清理失败或结果未知 | 保留原错误及残留，本次不重试；下一次合格启动检查再处理。 |
| 一次必要清理已成功，或可靠确认本次准备文件不存在 | 交付 `ABSENT` 与已尝试成功事实；不再清理，不据此宣称交接成功。 |

正常返回的进度保存失败继续遵守 SW-03 和原标记规则，不自行中止仍可完成的首次交付。未预期异常或取消导致交付终止时，统一边界依据上述事实处理清理；标记自己的临时文件清理由原保存拥有者负责。

异常的统一收场保留既有分类边界：复制或发布的普通 `Exception`，在没有另外发生未预期清理异常时继续形成未知副本回执；标记检查、创建和进度保存抛出的未预期异常，在完成有资格的副本清理后继续传播，由原服务暂停及诊断消费者处理。标记检查或创建失败仍暂停同一故障的后续尝试。必要清理自己的未预期异常采用下面的统一组合规则，不能套用复制或发布的回执边界。控制异常保留原传播对象，不能因集中清理而把它转成普通回执。

`_CopyDeliveryFacts` 只作为本次流程的内部事实构造器；所有返回及异常出口统一生成不可变的 `CopyReceipt`。异常上的 `copy_receipt` 唯一允许的类型是 `CopyReceipt`，不能放入内部构造器。实际抛出的原异常或异常组自身携带此属性，消费者以类型检查读取；`FileChannel` 的 `write_result` 仍独立保留。

在 `CopyReceipt` 的原字段之后追加下表中的默认字段，保留原位置参数。已有 `cleanup_error` 保留清理结果；`cleanup_attempted=True` 且 `cleanup_error=None` 表示本次清理已返回成功，并同时交付 `copy_location=ABSENT`，不能把尚未尝试与成功混在一起。清理失败仍保存此前的准备位置资格与实际错误，不声称文件一定存在或已经消失；只有确认成功才转换为 `ABSENT`。

| 新字段 | 类型与事实 |
| --- | --- |
| `file_stage` | `LogFileStage \| None = None`；导致当前退出的文件阶段，与已有标记进度 `stage` 分开。 |
| `publish_result` | `PublishResult \| None = None`；实际返回的完整发布事实，不用 `publish_error` 文本替代。 |
| `copy_location` | `CopyLocation \| None = None`；统一事实构造器交付的实际位置；旧调用缺省为未交付事实。 |
| `cleanup_attempted` | `bool = False`；一次必要清理开始前登记，之后任何出口不再提交。 |

发布返回 `MOVED` 后，PUBLISHED 进度异常上的回执仍保留该 `PublishResult`、已交接位置和零清理尝试。发布已开始后抛出异常的回执保留 `publish_result=None`、位置未知和零清理尝试。异常消费者使用同一回执交付标记、复制、发布和清理事实；原异常对象及优先级遵守下表。

原日志路径停用有一个同步结果消费入口：`FailureLogService` 接受可选的 `on_write_result: Callable[[WriteResult], None]`，默认 `deliver_failure_copy` 在得到正常或异常所携带的真实 `WriteResult` 后，于主事件循环立即调用它，早于任何后续进度保存、发布、清理或诊断等待。默认装配的回调仅在 `disables_file_logging` 为真时调用既有 `_mark_log_path_unsafe`；它只更新共享禁用事实并通知通道，不执行文件操作或写 ERROR 日志。没有回调的既有辅助调用保持兼容，注入的交付替身仍沿原签名运行。最终诊断继续使用类型明确的回执；单独的快照关闭错误不能停用仍可用的原日志。

原诊断消费者继续保留 SW-02 的幂等停用消费，供未绑定拥有者或注入交付端口使用；新增早期回调不取消这项已有责任。正常回执、异常携带的类型回执以及只有类型写入事实的异常，均先依据同一 `disables_file_logging` 消费结果，再推进诊断。默认回调已经停用的路径不因再次消费而恢复，也不重复文件操作。

| 异常组合 | 交付规则 |
| --- | --- |
| 副本操作 OSError，关闭均成功 | 按原 COPY_FAILED 回执处理，实际副本错误及一次清理结果分别保留。 |
| 任一副本关闭错误或关闭事实未知 | COPY_UNKNOWN，保留文件；源、目标关闭原因分别交付，不能据此停用仍可用的原日志。 |
| 首先发生程序异常或控制异常，之后关闭或清理也失败 | 原传播对象保持，次因附加到诊断或原异常；普通 Exception 是否形成回执，按上述实际阶段的原分类边界处理。 |
| 原 OSError 后发生程序 Exception | 在尚未转为回执的异常边界保留有序异常组；实际抛出的组也携带写入事实。 |
| 原 OSError 后发生非 Exception 的控制异常 | 保留该控制对象及此前错误，不转为普通成功结果。 |
| 原报告、时钟或业务退出结果已有，附属日志或诊断失败 | 保留主结果，按 SW-02 出口交付附属事实；异步取消继续生效。 |

### 必要清理的异常组合与最终出口

清理前先保存所有已返回的标记、写入和发布事实。这里的“原异常”只指仍掌握的真实异常对象；`copy_error`、`PublishResult.error` 等正常结果中的错误文本不构成异常对象。停止产生的撤回事实也不当作程序异常。不为错误文本制造 OSError 对象。

`_cleanup_staging_copy` 正常返回的 OSError 诊断属于预期清理结果：保存 `cleanup_error`，不改变已有回执或原传播对象。它之外抛出的程序异常、内部控制异常或异常组属于未预期结果。中央清理边界保存一次尝试和实际错误，再按下表决定传播；所有受影响出口生成同一个类型回执，附到实际传播的原对象或新组合上。

| 清理前掌握的责任 | 清理实际结果 | 最终出口 |
| --- | --- | --- |
| 正常回执，包括已结果化的复制错误、NOT_MOVED 或普通停止 | 成功、可靠缺失或预期 OSError 诊断 | 保留原回执分类和所有事实，清理结果分别保存；成功成为 ABSENT，预期错误保留 STAGING 资格。 |
| 正常回执，没有原异常对象 | 未预期程序异常或内部控制异常 | 传播该实际异常的同一对象，附加原回执事实；操作阶段为 COPY_CLEANUP，暂停及诊断沿原服务出口。 |
| 仍掌握首先发生的程序异常或控制异常 | 任何清理错误 | 保留原异常对象，次因附加到原异常及回执；未预期清理异常导致传播原对象，即使单独的复制程序异常原本可以形成未知回执。 |
| 仍掌握原 OSError 对象 | 预期 OSError 诊断 | 保留原阶段的出口规则，并保存清理次因；不制造第二个异常对象。 |
| 仍掌握原 OSError 对象 | 未预期程序 Exception | 传播有序异常组，成员依次为原 OSError 与实际清理异常；组自身携带完整回执。 |
| 仍掌握原 OSError 对象 | 非 Exception 的控制异常 | 传播该控制异常同一对象，以原 OSError 为次因并附完整回执，不转成普通回执。 |

清理发生未预期异常时，回执继续保存此前已经确定的副本分类、完整 `WriteResult`／`PublishResult` 和标记进度；位置仍为 STAGING 资格、`cleanup_attempted=True`、`cleanup_error` 为实际错误。已有原异常时保留原退出阶段；没有原异常时用 COPY_CLEANUP。清理不会覆盖或重复创建任何事实，也不会再次尝试删除。

主等待者的取消与内部文件控制异常继续分别消费：实际服务任务没有被父等待者取消，内部 `CancelledError` 可以沿上述出口传播到服务诊断，之后作为 `_ServiceFact` 被监督器正常收场；父等待者仍传播此前自己的首次取消。直接辅助调用的未预期控制异常也继续传播，不把 `current_task().cancelling()==0` 当作允许转换普通回执的理由。

## 启动候选与本次归属

`RunFileOwnership` 使用每次运行独立的名称空间及单调序号，副本身份和所有 `MarkerStore` 临时名共用它。专用文件名仍由现有构造函数生成，运行内归属保持常量空间。

`startup_sweep` 每个候选先核对名称，再用 `lstat` 核实普通文件；删除前重查本次归属、调用方活动名及启动停止事实。短同步边界不跨文件系统调用。新文件不得复用已经被认定为旧文件的路径。

普通日志由实际 `paths.log_file` 指定，现行配置允许它位于 `staging/logs`，其名字也可能符合副本或标记临时文件规则。名称匹配不能覆盖这个已知用途。默认 `run` 把同一配置目标传给日志拥有者及启动检查；检查先排除该普通日志的实际目标，再判定旧对象资格，不新增配置路径限制。

建议在 `LoggingOwner` 和 `startup_sweep` 增加可选的 `ordinary_log_path` 关键字参数，现有直接调用保持兼容。在拥有者的同一启动线程责任内解析配置目标和日志目录的路径，折叠相对段并解析已有符号链接。保护依据同时包含配置文件名在解析后父目录中的位置，以及该配置路径解析后的实际目标；用解析后的日志目录及候选名与这两个位置比较。前者保留持续追加和轮换使用的配置位置，后者保护通过文件链接引用的普通日志。路径解析和候选文件检查都不在事件循环线程执行。候选符号链接仍按 `lstat` 保留，不为删除跟随它；解析配置目标是确认应保留的普通日志，并不授权操作该目标。无法可靠确定保护目标时，本次检查交付诊断并保留候选，不以未知路径授权删除；不影响业务主结果或重试扫描。

正常普通日志名、符合副本名或临时名的普通日志目标都遵守同一排除规则。归档名按现有轮换规则带数字后缀，原候选判定不接纳这些名称；保持该规则即可。同名文件在不同的实际目录中不是同一目标，其他有资格的旧副本仍按原规则删除。

| 候选或检查结果 | 行为 |
| --- | --- |
| 本次分配的副本或临时文件，包括已经关闭的残留 | 排除。 |
| 专用名称的旧普通文件，前任操作结束已确认 | 删除一次；半成品和完整文件相同。 |
| 前任操作结束未确认 | 保留，不轮询等待新资格。 |
| 符号链接、目录或其他非普通文件 | 保留，不跟随链接。 |
| 正式标记、普通日志、业务报告或用途未知对象 | 保留；不读取正式标记内容来授权启动删除。 |
| 目录可靠不存在，或候选可靠消失 | 正常结束对应检查，不创建替代文件。 |
| 枚举、候选检查或删除失败 | 记录实际错误；单个候选失败不阻止其他已合格对象，本轮不重试。 |

## 文件责任与实施顺序

以下路径相对于 `apps/camctl`。文件划分为当前方案，内部调整须同步生产者、消费者和验收指向。

| 文件 | 修改责任 |
| --- | --- |
| `src/camctl/logging_runtime/models.py` | 有限文件阶段的权威类型。 |
| `src/camctl/logging_runtime/ownership.py` | 线程开始裁定、两个停止维度、同任务交接与结果消费。 |
| `src/camctl/logging_runtime/copies.py` | 全流程事实与类型回执、统一清理、同步写入结果消费、服务协调及当前归属和候选判定。 |
| `src/camctl/logging_runtime/clh_adapter.py` | 独立快照关闭证据、异常优先级及原日志事实。 |
| `src/camctl/host_files/handoff.py` | 异步发布及日志拥有者复用同一同步发布核心。 |
| `src/camctl/session/service.py` | 锁内登记、取消交接、正常早错及普通转受限的消费顺序、类型明确的诊断。 |
| `src/camctl/bootstrap/lifecycle.py`、`background_flow.py` | 存在性阶段、最小坏库装配、单一拥有者组合和分阶段收场。 |
| `docs/architecture/clock-recovery.md` | 区分父协程取消与业务取消动作的一次报告资格；已开始的实际报告责任仍须收场。 |
| `tests/unit/logging_runtime/test_copies.py`、`test_ownership.py`（已新增） | 纯内存阶段矩阵和回执、异常清理资格。 |
| `tests/integration/logging_runtime/test_copies.py`、`test_startup_ownership.py` | 真实关闭、异常组合、目录分类与当前生产者归属。 |
| `tests/integration/bootstrap/test_startup_log_sweep.py`、`test_log_operation_settlement.py`、`test_log_copy_diagnostics.py` | 默认入口、实际线程、两把锁、源停止、受限录像停止及诊断交叉。 |
| `tests/integration/bootstrap/test_emergency_session_exit.py` | 真实应急停止与补记期间的首次／重复取消、实际日志责任及会话锁边界。 |
| `tests/unit/bootstrap/test_background_flow.py`、`tests/integration/bootstrap/test_log_operation_settlement.py` | 组合的实际控制结果、直接观察取消与后续拥有者消费；真实文件控制原因经过 BackgroundFlow 后，日志及诊断仍在会话锁内结束。 |
| `tests/integration/bootstrap/test_restricted_report_cancellation.py`（已新增） | 默认受限报告已经派发后的父取消、实际生成及文件消费者。 |
| `tests/integration/bootstrap/test_directory_binding_runtime.py`、`test_report_shutdown.py` | 包装原报告装配时转发同一日志拥有者，保持原配置致命错误及生成进程收场结果的断言。 |
| `tests/integration/session/test_session.py` | 无日志拥有者及注入交付端口的既有停用消费、诊断顺序及会话主结果。 |
| `tests/integration/acceptance/test_entrypoints.py`、`tests/integration/host_files/test_file_contract.py`、`tests/integration/logging_runtime/test_clh_adapter.py` | 命令职责、共享交接及既有适配消费者。 |

### 任务一：核对设计并形成统一交接

- [x] Root 沿规格及真实默认入口自审本页，核对每种停止原因、操作阶段、资源资格、异常及结果出口。
- [x] 独立只读审查设计与计划的交叉关系；Root 核实并处理发现。必要阶段、组合实际控制结果及会话中间事实桥模型和验收已一致。
- [x] Root 形成一个实施说明，列出当前已正确实现的部分、待修代码及准确测试，在说明一致后明确恢复实施。当前代码和正确专项保留。

### 任务二：闭合日志操作及清理责任

**消费接口：** 上述线程入口与停止维度、SW-02 诊断、SW-03 保存结果和既有同步发布核心。

**交付接口：** 每个文件阶段具有开始、实际结果与停止语义；交付流程在所有出口使用同一清理资格，控制异常和原日志事实完整到达消费者。

- [x] 在纯内存测试中参数化标记检查、创建、COPIED 进度、发布、PUBLISHED 进度及恢复删除的 queued 和 started 状态；普通步骤撤回、必要清理排队仍执行，诊断期间服务资格保持。
- [x] 修订已有 `test_service_keeps_coordination_until_canceled_delivery_settles`：使用真实 `LoggingOwner`、`Supervisor`、`FailureLogService` 和受控异步交付及诊断。取消外层等待者后，在实际交付仍等待以及结果已到达但诊断门闩仍关闭的两个时点，分别发起同一服务调用；第二调用保持 pending 且不进入交付。原交付成功，不以服务暂停掩盖提前释放；最后放行诊断，确认原责任消费一次。纯内存测试只证明服务协调，不声称持有 OS 会话锁。
- [x] 在已由同一 owner 接手的服务任务中交付内部 `CancelledError`，锁内诊断保存该对象；`Supervisor.drain_required()` 正常返回一条 settled 及零 failed，外层首次取消对象不变，重复 drain 不重复消费。
- [x] 为已关闭且仍在准备位置的异常出口补反例：实际复制 RuntimeError／内部 CancelledError、COPIED 进度异常及清理异常。对比关闭未知、未交付事实及发布位置未知的保留分支。
- [x] 按必要清理决策表审计所有调用清理的出口：正常复制错误回执、普通停止、NOT_MOVED、原复制异常和 COPIED 进度异常。补齐没有原异常对象和原 OSError 对象的反例；中央边界决定异常组合、生成回执及一次传播，不在各调用点另造不同优先级。
- [x] 实施统一阶段类型及全流程事实对象，把清理集中到交付终止边界；使用关键字构造新增回执分支。新错误组合保留原对象或原回执语义和所有次因，不重复删除。
- [x] 核对恢复删除：queued 撤回不解除原暂停；实际结果及诊断消费完成后才沿原恢复规则改变暂停。
- [x] 更新前台异常和接手结果的诊断消费者，消费同一个类型回执及写入事实；接入同步写入结果回调，证明已确认原路径失效后，尚在等待后续清理或发布的复制流程不会让并行启动诊断再使用它。辅助错误不修改业务主结果。定向运行本任务涉及的单元及日志集成文件，保留有效失败及对应通过证据。

### 任务三：闭合默认会话与实际资源

**消费接口：** 任务二的拥有者、回执及诊断出口。

**交付接口：** 正常、早错、受限与取消路径共用同一锁内登记及收场；设备或源停止不会等待日志线程。

- [x] 复核已有启动资格测试，覆盖缺失、真实 stat 错误、非普通状态库、坏库最小路径、锁内 open／恢复／受理早错及未取得锁零扫描。
- [x] 用默认线程池被占满的场景证明业务先推进，真正取消可靠撤回 queued 扫描。取消处理的线性化事件先发生，再放行线程，避免测试竞态。
- [x] 取消原先以 `stop_new=False` 进入的实际收场等待时，同步停止普通本地工作及启动检查，再继续同一个收场任务；不因重复取消重建任务或改变首次取消。
- [x] 用真实启动候选 unlink 门闩证明取消后当前调用实际完成、下一候选不删除、SessionLock 与诊断持有到实际收场。
- [x] 复核已有默认复制与恢复删除取消用例：受控源停止先于日志门闩放行，重复取消保持原取消，两把锁覆盖实际结果与必要诊断。
- [x] 复核真实 winddown 停止先于日志收场；保留业务本地保存失败时不进入受限流程的对照。另验证初始受限 run 一次检查，正常及受限报告机会保持原数目。
- [x] 用同一必要收场等待规则覆盖初始受限、后期失信及 fatal 应急停止；真实 STOP 已开始时首次及重复取消，均保留同一任务和首取消，实际结果、适用保存及关闭完成后才能进入会话日志排空。放行 STOP 前已经同步停止普通本地工作及启动检查。
- [x] 验证普通转受限的前置业务消费期间取消：实际成功仍按原资格进入必要 winddown；实际失败仍跳过依赖该前提的受限流程。受限阶段在父取消后返回合格致命结果时，仍进入原资格的唯一应急阶段，再最终收场。
- [x] 受限或应急内部控制异常也进入统一最终收场；依赖前提失效沿原规则停止后继工作。原资格尚未成立的取消对照不创建必要阶段；已建立阶段中的取消按原目标资格、等待和预算继续有限处理。首次父取消与内部实际异常分别保留，不据控制异常推定设备结果。
- [x] 已建立的一次受限报告也由同一等待器持有；在真实 `WorkerSupervisor.generate` 已派发后首次／重复取消，报告实际生成、文件及适用保存先于最终会话消费。父取消不单独决定报告停止；真正超时、失败和前提失效仍使用原停止协议。
- [x] 核对并完成同一会话组合和日志门锁的分阶段消费，定向运行 bootstrap 与 session 专项。所有测试等待使用防挂保护和 finally 放行，防挂超时不成为产品操作完成事实。
- [x] 组合持有各拥有者的实际结果，内部控制异常不短路后续拥有者；直接观察取消与会话父取消分别验证，同一任务完成后再传播首取消或首实际控制对象，普通 Exception 组合保持兼容。

### 任务四：闭合文件集合及命令消费者

- [x] 完成独立 `test_startup_ownership.py` 草稿，使用真实生产者证明副本与每次临时名共用当前归属，已关闭残留仍排除；核对正式标记、未知用途、非普通对象及其他目录保护。
- [x] 删除前同时重查本次归属和调用方 `active_names`；纯内存反例在候选 `lstat` 期间新增活动名，证明原兼容入口也不删除它。
- [x] 默认启动检查消费实际普通日志目标，在真实路径下验证副本名／临时名及路径别名的保护，其他合格旧对象仍各删除一次。路径解析未知的对照保留候选并交付诊断，业务结果保持。
- [x] 默认 run 与新副本／临时文件重叠时，实际本次文件不被启动删除；单次旧删除失败后下一次 run 可以清理残留，期间不复制或补投旧日志。
- [x] 核对 init／submit／describe 零扫描，以及正式 CLI stdout 仍为单条会话 JSON 和单换行；stderr 诊断按实际事实输出。
- [x] 同步内部副本替身的真实关闭事实，运行共享 handoff 消费者专项。测试以字段与副作用判断结果，不固定完整人类文案。

### 专项断言与验证入口

下表给出实际测试函数及其断言范围。真实组件与受控接口分别说明，相关对照使用已有专项，不重复维护相同用例。

| 函数与所属文件 | 可证伪的结果 |
| --- | --- |
| 已新增 `unit/logging_runtime/test_ownership.py::test_owned_stage_cancellation_preserves_queued_and_started_results` | 参数化所有非启动阶段；普通 queued 可靠撤回且零调用，started 在操作体内收到停止后仍返回原结果、只调用一次，下一普通步骤零提交。服务结果、诊断及资格由下述独立服务专项核对。 |
| 已新增同文件 `test_required_cleanup_runs_when_queued_before_stop` | 必要 cleanup 在池中排队后收到停止仍执行一次，交付真实结果。 |
| 修订已有 `unit/logging_runtime/test_copies.py::test_service_keeps_coordination_until_canceled_delivery_settles` | 真实服务成功且未暂停，owner 接手后，同一服务的第二调用在实际交付及主循环诊断门闩关闭期间均 pending；放行后才进入，原结果及诊断各一次。 |
| 已新增 `unit/logging_runtime/test_ownership.py::test_internal_cancelled_error_is_settled_without_canceling_supervisor` | 原父等待先被取消并完成交接，再由服务操作交付内部控制异常；诊断保留内部原对象，监督器返回一条 settled、零 failed，父首次取消保持，第二次 drain 无重复责任。 |
| 已新增 `integration/logging_runtime/test_copies.py::TestDelivery.test_copied_progress_exception_cleans_confirmed_staging` | 真实复制已经返回关闭成功，COPIED 保存抛出原异常；准备副本删除一次，正式标记保留，publisher 零调用，异常附带此前真实写入及标记事实，位置 ABSENT、清理尝试为真且错误为空。可靠 FileNotFound 的对照同样结束，不再删除。 |
| 已新增同文件 `TestDelivery.test_cleanup_failure_preserves_primary_and_receipt` | 在上述有资格清理中注入 OSError、程序 Exception 或内部控制异常；异常携带的回执均为 STAGING、清理尝试为真，并保存实际清理错误。原传播对象或原回执语义保持，次因及一次尝试保留；不再删除或发布，不把失败记为 ABSENT。 |
| 已新增同文件 `TestDelivery.test_cleanup_unexpected_exception_after_resultized_error_is_propagated` | 真实复制正常交付已关闭的写入结果及错误文本，或者发布返回 NOT_MOVED；清理实际 RuntimeError／内部 CancelledError 同一对象传播，回执保留原分类、写入／发布事实、STAGING 和一次失败清理，正式标记保留，不重试。 |
| 已新增同文件 `TestDelivery.test_cleanup_unexpected_exception_during_stopped_delivery_is_propagated` | 分别在真实复制返回后、COPIED 进度返回后同步登记普通停止；必要清理实际 RuntimeError／内部 CancelledError 同一对象传播，阶段为 COPY_CLEANUP，分别确认进度或发布没有开始，失败清理一次。 |
| 已新增同文件 `TestDelivery.test_cleanup_exception_priority_after_raw_primary` | 分别覆盖 COPIED 进度及 COPY_WRITE 原样抛出的 OSError：关闭与归属已确认，预期清理 OSError 诊断保持原阶段分类，清理 RuntimeError 得到 `[原 OSError, 清理异常]` 的有序组，清理内部 CancelledError 得到该控制对象及原 OSError 次因；原程序／控制异常后的未预期清理保留首个原对象。COPY_WRITE 变体使用真实已返回的快照关闭事实。实际传播对象／组均有完整回执、STAGING、一次尝试和实际清理错误，publisher 零调用且标记保留。另覆盖真实复制原程序异常加未预期清理，按同一优先级传播原对象。 |
| 已新增同文件 `TestDelivery.test_published_progress_exception_preserves_moved_receipt` | 真实发布返回 MOVED，PUBLISHED 保存抛原异常；异常上的 `CopyReceipt` 保留完整发布结果和已交接位置，ready 副本保留，清理尝试为零。 |
| 已新增同文件 `TestDelivery.test_started_publish_exception_preserves_unknown_location` | 发布已开始后抛原程序异常；COPY_UNKNOWN 回执保留位置未知、没有发布结果且清理尝试为零，准备文件不被推测删除。 |
| 已有同文件 `TestDelivery.test_confirmed_closed_snapshot_exception_cleans_own_staging_once` | 线程内 RuntimeError／CancelledError、实际 source 与 target 均关闭；owned staging 清理一次，marker 保留，无 publish；控制异常保持原对象。 |
| 已新增 `integration/bootstrap/test_log_copy_diagnostics.py::test_write_result_disables_log_before_followup_wait` | 受控原通道故障的真实写入结果已返回、后续文件步骤仍阻塞时推进启动诊断；诊断走 stderr，原路径不再调用投递或新建处理器。仅副本快照关闭错误保持原通道可用，由 `integration/logging_runtime/test_copies.py::TestDelivery.test_copy_close_error_preserves_staging_and_marker` 独立验证。 |
| 已新增 `integration/session/test_session.py::TestRunCompletion.test_ownerless_custom_delivery_consumes_unsafe_write_result` | 真实服务使用注入交付端口，分别返回正常回执、异常携带回执、异常携带写入事实；已有停用回调先于 stderr 诊断，报告主结果仍为 report_error，诊断保留原报告错误。 |
| 已新增 `integration/bootstrap/test_startup_log_sweep.py::test_cancel_during_open_admission_drain_stops_all_new_work` | 直接调用原收场帮助器、允许新增工作，收到取消后组合停止入口与启动停止事实均已登记；放行后传播首次取消。 |
| 已新增 `integration/bootstrap/test_startup_log_sweep.py::test_cancelled_started_unlink_keeps_lock_and_stops_next_candidate` | 在默认会话的受控 unlink 已进入门闩后取消；替身交付注入的 OSError 并完成诊断前，下一 run 无法取得锁，第二候选零删除。真实删除及跨 run 重试由下一轮恢复专项独立验证。 |
| 已新增同文件 `test_initial_restricted_run_sweeps_once` | 初始时钟进入受限模式，旧合格 copy／temp 清理一次；必要停止及一次报告沿原资格推进，stdout 或返回结果保留 clock_invalid。 |
| 已新增 `integration/bootstrap/test_emergency_session_exit.py::test_cancel_during_emergency_stop_settles_before_logs_and_unlock` | 真实 `EmergencySettlement`、受控 STOP 端口、真实 SQLite／锁及日志 owner；在异步 STOP 门闩期间首次／重复取消。放行前已同步停止本地新增工作及启动检查，日志排空未开始且锁仍持有；放行后一次实际停止，日志收场前核对已保存的数据库停止结果及已关闭的独立连接，最后传播首取消。 |
| 已新增同文件 `test_internal_emergency_control_error_still_settles_logs` | 必要应急任务交付预建内部 CancelledError；它不成为正常停止结果，日志实际线程及诊断仍在会话锁内收场，最后传播该原对象。已有父取消的对照保持首父对象并保留内部次因。 |
| 已新增同文件 `test_initial_restricted_internal_control_stays_separate_from_parent` | 实际受限内部控制在真实日志诊断门闩期间保持原对象；无父取消时不撤回启动检查，有真正父取消时传播首父对象且保留内部异常已有附注。 |
| 已新增 `integration/bootstrap/test_startup_log_sweep.py::test_cancel_during_restricted_stop_preserves_result_before_log_drain` | 参数化初始受限与后期失信；真实 winddown 已保存 stop 意图且驱动进入后首次／重复取消。放行前本地 stop 与启动停止事实已登记，原调用未被父取消且日志排空未开始；放行后实际 SQL 结果及独立连接关闭先于日志排空，STOP 不重发，不进入尚未开始的一次报告，最终传播首取消。同步结果保存不以阻塞事件循环的门闩制造取消窗口。 |
| 已新增 `integration/session/test_session.py::TestRestrictedSession.test_required_phase_cancellation_keeps_original_eligibility` | 用受控原接口验证合格必要阶段已建立但尚未开始时取消仍只处理同一阶段；未取得原资格不建立应急／受限阶段。普通转受限前置业务消费的实际成功仍进入原必要收场，实际失败不进入；原预算及等待条件保持，内部控制异常与父取消分别交付。 |
| 已新增同文件 `TestRestrictedSession.test_cancelled_restricted_fatal_still_runs_qualified_emergency_settlement` | 受限必要任务在父首次／重复取消后实际返回 state_db_error／configuration_error；原应急回调一次执行并完成结果保存，再进入日志排空。首父取消及实际致命结果均保留，无普通工作或新报告；无原应急回调的对照不创建应急阶段。 |
| 已新增 `integration/bootstrap/test_restricted_report_cancellation.py::test_started_restricted_report_cancellation_preserves_actual_result` | 默认 once_report/report_flow 与实际 WorkerSupervisor；已派发生成后首次／重复取消，同步停止本地新增工作及启动检查，父取消不触发生成停止。放行后原结果由报告消费者交付，核对实际文件和 SQL 保存／失败事实、独立连接关闭及会话锁，最终传播首取消。实际生成失败的对照按既定协议确认退出并保留未完成责任；未开始报告的资格由上述受限取消专项独立验证。 |
| 已新增 `integration/bootstrap/test_log_copy_diagnostics.py::test_current_copy_and_temp_survive_overlapping_sweep` | 默认报告故障使用本次同一归属提供者，在真实扫描期间产生副本和临时文件；启动 unlink 不包含本次名字，失败临时清理由保存流程独立处理。旧对象单次删除按集合和数量核对，不依赖目录枚举顺序；不另保留替换整套报告流程的重复证明。 |
| 已有 `integration/bootstrap/test_startup_log_sweep.py::test_failed_old_copy_delete_is_retried_by_next_run` | 首次实际 run 单次删除失败且保留残留；第二次实际 run 清理成功，无旧日志复制或补投。 |
| 已新增 `unit/logging_runtime/test_copies.py::test_startup_sweep_rechecks_active_names_after_lstat` | 专用旧候选在 lstat 返回前被调用方登记到活动名序列；删除前的动态复查使该候选零删除，其他合格对象按原规则处理。 |
| 已新增 `integration/bootstrap/test_startup_log_sweep.py::test_configured_ordinary_log_survives_startup_sweep` | 默认 `build_runtime`／`execute_command`、真实日志通道、SQLite／会话锁及 `HOST_LOCAL_SETTLED`；参数化符合副本名和临时名的普通日志路径，核对扫描未调用其 unlink、原字节保持且后续正常日志仍写到该文件，其他旧副本及临时文件各删除一次。相对段和目录／文件符号链接别名仍按实际目标排除；同名但实际在其他目录的对照不误排除合格旧对象。文件链接另以明确门闩在保护目标解析完成后、枚举前替换配置位置为真实普通文件，核对配置位置及原实际目标均保留、其他旧对象仍删除；这项文件替换对照证明两个位置的保护，不作为完整日志轮换算法验收。 |
| 已新增同文件 `test_unknown_ordinary_log_target_preserves_startup_candidates` | 仅在启动线程解析保护路径处注入实际路径错误；默认装配下旧候选零删除，诊断已交付，原业务结果和一次检查事实保持。不是用整个扫描替身返回预制结果。 |
| 已新增 `unit/bootstrap/test_background_flow.py::test_combined_internal_control_settles_remaining_owners` | 内部 CancelledError 原对象带附注，后续实际拥有者及诊断先完成，再传播该对象；普通 Exception 的单个／多个出口保持原样。同一实际控制对象由后续拥有者再次交付时，不循环追加自身附注，原附注快照保持有限；同一附注消费者的 target 与 source 为同对象时同样保持原对象和原附注。 |
| 已新增同文件 `test_combined_observer_cancellation_waits_same_actual_task` | 直接取消组合的观察等待，不取消被等待的拥有者、不提前宣告结束；重复取消后原次序及实际调用各一次，最后首外部取消携带实际控制及附注。组合不据此新增业务停止资格。 |
| 已新增同文件 `test_combined_consumes_new_owner_work_after_prior_phase` | 同一个组合在首阶段已交付后，真实 BackgroundFlow 再开始一项责任；下一阶段等待并消费新的实际结果及后续拥有者，不重放此前成功或错误，当前阶段的重复取消仍等待同一任务。 |
| 已新增 `integration/bootstrap/test_log_operation_settlement.py::test_business_internal_control_still_drains_logs_before_unlock` | 真实文件执行器的受控执行体产生内部控制错误，BackgroundFlow 停止后消费该实际原因；覆盖后期时钟失信的排除日志前置消费，以及状态库致命退出的最终完整消费。真实 SQLite／会话锁及 LoggingOwner 的已开始线程、必要诊断在同一会话内完成。两个入口分别参数化无父取消与有父取消：前者最终异常必须是预建实际控制对象，后者最终异常必须是已观察到的首次父取消对象，且保留内部控制文本和预置附注。日志门闩放行前锁仍持有，重复取消不替换对象；所有实际责任交付后才传播，不重复操作或丢掉后续拥有者。 |

## 验证与提交

pytest 由唯一执行代理在同一 checkout 顺序执行；每个集成目录使用独立进程，遵守[测试指南](../../../apps/camctl/tests/AGENTS.md)。只有代码变化、失败或未解决的顾虑才重跑相关专项；中间命令、环境、退出码和关键输出集中到一个执行索引。

- [x] 生产和测试已冻结文件 SHA-256，Root 逐文件探索上下文，独立代理复核；处理发现后只复验受影响专项。
- [x] Root 批准完整门禁后，按以下顺序各运行一次独立 pytest：`unit`、`integration/logging_runtime`、`integration/session`、`integration/bootstrap`。全部使用部署版本 Python 3.11，记录实际命令、stdout、stderr、时间和退出码；失败时先定位责任再决定补跑范围。
- [x] 文档链接、标题锚点、实际测试名称、已勾选验收与证据范围一致；执行 `git diff --check`。
- [x] 完成后更新[验证记录](../../camctl/verification.md)与路线图 SW-04，形成独立提交并核对清洁状态，再交给[SW-05](2026-10-13-camctl-sw05-session-lock-errors.md)。

完整目录命令模板；最后一个路径每次只取上述一个目录：

```bash
UV_PROJECT_ENVIRONMENT=/workspaces/camctl/apps/camctl/.venv311 uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q
```

门禁通过仅证明本项软件实现；真实设备、ARM 部署与现场验收沿原路线图单独记录。
