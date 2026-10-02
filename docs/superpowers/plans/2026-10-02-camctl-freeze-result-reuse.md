# 报告冻结的原键复用

## 目标与依据

报告冻结已经保存后，调用方以原 `operation_key` 和事件时刻重新查询，仓储核实原完整事务，返回原 `GENERATE`、报告身份、完整 H、业务覆盖范围及生成版本。依据为[事务核实](../../camctl/database/transactions.md#公共完成与失败契约)、[报告固定依据](../../architecture/status-reports.md#从历史重建报告)、[冻结机会规则](2026-10-02-camctl-report-freeze-review.md#输入输出与不变量)和[原键消费者审查](2026-10-02-camctl-event-contracts-review.md#未完成的消费者与恢复边界)。文件划分和内部函数是实施建议。

## 输入、分类与不变量

输入是操作键、所属状态库连接和整数微秒的事件时刻，默认 `0` 同样作为精确输入核对。原冻结的创建行完整保存不可变生成依据。报告的文件状态、字节与发布事实仍可推进，不用它们重新决定原冻结响应。原冻结读取在同一可靠写事务内先于机会、ACK 和有效同步读取执行。

| 原键状态 | 原阶段及输入 | 结果 |
| --- | --- | --- |
| 不存在 | 输入合法 | 按同一事务的当前机会决定 `GENERATE`、`REUSE` 或 `SKIP`。 |
| 存在 | 恰好一个 `REPORT_CHANGED/FREEZE`，恰好创建一个报告，事件时刻相同 | 核实原报告及 H，只读返回原 `GENERATE`。 |
| 存在 | 阶段、组成或事件时刻不同 | 拒绝；不把该键用于当前机会，不改变历史或投影。 |
| 存在 | 原组、固定依据、物理报告身份或 H 无法解释 | 保留一致性错误；不返回部分报告、不补默认值。 |

`REUSE` 和 `SKIP` 不登记事务。可靠查不到原键只证明没有持久化冻结事实，不保存或声称原只读机会响应已经固定。

原创建行的 `row.id` 是报告身份。当前记录的 `created_event_id` 必须引用原冻结事件，固定的 `frozen_event_id`、`from_wm`、`to_wm` 与 `format_version` 必须与原创建事实相同。H 是原冻结事务之前的完整已提交边界；须核实其事务成员和末事件，不能把仅存在的头记录当完整范围证明。报告登记不能冻结自己的创建事件。合法初始边界单独处理，不把缺失的非初始边界解释为初始状态。恢复不读取当前设备、不新增设备或文件副作用，也不增加报告计数和目录。

原报告的固定范围通过共用 `read_ack_report` 规则校验。`to_wm` 必须等于 H 内最大的业务水位；`from_wm` 为 `0`，或者是 H 内某个完整业务事务的末业务水位。原创建事实与当前固定字段相同不能替代这两项历史证明。

| 固定范围分类 | 结果 |
| --- | --- |
| 起点为 `0` 或 H 内的业务事务末水位，终点等于 H 的业务水位 | 允许继续核实原冻结响应。 |
| 起点没有历史依据、位于事务内部或位于 H 之后 | 一致性错误，整项回滚。 |
| 终点不等于 H 的业务水位 | 一致性错误，整项回滚。 |

## 读取资源责任

冻结命令取得的标量查询游标由命令释放。共用报告读取中的单行查询同样在读取值后关闭，再解释行事实；查询未取得游标、缺行、读取错误及业务校验错误分别处理，SQL 异常保持原对象。流式同步读取继续由生成器在正常结束和异常出口释放游标。已知入口为 `read_ack_state`、`read_ack_report`、`read_outstanding_syncs`、`read_report_opportunity`、`read_covering_report`、`read_frozen_report` 和 `read_report_management`，只统一读取资源所有权，不改变 ACK、同步或发布资格。

## 实施顺序与门禁

- [x] Z1：先补真实 SQLite 反例，覆盖原键重复、后来提交、ACK、较新报告、已结束同步及异阶段；原响应与整个数据库均保持原事实。未知提交分别覆盖原事务存在和可靠不存在。
- [x] Z2：将操作键传入冻结命令，先核实原完整组、创建行、时刻、只读固定字段、固定业务范围和 H，再返回原响应。固定范围复用共用读取规则；新键的当前机会算法保持现有契约；不能另存第二份响应、重新执行机会判断或重新登记原报告。
- [x] Z3：覆盖坏组、缺失报告、创建身份错位、固定字段不同、原创建事实与当前字段相同但业务范围无效、坏 H、异时刻以及读取失败。确认整项回滚，输入错误与数据库错误不混作 `SKIP`。
- [x] Z4：用受接口约束的内存连接验证共用报告读取的游标责任及原异常；统一单行查询关闭边界，保留生成器释放规则。通过公开函数证明资源所有权，不复制内部调用图或完整登记清单。
- [x] Z5：两版相关单元与报告集成、独立复核及全量组件门禁；按名称保留既有失败，更新审查与路线图的局部 checkbox 后提交。完整 R2、E3、E4、H4 和真实同步生产者仍按各自门禁推进。

## 验证证据

验证使用仓库外的 Python 3.12 与 3.11 虚拟环境，`PYTHONPATH` 为 `/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests`。单元与集成分开运行，集成进程串行准备真实资源。

- 原键复用的 26 项初始反例在原实现上有 20 项目标断言失败、6 项通过；固定范围的两个补充反例确认原创建事实与当前字段相同仍可返回无效范围。共用游标责任的 28 项初始单元有 19 项目标断言失败、9 项通过。所有失败先排除了夹具约束或接口构造错误。
- Python 3.12 的新增冻结原键组合 36 项与原冻结组合 40 项，共 76 项通过。覆盖后续状态推进、非零业务边界、异阶段、异时刻、坏完整组、原创建身份、固定范围、H、六类 SQL 查询失败、未知提交的存在与不存在，以及未登记的只读机会。
- Python 3.12 与 3.11 的组件单元组合各有 1421 项通过、1 项取消选择及原有两条 asyncio 标记警告。取消选择的是 `apps/camctl/tests/unit/logging_runtime/test_pending_log.py` 中既有阻塞用例 `test_accepted_record_delivers_receipt`；该用例没有通过，不属于此次修复范围。
- 独立复核核实了原键读取顺序、共享固定范围守卫、前一完整 H、原创建引用、游标出口及单元资源隔离；独立运行新增读取单元 34 项通过。原创建正文与当前字段相同但范围无效的反例，以及合法非零范围与读取故障分支均已核对。
- Python 3.11 的报告与受理集成组合共 465 项通过，包含新增冻结原键反例及真实 ACK、同步结束、报告管理和受理消费者。Python 3.12 的相同组合包含在下述全量集成门禁中，均通过。
- Python 3.12 全量组件集成为 900 项通过、9 项失败及原有三条警告，失败名称与既有基线完全相同，均位于 `apps/camctl/tests/integration/outputs/test_qualification.py`：
  - `test_qualification_uses_business_order`
  - `test_same_time_obtain_wins_over_processing`
  - `test_existing_read_protection_preserves_original_copy`
  - `test_cleanup_restriction_rejects_and_records`
  - `test_delete_in_progress_rejects_with_dedicated_code`
  - `test_cross_device_candidates_do_not_block`
  - `test_grant_creates_all_records_atomically`
  - `test_internal_processing_grant_skips_delivery`
  - `test_unavailable_output_rejects_without_records`

产物资格的既有错误与日志收据阻塞保留为独立后续问题，不能据局部门禁通过宣称全量测试通过。实际同步开始生产者、完整对象历史恢复及其他报告管理的原键响应仍按各自门禁核验。

单元命令为：

```sh
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv/bin/python -m pytest apps/camctl/tests/unit -q -k 'not test_accepted_record_delivers_receipt'
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv-camctl-contracts-311/bin/python -m pytest apps/camctl/tests/unit -q -k 'not test_accepted_record_delivers_receipt'
```

Python 3.12 集成命令为：

```sh
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv/bin/python -m pytest apps/camctl/tests/integration -q
```

Python 3.11 相关集成命令为：

```sh
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv-camctl-contracts-311/bin/python -m pytest apps/camctl/tests/integration/reporting apps/camctl/tests/integration/acceptance -q
```
