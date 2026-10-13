# SW-05：会话锁错误分类与句柄收场实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务执行。先以失败测试锁定实际错误类别和副作用，再修正端口及消费者；独立复核真实锁和会话输出。

**目标：** `run`／`submit` 按实际发生的锁、数据库或内部故障报告错误；正常接纳冲突仍参与成功接管判定，句柄收场不掩盖首个错误或跳过其他资源。

**组织建议：** 锁端口统一形成带原始原因的 `LockError`，会话按责任边界分类。事务包装保留原异常及回滚结果，所有退出在实际本地责任结束后完成锁和连接收场，并保留后续错误。

**技术基础：** Python 3.11、既有 `LockBackend`、Linux `flock`、受理／关闭事务及 `SessionOutcome`。

**设计依据：** [会话错误标识](../../architecture/session-errors.md#错误标识)、[诊断字段及错误先后](../../architecture/session-errors.md#诊断字段)、[事务内接管](../../architecture/protocol-session.md#submit-的事务顺序)、[正常关闭接纳](../../architecture/protocol-session.md#run-正常关闭接纳)、[启动顺序](../../architecture/cli-commands.md#run-的启动顺序)。本计划承接[路线图 SW-05](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，编写及建议实施顺序在[SW-04](2026-10-13-camctl-sw04-startup-log-sweep.md)之后。

## 当前入口与文件责任

2026-10-13：默认装配把 `session/locks.py::acquire_session` 直接交给会话，`session/service.py::_run_session` 却把任何取得会话锁异常转为 `state_db_error`；接纳锁取得也有相同转换。锁端口已有 `LockError` 和冲突子类，但 `_open_lock_file` 的不可继承设置、清理关闭及 `_Lease.close` 的异常没有完整归一，关闭还可能覆盖先前解锁错误。

`submit` 的探测由受理事务内核执行，原错误可保存在 `DbOutcome.error` 中；`accept_input` 当前只构造 `AcceptanceStateError` 文本及 `outcome_kind`，没有把原异常交给会话分类。正常关闭事务的锁释放错误也被 `_try_close` 当成状态库错误。最终 `finally` 中一个锁关闭异常可能阻止另一把锁及数据库连接关闭，并外溢至 CLI，失去结构化会话结果。

路径相对于 `apps/camctl`，实现组织可调整，但不改变下列责任。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/session/locks.py` | 打开、不可继承、取得、探测、释放及关闭的完整错误事实与句柄归属。 |
| `src/camctl/session/service.py` | 锁取得、输入异常、关闭事务及最终收场的准确分类和错误合并。 |
| `src/camctl/acceptance/service.py`、`persistence/transaction.py` | 保留受理事务中原锁错误及实际回滚结果；不把数据库未知解释为受理未发生。 |
| `src/camctl/persistence/repositories/session.py`、`acceptance.py` | 原异常随已有 `DbOutcome` 传递，不丢失锁错误来源。 |
| `src/camctl/bootstrap/lifecycle.py`、`cli.py` | 审计 SW-04 早期错误路径、日志及默认输出；避免已可处理的锁错误外溢。 |
| `src/camctl/persistence/initialization.py` | 审计同一会话锁端口的 `init` 消费者，保持显式初始化和重复初始化的原结果契约。 |
| `tests/unit/session/test_locks.py` | 完整端口异常分类及句柄关闭测试。 |
| `tests/integration/session/test_lock_error_classification.py`（新增） | 真实受理／关闭事务与锁端口的组合分类。 |
| `tests/integration/bootstrap/test_session_lock_errors.py`（新增） | 默认装配及真实 CLI 的退出分类、锁持有、零前置副作用。 |

## 全局约束与接口

- 保持稳定锁文件和内核锁，不删除重建锁文件，不用文件存在性判定持有状态。句柄不可继承，未取得会话锁零受理、零设备工作、零 SW-04 扫描。
- `submit` 不取得会话锁；只有实际 `CONFLICT` 表示已有接纳者，`ACQUIRED_AND_RELEASED` 表示本次真正取得并成功释放。系统错误和释放未确认都不能伪造二者。
- 维持现有非阻塞锁获取方式。本任务不增加等待预算、自动重试或新公共错误码；会话锁冲突保留专门类型和冲突诊断。取得失败导致本次 `run` 无法履职时输出 `lock_error`，不能改成数据库损坏。
- 锁端口签名保持 `acquire_session(path, backend=None) -> SessionLease`、`acquire_admission(path, backend=None) -> AdmissionLease`、`probe_admission(path, backend=None) -> AdmissionProbe` 和租约 `close(...) -> None`。失败抛 `LockError`，冲突保持其专门子类；保留实际阶段、路径、errno、原异常及已确认关闭事实。
- `accept_input` 保留原 `DbOutcomeKind` 并通过异常原因传递原错误，例如 `AcceptanceStateError` 的 `__cause__` 指向原异常。事务回滚又失败时保留原操作错误和回滚错误，建议使用有序异常组；不可只保留最后一个错误，也不以异常文本匹配类别。
- 成功的锁探测必须发生在原受理写事务内；正常释放接纳仍在最终工作重查事务内。改变错误分类不能移动这些副作用或把它们放到提交成功之后。
- `ROLLED_BACK` 表示本次事务未提交，`UNKNOWN` 保留未知；锁错误不能让未完成受理输出成功 `needs_run`，也不能撤销之前已提交事务。
- 结果使用已有 `reason` 与 `details`；阶段只使用已定义的 `locking`、`handoff`、`shutdown` 等值。首个致命错误为主，后续真实错误进入无嵌套的 `secondary_errors`。辅助日志锁及独立报告工作锁仍按其原责任分类。
- Python、测试替身与运行隔离遵守[组件测试指南](../../../apps/camctl/tests/AGENTS.md)。Linux 真实锁测试证明开发环境行为，不替代 ARM 部署结论。

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
| 原会话已经失败，退出时再发生锁或连接错误 | 原主错误保持，附加实际后续错误。 | 按资源分别完成清理；不覆盖原诊断。 |

重点复核：不可继承设置后泄漏句柄、探测释放失败伪造空闲、事务包装丢失原类型、解锁及关闭双失败、一个收场错误阻止其他资源关闭。以下任务覆盖每个条件。

## 任务一：锁端口及句柄完整失败测试

**消费接口：** `LockBackend.open/try_lock/unlock/close/set_inheritable`。

**交付接口：** 原锁 API，所有预期系统故障为带真实上下文的 `LockError`，冲突类型保持，租约不假称已关闭。

- [ ] 扩展 `unit/session/test_locks.py`，新增 `test_inheritance_failure_closes_opened_handle`、`test_lock_failure_keeps_close_failure_as_secondary`、`test_probe_close_failure_never_reports_free`、`test_lease_unlock_and_close_errors_are_both_preserved`。分别覆盖 session、admission、probe 和 release 入口。
- [ ] 保留正常冲突、正常取得后释放、锁文件已存在但未加锁、不可继承及重复正常关闭的原测试。加入失败关闭的重复调用反例：不能重复关闭可能已被系统复用的数字句柄，也不能把未确认结果描述为可靠关闭。
- [ ] 运行专项保留语义失败；随后统一打开后所有出口的句柄处理，确保 `set_inheritable` 失败也有收场，`finally` 中的关闭错误不覆盖取得／解锁错误。
- [ ] 分别记录释放资格及句柄关闭的实际结果；正常二次关闭不重复调用系统，失败或未知关闭保持诊断且不授予新资格。不按猜测重用或重试同一 fd。
- [ ] 复跑专项和单元目录，审计两种后端。Windows 替身只证明相同接口分类，Linux 实际锁在任务三另验。

## 任务二：受理和关闭事务的分类传递

**消费接口：** 任务一的类型化锁错误、`DbOutcome`、`AcceptanceStateError` 及 `SessionOutcome`。

**交付接口：** `run_session`、`_submit_session`、`_try_close` 的准确分类与实际事务状态；既有成功接管模型保持。

- [ ] 新增 session 集成测试 `test_session_open_failure_is_lock_error`、`test_admission_acquire_failure_is_lock_error`、`test_submit_probe_failure_rolls_back_with_lock_error`、`test_submit_conflict_remains_success`、`test_close_admission_lock_failure_is_not_database_failure`、`test_lock_failure_and_rollback_failure_keep_unknown_transaction`。
- [ ] 真实受理仓储和事务内核执行失败注入，直接比较请求、动作和历史行；正常冲突用真实锁或符合端口的返回值，不以通用 `OSError` 伪造冲突。实际数据库查询错误作对照，仍为 `state_db_error`。
- [ ] 先运行专项保留错误分类失败；随后保留受理原异常、回滚结果和必要后续错误，在会话锁边界分类。不要给所有数据库异常套 `lock_error`，不要按中文文案识别错误。
- [ ] 修正关闭事务的结果消费；释放失败时不把 `close_admission` 当成功。已释放后数据库结束失败的路径保持接纳关闭事实，不为了回滚重新取得接纳锁。
- [ ] 审计 run／submit 的所有取得、探测、正常释放及异常释放入口，包括 SW-04 最小错误会话。只修改相同锁不变量的分支；未审计的其他内部异常不随本任务批量重分类。

## 任务三：最终收场与默认 CLI 验证

**消费接口：** 前两任务的错误事实、既有本地责任收场与默认装配。

**交付接口：** 原会话结果加真实后续错误；所有已取得资源独立收场，最终机器结果只有一条。

- [ ] 在新增 bootstrap 文件加入 `test_default_run_lock_failure_has_zero_business_effects`、`test_cleanup_lock_error_does_not_skip_other_resources`、`test_original_error_survives_later_lock_error`、`test_cli_emits_one_lock_error_result`。通过真实锁端口系统调用注入，不直接替换整个 `run_session` 返回预制结果。
- [ ] 未取得会话锁时核对无受理新历史、无设备调用、无启动清理；取得接纳失败时保留此前已提交的受理及下界事实，但不进入普通设备工作。
- [ ] 改造退出收场的错误汇总，使 admission、session、数据库连接和装配日志各自得到处理机会。保持先完成本地实际责任再释放锁；原取消继续传播，收场错误通过附注保留，不能强行制造正常成功结果。
- [ ] 在真实 Linux 进程持锁时运行第二入口，确认冲突分类，原持锁者不受影响；探测真实接纳锁冲突仍产生成功 `needs_run=false`。句柄不可继承和正常释放继续通过现有 `integration/session/test_locks.py` 核验。
- [ ] 核对报告工作锁失败仍为报告职责、日志协调失败仍是辅助诊断；本任务的会话分类不能覆盖这两个责任。
- [ ] 复验 `persistence/initialization.py` 的同一锁端口消费者及 `integration/bootstrap/test_initialization.py`，确认锁端口归一不改变显式建库、既有库身份和重复初始化结果；不把 `init` 加入 run／submit 结果协议。
- [ ] 分别执行 session、acceptance、persistence、bootstrap 目录，复验 SW-01 时间状态与 SW-04 的锁内清理边界。

## 验收命令与收口

从仓库根执行，新增文件在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/session/test_locks.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/session/test_lock_error_classification.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_session_lock_errors.py -q
```

完整门禁分别执行 `unit`、`integration/session`、`integration/acceptance`、`integration/persistence`、`integration/bootstrap`；真实 CLI 输出验证在上述 bootstrap 文件中完成。必要时调整相关旧测试的锁故障预期，但数据库故障对照不能放宽。

- [ ] 复核全部锁入口、事务包装、原错误先后与句柄收场；正常接纳冲突、回滚成功及未知结果均有独立证据。
- [ ] 记录红绿结果、实际锁与资源证据、环境、命令和退出码；独立复核后更新路线图 SW-05，形成独立修复提交。
- [ ] 下一项为[SW-06 读取分批发现](2026-10-13-camctl-sw06-bounded-read-discovery.md)，其默认会话与取消验证沿用修复后的锁及收场边界。

2026-10-13：计划已编写，生产修复与业务测试尚未执行；不需要真实相机或目标主机。
