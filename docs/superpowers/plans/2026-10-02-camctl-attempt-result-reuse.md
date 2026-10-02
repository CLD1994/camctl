# 普通尝试结果重送与限定行恢复

## 目标与依据

`OperationRepository.finish_attempt` 在原操作键重送时核实原尝试、实际结果和流程处置，返回原完整事务结束时的结果，不增加历史或重新执行设备操作。依据为[事务核实](../../camctl/database/transactions.md#公共完成与失败契约)、[操作与设备字段](../../camctl/database/operation-fields.md)、[事件审查](2026-10-02-camctl-event-contracts-review.md#未完成的消费者与恢复边界)和[精确值连续性](2026-10-02-camctl-history-read-review.md#正逆应用的值连续性)。文件划分和内部接口是实施建议。

## 输入、原事实与不变量

结果事件的变化行 `id` 是尝试的物理主键；票据的 `attempt_id` 是流程内的 `attempt_no`。从已保存行身份读取只读的 `run_id` 与 `attempt_no`，核对流程的责任键、目标及票据。`ValidatedOutcome` 必须保留校验时不可变的完整 `AttemptTicket`，消费时精确核对流程、尝试编号、责任、目标及操作类别。该上下文只用于运行时校验证明，不增加数据库字段或另存权威响应。无观察、无身份观察和带身份观察均遵守同一绑定规则；同一票据重新校验的等价结果可以使用，不要求同一个 Python 对象。第一次保存、已有终态的迟到结果和原键重送共用该检查。

| 原只读身份与当前票据 | 结果校验上下文与当前票据 | 处理 |
| --- | --- | --- |
| 不相符 | 任意 | 拒绝，数据库不变。 |
| 相符 | 不相符 | 拒绝，不能保存、交付迟到响应或复用。 |
| 相符 | 精确相符 | 继续按原键、结果及流程处置分类。 |

已结束尝试的状态、效果、结果正文和错误保持原事实，`result_event_id` 必须指向原结果事件；核对该事件的变化列与已结束记录，并按精确 JSON 语义比较全部结果输入。事件时刻是输入事实的时刻，原键重送沿用原时刻。结果正文比较包括外层版本、收场依据、观察顺序和实际调用信息；错误详情区分布尔与数字，数字比较不经 float。

流程字段仍可能由后来事务推进。原响应中的流程状态，以及原流程结束决定未改变的错误字段，须恢复至原完整末边界 H。只能使用同一写事务中可靠读取的当前值及边界 C，逆向解释固定 `(H, C]` 的历史；不能默认 `ACTIVE`、使用当前可变状态或增加第二份权威响应。

| 原键及输入分类 | 返回与副作用 |
| --- | --- |
| 原键不存在，票据和事实合法，尝试仍运行中 | 按现有结果、等待或结束规则保存完整事务。 |
| 原键不存在，票据合法，尝试已经结束 | 返回 `ALREADY_ENDED` 及可靠终态，不覆盖原结果。 |
| 原键存在，原阶段、目标、结果和处置全部相同 | 只读返回 `SAVED`、原尝试状态和 H 时的流程状态。 |
| 原键属于意图、读取配置恢复或其他阶段 | 拒绝复用，原历史与投影不变。 |
| 物理尝试、流程内编号、责任键、目标或票据与已校验结果不符 | 拒绝，不能把另一个请求与原身份组合。 |
| 状态、效果、完整结果、错误、事件时刻或流程处置不同 | 拒绝复用，不能把新输入解释为原事务。 |
| 原组、只读身份、已结束事实或恢复依据无法可靠解释 | 报错；不补默认值、不交付部分成功、不重新执行副作用。 |

| 原完整事务的组成 | 重送输入及原响应 |
| --- | --- |
| 单个 `ATTEMPT_RESULT`，分支为成功、失败或未知 | `retry_wait=False` 且没有 `run_finish`；返回 H 时实际流程状态，包括早已结束的流程。 |
| 上述结果及 `RETRY_WAIT/ENTER` | `retry_wait=True` 且没有 `run_finish`；结果、等待与同一流程身份相符；返回 H 时实际流程状态。 |
| 上述结果及 `OPERATION_CONFIGURED/FINISH` | `retry_wait=False` 且原 `RunFinish` 状态与错误相符；返回 H 时实际流程状态。 |
| 数量、顺序、分支、行身份或同行事实不符合上述分区 | 拒绝作为普通结果复用。 |

## 限定行恢复的边界

恢复只交付调用方明确请求的一行业务列，不交付完整对象、兄弟记录或依赖树。归属由调用方沿只读关联核实。限定行在 H 与 C 均须存在；若逆向遇到其创建事件，说明 H 时不存在，明确报错。未请求字段不进入恢复结果，也不用于声称完整行或完整对象一致。

复用现有事务范围核验、精确事件解码及值连续性规则。历史目录按对象及事件位置降序分批读取，批量上限建议为 128；只保留本批与最近事务的核验信息。页内事件可以不连续，目录累计次数必须连续。可靠当前对象的末事件必须与目录头一致；主表声明累计次数时，次数也须相同，没有主表计数的对象以目录次数为依据，具体分工见[报告管理恢复](2026-10-02-camctl-report-management-reuse.md#状态与输入契约)。H 的目录位置作为结束计数。每条事件属于其完整事务范围；正文非法、目录缺项、次数或字段值不连续时整项失败。SQL 错误保持实际异常，所有游标在交付和错误出口释放，函数不结束调用方事务。

| 恢复依据 | 结果 |
| --- | --- |
| H 与 C 都是完整边界，H 不晚于 C，列属于业务登记 | 继续读取固定区间。 |
| H 或 C 不存在、位于组中间、首尾或成员矛盾 | `ConsistencyError`。 |
| 请求列缺少当前可靠值、含主键或派生列 | `ConsistencyError`，不补造值。 |
| 目录头、当前对象元数据、区间累计次数或事件归属范围矛盾 | `ConsistencyError`。 |
| 后来事件改变目标行的请求列 | 核对当前恢复值与事件后值精确相等，再覆盖为前值。 |
| 后来事件不改变该行或没有请求列变化 | 不改恢复值，仍核对该条目录与事件。 |
| 目标行在 H 后创建，或必要列的值连续性失败 | `ConsistencyError`，不交付部分恢复。 |

## 实施顺序与门禁

- [x] F1：以真实保存入口建立结果单独保存、失败等待、成功或失败结束、未知结束、物理 ID 不等于流程内编号的重送反例。另覆盖异身份、异输入、异时刻与异阶段；失败须来自目标规则而非夹具错误。
- [x] F2：为限定行逆向值应用及目录读取建立纯单元反例，隔离 SQLite、登记和解码协作者。实现共享精确比较、完整 H/C 核验、目录计数和分页、明确存在性及游标释放。验证真实历史组合，不能用全库扫描或 JSON 数字经 SQLite/float 提取代替。
- [x] F3：先建立当前票据正确、结果校验上下文属于其他流程、编号、责任、目标或操作的反例，覆盖三个入口和无观察结果；真实另一目标的观察同样拒绝。同票据重新校验的等价结果允许。随后在模型与唯一验证生产者保留必填票据上下文，在三个入口共用边界精确核对全部字段，审计所有构造处，不只核对证据类别或观察身份。复用入口核对完整组成、只读身份与全部结果输入，恢复必要流程字段后返回原响应。不得把未变化列重新写入正文。
- [x] F4：验证后来重试成功、其他责任后来结束流程、保存结果前流程已取消、未变化流程错误，以及坏目录、坏历史和查询失败。确认重送不改变历史、投影、次数或派发资格。共用目标读取的查询确认和清理项游标须在正常、缺行及读取异常出口关闭一次；SQL 错误原样保留。
- [x] F5：两版 Python 单元和操作/历史相关集成通过，独立复核责任边界及反例；记录全量组件集成的实际结果并按名称保留既有失败，更新各计划和路线图后提交。

相关入口为普通结果保存及其迟到、重送路径；其他原键消费者分别按事件审查推进。本计划不关闭限定对象完整读取、`restore` 的范围与末事件审计或完整 H4。

## 验证记录

初始结果重送组合有 27 项目标失败及 3 项通过；完整身份上下文的反向组合另有 14 项目标失败及 5 项通过。修复后，`apps/camctl/tests/integration/operations/test_result_reuse.py` 的 66 项全部通过。真实组合覆盖提交前回滚后重送、提交已完成但收据丢失后核实、129 条配置变化同组跨页恢复，以及后来流程结束后原响应保持。成功结束的原错误为 `None`，测试明确核对该列未进入变化正文，重送仍能核对完整原错误。

两版 Python 分别执行组件单元组合，各有 1387 项通过、1 项取消选择及原有 2 条警告：

```sh
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv/bin/python -m pytest apps/camctl/tests/unit -q -k 'not test_accepted_record_delivers_receipt'
```

Python 3.11 使用 `~/.venv-camctl-contracts-311/bin/python`。取消选择的日志收据用例曾持续等待而未结束，不能视为通过。警告来自 `devices/test_read_session.py::test_source_stream_protocol_shape` 和 `operations/test_process.py::test_local_exit_is_exclusive` 的同步测试误标异步。

独立复核运行 102 项相关纯单元，全部通过。目标读取夹具阻断真实资源读取，并以独立源码模块实例隔离生产模块、枚举缓存和守卫登记；12 个游标分支核对正常、缺行、读取失败和查询执行失败的资源责任及原异常。票据生产者只有 `validate_outcome`，三个消费入口均核对完整原上下文；相同值的新票据对象允许使用。限定列恢复的证明范围没有扩大为完整对象。

Python 3.12 执行全量组件集成：

```sh
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv/bin/python -m pytest apps/camctl/tests/integration -q
```

实际结果为 864 项通过、9 项失败及原有 3 条警告。失败名称与[历史审查记录](2026-10-02-camctl-history-read-review.md#验证记录)相同，均位于 `outputs/test_qualification.py`：

- `test_qualification_uses_business_order`
- `test_same_time_obtain_wins_over_processing`
- `test_existing_read_protection_preserves_original_copy`
- `test_cleanup_restriction_rejects_and_records`
- `test_delete_in_progress_rejects_with_dedicated_code`
- `test_cross_device_candidates_do_not_block`
- `test_grant_creates_all_records_atomically`
- `test_internal_processing_grant_skips_delivery`
- `test_unavailable_output_rejects_without_records`

这些产物资格问题继续按各自责任修复，不能把全量集成视为通过。三条警告来自两个日志关闭用例未等待队列关闭协程，以及多进程同步测试误标异步。

Python 3.11 的相关集成组合有 422 项通过：

```sh
PYTHONPATH=/workspaces/camctl/apps/camctl/src:/workspaces/camctl/apps/camctl/tests ~/.venv-camctl-contracts-311/bin/python -m pytest apps/camctl/tests/integration/capture apps/camctl/tests/integration/operations apps/camctl/tests/integration/reporting apps/camctl/tests/integration/scheduling apps/camctl/tests/integration/outputs/test_sources.py apps/camctl/tests/integration/outputs/test_saved_errors.py apps/camctl/tests/integration/persistence/test_transactions.py apps/camctl/tests/integration/persistence/test_saved_transactions.py apps/camctl/tests/integration/history -q
```

文档本地文件链接及 `git diff --check` 通过；链接检查不校验标题锚点，新增锚点另按实际标题核对。普通结果复用的局部门禁通过；E3 的其他消费者、E4 证据规则及完整 H4 保持未完成。
