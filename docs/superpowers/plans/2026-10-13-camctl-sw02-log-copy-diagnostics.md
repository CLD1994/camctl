# SW-02：日志副本与标记失败诊断实施计划

> 供执行 Agent 使用：使用 `superpowers:executing-plans` 或已经明确指派的子任务逐项实施。先验证失败，再修改生产代码；每项由复核者核对真实回执、文件和诊断出口。复选框表示实施门禁，不表示计划已经执行。

**目标：** 标记检查、创建、进度保存及日志副本处理的实际错误进入可用诊断通道；辅助诊断不改变原报告错误、故障轮次或文件交接结果。

**组织建议：** 副本编排返回完整的标记保存结果，会话消费回执并通过装配提供的日志诊断端口输出。沿用既有普通日志和 stderr，不引入业务历史或新的副本触发入口。

**技术基础：** Python 3.11、既有 `CopyReceipt`、`MarkerSave`、异步日志通道及文件交接接口。

**设计依据：** [标记创建失败](../../architecture/log-failure-marker.md#故障标记创建失败)、[进度更新失败](../../architecture/log-failure-marker.md#故障标记的进度更新失败)、[副本失败清理](../../architecture/log-delivery.md#日志副本的失败清理)、[日志通道故障](../../architecture/logging.md#日志通道故障)、[会话错误](../../architecture/session-errors.md)。本计划承接[路线图 SW-02](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，编写顺序在[SW-01](2026-10-13-camctl-sw01-acceptance-clock-status.md)之后。

## 当前入口与文件责任

2026-10-13：`logging_runtime/copies.py::deliver_failure_copy` 保留正式标记检查、首次创建及 `COPIED`、`PUBLISHED` 两次进度保存的完整回执。`session/service.py::_trigger_failure_log` 消费这些事实及调用异常，默认诊断由 `bootstrap/lifecycle.py` 连接实际文件日志和 stderr。生产修复、专项组合、完整目录门禁及根代理与独立复核均已完成；实际结果见[验证记录](../../camctl/verification.md#sw-02-日志副本诊断验证2026-10-13)。

`CopyReceipt.outcome` 与副本实际阶段不能由标记进度代替。`MarkerSave` 保存原 `SaveOutcome`，并保留进度更新的前置 `MarkerCheck`；创建与各次进度错误独立呈现。普通会话和受限会话使用同一首次失败入口，默认报告流程在每份报告可靠登记文件失败后立即触发，并关联已知的 `report_id`。`FailureLogService.on_report_failure_with_factory` 在同一协调拥有者内组织请求构造、交付与异常后的暂停；`WriteResult.disables_file_logging` 统一提供后续文件日志的停用分类。

路径相对于 `apps/camctl`，新增内部字段、回调名和文件名是建议；若调整，须同时修改全部生产者、消费者和测试。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/logging_runtime/copies.py` | `deliver_failure_copy(request, *, publish) -> CopyReceipt` 保留完整保存回执；`FailureLogService` 保持暂停、抑制和恢复语义。 |
| `src/camctl/logging_runtime/clh_adapter.py` | 保存实际写入、轮换、复制、协调及关闭结果，提供统一停用分类；文件通道失效按既有规则收场，实际副本结果独立保留。 |
| `src/camctl/session/service.py` | `_trigger_failure_log(context, error) -> None` 消费回执和调用异常；审计普通及受限报告失败入口。 |
| `src/camctl/bootstrap/lifecycle.py` | 为 `SessionContext` 装配诊断出口，连接普通日志与可用 stderr；保留日志有序关闭。 |
| `src/camctl/bootstrap/recovery_logging.py` | 文件日志已经停用时，将后续恢复诊断交给剩余 stderr。 |
| `src/camctl/bootstrap/flows.py` | 每份报告的可靠文件失败立即进入共同副本入口，并关联已经可靠取得的报告身份。 |
| `src/camctl/logging_runtime/lifecycle.py` | 文件日志停用后，等待监听线程已经开始的操作实际结束，再完成一次适用关闭。 |
| `tests/unit/logging_runtime/test_copies.py` | 扩展现有进度失败测试，验证返回值和副本操作分类。 |
| `tests/integration/logging_runtime/test_copies.py` | 用真实目录核对进度失败后继续交付、发布失败后的文件及身份。 |
| `tests/integration/logging_runtime/test_clh_adapter.py`（新增） | 使用真实文件处理器核对错误分类、实际副本字节、停用及一次关闭。 |
| `tests/integration/logging_runtime/test_lifecycle.py` | 使用受控监听线程核对已开始写入实际结束前不能完成关闭。 |
| `tests/integration/bootstrap/test_log_copy_diagnostics.py`（新增） | 经默认装配验证实际 `run` 的诊断、错误优先级和非递归行为。 |
| `tests/integration/session/test_session.py` | 验证普通与受限入口消费回执，辅助诊断失败不阻止业务或改变原结果。 |

## 全局约束与阶段接口

- 不修改公共协议、报告 JSON、业务水位或数据库格式。日志副本不变成普通 delivery 或状态报告。
- 标记首次可靠创建是复制前提；进度保存失败单独不撤销该前提。只有实际文件交接结果可以证明副本发布。
- 已在 `ready`／`processing` 的副本不撤回、不删除、不重投。失败流程不自动重试，旧标记继续抑制重复交付。
- 每个实际错误保留发生步骤、结果分类、已知轮次／副本身份和原因。多个错误不能由最后一个覆盖；文案不逐字固定，机器枚举精确断言。
- 实际复制调用的 `WriteResult.rotation_error` 和协调释放错误同样进入回执与诊断。轮换失败但追加、复制可靠成功时仍可交付；协调失效按辅助日志通道规则处理，保留已经完成的复制及文件交接事实，不输出会话 `lock_error`。诊断不能依赖已经失效的共享文件协调条件而无限等待。
- 普通日志不可用时向仍可用的 stderr 记录；stderr 也失败则停止对该通道重复写入。不得把诊断写入 stdout，不新增诊断递归或独立重试。
- 不用可变全局收集器保存诊断。建议给 `CopyReceipt` 添加默认值字段 `marker_create: MarkerSave | None = None` 与 `marker_progress: tuple[tuple[MarkerStage, MarkerSave], ...] = ()`；保留现有位置参数及 `error` 的兼容性。保存的是实际返回分类，不重新查询来猜测原结果。
- 建议为 `SessionContext` 增加 `on_log_diagnostic: Callable[[str], Awaitable[None]] | None = None`。会话负责组织实际事实，装配负责通道写入；回调失败按日志故障处理。SW-03 可在 `MarkerSave` 中增加清理诊断，SW-04 沿用本出口，不能各自另建完整错误清单。
- 进度更新应绑定首次可靠创建的原轮次与副本身份。建议 `update_stage` 增加可选的预期轮次参数，生产副本流程始终传入原 `round_id`；标记保存与恢复删除共用会话内的同一协调拥有者。正式标记已删除或已属于新轮次时，旧更新只返回未保存事实，不重建或修改它；仅先读当前标记再无条件替换不足以关闭这一竞争。
- Python、测试隔离及运行记录遵守[组件测试指南](../../../apps/camctl/tests/AGENTS.md)；各集成目录分别执行，不要求测试必须由主 Agent 启动。

## 处理分类与复核重点

| 已完成步骤与新结果 | 副本及标记处理 | 必须输出的诊断 |
| --- | --- | --- |
| 正式标记检查错误，存在性未知 | 暂停本次运行中同故障的后续尝试，不复制。 | 检查步骤与原错误；不描述为不存在。 |
| 正式标记存在，内容不可读或无效 | 保持抑制，身份或进度未知，不复制。 | 内容错误与已确认存在的事实。 |
| 首次创建失败、已放置未同步或未知 | 保留已有或可能存在的标记，停止本次副本流程。 | 原 `MarkerSave.outcome` 与错误；不假称未创建或已可靠保存。 |
| 首次创建可靠成功，`COPIED` 进度保存失败或未知，副本自身正常 | 按实际副本条件继续发布，保留原身份。 | 进度保存结果，不能因最终发布成功丢弃它。 |
| 复制、同步或发布本身失败／未知，另有进度保存错误 | 保留原副本分类，按实际位置清理或保留；不重试。 | 所有进度错误、副本错误及已有清理错误。 |
| 文件可靠移入 `ready`，`PUBLISHED` 进度保存失败／未知 | 文件保持交接状态，不撤回；标记进度不声称可靠保存。 | 发布事实与进度保存错误分别表达。 |
| 请求工厂或副本调用抛异常 | 保留原报告及会话错误；不把异常吞掉后宣称副本成功。 | 异常发生步骤、类型和原因；未知文件结果保持未知。 |
| 诊断写入失败，其他职责仍能完成 | 不递归触发副本或终止普通设备工作。 | 向剩余可用通道记录；均不可用时保留原业务结果。 |

重点复核：失败回执仍带多个错误；`SUPPRESSED` 也可能带标记内容错误；受限报告失败仍只获得一次报告机会；状态库失效不阻止独立日志处理；通道故障不改变报告失败或设备资格。以下任务覆盖这些条件。

文件处理器须按实际失败步骤表达停用事实，不能从错误文案推断。格式化尚未触碰文件、原日志写入失败和副本自身写入失败有不同后续规则：

| 实际文件处理结果 | 通道及副本处理 |
| --- | --- |
| 格式化失败，文件操作尚未开始 | 保留记录错误及未追加、未复制的事实；不将其当作共享文件 I/O 故障。 |
| 轮换失败，协调有效，追加、刷新及复制成功 | 保留真实复制结果及轮换诊断，继续适用交付；原日志通道继续使用。 |
| 共享锁获取失败 | 不追加或复制，保存协调错误，停用本进程后续文件日志。 |
| 当前日志文件打开、追加或刷新失败 | 保存写入错误，停止副本交付，停用辅助通道及本进程后续普通文件日志。 |
| 触发记录追加及刷新成功，副本读取、写入或同步失败 | 保留副本错误及已知文件事实，按失败清理规则收场；原日志通道继续使用。 |
| 共享锁释放失败 | 保留已完成的追加、复制及交接事实，保存协调错误，停用本进程后续文件日志。 |
| 失效处理器关闭失败 | 独立保留关闭错误和未知状态，不重试关闭；已开始的监听操作继续等到实际结束。 |

辅助处理器只执行一次适用关闭。已取得共享锁的追加失败路径先完成适用解锁，再关闭处理器，不能提前关闭后制造额外的解锁错误。服务在请求构造或交付调用抛异常后暂停同轮后续尝试；可靠报告恢复解除暂停，正式标记仍按实际存在性抑制重复交付。

上述停用事实也约束后来触发的副本请求。普通日志先因当前文件 I/O 或协调失败而停用后，报告失败入口不得再为同一路径构造或复用文件处理器；请求工厂通过原异常诊断入口说明停用原因，服务暂停本轮后续尝试。验证须同时覆盖副本处理器先失效和普通日志先失效的两种先后顺序。

本次停用检查不删除或改写正式标记。下一次实际启动是否允许交付，仍按[故障标记异常后的再检查](../../architecture/log-failure-marker.md#故障标记异常后的再检查)核实正式文件；进程内暂停不形成永久禁用，正式标记不存在也不表示报告已经恢复。

## 任务一：保留副本编排的完整回执

**消费接口：** `MarkerStore.check() -> MarkerCheck`、`create(round_id, copy_name) -> MarkerSave`、`update_stage(stage) -> MarkerSave` 和既有发布端口。

**交付接口：** 扩充后的 `CopyReceipt`，带创建及按发生顺序保存的进度结果；实际副本 `outcome` 保持原定义。

- [x] 扩展 `test_progress_update_failure_does_not_block_delivery`，新增 `test_both_progress_failures_survive_published_receipt`、`test_copy_failure_keeps_prior_marker_diagnostic`、`test_marker_create_receipt_keeps_actual_non_success_outcome`。参数化覆盖各非成功 `SaveOutcome`；断言实际返回分类、阶段、轮次和错误事实。
- [x] 运行单元专项，确认当前失败来自回执丢失，保留日志。用现有内存文件和受真实接口约束的标记、发布替身，不访问真实目录。
- [x] 在 `deliver_failure_copy` 中保留创建结果及两次实际进度保存结果；各早退和成功返回均带已取得的事实。单独的进度保存错误允许正常流程继续；副本自身错误仍按原规则早退和清理。
- [x] 保留真实复制返回中的轮换错误，并让 `copy_with_write` 的协调释放异常通过结果交付，不继续吞掉。先用失败测试分别核对“轮换失败但追加及复制成功”和“复制已经完成后协调释放失败”，再验证默认诊断、通道禁用及副本事实相互独立；不增加复制或协调重试。
- [x] 按上述文件处理分类覆盖共享锁获取、文件打开、追加及刷新失败；明确的停用事实进入类型化回执与默认装配。验证当前文件日志失效后辅助及普通通道均停止后续写入，副本自身失败则保留可用的原日志通道。
- [x] 核对 `MarkerStore.check` 的内容无效分支：当前解码为 `state=None` 时没有错误文本，应给出内容无效的诊断事实，同时保持 `EXISTS` 和重复抑制。不可把内容无效转换为不存在，也不在日志中重复转储完整标记内容。
- [x] 审计 `FailureLogService.on_report_failure` 的抑制及暂停返回，保持首次标记失败的同运行暂停，后续可靠恢复才解除；不要为了收集诊断再次调用保存或复制。
- [x] 通过 `test_update_without_existing_marker_fails`、`test_old_progress_does_not_modify_new_round_marker` 和真实并发用例 `test_recovery_waits_for_original_progress_before_new_round` 验证旧进度边界。在原更新排队／写临时文件期间安排可靠恢复及新故障，核对原轮次、保存结果和正式文件字节；让创建、更新和恢复删除在同一协调拥有者下执行，不能用无条件重写新标记修复进度。
- [x] 复跑单元专项。扩展真实目录集成测试，验证 `COPIED` 和 `PUBLISHED` 保存错误后副本仍在正确目录、字节不变，并在副本自身失败时保持失败分类及所有错误。

## 任务二：会话消费与默认诊断装配

**消费接口：** 任务一的回执、`SessionContext.failure_log` 和 `copy_request_factory`。

**交付接口：** 装配后的异步诊断回调；正常及受限报告失败经过相同的非递归副本诊断处理。

- [x] 在 session 集成测试加入 `test_report_failure_consumes_copy_diagnostics`、`test_copy_factory_exception_is_diagnosed_without_reclassification`、`test_restricted_report_failure_still_clock_invalid` 及 `test_restricted_report_state_db_failure_uses_log_diagnostics`。用类型化回执覆盖上表，包括抑制但内容无效；分别断言原 `report_error`、`clock_invalid`、`state_db_error` 及原错误先后关系保持。
- [x] 在新增 bootstrap 文件加入 `test_default_run_records_marker_progress_failure`、`test_default_run_records_copy_and_cleanup_failures`、`test_unavailable_log_falls_back_to_stderr_without_recursion`。通过真实默认会话与文件日志验证步骤、原因和身份可观察；原报告失败在注入故障前已真实发生。
- [x] 运行新增专项，确认当前回执或异常没有进入诊断出口。再装配回调、消费创建／进度回执及 `CopyReceipt.error`，保留请求工厂和调用异常；所有返回分支使用相同出口。
- [x] 在受限一次报告的普通错误和状态库错误分支调用同一触发及诊断处理；不追加报告重试，不为副本取得普通接纳资格。审计默认 `report_flow` 的异常传播，防止两个层次重复触发。
- [x] 分别注入普通日志失效、stderr 失效及二者失效；输出不进入 stdout，副本尝试次数、标记身份及会话结果不改变。诊断写入不触发 `on_report_failure`。
- [x] 核对关闭前已接纳的诊断按现有日志生命周期处理，不能只断言回调被调用；复跑 session、logging_runtime、bootstrap 各目录及单元目录。

## 验收命令与收口

从仓库根执行，新增文件将在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/logging_runtime/test_copies.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_log_copy_diagnostics.py -q
```

完整门禁按指南分别运行 `unit`、`integration/logging_runtime`、`integration/session`、`integration/bootstrap`，要求相关场景全部通过。

- [x] 复核所有副本回执、早退、异常与两类报告消费者；诊断具有步骤及真实分类，多重错误完整保留。
- [x] 记录环境、红绿命令与日志、退出码、实际文件及诊断证据；复核后更新路线图 SW-02，并形成独立修复提交。
- [x] 将最终回执和诊断接口交给[SW-03 临时文件清理](2026-10-13-camctl-sw03-marker-temp-cleanup.md)，后续不得再次丢弃新增清理结果。

2026-10-13：本计划已实施并通过专项、完整目录及独立复核。完整目录后的双诊断出口失败组合仅增补测试，生产文件保持已审版本；新增用例及诊断模块分别通过。实际环境、命令、退出结果和后续责任见[验证记录](../../camctl/verification.md#sw-02-日志副本诊断验证2026-10-13)。
