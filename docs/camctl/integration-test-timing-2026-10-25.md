# 集成测试耗时与精简评估（2026-10-25）

camctl 的 16 个组件集成目录已完成逐项计时：**10,018 项，其中 9,999 项通过、16 项失败、3 项跳过；目录墙钟合计 4,814.507 秒，即 80 分 14.5 秒**。四项容量与增长测试合计 1,825.066 秒，占目录墙钟的 **37.91%**。日常回归的主要调整方向是单独安排这些资源门槛，以及合并重复的真实数据准备；删除毫秒级断言对整体耗时影响很小。

本记录的源码基线为 `9da49b20dcd1273074bb70b918b36ee4f1eb095c`。本轮只采集耗时、审查覆盖并保存报告，没有修改生产实现、测试断言或测试运行策略。根目录跨组件 pytest 仍在执行，尚未计入本报告的总数；其结果及根目录 Node 测试将在完成后补充。

## 数据与计时口径

以下数据保存在版本控制内，不依赖开发容器中的临时日志才能查阅：

| 数据 | 内容 |
| --- | --- |
| [逐项耗时](verification-data/2026-10-25-integration-timing/camctl-cases.csv) | 全部 10,018 个参数实例的完整 nodeid、源码位置、结果、setup／call／teardown 及合计秒数；按合计降序排列。 |
| [按测试函数汇总](verification-data/2026-10-25-integration-timing/camctl-functions.csv) | 将同一函数的参数实例聚合，保留数量、结果与各阶段耗时。 |
| [按测试文件汇总](verification-data/2026-10-25-integration-timing/camctl-files.csv) | 按模块聚合，便于定位重复准备所在文件。 |
| [运行元数据](verification-data/2026-10-25-integration-timing/measurement.json) | 环境、实际 UTC、逐目录命令、退出码、墙钟时间与 CSV 摘要。 |

环境为 Linux x86_64 开发容器、CPython 3.11.17、SQLite 3.53.1，使用 `apps/camctl/.venv311/bin/python`。实际执行日期以元数据中的 UTC 时间为准。每个目录独立启动一次 pytest，命令形态如下，具体目标与环境变量见元数据：

```sh
apps/camctl/.venv311/bin/python -m pytest \
  apps/camctl/tests/integration/<目录> -q --durations=0 -p timing_plugin
```

同一工作区只有一个集成 pytest 进程，遵守[组件测试隔离要求](../../apps/camctl/tests/AGENTS.md#运行边界与结果记录)。参数实例只测量一次，这些数字不是多轮中位数。

`total_s` 为 pytest 的 setup、call、teardown 之和。模块级或 session 级 fixture 的成本计入首次／末次消费者；测试函数或辅助函数内部的建库、拍摄准备及报告生成计入 call，不能把 call 全部解释为目标业务操作耗时。目录墙钟还包含启动与收集，全部实例的阶段耗时合计为 4,786.297 秒。失败只测量到实际失败位置；跳过原因保留在 CSV 中。

完整命令回执、phase 事件、fixture 计时及原始输出位于本地 `.local/verification/integration-timing-20261024T181943Z/`，目录名使用本轮实际 UTC。

## 目录结果

| 目录 | 项数 | 通过 | 失败 | 跳过 | 墙钟秒数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| acceptance | 338 | 338 | 0 | 0 | 68.687 |
| bootstrap | 1,367 | 1,353 | 13 | 1 | 737.271 |
| cancellation | 396 | 396 | 0 | 0 | 115.528 |
| capture | 2,128 | 2,127 | 1 | 0 | 645.806 |
| contracts | 442 | 442 | 0 | 0 | 13.406 |
| devices | 223 | 221 | 2 | 0 | 15.442 |
| history | 345 | 345 | 0 | 0 | 1,489.562 |
| host_files | 121 | 121 | 0 | 0 | 3.291 |
| logging_runtime | 112 | 112 | 0 | 0 | 1.949 |
| motor | 12 | 12 | 0 | 0 | 1.652 |
| operations | 314 | 314 | 0 | 0 | 15.125 |
| outputs | 2,518 | 2,516 | 0 | 2 | 526.983 |
| persistence | 925 | 925 | 0 | 0 | 1,058.986 |
| reporting | 447 | 447 | 0 | 0 | 104.780 |
| scheduling | 136 | 136 | 0 | 0 | 6.446 |
| session | 194 | 194 | 0 | 0 | 9.593 |
| **合计** | **10,018** | **9,999** | **16** | **3** | **4,814.507** |

## 日常回归可单独安排的四项资源门槛

这四项仍属于集成测试。建议为它们提供显式执行组，在发布验收及约定周期完整运行；日常功能回归按变更影响选择是否执行。该调整尚未实施，当前默认命令仍会运行它们。若实施，须同步测试运行指南与门禁入口，避免资源覆盖从执行流程中消失。

| 测试 | 本轮秒数 | 必须保留的资源边界 |
| --- | ---: | --- |
| `history/test_record_segment_history.py::test_large_growth_crosses_page_and_disk_cache_capacity` | 951.410 | 256／2,048 成员、成员数减二的缺口、批次不超过 7、无需旧事件解码、磁盘超过 2 MiB 缓存容量，以及 Python 峰值与查询步骤的增长限制。 |
| `persistence/test_segment_transaction_batches.py::test_fixed_batch_write_and_verification_have_bounded_residency` | 574.573 | 20／200／400 产物的真实批写和原结果核实；分别测量内存峰值，保留 fetch 上限、完整事务检查次数、索引和 VM 步数断言。 |
| `history/test_report_selection.py::test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window` | 176.783 | 默认 4,096 候选缓存溢出后的释放、按键续页不重扫、稀疏水位窗口及索引查询计划。 |
| `history/test_record_segment_history.py::test_growth_has_bounded_python_peak_and_linear_projection_reads` | 122.300 | 16／64／256 三档成员及缺口增长、投影读取量、Python 峰值和完整快照流。 |
| **合计** | **1,825.066** | **30 分 25.1 秒，占本轮目录墙钟 37.91%。** |

附近的小型集成用例已经分别覆盖批写原子可见性、末批及最终守卫回滚、原键重放、真实 COMMIT 回执未知后的核实，以及历史恢复的正常与损坏路径。它们不能代替上表的空间和复杂度信号，因此上述建议只改变日常执行选择，完整验收仍承担这 30.4 分钟的成本。

不能通过将 2,048 改小、将默认缓存改小或取消实际生产写入来保留原资源门槛的名称。`persistence/test_segment_result_batches.py` 的 128+1 边界等功能用例也不应因规模较大而一并移走。

## 可保留覆盖并合并准备的候选

以下为静态审查确认的结构性候选，尚未实施或取得改造后耗时。表中的“当前成本”是相关实例的实测合计，不是保证可节省的时间。

| 候选 | 当前成本 | 合并方式与覆盖约束 |
| --- | ---: | --- |
| history：取消中途保存与同 S 字节完整性 | 117.190 秒，2 项 | 两者都建立同一 220 成员世界。先完成真实 worker 中途取消、来源文件生命周期及快照提交检查，再在同一 H 生成第二份独立磁盘来源，验证既有 S 的字节一致性、偏移 65,540 的来源损坏与已存 BLOB 损坏。可少建一次完整世界。 |
| reporting：缺段投影与清理后重建 | 44.553 秒，2 项 | 都使用 `_source(..., (7, 8, 10))` 和相同冻结前缀。先验证保留产物、默认范围、9 号缺口及字段边界，再清理并按原冻结依据重建，保留 bytes／size／SHA 全部断言。 |
| cancellation：固定取消范围的拒绝矩阵 | 见下表 | 按真正改变初始事实的状态和寻址方式建世界；同一世界内逐项验证只读或完整回滚的非法输入。成功提交矩阵继续使用独立世界。 |
| bootstrap：安装版样例生成器的能力输入 | 21.948 秒，14 个非法时间实例；另有 11.117 秒的 4 个合法样例 | 按型号共享一次安装版 `describe` 的只读能力文档。保留全部安装脚本执行及各自输入／输出路径；合法样例仍分别 init／submit 并核 SQLite。现有 wheel fixture 已是模块级，不应再按实例数估算重复安装。 |
| capture：目录轮换与跨页发布 | 20.262 秒，2 项 | 将 CAM_001/0999→CAM_002/0001 的目录轮换输入并入既有跨页发布用例，明确跨页排序、原片／预览配对、默认范围及无媒体拷贝。保留 preview-only 起点与未知顺序的独立场景。 |
| outputs：候选容量的三次只读扫描 | 16.253 秒，3 项 | 同一真实数据库先生成 12 个候选并扫描，再补到 120；分别用新的发现 owner 与连接探针以批次 1 和 3 扫描。保留全部数量、弱引用释放、游标上限和查询计划断言。 |
| contracts：源码与 AST 解析 | 约 10.55 秒，相关规则 | 一次读取并解析只读源码快照，各规则独立遍历和断言。合成违规输入继续分别解析，保留导入图及空包锚点；不删除任何规则。 |

### 两项快照测试的执行顺序

具体目标是 `history/test_snapshot_fix_round1.py::test_cancel_started_save_keeps_source_until_actual_completion[middle]`（58.691 秒）和 `history/test_record_segment_history.py::test_snapshot_same_s_compares_all_bytes_and_broken_content_fails`（58.499 秒）。`first`、`later_source`、`rollback` 三个取消分区继续保留。

快照维护只写快照及维护进度，不推进业务历史 H；在 H 仍为当前头部时，默认 `auto` 路径仍从当前投影准备来源。因此可以在首个来源被正确清理之后，从同一 H 准备第二份来源并检查既有快照内容。串联前必须恢复保存函数与分块属性上的暂停包装，避免第二阶段继承门闩。第二份来源须使用不同文件，长度大于 65,540，实际读取损坏位置的一字节后再改写；损坏存储 BLOB 及增加版本的操作放在最后。

验收要同时保留取消后 worker 实际完成、文件在执行期存在且完成后删除、提交行数正确、H 不变、同 S 内容核实成功，以及两种真实字节损坏被拒绝。串联会使第一阶段失败时第二阶段不再执行，需在断言中清楚区分阶段。若保留两个独立 pytest 节点，则可研究只读生产基线加独立数据库副本，但连接与 runtime 重建成本更高。

### 取消矩阵的准备次数

这些函数都在 `cancellation/test_hasselblad_fixed_request_scope.py`：

| 函数 | 实例数 | 本轮秒数 | 可研究的世界数 |
| --- | ---: | ---: | --- |
| `test_original_source_cannot_disappear_behind_preview_label` | 48 | 21.048 | 按 `(state, kind)` 从 48 次降到 8 次，仍执行三种 basis 与两个入口的全部拒绝。 |
| `test_complete_model_rejects_omissions_extra_members_and_wrong_effects` | 14 | 6.302 | 按 `(state, kind)` 分为 3 个世界，保留每种错误成员与效果及两个入口。 |
| `test_original_sql_and_complete_rows_keep_same_start_partition` | 14 | 5.657 | 每种 state 建一次，共 7 次；在同一事实行上分别检查 HB 与 generic driver。 |
| `test_complete_scope_requires_authoritative_address_links_and_start_ranges` | 10 | 4.593 | 按 kind 分为 4 个世界，保留全部覆盖键缺失反例。 |

当前 `reject()` 只要求仓储结果不是 `COMPLETED`。实施同库循环前，应明确断言 `ROLLED_BACK`、连接没有遗留事务，并在每次操作后比较完整数据库 dump 和取消标记。`test_authoritative_complete_scope_keeps_valid_direct_and_linked_members` 的 20 项成功提交及 `test_foreign_residual_keeps_each_mixed_source_preview_scope` 的 24 项残余状态组合继续保留；不能让后续条件变成对已固定集合的重放。

### 收益较小的整理

`outputs/test_catalog_integrity.py::test_catalog_conflicts_never_become_selection_facts` 的 40 项共 2.614 秒。20 种损坏可各自在同一世界内顺序执行 read 与 fix，并分别检查异常、回滚和数据不变；收益较小。

`bootstrap/test_timelapse_finish.py` 的多文件成功用例可并入等待成功用例，保留最初发送／等待安排、两个独立文件及产物身份、数量与大小断言；可少运行一次约 4 秒的会话。未完成文件与未结束集合若合成三轮，默认每轮仍有 3 秒重试间隔，不能把整条被移除用例的耗时算作净节省；改零间隔前还需明确调用门闩，避免轮询错过中间状态。

## 可以省略或下移的局部断言

`acceptance/test_validation.py` 中 6 个 `TestPreciseNumbers` 实例和 `test_invalid_schema_is_rule_error` 共约 **0.02075 秒**，其精确行为已有单元测试覆盖。集成版会使用真实资源 registry，单元版则替换 registry；保留其余真实正文受理及引用解析用例后，可移除这 7 项重复断言。该整理主要减少维护重复，对速度几乎无影响。

`host_files/test_capture_sources.py` 的文件描述值校验、`workspace_path` 纯路径组合、`CapturedArtifact` JSON 编解码三项可迁入单元测试，需先保留对应的单元覆盖；实际归档、fsync、线程取消及文件核验继续在集成层。`bootstrap/test_package.py::test_resource_names_reject_arbitrary_paths` 同样可隔离资源登记集合后迁入单元测试，wheel 资源摘要与独立安装闭环继续保留。这些均不是本轮的主要耗时来源。

## 保留覆盖并继续定位的慢项

| 范围 | 本轮成本 | 判断 |
| --- | ---: | --- |
| capture 原生延时空尾矩阵 | 178.431 秒，4 项 | 尾空／中间空分别产生不同结果，取消又覆盖成功与失败来源的优先级。业务钟已手动推进，不能把约 44 秒每项归为自然等待。四种结果保留，后续单例栈采样再确定耗时环节。 |
| persistence 满批来源的普通／有界提交 | 162.938 秒，2 项 | 两条提交验证路径都要覆盖真实 128+1 页与成员批。可评估共享只读生产基线、分别复制后执行，不能把第二种模式变成已固定集合的重放。节省量尚未测出。 |
| persistence 跨页预览配对 | 47.485 秒，1 项 | 真实页边界与成员批边界上的原片／预览配对仍需覆盖；少一行数据不会解决主要耗时。 |
| persistence 多页成员与大量零段缺口 | 44.064 秒，1 项 | 131 个成员产生 129 个内零缺口，验证 128+1 缺口写入及第二结果页，不能直接缩小为普通小样例。 |
| persistence 未知键游标 | 31.439 秒，1 项 | 保留未知 key 的有序且无重复续页。缩到较小规模会改变它同时覆盖的跨源页和写批交叉条件，需先明确由哪些用例承接。 |
| reporting Action6 两种动作的历史报告 | 17.748 秒，2 项 | 普通录像的 `duration_s` 与原生延时的 `frames` 是不同合同，两态均保留。 |

outputs 默认、预览、显式 ID 及组／计划范围已在本轮之前完成六次来源准备合为三次的优化。本轮三个对应测试分别为 54.657、54.031、53.877 秒；当前数字已包含该优化，不能再次把它列为待实施收益。清理冷恢复与 pending 来源的消费者边界继续保留。

### 已取得的调用栈证据

history 大规模测试执行中进行了 15 秒、20 Hz 的非阻塞采样，取得 227 个有效样本、72 次采样错误。206 个有效样本包含 `_large_fixture`，195 个包含 `_verify_page_relations`；这些调用栈彼此重叠，只描述该窗口。源码也表明，tracemalloc 与查询增长测量在 `_large_fixture` 返回后才开始，因而该窗口的重成本在真实数据构造阶段。

`segments` 的逐成员核验及缺口构造会进入结果页读取，再验证整页关系；多条成员引用同一页时，该证明可能重复执行。persistence 大规模批写测试的一次实时栈也在结果页关系验证中。此处是由源码与采样支持的性能调查方向，尚未证明完整调用次数或全用例成本占比。后续若优化事务内的重复证明，须独立保持原页身份、固定历史边界及完整事务校验，不能直接取消守卫。

## 失败与跳过

这 16 项失败都已保留在统计中。没有因为失败或耗时而移除它们；本轮也没有据失败位置推断新容器是根因。

| 文件 | 数量 | 失败范围 |
| --- | ---: | --- |
| `bootstrap/test_distribution.py` | 2 | `test_installed_hasselblad_photo_pair_obtain_and_source_reuse[raw/jpeg]`。 |
| `bootstrap/test_osmo_shared_recovery.py` | 10 | 原结果页 partial／terminal、原读取 missing／mismatch、boundary／horizon／authorization、真实未知提交恢复，以及新结果轮次 none／partial。完整 nodeid 在逐项 CSV 中。 |
| `bootstrap/test_osmo_video_mode_completion.py` | 1 | `test_osmo_native_segment_resume_publishes_saved_conclusion_without_device_calls`。 |
| `capture/test_action6_budget_cold_recovery.py` | 1 | `test_action6_cold_recovery_decodes_saved_budget_and_continues_same_copy`；实际读取偏移 `[0, 0]`，预期 `[0]`。此前已有干净基线复现，见[环境恢复验证记录](verification.md#测试准备与等待矩阵提速)。 |
| `devices/test_adb_camera_registration.py` | 2 | `test_real_session_open_resets_settings_but_catalog_reads_do_not[dji-action6/dji-osmo360-ii]`；本轮未做新的干净源码对照，归因待核对。 |

三项跳过分别是 Windows 大小写别名核对、主机来源不携带读取配置的无效参数组合、内部副本不执行普通 delivery 发布的无效参数组合。准确 nodeid 与原原因均在逐项 CSV 中。

## 后续安排

1. 完成正在运行的根目录跨组件 pytest、4 个 Node 文件及已安排的单个 native 热点诊断，将实际结果补入本报告；不重跑已完成的 16 个目录。
2. 若实施提速，先建立四项资源门槛的显式执行入口及发布执行责任，再调整日常回归选择。该步只能缩短日常回归，不能宣称完整验收成本消失。
3. 优先实施两个 220 成员快照用例与两个缺段报告用例的准备合并，再处理取消拒绝矩阵和安装版能力导出复用。每项先保留原断言并测量受影响文件，之后按实际变更范围运行目录门禁。
4. 对 native 与真实大批次结果页验证做专门性能定位。只读数据库基线的复制属于候选方案；是否值得实施取决于准备成本占比，以及是否仍能保持原提交、重开与恢复边界。

上述步骤是后续建议，尚未改动测试组织或实现。行为验收继续遵守[测试规则](../../.agents/rules/testing.md)及对应目录的运行指南。
