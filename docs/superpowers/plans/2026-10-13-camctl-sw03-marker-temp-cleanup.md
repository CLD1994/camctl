# SW-03：标记保存临时文件清理实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务实施。先以失败测试证明本次保存残留，再修复保存拥有者；验证实际句柄、路径和回执。复选框在对应门禁通过后勾选。

**目标：** 标记保存操作实际结束并关闭句柄后，清理本次专用临时文件；保存错误与清理错误分别保留，正式标记和其他保存操作的文件保持各自归属。

**组织建议：** 在同步保存拥有者内部统一收口写入、替换和目录同步结果，然后对本次临时路径进行一次清理。通过 SW-02 的完整保存回执传递清理诊断，不让等待协程承担同步文件的删除责任。

**技术基础：** Python 3.11、既有文件操作注入点、`MarkerStore`、`MarkerSave` 与独立故障标记。

**设计依据：** [故障标记的临时文件](../../architecture/log-failure-marker.md#故障标记的临时文件)、[创建失败分类](../../architecture/log-failure-marker.md#故障标记创建失败)、[进度更新失败](../../architecture/log-failure-marker.md#故障标记的进度更新失败)、[实际文件完成](../../camctl/file-runtime.md)。本计划承接[路线图 SW-03](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，消费[SW-02 回执及诊断出口](2026-10-13-camctl-sw02-log-copy-diagnostics.md#全局约束与阶段接口)。

## 当前入口与文件责任

2026-10-14：`logging_runtime/copies.py::MarkerStore._save_payload(self, payload: bytes) -> MarkerSave` 为本次保存生成独立临时路径，并在操作结束、临时句柄确认关闭后清理自己的残留。`_write_synced` 分别保留写入、同步及关闭错误；关闭未确认时不删除临时路径。替换成功后记录源路径已移走，目录同步和目录句柄关闭分别诊断，正式文件保持实际放置事实。

路径相对于 `apps/camctl`。以下内部类型及文件组织是建议；已有保存分类与所有权是契约。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/logging_runtime/copies.py` | `_save_payload` 清理自己的临时路径；必要时使 `_write_synced` 区分写入错误与句柄关闭结果。 |
| `src/camctl/session/service.py` | 创建与每次进度回执分别呈现临时清理事实；异常诊断保留附注和异常组中的各原因。 |
| `src/camctl/bootstrap/lifecycle.py` | 标记恢复删除的结果即使为已删除，也呈现附属的目录句柄关闭错误。 |
| `tests/unit/logging_runtime/test_marker_temp_cleanup.py`（新增） | 使用内存文件操作替身覆盖保存与清理的完整分类，不访问真实文件或线程。 |
| `tests/integration/logging_runtime/test_marker_temp_cleanup.py`（新增） | 用真实临时目录和工作线程验证部分写入、替换、取消与句柄关闭顺序。 |
| `tests/integration/bootstrap/test_log_copy_diagnostics.py` | 在 SW-02 的实际诊断组合中验证本次清理失败和原保存失败同时可见。 |

## 全局约束与接口

- 本次保存只拥有本次生成的临时路径；清理不扫描目录、不按前缀批量删除，也不删除正式标记、日志副本或别的保存操作留下的文件。
- 临时文件完整也不能成为正式标记，不能在恢复时自动提升或据此授权复制。
- 保存结果继续使用现有 `SaveOutcome`。清理成功不把保存失败改成成功；清理失败不覆盖原保存结果、原错误或实际放置事实。
- `MarkerSave` 已追加 `temp_name: str | None = None` 和 `temp_cleanup: MarkerDelete | None = None` 两个默认值字段，复用 `DeleteOutcome` 表达已删除、不存在、失败或未知。SW-02 的创建和进度回执完整保留并呈现这些字段。
- `_cleanup_marker_temp(path: Path, *, save_outcome: SaveOutcome) -> MarkerDelete` 仅接收保存拥有者直接提供的路径，并在传播程序异常时附已知保存事实。只尝试一次删除；明确 `FileNotFoundError` 是无残留，`InterruptedError` 保存未知，其余 `OSError` 保存失败和原因。
- `_write_synced(path, data)` 的句柄由同步拥有者关闭。关闭抛错或结果不能确认时，不能只因线程返回就删除临时文件；保存回执保留关闭错误和残留，后续启动检查按 SW-04 再核实。实施时可用内部异常携带关闭状态，不能更改外部业务协议。
- 未执行临时删除时，`temp_cleanup` 保持 `None`，诊断说明未尝试及原因；`UNKNOWN` 用于实际删除已尝试但结果未知。关闭事实采用明确的内部类型或字段，不从错误文案推断。
- 目录同步成功与目录句柄关闭失败分别保存。替换及目录同步已确认、临时文件句柄也已关闭时，标记仍为 `SAVED`，另附目录关闭错误；真正目录同步失败仍为 `PLACED_UNSYNCED`。共享目录同步接口的正式标记删除，同样保留已删除及已同步的事实和关闭错误。
- 不在工作线程直接写 camctl 自身的 ERROR 日志；通过结果交给所属协程及 SW-02 出口。保留取消传播和原异常，不以清理异常取代原异常。
- Python 与测试运行遵守[组件测试指南](../../../apps/camctl/tests/AGENTS.md)；真实文件及线程验证放在集成测试。

## 保存结果与临时路径的分类

先判断保存操作是否实际结束、句柄是否已确认关闭，再判断本次路径。正式文件的存在性及保存可靠性始终由原保存结果表达。

| 保存和句柄事实 | 本次临时路径及删除结果 | 处理及回执 |
| --- | --- | --- |
| 操作仍进行或关闭未确认 | 任意 | 不删除，保留实际错误及该路径；不得把协程取消当成关闭证据。 |
| 临时写入／文件同步失败，句柄已关闭 | 部分或完整临时文件仍在 | 一次清理本次路径；原保存仍为未创建失败。 |
| 替换失败或结果未知，句柄已关闭 | 临时路径可能仍在，也可能已被替换移走 | 一次清理该临时路径；原保存仍为未知，不删除可能已放置的正式标记。 |
| 替换成功，后续目录同步失败 | 原临时路径已不存在 | 无残留；正式标记保留，原结果仍为已放置未同步。 |
| 替换及目录同步均已确认，目录句柄关闭报错 | 原临时路径已移走，临时文件句柄此前已关闭 | 保存仍为 `SAVED`，另附目录关闭错误；原同步事实保持。 |
| 保存可靠成功 | 原临时路径已不存在 | 保存成功保持，不制造清理失败或再次写入。 |
| 已具备清理资格 | 删除明确成功或路径明确不存在 | 返回对应清理事实；不推断日志副本已经交接。 |
| 已具备清理资格 | 删除失败或未知 | 保留原保存结果，另附清理结果、路径与原因；本次不重试。 |
| 原保存抛出不应结果化的异常 | 相关操作实际结束且句柄已关闭 | 尽力清理本次路径，附加清理诊断后传播原异常；不把程序异常伪装成普通保存成功。 |

替换正常返回已确认本次源路径被移走时，可直接记录 `NOT_PRESENT`，无须再删除该路径。替换抛错且结果未知时，只对本次原临时路径尝试一次删除，结果不能用于推断正式标记的状态。

临时删除只将预期的文件系统错误转换为 `MarkerDelete`。清理遇到程序异常时，原保存已有程序异常或取消异常的，保留同一原异常并附清理诊断；原保存为预期 `OSError` 的，按原保存异常、清理异常的发生顺序共同传播，并附已知保存结果和临时名；原保存没有异常的，传播清理程序异常并附已确认的保存事实。所有分支保留取消，不把程序错误包装成普通清理未知结果。

写入、同步、关闭和清理共享下列异常优先级。关闭或清理发生取消时，不能只把取消文案写入普通文件错误而返回成功或失败回执。

| 先发生的异常 | 关闭或清理的后续异常 | 传播与已知事实 |
| --- | --- | --- |
| 原程序异常或原取消异常 | 任意后续异常，包括取消 | 传播同一原异常对象，附加后续原因及实际关闭、清理和保存事实。 |
| 可以结果化的 `OSError` | `CancelledError` 等不属于 `Exception` 的异常 | 传播同一后续异常对象，附加此前的 `OSError`、临时名及已知保存或删除事实；临时句柄关闭未确认时不删除文件。 |
| 可以结果化的 `OSError` | 程序 `Exception` | 按发生顺序共同传播；保留两项类型、原因和已知保存或删除事实。 |
| 可以结果化的 `OSError` | 预期 `OSError` | 保持原结果分类，分别记录两项原因和实际关闭、清理事实。 |

重点复核：部分写入仍残留、替换实际完成但调用报告未知、关闭本身出错、另一保存操作仍占有文件、等待协程取消而线程尚未完成。以下任务都从公开 `create`／`update_stage` 或真实副本入口证实这些条件。

## 任务一：保存拥有者的失败测试与清理收口

**消费接口：** `MarkerStore.create(round_id, copy_name) -> MarkerSave`、`update_stage(stage) -> MarkerSave` 及窄文件操作注入点。

**交付接口：** 原保存分类加本次路径／清理结果；清理在同步保存操作的责任范围内完成。

- [x] 新增单元测试 `test_partial_write_failure_removes_only_its_temp`、`test_replace_failure_cleans_temp_and_preserves_formal_marker`、`test_replace_result_unknown_keeps_possible_formal_placement`、`test_missing_temp_after_write_failure_is_not_a_cleanup_error`、`test_cleanup_oserror_preserves_save_error_and_temp_ownership`、`test_unconfirmed_close_keeps_temp_without_retrying_close`。分别经 `create` 与 `update_stage` 运行分类，保留原正式标记字节作独立对照。
- [x] 部分写入及替换用例另放置不同保存操作的临时路径，确认不被删除；`test_unexpected_save_error_survives_cleanup_program_error` 和取消用例核对原对象传播。删除次数只在“一次且仅本次路径”的所有权契约处断言，不复制整个内部调用图。
- [x] 单独运行新文件，确认失败来自本次残留或原错误被覆盖，不能以缺少新增类型的导入错误代替语义失败。
- [x] 让 `_save_payload` 统一持有本次临时路径和保存结果，在实际操作结束、句柄关闭已确认后调用一次清理。保留 `_write_synced` 的关闭责任；必要时把写入错误和关闭未确认分别传给保存拥有者。
- [x] 加入“写入或文件同步先失败，随后关闭也失败”的反例，保留两个实际错误；不能让 `finally` 中的关闭异常覆盖原保存错误，也不能将关闭未确认当成可删除资格。
- [x] 验证写入失败后的临时关闭取消、目录同步失败后的目录关闭取消（保存与正式删除），以及保存失败后的临时删除取消；原普通文件错误不得吞掉取消。另核对原程序异常与关闭失败同时发生时仍传播同一主异常对象。
- [x] 分别验证目录同步失败、同步成功后目录关闭失败，以及同步和关闭都失败。核对保存及正式删除的实际分类、全部原因和默认诊断，已确认的同步不能被关闭错误抹去。
- [x] 各早退均附完整清理结果；`InterruptedError` 不能被普通失败分支吞并，明确不存在不输出伪清理错误。原异常传播时清理诊断以既有异常附注或等价方式保存。
- [x] 复跑新文件及整个单元目录，审计 `create`、`update_stage`、目录创建、写入、文件同步、替换、目录同步的所有出口。没有新建临时路径的入口不执行清理。

## 任务二：真实文件、取消与诊断消费者

**消费接口：** 任务一的 `MarkerSave` 和 SW-02 完整回执／诊断出口。

**交付接口：** 实际文件残留与诊断证据；外层等待取消不会提前删除仍在使用的文件。

- [x] 在新增集成文件加入 `test_real_partial_write_is_removed_after_handle_close`、`test_real_replace_failure_cleans_temp_and_preserves_marker`、`test_real_directory_sync_failure_keeps_placed_formal_marker`。窄注入点先真实写入部分字节并关闭，再报告失败；直接核对目录、正式文件字节和保存分类。
- [x] 用受控工作线程加入 `test_cancelled_wait_keeps_active_temp_until_save_owner_finishes`：写入期间暂停线程并取消等待协程，确认临时文件仍在；释放线程，确认同步拥有者在实际关闭后完成清理，不发生另一协程抢先删除。
- [x] 实际关闭结果无法确认时保留临时路径及诊断，不通过强行删除让测试通过。真实进程中断留下的旧临时文件由 SW-04 接手，本任务不在中断进程中假称已经执行清理。
- [x] 将 `MarkerSave.temp_cleanup` 的失败／未知接入 SW-02 的创建和两次进度回执；默认 `run` 诊断同时包含原保存步骤和本次清理步骤，进度保存失败后正常副本仍可发布。
- [x] 创建与两次进度回执分别验证 `SAVED` 附关闭错误仍进入诊断；默认 `run` 组合真实标记保存确认目录同步、注入一次关闭错误，副本仍发布且主报告结果保持。正式删除在目录打开异常或同步异常后关闭成功时，只诊断实际事实。
- [x] 单独执行新集成文件与 SW-02 诊断文件，再分别运行 `integration/logging_runtime`、`integration/bootstrap`。核对已发布副本、正式标记和其他临时文件未被误删。

## 验收命令与收口

从仓库根执行，新增文件在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/logging_runtime/test_marker_temp_cleanup.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/logging_runtime/test_marker_temp_cleanup.py -q
```

完整门禁分别执行 `unit`、`integration/logging_runtime`、`integration/session`、`integration/bootstrap`，其中须包含 SW-02 的实际诊断组合和本次共享目录同步的恢复删除诊断。相关分类全部通过才可收口。

- [x] 复核每个保存出口的实际完成、关闭和路径所有权，分别记录原保存错误、关闭错误和清理结果。
- [x] 保留红绿证据、真实路径与字节检查、解释器和命令结果；独立复核后更新路线图 SW-03。
- [x] 将最终临时命名和本次活动路径的归属交给[SW-04 启动清理](2026-10-13-camctl-sw04-startup-log-sweep.md)，不能通过扫描启动遗留文件替代本次保存拥有者的清理。

本项代码、测试和相关文档以独立修复提交交付。

2026-10-14：本项已实现，同步保存、临时清理及诊断消费者通过专项与四个完整目录回归，并完成根代理逐文件审核和独立复核。实际环境、命令、结果及异步责任边界见 [SW-03 验证记录](../../camctl/verification.md#sw-03-标记临时文件清理验证2026-10-14)。本次没有使用相机或目标主机。
