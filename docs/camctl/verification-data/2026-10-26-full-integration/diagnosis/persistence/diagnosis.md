# persistence 重放诊断

本次重放完整执行首轮已收集的原 936 个节点，结果为 251 个通过、685 个失败，退出码为 1。所有节点的 setup 和 teardown 都通过；685 个失败都记在 call 阶段，其中 684 个实际停在测试体内的世界准备或运行对象构造，目标生产操作尚未执行。首轮没有执行这些 936 个节点：它因另一个旧测试文件的导入错误在收集阶段退出。本报告不将这两轮称为两次完整 persistence 验证。

本次任务来自 `/tmp/camctl-integration-replay-20261026-u19lkvoe/summary.json` 中的 `camctl/persistence`，实际目录是 `tasks/001-camctl-persistence`。首轮任务由各自的 summary 定位为 `tasks/002-camctl-persistence`。本报告只读冻结日志、测试和源码，没有修改仓库或重跑测试；临时诊断文件写在本目录。

## 失败首因

| 首因与类别 | 实例数 | 实际已完成和被阻断的步骤 |
| --- | ---: | --- |
| 旧测试辅助函数把 `OwnedConnection` 传给要求异步 `write` 的 `accept_input`。 | 209 | 真实数据库已打开，入口已构造输入命令；访问 `database.write` 时抛出 `AttributeError`，该受理操作尚未提交。后续原活动、结果页、索引、拒绝分区或分页断言未执行。影响 `test_baseline_read_facts.py` 63 项及 `test_segment_result_page_inputs.py` 146 项。 |
| 旧测试辅助函数以 `owned=` 构造只接受 `database` 的 `CaptureRuntime`。 | 475 | 共享 `consumer_world` 已完成初始化、受理计划及保存调度窗口和开始事实；构造运行对象时抛出 `TypeError`，尚未调用实际拍摄处理器或控制驱动。后续结果来源、配对、分段、完成事务及容量操作未执行。 |
| 旧结构审计的函数许可集合遗漏五个只读一致视图入口。 | 1 | 静态审计已完成源码遍历，并通过直接 `.commit()` 与 `sqlite3.connect()` 两项检查，最后在许可集合比较时失败。五个遗漏入口均在无外层事务时 `BEGIN`，并在 `finally` 中 `ROLLBACK`；未自开写事务或提交。此节点没有执行这些函数。 |

当前失败首因中，环境或运行器错误为 0，已确认生产功能缺陷为 0，已确认合并新增缺陷为 0。这个分类只描述实际触发的首因，不证明被 684 个准备错误遮蔽的生产行为正确。

全部 685 个失败节点都已与 output.log 的 685 段 traceback 逐项对应；节点顺序同时与 pytest 的 685 条 FAILED 摘要核对。`classified-cases.json` 保存每个节点的原始 nodeid、仓库 nodeid、三个执行阶段、阶段事件行号、错误类别、错误行、源帧、完整 traceback 行范围和摘要。251 个通过节点同样保留其阶段记录。

代表证据：

- `tests/integration/persistence/test_baseline_read_facts.py::test_intent_freezes_original_request_without_changing_capture`：replay `artifacts/output.log:15-68`，终点是 `acceptance/service.py:121`，测试辅助入口是 `scheduling/test_start_action.py:64`。
- `tests/integration/persistence/test_segment_check_conclusions.py::test_no_result_decision_anchors_saved_baseline_end_and_keeps_action_running[False-False]`：`artifacts/output.log:3796-3852` 的 traceback，终点是 `capture/test_capture_contract.py:339`。
- `tests/integration/persistence/test_repositories.py::TestCommitPathAudit::test_repositories_commit_only_through_the_transaction_kernel`：`artifacts/output.log:3740-3795`，断言位于 `test_repositories.py:166`；遗漏 `_load_fixed_file`、`read_pairing_fact`、`read_result_file_input_from_reader`、`read_segment_finish_candidates` 与 `read_segment_finish_scope`。

## 静态来源对照

已比较合并前本分支 `a7755d81`、合并提交 `272fda66`、首轮源码 `9fbb734c` 和重放源码 `354e38f9`。没有执行两个父版本的测试基线。

`acceptance/service.py`、`persistence/runtime.py` 和共享 `_accept_plan` 所在测试文件在四个版本的 Git blob 完全相同。`_accept_plan` 与 `accept_input` 的函数语法树也相同，因此无 `write` 的输入组合在合并前就已存在。

`CaptureRuntime` 在四个版本都具有 `database` 字段且没有 `owned` 字段。整个类在合并中还有其他行为变化，故这里没有将类的所有行为视为相同；直接导致本次构造失败的字段契约，以及传递 `owned=` 的共享 `_runtime` 函数语法树，在四个版本均相同。该首因也先于本次合并存在。

结构审计测试文件和五个遗漏函数所在的三个生产文件在四个版本的 blob 相同，五个函数语法树也相同。旧 `test_page_read_cache.py` 在四个版本的 blob 相同，而它导入的生产模块在四个版本均不存在。上述静态证据支持“旧测试接口或审计集合未同步”的归因，不能替代父版本运行结果。

首轮与重放的整个 `apps/camctl/src` Git 树相同；另外核对重放任务冻结 source 中的 324 个受版本控制生产 blob，全部符合 `354e38f9`。精确提交、blob、函数摘要、冻结路径、解释器、命令、时间和证据摘要见 `provenance.json`。

## 容量与读取分页的实际验证边界

下述三个专项各自是一个 pytest 测试节点；513、129和5分别表示各测试的输入条数。

513条输入的容量测试节点 `test_fixed_batch_write_and_verification_have_bounded_residency` 在预算预检返回后，进入 `_ready_world` 并停在 `CaptureRuntime(owned=...)`。此时尚未为513条输入准备来源页、建立五个原页、运行受限业务进程、取得资源计量收据或核对业务结果。证据是 `output.log:38080-38139`。本次没有取得该负载的内存与交换空间合计预算验收结果。

129条输入的分页测试节点 `test_fixed_batch_reads_and_output_identity_lookup_keep_their_bounds` 同样停在运行对象构造；尚未安装 `MeasuredConnection`，没有执行真实完成写入、原键核实、跨每页128条的读取边界检查或产物身份查询索引检查。证据是 `output.log:38140-38223`。5条输入的worker功能辅助测试也在相同前置步骤失败，见 `output.log:42367-42428`。

`test_segment_transaction_batches.py` 共 112 项，106 失败、6 通过。6 个通过节点仅验证受控预算不可用时禁止昂贵准备，以及五种无效结果文件被拒绝；它们不能承接真实worker执行、513条输入的容量测试或129条输入的分页测试覆盖。

本轮有真实读取通过证据：`test_cache_queries.py` 两项通过；`TestDetachedBatch::test_batch_is_detached_and_read_connection_is_released` 以三条事件、每页两项读取，插入第四个事务后验证新读可见新提交、固定旧边界续页仅返回第三条并耗尽，以及第一页保持不变；同类读取错误传播节点也通过。`test_saved_transactions.py` 11个测试节点通过，但其小组读取不能替代129条输入的分页测试所要求的跨每页128条读取边界检查。逐项范围、源行与阶段证据见 `capacity-audit.json`。

首轮 `test_page_read_cache.py` 导入已取消模块而收集失败，原首轮 `cases.json` 为空；重放明确选择其余 23 个文件，库存与首轮已收集的 936 个节点逐项、逐序相同。该旧文件没有被重放，其六个测试函数承载的缓存生命周期、容量与事务分区覆盖仍是缺口，不能因其他读取测试通过而计为补齐。静态检查另见该文件第 9 行也导入当前不存在的 `read_reuse` 模块；首轮解释器实际报告的首个阻断仍是第 8 行的 `page_read_cache`，没有执行到第 9 行。

## 后续处理建议

1. 受理准备接口：在共享测试支持边界按当前正式入口装配异步业务数据库服务，或在存储契约测试中通过其受支持仓储入口准备合法状态。核对所有复用 `_accept_plan` 的消费者；保留原节点的拒绝、原键、恢复和分页分区，迁移方案需先列出公开入口与覆盖承接。不得为旧调用恢复生产测试专用接口。完成后重跑受影响完整目录，检查目标操作确实发生。
2. 运行对象构造：集中更新共享世界准备，让测试经当前模块正式入口取得所需状态和运行服务。已保存调度开始事实不能替代实际驱动启动；原分段、配对、完成事务、513条输入的容量测试和129条输入的分页测试目标分别保留。修复后核对真实目标调用与资源/业务收据，再执行受影响完整范围。
3. 事务结构审计：先以契约审计五个读取入口及同类读取入口的事务所有权；为只读入口建立单一权威识别方式，让测试从该来源读取，或改为验证明确事务不变量。保留拒绝自开写事务、自提交和直连数据库的检查，并补充只读事务正常、错误与嵌套生命周期的验证。
4. 缓存旧文件：明确原文件各缓存分区由当前事实读取接口的哪些公开入口和用例承接，再迁移该文件并恢复收集。覆盖映射未闭合之前继续将其标为未执行，不以移除文件或通过小规模读取用例结束此缺口。
