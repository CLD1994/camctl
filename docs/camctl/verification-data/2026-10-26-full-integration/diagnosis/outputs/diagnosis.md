# outputs 完整轮次只读诊断

固定源码：`9fbb734ca18e56934ba32942009e9ea46eccd089`；快照 `cf4f504b7648bb5004fd20e1d66bfb57ab6a2956a33b6203724b6f0262591950`。

自然结束：1956 passed、471 failed、116 errors、2 skipped。587 个不通过身份均已分类。pytest 471.72s，runner 475.90s，退出码1。

| 首因 | 身份数 |
|---|---:|
| 旧 Context/Runtime 构造仍传 owned= | 227 |
| 向新数据库端口传旧对象或漏 database | 13 |
| 异步装配或私有申请未 await | 315 |
| 异步业务消费未 await，读取尚未保存的事实 | 2 |
| 故障注入 SQL 前缀与实际语句不一致 | 3 |
| READ 准备夹具仍沿用 OwnedConnection 与同步消费 | 24 |
| 旧数据库对象使文件工作未启动 | 2 |
| 监测器把有界分页 fetchall 判为无界读取 | 1 |

583 个首因是旧接口未迁移，4 个是私有 SQL 监测失配。没有从这些首因确认此次合并新增的生产缺陷。

587 个失败测试函数在 a775、272、组合中正文一致，40文件中39个全文一致。相关 database 接口、异步装配及 fact_reads 有界读取在两边源码已存在。这是源码比较，未运行基线。

- **旧 Context/Runtime 构造仍传 owned=（227）**：生产 Context/Runtime 已要求 BusinessDatabase 类型的 database，测试仍传 removed owned 关键字。 代表：`apps/camctl/tests/integration/outputs/test_copy_complete.py::test_matched_copy_prepares_and_releases_source`。

- **向新数据库端口传旧对象或漏 database（13）**：SimpleNamespace 没有 database，或 OwnedConnection 被当成具有 read/write 的 BusinessDatabase 使用。 代表：`apps/camctl/tests/integration/outputs/test_outputs_contract.py::TestOutputLifecyclesAreIndependent::test_output_lifecycles_are_independent`。

- **异步装配或私有申请未 await（315）**：测试把异步装配 factory、for_item 或异步申请返回的 coroutine 当作运行对象/回执使用。 代表：`apps/camctl/tests/integration/outputs/test_cleanup_delete_save_recovery.py::test_delete_original_request_survives_save_failure[rollback-begin_cleanup_attempt]`。

- **异步业务消费未 await，读取尚未保存的事实（2）**：default exclusions 未等待 _fix_pending_selections；read_error_details 未等待 _finish_read_attempt，故分别得到空选择、NULL error_json。 代表：`apps/camctl/tests/integration/outputs/test_default_exclusions.py::test_obtain_runtime_consumes_saved_default_exclusions`。

- **故障注入 SQL 前缀与实际语句不一致（3）**：注入器使用 startswith(SELECT id, transaction_id, event_type)，实际 fact_reads.EVENT_COLUMNS 为 id,transaction_id,event_type；没有注入读错误，正常完成与断言矛盾。 代表：`apps/camctl/tests/integration/outputs/test_grant_reuse.py::test_original_group_read_error_does_not_create_new_grant[device]`。

- **READ 准备夹具仍沿用 OwnedConnection 与同步消费（24）**：同一24参数夹具的 runtime 仅有 owned，DriverReadSessions/RecordingInputCopies 也直接收到 OwnedConnection；部分分支又未 await finish/acquire，broad raises 和2秒等待掩盖更早的接口错误。 代表：`apps/camctl/tests/integration/outputs/test_read_preparation_settlement.py::test_preparation_settlement_survives_real_consumer_and_result_save[not_started-device]`。

- **旧数据库对象使文件工作未启动（2）**：shared_file_ownership prepare 的 CopyContext 第二位置参数是 OwnedConnection；prepare_copy 首次 database.read 即失败，文件线程尚未开始。 代表：`apps/camctl/tests/integration/outputs/test_shared_file_ownership.py::test_shared_file_operation_keeps_qualification_until_actual_end[delivery-prepare]`。

- **监测器把有界分页 fetchall 判为无界读取（1）**：Cursor.fetchall 无条件拒绝；实际 iter_transaction_events 有固定事务、id上下界及 LIMIT batch_size<=128，首次头读 limit=1。该 fetchall 不随整个来源集合增长。 代表：`apps/camctl/tests/integration/outputs/test_source_resolution_batches.py::test_source_write_and_original_key_keep_bounded_residency`。

迁移建议：用公开装配接口创建真实数据库执行器并等待异步业务完成；保持原身份、真实写入、取消、资源收场等业务断言。SQL故障必须证明命中实际读边界；容量监测继续拒绝无界读取，同时认可有明确事务/id范围和不超过128条 LIMIT 的分页并记录其实际返回数量。

逐身份的表象、首因和原日志行号见 `classified-cases.json`；三方函数摘要见 `failed-test-source-comparison.json`；接口摘要见 `interface-source-comparison.json` 与 `additional-api-comparison.json`。

未修改仓库、未增加测试执行。首因修正后仍可能暴露后续业务问题；这份分类不构成组件通过。
