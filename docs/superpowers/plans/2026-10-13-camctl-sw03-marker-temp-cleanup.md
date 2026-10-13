# SW-03：标记保存临时文件清理实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务实施。先以失败测试证明本次保存残留，再修复保存拥有者；验证实际句柄、路径和回执。复选框在对应门禁通过后勾选。

**目标：** 标记保存操作实际结束并关闭句柄后，清理本次专用临时文件；保存错误与清理错误分别保留，正式标记和其他保存操作的文件保持各自归属。

**组织建议：** 在同步保存拥有者内部统一收口写入、替换和目录同步结果，然后对本次临时路径进行一次清理。通过 SW-02 的完整保存回执传递清理诊断，不让等待协程承担同步文件的删除责任。

**技术基础：** Python 3.11、既有文件操作注入点、`MarkerStore`、`MarkerSave` 与独立故障标记。

**设计依据：** [故障标记的临时文件](../../architecture/log-failure-marker.md#故障标记的临时文件)、[创建失败分类](../../architecture/log-failure-marker.md#故障标记创建失败)、[进度更新失败](../../architecture/log-failure-marker.md#故障标记的进度更新失败)、[实际文件完成](../../camctl/file-runtime.md)。本计划承接[路线图 SW-03](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，消费[SW-02 回执及诊断出口](2026-10-13-camctl-sw02-log-copy-diagnostics.md#全局约束与阶段接口)。

## 当前入口与文件责任

2026-10-13：`logging_runtime/copies.py::MarkerStore._save_payload(self, payload: bytes) -> MarkerSave` 为本次保存生成独立临时路径。`_write_synced` 在 `finally` 调用 `os.close(fd)`；保存在临时写入和替换异常时直接返回，没有清理残留。替换成功后原临时路径通常已经消失；后续目录同步失败不能据此删除正式文件。

路径相对于 `apps/camctl`。以下内部类型及文件组织是建议；已有保存分类与所有权是契约。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/logging_runtime/copies.py` | `_save_payload` 清理自己的临时路径；必要时使 `_write_synced` 区分写入错误与句柄关闭结果。 |
| `tests/unit/logging_runtime/test_marker_temp_cleanup.py`（新增） | 使用内存文件操作替身覆盖保存与清理的完整分类，不访问真实文件或线程。 |
| `tests/integration/logging_runtime/test_marker_temp_cleanup.py`（新增） | 用真实临时目录和工作线程验证部分写入、替换、取消与句柄关闭顺序。 |
| `tests/integration/bootstrap/test_log_copy_diagnostics.py` | 在 SW-02 的实际诊断组合中验证本次清理失败和原保存失败同时可见。 |

## 全局约束与接口

- 本次保存只拥有本次生成的临时路径；清理不扫描目录、不按前缀批量删除，也不删除正式标记、日志副本或别的保存操作留下的文件。
- 临时文件完整也不能成为正式标记，不能在恢复时自动提升或据此授权复制。
- 保存结果继续使用现有 `SaveOutcome`。清理成功不把保存失败改成成功；清理失败不覆盖原保存结果、原错误或实际放置事实。
- 建议给 `MarkerSave` 添加 `temp_name: str | None = None` 和 `temp_cleanup: MarkerDelete | None = None` 两个默认值字段，复用 `DeleteOutcome` 表达已删除、不存在、失败或未知。SW-02 的创建和进度回执必须完整保留并呈现这些字段。
- 建议新增 `_cleanup_marker_temp(path: Path) -> MarkerDelete`，仅接收保存拥有者直接提供的路径。只尝试一次删除；明确 `FileNotFoundError` 是无残留，`InterruptedError` 保存未知，其余 `OSError` 保存失败和原因。
- `_write_synced(path, data)` 的句柄由同步拥有者关闭。关闭抛错或结果不能确认时，不能只因线程返回就删除临时文件；保存回执保留关闭错误和残留，后续启动检查按 SW-04 再核实。实施时可用内部异常携带关闭状态，不能更改外部业务协议。
- 不在工作线程直接写 camctl 自身的 ERROR 日志；通过结果交给所属协程及 SW-02 出口。保留取消传播和原异常，不以清理异常取代原异常。
- Python 与测试运行遵守[组件测试指南](../../../apps/camctl/tests/AGENTS.md)；真实文件及线程验证放在集成测试。

## 保存结果与临时路径的分类

先判断保存操作是否实际结束、句柄是否已确认关闭，再判断本次路径。正式文件的存在性及保存可靠性始终由原保存结果表达。

| 保存和句柄事实 | 本次临时路径及删除结果 | 处理及回执 |
| --- | --- | --- |
| 操作仍进行或关闭未确认 | 任意 | 不删除，保留实际错误及该路径；不得把协程取消当成关闭证据。 |
| 临时写入／文件同步失败，句柄已关闭 | 部分或完整临时文件仍在 | 一次清理本次路径；原保存仍为未创建失败。 |
| 替换失败或结果未知，句柄已关闭 | 临时路径仍在 | 一次清理该临时路径；原保存仍为未知，不删除可能已放置的正式标记。 |
| 替换成功，后续目录同步失败 | 原临时路径已不存在 | 无残留；正式标记保留，原结果仍为已放置未同步。 |
| 保存可靠成功 | 原临时路径已不存在 | 保存成功保持，不制造清理失败或再次写入。 |
| 已具备清理资格 | 删除明确成功或路径明确不存在 | 返回对应清理事实；不推断日志副本已经交接。 |
| 已具备清理资格 | 删除失败或未知 | 保留原保存结果，另附清理结果、路径与原因；本次不重试。 |
| 原保存抛出不应结果化的异常 | 相关操作实际结束且句柄已关闭 | 尽力清理本次路径，附加清理诊断后传播原异常；不把程序异常伪装成普通保存成功。 |

重点复核：部分写入仍残留、替换实际完成但调用报告未知、关闭本身出错、另一保存操作仍占有文件、等待协程取消而线程尚未完成。以下任务都从公开 `create`／`update_stage` 或真实副本入口证实这些条件。

## 任务一：保存拥有者的失败测试与清理收口

**消费接口：** `MarkerStore.create(round_id, copy_name) -> MarkerSave`、`update_stage(stage) -> MarkerSave` 及窄文件操作注入点。

**交付接口：** 原保存分类加本次路径／清理结果；清理在同步保存操作的责任范围内完成。

- [ ] 新增单元测试 `test_partial_write_failure_removes_own_temp`、`test_replace_failure_cleans_temp_and_preserves_marker`、`test_replace_unknown_keeps_original_outcome`、`test_missing_temp_is_not_cleanup_failure`、`test_cleanup_failure_keeps_both_errors`、`test_unconfirmed_close_keeps_temp`。分别经 `create` 与 `update_stage` 运行分类，保留原正式标记字节作独立对照。
- [ ] 加入 `test_cleanup_never_removes_other_save_temp` 和 `test_unexpected_save_exception_is_not_masked_by_cleanup`；替身中放置不同保存操作的临时路径。删除次数只在“一次且仅本次路径”的所有权契约处断言，不复制整个内部调用图。
- [ ] 单独运行新文件，确认失败来自本次残留或原错误被覆盖，不能以缺少新增类型的导入错误代替语义失败。
- [ ] 让 `_save_payload` 统一持有本次临时路径和保存结果，在实际操作结束、句柄关闭已确认后调用一次清理。保留 `_write_synced` 的关闭责任；必要时把写入错误和关闭未确认分别传给保存拥有者。
- [ ] 加入“写入或文件同步先失败，随后关闭也失败”的反例，保留两个实际错误；不能让 `finally` 中的关闭异常覆盖原保存错误，也不能将关闭未确认当成可删除资格。
- [ ] 各早退均附完整清理结果；`InterruptedError` 不能被普通失败分支吞并，明确不存在不输出伪清理错误。原异常传播时清理诊断以既有异常附注或等价方式保存。
- [ ] 复跑新文件及整个单元目录，审计 `create`、`update_stage`、目录创建、写入、文件同步、替换、目录同步的所有出口。没有新建临时路径的入口不执行清理。

## 任务二：真实文件、取消与诊断消费者

**消费接口：** 任务一的 `MarkerSave` 和 SW-02 完整回执／诊断出口。

**交付接口：** 实际文件残留与诊断证据；外层等待取消不会提前删除仍在使用的文件。

- [ ] 在新增集成文件加入 `test_real_partial_write_leaves_no_temp_after_close`、`test_real_replace_failure_keeps_formal_marker`、`test_real_directory_sync_failure_keeps_placed_marker`。窄注入点先真实写入部分字节并关闭，再报告失败；直接核对目录、正式文件字节和保存分类。
- [ ] 用受控工作线程加入 `test_cancelled_wait_does_not_remove_active_marker_temp`：写入期间暂停线程并取消等待协程，确认临时文件仍在；释放线程，确认同步拥有者在实际关闭后完成清理，不发生另一协程抢先删除。
- [ ] 实际关闭结果无法确认时保留临时路径及诊断，不通过强行删除让测试通过。真实进程中断留下的旧临时文件由 SW-04 接手，本任务不在中断进程中假称已经执行清理。
- [ ] 将 `MarkerSave.temp_cleanup` 的失败／未知接入 SW-02 的创建和两次进度回执；默认 `run` 诊断同时包含原保存步骤和本次清理步骤，进度保存失败后正常副本仍可发布。
- [ ] 单独执行新集成文件与 SW-02 诊断文件，再分别运行 `integration/logging_runtime`、`integration/bootstrap`。核对已发布副本、正式标记和其他临时文件未被误删。

## 验收命令与收口

从仓库根执行，新增文件在实施阶段创建：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/logging_runtime/test_marker_temp_cleanup.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/logging_runtime/test_marker_temp_cleanup.py -q
```

完整门禁分别执行 `unit`、`integration/logging_runtime`、`integration/bootstrap`，其中须包含 SW-02 的实际诊断组合。相关分类全部通过才可收口。

- [ ] 复核每个保存出口的实际完成、关闭和路径所有权，分别记录原保存错误、关闭错误和清理结果。
- [ ] 保留红绿证据、真实路径与字节检查、解释器和命令结果；独立复核后更新路线图 SW-03，形成单独修复提交。
- [ ] 将最终临时命名和本次活动路径的归属交给[SW-04 启动清理](2026-10-13-camctl-sw04-startup-log-sweep.md)，不能通过扫描启动遗留文件替代本次保存拥有者的清理。

2026-10-13：计划已编写，生产修复与业务测试尚未执行；不需要真实相机或目标主机。
