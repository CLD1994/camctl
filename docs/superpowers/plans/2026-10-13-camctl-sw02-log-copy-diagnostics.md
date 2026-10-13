# SW-02：日志副本与标记失败诊断实施计划

> 供执行 Agent 使用：使用 `superpowers:executing-plans` 或已经明确指派的子任务逐项实施。先验证失败，再修改生产代码；每项由复核者核对真实回执、文件和诊断出口。复选框表示实施门禁，不表示计划已经执行。

**目标：** 标记检查、创建、进度保存及日志副本处理的实际错误进入可用诊断通道；辅助诊断不改变原报告错误、故障轮次或文件交接结果。

**组织建议：** 副本编排返回完整的标记保存结果，会话消费回执并通过装配提供的日志诊断端口输出。沿用既有普通日志和 stderr，不引入业务历史或新的副本触发入口。

**技术基础：** Python 3.11、既有 `CopyReceipt`、`MarkerSave`、异步日志通道及文件交接接口。

**设计依据：** [标记创建失败](../../architecture/log-failure-marker.md#故障标记创建失败)、[进度更新失败](../../architecture/log-failure-marker.md#故障标记的进度更新失败)、[副本失败清理](../../architecture/log-delivery.md#日志副本的失败清理)、[日志通道故障](../../architecture/logging.md#日志通道故障)、[会话错误](../../architecture/session-errors.md)。本计划承接[路线图 SW-02](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，编写顺序在[SW-01](2026-10-13-camctl-sw01-acceptance-clock-status.md)之后。

## 当前入口与文件责任

2026-10-13：`logging_runtime/copies.py::deliver_failure_copy` 丢弃 `COPIED`、`PUBLISHED` 两次 `update_stage` 返回的 `MarkerSave`；`session/service.py::_trigger_failure_log` 忽略 `on_report_failure` 的 `CopyReceipt`，并吞掉请求工厂和副本调用异常。默认日志装配位于 `bootstrap/lifecycle.py::_failure_log_wiring`，普通会话错误日志经 `_emit_session_error` 投递，但没有消费上述副本细节。

`CopyReceipt.outcome` 与副本实际阶段不能由标记进度代替。当前 `SaveOutcome` 能区分可靠成功、未创建失败、已放置但未同步和未知；创建回执只留下错误文本，也需要保留该区别。受限会话 `_restricted_session` 的一次报告异常没有经过普通 `_drive_flows` 的触发入口，应在组合验证中核对这一接缝，按同一报告首次失败规则接入。

路径相对于 `apps/camctl`，新增内部字段、回调名和文件名是建议；若调整，须同时修改全部生产者、消费者和测试。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/logging_runtime/copies.py` | `deliver_failure_copy(request, *, publish) -> CopyReceipt` 保留完整保存回执；`FailureLogService` 保持暂停、抑制和恢复语义。 |
| `src/camctl/session/service.py` | `_trigger_failure_log(context, error) -> None` 消费回执和调用异常；审计普通及受限报告失败入口。 |
| `src/camctl/bootstrap/lifecycle.py` | 为 `SessionContext` 装配诊断出口，连接普通日志与可用 stderr；保留日志有序关闭。 |
| `tests/unit/logging_runtime/test_copies.py` | 扩展现有进度失败测试，验证返回值和副本操作分类。 |
| `tests/integration/logging_runtime/test_copies.py` | 用真实目录核对进度失败后继续交付、发布失败后的文件及身份。 |
| `tests/integration/bootstrap/test_log_copy_diagnostics.py`（新增） | 经默认装配验证实际 `run` 的诊断、错误优先级和非递归行为。 |
| `tests/integration/session/test_session.py` | 验证普通与受限入口消费回执，辅助诊断失败不阻止业务或改变原结果。 |

## 全局约束与阶段接口

- 不修改公共协议、报告 JSON、业务水位或数据库格式。日志副本不变成普通 delivery 或状态报告。
- 标记首次可靠创建是复制前提；进度保存失败单独不撤销该前提。只有实际文件交接结果可以证明副本发布。
- 已在 `ready`／`processing` 的副本不撤回、不删除、不重投。失败流程不自动重试，旧标记继续抑制重复交付。
- 每个实际错误保留发生步骤、结果分类、已知轮次／副本身份和原因。多个错误不能由最后一个覆盖；文案不逐字固定，机器枚举精确断言。
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

## 任务一：保留副本编排的完整回执

**消费接口：** `MarkerStore.check() -> MarkerCheck`、`create(round_id, copy_name) -> MarkerSave`、`update_stage(stage) -> MarkerSave` 和既有发布端口。

**交付接口：** 扩充后的 `CopyReceipt`，带创建及按发生顺序保存的进度结果；实际副本 `outcome` 保持原定义。

- [ ] 扩展 `test_progress_update_failure_does_not_block_delivery`，新增 `test_both_progress_failures_survive_published_receipt`、`test_copy_failure_keeps_prior_marker_diagnostic`、`test_marker_create_receipt_keeps_unsynced_or_unknown_result`。参数化覆盖各非成功 `SaveOutcome`；断言实际返回分类、阶段、轮次和错误事实。
- [ ] 运行单元专项，确认当前失败来自回执丢失，保留日志。用现有内存文件和受真实接口约束的标记、发布替身，不访问真实目录。
- [ ] 在 `deliver_failure_copy` 中保留创建结果及两次实际进度保存结果；各早退和成功返回均带已取得的事实。单独的进度保存错误允许正常流程继续；副本自身错误仍按原规则早退和清理。
- [ ] 核对 `MarkerStore.check` 的内容无效分支：当前解码为 `state=None` 时没有错误文本，应给出内容无效的诊断事实，同时保持 `EXISTS` 和重复抑制。不可把内容无效转换为不存在，也不在日志中重复转储完整标记内容。
- [ ] 审计 `FailureLogService.on_report_failure` 的抑制及暂停返回，保持首次标记失败的同运行暂停，后续可靠恢复才解除；不要为了收集诊断再次调用保存或复制。
- [ ] 新增 `test_late_progress_does_not_recreate_recovered_marker` 和 `test_old_progress_does_not_modify_new_round`。在原更新排队／写临时文件期间安排可靠恢复及新故障，核对原轮次、保存结果和正式文件字节；让创建、更新和恢复删除在同一协调拥有者下执行，不能用无条件重写新标记修复进度。
- [ ] 复跑单元专项。扩展真实目录集成测试，验证 `COPIED` 和 `PUBLISHED` 保存错误后副本仍在正确目录、字节不变，并在副本自身失败时保持失败分类及所有错误。

## 任务二：会话消费与默认诊断装配

**消费接口：** 任务一的回执、`SessionContext.failure_log` 和 `copy_request_factory`。

**交付接口：** 装配后的异步诊断回调；正常及受限报告失败经过相同的非递归副本诊断处理。

- [ ] 在 session 集成测试加入 `test_report_failure_consumes_copy_diagnostics`、`test_copy_factory_exception_is_diagnosed_without_reclassification`、`test_restricted_report_failure_uses_same_log_diagnostics`。用类型化回执覆盖上表，包括抑制但内容无效；分别断言原 `report_error`、`clock_invalid`、`state_db_error` 及原错误先后关系保持。
- [ ] 在新增 bootstrap 文件加入 `test_default_run_records_marker_progress_failure`、`test_default_run_records_copy_and_cleanup_failures`、`test_unavailable_log_falls_back_to_stderr_without_recursion`。通过真实默认会话与文件日志验证步骤、原因和身份可观察；原报告失败在注入故障前已真实发生。
- [ ] 运行新增专项，确认当前回执或异常没有进入诊断出口。再装配回调、消费创建／进度回执及 `CopyReceipt.error`，保留请求工厂和调用异常；所有返回分支使用相同出口。
- [ ] 在受限一次报告的普通错误和状态库错误分支调用同一触发及诊断处理；不追加报告重试，不为副本取得普通接纳资格。审计默认 `report_flow` 的异常传播，防止两个层次重复触发。
- [ ] 分别注入普通日志失效、stderr 失效及二者失效；输出不进入 stdout，副本尝试次数、标记身份及会话结果不改变。诊断写入不触发 `on_report_failure`。
- [ ] 核对关闭前已接纳的诊断按现有日志生命周期处理，不能只断言回调被调用；复跑 session、logging_runtime、bootstrap 各目录及单元目录。

## 验收命令与收口

从仓库根执行，新增文件将在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/logging_runtime/test_copies.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_log_copy_diagnostics.py -q
```

完整门禁按指南分别运行 `unit`、`integration/logging_runtime`、`integration/session`、`integration/bootstrap`，要求相关场景全部通过。

- [ ] 复核所有副本回执、早退、异常与两类报告消费者；诊断具有步骤及真实分类，多重错误完整保留。
- [ ] 记录环境、红绿命令与日志、退出码、实际文件及诊断证据；复核后更新路线图 SW-02，并形成独立修复提交。
- [ ] 将最终回执和诊断接口交给[SW-03 临时文件清理](2026-10-13-camctl-sw03-marker-temp-cleanup.md)，后续不得再次丢弃新增清理结果。

2026-10-13：计划已编写，生产修复与业务测试尚未执行；不需要真实相机或目标主机。
