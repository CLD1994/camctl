# SW-05：会话锁错误分类与句柄收场实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务执行。先以失败测试锁定实际错误类别和副作用，再修正端口及消费者；独立复核真实锁和会话输出。

**目标：** `run`／`submit` 按实际发生的锁、数据库或内部故障报告错误；正常接纳冲突仍参与成功接管判定，句柄收场不掩盖首个错误或跳过其他资源。

**组织建议：** 锁端口统一形成带原始原因的 `LockError`，会话按责任边界分类。事务包装保留原异常及回滚结果，所有退出在实际本地责任结束后完成锁和连接收场，并保留后续错误。

**技术基础：** Python 3.11、既有 `LockBackend`、Linux `flock`、受理／关闭事务及 `SessionOutcome`。

**设计依据：** [会话错误标识](../../architecture/session-errors.md#错误标识)、[诊断字段及错误先后](../../architecture/session-errors.md#诊断字段)、[事务内接管](../../architecture/protocol-session.md#submit-的事务顺序)、[正常关闭接纳](../../architecture/protocol-session.md#run-正常关闭接纳)、[启动顺序](../../architecture/cli-commands.md#run-的启动顺序)。本计划承接[路线图 SW-05](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，编写及建议实施顺序在[SW-04](2026-10-13-camctl-sw04-startup-log-sweep.md)之后。

## 当前入口与文件责任

2026-10-13：默认装配把 `session/locks.py::acquire_session` 直接交给会话，`session/service.py::_run_session` 却把任何取得会话锁异常转为 `state_db_error`；接纳锁取得也有相同转换。锁端口已有 `LockError` 和冲突子类，但 `_open_lock_file` 的不可继承设置、清理关闭及 `_Lease.close` 的异常没有完整归一，关闭还可能覆盖先前解锁错误。

`submit` 的探测由受理事务内核执行，原错误可保存在 `DbOutcome.error` 中；`accept_input` 当前只构造 `AcceptanceStateError` 文本及 `outcome_kind`，没有把原异常交给会话分类。正常关闭事务的锁释放错误也被 `_try_close` 当成状态库错误。普通 `run` 在下界事务结束后直接取得接纳锁，尚未遵守短交接写事务内取得的规格。最终 `finally` 中一个锁关闭异常可能阻止另一把锁及数据库连接关闭，并外溢至 CLI，失去结构化会话结果。

路径相对于 `apps/camctl`，实现组织可调整，但不改变下列责任。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/session/locks.py` | 打开、不可继承、取得、探测、释放及关闭的完整错误事实与句柄归属。 |
| `src/camctl/session/service.py` | 锁取得、输入异常、关闭事务及最终收场的准确分类和错误合并。 |
| `src/camctl/acceptance/service.py`、`persistence/transaction.py` | 保留受理事务中原锁错误及实际回滚结果；不把数据库未知解释为受理未发生。 |
| `src/camctl/persistence/repositories/session.py`、`acceptance.py` | 接纳取得、探测和关闭各在对应短写事务内完成；原异常随 `DbOutcome` 传递，不丢失实际租约或锁错误来源。 |
| `src/camctl/bootstrap/lifecycle.py`、`cli.py` | 审计 SW-04 早期错误路径、日志及默认输出；避免已可处理的锁错误外溢。 |
| `src/camctl/logging_runtime/lifecycle.py` | 完成真实 CLI 控制退出时的日志文件、通道及异步责任收场，保留原控制对象和普通辅助诊断。 |
| `src/camctl/persistence/initialization.py` | 审计同一会话锁端口的 `init` 消费者，保持显式初始化和重复初始化的原结果契约。 |
| `tests/unit/session/test_locks.py` | 完整端口异常分类及句柄关闭测试。 |
| `tests/integration/session/test_lock_error_classification.py`（新增） | 真实受理／关闭事务与锁端口的组合分类。 |
| `tests/integration/bootstrap/test_session_lock_errors.py`（新增） | 默认装配及真实 CLI 的退出分类、锁持有、零前置副作用。 |

## 全局约束与接口

- 保持稳定锁文件和内核锁，不删除重建锁文件，不用文件存在性判定持有状态。句柄不可继承，未取得会话锁零受理、零设备工作、零 SW-04 扫描。
- `submit` 不取得会话锁；只有实际 `CONFLICT` 表示已有接纳者，`ACQUIRED_AND_RELEASED` 表示本次真正取得并成功释放。系统错误和释放未确认都不能伪造二者。
- 维持现有非阻塞锁获取方式。本任务不增加等待预算、自动重试或新公共错误码；会话锁冲突保留专门类型和冲突诊断。取得失败导致本次 `run` 无法履职时输出 `lock_error`，不能改成数据库损坏。
- 锁端口签名保持 `acquire_session(path, backend=None) -> SessionLease`、`acquire_admission(path, backend=None) -> AdmissionLease`、`probe_admission(path, backend=None) -> AdmissionProbe` 和租约 `close(...) -> None`。预期系统失败抛 `LockError`，冲突保持其专门子类；保留实际阶段、路径、errno、原异常及已确认关闭事实。
- `accept_input` 保留原 `DbOutcomeKind` 并通过异常原因传递原错误，例如 `AcceptanceStateError` 的 `__cause__` 指向原异常。事务回滚又失败时保留原操作错误和回滚错误，建议使用有序异常组；不可只保留最后一个错误，也不以异常文本匹配类别。
- 成功的锁探测必须发生在原受理写事务内；正常释放接纳仍在最终工作重查事务内。改变错误分类不能移动这些副作用或把它们放到提交成功之后。
- `ROLLED_BACK` 表示本次事务未提交，`UNKNOWN` 保留未知；锁错误不能让未完成受理输出成功 `needs_run`，也不能撤销之前已提交事务。
- 结果使用已有 `reason` 与 `details`；阶段只使用已定义的 `locking`、`handoff`、`shutdown` 等值。首个致命错误为主，后续真实错误进入无嵌套的 `secondary_errors`。辅助日志锁及独立报告工作锁仍按其原责任分类。
- Python、测试替身与运行隔离遵守[组件测试指南](../../../apps/camctl/tests/AGENTS.md)。Linux 真实锁测试证明开发环境行为，不替代 ARM 部署结论。

## 复合失败与实际资源的统一模型

本项先以 SW-04 提交 `8755532fc5d0c6e5a1693cb1f5def8481c995eda` 为基线核对以下模型，再进入失败测试和实现。Root 负责同一权威计划与完整实施说明；相关设计未闭合时，依赖实施和测试保持暂停。实现中的私有类型和辅助函数可以按实际数据流组织，以下错误先后、资格和结果规则须一致。

### 锁操作事实及机器诊断

锁模块是操作子阶段与关闭事实的唯一来源。内部操作类型覆盖 `open`、`set_inheritable`、`try_lock`、`unlock`、`close`；类型定义只在锁模块维护。错误事实保留实际操作、路径、原异常、冲突观察、解锁与关闭的尝试和确认状态，以及后续实际错误。`errno` 只从实际原 `OSError` 取得，不根据文案推测。可用一个不可变事实类型承载这些信息，由 `LockError` 及未转换的内部异常共同携带，消费者只接受该类型。

预期系统错误转换为 `LockError` 并保留原 `OSError` 为原因；取得锁的真实冲突保持对应专门子类。锁端口内部发生程序异常或控制异常时，先完成已打开句柄的一次收场，再按下述统一优先级传播原对象；不把这类异常改成系统锁错误。类型化锁上下文也标明内部控制异常来自同步锁操作，避免会话把它误登记为父等待取消。旧的 `LockError(message)` 构造及所有锁函数签名保持兼容。

锁操作及其句柄收场采用同一优先级：如果实际发生了非 `Exception` 的控制异常，传播最先发生的那个控制对象；此前普通错误和此后实际错误仍按发生顺序保存在其他事实中。没有控制异常时，首个普通错误为主；首个 `OSError` 转换为 `LockError`，首个程序异常保持原对象。此规则也覆盖首个 `OSError` 或程序异常之后关闭句柄才发生控制异常的情形，不能把真实控制对象降为普通锁错误的次因。根事实的 `operation` 和 `original_error` 始终对应实际传播的主异常来源，取得、冲突、解锁与关闭状态仍描述本次完整操作。

关闭只发生一次，其他事实不重复登记同一错误对象。已有原控制对象时，后续错误不能替换它；其原附注保持。普通关闭 `RuntimeError` 不能越过先前的 `OSError`，而关闭的 `KeyboardInterrupt`、`SystemExit` 或 `asyncio.CancelledError` 必须原对象传播并保留此前错误。获取失败后关闭、探测收场和租约释放复用同一优先级选择，不能各维护不同的分类算法。事实访问器对 `LockError.facts` 和内部异常附属事实均作同一类型检查，不接受任意同名属性作为可信事实。

会话结果只使用既有 `reason` 与诊断字段。取得资格错误的 `details.stage` 为 `locking`，交接事务错误为 `handoff`，最终资源收场错误为 `shutdown`；内部操作子阶段进入人类诊断，不替代这些公共阶段值。可靠的路径和错误号进入 `path`、`errno`，后续错误按实际责任映射为平坦的 `secondary_errors`。

锁端口的异常交付与会话结果是两个边界。锁端口抛出原程序异常对象，锁事实及事务 cause 保留该对象；会话消费者将本项可识别的普通锁程序异常分类为 `SessionOutcome(reason="internal_error")`，供 CLI 按原会话协议输出，不能改成 `state_db_error` 或让它外溢而丢掉机器结果。只有控制异常原对象继续向会话调用方传播。普通程序首因后的回滚数据库错误仍为次因，事务状态和已提交事实保持。已有原附注及可信操作事实作为诊断来源保留，不把异常对象放进 JSON，也不批量重分类本项未审计的其他业务异常。

| 观察边界及实际错误 | 交付方式 |
| --- | --- |
| 锁端口遇到普通程序异常 | 抛出原对象，附带锁模块的可信操作及收场事实。 |
| `run`／`submit` 消费可信的普通锁程序异常或以它为首因的事务错误组 | 形成 `internal_error` 会话结果，保留实际阶段、路径、事务状态及平坦次因；租约再次交付同一错误时不重复登记。 |
| `run`／`submit` 消费同步锁或事务控制异常 | 完成适用必要责任与各资源收场后传播最先的同一控制对象；不形成普通会话结果。 |
| `init` 消费普通锁错误或程序异常 | 沿初始化结果契约保留原错误对象及实际库事实；控制异常按原对象传播规则单独处理。 |

| 锁端口实际经历 | 首因、事实与结果 |
| --- | --- |
| 打开成功，设置不可继承失败 | 尝试关闭已打开句柄一次；没有控制异常时保留设置失败为主，其余错误为次因；控制异常按上述统一优先级原对象传播。 |
| 加锁系统错误，关闭再次失败 | 没有控制异常时原加锁错误为主，关闭错误为次因；控制异常按上述统一优先级原对象传播；不交付租约。 |
| 取得锁发生冲突，关闭成功 | 取得端口传播原冲突子类；探测端口返回 `CONFLICT`。 |
| 已观察冲突，随后关闭失败 | 不交付探测结果；关闭为 `OSError` 时，取得端口保留冲突子类及关闭次因，探测端口以实际关闭故障返回锁错误。关闭为程序或控制异常时原对象传播，根事实为 `close` 及该原异常。两种情况均保留冲突观察，不把同一关闭错误重复登记为次因。 |
| 探测取得成功，解锁或关闭失败 | 不返回 `ACQUIRED_AND_RELEASED`；按统一优先级保留主异常、其余错误与已观察取得事实。 |
| 租约解锁失败，关闭仍可执行 | 仍关闭句柄一次，按统一优先级保留解锁及关闭的各自事实；关闭成功不抹去已发生的解锁错误。 |
| 租约关闭成功后再次关闭 | 正常空操作，不重复系统调用。 |
| 租约关闭失败或未确认后再次关闭 | 不再对同一数字 fd 解锁或关闭；重新交付保存的原失败，不用正常空操作宣称收场成功。 |

数字 fd 在开始关闭责任后不再用于重试，避免误关闭后来复用该数字的句柄。`fd == -1` 只表示本对象不再操作该数字；可靠关闭依据来自实际关闭结果。重交付同一保存错误不代表新发生了一次错误，最终诊断不重复登记它。

### 原事务错误的有序交付

原写事务内核是回滚结果的唯一来源。操作失败且回滚成功时，`WriteReceipt.error` 保留原异常，结果为 `rolled_back`；回滚再次失败时，保留 `[原操作错误, 回滚错误]` 的有序复合事实，结果为 `unknown`。普通异常可以采用有序 `ExceptionGroup`；控制异常保持原对象并附属交付次因，不能因为收场而改成普通异常。

`WriteReceipt → DbOutcome → AcceptanceStateError → SessionOutcome` 保留这一顺序。受理包装用 `__cause__` 交付原异常或原复合异常，并保持 `outcome_kind`；两个仓储直接消费同一事务事实。会话按首个实际原因的类型和责任分类，递归展开内部有序复合事实时保持先后，最终 `secondary_errors` 不嵌套。原锁错误后的回滚数据库错误不能成为新的主错误。

同步锁回调的内部控制异常也必须结束其实际事务责任。事务已开始且命令或 `complete_result` 交付非 `Exception` 的 `BaseException` 时，内核尝试一次适用回滚，随后重新抛出同一控制对象；不把它转换成普通 `DbOutcome` 或父等待取消。该对象携带类型明确的 `WriteReceipt` 事实：提交尚未开始且回滚成功时为 `rolled_back`，回滚失败时为 `unknown`，按顺序保留实际回滚次因和原附注。提交阶段已经开始后发生控制异常，仍保留原提交未知规则，适用收场不据此宣称未提交。事务尚未开始时不补造回滚操作。此统一边界覆盖普通取得命令、正常关闭命令及 `submit` 的结果回调，不另造三份控制异常规则，也不重发原命令。

回滚操作本身首次产生控制异常时，也依控制优先级传播原对象，并保留原操作错误及事务未知事实。内层已经尝试的回滚不能因控制对象经过外层捕获而再次执行；已有最先控制对象时，回滚的新异常仍为其余事实。

无事件命令的正常事务结束同样只执行一次 `ROLLBACK`。结束已确认时交付 `completed`；结束本身出错时交付 `unknown`，不再次发送 `ROLLBACK` 来制造确认结果。普通结束错误作为原事务错误交付，控制错误保持同一对象及未知事实。已取得的锁租约仍独立收场，普通流程为零，此前已提交的受理和下界保持。

确认回滚表达 `transaction_status: not_committed`，回滚或提交未知表达 `unknown`。状态只描述发生错误的那次事务，不否定此前受理或可信下界已经提交的事实。相关状态必须在机器结果中断言，不能只检查包装异常文字。锁操作没有关联状态事务时，不为它补造事务状态。

### 普通接纳资格的取得事务

正常 `run` 先完成原输入受理、时钟检查及适用下界保存，再在会话锁内开启独立的短交接写事务，执行原非阻塞接纳锁取得，可靠结束事务后才进入普通流程。当前默认入口直接调用锁端口，必须将这一取得点接入事务；不把此前已经提交的受理和下界改成同一事务，初始受限路径仍不取得普通接纳资格。

复用 `SessionRepository` 及 `commit_operation` 的无事件资格命令：命令在原 `BEGIN IMMEDIATE` 内调用原取得端口，返回 `read_only=True` 的 `CommandPlan`，沿已有可靠事务结束路径交付 `DbOutcome`。该命令不分配业务身份或历史事件。会话传入的取得回调在取得租约的同一同步调用中，立即把租约保存到外层清理变量；不能等仓储返回成功才登记。若后续事务结束失败，已取得的实际租约仍由本次会话独立释放，普通流程为零，事务错误及此前已提交事实分别保留。

建议内部接口为 `SessionRepository.acquire_admission(command: AcquireAdmission, key, owned)`，命令携带会话的取得并登记回调；`SessionRepositoryPort` 和所有相关测试替身同步此责任。纯内存替身调用同一回调再交付受控 `DbOutcome`，不访问真实数据库；生产不设置绕过该事务的兼容分支。正常 `submit` 的探测及正常 `run` 的释放继续在原有写事务内完成。

### 会话最终收场的错误合成

保留 SW-04 的必要阶段及实际本地／日志结果消费；完成这些责任后，逐项尝试关闭已取得的接纳租约、会话租约及数据库连接。每项收场独立，前一项出错不跳过后一项；日志及报告监督器继续由原生命周期分别收场，辅助锁不改成会话锁错误。

| 收场前的实际结果 | 后续处理 |
| --- | --- |
| 会话成功，最终锁错误 | 所有资源均得到收场机会，返回一个 `lock_error`，已提交业务事实保持。 |
| 会话成功，数据库连接关闭错误 | 返回所属数据库错误，其他资源仍独立处理；锁错误不能被连接错误遮盖。 |
| 只有报告错误，随后发生锁等致命错误 | 致命错误主导退出，原报告错误为次因，保持既有报告责任。 |
| 已有已分类致命会话错误，收场没有控制异常 | 保留原主错误，各项新错误按发生次序作为平坦次因。 |
| 已保存首次父取消或原内部控制异常 | 完成每项实际收场后，传播同一原对象；所有实际收场错误及已有附注作为次因，不改成普通会话成功或新的取消对象。 |
| 此前没有控制异常，收场本身发生控制异常 | 无论此前是成功还是普通会话错误，都保存首个实际控制对象，继续其余资源收场，再传播；此前错误诊断和后续实际错误按发生顺序作为有限附注或类型事实保留，已提交状态保持。 |

消费者区分同步锁操作的内部控制事实与等待协程取消，保存原对象及有限附注。父取消先于本次同步控制成立时，仍保持首次父对象；同一保存错误被租约再次交付时不重复附注，也不让来源与目标为同一对象的附注遍历无限增长。

上述身份保持覆盖本项审计的同步锁、事务控制和最终同步资源关闭。同步来源由锁模块或事务内核的类型事实确认；不能把锁回调交付的 `CancelledError` 登记成父等待取消。已保存同步控制对象之后，父取消仍执行原停止新增工作和必要收场，但不替换先前同步对象。父取消先到时，则保持首次父对象，把后到同步控制按序附属交付。未在本项审计的异步文件、报告及日志 owner（实际责任持有者）仍沿 SW-04 的实际任务事实和父等待结果规则消费，不因这次同步边界修复批量重分类。

CLI 已建立装配资源后，控制异常退出也须进入 `close_runtime`，消费装配持有的报告失败日志副本通道及适用普通日志兜底关闭。该关闭责任不依赖 `run_session` 交付正常结果。同步关闭每项独立执行，普通辅助通道错误继续按原日志诊断规则表达；已有控制对象保持原对象，后错有限附属交付，新生控制则在完成其余适用关闭后传播。此边界不把辅助日志锁或通道错误改成会话 `lock_error`。

`execute_command` 关闭报告监督器、受限报告监督器和普通日志时，沿用同一控制选择：原会话控制保持；原会话已经返回结果而关闭首次产生控制时，在该错误实际交付处保存对象，继续其余关闭，再传播该对象。后一个监督器或日志的控制异常不能覆盖它。原会话失败结果及实际后错保留为有限附注；报告和日志业务任务内部的分类继续遵守原责任规则。

首个监督器关闭仅产生普通错误、后续关闭首次产生控制异常时，同样传播该控制对象，并附属保留先前普通错误。CLI 关闭通知管道时也遵守这一顺序：原控制对象保持，后续关闭错误进入诊断；尚无控制对象而关闭首次产生控制时，保留该对象及先前普通错误。

| 已有控制事实及后来到达的来源 | 交付与实际责任 |
| --- | --- |
| 同步锁或事务的可信控制已保存，必要收场等待期间父取消到达 | 原同步控制对象仍主导传播；父取消诊断附属保留，并触发原停止新增工作。必要日志、本地责任和已取得资源完成适用收场后才传播。 |
| 父取消已保存，必要阶段后来交付同步控制 | 首次父对象保持；同步操作事实和错误附属保留，所有实际必要责任完成。 |

### 原应急资格及初始化消费者

会话只在本次会话租约确已取得、原应急回调存在且实际非时钟致命结果成立时进入既有必要应急阶段；具体录像的归属、排他资格、驱动及预算继续由原停止消费者判断。会话锁取得失败为零应急调用、零设备动作、零输入受理及零启动检查。正确分类后的接纳取得或正常关闭锁故障不能绕过既有每目标资格检查。

会话租约条件对每种错误分类统一适用。上述非时钟致命结果包括实际 `state_db_error`、`configuration_error`、`lock_error` 及 `internal_error`；报告错误和单独的时钟异常继续按各自有限收场处理。普通锁程序异常不能因被准确分类为 `internal_error` 而跳过原目标资格核对。

晚到时钟失信交接中，业务本地责任完成后关闭接纳锁失败，应在该交接消费者形成实际非时钟锁错误，阶段为 `handoff`，并保留相关时钟诊断；该关闭不属于状态库事务，不添加事务状态。不继续依赖关闭成功的有限受限步骤；会话锁仍持有时沿同一必要应急阶段收场。最终资源关闭已经位于必要业务及日志消费之后，不从该阶段再建立新的停止流程。原应急预算、目标规则、次数保存和单次阶段保持。

该交接中实际关闭若产生控制对象，则保留同一对象及可信来源，将已知时钟诊断附为有限说明后继续必要收场。父取消先到时，这条说明随同步控制事实附到首次父对象；同步控制先到时，说明保留在原同步对象上。

`initialize_state` 也消费同一租约结果。先保存原初始化结果，再独立消费会话租约关闭事实；没有控制异常时，可处理的锁失败返回 `InitResult(FAILED, ...)`，已有初始化错误仍为主并保留关闭次因。原控制异常保持原对象并完成适用关闭；此前没有控制异常而释放首次产生控制对象时，无论原初始化是成功还是普通失败，都传播该原控制对象，并按顺序保留原初始化诊断和其他实际错误。CLI 继续退出码 `1`、stderr 诊断、空 stdout 的初始化契约；不加入会话 JSON 协议。库若已经完整创建或绑定切换已提交，释放失败不撤销这些事实，再次 `init` 仍验证实际已有库。

`InitResult.error` 保留实际主错误对象；新增默认空元组 `secondary_errors`，按发生顺序保存后续普通错误对象，使既有三参数构造兼容。原失败只有诊断而没有错误对象时，原诊断仍为主，后续关闭错误进入该元组。原成功后首次关闭失败成为主错误；CLI 的 stderr 诊断同时表达实际关闭错误，保留各错误的类型化上下文。控制退出不构造普通失败结果，原初始化诊断和后错通过有限附注保留。

初始化取得锁时，普通程序异常沿现有 `InitResult(FAILED, ..., error=原异常)` 返回契约消费，原对象保存在 `result.error`；控制异常则保持原对象传播。未取得租约时不补造释放操作，原文件和库事实保持。这里的初始化结果契约与 `run`／`submit` 的会话结果分别验证。

### 日志关闭期间的取消

日志关闭须区分“已经开始一次关闭”和“实际关闭已经结束”。`close_attempts` 只表达前者，不能据此跳过仍在途的监听线程停止、文件处理器或队列关闭。普通日志和已停用文件日志使用同一个关闭任务持有实际结果；等待方取消不取消该任务，不重新发送已开始的停止或关闭操作。

| 发生位置与已知事实 | 关闭责任与传播 |
| --- | --- |
| 等待监听线程停止、文件处理器关闭或队列结束时，首次父取消到达 | 保存该控制对象，继续等待原实际操作，再完成后续适用关闭，最后传播；重复父取消不创建新操作。 |
| 关闭操作内部先产生控制异常，随后父取消到达 | 保存原内部控制对象，后来的取消及关闭错误进入有限附注，完成其他适用关闭后传播原对象。 |
| 监听线程停止返回普通或控制错误 | 保留未确认停止事实；普通分支保持既有处理器关闭机会，文件日志停用分支只在可靠确认监听结束后关闭处理器；两者仍独立关闭队列并消费结果。 |
| 已有关闭任务时再次调用关闭 | 等待同一任务并消费保存的实际结果，不重复摘要、线程停止或句柄关闭。 |

先以真实监听线程及同步点覆盖两个日志分支的父取消、重复取消和后续关闭错误，再修复共享关闭边界。默认 CLI 对照核对其原控制对象、失败副本通道及普通日志资源。此处不改变普通日志故障的辅助诊断职责，也不扩展会话业务锁的持有期。

### 补充验收入口

以下专项与原任务的真实组合一起执行，不用内部字段断言替代实际资源与事务结果。

| 建议测试位置及方法 | 必须证明的事实 |
| --- | --- |
| `unit/session/test_locks.py::test_inheritance_failure_closes_opened_handle`、同文件 `test_acquire_conflict_keeps_conflict_type_when_close_is_unconfirmed` | 设置失败／真实冲突后关闭也失败，首因、原对象或专门类型、实际 path/errno 与一次关闭均保留；探测没有成功返回。 |
| 同文件 `test_failed_lease_close_is_not_retried_or_reported_success` | 未确认关闭后再次调用不重复系统操作，重交付原错误及实际关闭事实。 |
| 同文件 `test_cleanup_exception_priority_preserves_actual_sources` | 获取失败后关闭、探测收场和租约释放分别注入首普通错误及关闭 `RuntimeError`／预建控制对象；普通错误仍以首因为主，控制原对象传播。类型访问器交付对应实际主操作、原对象、此前错误及一次关闭，租约重复关闭不重试且重交同一主异常。已有首控制的对照保持最初对象。 |
| 同文件 `test_conflict_close_internal_error_keeps_close_source` | session／admission 取得真实冲突后关闭抛程序或控制异常，类型访问器取得 `operation=close`、同一原异常及冲突、未确认关闭事实；没有重复关闭次因或额外系统调用。既有关闭 `OSError` 的冲突子类对照保持。 |
| 同文件 `test_probe_conflict_close_internal_error_keeps_close_source` | 探测已观察冲突，随后关闭抛程序或控制异常时，没有探测结果；原对象、实际 `close` 根来源、冲突和未确认关闭事实保持，没有解锁或重复关闭次因。 |
| `integration/session/test_lock_error_classification.py::test_run_lock_program_error_is_internal_error`（新增） | 会话取得、接纳取得及正常关闭分别由真实锁端口交付预建 `RuntimeError`。锁事实保留原对象，会话结果为 `internal_error`，实际阶段、路径、相关事务状态保持；其他资源仍收场，同一保存错误再次交付不重复诊断。 |
| 同文件 `test_submit_lock_program_error_keeps_cause_and_transaction`（新增） | 真实受理事务中的探测交付预建普通程序异常，包装 cause 保留原对象；回滚确认／失败分别为未提交／未知。程序首因为 `internal_error`，回滚数据库错为平坦次因，请求、ACK 和历史事实保持；不会外溢而失去会话结果。 |
| 同文件 `test_close_errors_keep_order_before_rollback_error_and_not_duplicated`（新增） | 正常关闭先发生解锁错误，再发生句柄关闭错误，最后回滚失败；机器次因按关闭、回滚的实际先后排列。异常组里的原锁错误在最终租约关闭时重交，不新增重复次因或系统调用，其他资源仍收场，原提交事实保持。 |
| `integration/session/test_lock_error_classification.py::test_lock_failure_and_rollback_failure_keep_unknown_transaction` | 真正受理或关闭写事务保留首 LockError、原 OSError 与回滚 SQLite 错误；机器结果 `lock_error`、`transaction_status=unknown`、平坦数据库次因，原提交事实保持。回滚成功的对照为 `not_committed`。 |
| `integration/bootstrap/test_session_lock_errors.py::test_control_exception_survives_all_cleanup_errors` | 父取消及同步锁内部控制两个来源分别参数化；原对象保持，每个已取得资源关闭一次，后错及原附注保留，本地／日志责任先结束。原会话成功／普通致命结果后，最终关闭首次抛控制对象的对照也传播该对象并保留原诊断。 |
| 同文件 `test_lock_control_and_parent_cancel_keep_first_source`（新增） | 晚到时钟转受限时，实际接纳关闭交付带类型事实的预建内部控制；仍在途的启动日志责任使最终必要收场等待。同步控制先到／父取消先到分别断言对应同一首对象、后到事实、实际日志与全部已取得资源结束，最终资源阶段没有新应急停止。保留 SW-04 异步文件 owner 与父取消的现有对照。 |
| 同文件 `test_cli_control_closes_copy_channel_without_reclassification`（新增） | CLI 已建立报告失败日志副本通道，实际同步锁或最终资源控制异常退出仍关闭该通道及适用普通日志资源。关闭再失败时，主控制对象和附注保持，其他资源仍得到机会；普通辅助关闭错误只进入原日志诊断，不产生会话锁错误或第二条机器结果。 |
| 同文件 `test_cleanup_lock_error_does_not_skip_other_resources` | 一个或多个锁关闭错误不跳过数据库连接及另一租约；原成功、报告错误及已有致命错误分别得到上述主／次分类。 |
| 同文件 `test_failed_session_acquire_never_enters_emergency_settlement` | 实际会话锁失败，原应急回调、设备、输入及启动检查均零调用。 |
| 同文件 `test_late_clock_lock_failure_uses_original_emergency_qualification` | 转受限时实际关闭错误成为锁错误，相关时钟诊断保留；会话锁持有期间仅推进原资格的唯一应急阶段，不进入后继报告。 |
| `integration/bootstrap/test_initialization.py::test_release_oserror_keeps_completed_initialization_facts`、同文件 `test_cli_init_reports_release_lock_error_only_on_stderr`、`test_cleanup_control_after_result_keeps_same_object_and_diagnostics` | 实际锁释放错误经 init 结果／CLI 退出码和 stderr 收口，stdout 为空；已创建库的身份、历史和文件保持，再次初始化按已有库验证。原初始化成功／普通失败后释放首次抛控制对象的对照保持该对象、原诊断及实际库事实。 |
| `integration/session/test_lock_error_classification.py::test_run_acquires_admission_inside_short_transaction` | 实际取得端口观察到连接处于写事务，普通流程只在事务可靠结束后进入；受理和下界事实保持，没有新历史事件或重复加锁。 |
| 同文件 `test_acquired_lease_is_closed_when_handoff_transaction_fails` | 取得真实租约后注入正常 `ROLLBACK` 结束错误；结束只尝试一次，事务为未知，普通流程零调用，已登记租约仍关闭，原数据库错误及此前提交事实保留。 |
| `integration/persistence/test_transactions.py` 中的事务控制专项（实施时同步实际文件与方法名） | 命令及结果回调在已开始事务中交付预建控制对象，实际回滚成功／失败分别交付原对象、确认回滚／未知状态及有序次因；提交阶段控制的对照保持未知，不把后续回滚当作未提交证据。原操作先抛普通错误、回滚本身再抛预建控制对象的对照须断言同一控制对象、原操作错误附属保留、事务为未知，以及内外层合计只执行一次回滚。 |
| `integration/session/test_lock_error_classification.py::test_transaction_control_keeps_original_object_and_closes_lease` | 原取得／关闭／探测的同步锁控制异常经过真实写事务后保持同一对象，实际事务责任结束或保留未知，租约立即登记且其他资源独立关闭，普通流程或成功接管结果为零；父等待取消另作对照。 |
| `integration/bootstrap/test_session_lock_errors.py::test_body_success_keeps_first_worker_close_control` | 实际会话已成功返回后，两个监督器关闭分别产生控制错误，或先产生普通错误再产生首个控制错误。保持首个实际控制对象及其他错误，两个监督器和日志都得到收场机会，连接关闭且两把锁可重新取得。 |
| 同文件 `test_cli_notification_close_keeps_original_control` | CLI 实际锁操作原控制退出后，真实通知管道关闭再抛普通或控制异常。原控制对象及附注保持，通知 fd 实际关闭一次，装配日志与连接已经关闭，stdout 为空。 |

## 错误分类及恢复表

| 实际发生的条件 | 会话结果与事实 | 后续处理 |
| --- | --- | --- |
| `run` 会话锁或接纳锁打开、不可继承设置、加锁失败 | `lock_error`，保留锁阶段、实际原因；未取得相应资格。 | 不继续依赖该资格的工作；关闭已取得的其他资源。 |
| `run` 会话锁／接纳锁真实冲突，当前非阻塞获取无法完成 | `lock_error`，诊断明确为冲突而非系统故障；不声称持有锁。 | 不新增等待或设备控制；主程序按既有进程协议处理。 |
| `submit` 有工作，探测接纳正常冲突 | 受理事务可靠提交后成功，`needs_run=false`。 | 既有接纳者承担接管责任；不是错误。 |
| `submit` 有工作，临时锁取得、解锁和关闭全部可靠完成 | 受理事务可靠提交后成功，`needs_run=true`。 | 不继续持有接纳锁。 |
| `submit` 无待处理工作 | 原事务成功，`needs_run=false`，无需探测。 | 不为了分类修复增加锁操作。 |
| 探测或正常关闭接纳发生锁故障，事务可靠回滚 | `lock_error`，本次事务 `not_committed`，不输出成功接管结果。 | 保留原业务状态；关闭已取得句柄。 |
| 锁故障之后，回滚又失败／结果未知 | 首个锁故障保持主错误，数据库故障作为后续错误，事务状态为 `unknown`。 | 不声称受理未发生；同请求后续按持久化状态核实。 |
| 实际数据库打开、查询或提交失败，未先发生独立锁故障 | `state_db_error`，保留实际事务结果。 | 不把数据库错误改成锁错误；执行原有有限错误收场。 |
| 锁责任边界发生非预期内部异常且仍可控制退出 | `internal_error`，保留原类型与诊断。 | 不以宽泛捕获转换为数据库错误；不扩大到其他未审计业务异常。 |
| 原会话成功，最终锁释放或句柄关闭失败 | `lock_error`；已提交业务事实保持。 | 后续其他资源仍须关闭，不宣称原锁已经可靠释放。 |
| 原会话已有已分类致命错误，退出时再发生锁或连接错误 | 原主错误保持，附加实际后续错误；原报告错误的对照按前述合成模型判断。 | 按资源分别完成清理；不覆盖原诊断。 |

重点复核：不可继承设置后泄漏句柄、探测释放失败伪造空闲、事务包装丢失原类型、解锁及关闭双失败、一个收场错误阻止其他资源关闭。以下任务覆盖每个条件。

## 任务一：锁端口及句柄完整失败测试

**消费接口：** `LockBackend.open/try_lock/unlock/close/set_inheritable`。

**交付接口：** 原锁 API，所有预期系统故障为带真实上下文的 `LockError`，冲突类型保持，租约不假称已关闭。

- [x] 扩展 `unit/session/test_locks.py`，新增 `test_inheritance_failure_closes_opened_handle`、`test_lock_failure_keeps_close_failure_secondary`、`test_probe_close_failure_never_reports_a_result`、`test_lease_unlock_and_close_errors_are_both_preserved`。分别覆盖 session、admission、probe 和 release 入口。
- [x] 保留正常冲突、正常取得后释放、锁文件已存在但未加锁、不可继承及重复正常关闭的原测试。加入失败关闭的重复调用反例：不能重复关闭可能已被系统复用的数字句柄，也不能把未确认结果描述为可靠关闭。
- [x] 运行专项保留语义失败；随后统一打开后所有出口的句柄处理，确保 `set_inheritable` 失败也有收场，`finally` 中的关闭错误不覆盖取得／解锁错误。
- [x] 分别记录释放资格及句柄关闭的实际结果；正常二次关闭不重复调用系统，失败或未知关闭保持诊断且不授予新资格。不按猜测重用或重试同一 fd。
- [x] 复跑受影响单元专项并审计两种后端；完整单元目录在最终门禁统一执行。Windows 替身只证明相同接口分类，Linux 实际锁在任务三另验。

## 任务二：受理和关闭事务的分类传递

**消费接口：** 任务一的类型化锁错误、`DbOutcome`、`AcceptanceStateError` 及 `SessionOutcome`。

**交付接口：** `run_session`、`_submit_session`、`_try_close` 的准确分类与实际事务状态；既有成功接管模型保持。

正常关闭的锁失败必须经过完整会话才能形成机器结果。失败租约在最终关闭时会重交同一保存错误，因此本任务同时接入上述逐资源错误汇总消费者；不能用一次性失败的测试租约避开真实重复交付，也不能等任务三才使任务二的错误结果可观察。任务三随后验证全部资源、默认 CLI、应急和初始化的完整组合。

- [x] 新增 session 集成测试 `test_session_open_failure_is_lock_error`、`test_admission_acquire_failure_is_lock_error`、`test_submit_probe_failure_rolls_back_with_lock_error`、`test_submit_conflict_remains_success`、`test_close_admission_lock_failure_is_not_database_failure`、`test_lock_failure_and_rollback_failure_keep_unknown_transaction`。
- [x] 真实受理仓储和事务内核执行失败注入，直接比较请求、动作和历史行；正常冲突用真实锁或符合端口的返回值，不以通用 `OSError` 伪造冲突。实际数据库查询错误作对照，仍为 `state_db_error`。
- [x] 先运行专项保留错误分类失败；随后保留受理原异常、回滚结果和必要后续错误，在会话锁边界分类。不要给所有数据库异常套 `lock_error`，不要按中文文案识别错误。
- [x] 把普通接纳取得接入上述短写事务，取得时立即登记实际租约；事务可靠结束后才进入普通流程，结束失败仍独立关闭租约。同步仓储端口和相关纯内存替身，验证无新增历史及已有提交保持。
- [x] 修正关闭事务的结果消费；释放失败时不把 `close_admission` 当成功。已释放后数据库结束失败的路径保持接纳关闭事实，不为了回滚重新取得接纳锁。
- [x] 同时实现 run／submit 最终同步资源的独立收场与错误汇总，使真实受理和关闭事务错误能够返回完整会话结果；相同保存错误只交付一次诊断，程序及控制对象按同一可信来源模型保留。先保持 SW-04 必要责任顺序，任务三再覆盖全部失败矩阵与默认装配。
- [x] 审计 run／submit 的所有取得、探测、正常释放及异常释放入口，包括 SW-04 最小错误会话。只修改相同锁不变量的分支；未审计的其他内部异常不随本任务批量重分类。

## 任务三：最终收场与默认 CLI 验证

**消费接口：** 前两任务的错误事实、既有本地责任收场与默认装配。

**交付接口：** 原会话结果加真实后续错误；所有已取得资源独立收场，最终机器结果只有一条。

- [x] 在新增 bootstrap 文件加入 `test_default_run_lock_failure_has_zero_business_effects`、`test_cleanup_lock_error_does_not_skip_other_resources`、`test_original_error_survives_later_lock_error`、`test_cli_emits_one_lock_error_result`。通过真实锁端口系统调用注入，不直接替换整个 `run_session` 返回预制结果。
- [x] 未取得会话锁时核对无受理新历史、无设备调用、无启动清理；取得接纳失败时保留此前已提交的受理及下界事实，但不进入普通设备工作。
- [x] 复核并补齐任务二已经接入的收场汇总，使 admission、session、数据库连接和装配日志各自得到处理机会。保持先完成本地实际责任再释放锁；原取消继续传播，收场错误通过附注保留，不能强行制造正常成功结果。
- [x] 在真实 Linux 进程持锁时运行第二入口，确认冲突分类，原持锁者不受影响；探测真实接纳锁冲突仍产生成功 `needs_run=false`。句柄不可继承和正常释放继续通过现有 `integration/session/test_locks.py` 核验。
- [x] 核对报告工作锁失败仍为报告职责、日志协调失败仍是辅助诊断；本任务的会话分类不能覆盖这两个责任。
- [x] 核对 CLI 控制退出必经装配资源收场；副本通道和普通日志兜底各独立得到一次适用关闭，原控制对象及辅助诊断保持。
- [x] 明确会话租约取得事实作为应急前提，核对接纳取得、晚到时钟交接和正常关闭的实际锁错误沿原每目标资格；最终资源释放阶段不新建应急任务。
- [x] 复验 `persistence/initialization.py` 的同一锁端口消费者及 `integration/bootstrap/test_initialization.py`，确认锁端口归一不改变显式建库、既有库身份和重复初始化结果；不把 `init` 加入 run／submit 结果协议。
- [x] 分别执行 session、acceptance、persistence、bootstrap 目录，复验 SW-01 时间状态与 SW-04 的锁内清理边界。

## 验收命令与收口

从仓库根执行，新增文件在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/session/test_locks.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/session/test_lock_error_classification.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_session_lock_errors.py -q
```

完整门禁分别执行 `unit`、`integration/session`、`integration/acceptance`、`integration/persistence`、`integration/bootstrap`；真实 CLI 输出验证在上述 bootstrap 文件中完成。必要时调整相关旧测试的锁故障预期，但数据库故障对照不能放宽。

- [x] 复核全部锁入口、事务包装、原错误先后与句柄收场；正常接纳冲突、回滚成功及未知结果均有独立证据。
- [x] 记录红绿结果、实际锁与资源证据、环境、命令和退出码；独立复核后更新路线图 SW-05，形成独立修复提交。
- [x] 下一项为[SW-06 读取分批发现](2026-10-13-camctl-sw06-bounded-read-discovery.md)，其默认会话与取消验证沿用修复后的锁及收场边界。

2026-10-14：SW-05 已完成。Root 已核对全部修改文件的生产者、消费者、规格和相关测试，并闭合监督方与通知管道关闭的控制优先级。完整独立复核发现日志关闭取消窗口，Root 按同一实际关闭任务及补充状态表完成一次修复，普通／停用两分支、重复取消、后错先后和真实 CLI 均有运行证据。

最终 `unit`、`integration/session`、`integration/acceptance`、`integration/persistence`、`integration/bootstrap` 分别为 5230、138、258、123、1134 项通过；另验完整日志目录 112 项、模块契约 438 项、报告工作锁单节点 1 项通过。两个跳过及三条既有警告见[正式验证记录](../../camctl/verification.md#sw-05-会话锁与退出收场验证2026-10-14)。最终八个进程退出码均为 `0`，18 个修改 Python 文件在完整门禁前后摘要一致。

原始红绿输出、命令、环境、进程、耗时、冻结清单及 Root 与独立复核记录保存在工作区忽略的 `.superpowers/sdd/2026-10-14-camctl-sw05-session-lock-errors/`。本项提供本地软件证据，下一项按串行顺序实施 SW-06；真实相机、ARM 部署和物理断电按各自计划验收。
