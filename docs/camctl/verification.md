# 软件验证与实施顺序

[Hasselblad 真机验证与 H6 部署交接（2026-10-28）](hasselblad-real-device-verification-2026-10-28.md)记录安装版 C host 与真实机身的参数、双源取回、取消、未知活动恢复及有限失败，以及现场暴露的绑定和 JPEG 校验问题。真实媒体、供电见证、软件修复与尚未完成的范围分别列明；客户端不在本次范围。

[关键阶段与命令日志验证（2026-10-26）](operational-logging-validation-2026-10-26.md)记录统一 JSON、INFO 阶段与实际命令、DEBUG 耗时明细的最终单元和定向集成结果，以及现有真机验证环境的安装一致性；扩大检查中的旧测试失配单独保留。

## 照片缓存绑定失败的冷恢复（2026-10-28）

源码 `7351cee5f318bff00719561c2cfa1d0795ab446f`（包含 `d51eadbe`）修复原读取绑定失败已保存、所属交付失败尚未提交时的恢复。直接设备源沿原路径；照片缓存从原结果事务的首事件固定历史边界，核实原 H、角色页、归档及 READ_END 与摘要不符事实，解析原设备来源后接续既有失败事务。取消或终态已经生效、缓存已清理时，仓储仅在原读取结果已核实且将返回既有只读收场的分区采用历史核验；活跃申请和没有原结果键的申请继续核当前资格，已保存业务键继续核原事务。原错误、结果键、时刻、身份及取消规则保持，恢复不新增读取、重拷或交付。

环境为 Linux 开发容器、CPython 3.11.17，工作目录为 `/tmp/camctl-photo-binding-cold-recovery-20261028`。两个反例分别经 `advance_obtain` 和 `OutputsRepository.fail_read_binding` 入口证明旧代码拒绝合法照片缓存及取消后的历史收场，均退出 1。修复后，既有 `test_publication_discovery_isolated.py` 文件 7 项通过，既有直接设备恢复单例 1 项通过；主代理另在最终源码运行受影响的 `test_canceled_publication_isolated.py` 完整文件，48 项、15 个子测试通过，2.60 秒，三组均退出 0。各组在独立进程执行，使用源码及内存协作者；原装配在导入前隔离包资源与生产协作者。绿测命令为：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/outputs/test_publication_discovery_isolated.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/outputs/test_canceled_read_end_recovery_isolated.py::CanceledRecoveryTests::test_binding_failure_recovery_preserves_original_identity_and_key -q
env -u PYTHONHOME -u PYTEST_ADDOPTS PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src /workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/outputs/test_canceled_publication_isolated.py -q
```

新增用例复用现有装配，从取回调度和仓储入口覆盖配置缺失、驱动不符、只读终态或取消退休、实时资格保留及错误来源拒绝，核对原申请和历史边界；不另建测试文件或框架。原结果加载和照片历史核验器在该单元中使用替身，因此拒绝传播不代表真实守卫对普通 HOST 或错误关联的组合已经验证。GPT-6-Astra 独立复核原事务边界、业务键重送、取消和终态分类后未发现剩余阻断。原始日志、命令及退出码收据归档于 `.local/verification/photo-binding-cold-recovery-20261028/`。未运行软件集成、真实仓储与 SQLite 冷重启、设备、host、构建或安装验证；真机环境未更新。

## implement_camctl 合并的软件验证（2026-10-28）

在 `fix/capture-results-and-delivery-recovery-20261028` 合入 `implement_camctl` 的 `07c13be7a6b7b15564ad4086dee4d9c47f7e0d0c`；合并前本分支为 `2312207b889707f1b128eac01af50b93c17bd4fc`，共同基线为 `61e73c8ac4e32dc5511664e0dd9f6c0a167926d3`。本地来源分支包含已拉取的远端提交。本次保留拍摄结果、发布取消及错误诊断修复，并接入 Hasselblad 原机来源、同步副本与有限缓存恢复。

RESULTS 恢复先持有原实际返回或未知结果，再由消费者决定重试或结束；Hasselblad 使用原 `local_result_recovery` 证据。DEVICE 照片的 `LISTED/CLOSED` 消费者先恢复可靠角色页的文件责任，再保存 RESULTS 结论。取消取回仅核对照片缓存的历史关联，缓存释放或清理不阻断同键及终态新键恢复；首次读取资格仍要求当前可用的缓存。

环境为 Linux 开发容器、CPython 3.11.17，工作目录为 `/home/cld/.codex/worktrees/7426/camctl`，解释器为 `apps/camctl/.venv311/bin/python`。各组在独立进程执行；清除 Python 与 pytest 外部选项、禁用插件自动加载和字节码写入。导入前隔离协作者或包资源，并阻止真实数据库连接、线程及子进程；只读取源码及使用内存替身。最终六组均退出 0，禁止器触发次数为 0。

| 范围 | 结果 |
| --- | --- |
| `capture/test_hasselblad_device_results_isolated.py`、`capture/test_result_recovery_owner_isolated.py` | 25 项通过。 |
| `outputs/test_publication_discovery_isolated.py`、`outputs/test_canceled_publication_isolated.py` | 53 项、15 个子测试通过。 |
| capture 的六个 `test_photo_*_isolated.py`；devices 的 `test_hasselblad_device_response_isolated.py`、`test_hasselblad_frozen_storage_isolated.py`；host_files 的 `test_photo_cache_reader_isolated.py`、`test_photo_result_workspace_isolated.py`、`test_photo_sync_observation_isolated.py` | 113 项通过。 |
| [拍摄结果与普通交付恢复](#拍摄结果与普通交付恢复的软件验证2026-10-28)第一组中除 `test_result_recovery_owner_isolated.py` 外的十个文件 | 133 项、8 个子测试通过；原结果拥有者另计入第一行。 |
| [错误日志投影与诊断复用](#错误日志投影与诊断复用的软件验证2026-10-28)第一组四个隔离文件 | 128 项通过。 |
| `host_files/test_capture_sources_jpeg_boundary.py` | 101 项通过。 |

以上路径均相对于 `apps/camctl/tests/unit/`，共 553 项、23 个子测试；重复运行不重复计数。命令、隔离入口及完整日志收据保存在 `tmp/implement-merge-20261028/`。新增接缝用例分别证实登记故障不结束原 RESULTS、缓存清理后历史关联仍可核验而不授予新读取资格，以及取消恢复拒绝替换缓存身份。取消恢复样例的交付此前已失败，原取消组只含动作终态，返回的首次清理集合为空；该样例不证明 PUBLISHING 的非空清理集合恢复。既有 AST 抽取用例只验证恢复持有逻辑，不作为真实组件组合证据。

独立静态复核覆盖两边的 capture、outputs、装配、诊断及数据库契约接缝，修正问题后未发现剩余阻断项。52 个变更 Python 文件的 AST 解析、事件转换 JSON 解析及差异空白检查通过。最初测试入口过度限制了无 I/O 的源码导入，禁止器在导入发生前中止；补齐必要的纯源码白名单或资源替身后执行上述最终范围，没有读取真实包资源。接缝红测与测试装配失败不计为最终通过。

本次没有新增或运行集成测试；来源分支中一个既有组件集成用例的 HOST 模式参数变更随分支合入，该用例没有运行。部署脚本从源码构建并生成能力说明，未增加部署依赖或配置字段，脚本无需调整。本次没有构建安装包、部署既有环境，也未执行 TX2、真实相机或物理断电验收。


## 错误日志投影与诊断复用的软件验证（2026-10-28）

[修复计划](../superpowers/plans/2026-10-28-error-log-projection-and-ownership.md)完成成功设置 stdout 摘要、同记录重复错误引用、会话最终摘要复用原诊断，以及报告内部通信的异常图边界保存。实际调用结果、数据库证据、机器会话 reason/details 保持各自的原编码；新收场错误及诊断登记失败仍交付完整适用事实。

受测源码为 `1345a4291822db9686828bcd04be80c37b4db932`，环境为 Linux 开发容器、CPython 3.11.17，工作目录为 `/home/cld/.codex/worktrees/7426/camctl`，解释器为 `apps/camctl/.venv311/bin/python`。使用显式源码路径，禁用插件自动加载及字节码写入；旧异步用例只显式启用 pytest-asyncio。单元替身隔离包资源、业务文件、数据库、网络、线程和子进程，被测源码加载保留。各范围按进程可变模块状态分别执行。

| 范围 | 结果 |
| --- | --- |
| 公共投影、会话来源及控制选择、最终格式函数使用实际纯投影、H4 原调用消费者，共 4 个隔离文件 | 128 项通过，6.43 秒，退出 0。 |
| 7 个既有日志单元文件、异常图编码器、CLI JSON 诊断和本地收场异常图，共 10 个文件 | 217 项通过，1.43 秒，退出 0；资源、线程、数据库及子进程禁止器触发次数为 0。 |

执行命令为：

```bash
env -u PYTHONHOME -u PYTEST_ADDOPTS -u CAMCTL_TIMING_REPO -u CAMCTL_TIMING_DATA PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/logging_runtime/test_projection_isolated.py apps/camctl/tests/unit/session/test_outcome_diagnostic_sources_isolated.py apps/camctl/tests/unit/bootstrap/test_error_projection_handoff_isolated.py apps/camctl/tests/unit/operations/test_h4_diagnostic_consumer_isolated.py -q --tb=short
env -u PYTHONHOME -u PYTEST_ADDOPTS -u CAMCTL_TIMING_REPO -u CAMCTL_TIMING_DATA PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python tmp/error-log-repair-20261028/run_audited_units.py
```

第二组入口只包含 `operations/test_error_diagnostics.py`，日志目录的 `test_diagnostics.py`、`test_log_diagnostics.py`、`test_diagnostic_durability.py`、`test_diagnostic_reservations.py`、`test_diagnostic_write_receipts.py`、`test_logger.py`、`test_standard_logging.py`，以及 bootstrap 的 `test_cli_json_diagnostics.py` 和 `test_local_work_error_diagnostics.py`。完整禁止器、命令、源码、进程回执和日志收据保存在 `tmp/error-log-repair-20261028/`。

既有布局断言已迁移到可解析引用、稳定诊断身份和结构化摘要，原错误字节、完整异常组、共享回执、顺序和控制对象断言保留。旧 `test_session_terminal_diagnostics.py` 仍因顶层导入真实登记资源而未运行；其中六种直接异常与两种失败结果分区，已由隔离的实际会话入口承接，保留 23 成员宽异常组、16 层深异常组及取消对象身份。新增最终格式函数单元使用真实纯投影函数，实际文件通道、队列、进程和数据库均使用替身。

独立静态评审覆盖投影、预编码及嵌套异常图、报告编码与解码、stderr 回退、会话来源合并、报告 worker 收场、诊断登记失败和后到控制异常。处理确定问题后复核未发现剩余阻塞。17 个变更 Python 文件的 AST 解析与差异空白检查通过。部署依赖、配置和能力说明不变，部署脚本按原目录复制源码，工作规则及部署脚本不需调整。

受控的 16 次成功设置、1 次失败设置样本，原始展开为 418568 字节，日志投影为 2977 字节；该用例另行输出体积收据并退出 0，已包含在上述通过数内。测试同时证明源对象不变。此数值只表示合成 JSON 输入的投影变化，不代表客户日志重跑、目标吞吐或真实相机验证。

本次没有新增、修改或运行集成测试，也未执行全量单元套件、真实报告进程通信、安装包、TX2、真实设备及物理断电验收。文件日志和 stderr 都不可用时继续保留原控制传播及停止重试边界，不能保证诊断输出。


## 拍摄结果与普通交付恢复的软件验证（2026-10-28）

[修复计划](../superpowers/plans/2026-10-28-capture-result-and-publication-recovery.md)完成首次配对的事务内边界固定、原 RESULTS 结果与业务处置共同保存，以及普通 PUBLISHING 的发现和取消收场。公共路径覆盖 Action6、OSMO 360 II 与 Hasselblad；Hasselblad 主机照片继续使用原 START 及文件恢复依据。

最终受测生产源码为 `f1aa91a97d8d2d82842ecb9570bb658d83232a30`。环境为 Linux 开发容器、CPython 3.11.17，工作目录为 `/home/cld/.codex/worktrees/7426/camctl`，解释器为 `apps/camctl/.venv311/bin/python`。显式指定源码路径、禁用插件自动加载及字节码写入，隔离真实包资源、数据库、业务文件、网络、线程和子进程；只加载被测源码与内存协作者。

| 独立进程范围 | 结果 |
| --- | --- |
| 配对、原结果拥有者与消费者、Hasselblad 恢复边界、取消录像读取、普通停止、分段完成、内部绑定、可用性装配、设备可用性及取消读取收场，共 11 个隔离文件 | 145 项、8 个子测试通过，4.52 秒，退出 0。 |
| 普通发布发现与取消发布恢复，共 2 个隔离文件 | 52 项、9 个子测试通过，2.28 秒，退出 0。 |

完整命令如下；两组在独立进程中运行，发布测试要求进程没有先前加载的 `camctl` 模块。

```bash
env -u PYTHONHOME -u PYTEST_ADDOPTS -u CAMCTL_TIMING_REPO -u CAMCTL_TIMING_DATA PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/persistence/test_result_pairing_atomic_isolated.py apps/camctl/tests/unit/capture/test_result_recovery_owner_isolated.py apps/camctl/tests/unit/capture/test_result_consumers_isolated.py apps/camctl/tests/unit/capture/test_h1_recovery_boundaries_isolated.py apps/camctl/tests/unit/capture/test_canceled_processing_read_isolated.py apps/camctl/tests/unit/capture/test_normal_recording_stop_isolated.py apps/camctl/tests/unit/capture/test_segment_completion_recovery_isolated.py apps/camctl/tests/unit/capture/test_saved_internal_binding_recovery_isolated.py apps/camctl/tests/unit/operations/test_availability_binding_assembly_isolated.py apps/camctl/tests/unit/devices/test_h2_availability_port_isolated.py apps/camctl/tests/unit/outputs/test_canceled_read_end_recovery_isolated.py -q --tb=short
env -u PYTHONHOME -u PYTEST_ADDOPTS -u CAMCTL_TIMING_REPO -u CAMCTL_TIMING_DATA PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest -p no:cacheprovider apps/camctl/tests/unit/outputs/test_publication_discovery_isolated.py apps/camctl/tests/unit/outputs/test_canceled_publication_isolated.py -q --tb=short
```

配对和 RESULTS 的独立评审核对原申请、事务、守卫、恢复和各设备消费者。发布评审进一步核对持文件资格后的状态重读、观察期间取消、已发生交接、普通未知失败的两个出口、原键重送、目标独立发现、分页、有效父明细及首次清理；处理发现后复核未发现剩余阻塞。24 个变更 Python 文件的 AST 解析、事件转换 JSON 解析和差异空白检查通过。部署脚本按目录复制源码和权威资源，本次没有新增依赖、配置或能力声明，脚本及工作规则不需修改。

验证期间有一轮新增单元用例因延迟导入未隔离，触及真实包资源目录，随后在资源名检查失败；没有读取到资源字节，也没有访问数据库或设备。该轮结果作废，补齐贯穿模块执行的导入阻断器和资源失败替身后重跑；原取消证据守卫覆盖保留。另一次合并运行中，前序 AST 测试留下的合成模块使发布测试的清洁进程检查中止，随后按上述独立进程范围执行。配对与 RESULTS 合并时缺失的协作者替身已补齐，没有放宽断言。

原始命令、日志、源码及退出码收据保存在 `tmp/capture-result-repair-20261028/`。本次没有新增或修改集成测试文件，也未调用集成测试入口；上述资源目录触达属于验证越界，已经作废。三个既有 `test_flow_events.py` 调用点只补齐 staging 参数，该文件未运行。真实 SQLite／文件／执行器组合、安装包、TX2、真实相机及物理断电均未验证，单元与静态评审结果不替代这些验收。


## 错误日志的断电持久性软件验证（2026-10-27）

[分级写入完成要求](../architecture/logging.md#分级写入完成与断电持久性)将 ERROR／CRITICAL 的成功回执绑定到文件及必要目录同步；普通调用继续立即提交。原记录的追加、持久性及 stderr 交付分别保存，副本失败不会重写已经追加的原记录。[实施计划](../superpowers/plans/2026-10-27-error-log-durability.md)已完成软件范围，使用方式见[通用日志入口](logging-runtime.md#日志异步投递入口与有界队列)。

最终受测源码为 `b3d0ebf237f173b754c5bd56078c53cf2e37b6b8`。环境为 Linux x86_64 开发容器、CPython 3.11.17、SQLite 3.53.1，工作目录为 `/workspaces/camctl`，解释器为 `apps/camctl/.venv311/bin/python`。显式绑定所属源码及测试路径，清除计时和 Python 环境变量，逐进程串行运行。实际 UTC 为 11:23:41—11:26:26，以下全部进程退出 0；命令统一为 `python -m pytest <范围> -q --tb=short`。

| 检查范围 | 实际结果 |
| --- | --- |
| `apps/camctl/tests/unit` | 9696 项通过、1 项跳过、2 条既有 asyncio 标记警告。 |
| `apps/camctl/tests/integration/logging_runtime` | 完整目录 295 项通过、1 条既有 asyncio 标记警告。 |
| `apps/camctl/tests/integration/bootstrap` 的五个日志文件及六个副本节点，精确命令保存在收据中 | 31 项通过；含真实生产装配、文件／目录同步失败、原控制异常及副本拥有者诊断。 |
| `apps/camctl/tests/integration/session/test_custom_delivery_durability.py` | 6 项通过；公开 `SessionContext`／`run_session` 覆盖三种自定义交付形状及两类写入事实。 |
| `apps/camctl/tests/integration/outputs/test_read_diagnostics.py` | 14 项通过。 |
| `apps/camctl/tests/integration/persistence/test_snapshot_error_diagnostics.py` | 2 项通过。 |

348 项相关集成保留取消、禁用、正常排空及原异常去重，新增真实文件同步门闩及失败见证。INFO 先创建或轮换后的 ERROR、新建目录链、日志文件符号链接的实际目标目录及正式名字目录均覆盖；同步失败仍保留实际追加事实，不确认持久成功。轮换使用正式名字间的硬链接，覆盖搬迁、删除及整理期间的中断，恢复先按文件身份去重和压紧，再判断正常淘汰。保留数为 1 的截断及最旧超限归档删除保持原规则。

错误收场分别核对文件操作控制与副本既有有序异常选择，保持原异常对象、因果及完整写入事实。实际副本拥有者在原日志同步失败后交付一次完整错误诊断；原日志已持久而复制、发布或解锁失败时，仍保留原日志成功事实。stderr 入口、不可读／不可附加的异常事实和关闭摘要均通过公共入口验证，不能用附属诊断失败改写原控制或已经完成的写入。

最终独立审查核对相同生产文件摘要，并以 9 个公共分区复核旧副本异常选择、独立解锁控制及两入口的符号链接目录同步，退出 0。分域较早探针各自保留实际快照，不计为最终来源重跑。完整命令、UTC、环境、来源、日志及退出码位于 `.local/verification/error-log-durability/final/`；分域、基线与独立审查原始证据分别在同级 `agents/`、`review/`，首次合并检查保存在 `attempt-df5d911b/`。

文档的 5777 个本地文件链接检查及差异空白检查通过，新增标题锚点另行核对。

首次合并检查为单元通过、日志集成 282 项通过及 2 项副本异常选择回归；保留旧断言修复后，最终完整日志目录通过。额外扩大 bootstrap 检查中的旧报告调度等待超时、低层接纳的 stderr 断言及报告身份文案断言，均在 `08abcafb` 隔离基线复现，未计为本轮最终范围通过。失配的格式化及轮换故障替身已迁移到实际边界，保留原断言；旧 session 三个形状因已废弃的 `open_connection` 装配在进入目标前失败，其覆盖由上述公开会话六分区承接，未将原文件称为通过。

本记录证明软件同步调用、顺序、失败处理及回执接线，不替代目标 ARM Linux 文件系统和存储设备的物理断电验收。本项未部署、未更新真机验证安装，未执行真实相机、目标性能、Windows 或 macOS 验收。

## 调用即提交与写入回执验证（2026-10-27）

[通用日志接口](../superpowers/specs/2026-10-27-general-logging-api.md)在调用时立即提交，忽略返回回执也会由原日志线程继续处理。`await logger.info(...)` 或稍后 `await receipt` 等待该条文件追加并 flush 的实际结果，不要求每条 fsync。原诊断及日志副本共享同一条写入事实，底层 alog 仍只等待接纳。使用方式见[日志入口](logging-runtime.md#日志异步投递入口与有界队列)，实施进度见[回执计划](../superpowers/plans/2026-10-27-log-write-receipts.md)。

最终受测生产源码为 `64b6625054ad85e40f668f00082dca8a46c2f15a`，环境为 Linux x86_64 开发容器、CPython 3.11.17、SQLite 3.53.1。工作目录为 `/workspaces/camctl`，解释器为 `apps/camctl/.venv311/bin/python`；显式使用本工作区源码路径，清除计时及 Python 环境变量。同一工作区的集成 pytest 逐进程执行。实际 UTC 为 09:51:27—09:54:20，以下各进程均退出 0。

| 命令范围，统一使用 `python -m pytest <范围> -q --tb=short` | 实际结果 |
| --- | --- |
| `apps/camctl/tests/unit` | 9685 项通过、1 项跳过、2 条既有 asyncio 标记警告。 |
| `apps/camctl/tests/integration/logging_runtime` | 189 项通过、1 条既有 asyncio 标记警告。 |
| `apps/camctl/tests/integration/bootstrap/` 中的 `test_logging_composition.py`、`test_one_shot_logging.py`、`test_cli_error_diagnostics.py`、`test_foreground_error_scopes.py`、`test_report_diagnostic_ownership.py` | 22 项通过。 |
| `apps/camctl/tests/integration/outputs/test_read_diagnostics.py` | 14 项通过。 |
| `apps/camctl/tests/integration/persistence/test_snapshot_error_diagnostics.py` | 2 项通过。 |

227 项定向集成覆盖真实队列、线程、文件及诊断消费者，包含 24 项通用 Logger 组合。验证不等待时业务继续、实际 flush 后回执完成、混合级别与旧入口共同排序、满载重要日志补位、取消一个等待者不撤回记录、重复等待不重投、正常退出排空、过滤及未写入的明确结果，以及禁用途中实际写入成功的事实。日志副本覆盖实际追加后复制失败、抑制、未开始及未写入的责任归还。

满载补位与接纳通知共用原通道，未创建逐条后台任务。失败路径回归覆盖按原顺序降级、writer 与 stderr 降级同时失败时全部回执终结、控制异常在收场后继续传播、回执回调相互完成时不死锁、不可格式化的 writer 异常及禁用时低层接纳等待及时结束。源位置、调用时冻结、上下文和媒体过滤保留原覆盖。

最终独立审查核对上述生产源码，并以 8 个公共接口内存探针复核降级顺序、控制异常收场、回执回调锁序、不可格式化异常及原 alog 的禁用接纳；全部探针退出 0，没有未闭合项。文档的 5767 个本地文件链接检查及差异空白检查通过，新增标题锚点另行核对。

完整命令、UTC、来源、环境、退出码与原始日志位于 `.local/verification/log-write-receipts/final/`，分域原始证据归档于同级 `agents/`。本项未执行全部组件集成套件、安装包或真实相机验收，未更新真机验证安装。

## 通用日志入口验证（2026-10-27）

[通用日志接口](../superpowers/specs/2026-10-27-general-logging-api.md)提供 `get_logger` 通用调用及 `get_sync_logger` 的低级别同步调用；[使用示例](logging-runtime.md#日志异步投递入口与有界队列)说明入口和原错误责任的关系。生产命令开始记录及快照维护默认警告已使用通用入口，文件拥有者的真实工作线程沿每次调用复制上下文。

最终组合源码为 `c2ac8ea93718508998120117346d8766044ff7ea`，在 Linux 开发容器、Python 3.11.17、SQLite 3.53.1 下验证。工作目录为 `/workspaces/camctl`，解释器为 `apps/camctl/.venv311/bin/python`；显式使用本工作区的源码与测试路径，清除不应传入子进程的计时和 Python 环境变量。同一工作区的集成 pytest 按目录顺序执行。

| 命令范围，统一使用 `python -m pytest <范围> -q --tb=short` | 实际结果 |
| --- | --- |
| `apps/camctl/tests/unit` | 9658 项通过、1 项跳过、2 条既有 asyncio 标记警告；退出 0。 |
| `apps/camctl/tests/integration/logging_runtime` | 157 项通过、1 条既有 asyncio 标记警告；退出 0。 |
| `apps/camctl/tests/integration/bootstrap/` 中的 `test_logging_composition.py`、`test_one_shot_logging.py`、`test_cli_error_diagnostics.py` | 18 项通过；退出 0。 |
| `apps/camctl/tests/integration/persistence/test_snapshot_error_diagnostics.py` | 2 项通过；退出 0。 |

177 项定向集成覆盖真实有界队列、文件和 stderr、工作线程、CLI 与数据库入口，包含 9 项新增通用 Logger 组合。观测覆盖同一模块 Logger 的并发隔离、调用时冻结及源位置、满载时低级别立即返回、重要日志重复取消时仍只交接一次、独立必要停止继续推进、原异常向外层传播去重、正在交接及日志副本预留／接管的所有权、通道禁用与有序关闭。消息及异常格式化失败仍沿共同快照过滤媒体 stdout，并保留可取得的 stderr 与错误事实。

文件线程缺失上下文、消息格式失败提前转为 repr 导致媒体进入日志、以及已结束目标的多条诊断在首条控制异常后留下等待者，均先用反例复现，再修复并复验。独立审查另以公共消费者内存探针核对实际 JSON、异常状态和原控制异常；已报告问题均已闭合。

完整命令、UTC、源码身份、环境、退出码和日志保存在 `.local/verification/generic-logger/final/`，分域原始记录及红绿证据归档于同级 `agents/`。上述结果不代表全部组件集成回归或真实设备验收；本次没有更新真机验证安装，也没有执行新的相机拍摄。

[设计入口](README.md) · [实现总览](implementation.md)

本页定义 camctl 实现的测试分工和跨职责验证要求。各业务专题中的决策表及验收场景仍须逐项落实。具体阶段与门禁见[第一版实施路线图](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)，接口、测试文件、失败用例和执行步骤见其引用的[模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)及[跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md)。进入代码实施前核对所需前置交付和实际代码。

各带日期的记录保存当时的环境、范围和结果，记录中的失败及尚未完成事项不自动代表当前状态。后续修复与当前遗留项从路线图核对；本次[文档复核发现的实现缺口](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)也按该入口跟踪。部署和设备证据的当前交接范围见[本页末节](#部署交接与待核验项)。

[集成测试耗时与精简评估（2026-10-25）](integration-test-timing-2026-10-25.md)保存本轮逐项计时、目录汇总、失败与跳过，以及保留覆盖前提下的执行分组和准备合并建议。逐项 CSV 与运行元数据一同纳入版本控制。

后续改造由[集成测试耗时优化计划](../superpowers/plans/2026-10-25-integration-test-optimization.md)推进，实际覆盖映射、测量结果及验证边界保存在[优化记录](integration-test-optimization-2026-10-25.md)。

## Action6 会话设置的临时输出处理（2026-10-26）

Action6 将关闭延时摄影熄屏省电和两路屏幕实时画面的设置纳入[相机会话准备](../architecture/camera-parameters.md#相机会话准备)的临时输出处理，普通准备和软件恢复共用同一精确命令规则。调用历史继续保留原始 stdout、stderr 和确认依据；调用错误及其他命令按各自契约处理。

两路实时画面设置的扩展在 Linux 开发容器、Python 3.11.17 下，使用下述相同四个测试文件执行定向单元验证，**130 项通过，1.57 秒，退出 0**。六条会话设置均覆盖返回文字忽略、原输出保留、同会话缓存、新会话重发、真实调用故障和软件恢复；码率及 OSMO 继续核对自己的输出规则。拆分预算中的两条实时画面接受用例在生产修改前失败，省电设置对照用例通过。命令、解释器、源码摘要、日志及退出码保存在 `.local/verification/action6-liveview-output-20261026/`。本项未运行集成测试或真实设备验证。

提交 `aac523d8` 已更新到现有真机验证安装。331 个生产 Python 文件及 27 项资源在提交归档、wheel 和安装副本中逐字节一致，主代理另行核对核心命令文件与当前源码一致；安装后的隔离导入检查确认两路实时画面命令均使用临时输出处理。5375 个现有配置、业务数据、日志、host 和客户端文件的部署前后摘要一致，依赖版本与主机 ADB 路由保留。host 和 camctl 保持停止，客户端继续运行；本次安装未执行数据库或设备命令。完整收据和旧 wheel、状态库、配置备份位于 `.local/deployments/20261026-action6-liveview-output/`。

Linux 开发容器、Python 3.11.17 下，执行 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices/test_action6_session_settings.py apps/camctl/tests/unit/devices/test_session_setup_budget.py apps/camctl/tests/unit/devices/test_osmo360ii_session_settings.py apps/camctl/tests/unit/devices/test_camera_power_session_settings.py -q --tb=short`，**113 项通过，1.45 秒，退出 0**。覆盖普通录像、原生延时、独立准备、软件恢复、原输出保存、会话缓存、调用失败以及 OSMO 的严格输出检查。使用正式驱动与受接口约束的传输替身；更新后的省电设置接受用例在生产修改前失败。命令、解释器、源码摘要、日志及退出码保存在 `.local/verification/action6-lapse-lock-20261026/`。本项未运行集成测试或真实设备验证。

提交 `c4301d4e` 已安装到 `/home/cld/.camctl/manual-validation-20261026`，安装解释器为 Python 3.11.17、SQLite 3.53.1。331 个生产 Python 文件及 27 项权威资源在提交归档、wheel 和实际安装中逐字节一致；包内另一个 Python 文件为资源示例脚本，随资源共同核对。主代理另行核对核心命令文件与当前源码一致，安装后的隔离导入及临时输出处理检查通过，依赖版本保留。5369 个现有配置、业务数据、日志、host 和客户端文件的部署前后摘要一致，主机 ADB 路由保留。安装完成时 host 和 camctl 均已停止；交付记录整理期间观察到 host 启动，保留该运行状态，进程观察另存于部署目录的 `final-processes.json`。旧 wheel、状态库与配置备份及完整收据位于 `.local/deployments/20261026-action6-lapse-lock/`，安装过程未执行数据库或设备命令。

## 会话设备配置的独立预算（2026-10-26）

[实施计划](../superpowers/plans/2026-10-26-session-setup-budget.md)为 ADB 相机的首次会话设备配置单独授予 30 秒，普通设置使用整体预算扣除设备配置部分后的完整控制预算。准备拥有者冻结本次整体与阶段预算；实际阶段分别计时，不能互相借用额度。候选发现、唤醒、准备占用及范围收尾使用同一个实际提前量：默认首次最多提前 40 秒，同会话配置缓存后恢复 10 秒，0 继续关闭提前准备。真正 START、基准读取及软件恢复保持原有各自期限。Action6 的返回文字处理遵守[相机会话准备](../architecture/camera-parameters.md#相机会话准备)。

源码 `d55acfa2` 在 Linux 开发容器、Python 3.11.17 下，拍摄、装配及受影响驱动定向单元 **2599 项通过，44.35 秒，退出 0**。覆盖设备配置满 30 秒后普通设置仍获完整预算、两个阶段各自超限、缓存及新会话、原错误与调用事实、取消、冻结意图和实际请求一致、0 提前量及范围触发。生产装配使用真实驱动与受约束传输替身，验证缓存使提前量由 40 变为 10，重开会话再变为 40；数据库边界使用替身。原日志及收据位于 `.local/verification/session-setup-budget-20261026/`。未运行集成或真实拍摄，未增加操作种类或状态库格式。

提交 `d55acfa2` 已安装到现有真机验证环境，主代理逐字节核对七个生产接缝在源码、wheel 和安装中一致。部署保留主机 ADB、配置及业务数据，host 和 camctl 已停止；收据位于 `.local/deployments/20261026-session-setup-budget/`，实际拍摄由用户继续验证。

## 分段来源的选择等待（2026-10-26）

待选来源批次一次带回来源动作状态、分段策略和录像处理状态。分段来源尚在拍摄或适用处理未结束时，编排直接等待，不逐来源读取完整事实或提交选择事务；来源结束后，下一轮沿原仓储事务复核并固定。已持有的保存与诊断回执继续优先恢复，普通非分段选择沿原路径执行。

Linux 开发容器、Python 3.11.17 下，`test_pending_source_selections.py` **21 项通过，0.99 秒，退出 0**，日志位于隔离工作区 `.local/verification/pending-segment-selection/unit.log`。用例使用数据库和批次发现替身，覆盖两种分段策略的拍摄等待、终态转换、检查／修复／丢弃等待、原保存恢复及错误、普通来源选择和空候选；未运行真实 SQLite 查询组合、集成或设备测试，未重测现场事务数量及耗时。

主分支合并后，取回、装配及拍摄准备相关单元 **1833 项通过，22.50 秒，退出 0**；相机驱动相关定向单元另为 **265 项通过，4.57 秒**，两组存在重叠，不合计为唯一用例数。相机失败结果及逐条计时现在附带实际设备命令和本次剩余调用期限，原错误码、响应和退出事实继续保留。新增两个分区先复现命令缺失，再验证超时和非零退出时的上下文。日志及诊断副本批次查询的只读核对位于 `.local/verification/latest-plan-obtain-wait-20261026/`；未运行集成测试或重新执行拍摄。

提交 `bf145155` 已安装到现有真机验证环境，主代理核对取回编排、分段选择仓储及相机驱动在源码、wheel 和安装中一致。主机 ADB 路由保持，host 和 camctl 已停止；部署收据位于 `.local/deployments/20261026-obtain-source-wait/`。现场新计划的空事务数量和 OSMO 设置超时原因由后续人工验证继续确认。

## 已结束活动的输出范围收尾（2026-10-26）

公共保存入口允许范围末页与原尝试共同保存，仓储继续限制范围责任只能提交范围事实。定向单元 **38 项通过，1.25 秒，退出 0**，Python 3.11.17；新增用例实际经过末页处置、结果持有、可靠保存和仓储分派，覆盖实时结果、已保存末页恢复、无关操作拒绝及保存未知或回滚时原页和原键保留。另覆盖范围已固定但占用未释放时，在后续有效准备期间只继续原释放而不新增读取或尝试。首批五个保存入口分区在修改前有三个失败；最终日志位于 `.local/verification/scope-result-save-20261026/unit.log`。数据库和设备边界使用替身，未运行集成或设备验证。

范围保存及释放接续提交 `d8f83839` 的安装记录核对了两个生产入口及事件登记在源码、wheel 和安装中一致，并确认当时 host 与 camctl 已停止。部署收据位于 `.local/deployments/20261026-scope-result-save/`。该次部署未初始化或迁移状态库，保留已保存的范围末页。

范围责任的事件登记覆盖普通流程的建立、实际配置、结束，首次及后续尝试，以及重试等待的建立、完成和放弃。上述八处种类约束接受 `RESOLVE_OUTPUT_SCOPE=11`，原事件校验继续生效。Linux 开发容器、Python 3.11.17 下，内存登记替身与历史解码定向单元 **179 项通过，0.72 秒，退出 0**；新增单元分别验证创建种类和完整前态种类，不读取真实包资源。日志位于隔离工作区 `.local/verification/scope-event-registration/unit.log`。真实包资源的九项登记回归归入 `tests/integration/history/test_scope_event_registration.py`，本次未运行；这些单元结果不证明构建、安装内容或真机流程。

事件登记修复 `097fce88` 的安装记录逐字节核对权威事件登记、wheel 内资源和实际安装资源一致，并核对上述八处声明均包含种类 11。部署收据见 `.local/deployments/20261026-scope-events/`，单元日志副本及安装核对见 `.local/verification/scope-events-20261026/`。该次部署未运行初始化、迁移或设备命令。

[实施计划](../superpowers/plans/2026-10-26-ended-output-scope.md)将已结束但范围未知的占用接入后续有效拍摄的提前准备。Action6 与 OSMO 的范围读取只保存目录身份与分页结束，不调用文件元数据；保留原结束后等待。原基准差集的全部归属核实后，独立保存范围确定事实并按统一条件释放旧占用。旧拍摄终态、错误、原 RESULTS 次数保持，范围责任独立有限计数。格式 V6 及显式输出范围升级入口保留已有库和历史。

提交 `10492c1a` 验证准备完成后由 `pending` 转为 `running` 的衔接：录像和延时摄影在 START 前再次核对准备时，继续取得原准备凭据；同样允许仍在准备中的 `running` 动作触发旧范围收尾。新增三个分区在修复前失败，修复后 `test_scope_closure.py` 与 `test_preparation.py` 共 **86 项通过，6.13 秒**，Python 3.11.17，退出 0。原日志位于 `.local/verification/report145-start-state-20261026/unit.log`。范围收尾开关在该状态转换用例中真实启用，准备和范围模块共同执行，数据库与设备边界使用替身；未运行集成测试或设备命令。

`10492c1a` 已构建并安装到 `/home/cld/.camctl/manual-validation-20261026`，主代理核对了实际安装源码及当前索引。host 和 camctl 已停止，使用原启动脚本继续人工验证。部署未初始化或迁移状态库，收据位于 `.local/deployments/20261026-start-state/deployment-receipt.json`。

Linux 开发容器、Python 3.11.17 下，持久化、操作及升级定向单元 **617 项通过**；接通后的拍摄、装配及受影响驱动单元 **2299 项通过，35.75 秒**；最后接口与新入口定向 **43 项通过，1.38 秒**。新驱动分区覆盖两台相机的单文件及分段模式、大小未知、停止后等待和目录错误；生产入口分区覆盖独立责任、原失败保持、释放、失败重试及无有效触发时不读取。原始主代理日志位于 `.local/verification/output-scope-20261026/`。本次不运行集成或真实设备验证，由用户在安装后人工验证。

提交 `32743a43` 已安装到现有真机验证环境，停止后原库完整备份并显式升级为 V6，升级命令退出 0，依赖检查通过。主代理另行核对六个生产入口的安装字节及当前索引。配置、能力说明和客户端、host 安装版本保留，未启动实际业务会话或设备命令。收据及原库备份见 `.local/deployments/20261026-output-scope/`。

## 已保存拍摄结果的收尾恢复（2026-10-26）

[修复计划](../superpowers/plans/2026-10-26-results-closed-recovery.md)落实原结果已经保存而后续整理尚未完成的恢复。公共结果入口先恢复原文件子责任，再按原重试要求分派：无需重试时返回经原票据、归属、结果和页时刻校验的 `CLOSED` 输入；需要重试时保留原间隔和累计次数；原在途尝试继续由原拥有者处理。原页发现查询同时取得重试标记，不增加独立查询，有页结果复用原文件恢复已经取得的输入。

Linux 开发容器、Python 3.11.17 下，新增九项定向单元通过，覆盖有无设备端口、有无分页、原文件信息读取错误、正常成功但未全的重试、3 秒间隔边界、在途尝试、原归属及文件登记错误保持。测试使用受接口约束的数据库、仓储和文件登记替身，保留真实结果解码与页校验，不连接真实数据库或设备。最终执行 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/capture apps/camctl/tests/unit/bootstrap -q`，**2222 项通过，35.62 秒，退出 0**；独立审查通过。原始日志保存在 `.local/verification/results-closed-recovery-20261026/`。本次未运行集成测试，真实设备恢复由用户验证。

修复提交 `e32d1996` 已安装到现有真机验证环境。328 个生产 Python 文件、27 项权威资源及 2 个构建辅助文件三方一致，依赖检查通过；主代理另行逐字节核对全部生产 Python 文件和当前索引。状态库、配置、两份能力说明摘要及依赖版本保持，host 与客户端安装版本保留。部署证据见 `.local/deployments/20261026-results-closed/`，部署未启动实际会话或连接原数据库。

## 延时摄影共享结果清单的并发修复（2026-10-26）

[修复计划](../superpowers/plans/2026-10-26-timelapse-scan-concurrency.md)针对共享清单的异步遍历：延时摄影处理器先同步固定扫描条目，再异步查询归属，保持跨设备并发与原页保存责任。相邻原申请恢复循环已经固定条目后再等待；新增回归通过公开 `capture_handler("camera_timelapse")`、受接口约束的替身及明确同步点控制交错。

Linux 开发容器、Python 3.11.17 下，旧代码的四项增删交错均稳定触发 `dictionary changed size during iteration`，两项错误保持用例通过。修复后新增六项通过，覆盖另一设备新增、移除扫描，当前动作有无原页，原申请及操作键只保存一次，异动作原页保持未消费，新增扫描在后续推进可见，以及查询异常、保存错误的原异常链保持。

最终执行 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/capture apps/camctl/tests/unit/bootstrap -q`，**2213 项通过**，耗时 32.79 秒，退出 0。原始失败、通过日志与执行证据保存在 `.local/verification/timelapse-scan-concurrency-20261026/`。本次未运行集成测试或调用设备；OSMO 文件信息读取超时属于独立的现场问题，不计入此项修复结论。

修复提交 `b3d00889` 已安装到 `/home/cld/.camctl/manual-validation-20261026`，Python 3.11.17、SQLite 3.53.1，依赖检查通过。328 个生产 Python 文件、27 项权威资源及 2 个构建辅助文件在源码、发行物和安装副本之间一致；主代理另行核对了全部生产 Python 文件、状态库、配置及两份能力说明。部署记录保存在 `.local/deployments/20261026-capture-scan-snapshot-0639/`。部署保留 host 与客户端的已安装版本，真实拍摄由用户验证。

## 相机首次准备的省电与 USB 模式设置（2026-10-26）

Action6 与 OSMO 360 II 在本会话首次拍摄准备时共同请求关闭延时摄影熄屏省电及选择仅充电；普通录像、原生延时和软件重启后的恢复准备复用同一命令组合。命令值由[相机会话设置](../hardware/camera-control-handoff.md#相机会话设置)维护，缓存与失败语义沿用[相机会话准备](../architecture/camera-parameters.md#相机会话准备)，进度见[实施计划](../superpowers/plans/2026-10-26-camera-idle-settings.md)。

Linux 开发容器、CPython 3.11.17 下执行 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices -q --tb=short`，设备组件单元 **1809 项通过、1 条既有 asyncio 标记警告**，pytest 耗时 69.93 秒，退出 0。新增 14 个用例使用正式驱动与受接口约束的传输替身，覆盖两机型的录像与延时首次准备、匹配 START、同会话复用、新会话重发、两条命令分别失败，以及软件恢复重新设置的成功与失败。新增前 10 个反例在原实现上均失败；独立复核已核对各生产入口及测试的独立期望。

命令、起止时间、输出、退出码和源码身份保存在 `.local/verification/camera-idle-settings-20261026/`。本项未运行集成测试或调用相机；真实设备是否保持亮屏、避免省电及保持 ADB 连接，仍由用户验证。

同日按用户要求更新 `/home/cld/.camctl/manual-validation-20261026` 的 camctl 与 host-demo，安装源码为 `d8c3b614`。323 个生产 Python 文件、27 项权威资源在源码、wheel 和安装副本之间逐项一致；相机设置对应的 camctl 源码与上述受测版本一致。安装解释器为 CPython 3.11.17、SQLite 3.53.1，依赖版本不变。旧会话经 SIGINT 收场，部署未打开数据库连接、初始化或迁移状态库，也未调用相机；配置、状态库和日志等文件状态保持。部署后两进程保持停止，使用原 `start-host-demo.sh` 启动；安装收据与数据保留核对位于 `.local/deployments/20261026-host-stop-camera-idle/`。

## 空业务会话的后台收场与退出（2026-10-26）

[修复计划](../superpowers/plans/2026-10-26-idle-session-exit.md)落实正常退出和报告失败退出的既有契约。主循环先用数据库原责任判断是否进入正常收场，等待并消费本轮已有后台任务后，直接进入原关闭写事务重查数据库与本地责任。收场和关闭之间不新建发现轮次；真实在途任务、未消费失败和取消仍沿原拥有者交付，关闭事务发现新工作时保持接纳并继续驱动。

受测源码 `058121f86558741ce72734be87900d694bafedd8` 使用 Linux x86_64 开发容器、CPython 3.11.17、SQLite 3.53.1、pytest 9.1.1。设置所属源码的 `PYTHONPATH` 后执行 `/workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q --tb=short`，**9191 项通过、1 项跳过、2 条既有 asyncio 标记警告**，退出 0，pytest 耗时 141.11 秒。原始命令、UTC、环境、日志和退出码保存在 `.local/verification/idle-session-exit-20261026/final-unit.json` 与 `final-unit.log`。

修复前，新增有限见证的 5 个分区无法进入正常收场而超时，默认空数据库实际命令也超时。修复后最终会话组合的 7 个分区通过，覆盖成功和报告失败、实际任务完成前不关闭、事实读取期间及收场期间晚到错误、收场期间新动作以及仍有持久化工作的继续驱动。隔离源码上的最终定向集成合计 **27 项通过**，bootstrap 12 项、session 15 项，各进程退出 0；其中包含默认空会话、原日志副本进度同步失败用例、日志装配及真实 SQLite 和内核接纳锁的关闭重查。原卡住用例现在返回 `report_error`，只尝试一次报告，错误日志及正式副本仍保留。完整命令、目标、冻结来源、进程、日志和计时保存在同目录的 `final-integration/`。独立源码复核确认预判不直接授权退出，收场后仍有原写事务的最终重查，未发现阻断问题。

扩大到日志副本完整文件的尝试为 35 项通过、2 项失败，未计作全文件通过。独立修复前基线 `4f83075f` 复现了两处旧测试问题：手工 `ReportContext` 缺少 `database`，后台先抛出 `AttributeError`，测试随后等待事件超时；另一测试在进入 `do_write` 时即增加 `append`，失败打开文件也计一次，目标 `run` 执行前已经与预期零次不符。原测试结果和独立探针分别保存于 `bootstrap-green.log` 与 `baseline-attribution/`，探针的主动取消不计通过。此证据不代表完整组件集成、安装包、真实相机或 ARM 部署验收。

## 全局错误诊断接入的软件验证（2026-10-26）

[实施计划](../superpowers/plans/2026-10-26-global-error-diagnostics.md)覆盖已处理的业务失败、完整异常、第三方 ERROR、早期命令及后台和收场错误。普通取回、自动预览和拍摄内部 READ 在公共结果消费者准备完整诊断；拍摄、媒体、清理、电机、报告和输入受理同样消费原结果。原异常、退出事实和业务身份随原结果交付，同一结果重送共用诊断状态。必要停止及资源关闭先完成，命令再等待日志交接；副本任务实际接管原记录，避免父等待取消后的重复追加。

最终回归基线为 `e77b429cff160ef5621958ee401242c92c19217e`，环境为 Linux x86_64 开发容器、CPython 3.11.17、SQLite 3.53.1、pytest 9.1.1。从所属仓库根设置 `PYTHONPATH="$PWD/apps/camctl/src"`，使用 `/workspaces/camctl/apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q --tb=short`，**9191 项通过、1 项跳过、2 条既有 asyncio 标记警告**，退出 0，pytest 耗时 145.27 秒；实际 UTC 为 01:56:03—01:58:33。新增模块在全新解释器中核对导入资源，14 个读取真实包资源的模块移至集成测试，覆盖保留。

组件集成通过仓库的[隔离并行入口](../../scripts/README.md#隔离并行集成测试)冻结源码，为每个目录建立独立资源和临时根，四路并行。日志运行时与主机文件完整目录，以及受理、装配、取消、拍摄、电机、取回、持久化和报告的新增及受影响文件，合计 **490 项通过**，各分组退出 0；其中最终装配定向 21 项单独完成，不重复计算首轮已通过项。实际组合覆盖真实日志队列和文件、SQLite 原结果、工作线程及报告进程通信；验证满载等待与 stderr 降级、重复取消、原异常图和原始 stderr、派生错误去重、必要停止继续推进，以及原父等待取消后的副本归属。独立源码复核限定命令装配、诊断任务、副本交接和关闭顺序，未发现确定的阻断缺陷。

完整命令、逐文件目标、冻结源码摘要、PID、日志、计时及退出码位于 `.local/verification/global-errors-20261026/final-unit.json`、`final-integration/`、`final-bootstrap/` 和统一 `final-gates.json`。首轮额外装配范围在旧日志副本用例等待期间主动中断，不能计为完整通过；该用例已经生成错误日志及正式副本，主会话仍因空取回、清理轮次的本地责任等待。当前源码的 15 秒对照和原基线 `272fda66` 的 20 秒对照均超时，退出 124：数据库原责任为零且报告已失败，本地责任仍为取回 1、清理 2，每轮继续创建空后台任务。任务栈、实际工作事实及基线收据保存于同一证据目录；空轮次调度的后续修复和通过范围见[空业务会话退出记录](#空业务会话的后台收场与退出2026-10-26)。本次未执行完整组件集成套件，既有 `owned` 参数、异步数据库端口及未等待协程的旧夹具失败仍按各分组对照保留。

本项没有构建或安装新包，没有执行 ARM 部署、真实相机、实际媒体工具或物理故障验证。文件通道可用时错误进入 `camctl.log`；初始化前、文件通道失效及第三方同步 ERROR 满载时交付 stderr，其长期保存依赖调用方。host 的普通日志转发仍有自身容量、截断和故障边界，不能把 stderr 交付解释为完整持久保存。

## 读取错误诊断的完整交付（2026-10-26）

[修复计划](../superpowers/plans/2026-10-26-read-error-diagnostics.md)落实[读取与分段契约](file-runtime.md)：普通取回、自动预览、内部媒体读取及本地主机来源读取，在原 READ 尝试中保存完整异常诊断和已知结束事实。原分段分类、已处理范围、同步和写入范围是否可确认、源结束及关闭错误均继续传递。ADB 保留实际调用事实与原始 stderr 字节；哈苏准备读取保留每步调用、全部原因和收场分类。

读取与关闭双重失败共同交付，未确认停止不能结束原 READ。致命会话结果、多个后台任务收场、运行日志和 CLI 错误出口保留完整异常图；日志等待实际写入凭据，不可用时沿既有 stderr 入口交付。含取消的异常组保持原叶对象并作为控制类错误外传，不继续普通动作；顶层异常类型及 `parent_cancel` 登记不保证与裸取消相同。

最终生产源码 `a5707f119aa7f5c5da1f43c4354ad5af6be81b38` 在 CPython 3.11.17 执行 `python -m pytest apps/camctl/tests/unit -q --tb=short`，**9128 项通过、1 项跳过、2 条既有 asyncio 标记警告**，pytest 耗时 138.32 秒，退出 0。实际 UTC 为 2026-10-25 23:22:45—23:25:05，命令包装耗时 140.14 秒；完整命令、日志及摘要见 `.local/deployments/20261026-read-error-diagnostics/full-unit.json` 和 `full-unit.log`。分项红绿证据及隔离范围保存于 `.local/verification/read-diagnostics-20261026/`。消费者最终直接范围明确拒绝真实线程、子进程和数据库调用；其早期宽域运行曾执行假会话的工作线程，原记录单列该限制，不将它当作最终严格隔离的证据。

协调者核对全部 23 个已审变更文件与作者提交的 Git blob 相同。独立接缝复核及实际值见证覆盖 ADB 原始字节、分段结果、三类 READ 消费者、未知提交原申请、关闭双重失败、会话收场和日志／终端出口，已处理发现均由 `independent-review.json` 汇总。安装包内另外执行诊断值和 JSON 往返核对，没有打开数据库、启动线程或调用设备；该结果不承担真实文件或相机读取验收。

同一受测源码的 wheel 已安装至 `/home/cld/.camctl/manual-validation-20261026/venv`，SHA-256 为 `2874b896d3e3a3b54291fcd706cfb13cfb70d43955e13d2c5214ebce8954994e`。323 个生产 Python 文件、27 项权威资源和 2 个构建辅助文件在源码、wheel 与安装副本间逐项相同；依赖检查通过，实际 Python 3.11.17、SQLite 3.53.1。只连接安装前后完整数据库副本，41 张表的结构和全部数据相同，完整性与外键检查通过；原数据库、WAL、SHM、配置、能力文件及日志摘要相同，输出文件清单、长度和修改时间相同。未初始化、迁移或重建客户端数据。

交付由 `.local/deployments/20261026-read-error-diagnostics/delivery-manifest.json` 汇总，人工环境的 `README.md` 提供启动与诊断说明。camctl 和 host 保持停止，未运行集成测试或连接相机，由用户继续真机验证。原报告 42 的三次失败已丢失的底层信息无法补回，现有历史仍不能区分具体设备原因；本次软件结果验证已取得的诊断能够继续传递，设备读取仍由新一轮人工操作核实。

## 拍摄准备结果交付与调度唤醒（2026-10-26）

[修复计划](../superpowers/plans/2026-10-26-preparation-result-handoff.md)针对人工验证中准备设置已完成、原等待者却无法继续基准读取及 START 的故障。实际执行器的原缺陷对照复现了同一键覆盖结果句柄：保存执行两次，第一等待者一直未完成。执行器现在拒绝尚未交付结果的同键新提交，各原申请拥有者共用原保存及完整状态应用；实际结果未知时仍按原键核实，取消不重复设备设置。受控对照及原日志冻结在 `.local/diagnostics/report21-window-20261025T211633Z/`。

纯单元通过真实准备、调度及录像处理入口，隔离数据库、设备和线程等协作者，核对成功后继续 START、取消后释放以及后续同设备动作取得资格。独立评审补齐内部 READ 业务保存失败恢复、正式取回完成后先清理原读取责任再交付实际错误，以及等待结果期间容器变化的接缝；原错误、实际回执和原业务申请保持不变。

窗口首次观察的同轮次受控对照从 145／414 次观察事务降为各 1 次，窗口末尾仍进入原事务复核。空清理和稳定拍摄等待不再因自身无进展完成而即时唤醒；其他动作的待保存责任不再使当前录像重复通知。全局 READ 消费或替换原申请、真正安排新的续读时仍通知，同一续读资格仅重建发生时间不算进展。非空清理、同动作尚未细分的待保存等待及仍返回空结果的复杂处理器保留原保守通知，外部新计划的周期检查也保持原合同；本项不宣称所有周期读取或即时循环均已消除。

分项红绿日志、命令、源码身份和退出码保存在 `.local/verification/prepare-handoff-20261026/`。其中 `read-save/final-followup-receipt.json` 区分早期广域回归与最终 75 项 READ 直接矩阵，`window/pending-scope-receipt.json` 记录最终 176 项相关单元通过和两个旧实现失败对照。部署与最终完整单元记录由 `.local/deployments/20261026-preparation-handoff/` 汇总。本项遵照用户要求不运行集成测试，不启动 camctl 或 host，不连接相机；设备实际响应及启动时延由用户真机验证。

最终源码 `ccf050c6584c308b4fb816e806efb7042c4f76e8` 在独立冻结工作树中使用 CPython 3.11.17 执行 `python -m pytest apps/camctl/tests/unit -q --tb=short`，**8990 项通过、1 项跳过、2 条既有 asyncio 标记警告**，pytest 耗时 150.55 秒，退出 0。实际 UTC 为 2026-10-25 22:25:49—22:28:21，命令包装耗时 152.65 秒；完整命令和日志摘要见交付目录的 `full-unit-final.json` 与 `full-unit-final.log`。独立 READ 复评另通过原正式入口的四类见证及 75 项直接单元，最终唤醒尾项独立通过 38 项；范围与结论记录于 `independent-review.json`，未解决发现为空。

同一冻结源码构建的 wheel 已更新至 `/home/cld/.camctl/manual-validation-20261026/venv`，SHA-256 为 `adf3271ff0558947c8b03c73efe27de182816cc8e3c4bba9fe67f1027592c3ba`。322 个生产 Python 文件、27 项权威资源和 2 个构建辅助文件在源码、wheel 与安装副本间逐项相同；依赖检查通过，实际解释器为 Python 3.11.17、SQLite 3.53.1。安装版实际执行器配合内存协作者的对照确认同键重复申请被拒绝、原等待者可靠完成、交付后复用键可正常撤回；该对照不创建线程或数据库。

保留核对比较停止后的备份与安装后数据库完整副本，41 张表的结构和全部数据相同，完整性与外键检查通过；原数据库、WAL、SHM、配置、能力文件及日志摘要均未变化，输出文件清单、长度及修改时间相同。未打开原数据库，未初始化、迁移或修改过期动作；客户端数据未重建。安装及核对的命令、日志与逐文件摘要由 `delivery-manifest.json` 汇总。人工环境的 `README.md` 提供启动命令，camctl 与 host 保持停止，由用户提交新计划验证。

## 状态目录与远端异步数据库集成（2026-10-26）

客户端修改与状态目录实现合入本地 `implement_camctl`，远端基线同步至 `8db533570eb73919b19aed152842e017becb0f55`。异步报告发布继续为每次写入取得实际时间，产物选择继续应用已取回文件的排除集合。状态目录的结构、派生、读取及投影契约见[历史目录](database/history.md)、[报告字段](../architecture/report-format.md)与[实施计划](../superpowers/plans/2026-10-26-state-directory-integration.md)。

最终单元验证在 WSL Ubuntu 24.04、CPython 3.11.15 中执行。从仓库根运行 `/home/cld/.cache/camctl-state-directory-python311/bin/python -m pytest apps/camctl/tests/unit -q --tb=short`，8681 项通过、1 项跳过，退出 0，耗时 126.81 秒。跳过项只验证非 Linux 平台的父进程守护无操作行为；两项警告来自同步测试上的 asyncio 标记。会话为 `27648`，完整输出保存在 `tmp/state-directory-wsl-unit.log`。此解释器链接 SQLite 3.50.4，单元用例隔离数据库操作；本次结果不承担真实 SQLite 运行条件验证。

状态目录的真实数据库证据来自 Windows、CPython 3.11.12、SQLite 3.54.1：有限与有界事务的提交、回滚文件共 7 项通过；合入远端 `1924faf0` 后，状态时间专项 6 项通过，覆盖完整转换、非单调时钟、冻结字节重建、真实本地完成时间、稀疏索引访问及损坏目录拒绝。结构检查通过 3966 项断言，客户端报告相关 4 个测试文件共 560 项通过。日志分别为 `tmp/state-directory-history-focused.log`、`tmp/state-directory-latest-status.log`、`tmp/state-directory-database.log` 与 `tmp/state-directory-client.log`。

Windows 的全量单元初次复验为 8599 项通过、30 项失败、2 项 setup 错误，其中本地时间注入测试已按异步接口修复，并进入最终 Linux 全量通过范围。其他失败涉及 POSIX 路径假设，两个 setup 错误涉及超长参数产生的 Windows 环境变量限制，原日志为 `tmp/state-directory-latest-unit.log`。远端 `1924faf0` 的报告集成尝试为 151 项通过、291 项失败、10 项错误，已确认的失败入口包括旧夹具向 `CaptureRuntime` 传递 `owned`、向异步受理传递 `OwnedConnection`；日志为 `tmp/state-directory-latest-reporting.log`。后续验证范围限定为单元测试，完整集成测试另行处理；未把此前专项通过或最终单元通过解释为完整集成验收。

## 计划创建时间的毫秒精度（2026-10-26）

Windows、CPython 3.11.12 中，时间值类型的 90 项单元测试通过，覆盖精确整数毫秒换算、起点两侧、零毫秒、旧秒级时间和非法精度。真实受理、数据库写入及公共投影的新增用例先因 `plans.created_at` 整秒约束失败；按契约放宽到整毫秒后，`integration/acceptance/test_acceptance.py` 的 52 项通过，数据库实际保存 `1736899200047000` 并投影为 `2025-01-15 00:00:00.047`。

受理测试使用独立的 `created-at-python311-sqlite` 环境，链接 SQLite 3.54.1；原环境链接 3.47.1，不满足仓库运行条件，其受理结果不计作业务验证。`integration/contracts` 的 442 项通过，模块依赖检查另有一项失败；独立读取基线 `142b7925` 的源码导入图与当前源码，所得九条违规边完全相同。受理目录完整运行另有五项未通过，涉及事务替身的 `total_changes`、Windows 输出捕获与设备描述；本项只据已通过的受理文件和时间投影用例确认新增行为，没有将目录结果计作全绿。

数据库结构检查通过 3925 项断言，公共协议、报告依赖登记与文档链接检查通过。日志位于 `tmp/created-at-ms-python-unit.log`、`tmp/created-at-ms-python-contracts.log`、`tmp/created-at-ms-python-acceptance-sqlite.log`、`tmp/created-at-ms-python-acceptance-focused.log`、`tmp/created-at-ms-database.log` 与 `tmp/created-at-ms-baseline-imports.log`。本项直接更新当前开发结构；未连接真实设备，未执行完整 Python 测试套件。

## 共享数据库事实读取与正常运行快照维护的验证（2026-10-26）

[本次计划](../superpowers/plans/2026-10-26-read-sharing-snapshot-runtime.md)完成剩余实际输入共享，并将正常运行的业务操作及快照维护接入同一个数据库执行器。连接在所属线程创建、使用和关闭，有限事实跨线程交付；设备、文件和运行时状态继续由所属协程管理。启动发现遗留候选，可靠提交及外部对象发现唤醒维护，候选为空时等待通知。业务优先于尚未开始的维护，已经执行的维护不能被抢占；快照积压不延长会话。正常报告的轻量变化探测仍有周期读取。

最终受测并安装的生产源码为 `8db533570eb73919b19aed152842e017becb0f55`，CPython 3.11.17、SQLite 3.53.1。完整单元命令为 `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，8642 项通过、1 项跳过、2 条既有 asyncio 标记警告，pytest 耗时 117.55 秒，退出 0。日志和起止记录位于 `.local/verification/read-sharing-snapshot-runtime-20261026/final-unit-001/`，执行会话为 7987；323 份源目录 Python 文件通过 3.11 AST 检查，含生成资源模块。具名独立评审覆盖本轮 76 个生产文件的实际变更，`final-independent-review.json` 无未解决发现。

异步接线的单元同时覆盖等待期间资格变化、原尝试已结束、文件任务停止、实际提交后的事件循环交接、回滚及未知结果的原键保留。真实 `SavedResultEntries` 和 `RegisteredSourceFiles` 接口分别约束分页结论和原片选择；STOP 使用固定 START 锚点，在真实返回后先持有原结果再等待保存。固定文件输入的普通整数页编号另由下节类型及编码反例约束。本轮按用户要求未运行集成测试、CLI 或真实设备。

### 相同真实前态的完成保存与原键核实

使用先前保存的 H893 前态、原动作 21／集合 4／活动 6／RESULTS run28、原请求键与发生时间，批次仍为 128。全部诊断只连接新副本；原人工环境仅比较元信息与停止状态。导入、准备和映像核对均在目标计量之外。

| 同口径指标 | 前版完整保存 | 本轮完整保存 |
| --- | ---: | ---: |
| SQL 总 execute | 327 | 216 |
| SELECT | 301 | 190 |
| INSERT／UPDATE | 20／4 | 20／4 |
| BEGIN／COMMIT | 1／1 | 1／1 |
| 读取行，包含重复 | 286 | 175 |
| 成功写入行 | 24 | 24 |
| 事件／业务行变更 | 4／5 | 4／5 |

本轮确实提交事务 609、事件 894—897。四事件全部列及历史正文原字节一致，34 张业务表的写前 406 行、写后 409 行均与独立历史映像相同，外键和完整性通过。190 次 SELECT 按阶段为规划 61、验证上下文 36、事件校验 51、事件生成 23、读取初态 7、写入阶段内读取 5、最终检查 4、未归类 3。原键只读核实另计 97 次 execute，其中 SELECT 为 95 次、BEGIN 与 ROLLBACK 各一次，写入为零，返回原结果且副本不变。收据为 `.local/verification/transaction-facts-20261026/shared-runtime-final-{finish,verify}-001/receipt.json`。

实际共同端口另用一个独立副本验证，经过真实 `BusinessDatabase.write` 和 `SnapshotMaintenanceOwner.observe_commit`。仓储仍为 216 次 SQL／190 次 SELECT，提交后的维护资格额外执行 1 次 WITH 查询。连接创建、仓储回调、提交观察和实际关闭均在同一数据库 worker，业务上下文跨线程保持，关闭后线程结束；事件、业务映像与原环境观察仍相同。该样本未启动候选扫描或维护批，阈值 64、批次 16；不能用单次无竞争任务证明竞争调度时延。证据为 `.local/verification/read-sharing-snapshot-runtime-20261026/shared-port-final-001/`。

资源观察在另一独立副本执行，与轻量计时分开。190 次 SELECT 包含 90 种实际 SQL 与绑定组合、100 次额外重复；没有摘要容量溢出，不把 90 解释为必要查询次数的下限。事实缓存查询 292 次，命中 233 次、未命中 59 次，峰值 23 条、估算 127122 字节，范围结束后释放；该组件解码事件 4 次。Python 分配峰值为 26848177 字节，包含诊断开销。缓存预算、实际分配和所测小样本各自解释，容量与写后失效由单元另行约束。证据为 `shared-runtime-final-memory-001/`。以上耗时为各一次开发环境观察，不外推稳定提速或真机耗时；阈值比较见下节。

### 报告字节与安装交付

报告 43、44 按原对象批次 32、事件批次 256 重建，分别为 2221、2878 字节，长度、登记摘要及保存黄金文件逐字节比较均一致，临时输出清理及输入副本不变核对通过。证据为 `shared-runtime-final-reports-001/receipt.json`。

发行物已安装至 `/home/cld/.camctl/manual-validation-20261025`，wheel SHA-256 为 `d8f2bf9f576e6525fce466ccf89ac03837f5f34a6500af43f89cd2aa4d1629fb`。322 个生产 Python 文件、27 项权威资源和 2 个构建辅助文件与受测源码、wheel 及实际安装逐项一致，依赖检查通过。原数据库仅复制后连接审计，41 张表、Schema、V5 元信息、实例身份及持久日志模式一致；原数据库、WAL／SHM、配置、日志和能力文件未改变，camctl 保持停止。发行审计由 `.local/deployments/20261026-shared-db-snapshots/delivery-manifest.json` 汇总，软件与真实设备验证分别交付；真实设备由用户后续手测。通用有限行历史恢复的正反向择路仍保留在原历史性能计划的未完验收项中。

## 固定文件输入的页编号类型验证（2026-10-26）

固定文件输入在校验完成页编号后交付普通 `int`，可直接进入有限保存模型的 `document` 编码。`test_saved_file_inputs.py` 分别覆盖 Action6 和 OSMO 的完成文件，检查真实返回字段的精确类型，并将完整返回模型交给生产编码器；未知完成状态的回退页编号也经过同一编码检查。新增用例在修复前两项失败，修复后连同固定成员消费和收尾事实共 104 项单元通过，Python 3.11.17，退出 0，耗时 1.25 秒。

本次单元隔离数据库与设备协作者，没有运行集成测试、CLI 或设备。命令、红绿日志保存在 `.local/verification/read-sharing-snapshot-runtime-20261026/completion-page-type-{red,green}.log`。既有保存副本的收尾重放从成员已固定的状态开始，不经过此次输入形成边界，其结果不能替代本项类型及编码验证。

## 快照阈值与维护等待的副本测量（2026-10-26）

本项冻结维护器源码 `bd368dc6a73812846b1741b897c3f8bb4eaa38ca`，使用保存的真实数据库副本 C931、C893，分别在三个独立副本上运行阈值 16、32、64。维护经过真实 `BusinessDatabase`、共同数据库执行器和 `SqliteSnapshotStore`，逐快照与独立 initial 正向回放比较；34 张业务表及原历史文本保持相同。本项没有启动正常 camctl、CLI、相机或集成测试，最终生产接线另行组合验收。

| 当前完整边界 C | 阈值 N | 保存快照数 | 编码字节数 | 总维护经过时间 |
| --- | ---: | ---: | ---: | ---: |
| 931 | 16 | 7 | 97896 | 422.80 ms |
| 931 | 32 | 4 | 87085 | 410.78 ms |
| 931 | 64 | 0 | 0 | 0.39 ms |
| 893 | 16 | 7 | 97897 | 425.58 ms |
| 893 | 32 | 4 | 87086 | 404.54 ms |
| 893 | 64 | 0 | 0 | 0.43 ms |

有候选样本的主要成本是加载恢复，约 384～400 ms；JSONL 编码约 14～19 ms，保存约 4～6 ms，生产格式不压缩。这里新建的 S893、S931 都晚于报告需要的 H862，因此不能用于该旧状态；动作 21 仍分别逆向读取 18、20 条事件。

另从 C931 撤销至完整事务边界 S861，并以独立初始正向回放核对 155 个对象、34 表共 394 行。动作 21 此时累计变化 18 次，只达到 N16。将真实准备的 5975 字节 S861 快照延后保存至独立 C931 后，H862 的相同 8 行由逆向 20 条事件改为正向 1 条事件，单次恢复为 109.04→55.14 ms。加载、编码、保存该快照分别为 5.82、0.74、4.95 ms；WAL 增长 24752 字节，关闭后数据库增长 4096 字节。这是经独立核对的受控时序，不能据此声称原运行在 S861 实际生成过快照。

独立排队样本在真实分段依据恢复操作开始时提交一条业务读，该维护操作执行 447.516 ms，业务读等待 447.317 ms、执行 0.457 ms。业务优先只选择下一项尚未开始的操作，无法抢占已开始的维护；本项没有人为延迟。以上均为单次开发环境观察，不外推稳定提速或真机耗时。

默认阈值保持 64。较小阈值能在较早历史位置留下快照，但本例也显示维护加载和业务等待的实际成本；当前证据不足以认定所有部署使用 16 更合适。阈值调整应结合报告截止位置、实际命中率及业务排队测量。汇总与各次命令、日志、退出码和逐操作时间戳由 `.local/verification/transaction-facts-20261026/threshold-summary-bd368dc6-001.json` 索引。

## 开发容器重建后的运行环境

环境恢复、测试提速与统一交付由[接续计划](../superpowers/plans/2026-10-25-environment-test-speed-delivery.md)跟踪。恢复后的开发解释器位于 `.local/environments/dev-python311-20261025/`，`apps/camctl/.venv311` 指向该目录。旧失效环境保存在 `.local/archives/environment-20261025/venv311`。CPython 3.11.17 实际链接 SQLite 3.53.1；`uv sync --project apps/camctl --python 3.11 --group test --frozen` 使用显式的开发环境目录，依赖检查通过。运行库检查验证 STRICT、JSON、增量 BLOB 及临时文件库 WAL/FULL，退出 0。

客户端使用 Node.js 24.22.1、npm 11.21.1，`npm ci --engine-strict` 按原锁文件安装 157 个依赖，完整依赖树检查通过；配套 Chromium 及系统依赖已经安装。容器另准备 ADB 和 ffmpeg／ffprobe，只检查工具版本，没有连接设备。环境路径、版本及锁文件摘要保存在 `.local/verification/environment-recovery-20261025/environment.json`。

新环境首次完整 Python 单元测试为 7137 项通过、1 项跳过，77.56 秒，退出 0；保留两条既有同步测试的 asyncio 标记警告。客户端类型检查及生产构建退出 0，保留既有主包体积提示。命令、工作目录、实际 UTC、源码身份、日志和退出码保存在同目录的 `full-unit-baseline` 与 `client-build` 记录中；这些结果属于开发环境，安装交付和设备复测分别验收。

### 测试准备与等待矩阵提速

capture 原等待守卫的两个矩阵共 78 个参数实例，分别为 8.72 秒和 3.74 秒。当前矩阵使用内存行事实、真实包资源、事件构造和实际具名守卫，共 74 项；重复的合法样例合并，失败但效果已确认及原发送观察保留的分区继续独立覆盖。矩阵仍归入集成测试，真实事务、等待运行时和回滚协作由原集成用例验证。最终矩阵 74 项为 0.38 秒，合并树复核为 0.37 秒，均退出 0。两项结果输入校验由既有 4 项单元参数实例覆盖，不再逐例建立集成数据库。旧矩阵分两次启动，新矩阵一次启动；上述数字只描述这些目标，不外推整个目录的净提速。

完整 capture 目录为 2127 项通过、1 项失败，712.90 秒，退出 1。该轮早于最终失败样例的单行效果事实修正；修正后完整 74 项矩阵已重新验证。唯一目录失败为 `test_action6_cold_recovery_decodes_saved_budget_and_continues_same_copy`，在读取偏移断言得到 `[0, 0]`，预期为 `[0]`；当前树单例及干净 `fdcdc92` 基线均在同一位置复现，属于已记录的既有失败。完整输出、基线对照、最终补丁及命令回执位于 `.local/verification/capture-speed-20261025/`。

outputs 的六个慢目标在当前环境共 336.659 秒。剖析显示主要时间用于测试准备阶段的真实分段拍摄及其历史核验，选择消费本身耗时很小。当前默认、预览和显式 ID 使用同一真实来源创建三个独立取回；组范围与计划范围创建两个独立消费者，分别经过等待来源、来源失败和等待准备；清理及冷恢复继续使用独立完整来源。六次重复拍摄准备减少为三次，原固定项、产物数量、操作键幂等和每个消费者的失败及等待断言保留。三个对应测试通过，记录器耗时为 164.972 秒。完整 outputs 目录为 2516 项通过、2 项跳过，560.87 秒，退出 0；减少的三个 pytest 节点对应三个选择模式及两个范围模式在同一来源下的独立业务场景，实际断言保持。两轮同期负载不同，只据此记录本次目标的实际耗时，不把差值推广为完整目录或目标硬件结论。最终合并树分别运行等待矩阵及三个取回场景，均退出 0。证据位于 `.local/verification/outputs-speed-20261025/`。

### 独立安装与手工验证入口

统一交付位于 `.local/deployments/20261025-environment-refresh/`。wheel 的 305 个生产 Python 文件、27 份权威资源与合并树和独立安装版逐字节一致；运行依赖由原锁文件导出并带哈希安装，依赖检查通过。发行摘要、最终源码核对、数据目录及证据入口集中在该目录的 `delivery-manifest.json`。客户端 ZIP 的 53 份文件经清单、CRC、源树和独立安装目录核对，真实包 HTTP 检查通过；host 安装版的报告领取及失败诊断结果见[组件记录](../host-demo/verification.md)。公共协议与完整数据库结构检查均退出 0。

新的主机环境位于 `/home/cld/.camctl/manual-validation-20261025`，主机库没有计划、动作、产物或报告；客户端独立数据目录 `client-data/` 已显式初始化，覆盖水位为 0，草稿、请求、预设、报告和导入均为空。旧恢复数据库与审计文件保留，交付核对再次验证已归档文件摘要。旧计划的软件收场继续以报告 24、25 及原审计为证据，不产生新的拍摄结果。

在两个开发容器终端中分别运行：

```sh
/home/cld/.camctl/manual-validation-20261025/start-host-demo.sh
```

```sh
/home/cld/.camctl/manual-validation-20261025/start-client.sh
```

客户端默认访问 `http://localhost:4310`。主机入口读取同目录 `config.toml`；`environment.sh` 保存原手工部署使用的 Windows ADB 服务默认连接，调用方的环境变量可以覆盖它。host 交互终端使用计划文件的容器绝对路径执行 `submit`，使用 `claim` 领取完整文件，具体步骤保存在交付目录的 README 中。Windows 客户端使用该目录 `client/` 下的 ZIP，按包内说明安装并运行 `start.ps1`；当前运行检查在 Linux x86_64 完成。真实相机在线状态、重新拍摄、Windows 运行与目标 ARM64 验收分别保留证据边界。

## 事务事实读取与普通完成消费的验证（2026-10-26）

[本次计划](../superpowers/plans/2026-10-26-transaction-facts-refactor.md)已接通真实 `finish_segment_recording`。事实首次形成时，命令及独立守卫核验来源；普通完成消费已保存的文件、成员及 assessment／conclusion，并独立检查本次终态、产物、关联和共同发布。事务内核固定开始时的完整提交边界 C0；固定事件、事务及文件共用 128 条、2 MiB 的保留预算，当前行和集合查询直接读取。有限入口消费内核已验证的内存前序，有界入口消费同连接已经写入的前序；当前提案不因此获得事实资格。候选批次同时交付固定文件，范围批次同时交付文件身份。

### 完整保存与原键核实

受测生产源码为 `7f633a15d4f7a753305b3246c79935c09c770bed`，CPython 3.11.17、SQLite 3.53.1。使用[此前准备的同一真实前态](#真实历史副本的完整录像保存重放2026-10-25)，保持动作 21、集合 4、活动 6、RESULTS run28、原申请、操作键、发生时间和批次 128；只复制已有保存前态，不启动相机或创建新的拍摄业务。下面两列都是完整写入口，准备、导入、映像及完整性检查均不计入目标窗口。

| 同口径指标 | 上轮完整保存 | 本轮完整保存 |
| --- | ---: | ---: |
| SQL 总 execute | 1881 | **327** |
| SELECT | 1855 | **301** |
| INSERT／UPDATE | 20／4 | 20／4 |
| BEGIN／COMMIT | 1／1 | 1／1 |
| 读取行，包含重复 | 1800 | 286 |
| 成功写入行 | 24 | 24 |
| 事件／业务行变更 | 4／5 | 4／5 |
| 生产事务经过时间 | 935.540 毫秒 | 100.716 毫秒 |
| 完整入口外层经过时间 | 1.220938 秒 | 0.233656 秒 |

本轮实际提交事务 609、事件 894—897，原键此前不存在。原片 device_file30 保存为 output29，预览 device_file29 保存为 output30，并生成 origin15；最后发布集合 4。四条历史记录的全部 SQL 字段及 `body_json` 原文字节与原记录一致；34 张业务表在保存前的 406 行、保存后的 409 行均与独立历史映像逐字节相同。返回边界、外键与完整性、事务收场均通过。

保存后的 `verify_segment_finish` 另行计量：**127 次 execute＝125 次 SELECT＋BEGIN、ROLLBACK 各一次**，读取 130 行，写入 0 行，外层 0.157733 秒；返回原结果，副本内容不变。该口径不与上表完整写操作混算。主计时及只读核实收据分别为 `.local/verification/transaction-facts-20261026/final-source-001/`、`final-source-verify-001/`。

### 剩余读取与容量观察

完整保存的 301 次 SELECT 按生产阶段分别为：规划 82、验证上下文 57、事件校验 111、事件生成 32、读取初态 7、写入阶段内读取 5、最终检查 4、未归类 3。独立资源样本按 SQLite trace 的实际展开 SQL 原字符串计算 SHA-256，包含实际绑定且保留原空白；只保留最多 512 个摘要及次数，不保存参数正文。本例 SELECT 的 93 个不同摘要共调用 301 次，即有 208 次额外重复，容量没有溢出。

重复较多的查询读取实际当前集合、动作及运行身份，或文件页、配对端点、尝试及来源的直接关系。完整成员、缺口、产物及 origin 的覆盖检查仍查询实际集合。相同 SQL 和绑定并不说明期间的当前值不变；本次未逐条证明这 208 次重复全部必需，也不把 301 次解释为下限。普通完成已不重新执行旧基准、START／WAIT 和文件事实的创建分类。

资源观察单独使用新副本，排除在主计时外。事实组件共查询保留项 181 次，命中 169 次、未命中 12 次、淘汰 0 次；该组件入口解码历史事件 4 次，生产计量的全部历史解码为 7 次。保留峰值为 12 条、估算 57385 字节，事务结束后范围释放。Python 分配峰值为 27392964 字节、调用后为 4481565 字节，包含诊断计数的开销；保留预算估算不等于实际分配字节。小样本不能单独证明随业务规模增长时的内存上界，容量、超大项不保留、淘汰及生命周期由单元反例另行约束。详细用途和实际绑定重复记录位于 `final-source-memory-002/`，此前独立资源记录 `final-source-memory-001/` 保留。

### 回归与人工验证交付

同一保存库上的报告 43、44 按原对象批次 32、事件批次 256 重建，分别为 2221、2878 字节；长度、登记摘要及与保存黄金文件的逐块字节比较全部一致，临时文件清理和输入副本不变检查通过。报告收据位于 `final-source-reports-001/`。

完整单元使用部署版本 Python：`PYTHONPATH=apps/camctl/src:apps/camctl/tests PYTHONDONTWRITEBYTECODE=1 apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，**8242 项通过、1 项跳过、2 条既有 asyncio 标记警告，110.29 秒，退出 0**。收据及完整日志为同目录 `final-unit-001/receipt.json`、`unit.log`。新增单元覆盖 C0、跨连接隔离、当前写后可见、有限及有界已验证前序、直接关系矛盾、失败传播、容量及成功／回滚／未知／取消释放。41 个修改 Python 文件通过 Python 3.11 AST 检查；独立源码审查无未解决缺陷，收据为 `final-review-source.json`。有限入口的真实守卫链代表单元使用空候选；带原片及预览的完整提交由有界入口的真实副本样本覆盖。本次未运行组件或跨组件集成测试，也未执行真机拍摄。

人工环境 `/home/cld/.camctl/manual-validation-20261025` 已安装上述受测生产源码对应的 wheel，SHA-256 为 `9a965b9ecf6c6e62de82b1b7152cf37d72f246c6fe19649a154bec3611f4e7a2`。319 个生产 Python 文件、27 份权威资源和 2 个构建辅助文件与源码、wheel、实际安装逐项一致，依赖检查通过。安装前后全部 41 张表、V5 元信息及实例身份相同；原数据库、WAL/SHM、配置、日志和设备能力文件的摘要、长度与修改时间未变，camctl 继续停止。发行及实际安装审计由 `.local/deployments/20261026-transaction-facts/delivery-manifest.json` 汇总；人工入口 README 已指向本次发行说明，另存 `readme-handoff.json`。真实设备验证交由用户进行。

全部副本计量与证据入口集中在 `.local/verification/transaction-facts-20261026/final-summary-001.json`。副本诊断期间只核对原人工环境文件元信息与停止状态，没有打开原数据库、配置或日志正文。安装审计的读取范围以交付收据为准。上述耗时各为一次轻量观测；副本物理布局、开发机和现场负载不同，不推断稳定提速比例或目标硬件耗时。历史性能计划中通用有限恢复择路、正常 run 的共享线程装配及快照维护等未完项继续由原计划跟踪，本次不扩大完成声明。

## 真实历史副本的完整录像保存重放（2026-10-25）

人工环境计划 6、动作 21 的原 `finish_segment_recording` 完整事务已在独立副本上重新执行。使用当前人工 venv 中已安装、与源码 `bbdae16721bfa9c97588f231023ae52146c8f2b5` 一致的包，Python 3.11.17、SQLite 3.53.1。原操作键为 `d93c62d4778b4195342bfbcc15e35d5d`，原申请、业务时刻、集合、活动、操作流程及批次 128 全部保留；本次实际提交事务 609、事件 894—897，没有走已保存操作的只读核实分支。

原人工库保持停止。先复制原库及 WAL/SHM，再仅对副本连接 SQLite；全部 41 张表与此前保存库的逻辑映像相同，快照 SHA-256 仍为 `0fa9966885baec817d55de9fb70bd90f406c29c0ae3cc9aa3086ab7fa0971ef7`。副本按原事件的 before/after 逐项核对后，逆序撤销原保存及其后续事件，共 38 条事件、41 项行变更，保留事务 608／事件 893 及之前的原历史，恢复派生元数据和目录。独立从原只读历史恢复 H＝893、897 各 165 个实体，核对全部 34 张业务表：写前 406 行、写后 409 行均与独立恢复结果逐字节一致。前态重建、连接初始化和结果验收均在目标计量之外。

| 同口径指标 | 原现场保存 | 当前副本完整保存 |
| --- | ---: | ---: |
| SQL `execute_calls` | 124176 | **1881** |
| 实际交回的行，含重复读取 | 129875 | 1800 |
| 成功写入的行，按正 `rowcount` 累加 | 24 | 24 |
| 事件／业务行变更 | 4／5 | 4／5 |
| `db_transaction` 经过时间 | 15106.814 毫秒 | 935.540 毫秒 |
| 提交阶段 | 2.518 毫秒 | 4.296 毫秒 |

本次 `executemany_calls` 为 0。SQL trace 在目标调用前启用、返回后立即关闭，观察到 **1855 次 SELECT、20 次 INSERT、4 次 UPDATE、BEGIN 和 COMMIT 各 1 次**，合计 1881 次，与生产 API 计数相同；没有 DELETE 或 ROLLBACK。完整入口外层耗时为 1.220938 秒，包含装饰事务之外的入口工作，不能与表中的事务时间混用。规划、验证上下文、事件生成、事件校验的 SQL 分别从 30262、17078、17053、59741 次降至 292、373、576、598 次；`sql_writes` 阶段仍为 29 次，包含该阶段内的读取。

返回结果及边界与原结果一致，真实生成的四条历史记录全部 SQL 字段及 `body_json` 字节与原保存相同；提交后的独立原键核实也通过。库完整性、外键、连接收场、前态文件未变以及原人工库、WAL/SHM、配置、日志、能力文件未变均通过，camctl 保持停止。命令于 UTC 2026-10-25T15:24:01.758743 至 15:24:03.864744 执行，退出 0；完整来源、重建过程、原测量提取、生产计量、SQL 分类、映像和独立审查保存在 `.local/verification/finish-write-replay-20261025/`。原计量来自 `.local/verification/recording-window-20261025/plan-6-action-19.json` 中原日志第 1094 行的保存提取。

该完整保存样本的 SQL 调用较原记录减少约 98.49%。这是原真实业务输入在副本上的单次重放；重建会改变副本物理页布局，本次没有原现场的相机、调度及并发负载，且 trace 带来观察开销，因此不据耗时外推稳定提速比例。本次未修改生产代码、启动 camctl、调用设备或运行集成测试。该结果与此前 38520→360 次的只读 `verify_segment_finish` 分开记录。

## 报告公开分支与快照直接读取的验证（2026-10-25）

源码 `bbdae16721bfa9c97588f231023ae52146c8f2b5` 已接通按实际公开内容请求增长关系的报告读取。动作先恢复有限事实；实际分段错误按错误头的来源取得完整成员、缺口及范围，覆盖结果只取得默认范围及其引用成员。取回失败可以触发来源动作的展开，来源自身错误为空不影响请求成立。已证明没有正式集合的合法错误仍输出契约要求的空数组。未读取的关系继续拒绝访问，通用完整恢复和公开变化依赖保持原范围。

已提交快照直接提供 S 状态，读取职责见[快照读取、构建与验收](historical-state-query.md#快照读取构建与验收的责任)。完整及报告快照路径均不复制当前业务候选；精确命中直接读取快照，正向仅处理 `(S,H]`，从初始恢复仅处理 `(0,H]`。通用对象快照入口同样按选路取得状态，创建行使用原事件业务值及统一派生规则。快照中的旧值和 `null` 保持原样，行结构、归属、主行、重复身份、格式和固定元信息继续核验。

真实保存库仍为 SHA-256 `0fa9966885baec817d55de9fb70bd90f406c29c0ae3cc9aa3086ab7fa0971ef7`。原库没有快照；专项在独立副本中由生产恢复、准备和保存端口生成 S＝861／862 的派生快照，不新增业务事件。下表比较范围裁剪阶段 `3a4dec78` 与最终源码，每分区、每入口各一次；快照准备不计入目标恢复。

| 分区 | 完整／报告入口的对象事件数，调整前→后 | 最终源库全部 SQL，完整／报告 | 最终正文读取 |
| --- | --- | --- | --- |
| S＝H＝862 | 20→0 | 63／48 | 0 条正文查询，0 次解码。 |
| S＝861，H＝862，正向较短 | 21→1 | 89／55 | 仅 `(861,862]`，事件 862 读取、解码、应用各一次，共 313 字节正文。 |
| S＝862，H＝893，逆向较短 | 2→2 | 788／108 | 逆向 2 条事件，共 809 字节正文。 |
| S＝862，H＝885，正反平局 | 10→10 | 811／131 | 逆向 10 条事件，共 5335 字节正文。 |

最终精确及正向路径的当前业务候选复制均为 0 行。源库全部正文查询与对象事件分页、解码交叉核对，未发现旁路正文读取。四分区各自执行无快照完整控制、完整快照入口和报告入口，共 12 项全部通过；完整 H 映像及报告所需行与独立控制一致，并与调整前的排序映像核对一致。源库、副本和 WAL/SHM 不变，连接与临时文件全部释放。完整正向恢复分别核对 S 行结构与 H 结果，使用两份阶段校验结构，本次累计建表 27.345 毫秒；报告正向沿原共享作用域只建一份，13.351 毫秒。包含时间会嵌套，单次冷进程计时不作为稳定提速或目标硬件结论。

完整报告 43、44 仍从原无快照保存库重建，与上一轮已安装源码 `27d1479c` 采用相同参数、冻结登记和输入。下表为本轮增量对照，不能与录像收尾的 360 次 SQL 操作混用。

| 测量项 | 报告 43，H＝862，前→后 | 报告 44，H＝897，前→后 |
| --- | --- | --- |
| 源库全部 SQL | 635→620 | 843→817 |
| 源库 SELECT | 512→497 | 674→650 |
| 动作候选及 JSON 正文 | 13→13 行；7176→7176 字节 | 16→14 行；8266→7569 字节 |
| 临时行库 SQL | 180→198 | 240→231 |
| 行校验库 SQL | 337→337 | 633→579 |
| 源连接／校验结构创建 | 1／1→1／1 | 1／1→1／1 |

动作 21 在 H＝897 没有分段错误及覆盖判断，当前只恢复动作、活动和正式集合三行，省去两行成员。两个原报告没有触发非空增长展开，该分支由受真实接口约束的纯单元及独立代码审查覆盖。动作 20 仍需按失败头查询来源 19，未建立跨动作无界缓存。报告 43 的临时行库 SQL 增加 18 次 SELECT，因此本次收益不表示所有类型的查询都减少。

报告 43 保持 2221 字节及 SHA-256 `222f24875b4cb2334583755e5f98ac30510755ecb405bc8f59327300da585118`；报告 44 保持 2878 字节及 SHA-256 `f03be0eb74e37f5c5d53f0031305548faf35abd7744005c43715b0ed56ac3210`。最终文件与原登记及前版逐字节相同。本次完整报告生成分别为 1.820、1.782 秒，仅记录单次带观察样本。

Linux 开发容器、CPython 3.11.17、SQLite 3.53.1 下，最终源码执行 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25T14:49:08.234611 至 14:51:10.265235，取得 **8329 项通过、1 项跳过、2 条既有警告，pytest 用时 110.51 秒，退出 0**。原 schema／归属／关系错误保留，旧独立历史比较用例按构建验收职责调整；修改的集成源码只作 AST 语法检查，按约定未运行集成测试。权威登记检查、3 项 Node 单元、资源同步核对及文档检查通过。最终独立审查已关闭实际发现的错误头引用识别和阶段校验库冲突，无未解决发现。

完整命令、冻结源码、旧失败见证、单元和独立审查位于 `.local/verification/report-history-completion-20261025/`；最终专项门禁为 `after-snapshot-validated-gate.json`，正文范围为 `after-snapshot-validated-event-body-audit.json`。原报告最终文件及摘要位于 `.local/verification/report-required-history-20261025/after-snapshot-validated/`。专项使用已保存真实历史，属于只读性能诊断，未运行 CLI、调度器或设备；纯单元与这些诊断不证明真实并发或相机时序。

人工验证环境已安装此源码的 wheel，SHA-256 为 `0ff14778664431109ecd9460ef3e0731510b8d5853f5d22a3b14b3a7503aad26`。320 个生产 Python 文件、27 项权威资源在源码、wheel 和实际 venv 中一致，安装前后依赖检查通过。原库 41 张表、完整结构、V5 元信息、实例身份、日志模式及原库和 WAL/SHM 文件摘要、长度、修改时间均相同；配置、日志及既有能力文件未变。仅对原库文件的独立副本建立 SQLite 连接，没有格式升级或执行 camctl 命令，人工环境 camctl 保持停止。安装收据位于 `.local/deployments/20261025-report-history-completion/delivery-manifest.json`，现场拍摄及报告／媒体结果由人工继续验证。

通用有限主行与限定业务列的快照择路、正常 `run` 的快照维护装配及共同数据库派发仍由[原性能补全计划](../superpowers/plans/2026-10-25-history-performance-closure.md)独立跟踪。本次报告范围及快照正文读取的验收不代替这些生产接线。

## 报告必要历史读取的验证与安装（2026-10-25）

报告生成已接入专用历史入口：根据[字段依赖登记](database/report-dependencies.md)推导投影、关联和前置校验的保守表闭包，再按对象归属及 H 处唯一正式发布的集合限定候选。操作流程、尝试、结果页和基准等无关内部投影不再复制到报告恢复库。普通产物、交付及引用文件也使用报告入口；交付展示只补来源产物主行，继续核验拷贝和目标文件。完整历史恢复与快照维护仍使用完整自身范围。行为边界见[历史查询责任](historical-state-query.md#报告读取与完整历史恢复的责任)，基线及剩余问题见[范围审查](report-history-read-scope-review-2026-10-25.md)。

取消项复用首次恢复的目标；取回失败按 H 处全部相关交付一次遍历，再以可再生磁盘索引保持来源、分支和交付身份顺序，保留非入选交付的失败和缺失关系检查。同来源、同 H 的分段展开可借用当前动作句柄。每份报告复用一个只读源连接和一份空行校验结构，各页仍单独结束读事务，各对象独立清理唯一键。生成取消或写文件失败时先关闭读取流，再释放共享资源。工作进程的实例、目录和超时设置进入实际生成连接；事件批次独立于候选行批次并传递至全部报告恢复端口。

完整报告对照使用同一只读保存库，SHA-256 为 `0fa9966885baec817d55de9fb70bd90f406c29c0ae3cc9aa3086ab7fa0971ef7`。基线源码为 `6847e556a0384f7c85b364e822fe4a47c0cbe4b6`，优化源码为 `27d1479c5932ceb3112e45d2ade9b6fcd403d032`，两侧分别冻结源码并从各自权威来源同步资源。下表是每份完整报告各生成一次的结果，SQL 按 SQLite 跟踪回调计数，含连接初始化；动作候选不包含普通对象内部恢复的候选，不能把该行数解释为整份报告所有查询行。

| 测量项 | 报告 43，H＝862，优化前→后 | 报告 44，H＝897，优化前→后 |
| --- | --- | --- |
| 源库 SELECT | 1395→512 | 1432→674 |
| 源库全部 SQL 回调 | 2459→635 | 2548→843 |
| 源库连接打开 | 82→1 | 86→1 |
| 完整校验结构创建 | 5→1 | 5→1 |
| 动作候选行及 JSON 正文 | 27→13 行；21504→7176 字节 | 27→16 行；21504→8266 字节 |
| 动作恢复处理的事件 | 24→24 | 4→4 |
| 墙钟／进程 CPU 秒 | 2.067／2.175→2.098／2.207 | 2.498／2.625→2.121／2.237 |

报告 43 保持 2221 字节、SHA-256 `222f24875b4cb2334583755e5f98ac30510755ecb405bc8f59327300da585118`；报告 44 保持 2878 字节、SHA-256 `f03be0eb74e37f5c5d53f0031305548faf35abd7744005c43715b0ed56ac3210`。优化前后文件字节相等，并分别符合原冻结登记；输入库及 WAL/SHM 摘要不变，生产代码关闭全部观察连接，临时文件无残留。临时行库 SQL 分别为 230→180、174→240，校验库为 925→337、1065→633；普通对象也进入专用校验，因此不能声称全部临时库操作都减少。原始命令、源码冻结、逐表数据及完整对照位于 `.local/verification/report-required-history-20261025/` 的 `baseline/`、`after-first/`、`comparison-after-first.json`。单次带观察采样不证明稳定提速，报告 43 的本次耗时基本持平；这些完整报告也不是此前录像收尾 360 次 SQL 的操作。

Linux 开发容器、CPython 3.11.17、SQLite 3.53.1 下，优化源码运行 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25T09:15:30.659235 至 09:19:27.878574，结果为 **8274 项通过、1 项跳过、2 条既有警告，pytest 用时 206.91 秒，退出 0**。外层执行含启动和收场共 223.79 秒；命令、版本和日志保存在上述目录的 `unit.json`、`unit.log`。核心及最终生产接线分别独立审查，未发现实质缺陷，记录为 `core-review.json`、`independent-review.json`。按用户要求未运行集成测试，未启动人工环境 camctl 或调用设备；纯单元不能证明真实并发、资源组合或相机时序。

人工环境已安装本轮 wheel，SHA-256 为 `d0ff7de46bb32429a771a8a4f5678df158db1d4916b43cc9238f0b5ef77e4b00`。318 个生产 Python 文件及 27 项权威资源与源码、wheel、实际安装完全一致，安装前后依赖检查通过。41 张表、完整结构、V5 元信息、实例身份、原数据库及 WAL/SHM 摘要与修改时间均保持一致；配置、日志、host/client 既有能力文件未变，人工环境 camctl 保持停止。状态审计只对原文件的独立副本建立连接，没有执行任何 camctl 命令或格式升级。收据位于 `.local/deployments/20261025-report-required-history/delivery-manifest.json`；实际设备结果由用户继续人工验证。

该阶段的保守闭包及无快照样本作为后续增量基线保存。按实际公开分支恢复增长关系、精确快照直接读取及正向严格区间已经由[后续验证](#报告公开分支与快照直接读取的验证2026-10-25)完成；通用有限主行择路和正常 `run` 维护装配仍由原补全计划分别跟踪。

## 历史性能复核与剩余小结果复用（2026-10-25）

复核[历史计划 H4、H6、H7](../superpowers/plans/2026-09-30-camctl-history.md)及路线图阶段 7 后，确认完整对象仓储已按对象计数选择正逆路径，有限主行与限定业务列仍固定逆向。分段对象快照先逆扫 `(S,C]` 证明一次赋值字段，实际成本不能仅以 `(S,H]` 计；正常 `run` 也尚未装配快照维护及业务优先派发。相关结构与生产接线仍由[补全计划](../superpowers/plans/2026-10-25-history-performance-closure.md)跟踪。本次未改变状态库格式或生产线程模型。

已实施四个小结果入口的成功核验复用：结果页票据、文件归属、保留成员输入和配对事实。原核验正文经独立 AST 对照保持一致；原片首次配对查询及错误事实拒绝保留。同一只读样本、相同返回结果下，SQL 从 405 降至 360 次，199 种 SQL 与参数组合不变，重复执行从 206 降至 161 次；票据定位从 22 降至 1 次，原片空配对定位从 10 降至 1 次。调用剖析耗时为 0.583 和 0.461 秒，仅记录这次采样，不外推目标主机稳定提速比例。样本有 118 条维护进度，最大未包含变化为 42，默认阈值 64 下无候选，故不能以样本没有快照证明维护异常或将 360 次 SQL 归因于缺少快照。

基准原记录共享既有三份窗口的独立实验保持隔离：与小结果优化组合时，SQL 为 360→382，结果页关系重验为 8→13，事件解码为 169→182，基准查询减少但结果页淘汰增加。生产窗口用途与容量保持原状。实验的源码、相同快照和结果核对、单元记录位于 `.local/verification/history-performance-closure-20261025/baseline-window-experiment/`。

快照维护在候选返回、依据返回及准备结束后三处重新检查收尾状态，停止后不进入下一阶段，已取得资源沿原释放路径回收；实际错误和已开始操作的结果处理保留。合并生产源码 `1ac3bba2` 在 Python 3.11.17、SQLite 3.53.1 上的单元全量为 **8140 项通过、1 项跳过、2 条既有警告，114.56 秒，退出 0**。UTC 窗口为 2026-10-25T06:40:18 至 06:42:19；命令、工作目录、源码、日志、退出码和只读对照位于 `.local/verification/history-performance-closure-20261025/`。按用户要求未运行集成测试或相机命令，生产线程组合与真机时序仍未验证。

该阶段安装的 wheel SHA-256 为 `b4dd17075da257ddc12eb31ba3b9b43eed8f9951ef56ed31d06ba2b3fd5a49f3`。316 个生产 Python 文件及 27 项权威资源与源码、wheel、实际安装一致；依赖及静态能力检查通过。安装前后配置、41 张业务表、V5 元信息及实例身份一致，日志未变，camctl 保持停止。交付清单位于 `.local/deployments/20261025-history-read-proof-reuse/delivery-manifest.json`。

## 公共事务读取复用的验证与安装（2026-10-25）

公共完整行、历史事务范围、有限核实结束决定与原生成员分类接入单次事务复用。完整类型化输入限定读取身份，`validated` 与完整成员核验分别保留资格；写入和独立守卫阶段切换继续清空依据。结束决定首次仍按原完整申请核实，原生分类首次仍完整读取两遍成员。有限原页窗口保持三份原历史行，通用缓存只增加有容量限制的事实和小型结果，不保留完整成员集合。行为契约由[事务读取复用](persistence-runtime.md#单次完整事务的读取复用)维护，方案选择和进度由[专项计划](../superpowers/plans/2026-10-25-common-transaction-read-reuse.md)记录。

同一固定数据库快照、原保存申请及只读 `verify_segment_finish` 的 SQL 从上一交付的 **1416 次降至 405 次，减少 71.4%**；相同语句和参数的重复执行从 **1218 次降至 206 次**，当前共 199 种语句及参数。启用调用剖析的经过时间为 **0.944 秒至 0.583 秒**。返回结果及快照 SHA-256 一致；最初未复用版本为 38520 次 SQL／10.805 秒。有限页窗口合并后的基线为 16772 次 SQL，文件证明重新接入小值复用后继续下降。剩余读取包含不同历史事实、页窗口重读及其他资格查询，不以缓存任意 SQL 代替业务核验。该对照是单次只读诊断，包含计数和剖析开销，不是原完成写事务或目标硬件提速结论。完整比较见 `.local/verification/common-transaction-read-reuse-20261025/performance-comparison.json`。

验证源码为 `1973dc05c0d517a890b92e5dcfc1ee32da9266dd`。Linux 开发容器、CPython 3.11.17、SQLite 3.53.1 下运行 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25T05:59:52.342596+00:00 至 2026-10-25T06:01:38.017619+00:00，结果为 **8087 项通过、1 项跳过、2 条既有 asyncio 标记警告，100.71 秒，退出 0**。原四个读取函数正文语法树与合并基线一致，独立只读复核未发现实质缺陷。命令、环境、源码、退出码、日志、正文核对与复核范围保存在上述目录的 `unit.json`、`unit.log`、`original-read-body-audit.json` 及 `independent-review.json`。按用户要求未运行集成测试或相机命令。

原人工环境 `/home/cld/.camctl/manual-validation-20261025` 已安装对应 wheel，SHA-256 为 `7e2fc17999554d19681a0e4fb9e05faeefb240359be13dfc68f8f9c62c898968`。316 个生产 Python 文件和 27 项权威资源与源码、wheel 和实际安装逐项一致；依赖检查与静态能力导出通过。安装前后配置、41 张业务表、元信息及实例身份相同，数据库继续使用 V5；主机和客户端能力文件相同。交付清单位于 `.local/deployments/20261025-common-transaction-read-reuse/delivery-manifest.json`，人工入口 README 已更新。

camctl 按用户要求继续保持停止，安装后确认没有相应进程，日志大小及修改时间未变。报告维护的空闲完整冻结门控继续保留，会话默认每 500 毫秒的一次索引水位查询仍用于发现变化。真实录像写事务、取回结果及后续启动按[人工验证指南](../hardware/result-reading-performance-validation.md)由用户提交新计划核对。

## 结果证明复用与报告按需维护（2026-10-25）

人工环境计划 6 的延时动作 21 发布一份原片和一份预览，`finish_segment_recording` 仍执行 124176 次 SQL，经过 15.107 秒，实际提交仅 2.518 毫秒。调用剖析确认，规划、规范事件生成、独立封口及业务守卫多次展开同一结果页、固定文件及内部完成／配对证明。修复在一次完整事务内复用已成功核实的读取；本连接写入后失效，作用域退出后释放，原当前资格、历史完整性及实际共同发布核验保留。边界由[持久化设计](persistence-runtime.md#单次完整事务的读取复用)定义。

同一固定数据库快照、保存请求及只读 `verify_segment_finish` 入口的 SQL 从 **38520 次降至 1416 次**，历史事件解码从 **8035 次降至 181 次**，启用调用剖析时的经过时间从 **10.805 秒降至 0.944 秒**。返回结果和快照 SHA-256 一致。该对照不是原完成写事务，不据此推算实际拍摄的净提速；原 79 秒总计时也不能事后拆解。基线、优化调用剖析及 `read-comparison.json` 保存在 `.local/verification/segment-query-amplification-20261025/`。

报告后台拥有者在创建任务之前检查业务水位、首次恢复及到期同步。没有触发时不打开维护连接、不创建后台报告任务，也不进入 `_FreezeCommand`；已有会话连接仍保留默认 500 毫秒的一次索引水位查询，外部新计划的轻量发现继续执行。实际 SQLite 查询计划使用覆盖索引。失败后的新变化、尚未尝试的其他报告和最终关闭期间的新提交继续处理；仅等待 ACK 时正常退出。行为由[报告执行设计](report-runtime.md#报告维护的轻量观察与推进)维护。

验证生产源码为 `76903bf49d8ba8a5ea0344cf63d4bb86a6cdfb3b`。Linux 开发容器、CPython 3.11.17、SQLite 3.53.1 下运行 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25 05:25:17 至 05:27:22，结果为 **7993 项通过、1 项跳过、2 条既有 asyncio 标记警告，119.16 秒，退出 0**。独立复核未发现可达的实质缺陷；五个原证明读取函数的业务语法树保持一致。完整命令、版本、源码、退出码和日志位于上述目录的 `unit.json`、`unit.log`、`proof-review.json` 及 `agent-evidence/`。本轮未运行集成测试或相机命令。

原人工环境 `/home/cld/.camctl/manual-validation-20261025` 已安装对应 wheel，SHA-256 为 `5231241fcb5e02d369a586cbb95b5b4fcfbf978eca2689e83b01cdf58f4cafba`。315 个生产 Python 文件和 27 份权威资源与源码、wheel、实际安装逐项相同；依赖检查及原配置下静态能力导出通过，能力说明与现有主机和客户端文件一致。安装前后配置、41 张业务表、元信息及实例身份相同，数据库继续使用 V5。交付清单位于 `.local/deployments/20261025-result-proof-reuse/delivery-manifest.json`。

按用户要求停止原 camctl 后未重启，安装结束再次确认没有 camctl 进程、日志大小和修改时间未变化。人工环境 README 已更新。新录像写事务耗时、相机结果和后序动作启动由用户按[人工验证指南](../hardware/result-reading-performance-validation.md)继续核对。

## 录像结果事务成本计量的验证与安装（2026-10-25）

本轮为录像结果完整事务增加阶段独占计时、线程 CPU 时间、SQL API 调用及行数聚合，并记录历史解码和校验的包含子调用明细。保存动作、方法和原操作键贯穿分段原保存及保存恢复；字段解释与新计划测量步骤见[人工验证指南](../hardware/result-reading-performance-validation.md#事务成本分解)。事务、批次、调度、重试和设备调用沿用原行为。

验证源码为 `188c307d1f8d000da33cdc0183bdea40b54e8aea`，环境为 Linux 开发容器、CPython 3.11.17、SQLite 3.53.1。命令为 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25 03:25:50 至 03:27:37，结果为 **7796 项通过、1 项跳过、2 条既有 asyncio 标记警告，102.46 秒，退出 0**。命令、环境、源码及全部输出保存在 `.local/verification/transaction-cost-measurement-20261025/unit.json` 和同目录 `unit.log`。独立只读审查通过；移除计时包裹后的事务业务语法树与原实现一致，核对收据为同目录 `ast-equivalence.json`。本轮未运行集成测试或真实相机命令。

人工环境 `/home/cld/.camctl/manual-validation-20261025` 已安装对应 wheel，SHA-256 为 `63e0ba5eb88db52633c1025cebdf521b6a38a2064380d8097102203752206711`。314 个生产 Python 文件及 27 项权威资源与源码、安装包逐项一致，依赖检查和原配置下的静态能力导出退出 0；能力与主机、客户端现有文件一致。安装前确认没有待执行动作、拍摄占用、在途尝试及基准读取，随后停止空闲的旧 CLI 进程。安装前后配置字节、41 张业务表、元信息和实例身份均相同，数据库继续使用 V5。安装核对与原进程退出记录由 `.local/deployments/20261025-transaction-cost-measurement/delivery-manifest.json` 汇总。

该版本提供计量能力；原日志只有约 79 秒的总耗时，不能事后还原内部占比。实际阶段数据仍待用户以新的计划时间和 `request_id` 运行同配置录像后，从 `camctl.log` 中读取。

## 软件恢复启用配置与诊断修复的合并验证（2026-10-25）

软件恢复配置使用 `devices.<id>.software_recovery.enabled`，默认关闭；驱动维护已验证的精确固件及原拍摄范围。当前记录覆盖 OSMO 360 II 构建 38390 的普通分段录像，真实固件仍在原恢复检查中读取并精确核对。原冻结责任继续使用原指纹、期限和预算。准备占用的报告事实修复同时包含在该版本中，实际行为由[配置简化设计](../superpowers/specs/2026-10-25-software-recovery-enablement-design.md)及下节诊断记录说明。

完整验证源码为 `7f41a3c2d8cee27fe6593eae3766abb23130eb45`，Linux 开发容器、CPython 3.11.17、SQLite 3.53.1。命令为 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25 02:20:13 至 02:22:25，结果为 **7757 项通过、1 项跳过、2 条既有 asyncio 标记警告，129.93 秒，退出 0**。完整命令、源码、环境和退出码保存在 `.local/verification/recovery-enable-config-20261025/unit.json`，同目录 `unit.log` 保留全部输出。独立静态评审通过；直接使用旧接口的集成夹具已机械迁移，未导入或运行集成测试。

人工环境已安装对应 wheel，SHA-256 为 `8581af414ae1b26aa57d28cd821c0b45e19ec9183ef3ca6a9b1f056a7cf810e2`。313 个生产 Python 文件和 27 项权威资源与源码、安装包逐项一致，依赖检查和实际配置下的静态能力导出退出 0。配置仅将 OSMO 软件恢复段的旧启用字段迁移为 `enabled = true`，原配置保留备份，其他值逐项一致；安装版配置加载与支持记录纯对象核对通过，不调用相机。

安装前没有拍摄占用、在途尝试或 CLI `run`，保留 4 个待执行动作。安装前后 41 张业务表、元信息及原实例和目录绑定全部相同，数据库继续使用 V5。主机与客户端能力文件与安装版导出一致。安装、配置迁移及状态核对收据由 `.local/deployments/20261025-recovery-enable-config/delivery-manifest.json` 记录；没有运行真实拍摄或重启命令，人工验证继续由用户执行。

## 准备占用的报告事实切片修复（2026-10-25）

人工验证的 host 诊断记录显示，计划 4 的 `run` 在准备占用保存阶段以 `state_db_error` 退出，原因为报告关联解析缺少 `device_activities` 事实。新准备活动的生产者只提供动作行，报告目标派生则在应用事件新行前读取事务前切片；缺少来源表声明使事务回滚。该计划没有保存准备活动或新设备调用，录像与延时动作均未派发 START。

生产修复 `45635231` 在新建准备活动路径显式提供空的 `device_activities` 事务前切片，完整活动仍由事件新行提供。照片、录像和延时的回归直接调用真实 `ReservePreparationCommand.plan()` 和 `event_report_targets()`；修复前 3 项均复现原错误，修复后与历史派生及准备事件相关范围合计 **50 项通过，0.77 秒，退出 0**。命令、环境、源码及日志收据保存在 `.local/verification/preparation-report-facts-20261025/unit.json`。独立静态复核通过；未运行集成测试或真实相机命令。

对应安装包已更新原人工验证环境，源码、wheel 与安装文件逐项一致；313 个生产 Python 文件和 27 项权威资源核对通过。安装前没有在途调用、拍摄占用或 CLI `run`，保留 4 个待执行动作。安装前后 41 张业务表和元信息均未改变，数据库继续使用 V5，配置保持原值。安装包 SHA-256 及完整核对由 `.local/deployments/20261025-preparation-report-facts/delivery-manifest.json` 记录。原计划窗口已经结束，人工测试使用新的计划时间及 `request_id`。

## 公共拍摄准备与哈苏接入（2026-10-25）

验证源码为 `8d89fa1112e8474b751a62d416b2ccd9084da667`，范围为公共准备合同、哈苏独立设置与实际 START、照片调度及仓储、V5 格式与保留历史升级，以及准备返回后的取消、过期和真实错误保存。MF 的 `focus` 与 AF-S 的 `autofocus` 保留在实际 START 内，准备恢复不重放镜头操作或已存在的照片 START。

Linux 开发容器使用 CPython 3.11.17、SQLite 3.53.1。完整单元命令为 `PYTHONPATH=apps/camctl/src:apps/camctl/tests apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`，UTC 2026-10-25 01:47:53 至 01:50:42，结果为 **7729 项通过、1 项跳过、2 条既有 asyncio 标记警告，166.02 秒，退出 0**。命令、源码、环境及退出码收据保存在 `.local/verification/shared-capture-preparation-20261025/unit.json`，完整输出为同目录 `unit.log`。独立静态评审通过；本次未运行集成测试或真实相机命令。

人工验证环境已安装 `.local/deployments/20261025-shared-capture-preparation/packages/camctl-0.1.0-py3-none-any.whl`，SHA-256 为 `021d7d52c59adb84cfced5bd0ee56a00e7986760ceb1ce1362d6a85918646ee2`。安装后的 313 个生产 Python 文件和 27 项权威资源与源码、wheel 逐项一致；运行时依赖检查及静态能力导出退出 0。已安装纯对象核对确认哈苏支持独立准备，公共默认提前量为 10 秒，当前格式为 V5；这些检查不访问设备。

安装前确认原环境没有待执行动作、拍摄占用、在途尝试或 CLI `run`。正式 `init --upgrade-capture-preparation` 在原目录生成一致备份，将实际库从 V4 升级为 V5，退出 0。升级前后及备份逐表核对，41 张业务表的内容全部相同，仅元信息格式由 4 改为 5；原实例、三处目录绑定、配置及产物登记保留，外键检查无错误，完整性检查为 `ok`。原 host 与客户端能力文件和安装版导出一致。部署收据、备份路径及核对文件由同目录 `delivery-manifest.json` 记录。

本节的软件与安装结果不代替真实设备验收。用户继续使用原人工验证入口提交新计划；默认准备应留足 10 秒及计划传递时间，哈苏实际对焦仍须在原启动窗口内完成并派发 `capture`。

## 拍摄提前准备与基准单次扫描（2026-10-25）

按照[提前准备规格](../superpowers/specs/2026-10-25-capture-preparation-design.md)，Action6 与 OSMO 的默认提前量为 10 秒。设置准备独立保存，完整基准复用单次扫描；准备期间保持 pending，到计划时间才复用原活动授予真正 START。原取消、过期、冷恢复、结果保存未确认及分段失败结论均沿同一可靠责任处理。

最终合并生产源码为 `c6dd84cd37ebf84157be779a23783a70a2a5a3f5`。Python 3.11.17、SQLite 3.53.1 的完整 camctl 单元测试为 **7591 passed、1 skipped**，83.90 秒，退出 0；两条警告来自既有同步测试的 asyncio 标记。原命令、源码身份、日志和退出码位于 `.local/verification/capture-preparation-20261025/unit.{json,log}`。独立静态审查核对了准备凭据、真实 SQL 字段、持久收场责任、重复取消和分段历史锚点。按用户要求没有运行集成测试。

安装交付位于 `.local/deployments/20261025-capture-preparation/`。wheel SHA-256 为 `87d58120d2216566fef811c94dad0a3945d63dbbe2276cf65c36cf20875e0e64`；312 份生产 Python 文件和 27 份权威资源与源码、wheel、独立安装逐字节一致。依赖检查、能力说明导出及安装版默认提前量核对均通过。原人工验证入口已经更新，配置保持原值。

已在没有未完成拍摄、设备调用和旧 CLI 会话时，通过安装版 `init --upgrade-capture-preparation` 将人工状态库从 V3 升级为 V4。SQLite 一致备份保留；升级前后 41 张业务表的行数及完整内容摘要一致，仅元信息格式编号变化，实例、目录绑定、历史、水位和产物登记保留。外键与完整性核对通过。备份位置、逐表核对、构建及安装记录由 `delivery-manifest.json` 统一引用。这是实际安装升级核对，没有执行拍摄命令；真实准备耗时与启动结果仍由用户按[人工验证指南](../hardware/result-reading-performance-validation.md)核对。

## 结果读取与调度性能优化（2026-10-25）

[结果读取性能计划](../superpowers/plans/2026-10-25-result-reading-performance.md)已完成软件实施及人工环境更新。分段结果现在对每个固定目录执行一次完整枚举，以临时文件缓冲并连续分页；主机按原活动固定基准筛选新增文件，仅对这些候选查询大小，结果仓储继续独立核对差集。元数据私有帧保留逐文件错误，读取兼容原格式。前页历史沿同一拥有者增量核对，暂停或保存未知时保留原工作责任。

到期动作的窗口观察从同设备长任务中独立推进，报告维护使用自己的后台拥有者。报告前提错误在等待日志副本前交给中央停止派发，文件阶段取消先取得实际结果；会话在实际任务结束后关闭目录缓存，诊断取消不跳过必要资源清理。各阶段耗时通过现有普通日志队列记录，文件完成等待单独计时。

Linux x86_64 开发容器、Python 3.11.17、SQLite 3.53.1 的合并树完整单元测试为 **7258 项通过、1 项跳过，107.86 秒，退出 0**。两条已有警告分别来自设备和进程模块中同步用例的 asyncio 标记。独立评审的长报告、致命错误、文件阶段返回错误与取消、缓存关闭失败均有受控单元回归。本次按用户指定不运行正在独立重构的集成测试；完整命令、源码身份、实际运行时间及退出码保存在 `.local/verification/result-reading-performance-20261025/unit.json` 与同目录日志。

人工环境仍使用 `/home/cld/.camctl/manual-validation-20261025`。没有 CLI 会话执行时更新了安装版，日志级别设为 INFO；原配置和前一份 wheel 保存在本次交付目录。新 wheel 的 308 个生产 Python 文件和 27 份权威资源与源码及安装目录逐字节一致，运行依赖检查退出 0。安装版 `describe` 与现有主机及客户端能力说明逐字节一致，不调用相机。交付清单及安装核对位于 `.local/deployments/20261025-result-reading-performance/`，当前环境 README 指向该目录。实际相机调用、阶段耗时及产物正确性由用户按[人工验证指南](../hardware/result-reading-performance-validation.md)验收，尚无本次真实设备提速结论。

## 三秒停止后文件完成等待（2026-10-25）

OSMO 普通录像、OSMO 原生延时及 Action6 原生延时在可靠 STOP 确认后的文件完成等待为 3 秒，能力说明从对应生产定义生成。新受理动作保存 `file_completion_wait_ms=3000`，既有动作按原执行定义继续恢复；自然结束的结果余量由独立字段表达。

Python 3.11.17 的六份相关单元测试为 300 项通过、4.78 秒、退出 0，覆盖驱动工厂、执行定义冻结、完成上下文和结果读取。安装版工厂实际生成的三类任务均保存 3000 毫秒等待。wheel 的 308 个生产 Python 文件和 27 份权威资源与源码及安装目录逐字节一致，主机和客户端的磁盘能力说明已同步。当前人工环境 README 指向 `.local/deployments/20261025-file-completion-wait/`，交付清单及单元回执分别在该目录和 `.local/verification/file-completion-wait-20261025/`。本次未运行集成测试或调用真实相机，实际拍摄由用户继续人工验证。

## 无产物来源失败的取回与客户端说明（2026-10-24）

[来源失败计划](../superpowers/plans/2026-10-24-source-action-failed.md)的生产与消费链已经接通。普通及分段来源只有在动作 FAILED、适用处理完成且完整正式产物目录可靠为空时，才对默认及预览固定 `source_action_failed`；原拍摄错误保持。受理失败的空执行定义经过真实取回入口及事务守卫处理，分段存在性查询使用有界 SQL；保留产物、显式 ID、取消、过期及固定历史边界均按原规则。

Python 3.11.16、SQLite 3.53.1 的最终串行结果：`unit/outputs` 为 847 项通过；`integration/outputs` 为 2519 项通过、2 项跳过、616.75 秒；`integration/reporting` 为 447 项通过、78.07 秒，均退出 0。独立评审提出的受理失败空值入口与全目录枚举问题均有先失败反例及修复后通过结果。最终日志和命令位于 `.local/worktrees/source-action-failed-backend-20261024/.local/verification/source-action-failed-backend-20261024/`；只有最终独立串行运行列为门禁结果。合并树单元测试为 7137 项通过、1 项跳过、40.33 秒，保留两条既有 asyncio 标记警告。

生产链生成的报告样例为 1005 字节，SHA-256 为 `5f5feafddbbd6a5547429b4e056aa73c839a788b9061fd92331d5a8d370df579`。客户端通过真实文件上传、摘要及文件名核验、报告解析和状态保存导入同一份字节，状态为 `accepted`，覆盖水位为 16；页面说明函数返回来源名称、ID、失败说明及原 `capture_failed`。报告省略空 `outputs`，客户端不会以缺少显式空数组拒收，也不会据省略字段清除已知产物。客户端单元 3229 项、报告导入 51 项、预览浏览器 19 项、类型检查及构建通过。样例、导入数据、命令脚本及证据位于 `.local/deployments/20261024-source-action-failed/validation/`，客户端独立结果见[客户端记录](../client/verification.md)。

wheel `2511ea93e54104cf96fde38efb3130a4f1b2539e09206a3238bdf3c6f01f9c5e` 的 305 个生产 Python 文件及 27 份权威资源，与源码和独立安装版逐字节一致；固定依赖检查通过。独立安装版的初始化、能力导出及空计划运行均退出 0，能力说明 SHA-256 为 `c8af4c68a6c414af31a391254c23793c4a0d146337645f441b9f256788d5271b`。待交付客户端 ZIP 的 SHA-256 为 `3ab57b4297d2ed80f3bcbbab5c3772aabf2d17799e8dcb6a9b1c5fc6e6d79fc1`。这些安装审计不代表当前手工环境已经更新；统一部署等待集成测试提速完成。

## 基准目录读取等待与进程恢复（2026-10-24）

请求 `1501513342387992114` 的 Action6 来源 11 在正式 START 之前读取文件基准，前五页保存 640 条。第六页尚在执行时，共同恢复入口把正常等待解释为保存失败，CLI 返回 `state_db_error`，报告 23 保留来源与预览正在执行的快照。现场、原错误及不接触相机或状态库的同文复现实验保存在 `.local/diagnostics/report23-baseline-20261023T220854Z/`。

修复由[基准等待与恢复计划](../superpowers/plans/2026-10-24-baseline-waiting-recovery.md)跟踪。正常等待由 WAITING 表达，PENDING 只表达原保存未可靠完成，包括没有附加异常的 NOT_EXECUTED。其他流程不消费实际调用拥有者尚未交付的结果；本会话尚未派发的意图恢复后继续原拥有者，取消或窗口结束时先按原结果键关闭意图，再提交终态。进程恢复按稳定游标分批发现已经可靠结束目录调用的未派发终态范围，逐项通过原释放事务解除占用；INTENDED 和仍有本地拥有者的调用不被接管。

环境为 Linux x86_64 容器、Python 3.11.16。正式专项在独立源码副本中执行，`PYTHONPATH` 指向副本，包资源也只同步到该副本；逐项日志、命令、工作目录、UTC、解释器及退出码位于 `.local/verification/baseline-waiting-20261024/`。已验证的范围如下：

| 范围 | 结果及证据 |
| --- | --- |
| 等待、保存重核与取消 | 原分段等待的两项反例在旧实现报出同文错误；新增意图恢复、窗口结束、关闭失败和 NOT_EXECUTED 反例分别先失败。 |
| 基准共同恢复专项 | 最终 17 项通过，3.21 秒，退出 0，包含普通／分段在途读取、冷恢复、未返回意图保持占用，以及批量为 1、首项仍由本地持有时的多页恢复。 |
| 装配组合 | 最终基准、工具取消及拍摄取消组合为 27 项通过，5.19 秒，退出 0，包含冷恢复与分批场景。 |
| 拍摄组合 | 六个基准与分段相关文件共 128 项通过，61.20 秒，退出 0。未派发意图取消的既有用例改为核对 UNCONFIRMED、空实际返回时刻及空错误，不再要求留下 INTENDED。 |
| 单元测试 | 7132 项通过、1 项跳过，41.02 秒，退出 0；保留两条既有 asyncio 标记警告。 |

扩展初轮装配目录为 1343 项通过、13 项失败、1 项跳过。独立归档 `d08bf72` 后保留完整目录收集，再筛选对应函数，13 个失败 nodeid 及各自 actual/expected 与初轮完全相同；另外 7 个参数分支通过。失败集中在 `test_distribution.py` 的两项状态库版本预期，以及 `test_osmo_shared_recovery.py` 和 `test_osmo_video_mode_completion.py` 的原读取准备、页状态和动作结果预期。完整名称和比较记录见 `bootstrap.log`、`baseline-target-rerun.log` 与其 receipt；不将这些基线失败记为本修复通过证据。

扩展初轮拍摄目录为 2132 项通过、2 项失败。`test_intent_saved_but_cancelled_before_call_has_no_fabricated_return` 的旧断言对应上述已更新并通过的意图关闭规则；`test_action6_cold_recovery_decodes_saved_budget_and_continues_same_copy` 仍在首次读取偏移断言失败，属于下节已独立复现的既有失败。

原现场使用与已安装环境匹配的 `f532db5` 权威资源及修复源码执行正式 CLI，未进行数据库迁移。两次 `run` 均退出 0，外部设备工具由独立包装程序禁止派发，实际调用数为 0。来源 11 因启动窗口已结束而过期，预览 12 失败，报告 24 正式发布；随后原活动 4 经冷恢复释放范围占用，报告 25 覆盖水位 `(67,81]`，SHA-256 为 `8ad85de76261d114e4b70e12ab79c812cc99b2d93b277e969abd388a226010c0`。原 297 条历史及两次恢复前各自的全部历史、原操作流程与尝试逐字节保留；快速检查通过，外键错误为 0。源码身份、前后快照、受限命令、输出与审计见 `.local/deployments/20261024-baseline-waiting-recovery/`。这些结果验证软件收场，不提供重新拍摄的真机证据；统一安装包及开发环境更新另随交付记录。

## 手工验证启动失败收场修复（2026-10-24）

Action6 原生延时在拍摄设置阶段返回 `action6_response_unconfirmed`，原因是 `simulation_service_unavailable`；原 START 尝试已经保存为 `NO_EFFECT`，但分段流程未将该驱动错误归一化为业务失败，导致执行会话退出，动作及预览仍保持运行中，最后报告未覆盖失败结果。

公共分段的无结果启动失败决定现按原已关闭 START 和冻结边界内全部已结束尝试分类：既有业务错误沿原语义处理；原流程失败且全部尝试可靠无效果时，动作以 `capture_failed/device_failed` 结束；其余原始驱动错误以 `capture_result_unconfirmed/start_unknown` 结束，并保留未知活动占用。原流程和尝试中的驱动错误、次数与结果事件均保留。该决定由首次收场、冷恢复及原结论核验共同使用，不依赖终态后的活动投影，也不重新授予启动资格。普通录像仍按原启动窗口与次数预算决定重试或耗尽。

回归覆盖 Action6 与 OSMO 分段普通录像、原生延时、原始驱动错误、既有业务错误，以及终态写入回滚后的恢复；未知效果用例单独核对占用保留。真实失败快照分别由原安装版与修复源码只读核实，前者重现一致性异常，后者得到 `capture_failed/device_failed`。原始快照与诊断位于 `.local/diagnostics/manual-timelapse-20261023T210151Z/`；软件命令及结果位于 `.local/verification/start-failure-settlement-20261024/`。本次不增加新的实拍验收结论。

以下命令均在仓库根执行，使用 Python 3.11.16；对应日志保存命令结果，执行记录保留实际退出码：

| 命令范围 | 结果 |
| --- | --- |
| `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q` | 7132 passed、1 skipped、2 warnings，40.02 秒，退出 0；警告为既有同步测试的 asyncio 标记。 |
| `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/capture/test_segment_start_failures.py -q` | 最终 20 passed，3.88 秒，退出 0；修复前六项原始驱动失败用例重现原异常。 |
| `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/persistence/test_segment_check_conclusions.py -q` | 24 passed，3.83 秒，退出 0。 |
| `apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/integration/persistence/test_segment_result_conclusion_groups.py apps/camctl/tests/integration/persistence/test_segment_result_versions.py -q` | 49 passed，13.78 秒，退出 0。 |

扩展拍摄组合中 59 项通过，`test_action6_cold_recovery_decodes_saved_budget_and_continues_same_copy` 在首次打开读取流次数断言失败，实际偏移为 `[0, 0]`，期望为 `[0]`；该轮退出 1。独立归档原 `f987be1` 源码后，同一用例也在相同位置失败，尚未进入状态库冷重开或预算解码。因此保留该既有失败及基线对照，不将其记录为本次修复的通过证据；本次没有更改该用例或其读取流程。

修复 wheel 的 SHA-256 为 `4d5bd0ef5158c9d483830df33f80c85d67458c2cd32a71fe0b9bb93619c97188`，305 个生产 Python 文件及 27 份权威资源与源码、独立安装版逐字节一致；依赖检查通过，导出能力说明与用户客户端包相同。当前手工验证目录 `/home/zsm1695/.camctl/manual-validation-20261024` 已更新该安装包，host-demo 与客户端包沿用原交付物。

更新前备份原库、配置、日志和交接文件后，通过安装版 `camctl run --config <原配置>` 恢复请求 `5933339839340923781`。恢复入口的外部相机工具由独立包装程序禁止派发，实际工具调用为 0，CLI 退出 0。原延时及预览分别以 `capture_failed/device_failed`、`obtain_items_failed` 结束，报告 6 已发布，覆盖水位为 `(0,24]`，摘要为 `7cfd493194b86ad8cfb0c020102aba63ed697280a198152e625ff2b61e002af2`。原 67 条历史事件、原 START 流程和唯一一次启动尝试均逐字节保持；状态库快速检查通过，外键错误为 0。完整安装、前后库快照、命令与恢复审核位于 `.local/deployments/20261024-start-failure-fix/`。本次只恢复原任务的软件收场，不证明相机控制服务已恢复。

后续请求 `8008267051166010444` 在录像设置失败返回时耗尽启动窗口，动作按可靠未启动事实过期，预览选择保存了默认范围未确认的错误。报告 11 的读取器却把进入过准备阶段的 `EXPIRED` 来源也要求为已发布分段集合，因而再次退出。现仅将过期状态排除出该缺集断言；成功、失败、取消来源及覆盖结果的一致性校验保持。报告继续如实表达过期与无集合，不补造结果集合或放宽 Schema。新增普通录像和原生延时的真实过期事务、预览选择失败、固定历史生成与同边界重建用例，修复前两项重现异常，修复后报告组合 68 passed／41.52 秒、报告单元 744 passed 和 1 skipped／1.88 秒，均退出 0。日志位于 `.local/verification/expired-segment-report-20261024/`。

当前安装已再更新至 wheel `b478184d77f927d9386defa599e0e46aa88cc34f5c197efd00cdfefa42f22d82`；305 个生产文件及 27 份权威资源与源码和独立安装版一致。报告 11 沿原编号、水位 `(24,41]` 与冻结事件 149 重建，摘要为 `489594a8c89634e26b25b4a7e3ec7732cd4092ef7749e546393a8436194e8695`。更新前退出空闲 host-demo 并备份原库后，正式恢复入口退出 0，设备工具调用为 0；后续延时因启动窗口已经错过而过期，最新报告 12 已发布，水位 `(24,47]`。原 150 条历史事件、原启动流程和尝试不变。原现场快照位于 `.local/diagnostics/expired-segment-report-20261023T213225Z/`，安装、前后快照及审核位于 `.local/deployments/20261024-expired-segment-report-fix/`。该记录仍不提供新的实拍或相机服务恢复证据。

## 容器与真机分段验证（2026-10-23）

本次执行对应 [OSMO 分段计划 P9](../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md#p9按独立证据层次完成容器与实机验收)及 [Action6 联合计划 V12](../superpowers/plans/2026-10-23-video-result-modes-and-action6-segmentation.md#v12容器与实机媒体验收)。环境为 Linux x86_64 开发容器，通过 `ADB_SERVER_SOCKET=tcp:host.docker.internal:5037` 访问 Windows 主机 ADB 服务。两台当前固件均仅支持分段视频，单文件真机矩阵不适用；软件中的单文件能力与原验证范围保持。

原始证据根为仓库内 `.local/acceptance/video-real-20261023T062816Z/`。每个场景使用独立配置、状态库、客户端存储和交接目录，保存实际请求、安装版 `describe`、C host 命令、CLI 工具启动记录、报告、媒体导入凭据及 ACK。原相机文件和失败记录保留，后续独立取回不改写原拍摄结果。

### 安装与定向软件验证

相机会话准备及响应修复阶段的安装环境为 `/tmp/camctl-video-real-settings-v2-20261023/venv`，Python 3.11.16、SQLite 3.53.1、ffprobe／ffmpeg 6.1.1。wheel 位于证据根 `session-settings-runtime-v2/dist/camctl-0.1.0-py3-none-any.whl`，SHA-256 为 `3c5e80e31ade0d5fad5e3addac0886ebc67aed9d66ffb079aabd43f9ee791fa7`。同目录 `source-wheel-install-manifest.json` 核对 298 个生产 Python 文件与 27 个权威资源，325 项源码、wheel、安装文件逐字节一致；`uv pip check` 和安装版静态能力导出均退出 0。Action6 设备读数为固件 `v10.00.39.16`；OSMO 商品固件版本未知；系统构建读数为 `eagle4_oq102-userdebug`、增量 `38390`。早期场景使用各自归档的发行物；修复后继续原任务的记录保留实际版本边界。

本次修复包括：预览取回不再误用原片检查副本；Action6 结果读取按冻结模式选择原始分段页，完整扫描结束标记在保存页替换后仍保留；保留产物的完成判定使用已登记状态及原明确失败；OSMO 验收工具只对原片要求两路主视频。另将[相机会话准备](../architecture/camera-parameters.md#相机会话准备)接入两型号的普通录像和原生延时。已观察到的 `simulate_device` 服务不存在／查找失败诊断，即使伴随退出 0，也不再记录为设置接受或进入会话缓存。

以下命令均在仓库根执行，使用 `apps/camctl/.venv311/bin/python`。源码测试设置 `PYTHONPATH=$PWD/apps/camctl/src`；安装版测试设置 `UV_PYTHON=$PWD/apps/camctl/.venv311/bin/python` 和 `UV_PROJECT_ENVIRONMENT=$PWD/apps/camctl/.venv311`。

| 范围与 pytest 目标 | 实际结果 | 证据根下日志 |
| --- | --- | --- |
| `apps/camctl/tests/unit/devices` | 1598 passed，24.23 秒，退出 0；保留一条既有的同步测试误标 asyncio 警告 | `session-settings-service-unit.log` |
| `apps/camctl/tests/integration/devices/test_adb_camera_registration.py` | 30 passed，1.52 秒，退出 0；覆盖两型号正式会话入口复位 | `session-settings-registration.log` |
| `tests/integration/test_action6_builtin_segmented_roundtrip.py` | 2 passed，69.13 秒，退出 0；安装版普通录像、原生延时经 C host、CLI、文件及客户端交付 | `session-settings-v2-installed-chain.log` |
| `apps/camctl/tests/integration/devices/test_action6_result_pages.py` | 9 passed，1.97 秒，退出 0；正式结果适配器及跨页配对 | `action6-result-pages-final.log` |
| `apps/camctl/tests/integration/capture/test_action6_segmented_normal.py` | 4 passed，14.00 秒，退出 0；正常发布及部分结果耗尽后的有限失败 | `action6-segmented-completion-final.log` |
| `apps/camctl/tests/integration/outputs/test_segment_copy_reuse.py`、`test_segment_local_integrity.py` | 9 passed，25.65 秒，退出 0；同一 outputs 目录顺序执行 | `segment-copy-reuse-final.log` |
| `docs/hardware/tests/unit/test_validate_osmo_segments.py` | 54 passed，0.06 秒，退出 0 | `media-tool-final-unit.log` |

新增服务错误分类的 6 项测试在修复前全部失败；修复后与设备目录一同通过。软件替身不承担真实固件、媒体时长或设备状态证据。

随后按既有结果页范围，将分段模式的逐文件 metadata 调用合为一次页内批量读取。设备脚本分别保存每项身份、退出码、stdout 和 stderr 的原始字节；传输、帧、单项任一失败仍使结果页不完整，不增加重试、放宽长度格式或重置期限。生产接口只读扫描完成 Action6 的 734 份文件、23 页、46 次 ADB 调用；OSMO 完成 32 份 OSV／LRF、1 页、2 次调用。各原始页位于 `metadata-batching-continuation/`，这次成功扫描不撤销早期任务的终态失败。

批量读取安装环境为 `/tmp/camctl-video-real-batch-20261023/venv`，版本与上一阶段相同。wheel 位于 `metadata-batching-continuation/runtime-batch-v1/dist/camctl-0.1.0-py3-none-any.whl`，SHA-256 为 `c7af3e20348164aad5dc13fc3378fb1c7ada275474f2506eae4496ecbc71bc45`；298 个生产 Python 文件和 27 个资源共 325 项源码、wheel、安装文件一致，`uv pip check` 和 `describe` 退出 0。

以下批量读取测试继续使用仓库的 `apps/camctl/.venv311/bin/python` 启动 pytest，真机安装环境与测试解释器分别记录。组件集成复核另存 `device-integration-recorded.command.json`，包含完整 argv、cwd、环境、UTC 和退出码。

| 批量读取验证目标 | 实际结果 | `metadata-batching-continuation/` 下日志 |
| --- | --- | --- |
| `apps/camctl/tests/unit/devices` | 1626 passed，24.43 秒，退出 0；一条既有 asyncio 标记警告 | `all-unit-devices.log` |
| `apps/camctl/tests/integration/devices/test_adb_camera_transport.py`、`test_action6_result_pages.py`、`test_osmo360ii_result_pages.py` | 首轮 41 passed、3.04 秒；补存完整命令的复核为 41 passed、2.91 秒，均退出 0 | `device-integration.log`、`device-integration-recorded.log` |
| `tests/integration/test_action6_builtin_segmented_roundtrip.py`、`test_osmo_builtin_record_segmentation_roundtrip.py`，筛选 `action6_segmented or normal or native-tail` | 4 passed、6 deselected，212.18 秒，退出 0 | `installed-chains.log` |

普通取回的源摘要失败收场阶段基于提交 `6946e63192a03548b791b95e314843808eda1f41` 构建，安装在 `/tmp/camctl-video-real-recovery-v1-20261023/venv`。wheel 位于证据根 `resume-20261023T145540Z/runtime-recovery-v1/dist/`，SHA-256 为 `4e12e99434be292e321acbc769f22b7bd5b0d6bd6167b0582fbb7034344aa69d`；298份生产 Python 文件及27份权威资源共325项在源码、wheel、安装位置的字节一致。锁定依赖检查、独立临时库初始化与静态能力导出均退出0，实际 Python 3.11.16、SQLite 3.53.1；完整命令与安装清单位于同目录。该阶段包不包含尚在独立复核的分段检查收场或软件重启恢复。

后续完整恢复候选已集成分段检查失败及分页收场、独立软件重启责任和 V3 显式升级。主树 `06645ee` 的组合验证为7132 passed、1 skipped、2条既有 asyncio 标记警告；受影响的7组组件集成合计141 passed，均退出0，记录位于 `resume-20261023T145540Z/combined-recovery-integration/`。软件恢复候选树的完整 persistence 为925 passed／713.47秒，history 为345 passed／853.38秒；原始命令、PID、UTC 和退出码位于 `.local/worktrees/real-device-reboot-20261023/.local/reboot-validation/`。这两轮自然结束，没有将此前主动中断的 capture 轮次计作通过。

大历史夹具的临时 `fixture-members.db` 插入随后改为每页不超过128条的事务；正式状态库的公共事务、FULL、256／2048规模及测量断言不变。同一20／40条 setup-only 对照实测分别从2.222180／4.987871秒降为1.923613／4.428157秒，同步调用从159／279降为83／123，行数一致，异常回滚与关闭验证通过；记录位于 `.local/worktrees/real-device-test-perf-20261023/.local/verification/large-fixture-*`。完整 history 的345项通过属于优化前版本，不将小规模 setup 对照写成优化后的完整历史门禁。该单文件测试优化集成后为 `2c64c1b`，生产源码、权威数据库资源及协议与组合验证版本一致。

完整候选的隔离安装环境为 `/tmp/camctl-video-real-recovery-v2-20261023/venv`，来源提交 `2c64c1bf330aa028f6fe901cc08307af1b8ece8e`。wheel 位于 `resume-20261023T145540Z/runtime-recovery-v2/dist/`，SHA-256 为 `4c2b8f55b67c6c3d2e84b30b17fce87b2d189e36286e7e8e341c5f6ba8b05f1d`；305份生产 Python 文件及27份权威资源共332项源码、wheel、安装字节一致。包内 Python 验收脚本按权威资源归类，没有再次计入生产模块。依赖检查及两型号静态能力导出通过，离线固件指纹复算与已保存原始读数一致。

该安装包新建状态库为 V3；复制前一阶段真实 V2 空 smoke 库后，普通 `init` 保持 V2，显式 `init --upgrade-software-recovery` 才升级为 V3，并保留有效 V2 备份、原实例、目录绑定和非元信息行摘要。重复核验通过；输入库只有两条单份状态记录，不能据此宣称非空业务历史升级已由安装版实测。原始辅助检查失败及修正分别保留在上述目录的 `runtime.json`、`commands.jsonl` 与日志中。这些是容器软件及安装证据，活动录像重启、不自动续拍和正式恢复的真机证据尚未取得。

主树在上述固定提交顺序执行普通摘要失败恢复、OSV 输入失败和录像恢复三个组件测试文件，结果分别为7 passed／1.38秒、33 passed／7.13秒、3 passed／0.80秒，均退出0；命令、UTC、源码提交和输出位于 `resume-20261023T145540Z/integrated-source-digest/`。初始化的单事务与 WAL／FULL 读回修复另在 `2dcc6f3` 核对27项初始化测试，以及相同三项清理恢复矩阵；后者实测3.90秒、161次 fsync。记录位于 `initialization-integrated-155346/`，该目录属于同一恢复阶段；不把容器测量解释为目标硬件性能。

### 已观察的设备与交付结果

| 场景目录 | 控制、文件和客户端事实 | 证据边界 |
| --- | --- | --- |
| `osmo360ii-segmented-record-auto-063507` | 唯一 START、STOP 正常；3 份 OSV 和 3 份 LRF 分别交付并由客户端导入，ACK 12、水位 65 | 预览复用修复后沿原状态库完成，没有重拍。 |
| `osmo360ii-segmented-record-manual-080632` | 包含新会话设置的安装版完成手动曝光 12 秒录像；3 份 OSV、3 份 LRF 的独立取回及客户端导入成功，ACK 11、水位 65 | 本轮完整业务链成功；媒体结论单独说明。 |
| `osmo360ii-segmented-timelapse-auto-064635` | 2 秒间隔、300 秒目标自然完成；唯一 START，无 STOP；1 份非零 OSV 和配对 LRF 交付，零字节尾部保持观察；ACK 8、水位 39 | 采样端点及预期播放帧率尚未独立核实。 |
| `osmo360ii-segmented-record-auto-082757` | 8 秒目标在唯一 START 保存后中断本次主机执行进程，等待 18 秒，再沿原状态库恢复；唯一 STOP 正常。5 份 OSV 和 5 份 LRF 保留，默认前 2 份原片及配对预览交付，ACK 10；新取回明确选择范围外 3 份原片及其预览，导入并 ACK 21 | 原执行恢复及后到取回前后冻结定义、集合、默认范围和控制记录相同；没有重拍、相机重启或删除文件。 |
| `action6-segmented-record-auto-080425` | 四项会话设置、唯一 START 和唯一 STOP 均取得正常返回；两份 MP4 及两份 LRF 文件事实已观察，但三次结果读取因文件长度响应无效而失败；原拍摄错误 13，默认原片及预览取回均为错误 21，失败报告 ACK 7、水位 25 | 原拍摄和默认范围保持失败／未知。新显式 ID 取回动作 5 单独交付两份已登记 MP4，客户端导入成功并 ACK 14；没有成功的预览交付。 |

新安装物另执行以下 Action6 场景；它们与原失败任务分别保留独立身份。

| 场景目录 | 控制、文件和客户端事实 | 证据边界 |
| --- | --- | --- |
| `action6-segmented-record-auto-094948` | 12 秒目标，唯一 START／STOP 均正常；完整结果登记 2 份 MP4 及 2 份 LRF，默认原片、预览分别取回成功，4 份文件经客户端导入并 ACK 8、水位 51 | 首次 RESULTS 的目录调用超时保留，原结果流程第二次尝试成功；没有重拍。 |
| `action6-segmented-timelapse-auto-095957` | 300秒目标、2秒间隔，唯一 START，无 STOP，自然完成；来源与原片取回成功，1份 MP4 及配对 LRF 已登记。摘要失败修复后沿原库完成旧预览失败收场并 ACK 10；新请求另取回 LRF 成功，ACK 15、水位60 | 原 copy 的源摘要仍为空、校验仍为 FAILED，原失败没有改写。初次超时、原状态备份、恢复与新交付分别保留证据。 |
| `action6-segmented-timelapse-auto-150650` | 同一批量读取安装物执行新的 `video_jpeg` 任务，300秒目标、2秒间隔；唯一 START，无 STOP，完整结果登记151张 JPEG、1份 MP4 和1份 LRF。来源、原片及预览取回均成功，304份独立交付由客户端导入，ACK 20、水位2000已保存 | 这次合法参数试验正常生成必需照片，检查数量为0，未触发 native 异常选段。它与 `095957` 保持独立身份，不替代原摘要失败的收场验证。 |

`action6-segmented-timelapse-auto-150650/cross-layer-evidence.json` 逐项核对304份已交付文件：实际长度、落盘 SHA-256、原 copy 保存的源摘要和目标摘要全部一致，共381605707字节；报告确认水位从原状态库只读核对。该次审计没有另发相机摘要命令。原执行会话结束后不再可查询，未据此补写 shell 退出码；业务、文件及客户端结果分别由原库、交付字节和客户端凭据证明。

普通录像专项 `action6-segmented-record-auto-103923` 使用新的 16 秒目标和 10 秒异常余量，在唯一 START 保存后暂挂本次 CLI 组约 35.02 秒，再恢复原组。STOP 正常，保存的 `control_elapsed_ns=36512076393` 确认进入生产异常检查；4 份 MP4 和 4 份配对 LRF 均登记保留。仅前两份原片执行检查，保存的时长分别为 9.542867、9.709700 秒，达到目标后不检查后两段。

默认两份原片及配对预览交付、客户端导入和 ACK 10 成功；后到请求明确取回范围外的输出 3、4、7、8，再独立取回已检查的输出 1、5，两次均成功，最终 ACK 24、水位 123。

两份检查输入均完整校验，原先已等待的默认原片取回从对应主机中间文件读入独立交付，预览仍从设备 LRF 读取。等待者完成后，两个检查输入均已可靠释放并清理；后到请求为已检查原片新建设备读取副本。`default-chain-audit.json` 逐行核对来源定义、集合、默认范围、结论、检查及原控制记录在新请求前后相同；8 份设备原片／预览保持可用，没有设备删除流程。原始进程、时钟、唯一控制及脚本摘要位于 `action6-overrun-harness/`；本次没有在检查中终止进程，不宣称取得检查中断冷恢复证据。

检查冷恢复专项 `action6-check-cold-112419/action6-segmented-record-auto-112419` 使用同一安装物、新的 16 秒任务及 `copy.segment_size="1 MiB"`。四条会话准备均记录为接受，START 返回 `00`；本次 CLI 暂挂约 35.024 秒后，唯一 STOP 在 11:25:06.098029 UTC 发起、11:25:12.583112 UTC 返回本地退出码 255。驱动记录 `response_missing`、响应数 0、stderr 为空，STOP 流程为 UNCONFIRMED、效果未知；原始 STOP stdout 未持久化，不能据此声称 stdout 为空。来源、原片取回、预览取回分别以错误 14、21、21 结束，失败报告已导入并 ACK 4；没有检查、副本或检查中断冷恢复。原活动仍为 UNKNOWN／HELD，原库和文件保留，没有补发 STOP、读取结果或重拍。

本次只读设备日志在 STOP 失败数分钟后取得，显示视频管线状态转换失败及 USB 通信超时；缺少关联证据，未将这些日志判为本次 STOP 失败的原因。专项控制器另有候选限制：把等待分段收尾的 ACTIVE 结果流程一并当作活动设备调用，会拒绝“结果尝试已经完整成功、第二段 READ 尚在途”的合法状态；该脚本问题与本次先发生的 STOP 失败分别记录，尚未修正。用户要求收尾并停止实施后，未继续执行设备控制或修复。

恢复实施后的控制器仅把无结果的 RUNNING 实际尝试计为在途设备调用，并保存全部活动流程作旁证；其他中断门槛保持。修正脚本、前后摘要及只读核对位于 `resume-20261023T145540Z/` 的 `camctl-action6-cold-controller.py` 和 `cold-controller-fix.json`，没有以此声称取得新的实机冷恢复证据。

同一恢复阶段另对两型号各执行一次独立的软件重启能力探针。Action6 在8.988秒后观察到新的启动标识，四项会话设置均正常返回，重启前后750条文件路径一致；OSMO 在24.798秒后观察到新的启动标识，两项设置均正常返回，前后32条路径一致。原始命令、boot ID、目录及日志分别保存在 `reboot-capability-action6-150428/` 与 `reboot-capability-osmo360ii-150824/`。这些事实只证明该次整机重启与设置恢复可执行，不能证明活动拍摄不会自动续录、文件内容完整、旧状态库责任已释放或自动恢复流程通过。

Action6 活动录像重启资格场景 `resume-20261023T145540Z/active-reboot-qualification-action6-20261023T174151860662Z` 使用上述 recovery-v1 安装包、8秒目标、唯一 START／STOP 预算及冻结的1毫秒 STOP 超时。START 成功，STOP 本地超时且效果未知；来源失败14、两项取回失败21，失败报告导入并 ACK 5、水位22。编排脚本随后暴露 ACK 参数列读取和 CLI 进程识别问题，修正后只读核对原业务结果，并在实际 CLI 和设备调用结束、精确 host 身份确认后向该 host 发送一次 SIGTERM；没有重拍或改写业务库。原编排退出6和脚本错误均保留，后补的本地收场记录不将它改记为成功执行。

本场景后续3秒元数据观察没有取得同一路径长度增长，观察命令退出10。只读启动记录显示：15:55:12 UTC 的 `boot_id` 为 `6752d4b0-561a-4df0-a0d9-f4ac38d78a59`，17:53:46 UTC 已为 `0915fdeb-0812-4400-88a8-a9bd79001c14`；按后一次的511.92秒开机时长估算，开机约在17:45:14 UTC。本场景记录中没有发送重启命令，启动原因属性及 pstore 没有给出原因，现有日志也不足以归因。设备重启原因保持未知；该次记录不构成活动录像受控重启资格，依赖该状态的 Action6 控制暂停，OSMO 的适用范围已独立完成归档。原始观察、启动读数和命令位于本场景的 `observe-runs/` 与 `read-only-no-growth-diagnosis/`。

补充只读调查保存于该场景的 `boot-cause-investigation-20261023T183733Z/`：当前启动标识没有再次变化，journal 报告没有日志文件，未找到 `last_kmsg` 或 pstore 内容。取得的内核日志首条已是开机后356.382610秒，不覆盖重启瞬间；调试接口 `/sys/kernel/debug/boot_reason/hw_boot` 仅返回与原命令行 `boot_mode` 相同的 `pwr_mode`，没有提供可判定的重启原因。

OSMO 活动录像资格场景 `resume-20261023T145540Z/active-reboot-qualification-osmo360ii-20261023T180748758987Z` 同样使用 recovery-v1、8秒目标及1毫秒 STOP 超时。唯一 START 成功，唯一 STOP 本地超时，来源失败14、两项取回失败21，失败报告 ACK 5、水位22；编排退出0，本机场景进程均已收场。同一路径3秒长度观察退出10，没有取得增长；随后扩大只读观察，在同一启动代次的约7秒内发现两份新增 OSV，设备 `cam_status_callback` 日志持续报告 `timelapse:0,recording:1`。这两种观察分别保留，没有把首轮无增长改记为通过。

在该活动状态下人工安排的唯一 `adb reboot` 返回0，24.833秒后观察到启动标识从 `0f49d15c-9045-4d9d-b793-30a9afebfee6` 变为 `7929d47f-1892-4e2d-bf70-e9f398f54001`。等待20秒后两条会话设置均正常，存储可用。后续约7秒完整目录两次均为118条、没有新增或缺失路径，资格拍摄前的32条路径全部保留；保存的10条状态回调均为 `recording:0`。重启探针前后的6条路径差异均为此次录制的 `.trinf` 或 `MET` 辅助文件，未据此判断原片损坏或完整。实际命令、原始输出及重启前后状态位于本场景的 `read-only-rolling-diagnosis-20261023T181025Z/`、`post-manual-reboot-observation-20261023T181331Z/` 和证据根的 `reboot-capability-osmo360ii-181134/`。冻结的 `firmware-reboot-qualification.json` 关联各文件摘要及带来源类型的实际固件指纹；它证明本次普通分段录像在整机重启后终止且观察期间没有自动续拍，不证明旧录像完成、原片完整或正式安装版持久化恢复已通过。

正式安装版恢复随后沿该资格场景的同一非空 V2 库执行，证据位于其 `formal-recovery-20261023T184747Z/`。recovery-v2 的显式 `init --upgrade-software-recovery` 成功；提交新计划前，将官方生成的 V2 备份与 V3 库逐表比较：40张原表中20张非空，原业务行和历史全部相同，仅允许元信息格式从2变为3，唯一新增表 `software_recoveries` 此时为空，完整性及外键核对通过。旧配置和原计划文件保持；新配置仅将后续任务的 STOP 超时改为30秒并启用已核实的同源固件指纹，原任务冻结的1毫秒预算不变。一个仅在本地导出、执行窗口不足的计划包保留为未提交，实际新请求只提交一次。

正式恢复责任1保存一次重启意图，实际工具记录为一次 `adb reboot`，启动标识从 `7929d47f-1892-4e2d-bf70-e9f398f54001` 变为 `a70ff605-4316-4fb9-a7e2-31e94bcc3346`；重新准备及一页原基准差集核实完成后进入 READY，旧活动1为 ENDED／RELEASED。原来源仍失败14、两项原取回仍失败21，原 START／STOP 流程和尝试完整保留。后续8秒录像动作5及两项取回6、7均成功，唯一新 START／STOP 各一次，2份 OSV 和2份 LRF 由原逻辑客户端导入并 ACK 19、水位82。独立设备端源 SHA、复制源摘要、目标摘要及本地字节的四方核对4／4一致，前后设备启动标识相同；原始输出位于 `independent-source-sha-20261023T190117Z/`，没有回写 `device_files.sha256`。该程序恢复验证的是手动资格观察后、设备已空闲而原库仍持有未知活动责任的情况；活动录像被重启终止及不续拍的事实由前一独立资格过程证明，不将两次重启合并为一次现场执行。

原编排会话退出2，原因是辅助校验器将旧来源的整行投影当作不可变数据：实际只有 `last_event_id` 从49到129、`change_count` 从22到49，二者按[公共历史字段](database/common.md)及权威事件登记由新增恢复历史派生。后补的只读审计依据登记核对这两列与实际历史关联一致，其余原来源字段、原流程及尝试完全相同，业务核对通过；没有重新提交或拍摄。实际执行脚本、原错误和退出2保持，修正后的校验及控制计数位于 `post-error-audit-20261023T190001Z/`。C host 的原 trace 记录 SIGPIPE 退出；后续核对原 strace PID及本场景 host、CLI、ADB、后代均已结束，没有补造正常退出或未取得的 Popen 回收结果。

恢复后的本地媒体报告位于 `resume-20261023T145540Z/media-osmo-formal-recovery-20261023T185533Z/`。默认与全部保留范围均为上述两份 OSV，两路主视频各累计精确 `122122/15` 毫秒，约8141.467毫秒，8秒目标的短缺为0。完整探测及逐流解码均退出0，保留8项诊断，段间连续性仍未评估。实际 helper 的 PID、命令、UTC、退出码和前后摘要保存在同名 `.command.json`。六个 OSMO 场景的最终一致状态库副本、请求、报告确认、媒体和独立源摘要索引位于 `resume-20261023T145540Z/osmo-p9-final-audit-20261023T190759Z/`；其中单文件场景按不适用范围冻结，未将其未知占用改写为已释放。

Action6 在首次设置时，工具退出 0，却在 stdout 明确报告 `server not exist`。相机进程和画面管线日志显示异常；一次相机服务重启请求后仍无法设置。随后整机重启，取得新的 boot ID，四条设置均返回服务连接及注册成功；后续 12 秒录像的控制命令没有再次出现 `-1002`。这证明上述执行顺序后的观察结果，不把服务异常解释为此前 STOP 失败的已证实原因。原始设置、服务及重启记录保存在 `session-settings-device/`。

Action6 的结果读取问题已在只读扫描第 178 个 metadata 调用复现：旧 DNG 文件的长度命令退出 0、stdout 和 stderr 均为空。容器原 ADB 34.0.4 的标准输入选项对照、官方 ADB 37.0.1 客户端、shell 变量收集后打印、独立版本帧调用均有单独归档；这些对照仍分别出现空响应、退出 255 或超时。后续采用上述页内批量接口，已完成新安装物的真实普通录像登记与交付。新版客户端来自 [Android 官方发布入口](https://developer.android.com/tools/releases/platform-tools)，只用于独立探针，没有替换任务使用的客户端。逐文件查询空响应的原因尚未确定。读取器继续拒绝空长度，不将错误当作空目录、完整扫描或默认范围。已终态的原任务没有重新开放、重拍或补发控制命令。

早期 OSMO 单文件原生延时任务 `osmo360ii-single_file-timelapse-auto-065733` 的来源因不符合必要产物合同而失败，原库仍保存 `UNKNOWN_HELD` 活动责任。当前驱动无活动查询，公开取消入口保持来源终态且不会为该旧责任新发 STOP；残留处理也没有能确认该未知活动归属的查询。该旧库保留原失败和文件，不用后续独立场景的 STOP 改写它。当前固件的单文件真机矩阵不适用，旧库按该范围冻结归档；旧责任保持原状态。适用机型的自动恢复已按[无人值守软件重启规则](../architecture/camera-recovery.md#无人值守的软件重启恢复)实现并取得上述软件、安装及独立实机恢复证据；原冻结库没有执行该恢复，不将其记为责任已释放。

原生延时预览最初的状态交接缺口为：源摘要超时先保存 copy 校验 FAILED，完整读取收尾随后返回 `COMPLETION_FAILED`，读取流程将其作为未完成前提抛出 `state_db_error`，原 READ 尝试及动作没有终态。该普通交付路径已按[源摘要失败收场规则](../architecture/file-copy.md#实际读取结束与所属结果)修复并在上述阶段安装包复核。真实恢复前先备份原库、冻结定义和完整失败副本；旧本机拥有者结束后，正式 C host 沿原状态库启动无新计划的 `run`，使用原 READ 尝试续接结束确认。尝试4保存为 SUCCEEDED，流程4以 `source_file_unconfirmed` 结束，旧预览动作失败21，失败报告导入并 ACK 10；没有发送取消。

随后新请求明确选择原输出2，建立动作5及独立交付3，取回4195828字节 LRF 并成功导入客户端，ACK 15、水位60。新源摘要、目标摘要及实际落盘摘要均为 `18a284a329a2641008ba5b4fa40f6468b77389899c654fd21a8ee2389c1899bf`；旧 copy 2仍为校验 FAILED、源摘要为空，校验错误、轮次及已提交字节均未改变。原前三个动作的冻结定义、原 READ 累计次数和唯一拍摄控制也逐项保持。恢复阶段的实际工具记录没有 SHA 调用，新请求有一次 SHA 调用；两阶段均没有新增拍摄控制。恢复读取沿原偏移打开，底层 `dd` 按65536字节块对齐，并由驱动跳过块内余数，不把该设备命令写成按字节精确定位。

原状态与失败副本位于 `resume-20261023T145540Z/digest-failure-before-recovery/`；恢复脚本、原始日志及合并核对位于该阶段的 `resume-old-preview-*`、`new-preview-after-digest-failure.log` 和 `source-digest-real-recovery-audit.json`。新请求、客户端凭据和独立工具记录位于原场景的 `preview-after-digest-failure/`。两次编排命令均退出0；辅助脚本等待 host 关闭超时的观察保留，后续只读进程检查没有发现带本场景配置的 host 或 CLI，不据此补写未取得的 host 正常退出码。当前阶段只证明普通交付收场及独立新取回，分段检查内部消费者另行验证。

无人值守部署已确认没有逐台硬件复位或供电控制，ADB 软件重启作为受支持驱动的恢复兜底。独立恢复责任、相关持久化和 V3 显式升级已取得上述软件及安装证据，OSMO 普通录像的活动重启资格及独立的正式恢复、放行和后续业务链均有上述实机记录；Action6 对应资格和恢复尚未取得实机结论。各原库的活动状态、原失败和原文件继续按各自真实事实保存。此前 Action6 诊断中的人工整机重启及重连设置记录，不替代本项无人值守流程或 OSMO 机型的验收。

### 媒体与独立源摘要

相机端另行执行摘要命令取得上述五场景的 26 份已交付源文件 SHA-256，逐项与读取、交付和本地副本核对，26／26 一致。原始输出和对照位于 `independent-source-digests/`；不依赖 `device_files.sha256` 是否已填写来声称取得设备端独立摘要。

批量读取安装物的新 Action6 两场景另核对6份文件：独立设备端 SHA-256 与全部6份本地副本、数据库 target 摘要一致；其中普通录像4份和 native MP4 的 copy 已可靠保存源摘要。native LRF 的原 copy 源摘要仍为空、校验仍为 FAILED，后续独立查询和新交付均没有更新这次失败。`action6-batch-v1-six-file-comparison.json` 和两份 `*-cross-layer-evidence.json` 保留初次核对；上述恢复阶段补齐原片客户端导入、旧失败 ACK 和新预览交付 ACK，版本边界分别记录。

OSMO 普通自动与手动两轮的 P7 报告分别位于 `media-osmo-auto-v2/`、`media-osmo-manual-v2/`。目标均为 12000 毫秒，默认短缺容限 100 毫秒；默认范围和全部保留集合的两路主视频分别累计约 12345.667、12145.467 毫秒，短缺均为 0。自动轮首个非尾段的两路主视频各为 165 帧、5505.5 毫秒，帧率均为 `30000/1001`；保留其比参考范围多 5.5 毫秒的观察。逐段判定采用[设备分段契约](../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md#设备分段与命名契约)，该偏长不单独构成失败；本次规则调整未重跑媒体工具或改写原始报告。手动轮前两非尾段约 5438.767、5005 毫秒。全部主视频及音频逐流解码退出 0，timescale、帧率估计及私有流 codec 诊断均保留；段间连续性未由聚合时长证明。

OSMO 原生延时的 `media-osmo-native-v2/` 报告测得两路各150帧、5005毫秒；原报告继续保留。新增工具按[独立帧数验收规则](../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md#原生延时的采样数量验收)从原任务目标300000毫秒及间隔2000毫秒独立推导参考范围150～151帧，默认短缺容差3帧，最低147帧。真实媒体复核结果位于 `media-osmo-native-v3-frames/osmo-p7-report.json`：默认交付与全部保留范围各自的两路主视频均为150帧，逐路数量通过，源身份匹配后源数量结论也通过。缺少独立采样端点和预期帧率依据，预期播放时长仍为未知，段间连续性仍未评估；数量通过不代表这些独立事实已核实。

该扩展的单元验证为66 passed、0.07秒；集成验证为32 passed、59.69秒，包含真实媒体工具及既有大清单内存场景，均退出0。完整命令、工作目录和退出码在 `.local/worktrees/real-device-frames-20261023/.local/real-device-frames-acceptance/` 下的 `unit-final-recheck.run`、`integration-final-recheck.run` 及对应日志；候选提交 `d1b8812ec2d05ad08fbac03767004bf6561ea5b0` 经独立审查后集成，未改 camctl 运行依赖。

真实媒体复核使用 `.local/verification/osmo-directory-20261023/media-venv/bin/python` 调用该候选的 `docs/hardware/validate_osmo_segments.py`，读取原 `media-osmo-native-v2/osmo-p7-manifest.json`，参数为 `--shortfall-tolerance-ms 100 --frame-count-shortfall-tolerance 3 --batch-size 64`。UTC 15:12:50 至15:14:04运行74.31秒，退出0；完整 argv、cwd、工具源码 SHA-256 和退出结果保存在新报告同目录的 `command.json`，stdout、stderr分别保存，未覆盖旧报告或媒体。

OSMO 异常多录的 `media-osmo-overrun-v2/` 报告分别测量全部 5 段和默认 2 段。两路全部保留集合各约 24991.633 毫秒，默认范围各约 10443.767 毫秒，8 秒目标的短缺均为 0；范围外原片不参与默认覆盖。`scope-comparison.json` 核对主机恢复、后到的明确 ID 取回前后默认范围及唯一控制不变。完整视频和音频解码均退出 0，保留与正常轮相同类别的诊断，段间连续性仍未单独证明。

Action6 的 `media-action6-record-explicit-v2/` 报告只分析两份显式交付原片：主视频约 10844.167 毫秒／325 帧、1801.8 毫秒／54 帧，完整解码均退出 0 并保留 timescale 诊断。其和为已取回文件的实测总和，不能代替失败来源的完整扫描或默认范围结论；音频未单独枚举，不能据此断言不存在。

Action6 新普通录像的 `media-action6-normal-batch-v1/` 记录主视频约 10443.767 毫秒／313 帧和 2135.467 毫秒／64 帧；主视频累计精确为 `377377/30` 毫秒，约 12579.233 毫秒。音频分别约 10432、2133.333 毫秒；两份 MP4 完整解码退出 0，保留 timescale 诊断。

新原生延时的 `media-action6-native-batch-v1/` 对已发布 MP4 独立计数：唯一 HEVC 主视频 151 帧、约 5038.367 毫秒，符合冻结目标 `floor(300/2)+1=151`；附带 MJPEG 封面排除计数，未发现音频流。完整主视频解码退出 0，并保留 timescale 诊断。上述正常链媒体测量不能代替异常恢复的生产选段及检查副本责任证据。

Action6 正常 `300/2, outputs=video` 在完整自然结果上不进入生产帧检查；START 未知、结果有限耗尽和取消也不构成该入口。仅完整自然集合上的真实必需照片缺失、视频缺口等异常会进入恢复检查，当前没有可保证触发它的既有主机中断安排。151 帧的正常原片测量已取得，native 异常选段仍未验。

Action6 异常专项的 `media-action6-overrun-batch-v1/` 分别分析固定默认前缀和全部保留集合。默认两份主视频的精确时长为 `143143/15`、`97097/10` 毫秒，累计 `577577/30` 毫秒，约 19252.567 毫秒；第一份单独不足 16000 毫秒，最短达标前缀确为两份。四份唯一原片共 `359359/10` 毫秒，约 35935.9 毫秒、1077 帧；预览和重复交付不参与该累计。生产检查保存的六位小数秒数累计值与独立时间基计算相差约 0.333 微秒，未改变本次选段。8 份唯一原片／预览均有独立源摘要，全部 10 份交付的源摘要、目标摘要、落盘长度与 SHA-256 一致；两份后到重复交付分别与对应首份字节相同。每份文件均探测到唯一主视频、附带 MJPEG 封面及 AAC 音轨，8 份完整解码均退出 0，保留 timescale 诊断；没有据累计时长推断段间无缝或独立播放质量。

## OSMO 分段目录命名的软件验证

OSMO 目录、分段解析及错误位置使用[命名契约](../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md#设备分段与命名契约)中的 `CAM_001`、`CAM_002`。相关组件、安装版联合链和媒体验收清单使用同一目录形式；文件名中的 datetime 保持十四位。目录内索引与跨目录数值顺序、完整主干配对及独立产物身份遵守原契约。

命令记录的执行日期为 2026-10-22 UTC。环境为 Linux x86_64、Python 3.11.16、SQLite 3.53.1、Node.js 24.20.0 和 pnpm 12.3.4。原始命令、工作目录、解释器、UTC 起止、日志及退出码保存在 `.local/verification/osmo-directory-20261023/`；组件集成按目录分别启动进程并顺序执行。

| 验证范围 | 实际结果 |
| --- | --- |
| 解析失败复现及路径单元测试 | 跨目录排序和精确配对两项先因目录无法识别而失败；修正后两个相关文件共 44 passed，退出 0。 |
| 完整 camctl 单元测试 | 6977 passed、1 skipped、2 条既有 asyncio 标记警告，退出 0。 |
| 拍摄及恢复组合 | 普通录像范围恢复与原生分段完成两个文件共 30 passed，退出 0。 |
| 分段持久化组合 | 完成事务、结果结束决定、批次、存储及结果集合五个文件共 297 passed，退出 0。 |
| 分段历史恢复组合 | 两个文件共 39 passed，退出 0。 |
| 默认范围与取回组合 | 10 passed，退出 0。 |
| OSMO 装配恢复组合 | 22 passed，退出 0。 |
| 媒体验收工具首轮 | 单元测试 52 passed；集成测试 18 failed、4 passed，原环境缺少工具专用依赖 `ijson`，原始输出保留。 |
| 独立媒体验收工具环境 | 按工具登记准备 Python 3.11.16、`ijson==3.6.0` 和 pytest 9.1.1；单元 52 passed、集成 22 passed，两个进程均退出 0，原环境和失败日志保持。 |
| 完整客户端单元测试 | 65 个文件、3197 项通过；缺口目录位置显示先取得一项失败复现，再完成修正。 |
| 客户端构建与分段页面 | 构建退出 0，保留 chunk 大小提示；实际浏览器的分段结果文件 5 项通过，退出 0。 |
| 安装版普通录像分段联合链 | 8 passed，631.99 秒，退出 0。 |
| 安装版原生延时联合链 | 4 passed，155.92 秒，退出 0。 |
| 分段错误报告与历史重建 | 2 passed，30.31 秒，退出 0；覆盖缺口详情、保留原片、默认前缀及清理后的同边界原字节重建。 |
| 历史实机路径离线复核 | 使用既有自动录像归档中的两份 OSV 及对应 LRF，四条实际路径均取得正确目录号、索引及后缀。 |

独立只读审查未发现阻塞项。收尾检查通过 5264 个本地文件链接，新增引用锚点已按实际标题核对，`git diff --check` 通过。本节提供软件及历史路径离线证据，不表示重新完成 ARM 部署、相机拍摄或现场媒体核验；设备验收继续按[分段计划 P9](../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md#p9按独立证据层次完成容器与实机验收)取得各自证据。

## OSMO 分段生产接入与联合验收（2026-10-22 UTC）

拍摄与范围取回接入提交为 `dc2db5d`。对应[实施计划 P4—P8](../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md#当前执行位置与继续顺序)。环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1、Node.js 24.20.0。完成模块按冻结的任务结果策略分派；Action6 保持单原片，OSMO 的单原片任务另由安装版清理链验证。下列记录按执行顺序保留各轮失败、修复及最终完整回归结果；不同阶段不拼为同一次完整运行，当前阶段由正式计划维护。

原始命令、工作目录、解释器、UTC 起止、退出码及日志保存在 `.local/verification/osmo-segmentation-20261022/` 的同名 JSON／log。复现入口为本计划的[验证命令](../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md#执行时使用的验证命令)与[跨组件测试说明](../../tests/integration/README.md#osmo-360-ii-正式内置合同安装版联合链)。

| 已执行范围 | 结果与证据前缀 |
| --- | --- |
| 普通分段结果、当前扫描与跨轮保留、有限决定 | `capture-current-recheck` 14 项、`capture-current-gaps` 38 项、`capture-conclusions` 14 项通过。 |
| 普通取消与原生空尾 | `capture-cancel2` 17 项、`native-completion3` 4 项通过；START 失败及未启动取消 `start-failure` 6 项通过。 |
| 占用与计划／组等待 | `release-recovery` 1 项、`group-scopes2` 2 项通过；释放回执丢失沿原申请核实，同目录下一来源可继续执行。 |
| 显式未确认条目 QUERY v2 | `query-recovery` 7 项通过，含原结果与业务保存回执未知、冷恢复、有限预算和单原片隔离。 |
| 历史报告 | `reporting-segments` 9 项通过，含缺口完整详情及清理、摘要变化后的同边界原字节重建；最终完整 `reporting-repaired-161745` 435 项通过。 |
| 单元汇总 | 后续恢复接线修正后的当前源码完整复验 `unit-software-final-192326` 为 6977 passed、1 skipped、2 条既有警告，45.06 秒、退出 0。跳过项仅验证非 Linux 平台的父进程守护无操作行为，在本次 Linux 环境不适用。 |
| 客户端完整验证 | `client-unit` 65 文件／3197 项、`client-integration` 40 文件／1566 项通过；`client-typecheck` 和 `client-build` 退出 0。构建保留原有大块体积提示。18 项直接依赖与已锁定版本一致，锁文件未修改。 |
| 安装版分段矩阵 | 初轮 `installed-segments` 为 4 passed、2 failed；后续 `installed-segments-final-150140` 为 5 passed、1 failed。跨轮配对身份修正后增加少于默认选段数及非尾零原片分区，最终 `installed-segments-final-163749` 为 8 passed，633.01 秒、退出 0。 |
| 完整 outputs | `component-outputs-isolated-143747` 2487 passed、2 skipped，退出 0。两项跳过分别是内部副本不发布普通 delivery、主机源不携带设备读取配置；未缺少工具。 |
| 规格与静态检查 | `database-final` 39 表／3803 项断言；`history-sql-final`、`event-final`、`dependencies-final`、`protocol-final`、`config-final` 均退出 0。 |

完整 capture 初轮 `component-capture-141545` 为 2040 passed、8 failed。8 项均因原始页查询同时计入文件索引事件；查询限定到权威 `APPEND` 分支后，完整时间资格文件及分段处理器文件 `capture-last-boundaries` 共 136 项通过，包含跨目录、首位置只有 LRF 和顺序未知的三个新增组合。原有实际时刻、文件完成和调用次数断言保留。完整 outputs 在独立工作区 `/tmp/camctl-osmo-verify-20261022`、提交 `d4e9be0` 上通过；完整 capture 的最终通过结果在 `ab1820e` 上取得，包资源与当前工作区分别同步。

outputs 初轮 `outputs-final` 为 2449 passed、35 failed、2 skipped；35 项来自三个事件序列夹具缺少独立媒体读取范围。补齐真实读取范围后，相关 118 项及上述完整目录通过。reporting 初轮为 433 passed、2 failed；测试替身补全真实分页历史端口并从实际生成函数传入读取器后，错误分类专项 3 项及上述完整目录通过，状态错误、停止发布和库／文件不变的断言保留。

bootstrap 初轮发现失败后主动中断，`component-bootstrap-145433` 为 213 passed、1 failed，退出 2。恢复夹具补全正式 `capture_segment_writes` 集合，并同步另一处共同恢复夹具；恢复及报告错误流程三个完整文件 `bootstrap-recovery-fixtures` 共 4 项通过。完整 bootstrap 仍待重跑；其余组件在独立工作区 `/tmp/camctl-osmo-components-20261022`、提交 `b776301` 上逐目录运行。

完整依赖回归发现报告层引用拍摄／设备模块、拍摄预分类直接依赖 SQLite。共享有限值和错误头移至 `contracts`，临时排序索引移至持久化适配器；未放宽依赖检查。修正后 `contracts-repaired-161619` 为 442 passed，`devices-repaired-161714` 为 220 passed，`reporting-repaired-161745` 为 435 passed。`unit-architecture-final-151618` 为 6977 passed、1 skipped，保留两条既有警告。

跨轮配对查重原先把 `AttemptTicket.attempt_id` 的尝试序号直接与配对事实中的实际尝试行 ID 比较，导致第二轮已保存事实无法复用。修正为按原 run 和尝试序号定位实际行；`capture-pairing-repeat-151747` 完整文件 25 passed，覆盖两轮行 ID 与序号不同、配对及正式产物不重复、唯一 STOP。

历史全库独立预期补齐结果页索引、基准调用、分段集合及成员和配对事实的归属，并登记实际 `BASELINE_READ` 验证入口；内部读取场景继续逐行比较动作自身完整成员。`history-oracle-repaired2-162140` 完整文件 21 passed；完整 history 目录另行复验。

`ab1820e` 的完整 persistence 回归在提交路径审计处为 114 passed、1 failed，临时索引连接不能置于正式仓储目录。连接责任移出后，`persistence-connection-audit-163307` 完整文件 55 passed；随后模块依赖检查发现业务层不能直接调用持久化内部适配器。最终仓储保留有序读取端口，`persistence/segment_order.py` 只管理临时索引的连接与清理，业务层继续消费仓储。正式状态库连接与提交规则不变；最终结构的 `persistence-adapter-final-163633` 为 55 passed，`contracts-adapter-final-163702` 为 442 passed。

bootstrap 的首次结果页尚未返回即中断场景暴露分段入口错误要求已有原页；普通和原生完成模块现保留无观察、当前集合未知及原有限预算，不能把无页解释为已确认空集合。`bootstrap-osmo-recovery5-162857` 完整文件 22 passed，覆盖原页复用、无页／部分页恢复、原预算及原尝试；分段场景使用合法 CAM 名称，内部媒体读取场景显式冻结单原片任务。`native-first-page-recovery-163013` 完整文件 5 passed，包含原生首次页前中断冷恢复，未重新 START 或 RESULTS。

容器重启中断了 `component-capture-final-145524`、`component-bootstrap-final-150123` 和 `component-persistence-isolated-151822`；这些日志没有最终退出码，不计为通过。bootstrap 和 persistence 在中断前已有失败，后续分别定位并复验。其他独立目录已有完整退出 0 的结果：acceptance 338 项、cancellation 396 项、host_files 120 项、logging_runtime 112 项（保留一条既有标记警告）、motor 12 项及 operations 314 项；原命令和提交保留在 `isolated-components-results.json`。history 的旧归属断言和事件覆盖表待补齐后复验。

完整 capture 的 `capture-candidate-ab1820e-162339` 为 2052 passed，1323.68 秒、退出 0。其后无首张结果页的修正由原生 5 项及 OSMO 装配 22 项补充核对；完整组件不以这些局部结果拼接成一次运行。

完整 bootstrap 在 `e87ae61` 上的首轮为 4 passed、1 failed：共享夹具误将 Action6 冻结定义也断言为 OSMO 分段策略。夹具分别声明 OSMO OSV 与 Action6 MP4 路径，Action6 继续断言无分段策略；未修改 Action6 生产行为。修正后的两个完整文件 `bootstrap-shared-policy-fixed-164916` 为 28 passed。完整 history 首轮 `history-candidate-ab1820e-162341` 为 343 passed、1 failed：三路径预期全局剔除了 `created_event_id`，但该列在结果页索引等表中是业务事实。改为按权威表登记保留完整业务列，`history-replay-columns-fixed-164916` 完整文件 3 passed；两目录分别完整复验。

P8 的其余完整组件目录、既有 OSMO、单原片清理及 Action6 安装链继续按实际结果收尾，未据局部通过勾选完整阶段。所有设备输入均为受真实契约约束的替身；本次没有 ARM 部署、相机控制或现场媒体验收，P9 待用户另行安排。

后续完整 persistence 的 `persistence-complete-final-164931` 为 318 passed、1 failed。耗尽配置事务夹具原先提供明确失败的 RESULTS，却申请未知结果耗尽；改为未完成且没有明确调用错误的原页，继续覆盖配置采纳、流程关闭、独立守卫、原键、真实提交前后故障和历史三路径。完整文件 `persistence-exhaustion-fixture-fixed-165544` 为 36 passed；完整目录继续复验。

P5 的 `segment-scope-cleanup-cold-165720` 完整文件为 10 passed、233.45 秒、退出 0。六份原片及六份预览先由正式拍摄事务发布；原取回范围固定后，公开清理事务只限制第二份原片，再保存实际文件缺席观察并关闭／重开状态库。原选择及原键回执不变；新的默认选择仍为原前三段，第二段不可用，不以第四段补位，三个预览仍独立可选，范围外明确 ID 保持精确。按来源清理固定全部 12 份正式产物，不受默认前三段限制。此项验证选择及文件事实消费，没有执行设备删除。

既有 OSMO 安装链初轮 `installed-osmo-existing-164823` 为 2 passed、2 failed，旧预期把跨会话只有一份尾段的普通录像当作成功，并要求 LRF 在原页内已经配对。实际报告为 `capture_result_unconfirmed/completion_unknown`，保留两份合格产物；配对由完整扫描的实际配对事实提供。测试分别核对原失败、有限预算耗尽、每轮原页、配对事实身份及后续取回，不改变生产判定。最终 `installed-osmo-existing-budget-final-170015` 完整模块为 4 passed、156.62 秒、退出 0，含源码／安装版能力和资源一致性。中间两次预期修订失败日志继续保留；此模块也归档实际状态库与客户端凭据。

后续完整 history 的 `history-complete-final-165029` 为 344 passed，1060.75 秒、退出 0；安装版单原片清理链 `installed-single-original-cleanup-final-170335` 为 11 passed，576.29 秒、退出 0。完整 bootstrap 的 `bootstrap-complete-final-165029` 为 1180 passed、1 failed、1 skipped，配置变化恢复场景在逆向移除 `result_page_files` 时发现种子缺行。完整 persistence 的 `persistence-complete-delivery-170335` 为 769 passed、1 failed，启动前失败场景的集合数量断言不符。两项失败分别核对完整实体种子及无 RESULTS 来源发布契约，尚未计为通过；串行队列因此未运行 scheduling 和 session。

配置变化测试现从实际事件涉及的全部当前行建立逆向种子，并按各表权威登记保留业务列；四表裁剪仅用于本场景的业务比较。`bootstrap-projection-seed-fixed-181544` 完整文件为 4 passed。启动失败测试按既定无 RESULTS 发布契约核对唯一零成员来源、实际发布标记、无正式产物、无 RESULTS 流程及唯一 START 调用；`persistence-start-contract-fixed-181636` 为 1 passed。两项修正均为测试预期维护，生产行为保持原契约，完整目录另行验证。

在提交 `5d87469` 上，`scheduling-complete-final-181848` 为 136 passed，`session-complete-final-181859` 为 194 passed，均为完整目录且退出 0。`installed-action6-regression-final-181751` 的普通录像及原生有限延时两个安装版联合用例均通过，78.95 秒、退出 0；分别经实际 wheel、C host、CLI、持久化、取回、客户端导入和 ACK，继续使用各自原有计时与单原片规则。bootstrap、persistence 的完整复验使用两个独立工作区，实际命令及退出结果继续保留。

`5d87469` 在 `/tmp/camctl-osmo-verify-20261022` 上的完整 persistence 复验 `persistence-complete-source-fixed-181750` 为 860 passed，1234.17 秒、退出 0；包含 20／200／400 文件的固定批次写入、核实和内存增长检查。同提交在 `/tmp/camctl-osmo-components-20261022` 上的完整 bootstrap 复验 `bootstrap-complete-seed-fixed-181749` 为 1340 passed、1 skipped，1490.71 秒、退出 0；跳过项为 Windows 专用的大小写路径检查，在本次 Linux 环境不适用，未缺少工具。

按 camctl 测试指南的权威目录清单核对，16 个组件集成目录均有分别完整运行、退出 0 的记录。上述单元、客户端、静态检查、安装版分段八场景、既有 OSMO、单原片清理链及 Action6 安装链共同提供本次软件汇总证据；各阶段保留自己的提交、工作目录和实际范围，后续修复由对应的完整目录或受影响文件复验。软件设备替身不提供 ARM、相机固件或现场媒体结论。

发行物收尾审计 `release-wheel-final-192125` 核对实际安装链归档的 `tmp/osmo-installed-chain-archive-81825/dist/camctl-0.1.0-py3-none-any.whl`：290 份生产 Python 文件与当前源码逐字节一致，27 份资源与唯一权威映射中的仓库来源逐字节一致，且无额外资源或生产 Python 文件。wheel 的 SHA-256 为 `176e3418a4ff02ca58f5de365039af269b6d6967e25d477fbcf24b3394f27bd9`。包资源只读同步检查退出 0；现场计划生成入口及独立媒体验收工具部署入口由[演示说明](../hardware/camera-demo-validation.md#osmo-正式计划与分段验收)维护。

文档收尾检查 `docs-software-final-192326` 核对 5261 个本地文件链接，退出 0；该脚本不校验标题锚点。本节、正式计划与演示说明之间的引用锚点已按实际标题逐项核对，`git diff --check` 通过。

## OSMO 分段原页、事务接缝与文件证明（2026-10-22 UTC）

按[分段实施计划](../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md)继续软件实现。环境为 Linux x86_64、Python 3.11.16、SQLite 3.53.1。主线已接入原始设备结果页、逐页文件索引及其历史核实，并将结果页、来源解析和原分段完成的事务读取范围及封口共同保留。文件归属提交为 `db24903`；批内原页证明复用提交为 `cd80f4f`。

日志保存在 `.local/verification/osmo-resume-20261022/`。下表是分阶段的实际执行范围，各组件集成目录分别启动进程；没有把多次运行拼作一次完整目录通过。

| 验证范围 | 实际结果 |
| --- | --- |
| 原页索引及原键、有限／有界事务反例 | 146 passed，退出 0。 |
| 原页历史、来源、报告对象选择 | 分别为 21、12、43 passed，退出 0；这些是共享事务接缝合成阶段的结果。 |
| 分段保存与分页 | 89 passed，退出 0，包含批内复用及跨读取重新检查的新增反例。 |
| 原有分段完成事务 | 其余 103 passed、1 deselected；单独的 20／200／400 文件增长测试 1 passed。两次均退出 0，内存与读取量断言保留。 |
| 原页文件归属 | 74 passed，退出 0，覆盖原始未配对 LRF、原键核实、实际 SQL 证明和两个事务入口。 |
| 严格文件证明读取 | 13 passed，退出 0，覆盖停止／自然等待、零字节、未配对预览、缺少证明、损坏引用、历史边界、数据库读取错误和文件自身历史查询索引。 |
| 完整扫描配对及文件事实组合 | 120 passed，退出 0，合并执行配对、原页归属和严格证明三个完整文件；后增两项跨轮首次关联与冲突测试各自通过。最终完整配对文件为 35 passed，退出 0。 |
| 严格成员及候选组合 | 原页索引、配对／成员、分段保存三个文件在排除三项单列增长测试后，287 passed、3 deselected，退出 0；原键新连接及损坏配对引用反例另为 2 passed，退出 0。此前文件归属、严格证明及分段组合为 213 passed，退出 0。 |
| 严格成员接入后的事务与拍摄回归 | 分段完成事务 103 passed、1 deselected，退出 0；结果页历史、文件观察及停止后文件证明三个文件 94 passed，退出 0。日志为 `b24-terminal-regression.log`、`b24-capture-regression.log`。 |
| 既有文件观察与停止后完成 | 60 passed，退出 0。 |
| 完整 unit | 6903 passed、1 skipped、2 条既有 asyncio 标记警告，退出 0。 |
| 结构、事件、报告依赖、公共协议、包资源及文档文件链接 | 各阶段对应检查通过；严格成员接入后结构为 37 张表、3599 项断言，事件登记集成 58 passed。未据此声明标题锚点已全量检查。 |

组合初轮发现结果页关系读取使用 `fetchall`，使增长测试失败；修复后用有限批次及额外一行检查保持完整性。随后一次增长复测运行 1561.54 秒后由本任务中断，退出 2，未记为通过。新增批内复用后，同一增长测试在 352.92 秒内通过；该记录不推导 ARM 性能。

完整扫描配对覆盖跨页、缺件、歧义、零大小、未知元信息、有限／有界事务、原键精确输入、COMMIT 前后回执丢失、新连接核实，以及文件自身历史和快照恢复。索引专项在 20／200／2000 条无关目录样本下核对实际查询计划与 SQLite 指令步数，证明族计数查询使用前缀索引且不随这些无关条目增长；该专项不代表整体运行链或目标硬件性能。相关日志为 `b23-persistence-regression.log`、`b23-final.log`、`b23-unit.log` 和 `b23-event-registry.log`。初轮快照测试漏准备动作快照、查询量断言未区分首次编译成本，修正测试夹具与增长判定后保留原失败日志。

独立评审指出逐预览回读全部页及动作历史的两条增长路径。消费者现使用已提交 A1 事实的有限证明，原 APPEND 保存继续保留完整 F 前重算；基准与事务类型查询分别使用对应索引。真实增长测试固定首批两文件族，再保存 20／200／2000 页无关文件：有限入口的保存、独立守卫及原键核实始终为 1319 次读取查询、约 33700 个 SQLite 指令步；有界入口始终为 1319 次、约 33600 步。固定基准消费者分别为 93 条语句、约 2300／2600／2400 步。三项实际业务测试均退出 0，日志为 `b24-actual-growth-2.log`、`b24-bounded-growth.log`；这些数字只代表本地测试的固定族，不代表 ARM 性能。类型索引引入后曾使事务查询误选索引，真实增长测试检出后通过事务／类型组合索引修正，原失败日志保留。

严格成员保存、读取及候选共同核固定文件的完成与配对事实；未取得证明的裸 COMPLETE 文件仍保持未知，名称没有 CAM 顺序键不妨碍可靠完整主干配对。原键复用同时核实际成员、原完整输入及固定引用，读取错误不作为缺键。实际正常／异常收场、默认范围、完整报告生产和安装版分段联合链继续由 P4—P6、P8 完成，不能把本节记为这些阶段已经通过。ARM 部署、真机拍摄及现场媒体验收待用户另行安排。

## 基准目录调用意图输入

候选 `0a23500` 增加纯类型 `BaselineReadIntent`，沿实际 DirectoryRequest 契约规范化并固定绑定、完整目录、原游标、有限正 Decimal 超时和固定批次；同活动 COLLECTING 前缀、首次／后续输入和两份规范操作键分别校验。输入类不访问数据库、目录端口或设备，也不以调用意图证明已派发。

新增 47 项单元测试先因类型缺失有效失败，实现后全部通过；完整 unit 为 6973 passed、1 skipped、2 条既有 warning，退出 0。日志为 `.local/verification/osmo-resume-20261022/a1p-intent-model-red.log`、`a1p-intent-model-green.log`、`a1p-intent-unit.log`。独立限定复核未发现可证实的问题或不必要限制。

这些证据只覆盖输入类型。基准调用表、原意图／结果保存、同 H 读取、实际调用拥有者及共同处理器尚未接入；数据库归属、连续序号、上一实际返回及跨记录双键唯一性不能由输入类自证。

## 基准调用首票意图持久化（2026-10-22 UTC）

在`7f4c98b`后增加baseline_read_calls结构与事件登记，以及首票INTEND的真实保存、原键精确复用和完整边界读取。原输入、原始双键和精确超时由动作历史持有；有限和有界事务从原写连接独立核对活动、设备及完整目录，事件和上下文一起自报其他绑定仍会回滚。真实动作快照包含调用行，后来固定基准不改变原票及原H的读取。

Python 3.11.16、SQLite 3.53.1 下，新增首票测试及事务路径审计共67项通过；原基准历史和准备回归26项通过；完整operations目录314项、完整unit目录6973项通过，unit保留1项跳过及2条既有warning。原始日志与命令、HEAD、环境、UTC和退出码位于`.local/verification/osmo-resume-20261022/a1p-intent-store-*.log`及同名JSON。最初7项因保存入口缺失有效失败；中间登记形状、测试目录范围和快照断言修正前的失败日志继续保留。

结构检查38表3670项断言、事件登记37类128分支、报告依赖、公共协议、资源同源、文档链接和空白检查通过。这些结果只覆盖首票输入持久化；实际RETURN/UNCONFIRMED、后续页及分页发现、原申请独立核实、实际Task拥有者和共同处理器仍待实现。未执行ARM、设备或媒体验收，不勾选P4及安装版联合链完成。

## 基准目录实际返回与连续页（2026-10-22 UTC）

在`96aeb82`后增加冻结的BaselineReadReturn和真实保存入口。非空成功页复用原APPEND形成逻辑，与RETURN同事务、同返回时刻保存；空页保存实际下一游标，真实错误保存原ErrorValue。后续INTEND只沿上一实际成功返回继续，前缀计数从上一原输入及实际批次派生。原结果键复用与原H读取保持原内容，后来调用和FIX不改写已有返回。

新有限范围由既有事务内核绑定并封口，两种入口均核实际前序APPEND和唯一末RETURN。持久化新文件与事务路径审计92项通过；基准准备、历史及原始结果页回归47项通过，operations目录314项通过。完整unit最终单独运行6975项通过，保留1项跳过与2条既有warning。原始命令、UTC和退出码见`.local/verification/osmo-resume-20261022/a1p-return-store-*.json`及同名日志；初始6项有效RED、时刻不一致的2项有效RED及有界读取未完成事务的中间失败均保留。两次早期unit与资源同步有重叠，其后使用`a1p-return-store-unit-isolated.log`作为最终单元证据。

结构检查38表3671项断言及事件登记通过；本阶段没有新增SQL或公开协议字段。实际Task及保存拥有者、UNKNOWN的本地收场与恢复来源、分页发现和完整独立verify接口仍待实现；本阶段不接旧处理器终态，不勾P4完成，也未执行设备或媒体操作。

独立只读复核`96aeb82..6f2b517`未发现阻断缺陷。按其测试精度建议，补充两种入口中数量、编号与事件结构均合法、但末RETURN被另一条APPEND替换的反例；整组回滚。相关组测试16项通过、25项未选，日志`a1p-return-legal-replacement.log`；这次补充只修改测试，未改变业务规则。

## 基准调用只读核实与分页发现（2026-10-22 UTC）

继实际返回保存后，提供原结果键读取、完整原申请只读核实、失败依据读取和固定动作/历史边界的调用分页。读取期间使用同一可靠视图；分页上限1、2、128及游标绑定已经验证。新连接核实不会创建事件，意图原H与后续返回原H分别恢复；实际错误不从动作最终错误倒推。

新增2项有效RED后，相关持久化文件与事务路径审计94项通过，完整unit6975项通过，保留1项跳过及2条既有warning。日志为`.local/verification/osmo-resume-20261022/a1p-read-port-red.log`、`a1p-read-port-final.log`和`a1p-read-port-unit.log`，同名JSON保存实际命令、提交前HEAD、UTC及退出码。UNKNOWN保存及其恢复读取、实际调用/保存拥有者和生产接线仍待，未验证设备或完整P4。

## 基准目录实际调用拥有者（2026-10-22 UTC）

新增逐页PendingBaselineRead，使用既有owned_tool_call持有实际目录Task；BaselineSaveOwner扩展INTEND/RETURN并保留原追加/FIX。意图可靠后、实际Task创建前分别调用原资格端口，后续生产装配须从最新取消及窗口提供该端口。实际返回先由原拥有者接手并固定一次时刻，Task结束后保存原RETURN，随后传播等待者取消。已有返回不因回执未知而重读，时钟失败保留实际返回及诊断，不补造UNKNOWN或终态。

拥有者与原基准历史/准备回归33项通过；完整unit6975项通过，保留1项跳过和2条既有warning。日志为`.local/verification/osmo-resume-20261022/a1p-owner-final-2.log`、`a1p-owner-unit.log`及同名JSON。最初3项因模块缺失有效RED；实际Task结束前的保存接手、StopSignal接口不符分别经有效RED修正。新增替身消费实际DirectoryReader及StopSignal契约，不访问相机。UNKNOWN本地收场/冷恢复上下文、真实RuntimeDeps/lifecycle/handler资格及所有终态接线仍待，不能据此勾选完整P4。

## 基准返回未知与原责任恢复（2026-10-22 UTC）

在`a51be56`之后增加UNCONFIRMED保存和绑定原调用的恢复上下文。恢复先接手实际Task、已取得的返回和原保存申请，再沿原结果键读取完整事实。只有原责任已经实际收场、没有可恢复返回且可靠缺键时，才按既定同会话依据或两种已确认主机恢复边界保存未知；冷恢复使用固定启动上界。未知只固定state/result_event_id，不补造returned_at、设备错误、动作终态或释放。

拥有者、原基准历史及准备回归共52项通过，涵盖实际在途任务、固定边界、原键恢复四类结果、新连接、COMMIT回执丢失、读取和时钟失败，以及错误上下文。持久化与事务路径审计共100项通过，包含有限／有界事务、精确原键申请、UNKNOWN不能覆盖实际返回，以及快照和旧边界。完整unit单独运行6975项通过，保留1项跳过及2条既有warning。日志分别为`.local/verification/osmo-resume-20261022/a1p-cold-recovery-complete-2.log`、`a1p-unknown-store-complete.log`和`a1p-unknown-unit-isolated.log`；同名JSON保存命令、环境、UTC和退出码。

原始RED及中间失败日志继续保留。时钟异常测试初轮把合法的负数时间误当非法值，修正为布尔值后通过；未因此修改生产时间契约。早期unit与集成资源同步发生重叠，最终采用上述单独运行的unit证据。实际调用拥有者和只读入口的独立复核`6f2b517..a51be56`未发现实际缺陷；本节新增恢复分支另行复核。真实RuntimeDeps共享集合、资格提供者、共同处理器和终态发布尚未接入，不能据此勾选P4或安装版联合验收。ARM、设备和媒体验证待用户安排。

独立复核`a51be56..42336ee`没有发现Critical或Important级生产缺陷，指出读取异常未留存在原拥有者的既定契约遗漏，并要求补齐UNKNOWN独立守卫验收。两处读取现在保存同一异常对象后原样抛出；有效RED2项后拥有者完整文件27项通过。增加原scope／键／调用／时刻／结果引用、结构合法其他事件替换及APPEND→UNKNOWN组反例，两事务入口拒绝后完整相关行和历史不变；持久化与审计118项通过。随后两入口新连接的精确原申请及非完整边界复核2项通过、61项未选。日志为`a1p-recovery-read-diagnostic-red.log`、`a1p-recovery-read-diagnostic-green.log`、`a1p-unknown-review-matrix.log`和`a1p-unknown-fresh-review.log`。这些是原验收的补齐，没有新增UNKNOWN资格或恢复机制。

## 统一结果集合的创建与固定来源读取（2026-10-22 UTC）

在`10d96fc`之后推进P4的A2基础：`SegmentResultRef`使用实际set_id/version；`segment_sets`保存来源种类、完整H0、发布处置和当前扫描。新创建入口独立核实际动作／活动／RESULTS及最大实际轮次，原键保留完整原申请。逐文件来源读取从原页索引去重，恢复H0的严格文件证明；最后一轮未完整时不回取旧完整扫描。无RESULTS与实际未执行动作分别处理，原完整扫描模型接口继续可用。

原H0之后新增本run尝试、页或文件资格变化会拒绝旧申请。集合建立后，同一RESULTS责任不再取得新尝试；命令预检与原写连接的独立意图守卫分别执行核对。两事务入口的自报来源、集合身份及缺范围反例均被拒绝。原始6项缺接口RED、后续5项缺成员读口RED、两入口错误集合身份RED2，以及资格和fence的实际失败日志均保留。

新集合、来源分页及仓储审计最终84项通过，日志为`.local/verification/osmo-resume-20261022/a2-create-final-2.log`。覆盖当前轮次错误／metadata未知、跨轮漏文件、严格完成证明未知、正向与负向配对事实、原申请、新连接式历史恢复、B=1/2/128及原索引查询。此前新集合与原分段存储组合100项通过；完整operations314项通过，分别见`a2-result-set-and-inputs.log`及`a2-operations-regression.log`。

完整unit最终单独运行6975项通过，保留1项跳过及2条既有warning，日志`a2-create-unit-final.log`；同名JSON保存实际环境、命令、UTC及退出码。

无RESULTS集合经initial/current/snapshot恢复，后来集合不进入旧H。原分段历史相关32项通过、4项未选，日志`a2-history-related.log`；增长专项单独1项通过、35项未选，日志`a2-history-growth.log`。早期历史文件运行因旧增长断言漏算既有result_page_files而1项失败，另27项通过后由本任务中断，退出2；未记为全文件通过。修正后按成员、缺口和原页索引三类线性行计数，原内存上限与增长差值不变。其他未选增长专项不据此宣称通过。

结构38表3725项断言、事件37类129分支393列、报告依赖、公共协议、包资源同源和空白检查通过。统一成员追加／双向固定、取消版本完整核验、发布视图、有限结论及真实处理器继续由A2后续和A3—A5完成；本阶段不是A2或P4整体完成。未执行设备、ARM或现场媒体操作。

## 统一结果成员固定与跨轮预览保留（2026-10-22 UTC）

在`ace3941`基础上增加统一成员的有限追加及双向固定，实际引用、分类、身份和固定计数由同一写连接的守卫重新核对。两事务入口可原键接手；遗漏、错误file_event／分类、自报行身份、虚增固定计数或缺少读取范围均拒绝，固定后新键不能再追加。跨轮A/B、最后轮次错误／元信息未知仍固定全部原来源；旧H恢复原成员。取消版本准备补齐无前版的动作／取消事件唯一索引，仍不发布终态或正式产物。

真实两轮配对测试确认并修复一处实现错误：后轮只有LRF、可靠记录MISSING时，保留来源曾错误选最新负向配对，覆盖早期可靠正向关联。保留集合现在沿H0文件已经保存的首次正向证明，当前轮次仍独立采用自己的事实。有效RED1后，正向／负向及跨轮保留3项通过；日志为`.local/verification/osmo-resume-20261022/a2-retained-pair-red.log`和`a2-retained-pair-green.log`。这项修复落实原R保留契约，没有新增防御假设。

新集合／成员文件与仓储审计97项通过，日志`a2-member-stage-final.log`。成员接口最初2项因实现缺失有效RED；无前版取消唯一性1项有效RED后两分区通过。有限／有界追加、固定、原键和历史旧H分别核对。结论、native、发布视图和共同处理器仍未接入；本节不宣称完整A2、P4或安装链通过。

完整unit单独运行6975项通过、1项跳过及2条既有warning，日志`a2-members-unit.log`。结构38表3727项断言、事件37类130分支及空白检查通过。

## 统一结果来源与原完整扫描接口的适用范围修复（2026-10-22 UTC）

独立评审发现原创建阶段的三处实现遗漏：后轮负向配对会取消早轮可靠预览资格；原完整扫描写接口可以把跨轮集合只按末轮固定，并保存不适用的普通判断；当前扫描误把基准内旧文件的未知元信息当成本次差集未知。这些问题均有既定规则，修复没有增加设计分区或防御假设。第一项已在`7a9a04f`修复，本轮补齐后轮MISSING／AMBIGUOUS／CONFLICT的真实事务验证。

原完整扫描成员接口只保存其原模型集合；统一结果始终经H0成员端口追加和双向固定。普通判断、缺口、范围及原发布入口仅适用于COMPLETE_SCAN+RESULT；只读当前扫描引用仍可用于独立计算。命令预检和两类事务的独立守卫分别核这些条件，绕过命令预检也不能写入。当前扫描的元信息检查改为只消费实际差集身份，仍核原完整页链及错误。

新增集合文件62项通过，日志`.local/verification/osmo-resume-20261022/a2-review-fixes-green.log`；随后补充的统一完整集合兼容矩阵4项通过、62项未选，日志`a2-review-complete-writer.log`。该矩阵确认统一成员接口固定后仍可保存适用的普通判断。原分段存储与仓储审计144项通过，日志`a2-review-legacy-and-audit-valid.log`。初次反例包含文件排序和缺口生成器前提不成立的夹具，日志保留；修正夹具后的基准过滤反例4项有效失败、2项通过，再修复实现。误写审计文件名的一次命令未执行测试，退出4，不计入验证结果。

完整unit独立运行6975项通过、1项跳过及2条既有warning，日志`a2-review-fix-unit.log`。5231个本地文档链接及空白检查通过。本节仅记录已验证的软件存储范围；取消版本、有限结论、真实处理器、发布视图和安装链继续按原计划推进。

独立评审固定`ace3941..a01c85a`复核三项修复及新增统一成员阶段，确认原三项Important闭合，未发现新的Critical／Important／Minor。评审只读核对原日志、JSON及SHA-256，没有重跑测试或修改工作区；后续取消完整恢复、结论、共同完成和消费者仍另行验收。

## 显式取回源检查身份与共享内核组合

按 OSMO 计划继续 P5 身份底座。主线候选为 `747b754`，环境沿用 Python 3.11.16、SQLite 3.53.1；实现与两次守卫修复已经独立评审，事务入口合成另经限定复核。原页、来源、准入和分段的读取范围、实际前序、封口及完成检查保留，新取回读口从同一实际写连接独立绑定。

| 验证范围 | 实际结果及日志 |
| --- | --- |
| 主线完整 unit | 6926 passed、1 skipped、2 条既有 warning，退出 0；`ua2-combined-unit.log`。 |
| 主线完整 operations | 314 passed，退出 0；`ua2-combined-operations.log`。 |
| 主线 persistence 初轮 | 1 failed、599 passed、4 deselected，退出 1；`ua2-combined-persistence.log`。四个增长专项已有独立证据，本轮明确排除。 |
| 事务路径审计修正后完整文件 | 51 passed，退出 0；`ua2-read-audit-final.log`。补登记六个实际只读核实函数，并逐函数验证拒绝 BEGIN IMMEDIATE；未重跑整个 persistence，不将两次结果合记为完整目录通过。 |
| 主线清理原键及保存恢复 | 73 passed，退出 0；`ua2-combined-cleanup.log`。 |
| 五类既有 run 创建者及历史恢复 | 5 passed，退出 0；`ua2-combined-history.log`。涵盖普通 Begin、调度 START、内部／交付 READ、绑定失败的必要责任和应急补记。 |
| 数据库及事件登记 | 37 张表、3611 项结构断言；36 类事件、125 个分支、365 列，退出 0。 |

上述主线日志及带实际 UTC、命令、HEAD、导入路径、摘要和退出码的 JSON 在 `.local/verification/osmo-resume-20261022/`。初轮 persistence 唯一失败是审计未登记已交付的只读入口，没有业务保存失败；全部失败及修正日志保留。审计按原规则允许登记函数以 BEGIN、COMMIT 或 ROLLBACK 管理只读视图，拒绝 BEGIN IMMEDIATE、仓储的 `.commit()` 方法调用及直接连接数据库。

原身份工作树另有真实 RUNNING 前票与虚构上下文的有效反例；修复从实际 SQL 读取原意图、结果 delta 和完整事务。随后“流程增加次数但没有自己的新票”的 16 例中，初轮 10 failed、6 passed；补齐两入口检查后，身份文件 105 passed。旧树完整 outputs 的 35 项合成来源／媒体读取契约失败继续保留，未据旧归档相同而记为通过。

本阶段只交付身份、配置分区、预算和直接操作 API，不证明实际 QUERY、文件可用性、来源解析、取回资格或调度完成。P4 共同处理器、P5 后续生产链、P6 后端完整报告和 P8 仍未完成；ARM、真机及现场媒体操作待用户安排。

## Hasselblad 完整参数的安装版联合续验（2026-10-21 UTC）

按[完整参数计划 H7—H9](../superpowers/plans/2026-10-21-hasselblad-photo-parameters.md#h7更新完整样例及安装资源消费者)，在 `39b01db` 候选上验证当前安装链。环境为 Linux x86_64／WSL2、Python 3.11.16、SQLite 3.53.1、Node.js 24.20.0 和 pnpm 12.3.4。客户端从原 `package-lock.json` 安装，仓库外候选的 279 个受控文件与当前输入逐一相同；类型、构建和完整测试结果见[客户端续验记录](../client/verification.md#hasselblad-命名选项与安装版参数验收2026-10-21-utc)。

| 验证范围 | 实际结果 |
| --- | --- |
| 完整 unit | 6908 passed、1 skipped、2 条既有 asyncio 标记警告，退出 0。 |
| 完整 bootstrap | 13 failed、1327 passed、1 skipped，退出 1。开始于 `3a23add`，运行期间仅提交现场指南和组件入口文档，生产及测试字节保持。 |
| 两份完整 Hasselblad 根组合模块 | 36 passed，985.12 秒，退出 0；同一候选顺序执行全部参数、网页、导入、正常和故障场景。 |
| 受影响的能力、时间参数和 C host 根回归 | 四份完整模块共 19 passed，148.33 秒，退出 0，包含参数模块的再次消费。 |
| C host 完整测试 | 单元 12／12、集成 22／22，通过独立构建目录执行，均退出 0。 |
| 清理来源契约修正后的完整测试文件 | 8 passed，16.10 秒，退出 0；补查成员批次、唯一末固定事件、原完整申请及目标固定顺序。 |
| 完整 camctl 组件集成目录 | 按组件指南 16 个目录分别启动 pytest。14 个目录共 5256 passed，全部退出 0；bootstrap、outputs 的本次完整结果见下文。 |
| 完整 outputs | 2435 passed、35 failed、2 skipped，退出 1；35 个失败用例 ID 及各自首个错误与 2026-10-19 的归档逐项相同。 |
| 协议、事件、报告依赖、数据库规格及资源 | 均退出 0；27 项包资源已同步并核对。 |

14 个通过的目录分别为 acceptance 338、cancellation 396、capture 1990、contracts 442、devices 220、history 339、host_files 120、logging_runtime 112、motor 12、operations 209、persistence 315、reporting 433、scheduling 136、session 194。初轮目录候选为 81b0a5e；contracts 修正后和后三个完整目录在 fb1f0a1 上运行，两候选的生产源码及资源一致。acceptance、cancellation、capture 的首轮启动器漏建临时父目录，形成 setup 错误；原日志保留，修正后按目录完整重跑并取得表中结果。contracts 首轮两项为结构检查器误把可重建派生库当作普通业务 SQLite、误把本地 URI 解析当作网络客户端；限定检查范围并加入仍拒绝其他业务 SQLite 与网络客户端的反例后，完整目录 442 项通过。outputs 的 35 项失败与当前录像来源、修复媒体独立读取责任有关，仍保留失败；不能用旧归档相同这一事实认定通过。

outputs 的 35 项失败可按测试入口复核：test_product_event_sequence.py 有 5 项，其中 3 项在合成的取回授予与录像来源完成事务中要求原计划读取范围，另 2 项在修复媒体复核时要求同一范围；授予计划已经声明逐产物读取范围。test_selection_derived_sequence.py 有 30 项，合成修复成品登记和选择时未声明原计划读取范围，正式修复提升守卫因而取不到独立媒体读取端口。当前事务内核对一条命令最多接受一个事实读取范围。上述合成组合是否属于有效单事务命令、应拆分事务，还是需要新的共同读取契约，不能由失败输出自行决定；目前没有放宽守卫、删除断言或补造媒体资格。

安装链中的 12 个正常场景覆盖 RAW／JPEG × host／device × 三个精确短快门；六个故障场景覆盖两种正式源的 START 结果保存后重启、完整提交后回执丢失，以及结果缺失和原报告重建。测试实际经过网页预设重开／导出、安装 wheel、C host、CLI、SQLite、完整历史、原图／预览各两次独立取回、`Files.upload` 和累计 ACK。相机只在 `hb_cli` 进程边界使用受契约约束的替身。

bootstrap 的两项清理来源失败来自旧断言：现行契约要求成员批次和唯一末固定事件，而原断言只列一个来源事件。只读原失败库确认实际事件及事务范围符合契约，随后补齐上述独立断言并取得完整文件通过结果。其余十一项位于 `test_osmo_shared_recovery.py`，已有分段完成依据的来源要求原完整分段申请和同组最终 `ACTIVATE`，当前运行时尚未提交该事务；未放宽生产守卫或测试。完整 bootstrap 的本次结果仍是表中的 13 项失败，不能用定向通过数替代完整目录的重新验收。H7 步骤 5、H8 步骤 6 保持未完成。

全部命令、起止 UTC、候选／差异摘要、日志、退出码与 JUnit 已归档到 `.local/verification/historical-evidence-20261023/hasselblad-photo-20261022/main/`；归档时逐件核对229份文件的长度与 SHA-256，清单位于该历史证据根的 `manifest.json`。安装链的1568份业务证据继续保存在 `.local/verification/hasselblad-photo-20261022/installed.tar.gz`，归档 SHA-256 为 `f4efb43d493d73c1e3e24d9812dc066984a36a6901b88deb7b0160d04b931181`。目录名沿用对应本地日期，执行时间以各收据的 UTC 为准。

本轮由当前 Agent 自审，未使用子代理，不提供新增独立评审者结论。本项仅证明软件与安装版联合链；ARM 部署、真实机身／镜头参数、JPEG 设备源字节一致性及现场客户端／ACK 仍按 H9 另行取证，没有新增真实相机操作。

本地另从已验同源候选构建 wheel、锁定依赖清单及 C host 源码包，逐件比对 wheel 内 264 份源码、27 份权威资源和源码包内 69 份受控文件，结果保存在 .local/deployments/20261022-hasselblad-parameters/。只读 ARM SSH 探测连接超时，未执行远端命令；这些本地材料不代表已完成部署或 H9 设备验收。

## OSMO 360 II 新固件真机复验（2026-10-20 UTC）

TX2 使用 ARM64、Python 3.11.17、SQLite 3.53.1 和本机 ADB 1.0.39，当前相机 serial 为 `123456OSMO360II`，`ro.dji.build.version=10.00.02.27`。默认应用缺少正式 OSMO 驱动，本次在独立目录使用此前已验 wheel `25bd19567a397678690b3e13c2bf992e78d2459208589a32a64013b79ca84a48`，266 份包内文件逐一匹配；每个场景使用独立配置和状态库。

| 实际采集场景 | 媒体结果 | 正式动作结果 |
| --- | --- | --- |
| 手动曝光、标准码率、10 秒录像 | 两组 OSV/LRF；两段 OSV 的双路主视频各为 164 帧／5.472133 秒、151 帧／5.572233 秒。 | `invalid_outputs`：冻结合同要求恰好一份 OSV，实际两份。 |
| 自动曝光、高码率、10 秒录像 | 两组 OSV/LRF；两段 OSV 的双路主视频各为 164 帧／5.472133 秒、141 帧／4.704700 秒。 | 同样因单原片数量限制失败。 |
| 自动曝光、高码率、2 秒间隔／300 秒延时 | 一组非零 OSV/LRF，主视频各 150 帧／5.005000 秒；另有 0 字节 OSV/LRF。 | `invalid_outputs`：新增空 OSV 不满足最小长度要求；延时合同没有单原片数量限制。 |
| 手动曝光、标准码率、30 秒间隔／300 秒延时 | 一组非零 OSV/LRF，主视频各 11 帧／0.367033 秒。 | 成功；USB 断开、重新枚举后恢复，boot ID 前后一致。 |

用户确认约 5 秒录像分段为预期行为。四轮实际采集分别只发一次 START，普通录像各有一次可靠 STOP，并完成 30 秒文件等待；两轮延时无 STOP，在可靠发送返回后 330.029091 秒、330.054261 秒开始读取结果，等待期间没有新增相机调用。分段的独立时长不直接相加认作连续成片时长，媒体帧数也不替代采样参数的独立读回。

12 份非零媒体的副本大小均与登记源长度一致，取回前后的相机源、TX2 副本与开发容器副本 SHA-256 一致。全部主视频及音频轨完整解码，附件 MJPEG 单列并排除。FFmpeg 均退出 0；手动录像第二段 OSV/LRF 报 `Could not find ref with POC 6`，其余媒体无诊断。该诊断与空尾段的成因尚待核实，用户尚不清楚空尾段是否预期，不据退出码或非零原片宣布整组验收通过。

最初的自动曝光准备在 `1e 0100` 响应的 `00` 后出现 `Create Dir /data/dji/amt/ Success.`，触发严格格式校验失败，尚未发送 START。目录创建时间与该次命令一致；随后相机空闲、boot ID 与原有 12 文件清单未变。这些事实支持初始化解释，但没有证明全部初始化机制。

相机最终空闲，原片和两份空尾段保留，源目录共 26 个文件；默认配置和状态库前后摘要一致。证据位于 `.local/diagnostics/20261020-osmo-firmware-revalidation/`，包含各场景计划、原始调用、SQLite、媒体、逐文件解码、`acceptance-summary.json`、`evidence-manifest.json` 和 `REPORT.md`。本次没有修改生产代码；验证范围为正式 CLI、内置驱动及媒体检查，不包括 C host／客户端导入与 ACK 联合链，OSMO 独立单张拍照仍没有生产接口。

## 范围清理来源解析修复的软件验证（2026-10-19 UTC）

按[来源解析修复计划](../superpowers/plans/2026-10-19-cleanup-source-resolution.md)，修复跨计划或按动作实例清理在来源仍为 `PENDING` 时直接固定目标，导致会话以 `state_db_error` 退出的问题。范围清理复用来源解析事务；来源及依赖可靠保存后才选择产物，只有取回创建 `obtain_source_selections`。公共输入、设备命令及数据库结构保持现行契约。

Linux x86_64、Python 3.11.16、SQLite 3.53.1 下，各目录分别启动 pytest 并串行执行。首轮七个新场景重现原来源守卫错误；最终结果如下：

| 范围 | 实际结果 |
| --- | --- |
| unit | 5950 passed、1 skipped、2 条既有 asyncio 标记警告。 |
| outputs 完整目录 | 2198 passed、35 failed、2 skipped；35 个用例 ID 和首个错误与既有归档逐项相同，无新增或消失的失败。 |
| bootstrap 完整目录 | 1308 passed、1 skipped。 |
| acceptance | 266 passed。 |
| cancellation | 103 passed。 |
| history | 212 passed。 |
| reporting | 373 passed。 |
| 新增来源事务文件，含完整目录结束后追加的多来源场景 | 7 passed；覆盖保存回滚、提交前后未知、原键续接、固定来源复用、整计划／组／指定动作及三种拍摄来源。 |
| 新增正式装配文件 | 8 passed；覆盖跨计划与动作实例引用、来源缺失、有效空范围、原执行中动作恢复、等待和取消，并核对实际报告。 |

从 TX2 原库只读取得一致性副本后，在本地副本使用生产仓储续接原清理 action `6`：其状态由 `running + source=PENDING + targets=PENDING` 推进为两个集合均 `FIXED`，来源为原 action `1`、`3`，目标为原 output `1`—`4`，未创建取回选择。操作尝试总数保持 9，没有执行文件端口。此项证明原事务入口可续接现场状态，不等同于真实文件删除验收。

命令、UTC、JUnit、日志、源码摘要、历史失败逐项比较及本地副本凭据保存在 `.local/verification/cleanup-source-resolution-20261019T020920Z/`，现场只读快照保存在 `.local/diagnostics/20261019T015606Z-cleanup-error/`。独立只读评审核对来源、空集合、取消及原保存责任，未发现本次改动引入的确定性正确性问题；本项没有新增拍摄或操作真实文件删除。

## 当前驱动的 TX2 部署与能力同步（2026-10-19 UTC）

从 `937b134` 构建 wheel 和锁定运行依赖清单。构建产物中的 238 份 Python 源码及 27 份资源分别与仓库权威输入核对；TX2 的 ARM64、Python 3.11.17 和 SQLite 3.53.1 环境完成依赖安装检查及 wheel 更新，265 份安装文件与发行物逐字节一致。OSMO 使用必填码率和仅模式曝光的新参数；两个拍摄任务均由实际安装版导出。

更新前确认没有运行中的 camctl／host-demo，也没有未完成动作，备份原安装内容、配置与状态库。更新后核对状态库完整逻辑内容、配置及其默认软链接、57 份业务文件和现有 host-demo 的 9 份安装文件均保持原样。设备仍为 `osmo360ii` 和 `hb0`，哈苏正式源配置为 host；默认与显式配置的 describe 字节完全相同，取回本地后经公共 Schema 校验，并与源码导出逐字节一致。

本地交付物、运行记录与 SHA-256 清单保存于 `.local/deployments/20261019002145Z-937b134/`，目标备份和安装记录保存在 TX2 的同名部署目录。客户端 ZIP 的实际安装、网页和导出验证见[部署包交付记录](../client/verification.md#当前部署包与两设备能力交付2026-10-19-utc)。本项验证安装及能力交接，没有触发真实拍摄或相机设置命令。

## OSMO 360 II 可选参数与会话设置的软件验证（2026-10-18 UTC）

在 Linux x86_64、Python 3.11.16、SQLite 3.53.1、Node.js 24.20.0 和 pnpm 12.3.4 中，按[实施计划](../superpowers/plans/2026-10-19-osmo360ii-selectable-settings.md)验证当前参数契约。普通录像和原生延时均要求动作显式携带码率档位，逐次发送相应设置；固定 ISO、EV 和视频输出由驱动维护。会话固定设置按设备绑定、ADB serial 和完整命令分别记录，只复用已可靠接受的结果。

以下范围均保留完整命令、工作目录、解释器、起止 UTC、日志和退出码于 `.local/verification/osmo-settings-20261019/`。表中的 pytest 路径由该记录器使用项目 Python 3.11 执行；正式 OSMO 安装链使用对应指南要求的解释器直接启动。

| 范围 | 实际结果 |
| --- | --- |
| `apps/camctl/tests/unit` | 5950 passed、1 skipped、2 warnings，退出 0；两条 warning 仍是既有同步测试的 asyncio 标记，原 skip 原因未从 `-q` 输出取得。 |
| `apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py` | 14 passed，验证能力 Schema、原输入、生效参数和数据库保存。 |
| `apps/camctl/tests/integration/devices/test_adb_camera_registration.py` | 29 passed，包含实际会话初始化重置记录、静态登记和能力导出保持记录及零次设备调用。 |
| `apps/camctl/tests/integration/bootstrap/test_adb_camera_dispatch.py` | 16 passed，验证派发资格、取消、启动窗口和原参数执行。 |
| `apps/camctl/tests/integration/bootstrap/test_distribution.py` | 38 passed，安装版示例、能力导出和受理一致。 |
| `docs/hardware/tests/integration/test_action6_record_probe.py` | 84 passed、316 subtests passed；生成器支持有值的带类型标注赋值，未知依赖、非法源码形状和原 Action6 诊断流程仍受检。 |
| `tests/integration/test_device_capability_declarations.py` | 6 passed；安装版 JSON 经真实客户端加载、HTTP、参数表单和设备指南消费。 |
| `tests/integration/test_timelapse_time_parameters.py` | 3 passed；两款参数域经 Python、浏览器、Node.js 导出和受理／取消／报告链核对，包含 OSMO 两档码率和仅模式曝光对象。 |
| `tests/integration/test_osmo_builtin_timelapse_roundtrip.py` | 4 passed；正式内置合同经客户端、C host、安装 CLI、SQLite、恢复、独立取回、导入及 ACK 验证。 |
| 客户端两份浏览器测试中的“能力源”目标 | 3 passed，98 项未被该目标选中；合成能力与安装版能力分别验证模式切换、必选码率保存／导出和固定字段不产生控件。类型与格式检查通过。 |

固定设置的单元用例先失败，再由实现通过，覆盖首次普通录像／延时、同会话重复动作、同档位码率重复设置、两台设备、同进程新会话、部分设置成功、未知返回、取消及原 START 完整期限耗尽。安装链的 ADB 进程替身只接受当前安装包的明确命令；普通录像的停止后文件等待仍使用真实等待。模拟文件和工具返回不提供真实设备画质、码率生效或新增命令的实机效果证据。

正式安装链首轮由 `uv run` 启动时，开发环境路径进入了其运行环境，四项均在独立部署路径断言处失败，尚未执行业务动作；按文档改用 Python 3.11 解释器直接启动后四项通过，保留首轮失败日志。协议、同源生成物、文档文件链接及差异检查通过，新增标题锚点另行核对。

本地当前驱动另导出 `.local/deployments/20261019-osmo-settings/device-capabilities.json`，包含 `osmo360ii` 和采用主机正式源的 `hb0`。公共 Schema 及实际字段检查通过，SHA-256 为 `c64ed7aa118e120ceec8961f615f8b519b6e39866f7a258e3951130dbba351b6`。该文件与本次驱动代码配套，本项没有部署 TX2 或执行真实相机命令。

独立评审者对 `a8b87b6..b8bb4f3` 完成串行只读复核，核对固定设置记录、实际会话重置、失败门禁、新参数、生成器和已有日志，未发现影响驱动软件交付的问题。客户端通用表单的进一步复核及其待修复显示／测试问题另见[客户端记录](../client/verification.md#设备能力解析与选项联动复核2026-10-18-utc)。

## 设备文件清理的 TX2 实机验收（2026-10-18 UTC）

[设备文件清理计划 R7](../superpowers/plans/2026-10-18-device-file-cleanup.md#r7用已验发行物完成-osmo-目标设备验收)已通过真实 OSMO 360 II 的正式清理链验收。现场使用 `cld` 账户、ARM64、Python 3.11.17、SQLite 3.53.1 和 ADB 1.0.39；USB 为 `2ca3:0020`，产品属性为 `eagle4_oq102`，固件为 `10.00.00.57`／`01.01.02.10`。本次检查至最终目录核对的相机 boot ID 均为 `64eb2280-7f6e-48bb-bafd-5bed1dd7a797`。

部署使用下节软件联合链已验证的 wheel，SHA-256 为 `92d26137b6ddbb6ada476dcbcfce1a2f30be57e536769cd92c9e9c2f69e2362f`；安装后的 265 份源码及资源与 wheel 逐字节一致，9 项运行依赖与配套锁定清单一致。复用的 ARM host SHA-256 为 `2889f129dfa6abe1346b30584fbdd21eb23a8027e026256be75976b898400461`，其源码目录与此前已验版本 `bc0cad2` 相同。实际 `describe` 导出 OSMO 删除支持 true、Action6 false。

现场保留原配置、状态库及旧安装备份，没有执行 `init`。包含 WAL 的初始数据库备份 SHA-256 为 `08aed14ac2fa0884db2c78d458e8dc3907cc023bec1daf4ad4d0bc0d138d3a80`，与上次验收归档一致；生产责任查询没有未完成动作、开放尝试或必要收场工作。原终态活动中的未知设备观察保持，不据此补发拍摄命令。独立验收配置使用 DELETE／QUERY 各 1 次预算、各 60 秒期限；原 `camera-demo/config.toml` 字节保持。

### 精确文件、调用与结果

两个目标来自不同拍摄组，均先核对当前设备大小和 SHA-256、正式产物身份及独立本地交付副本，随后通过真实客户端导出、C host 与安装版 CLI 提交。共享远端目录为 `/mnt/media_rw/emulated/DCIM/CAM_001/`。选取较小的既有正式源即可验证双向不级联，无需新增拍摄。

| 删除目标 | 原文件及大小 | 正式请求与结果 | 保留的配对产物 |
| --- | --- | --- | --- |
| output `9` | `CAM_20000101053528_0041_D.OSV`，17,350,298 字节 | 请求 `9079710386731686124`，action `40`／item `2` 成功。先 QUERY 确认在场，再 DELETE 一次，保存可靠缺席。 | output `8` 的同组 LRF，3,127,250 字节，大小及摘要保持。 |
| output `20` | `CAM_20000101133229_0047_D.LRF`，3,198,117 字节 | 请求 `3643755395682129271`，action `41`／item `3` 成功。DELETE 一次，保存可靠缺席。 | output `21` 的同组 OSV，17,587,655 字节，大小及摘要保持。 |

目录条目数包含目录根，依次为 `71 → 70 → 69`；差集严格等于上述两个精确路径。独立只读检查取得两目标缺席的完整 NUL 帧，两份配对文件的大小和摘要不变。所有保留本地副本再次校验通过；数据库没有相应正式修复 MP4，本项不声明主机 MP4 的实机删除结果，主机正式源由下节软件链验收。

首次请求 `6694547240682406550` 的验收日志启动器有换行转义错误，Python 在执行实际 ADB 前编译失败。该请求留下的本地 DELETE／QUERY 失败、未知效果及 action `38`／item `1` 终态均保留，实际设备调用为零。将启动器改为独立文件并在目标 Python 编译通过后，重新核对四份文件，再提交表中的新请求。框架按原未知文件事实先查询在场，随后才删除；没有改写旧失败、重放旧票据或刷新旧次数。启动器只限制本次清单并记录参数，随后原样执行 `/usr/bin/adb`，没有替换生产工厂、解释器或设备响应。

新请求 `2179098343299311917` 再清理 output `9`、`20`，action `42` 的两个成员均返回 `ALREADY_CLEANED`，没有增加操作流程、尝试或实际调用。整个现场正式链实际使用 QUERY 一次、DELETE 两次；独立只读目录、摘要及协议检查另行归档，不计作业务 QUERY 尝试。三个实际文件调用均正常退出，保存的观察分别为在场、缺席、缺席。

### 报告、ACK 与不可变历史

C host 领取完整报告 `164`，范围为 `0..541`，大小 34,123 字节，SHA-256 为 `a72dc90fed560844af4992b43ab2229e45d8be1fd9a7f1633877496c687d37d4`。`ready → processing` 前后字节相同，原有 56 份 processing 文件保留；旧报告摘要保持。逻辑报告 `155..163` 仍保留发布记录，其 ready 文件按[覆盖替换规则](../architecture/report-publication.md)由完整报告 `164` 承担。

真实客户端导入后，output `9`、`20` 为 `cleaned／completed`，配对的 `8`、`21` 仍为 `available／not_requested`。客户端通过 `Application.downloadRequest` 为原请求 `2179098343299311917` 加上 `last_report_id: "164"` 重送，目标库吸收 ACK `164`、watermark `541`；计划、动作、产物、清理项、操作流程、尝试和报告数量均不增加。

初始／最终数据库的独立逐字段比较确认：原 1700 条事件、1281 个事务、37 份计划、37 个动作、53 个操作流程、53 次尝试、21 份交付、21 份拷贝、154 份报告与数据库身份保持。只有原 outputs／device_files 的 `9`、`20` 更新清理事实，其余产物、设备文件、中间文件和配对关联保持；首次失败成员的完整行也保持。最终必要工作为零、没有未确认报告责任，本次验收 host 在确认无 CLI 子进程后结束，共享 ADB 服务端保留。

### 证据与未验边界

完整命令、起止 UTC、退出码、原始调用及响应、配置摘要、文件清单、旧安装备份、各阶段 SQLite 备份、报告及客户端凭据统一保存于 `.local/acceptance/device-file-cleanup-20261018T211358Z/`；目标原件位于 `/home/cld/.camctl/device-file-cleanup-acceptance/20261018T211358Z/`。下载的现场包 SHA-256 为 `7ab542e831d94f25e268615c1a2cf93b6af202128eb227497470b0fa9d0bdbe8`，65 份文件逐一校验通过；最终 `final.db` SHA-256 为 `1afa2a63a558297bdec2bad1f7064ad01b6e567708284ec661326aba63bb1e6a`。具体文件备份及完整摘要以 `file-manifest.verified.json` 为依据，决策和数据库比较分别见 `execution-decision.json`、`database-comparison.json`。

本项验证 ARM 实际工具、完整帧、精确删除、可靠缺席、新请求复用及真实报告交接。没有新增拍摄、发送 START／STOP、操作 Action6 或执行物理断电；中断、保存未知及取消恢复的完整矩阵由 R6 软件证据承担。软件门禁的既有 35 项 outputs 失败与两条标记告警仍按下节原样记录。

## 设备文件清理的软件联合验收（2026-10-18 UTC）

在 Linux x86_64／WSL2、Python 3.11.16、SQLite 3.53.1 和 Node.js 24.20.0 中，验证[设备文件清理计划](../superpowers/plans/2026-10-18-device-file-cleanup.md)的 R6 软件联合链。[安装版联合用例](../../tests/integration/test_device_file_cleanup_roundtrip.py)复用已有独立动作 `delete_action_outputs`，通过真实客户端导出、C host、仓库外 wheel、CLI、SQLite、正式源文件、状态报告及累计 ACK 验证清理执行和恢复。ADB／`hb_cli` 只在外部工具边界提供受合同约束的文件和响应；没有替换生产文件工厂、解释器、清理状态机或仓储。

最终联合输入为生产代码 `34d0cbd`、文档跟踪提交 `bdfc601` 及归档收据所列根测试字节。安装 wheel 的 SHA-256 为 `92d26137b6ddbb6ada476dcbcfce1a2f30be57e536769cd92c9e9c2f69e2362f`，13 个相关模块实际从独立 venv 的 `site-packages` 加载，包括清理装配、后台执行及会话模块。通过正式内置登记核对 OSMO 的 DELETE／FILE_QUERY 工厂和端口：设备删除与文件查询均支持，拍摄状态查询仍不支持；这项软件声明不等于真机删除已验收。

同一次根 pytest 命令运行新增模块的 10 个分区及原 OSMO 模块的 4 个用例，实际结果为 **14 passed，669.06 秒**，退出 0。命令、起止 UTC、Python、HEAD、跟踪差异与新增文件 SHA、前后输入及完整日志保存于 `.local/verification/r6-serial-roundtrip/1792357637593371819/`；各场景的实际数据库、请求、调用、门闩、进程收场、文件和报告凭据保存于 `.local/r6/artifacts/`。执行命令为：

```bash
apps/camctl/.venv311/bin/python -m pytest \
  tests/integration/test_device_file_cleanup_roundtrip.py \
  tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
```

清理覆盖以下实际组合：

- 主机 repaired、设备 OSV、设备 LRF、主机 original、主机 preview 分别使用新请求精确清理。关联源、其他产物及五份已经发布的交付副本保持，设备文件只产生两次删除调用，三个主机源使用真实文件执行器。
- Hasselblad 设备源不支持删除时，同请求中的主机原图和预览仍分别完成；设备项没有 DELETE／QUERY 尝试。对已清理产物提交新请求，只本地交接，不增加调用或改写旧结果。
- DELETE 效果已发生但返回丢失后结束旧组，新进程沿原尝试恢复 UNKNOWN，以一次 QUERY 核实缺席；原票据不重放。查询在场后分别验证新 DELETE 使用当前配置、删除预算耗尽及查询未知耗尽，原尝试继续保持原配置与累计次数。
- 真实结果事务提交后回执未知，以及成员已终态、伴随流程尚未结束两个窗口，均由新 host 本地消费原结果；没有新增 DELETE／QUERY，原事件和成员终态保持。
- DELETE 在途时，取消事实先可靠保存；原尝试仍 RUNNING、没有结果事件，门闩进程仍活动且旧组没有 QUERY。结束旧组后，新组恢复 UNKNOWN，只核实一次在场，清理项为 `canceled + file_delete_failed`，动作保持取消、不可撤销限制保持，取消动作如实报告 `cancel_items_failed`。

修复成品来自真实双主视频 OSV 样本：两路 64×64、10fps、3 秒 H.264，经正式准备及后轮修复生成 MP4。源和成品均实际运行 ffprobe 和全流解码，登记的长度、摘要和两路流信息一致。普通录像 STOP 后到首次结果目录读取保留真实不少于 30 秒的等待；业务钟推进不替代该等待。LRF 是独立非空模型字节，不声明媒体可解码或真实设备画质、分辨率、采样时长。

组件证据按实际版本分别保留，不能合称最终提交同一次全目录通过：

| 范围 | 冻结版本及实际结果 | 后续覆盖与边界 |
| --- | --- | --- |
| unit | root 在 `34d0cbd`：5902 passed、1 skipped、2 warnings | 两条 warning 是既有同步测试的 asyncio 标记；原 skip 原因未从 `-q` 输出取得。 |
| devices、operations | 初始 `1a393d4`：分别 204、200 passed | 后续媒体及调度修复不改设备、操作合同或对应测试；按实际未改输入沿用。 |
| capture、host_files、cancellation | `ce3b34b`：分别 1914、117、103 passed | fix1 修改的观察取消先后及通知引用由新 unit、session 和 bootstrap 验证，未改这些目录的实际业务入口。 |
| session | `4e81075` 冻结字节：192 passed | 覆盖真实锁控制、观察者取消、run 父取消、启动及受限阶段。 |
| bootstrap | `ce3b34b` 完整目录：1298 passed、2 failed、1 skipped | 两项失败来自测试包装器漏传 `waiter`；`34d0cbd` 修正后两份完整相关文件 47 passed。fix1 七文件 101 passed，root 合入后 owner／锁／启动三文件共 62 passed。未将这些数字相加为新全目录结果。 |
| outputs | `ce3b34b`：35 failed、2194 passed、2 skipped，退出 1 | 35 项均在 `test_product_event_sequence.py`、`test_selection_derived_sequence.py`，ID、首错及位置与原基线相同，逐项差异为零；本目录未全绿。 |

root 新组合收据位于 `.local/verification/cleanup-r6-serial-integration/`；原五个任务工作树的日志、证据及未提交原件已归档到 `.local/verification/worktree-archives/`，`manifest.json` 保存旧目录、分支、HEAD 与各归档摘要，原始命令仍保留当时的工作目录。本计划交接材料继续保存独立复核。媒体阶段修复让准备任务可靠登记原输出后结束，后轮重新取得完整文件资格；清理调度修复让中央在实际文件调用等待期间继续保存取消，并在错误退出时先停止新增普通工作、再接回真实结果和原保存责任。这些实现契约由[文件执行](file-runtime.md)和[实现总览](implementation.md#动作执行协程的创建时机)维护。

最终协议、文档链接与差异检查另有实际命令及退出码。安装版和组件软件结果不替代 ARM 依赖、设备工具、真实物理删除或物理断电验收；R7 现场结果独立记录。

## 设备能力声明的安装版联合验证（2026-10-18）

在 Linux x86_64／WSL2、Python 3.11.16、SQLite 3.53.1、Node.js 24.20.0 和 pnpm 12.3.4 中，验证设备声明、任务说明和清理入口展示的安装版软件链。新增的[联合用例](../../tests/integration/test_device_capability_declarations.py)在仓库外离线构建 wheel，按锁文件安装运行依赖，使用独立虚拟环境执行真实 CLI；安装进程清除 Python 源码查找环境，另核对导入位置确实位于该独立环境。源码与安装版 `describe` 的精确 JSON 一致，公共 Schema 与 wheel 内资源字节一致。

同一份真实配置覆盖全部正式内置驱动、Hasselblad 主机／设备正式源，以及同驱动不同逻辑设备。通过真实登记接入的任意驱动另覆盖空拍摄目录和明确支持删除；源能力没有按驱动名称或动作数量推断。真实 `init`、`submit` 与 SQLite 验证：主机源受理的旧请求在改为设备源后重送，原执行定义保持；新请求保存设备源定义。导出期间的拍摄、删除端口和 ADB／`hb_cli` 工具均设置禁止调用边界。该用例不执行拍摄和清理，不证明设备文件实际可删除。

本次从任务工作树根目录依次运行以下范围，均退出 0；原始命令、UTC、日志和退出码保存在该树 `.local/acceptance/c5/`：

| 命令或目标 | 实际结果 |
| --- | --- |
| `node scripts/check-protocol.mjs` | 4 份 Schema、5 份能力说明及既有协议正反例通过。 |
| `PYTHONPATH=apps/camctl/src apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit/devices -q` | 1096 项通过；一条已有的非异步函数 asyncio 标记警告。 |
| 同一解释器执行 `apps/camctl/tests/integration/devices` | 178 项通过。 |
| 同一解释器执行 `apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py` | 14 项通过。 |
| 同一解释器执行 `apps/camctl/tests/integration/bootstrap/test_distribution.py` | 38 项通过。 |
| 同一解释器执行 `tests/integration/test_device_capability_declarations.py` | 6 项通过；含安装 JSON 进入真实客户端加载、HTTP、参数表单、设备指南及清理入口。 |
| `node scripts/check-doc-links.mjs` | 4863 个本地文件链接通过；不校验标题锚点。 |

页面联合验证还使用两份实际安装版导出，核对 Hasselblad 从主机源切换为设备源再切回时，指南和清理入口按当前设备声明变化。非法字段和重复键重载失败时，页面显示错误并继续按旧有效目录判断；首次加载失败与有效空目录按未知保留相应入口。混合取回按各产物真实来源判断，修复成品和产物状态保留，草稿与已导出原请求保持不变。客户端完整单元和集成门禁已通过，具体结果见[客户端记录](../client/verification.md#设备能力声明的安装版消费验证2026-10-18)。这些软件结果不代替 ARM 部署、真实相机或物理断电验收。

## OSMO 最小延时间隔与两款相机录像启动延迟的现场确认（2026-10-18）

用户现场核实：当前 OSMO 360 II 支持的最小延时采集间隔为 **2 秒**；收到 1 秒间隔参数时，设备按 2 秒解析和执行。因此，此前 Auto 延时请求的 1 秒／300 秒与两路各 151 帧的结果已有设备行为解释，不再将其列为原因未知的帧数差异。原请求仍保存 1 秒，原始负载、调用、文件和媒体测量结果不改写为 2 秒；该现场结论不等于程序已经将原生效参数改为 2 秒，也不构成新增的参数回读记录。Action6 不采用 OSMO 的 2 秒下限。

用户同时确认：Action6 与 OSMO 360 II 普通录像的启动命令执行成功后，设备实际开始录像仍有 **1 秒内的延迟**，因而成片可能比主机目标时长少接近 1 秒。这是用户对当前设备行为的核实；本项没有新增 Agent 拍摄或独立的首帧计时测量，也不把这段延迟固定为恰好 1 秒。既有媒体时长观测继续保留，包括超过目标和短于目标的不同样本。

这两项事实曾暴露型号间隔域和录像启动确认语义的修订项。当前实现已按[启动余量与时间参数设计](../superpowers/specs/2026-10-18-camera-startup-margin-design.md)完成：OSMO 新请求最短间隔为 2 秒，Action6 保留自己的间隔域；新普通录像以可靠命令确认和冻结启动余量安排后续时点，并继续把实际开始时间保留为未知，除非另有设备观察。完整软件门禁、c76 安装快照和 OSMO 定点实机结果见[本轮启动余量验收](#启动余量-tx2-定点验收2026-10-18)。

用户进一步确定：普通录像保留至少达到目标时长、允许略多录的要求；两款相机的普通录像与延时摄影均采用独立的 1 秒启动余量。设备自行计时的延时只在主机自然核实之前增加等待，设备收到的目标时长保持原值。完整规则由[启动余量与时间参数设计](../superpowers/specs/2026-10-18-camera-startup-margin-design.md)维护；实现进度由[启动余量实施计划](../superpowers/plans/2026-10-18-camera-startup-margin.md)维护。

本项保留 2026-10-18 的现场观察及此前原请求证据，不把旧 1 秒输入、负载、文件或媒体结果改写成新 2 秒请求，也不声称该观察独立测得首帧时刻。新实现、软件验证、安装物和当前 OSMO 定点实机范围见[本轮启动余量验收](#启动余量-tx2-定点验收2026-10-18)；Action6 真机验证仍等待固件修复。

## OSMO 360 II 完成依据与正式接入的复核（2026-10-18）

本次复用此前归档证据，核对自然结束的 01／06／09、普通录像的 03／04 和原生延时主动停止的 05。六组保存的日志窗口中取得的是同 boot 的新待机回调，没有找到与具体文件 ID 关联的文件关闭或完整集合通知；这一结论只覆盖已归档窗口，不表示设备其他日志中不存在相应信号。原目录差集、十二份媒体的摘要及解码结果继续保留原通过范围，不据此补造文件此后不再写入或集合已齐的设备保证。

归档的两份 `dji_mb_ctrl -h` 只列出可选的 `-o timeout`，没有给出默认值或单位。既有无 `-o` 的三次原生延时 START 快速返回，证明这些样本的响应没有等到整项采集完成；现有原始调用不足以证明工具默认响应上限。正式接入仍需按明确外层有限期限管理调用，响应未知时不重新启动设备。

当前正式内置合同已登记 OSMO 普通录像和原生延时；安装版 `describe` 与联合业务链已覆盖两类动作。30 秒自然结束余量、独立 30 秒 STOP 文件等待、可选 LRF、低功耗取消收场和双主视频媒体修复按[补充设计](../superpowers/specs/2026-10-18-osmo360ii-capture-completion-design.md)执行。软件验证与实际设备验收分开记录；截至 2026-10-17 16:42 UTC 的前置资料复核时，正式 TX2 业务链尚未完成。此后取得的现场结果见本节下方的[TX2 正式业务验收记录](#tx2-正式业务验收2026-10-18-起)，当前任务状态以[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)为准。

本轮证据入口为本地忽略目录 `.local/acceptance/osmo360-formal-integration-20261017T164203Z/`。主机只读检查的实际 UTC 为 2026-10-17 16:42，OSMO USB／ADB 在线，未见 camctl 或 host-demo 进程；用户随后确认当前待机、供电充足。未发送新设置、START 或 STOP，未重复旧拍摄，未操作 Action6。本项是资料与前置状态复核，不计为新增拍摄、软件测试或正式业务验收。

### 正式软件接入验证（2026-10-18）

主线 `bc0cad2` 的 Linux x86_64／WSL2、CPython 3.11.16 回归结果为：unit `5677 passed, 1 skipped`（26.61 秒）、capture `1357 passed`（580.22 秒）、session `139 passed`（26.98 秒）、history `212 passed`（52.26 秒）、cancellation `103 passed`（13.71 秒）、persistence `123 passed`（4.13 秒），命令均退出 `0`。原始命令、UTC、解释器、提交和退出码记录在 `.local/acceptance/osmo360-formal-integration-20261017T164203Z/software/` 的 `final-*.call.json` 与对应日志中。

针对 `bc0cad2` 的补充回归中，`test_media_assembly.py` 与 `test_osv_media_pipeline.py` 共 `6 passed`（15.36 秒），`host_files` 共 `117 passed`（2.24 秒），修正旧 OSMO 能力断言后的 `devices` 完整目录共 `166 passed`（4.90 秒），均退出 `0`。首次 `devices` 执行的唯一失败来自旧测试仍要求 `describe` 返回 `actions=[]`；只更新该测试预期后重跑，生产代码未改。完整命令和日志记录在 QA 工作区 `/opt/devcontainer/.codex/worktrees/osmo-final-bootstrap/camctl/.local/verification/osmo-final-bootstrap/` 的 `manifest-bc0cad2.json`、`manifest-bc0cad2-devices-corrected.json` 及各自日志中。

完整 `bootstrap` 目录在冻结快照 `74b010c` 上取得 `1174 passed, 1 skipped`（1043.25 秒，退出 `0`）；这项结果只属于该快照，不记作 `bc0cad2` 的全目录结果。原始完整日志和清单位于 QA 工作区 `/opt/devcontainer/.codex/worktrees/osmo-final-bootstrap/camctl/.local/verification/osmo-final-bootstrap/bootstrap-74b010c.log` 与 `manifest-74b010c.json`。`bc0cad2` 上另有针对媒体路径的两个 bootstrap 文件，完整日志见该 QA 工作区；安装版 OSMO 联合链日志及运行清单见 `/workspaces/camctl/.local/acceptance/osmo360-formal-integration-20261017T164203Z/software/cross-final/` 和下文跨组件记录。

跨组件实测中，候选链 `test_real_camera_demo_roundtrip.py` 为 `4 passed`（pytest 60.74 秒），通用 C host 链 `test_camctl_c_module_roundtrip.py` 为 `3 passed`（pytest 35.04 秒），均在生产代码快照 `bc0cad2` 运行。OSMO 安装版链 `test_osmo_builtin_timelapse_roundtrip.py` 在 `13482ca` 快照取得 `4 passed`（pytest 91.56 秒，退出 `0`）；该快照以 `bc0cad2` 为生产代码基线，后续提交只改集成测试。安装版正式 `describe` 导出普通录像和原生延时，业务组合覆盖普通录像配 LRF、原生延时配 LRF 及不配 LRF；其 ADB 与时间边界及客户端 `Files.upload` 验收范围见[跨组件集成说明](../../tests/integration/README.md#osmo-360-ii-正式内置合同安装版联合链)。OSMO 安装版链完整日志与运行清单保存在 `/workspaces/camctl/.local/acceptance/osmo360-formal-integration-20261017T164203Z/software/cross-final/`；对应 QA 原始清单在 `/opt/devcontainer/.codex/worktrees/osmo-final-cross/camctl/.local/verification/osmo-final-cross/test_osmo_builtin_timelapse_roundtrip-green.log.meta.json`。

公共协议检查 `node scripts/check-protocol.mjs` 于 2026-10-17 19:50:46 UTC 在 `f66d296` 退出码 `0`。检查覆盖 4 份 Schema、27 份报告及摘要、19 份计划、5 份能力说明、166 个结构正反例、23 个动作错误编号及详情，并通过电机共同夹具 76 例和视频大小估算共同夹具 246 例；边界说明未声称运行生产受理、客户端合并或设备联调。原始输出及命令清单见 `.local/acceptance/osmo360-formal-integration-20261017T164203Z/software/final-protocol.stdout`、`final-protocol.stderr` 与 `final-protocol.call.json`。

跨组件候选链首次运行前发现工作区缺少被忽略的生成包资源，四个用例因此以 `registry/enum-registry.json` 未登记失败；保留的首次日志为 `/workspaces/camctl/.local/acceptance/osmo360-formal-integration-20261017T164203Z/software/cross-final/test_real_camera_demo_roundtrip.log`。执行 `apps/camctl/scripts/sync_resources.py` 生成 27 项资源并通过资源检查后，同一候选命令重跑为绿色；该故障属于工作区资源准备，不作为生产代码失败，也不隐藏首次诊断。本节记录的软件结果不替代 ARM 安装或真实 OSMO 拍摄、取回与 ACK 验收；本节所列软件门禁未操作相机，也未操作仍暂停的 Action6。软件实现和进度由[完成契约实施计划](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md)维护；TX2 实测证据见下节。

### TX2 正式业务验收（2026-10-18 起）

本轮在原业务状态上安装并运行正式 OSMO 能力，任务 6 规定的 TX2 业务验收场景现已完成。验收覆盖 Auto EV 0 与 Manual ISO 800 普通录像、Auto 原生延时、低功耗中断恢复和有限取消；各轮的独立交付、媒体核验、客户端导入与 ACK 见下文。当前场景及结论范围由[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)维护；Action6 未访问且继续等待固件修复。

目标运行账户为 `cld`。保留现有 `state.db`，先备份 `arm-deployment`，未重新执行 `init`。从生产快照 `bc0cad2` 构建并安装的 wheel SHA-256 为 `bc04f3aed94691d648579417b817aec352fbc91f5aaed655fe262f7270e20da2`；安装文件与 wheel 内文件逐字节一致。实际运行环境为 Python 3.11.17、SQLite 3.53.1；现场从当前源码在 ARM 上构建的 C host SHA-256 为 `2889f129dfa6abe1346b30584fbdd21eb23a8027e026256be75976b898400461`。正式 `describe` 导出普通录像和原生延时两项能力。两款设备使用相同 serial，现场按 USB `2ca3:0020` 及型号／固件信息确认 OSMO 360 II；Action6 未访问。

#### Auto EV 0、10 秒普通录像首轮

普通录像请求 `7611011678869730489` 对应 action 7，只发送一次 START 和一次 STOP，二者均收到 `00`。STOP 后实际等待 `30000662038 ns`。相机新生成 `0038.OSV`，大小为 `226150703` 字节；媒体包含两路 `3840×3840` HEVC，各 305 帧、10.176833 秒，以及 10.154667 秒 AAC。所有音视频流均完整解码退出 `0`。

独立取回请求 `6596954726306245016` 创建 action 8，使用拍摄动作已登记的 output 3 创建 delivery 3，交付文件为 `3.OSV`。相机源文件、TX2 副本和本地领取文件 SHA-256 均为 `e2a00d948e2b1db291deafc7560a6d10a368eee38798c5b4a30444c0d7428cb2`。客户端实际调用 `Files.upload` 导入视频并核验报告；沿同一取回请求以新 ACK 重送后，action、output 和 delivery 数量未增加，report 32 与 watermark 90 已吸收，原有 5 个动作保持不变。

首轮还观察到 `0038.LRF` 存在，`stat` 大小为 `20538481` 字节，但原 capture 的结果扫描使用 10 秒调用预算，读取 `preview_directory` 超时，因此该文件没有登记为正式 output，也没有建立配对关系。这表示本次可选预览读取未完成，不表示相机没有生成 LRF。之后已将仅 OSMO 的 `result_check.call_timeout_s` 配置为 60 秒，保留变更前配置备份；首轮结果仍按原 10 秒预算记载，不回填登记、不重拍。该次只读检查及配置调整见同一证据目录中的 `01-record-preview-readonly.stdout` 与 `result-timeout-deployment.stdout`。

#### Auto 1 秒／300 秒原生延时

请求 `3606871195239330004` 对应 action 9、activity 4。执行只发送一次 START，没有发送 STOP；保存的 `SENT` 状态没有 `started_at`，按 `TIME_AND_OUTPUTS` 完成，结果读取一次并实际使用 60 秒调用预算。拍摄 action 9 为 `SUCCEEDED`，本轮活动状态没有独立设备状态回读；`TIME_AND_OUTPUTS` 判定仅按保存的时间与产物规则完成结果登记并释放占用。可靠等待与结果依据来自原等待 event 359 及 result page 361–364。

结果核实取得同 stem `0039` 的正式文件：output 4 为 LRF、大小 `11222691` 字节；output 5 为 OSV、大小 `196560036` 字节。原片和预览通过可靠事件建立配对。独立取回后，OSV 为 delivery 4（`4.OSV`），LRF 为 delivery 5（`5.LRF`）；两份文件在相机源、TX2 副本和本地领取副本之间的长度与 SHA-256 均一致。OSV 摘要为 `195967172765ccfa9a8af9a415e4c8a62b3bca8f8b941e5723da6c4b55b35d0d`，LRF 摘要为 `b0aa9663f3aba22c5adb5d08be159700c8e0ca8745a79144f6c45ada69de109a`。两份文件均经真实客户端 `Files.upload` 验证并导入，ACK 已吸收 report 45、watermark 132；验收前后的 boot ID 相同。

Auto 1 秒／300 秒这一轮的 OSV 有两路 HEVC 主视频，各 151 帧、5.038367 秒，没有音频；配对 LRF 有一路 `1920×960` HEVC、151 帧、5.038367 秒。离线 `ffprobe -count_frames` 对该轮 OSV 两路主视频分别核得 `nb_read_frames=151`，所有实际媒体流解码退出 `0`。请求目标为 1 秒间隔／300 秒采集，但没有独立读回实际采样间隔，这一轮的 151 帧不能据此解释为实测间隔，也不能据此宣称 1 秒间隔设置已被独立证明。

#### Manual ISO 800、10 秒普通录像

请求 `990542672445894479` 对应 action 12，只派发一次 START 和一次 STOP，二者均返回 `00`。STOP 后实际等待 `30001504101 ns`；结果检查使用 60 秒调用预算，并以正式结果事件登记同 stem `0040` 的原片与预览。OSV 为 output 7、delivery 6（`6.OSV`），大小 `245294138` 字节，SHA-256 为 `31e7663aee468cf68d35ad016e568ec335e747366684047ca0299644566ea196`；LRF 为 output 6、delivery 7（`7.LRF`），大小 `22046866` 字节，SHA-256 为 `841e5b23a18e4b81f235e5e86453923dabfbd9f8e4baa7cf5d7ad48622843e8c`。两份文件在相机源、TX2 副本与本地领取副本间的长度和 SHA-256 一致。

OSV 有两路 `3840×3840` HEVC 主视频，各 331 帧、11.044367 秒，并含 11.050667 秒 AAC；LRF 有一路 `1920×960` HEVC、331 帧、11.044367 秒，并含 11.050667 秒 AAC。记录的音视频流均完整解码退出 `0`。客户端实际通过 `Files.upload` 导入并验证两份文件；ACK 已吸收 report 59、watermark 174，验收前后 boot ID 保持 `f6f90cbf`。

低功耗中断恢复与有限取消场景均已完成。每轮沿原请求核实，不以新请求替代未知结果；目标和取消 action 的终态分别记录，不混同有限收场、停止结果和目标采集结果。Action6 继续等待固件修复，本轮未访问该设备。

#### 30 秒／300 秒低功耗进程中断恢复

请求 `646068998638568558` 对应 action 15、activity 6，参数为 Manual ISO 800、30 秒间隔／300 秒视频。可靠 `SENT` 保存时 `started_at` 为空，baseline event 为 `643..643`、`expected_check_at=1792268040281689`，结果余量为 30 秒且未使用额外等待；task key、有效参数和执行规格在进程切换前后保持一致。确认相机 USB 断开且自 `SENT` 起经过 `45.484195` 秒后，才结束原 host PID 476 和 CLI 进程组 9030；新 C host PID 9181 使用原状态库、配置和 FIFO 恢复并重送同一请求。该任务只尝试一次 START，未运行 STOP；拍摄 action 最终 `SUCCEEDED`，但 `TIME_AND_OUTPUTS` 的 activity 状态仍为 `unknown`，没有创建新拍摄 action。

独立取回的 OSV 为 output 9、delivery 8（`8.OSV`），大小 `17350298` 字节，SHA-256 为 `048e863e519a3a7b3acc38637b4b01a6bb44ed66716f3f3328ef3d625e84782d`；配对 LRF 为 output 8、delivery 9（`9.LRF`），大小 `3127250` 字节，SHA-256 为 `06855a5ca7347da4d65720ccaba66e212f947201b10f6744bf2585d695d01d73`。两份文件在相机、TX2 和本地副本间的长度与摘要一致，OSV 双主视频和 LRF 视频各 11 帧，所有实际媒体流解码退出 `0`。客户端验证并导入文件；原取回请求 `1178142317757859341` 的 ACK 吸收 report 71、watermark 216，原拍摄请求仍只有一个 action，boot ID 未改变。

有限取消以 request `7086359445954081151`（目标拍摄 action 18）和取消 request `2720512708612506655`（action 19）执行。原 `SENT`、task key、baseline event `785..785`、`expected_check_at=1792268478845531`、30 秒余量及 0 额外等待均保持不变。低功耗窗口内 STOP run 27 / attempt 27 只执行一次，随后以本地退出码 `1` 失败，错误为 `osmo360ii_response_unconfirmed`，stderr 为 `error: device '123456789ABCDEF' not found`；`attempts_used=1`、`retry_wait_required=0`，没有重发 START 或重复 STOP。等待期间目标和取消 action 仍运行、占用仍保持；原有限收场随后由 results run 28 / attempt 28 读取成功结果页并保存 `FINITE_CANCEL_END` 依据。最终目标 action 18 为 `CANCELED`，取消 action 19 为 `FAILED/cancel_items_failed`（错误码 `23`）。activity 7 为 `ENDED/RELEASED`，但 `completion_basis=UNDETERMINED`、`result_set_state=UNEXAMINED`，`capture_json` 与 `result_check_json` 均为空；该结果只结束有限收场责任，不证明目标拍摄完成或 STOP 成功。

取得的同 stem `0042` 文件为 OSV output 11 / delivery 10（`10.OSV`），大小 `17506313` 字节，SHA-256 `744e9f2e2bceec4a11abb4b664ae0f5b2088fa8e61191af293c8d9fd1cb9d655`；LRF output 10 / delivery 11（`11.LRF`），大小 `3153436` 字节，SHA-256 `897467cdf5be27ed08aaf03c1dca8d9bfc76b546733202351b7efb6a0e0ac805`。两路 OSV 主视频与 LRF 主视频均为 11 帧；实际媒体流完整解码退出 `0`。原片和预览均保留，相机源、TX2 副本和本地交付文件的长度与 SHA-256 一致；真实客户端 `Files.upload` 均验证通过。原 preview obtain request `8595314106976216469` 吸收 report `86`、watermark `267`，包含一个原请求 action；相机 boot ID 保持 `f6f90cbf`。

最终只读状态检查的 SQLite `quick_check` 为 `ok`，USB／ADB 在线且 camera boot ID 不变；21 个 action 均为终态，7 项设备占用均已释放，30 个 operation run／attempt 保留，11 个 output 与 11 个 delivery 登记在库。既有 action 1—5 的状态未变。最终归档的 59 份文件共 `10957808` 字节，逐文件长度和 SHA-256 均与远端核对一致。Action6 继续等待固件修复，本轮未访问该设备。

本轮忽略证据目录为 `.local/acceptance/osmo360-formal-integration-20261017T164203Z/`；普通录像概要为 `01-record-summary.json`、`02b-record-manual-summary.json`，Manual 过程记录为 `02b-record-manual-watch.stdout`；延时观察及交付概要分别为 `02-timelapse-watch.stdout`、`02-timelapse-summary.json`，帧数复核为 `02-timelapse-frame-count.stdout` 与 `02-timelapse-frame-count.call.json`；恢复连续性和交付分别见 `03-recovery-continuity.json`、`03-recovery-summary.json` 与 `03-recovery-watch.stdout`；有限取消事实见 `04-cancel-continuity.json`、`04-cancel-target-terminal.json`、`04-cancel-command-terminal.json`、`04-cancel-events-readonly-v2.stdout` 和 `04-cancel-summary.json`。最终状态与远端归档逐文件核对见 `final-state.stdout`、`final-remote-evidence/final-state.db`、`final-remote-evidence-integrity.json`。原始请求、日志及配置备份支撑本节细节；通过范围限于本计划所列 OSMO 场景。

### 启动余量 TX2 定点验收（2026-10-18）

本节记录独立的启动余量软件、ARM 安装与五轮 OSMO 实机验收。软件门禁分别运行于具名快照；源码等价复核确认其适用生产代码对应当前交付快照 `c76e496059aff067a7488e555a305167faaecfab`，不将不同快照描述为同一次运行。

#### 软件门禁

全量 unit 在 `b5b8c5c7dcf06a22bcfb7399d26f0abedc45ad3c` 取得 `5711 passed, 1 skipped`（31.37 秒，退出码 0）；完整 capture 在 `1e04c647ea4424e097c93046ee7d553eeef4cf50` 取得 `1872 passed`（814.42 秒，退出码 0）。完整 bootstrap 在 `209cfae6940f558a7e1d71b8c78bfde25172dfda` 取得 `1175 passed, 1 skipped`（1201.62 秒，退出码 0）；同一快照的六个独立集成目录 cancellation、session、persistence、history、devices、host_files 分别为 103、139、123、212、166、117 项通过，合计 860 项，均退出 0。Task 3 的五项跨组件门禁在 `1c3c72261c8b4c3218d6c6b97df3ad1f80c1b6a8` 分别通过 4、3、3、4、1 项，合计 15 项，均退出 0；生成资源和协议检查均退出 0。独立快照、运行收据和源码等价依据分别保存在本轮持久证据目录 `.local/acceptance/camera-startup-margin-20261017T215355Z/software-validation/sdd-software/` 的 `final-branch-review.md`、`final-capture-gate.md`、`final-core-gates.md`、`bootstrap-verification.md` 与 `task-3-report.md`；软件原始归档位于本轮 `.local/acceptance/camera-startup-margin-20261017T215355Z/software-validation/`。

#### ARM64 安装

`deployed.json` 记录安装来源为 `c76e496059aff067a7488e555a305167faaecfab`。安装 wheel 的 SHA-256 为 `90c8b4a242fe17da5f5779656af999ef96ba5e7922b30090b081289a36714628`；复用的 ARM C host SHA-256 为 `2889f129dfa6abe1346b30584fbdd21eb23a8027e026256be75976b898400461`。正式 `describe` 的 SHA-256 为 `b9927505734d7584b2e8da98c5365b0657dfd91e3a685311f1fd90afbdcb3a7e`，安装后描述摘要相同、退出码为 0，并报告 OSMO 最小间隔 2 秒。目标运行时为 Python 3.11.17、SQLite 3.53.1；两款设备使用同一 serial，现场通过 USB product 与型号信息确认 OSMO 360 II，未访问 Action6。安装清单和 describe 记录见 `.local/acceptance/camera-startup-margin-20261017T215355Z/deployed.json`。

#### 五轮实机结果

- **Auto 普通录像，目标 10 秒。** 两路主视频各为 15.982633 秒。START 与 STOP 控制调用分别耗时 7.704439 秒和 2.802223 秒；STOP 意图相对原确认点 A 为 11.372831 秒。这些是控制时序，不是首帧时刻测量，也不证明首帧延迟恰好为 1 秒。
- **Manual ISO 800 普通录像，目标 10 秒。** 两路主视频各为 11.678333 秒；STOP 意图相对 A 为 11.134951 秒。此时间仍表示控制时序，实际首帧时刻未独立测量。
- **Auto 原生延时，输入间隔 2 秒、采集时长 300 秒。** 唯一 START 后未派发 STOP；首次 RESULTS 意图在 A 后 331.536804 秒。OSV 两路主视频各 151 帧，播放时长各为 5.038367 秒；这段媒体播放时长不表示 300 秒采集时长，也未独立读回相机实际采样间隔。
- **Manual ISO 800 恢复，输入间隔 30 秒、采集时长 300 秒。** 在原预计核实点 Q 之前，对本轮自有 C host 进程及 CLI 进程组发送 `SIGKILL`（强制终止信号），随后重启 C host；没有重启 TX2。恢复沿原 A/S/D/R/E、action/activity/run/attempt/key/baseline 和唯一 START 继续，并等待原 Q，没有从 host 重启时点重新等待 331 秒。完成等待与首次 RESULTS 意图分别晚于原 Q 0.296444 秒和 0.325541 秒；时序和进程操作收据见 `.local/acceptance/camera-startup-margin-20261017T215355Z/03-recovery-cut.call.json`、`.local/acceptance/camera-startup-margin-20261017T215355Z/03-recovery-cut.stdout`、`.local/acceptance/camera-startup-margin-20261017T215355Z/03-recovery-host-restart.call.json`、`.local/acceptance/camera-startup-margin-20261017T215355Z/03-recovery-host-restart.stdout` 与 `.local/acceptance/camera-startup-margin-20261017T215355Z/03-recovery-continuity.json`。
- **Manual ISO 800 有限取消，输入间隔 30 秒、采集时长 300 秒。** A 后 45.452824 秒观察到 USB 断开，之后才安排并提交取消；该观察时点不作为取消生效时刻。唯一 START 与唯一 STOP 均保留，STOP 原结果为设备断开错误。目标 action 34 最终 `CANCELED`，取消 action 35 最终 `FAILED/error_code=23 (cancel_items_failed)`；活动 `ENDED/RELEASED`，但 `capture_json` 为空、完成依据为 `UNDETERMINED`、结果集为 `UNEXAMINED`。有限收场结束不代表目标采集成功或 STOP 成功。完成等待与首次 RESULTS 意图分别晚于原 Q 0.027148 秒和 0.053219 秒。

五轮共 5 份原片和 5 份实际预览，合计 10 份，均分别取回；相机源、TX2 副本和本地副本摘要一致。FFmpeg 对每份文件分别映射 `0:v` 和可选的 `0:a?`，所有实际音视频流均完整解码通过，10 次解码退出码均为 0 且 stderr 为空；data 流不在该解码结论内。客户端 `Files` 导入和累计 ACK 通过。一次客户端 helper 在 export、submit 和 START 之前遇到 schema 拒绝；原始诊断保留在本轮证据目录，不计入五轮业务结果。

#### 原状态保全、归档与范围

最终状态核对确认旧数据库九张业务表的原有行逐字段不变：plans/actions 各 21 行，activities 7 行，files/outputs/deliveries 各 11 行，runs/attempts 各 30 行，copies 11 行。原有 935 条 history_events 和 704 条 history_transactions 记录保持不变；最终分别有 1700 条和 1281 条记录。原 9 份 OSMO 源文件共 742411731 字节且摘要不变，配置原字节及相机 boot ID 均保持不变。最终数据库 SHA-256 为 `08aed14ac2fa0884db2c78d458e8dc3907cc023bec1daf4ad4d0bc0d138d3a80`。最终状态为 37 plans、37 actions、12 activities、21 files、21 outputs、21 deliveries、53 runs、53 attempts；新增五项 activity 均 `RELEASED`，新增 16 项 action 中除上述 action 34、35 外均 `SUCCEEDED`。

远端完整归档已保存到本地，77 个普通文件逐项摘要核对通过；归档 SHA-256 为 `c2a3030dc2d78e0193969a51c19d881af5437311d75303616017ea0756e5cb79`。收尾仅停止本轮自有 host 与 USB monitor，保留共享 ADB server，收尾前没有受管 CLI 子进程。最终状态、清理和归档审计分别见本轮目录的 `final-state.json`、`final-cleanup.json` 与 `final-remote-archive-audit.stdout`。

本轮只验证所列五种 OSMO 场景。它不覆盖全部间隔组合、成像质量、物理断电、曝光 ISO 独立回读或精确首帧时刻；启动余量和参数完整契约以[批准规格](../superpowers/specs/2026-10-18-camera-startup-margin-design.md)为准。Action6 未访问，仍等待固件修复。

### 既有 OSV 的离线双流裁剪核验

补充设计等待用户审阅期间，在 x86_64 开发环境使用 FFmpeg 6.1.1 和既有 0033 OSV 副本核对真实媒体处理行为，没有操作相机或修改生产代码。仅使用默认流选择、`-c copy -t 5` 生成的对比 MP4 只有一路主视频，说明默认选择不能承担双路视频修复。

显式映射原文件的两路主视频及音频后，独立 MP4 中保留两路 `3840×3840` HEVC Main 10，各 150 帧、5.005000 秒，以及一条音频；容器时长为 5.013333 秒。两路视频各 150 个包及音频 235 个包的压缩数据 SHA-256 均与原流对应前缀一致，全部视频／音频流严格完整解码退出 0。原 OSV 在处理前后的长度及 SHA-256 一致。本项只证明该样本的无重新编码媒体内容处理，不证明厂商原生编辑兼容、未知元数据保留或其他裁剪边界。

随后在 TX2 ARM64 上使用其已有的同一 OSV 副本和 FFmpeg 3.4.11 复核这条媒体路径，仅执行主机文件与工具操作。裁剪、结构读取和全部视频／音频流严格完整解码均退出 0；生成的双路 HEVC 各 150 帧、5.005000 秒，AAC 音频及容器时长为 5.014000 秒。原 OSV 处理前后长度与 SHA-256 一致。这项结果补充了目标主机旧版工具的实际行为，不计为相机拍摄或正式 camctl 媒体处理链通过。

当时的静态核对还确认：`MediaProbe` 只返回单个容器时长，无法表达双路编码、尺寸、附加图标记或逐路时长；生产修复适配器只追加 `-c copy -t`，没有明确流选择。该软件缺口之后已由正式实现补齐逐流探测、事实持久化、修复流映射及输出校验，软件范围见[完成契约计划任务 3](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-3逐流媒体事实与修复)及[正式软件验证记录](#正式软件接入验证2026-10-18)。本段的既有 OSV 离线样本只证明该样本的媒体内容处理行为，不验证厂商私有编辑兼容或当前 TX2 正式修复链。

新增证据位于上述本轮目录的 `offline-dual-stream-repair-01/`，开发环境入口为 `summary.json`，TX2 入口为 `arm-evidence/summary.json`；每次工具调用另存 argv、UTC、耗时、退出码及原始输出。15 份 TX2 证据与取回副本的长度及 SHA-256 全部匹配，清单为 `arm-evidence-integrity.json`。它是既有媒体的离线工具可行性证据，不计为新增真机拍摄、正式驱动测试或完整业务验收；完成和取消运行假设的适用范围由上述补充设计维护。

## OSMO 360 II 响应处理与可用接口的用户确认（2026-10-17）

用户对本日真机结果补充以下执行依据：延时 Auto 的 `1e 0000` 返回 `ee` 暂按正常响应处理，用户后续向官方核实；重复原生延时 STOP 返回 `df` 正常，重复 STOP 没有副作用；当前没有可供使用的参数回读接口或媒体解析说明。当前命令与上下文的处理范围统一由[OSMO 响应处理与可用接口](timelapse-command-encoding.md#osmo-响应处理与可用接口)维护。

以上是用户提供的当前执行依据，`ee` 尚未取得官方核实。后续可继续独立 Auto 采集及接入验证，不再把 `ee`、重复 STOP 的 `df` 或要求提供不存在的回读接口列为等待用户处理的阻塞。原试验因当时门控而停止、没有发送后续命令的事实仍保留；确认响应正常不将旧试验改判为已经拍摄，也不补造参数实效、设备完成或文件完成证据。Action6 的固件修复暂停范围不变。

## OSMO 360 II 全量真机验证（2026-10-17）

本次通过 SSH 在 TX2 执行普通录像及原生延时的候选命令验证，覆盖当时可用候选参数中的功能与曝光分支，时间组合选代表值。Auto 已按[用户执行依据](#osmo-360-ii-响应处理与可用接口的用户确认2026-10-17)完成独立采集和媒体完整性核验。**截至本候选验证轮结束时，全量验收尚未完成：正式 OSMO 任务工厂及业务链尚未接入。** `ee`、重复 STOP 的 `df` 和可用接口范围按当时适用约定处理。已通过的 1 秒／300 秒 Manual 最短手工演示保留原现场验收范围，没有为补材料重复拍摄。Action6 继续按其固件修复门槛暂停，本轮未操作 Action6；当前软件及正式 TX2 状态见[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)和[正式现场验收记录](#tx2-正式业务验收2026-10-18-起)。

环境为 TX2 ARM64、Python 3.11.17；相机 USB 为 `2ca3:0020`，产品为 OSMO 360 II，ADB product 为 `eagle4_oq102`，固件读数为 `ro.dji.build.version=10.00.00.57`、`persist.plt.external_version=01.01.02.10`。预检内置存储约 105 GB 可用；当前 OSMO 的目录范围为内置 DCIM，没有 SD DCIM。可选电池 sysfs 节点不存在的预检错误已保留，用户确认相机待机且供电充足。安装版 `describe` 对 `dji-osmo360-ii` 仍返回 `actions: []`。

### 控制、连接恢复与媒体结果

各次 START 均省略 `-o`，每个实际拍摄用例只启动一次。较长间隔自然采集的等待期只记录 TX2 主机 USB sysfs、udev 和内核日志，不运行相机 ADB 轮询、保活或 STOP。目标 300 秒后额外观察 30 秒，再做一次结束状态读取；这个窗口是本轮观察安排，不是设备完成、文件写完或恢复期限的保证。结束状态分别核对原 boot ID 和晚于本次 START 的新待机日志，再读取目录差集；主动停止用例另核对同 boot 下的采集状态及晚于 STOP 的待机日志。

| 用例 | 实际控制及状态 | 文件与核验结果 |
| --- | --- | --- |
| Manual ISO 800 延时，30 秒／300 秒，仅视频 | 九条设置及 START 各返回唯一 `00`；START 约 0.254 秒返回。主机一秒采样在 START 后约 36.602 秒见 USB 离线，短暂重现后再次离线，约 306.603 秒见恢复。330 秒状态读取成功，同 boot、新待机日志；用户确认自动待机且期间无手动操作。 | 0032 OSV 为 17,426,590 字节，LRF 为 3,115,374 字节。主视频双路 `3840×3840` H.265，各 11 帧，媒体时长 0.367033 秒。 |
| Manual ISO 800 延时，60 秒／300 秒，仅视频 | 九条设置及 START 各返回唯一 `00`；START 约 0.251 秒返回。主机一秒采样约 66.478 秒见 USB 离线、306.479 秒见恢复；330 秒状态读取成功，同 boot、新待机日志。本例待机依据为设备日志，未另行取得屏幕观察。 | 0036 OSV 为 11,150,869 字节，LRF 为 2,783,983 字节。主视频双路 `3840×3840` H.265，各 6 帧，媒体时长 0.200200 秒。 |
| 首次 Auto 延时，2 秒／300 秒，仅视频 | 前四条设置为 `00`，第五条 `1e 0000` 返回 `ee`；当时的逐条 `00` 门控停止，未发送 `6c`、START 或 STOP，没有重试。 | 未进入采集，不产生本次拍摄媒体；后续用户已暂按正常响应接受，旧试验的未采集事实不改写。 |
| 用户确认响应规则后的独立 Auto 延时，2 秒／300 秒，仅视频 | 六条设置中只有 `1e 0000` 返回 `ee`，按用户暂定规则继续，其余设置及唯一 START 为 `00`；START 约 0.223 秒返回。330 秒时读取到同 boot 的新待机日志，360 个主机 USB 样本未见节点离线；没有采集中 ADB 轮询、STOP 或重试。 | 0037 OSV 为 196,545,045 字节，LRF 为 11,203,692 字节。主视频双路 `3840×3840` H.265，各 151 帧，媒体时长 5.038367 秒；请求的实际 Auto 效果没有独立回读。 |
| 普通录像 Manual ISO 800，主机目标 10 秒后停止 | START、STOP 各返回唯一 `00`；START 约 1.716 秒返回。停止前为录像状态，停止后有新待机日志，boot ID 相同。 | 0033 OSV 为 247,982,780 字节，LRF 为 22,236,131 字节。主视频双路 `3840×3840` H.265，各 335 帧，媒体时长 11.177833 秒。 |
| 普通录像 Auto EV 0，主机目标 20 秒后停止 | START、STOP 各返回唯一 `00`；START 约 2.254 秒返回。停止前为录像状态，停止后有新待机日志，boot ID 相同。 | 0034 OSV 为 458,207,622 字节，LRF 为 39,441,613 字节。主视频双路 `3840×3840` H.265，各 624 帧，媒体时长 20.820800 秒。 |
| Manual ISO 800 延时，2 秒／300 秒，约 10 秒主动停止 | START、原生 STOP `01 00` 各返回唯一 `00`；停止前为延时采集状态，停止后有新待机日志，boot ID 相同。 | 0035 OSV 为 7,519,360 字节，LRF 为 2,636,419 字节。主视频双路 `3840×3840` H.265，各 3 帧，媒体时长 0.100100 秒。该用例验证主动停止，没有等满 300 秒。 |
| 上一轮已停止并完成媒体验证后，在待机时重复一次原生延时 STOP | 前置日志为待机，同 boot；`01 00` 返回 `df`，本地退出 0、stderr 为空。当时的门控停止，未重试；用户随后确认仍待机，期间无手动操作。 | 原始响应保留；用户进一步确认这是正常的重复停止响应，重复 STOP 没有副作用，后续按当前响应约定处理。 |

补充 Auto 用例前，08 独立试验在只读预检中未取得最近的待机回调而退出，设置与 START 均为零次，未开始本次采集；其主机观察器已停止并收场。用户随后确认已点亮相机、处于拍摄待机界面，09 使用新请求保存该前置依据，并在日志取得待机回调后仅启动一次。08 的原始预检及零次控制事实没有被覆盖。新诊断的特定响应门控还通过了 13 项本地无设备边界检查，该检查不代替正式驱动测试。

上述 0032—0037 的主视频和预览文件共 12 份，均逐个验证相机源文件在复制前后的长度和 SHA-256，TX2 与开发机副本一致；本地 `ffprobe` 解析和全部视频／音频流的严格完整解码通过。原片继续保留。这些是本候选验证轮的副本与源观测结果，不建立之后不再写入或产物集合齐备的保证；主机停止目标和实际媒体时长分别记录，不声称得到精确 10 秒或 20 秒成片。30 秒与 60 秒间隔的 USB 恢复采样均在 START 后约 306.5 秒，不将它与 300 秒目标的差值解释为精确的“设备采集结束后恢复延迟”。

### 参数生效与未完成范围

表中的 ISO、Auto 和 EV 均为请求参数，本轮没有独立确认这些曝光设置的实际效果，0034 的 Auto／EV 0 也未取得独立回读。普通录像中的 `simulate_device` 设置只取得工具退出 0，没有设备参数确认。ExifTool 检查 0032 和 0033 时提示未知 `dvtm_OQ102.proto`，未解析出 ISO、快门等逐帧参数；不能用请求、`00` 或媒体解码替代这些参数的实际生效证据。0033 两路抽帧已保存，一路大部视野被近物占据，另一路可见办公室；本次没有采用标准场景验收光学清晰度或全景拼接。

本候选验证轮没有运行全部 134 种时间组合，也没有由代表值推导最长间隔／时长的设备支持。FOV 的全景适用性、增稳与光圈接口仍按原资料缺口处理，不作为已支持分支通过。未执行物理断电、拔线或媒体删除。重复 STOP 的安全性已由用户确认；默认工具响应等待的完整边界、正常／取消／异常结束的适用规则、单文件完成及集合齐备的正式契约继续由 [D2](../superpowers/plans/2026-10-16-osmo360ii-timelapse-driver.md#d2-明确返回结束及产物契约) 跟踪。没有参数回读接口的事实按当时边界保留，不因此要求新增接口。**截至该候选验证轮结束时，D3／D4、E2／E3 的正式接入及业务验收尚未完成，普通录像也缺少独立正式合同与交付计划；此为当时的状态，不代表当前接入进度。** 当前实现与 TX2 验收进度见[完成契约计划任务 1—6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md)和[TX2 正式业务验收记录](#tx2-正式业务验收2026-10-18-起)。原生命令成功样本不计为正式 `camctl`、C host、客户端、独立取回或 ACK 联合验收。

原始证据位于本地忽略目录 `.local/acceptance/osmo360-full-20261017T144805Z/`，入口为 `REPORT.md`、`coverage-matrix.json`、`commands-executed.json`、`remote-final-01`—`remote-final-09`、`media-audit-*` 及 `01-observation-summary.json`／`06-observation-summary.json`／`09-observation-summary.json`；远端保存在 `/home/cld/.camctl/osmo360-acceptance/full-20261017T144805Z/`。每次调用保存实际 argv、UTC 与单调时钟、退出码、stdout 和 stderr；原始试验与失败不覆盖。补充 Auto 验证后的主机只读检查时 USB／ADB 在线，没有本轮探针进程残留；602 份远端证据与开发机副本的长度和 SHA-256 全部匹配，清单为 `evidence-integrity-after-user-policy.json`。首批 492 份校验记录仍按当时范围保留。

## Action6 长间隔延时固件缺陷与验证暂停（2026-10-17）

用户亲自验证后确认：Action6 当前固件存在缺陷，进入长间隔延时摄影后，ADB 不会像 OSMO 360 II 一样在采集完成后自行重连。该问题按用户真机确认登记；内部实现原因、此次故障固件的现场版本读数、修复版本及发布时间未提供。本次没有新增 Agent 设备调用或媒体核验。

此前交付给用户的手工用例为 30 秒间隔、300 秒（5 分钟）采集、仅视频、手动 ISO 800；本次结论采用用户现场确认，没有归档此次逐条调用、精确恢复观察时长或新增媒体测量值。原始试验、失败记录、原片及合法状态库继续保留，不把 OSMO 的低功耗与自动重连行为外推给 Action6。

用户要求 Action6 真机验证等待固件修复后再进行。暂停范围、恢复前提及未完成项由[T9 当前安排](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)维护；当前固件问题不表示此前未完成的正式延时合同、完整业务验收或软件占用释放缺陷已经解决。

## OSMO 360 II 延时低功耗与 ADB 恢复的现场确认（2026-10-17）

用户现场排查确认：OSMO 360 II 使用较长延时间隔时，例如 30 秒，首次间隔到达进行拍摄时会进入一种特殊低功耗模式，观察到功率显著降低；此后随拍摄间隔反复出现这一行为，直到采集完成。期间 ADB 因低功耗模式断开，拍摄完成后约 5 秒恢复连接。这是用户对当前设备和场景的现场确认；内部模式名称、功率数值、进入条件的完整范围及精确恢复时序未归档，不扩展为所有固件或时间组合的保证。

此前本日的独立诊断请求为手动 ISO 800、30 秒间隔、300 秒采集、仅视频。九条设置和一次 START 均退出 0、stderr 为空，并分别返回唯一 `00`；START 调用约 0.240 秒返回。主机记录到 START 调用开始后约 41.359 秒的整机 USB 节点移除，之后出现短暂重枚举和再次移除。用户在等待期间确认相机仍在延时采集；等满 300 秒后的唯一状态读取报 `device not found`，未执行后续目录读取、STOP 或重试。原始材料保存在本地忽略目录 `.local/acceptance/osmo360-tx2-20261017T124157Z/`，入口为 `remote-final/` 和 `independent-audit/REPORT.md`。

用户的后续现场确认补充了该低功耗场景下断连及自动恢复的原因，不能将采集中 ADB 不可用直接判为拍摄失败。约 5 秒是连接恢复的观察值，不是文件写完、产物集合齐备或固定等待上限的保证；当前运行假设统一见[2026-10-18 完成设计](../superpowers/specs/2026-10-18-osmo360ii-capture-completion-design.md)，TX2 低功耗恢复与取消进度见[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)。D2 保留本节独有的用户现场事实和未覆盖设备条件，不重复维护当前软件规则或总体进度。

2026-10-15 的独立失败试验仍按原范围保存：相机日志记录 `/system/bin/dji_media_server` 的 `SIGABRT`、`Do not Initialize OMX again:init` 断言，前后相机 boot ID 不同，新 OSV 和 LRF 均为 0 字节。证据入口为本地忽略目录 `.local/acceptance/osmo360-20261015T0336Z/report.md` 及 `revalidate-20261015T0709Z/revalidation.txt`；日志顺序不证明崩溃导致 USB 挂起或重启的完整因果链，也不能因本次低功耗结论将旧试验改判为正常完成。

Action6 的专项结果见[固件缺陷记录](#action6-长间隔延时固件缺陷与验证暂停2026-10-17)。用户确认两款相机的自动重连行为不同；Action6 不采用 OSMO 的约 5 秒恢复观察值。

## OSMO START 可选参数的用户确认（2026-10-17）

用户明确说明 OSMO 360 II 延时 START 的 `-o 20000000` 不必要，可以省略。后续新验证命令采用[OSMO START 调用](timelapse-command-encoding.md#osmo-start-调用)；历史原始调用及既有现场记录继续保留其实际参数。

本项是用户提供的参数说明，没有据此重新拍摄，也不构成省略参数后的 USB 稳定性验证。默认工具响应等待上限及单位仍未知；适用的执行与完成规则见[2026-10-18 完成设计](../superpowers/specs/2026-10-18-osmo360ii-capture-completion-design.md)，本轮 TX2 正式业务验收状态见[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)。本段只保留用户对 `-o` 参数的历史确认。

## Action6 TX2 真机验证与暂停（2026-10-17）

环境为 TX2 ARM64、Python 3.11.17、SQLite 3.53.1，部署源码基线为 `f803cc0c5670e4afa99c06c2e2fc8e52e33159c3`。本次通过 SSH 在 TX2 执行正式普通录像和独立原生延时诊断，原片保留。用户选择覆盖功能和参数分支，间隔与采集时长采用代表值；没有遍历全部时间组合。

延时场景反复出现 USB 设备移除和 ADB 失联，用户要求暂停这台 Action6 的后续验证。暂停时用户确认相机已自动回到待机，第二轮期间没有手动操作；主机最后一次只读检查仍未见相机 USB 节点。后续设备顺序、未完成项和恢复前提由[当前真机验证安排](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)维护。

### 普通录像与交付

首次普通录像尝试在 STOP 调用时遇到 `device not found`，正式动作按 `recording_stop_failed` 失败；用户随后手动停止并重启相机，原失败记录保留。恢复后的 9 个核心用例正式拍摄成功，目标均为 10 秒，覆盖 4K30／8K30、三种 FOV、两种增稳、Auto 的 EV −0.7／0／+0.7 及 Manual 的 ISO 100—3200。分辨率、FOV 和增稳覆盖了 12 种组合中的 6 种，没有完成全部组合。

9 段视频的尺寸和帧率与请求匹配，源文件与副本的长度和 SHA-256 核对通过，主视频均完成严格解码。实际主视频时长为 9.376033—9.943267 秒；控制目标与媒体时长分别记录，本次没有据此判定精确十秒媒体时长达标。Manual 六档 ISO 与文件自身元数据一致，该结果不是相机独立参数回读。

基线用例完成正式独立 `obtain`、C host 文件领取、客户端导入及报告 ACK，报告 9 的确认水位为 26。另 8 个正式取回因下述占用缺陷保持待处理，已停止本轮运行；对应的只读诊断副本及媒体核验不能算作这 8 个正式取回成功。拍摄前取消已验证，未发送相机控制命令。

### 设置失败后的占用释放缺陷

显式光圈 f2.8／f4.0 返回 `e3`，标准／高码率均报 `action6_setting_unconfirmed`；四个用例均在 START 前停止。`e3` 的设备含义未确定，工具本地退出码为 0 不表示设置已生效。

f2.8 用例的整项录像启动尝试已可靠保存为 `NO_EFFECT`，动作最终失败，原调用与准备已结束，但活动仍为 `UNKNOWN`、占用仍为 `HELD`，阻断同设备后续拍摄和取回。只读审查确认释放判定没有完整消费同事务的动作终态及准备完成事实；这属于软件占用释放缺陷，与已启动后 STOP 未确认的情况分别处理。原状态库未清空，也未直接修改占用。

该缺陷尚未修复。后续修复需要覆盖实际失败结果和预算耗尽两个终局入口，并为既存终态活动保留原历史、追加可靠释放；仍有未知启动效果、在途调用或未完准备责任时应继续保留占用。具体实施与回归应先形成独立计划，不能用新库正常用例替代原库恢复验证。

### 原生延时六个诊断分支

下表的间隔和时长都是实际请求值，文件数和帧数是观测值，不作为由时长除以间隔推导的完成判据。用户分别报告这些采集已自动回到待机；现场确认、文件核验与程序化完成契约各自保留证据范围。

曝光与输出|间隔／采集时长|实际文件与媒体检查
-|-|-
Auto，仅视频|1 秒／300 秒|初次取回检查失败；现场待机后另作一次只读复制，源／副本核对及完整视频解码通过，MP4 为 301 帧。原失败保留。
Auto，视频+RAW|2 秒／300 秒|结束后清单含 151 个 DNG 和 MP4／LRF；视频为 151 帧并完整解码，抽取 3 个 DNG 解码，其余未逐张解码。
Auto，视频+JPEG|3 秒／300 秒|结束后清单含 101 个 JPG 和 MP4／LRF；视频为 101 帧并完整解码，抽取 3 个图片容器核验全部内嵌图片，其余未逐张解码。
Manual，仅视频|30 秒／600 秒|采集中 USB 断开，期限后的目录读取失败；用户确认待机并重启恢复后，只读副本核对和完整解码通过，MP4 为 21 帧。
Manual，视频+RAW|4 秒／300 秒|结束后清单含 76 个 DNG 和 MP4／LRF；视频为 76 帧并完整解码，抽取 3 个 DNG 解码，其余未逐张解码。
Manual，视频+JPEG|5 秒／300 秒|采集中 USB 断开，期限后的目录读取失败；用户确认自动待机，结束文件清单和媒体完整性尚未核验。

三个 Manual 分支均请求 ISO 800。Manual RAW 的首次前置 SD 目录读取出现空通道异常，在设置和 START 前退出；通道核对后另建独立记录执行一次 START，原前置失败仍保留。

这些是候选命令诊断结果。该部署正式能力只导出 Action6 `camera_record`，没有原生延时任务工厂；延时 STOP 未映射或验证，实际返回阶段、自动结束、单文件写完及集合齐备尚未形成正式执行契约。`00`、主机等待、用户观察和选样解码不能合并为正式延时业务验收通过。

### USB 断开的两轮受控复现

在同一 TX2、原线材、接口和供电下，两轮分别使用相机重启后的不同 boot ID，均先静默待机 90 秒，随后执行相同的八条设置和一次 START：Manual ISO 800、视频+JPEG、5 秒间隔、300 秒采集。九条控制调用均退出 0、stderr 为空，并各返回一条完整 `00`。START 后探针只在主机等待，没有相机 ADB 轮询；独立观察器只读取主机 USB sysfs 和日志。

观察|第一轮|第二轮
-|-|-
90 秒静默待机|90 个连续样本，未见相机 USB 移除|90 个连续样本，未见相机 USB 移除
udev 整机 USB 节点移除|START 前计时锚点后 16.364068 秒|START 前计时锚点后 16.367447 秒
等待结束后的原定一次目录读取|等满 300 秒，`device not found`，退出 1|等满 300 秒，`device not found`，退出 1

首轮断开后用户观察相机仍在延时采集，之后确认自动待机并重启相机恢复连接；第二轮结束后用户确认自动待机且期间没有手动操作，未提供进入待机的精确时刻。每轮只有一次 START，没有 STOP、自动重试或下载。第二轮之后未启动第三轮，也未由 Agent 重启相机。

两次记录证明当前操作序列下可以重复观察到 USB 移除，尚未确定根因，不能认定 START 单条命令导致断开，也不能由 2/2 推导总体复现概率。移除前最后可见 USB 样本均为 `runtime_status=active`、`power_control=on`；该采样状态不提供相机内部原因或主机电源管理的完整结论。udev 与 dmesg 保留各自原始时标，不跨时钟排序推导因果。

### 本次证据入口

原始调用、状态库、报告、诊断副本和本地审计位于忽略目录 `.local/acceptance/action6-tx2-20261017T083952Z/`；总索引为 `verification-status.json`，覆盖核对为 `coverage-reconciliation/REPORT.md`，占用缺陷为 `setting-no-effect-audit/review.md`。各原生分支保存在 `native-branches/`、`native-ended-observations/` 和对应的媒体审计目录。

两轮断连复现保存在 `.local/acceptance/action6-usb-repro-20261017/`，入口为 `experiment.json`、`TWO-ROUND-COMPARISON.md`、各轮 `calls/` 和 `host-observer/`。监听共 1016 个连续样本，完整覆盖两个基线及两个采集等待窗口，结束后自有监听进程均已收场。`final-verification.json` 核对 TX2 与本地的 127 个原始证据文件，长度和 SHA-256 全部一致。这些本地材料及相机原片保留，不随 Git 分发；本节记录实际结论，不代表整个验收范围完成。

## OSMO 360 II 最短延时手工演示的现场验收（2026-10-16）

用户在 TX2 终端按[最短手工演示顺序](../hardware/camera-demo-validation.md#osmo-360-ii-的最短手工演示顺序)验证 OSMO 360 II 全景静止延时摄影。本次请求为间隔 1 秒、采集 300 秒、仅输出视频；其余请求为 8K／30 fps、Pro、M 档、ISO 800、1/60 秒快门和 5200 K 白平衡。命令中的采集时长不表示成片播放时长。

验收执行者为用户。用户执行演示后明确反馈“验证通过，一切正常”，本组合的手工延时演示按用户现场整体确认验收通过。当前无需为同一演示再次拍摄或补交材料。

本次逐条原始调用、固件版本记录、媒体文件及测量结果未在仓库归档，因此不补写逐项响应或参数回读结果、实际采集与处理耗时、成片长度、帧数、文件摘要或精确结束时间。此前 30 秒间隔／600 秒采集的试验记录及其结论继续按原范围保留。

这项结果提供了本组合的手工现场依据。START 返回阶段、自动结束的机器判定、STOP 适用性与重复安全性、文件写完和集合齐备的程序化依据仍由[驱动计划 D2](../superpowers/plans/2026-10-16-osmo360ii-timelapse-driver.md#d2-明确返回结束及产物契约)分别维护。正式内置合同、安装版 C host、客户端导出、独立取回、报告导入及 ACK 的完整联合验收继续按[交付计划 E2／E3](../superpowers/plans/2026-10-16-osmo360ii-timelapse-delivery.md#e2-正式内置合同的安装版联合验收)推进；本次文档登记不启用正式驱动能力。

## 延时参数与候选软件验证（2026-10-16）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1、Node.js v24.20.0。时间参数、当前新请求校验和可信原生效参数的恢复分别沿[共同参数资料](../hardware/camera-control-handoff.md#间隔与采集时长限制)及[候选编码契约](timelapse-command-encoding.md)验证；实施任务见[延时领域计划](../superpowers/plans/2026-10-16-osmo360ii-timelapse.md)。

两款候选使用同一时间权威定义生成 Schema、资料表格和独立诊断 JSON。当前新请求遵守有限时间域及严格大小关系；最短样例为 1 秒间隔、300 秒采集，Action6 保留 Auto，OSMO 保留手动 ISO 800。原已受理参数、完整命令模板、执行定义、绑定、目录基准及未决 START 在重启和当前能力收窄后保持，不按新规则改写历史。

各阶段在独立工作区提交并经独立审查，按依赖合入主工作区。pytest 使用各树自己的 Python 3.11 环境；主工作区命令为 `UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --frozen --project apps/camctl --group test --python 3.11 pytest <目标> -q`。各集成目录分别启动进程，同一工作区顺序运行。下表分别记录最终有效范围，不把专项、阶段回归和组合复验相加。

检查范围|实际结果与覆盖
-|-
主工作区参数、命令及驱动单元|418 项通过，13.18 秒；覆盖精确参数、当前新请求与可信原事实分流、两款完整模板及有界时间替换。
主工作区公共派发集成|15 项通过，1.95 秒；覆盖原生效参数、真实 SQLite 重开及候选合同注入边界。
主工作区跨语言接缝|3 项通过，19.29 秒；实际候选 `describe` 的 Schema 经 Python、真实 Chromium、Node 后端及客户端导出共同消费，两款各 134 对合法时间、精确数值、曝光和输出边界通过。
安装包及计划生成器|`bootstrap/test_package.py` 3 项、`bootstrap/test_distribution.py` 38 项分别通过；已安装资源按实际能力生成计划，缺失或重复能力、非法及邻近精确时间在生成前拒绝。
Action6 候选产物|`bootstrap/test_adb_camera_products.py` 16 项通过，43.40 秒；固定六种输出与曝光组合、必要产物及部分合格文件保留，另有两条固定历史域外时间的新请求拒绝。
两款候选完整软件链|根 `test_real_camera_demo_roundtrip.py` 4 项通过，57.14 秒；两条普通录像链和两条最短延时链覆盖 C host、CLI 拍摄、重启恢复、独立取回、报告导入、ACK 及源文件保留。
主工作区 Action6 诊断|硬件单元 43 项、10 个子用例通过；脚本集成 83 项、316 个子用例通过，分别为 0.04 秒、20.99 秒。新请求在工具调用前校验；五类历史布局按原完整调用及双存储基准读取，缺失、混杂或矛盾明确拒绝。
客户端完整门禁|单元 2883 项、集成 1350 项通过，类型与构建通过；HTTP、SQLite、浏览器及草稿、预设、固定请求恢复的实际范围见[客户端验证记录](../client/verification.md#2026-10-16-延时参数保存与恢复验证)。

Action6 独立诊断的四文件交付见[操作说明](../hardware/camera-demo-validation.md#action6-独立诊断交付)。隔离目录以 Python `-I` 运行实际脚本及调用采集器；删除当前时间 JSON 和候选编码模块后，最早两类记录仍可只读复查，不发送设置、START 或 STOP，不等待采集，不改写原文件。该路径没有依赖源码工作区或 camctl 安装；新诊断的 ADB、ffprobe 和时钟由受约束的替身提供。独立目录通过不能代替 Windows 本机或 ARM 验证。

组合树的两项生成器 `--check`、包资源逐字节 `--check` 及诊断 `--help` 均退出 0。生成器源码变异验证覆盖重绑定、相对或嵌套导入、未知依赖和分发漂移的明确拒绝；生产完整模板和纯编码函数仍是独立诊断编码的来源。

现存五个工作区的阶段命令、日志和退出码已归档到 `.local/verification/historical-evidence-20261023/osmo-timelapse-20261016/`，共430份文件；每个工作区独立保留，其来源、长度及 SHA-256 由该历史证据根的 `manifest.json` 登记。主树的 `integrated-d1-unit`、`integrated-d1-dispatch`、`integrated-e1-p3`、`integrated-d1a-unit`、`integrated-d1a-integration`，交付树的 `logs/e1-chain-verify-*`、`logs/e1-chain-review-products.log`，诊断树的 `d1a-fix-logs` 及客户端树的 `task-C3-final-*` 均按原工作区保存。原阶段日志与后续复验分别保留。

候选整链通过测试启动器显式注入相机合同，ADB、ffprobe 和采集时钟按测试范围替代；生产登记未由这些测试启用延时。整秒负载映射来自已有完整样例的有界推导，保留非时间字节，仍需实机核验模板适用性和未观察位。软件测试没有取得参数生效、实际开始、自然结束、停止重复安全性、单文件写完或结果集合齐备的新设备依据。正式 OSMO 合同和 ARM 完整业务验收继续由计划中的设备及后续阶段跟踪，Action6 正式延时仍沿原 T9 门槛。

## SW-01 时间状态验证（2026-10-13）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1。受理仓储统一使用整数登记的 `UNCHECKED`，保存受理时取得的实际读数；检查通过后的可信下界事件使用登记的 `TRUSTED` 和实际通过读数。启动顺序、下界更新资格、公共接口和数据库格式保持原契约，既有事件不迁移或修订。实施范围见[SW-01 计划](../superpowers/plans/2026-10-13-camctl-sw01-acceptance-clock-status.md)。

新增 [受理专项](../../apps/camctl/tests/integration/acceptance/test_clock_status.py)覆盖首次注册、动作参数失败、来源成员、输入拒绝、读入／解析诊断、复用加 ACK、无效 ACK、推进及相等水位的同步结束，以及无新增事实的幂等路径。新增 [默认入口专项](../../apps/camctl/tests/integration/bootstrap/test_acceptance_clock_status.py)组合真实会话装配、SQLite 和锁，核对无下界／已有下界的 `submit`、正常接纳期间的并发提交、检查通过／失败及后续运行的旧历史保持。按原完整历史边界恢复的计划与动作，与受理后的原投影一致；普通设备流程在检查失败时没有取得执行资格。

加强 [会话时钟专项](../../apps/camctl/tests/integration/session/test_clock.py)，分别验证首次建立和推进下界事件的实际读数及 `TRUSTED`；相等读数经过真实 `enter_execution` 后，事件和事务均保持原样。两类生产缺陷分别有有效失败记录：受理事件实际为 `2`、预期为 `1`；下界事件实际为 `1`、预期为 `2`。

所有 pytest 从仓库根执行，使用 `UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest <目标> -q`；单元及各集成目录分别顺序启动。最终有效验证如下：

| 命令目标（相对于 `apps/camctl/tests`） | 实际结果 |
| --- | --- |
| `unit` | 5076 项通过、1 项跳过、2 条既有警告 |
| `integration/acceptance` | 250 项通过 |
| `integration/session` | 100 项通过 |
| `integration/bootstrap` | 997 项通过、1 项跳过 |
| `integration/reporting/test_ack_sync.py` | 44 项通过 |
| `integration/acceptance/test_clock_status.py` | 10 项通过，含完整目录之后加强的旧事件原键重送断言 |
| `integration/session/test_clock.py` | 4 项通过，含完整目录之后加强的真实无更新路径 |
| `integration/bootstrap/test_acceptance_clock_status.py` | 4 项通过 |

以上命令退出码均为 `0`，stderr 日志为空。单元跳过验证 Linux 之外的守护行为；bootstrap 跳过依赖 Windows 文件系统的大小写别名。两条警告来自已有同步测试的 asyncio 标记，未修改对应测试。早期一轮受理回归未捕获退出码，最终扩展后的完整目录已有退出码 `0` 的记录。

本次验证只覆盖软件受理、会话、事务和历史恢复，没有连接相机或 ARM 主机。

## SW-02 日志副本诊断验证（2026-10-13）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1。[副本编排](../../apps/camctl/src/camctl/logging_runtime/copies.py)保留标记检查、创建、进度保存和文件处理的完整回执；[会话入口](../../apps/camctl/src/camctl/session/service.py)消费各阶段错误，默认装配向实际可用的文件日志或 stderr 输出诊断。每份报告可靠登记文件失败后立即触发，副本含已知的 `report_id` 和实际报告错误；进度保存失败保留其原分类，已可靠交接的副本保持原字节和位置。实施范围见[SW-02 计划](../superpowers/plans/2026-10-13-camctl-sw02-log-copy-diagnostics.md)。

[默认组合专项](../../apps/camctl/tests/integration/bootstrap/test_log_copy_diagnostics.py)验证首份报告副本先于后续报告推进、两份报告同轮失败及恢复后的新尝试、进度检查的存在性与原错误、原日志和副本文件错误的不同处理，以及普通和受限会话的原结果。普通文件诊断和 stderr 同时失败时，真实默认会话仍保留原报告结果和已交接副本，停止再次写入失败的诊断出口。请求构造或交付异常由同一服务暂停本运行后续尝试；后续正常启动仍按正式标记核实故障，进程内暂停不成为持久业务状态。

[真实文件适配专项](../../apps/camctl/tests/integration/logging_runtime/test_clh_adapter.py)和默认日志通道组合验证：格式化失败后下一条合法记录仍可写入；原日志打开、追加、刷新或共享协调失败后停用本进程后续文件日志，普通日志先失效时也禁止后来副本入口重新构造处理器。副本自身读取或写入失败时保留仍可用的原日志。轮换失败但追加成功时继续写入，连续故障只提示一次；关闭失败独立保留未知事实，已开始的监听工作实际结束后才完成收场。

标记旧轮次更新与恢复、新轮次创建共用协调拥有者；真实目录验证正式标记身份及交接文件字节。各阶段具有可核对的失败记录，分别证明丢失回执、错误去重、异常后再次尝试、延后的首次副本及失效通道重复打开等问题；修复后的单元、真实文件和默认组合均通过。完整门禁从仓库根按既有 Python 3.11 命令分别执行，最终有效结果如下：

| 命令目标（相对于 `apps/camctl/tests`） | 实际结果 |
| --- | --- |
| `unit` | 5090 项通过、1 项跳过、2 条既有警告 |
| `integration/logging_runtime` | 45 项通过、1 条既有警告 |
| `integration/session` | 106 项通过 |
| `integration/bootstrap` | 1025 项通过、1 项跳过 |
| `integration/bootstrap/test_log_copy_diagnostics.py` | 29 项通过，含完整目录后增补的双诊断出口失败组合 |

以上命令退出码均为 `0`，stderr 日志为空。跳过范围与 SW-01 相同；三条警告均为已有同步测试携带 asyncio 标记，日志目录新增的第三条来自 `test_handler.py::TestTwoProcesses::test_two_processes_share_file`。最终目录记录为 `084`、`085`、`086`、`092`，最后仅改 bootstrap 的工厂检查后，重新执行了受影响的完整目录。随后仅新增双诊断出口失败组合，生产文件保持原版本；`093` 新用例及 `094` 完整诊断模块分别通过，均退出码为 `0`。

本次软件证据覆盖日志回执、报告触发及诊断装配。标记临时文件清理和取消后的文件责任由后续 SW-03、SW-04 承接，设备及目标部署验收保持各自范围。

## SW-03 标记临时文件清理验证（2026-10-14）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1。[标记保存拥有者](../../apps/camctl/src/camctl/logging_runtime/copies.py)在同步操作结束、临时句柄确认关闭后，只清理自己生成的临时路径。替换成功后记录源路径已移走；关闭未确认时保留路径并记录未尝试删除。保存、关闭和清理错误分别保留，目录同步成功后的关闭错误不抹去已确认的保存或删除事实。实施范围见[SW-03 计划](../superpowers/plans/2026-10-13-camctl-sw03-marker-temp-cleanup.md)。

[单元专项](../../apps/camctl/tests/unit/logging_runtime/test_marker_temp_cleanup.py)通过内存路径和文件描述符替身，验证创建与进度保存的本次归属、外部文件及正式标记字节保持、未知替换、零字节写入、一次删除、关闭未知，以及程序异常、取消和有序异常组。先发生普通文件错误、后续关闭或清理取消时，传播同一取消对象并保留此前原因；原程序异常或原取消优先级保持。创建及两次进度的诊断消费者分别覆盖 `SAVED` 携带附属错误，正式删除的诊断只表述实际已知的关闭事实。

[真实文件与线程专项](../../apps/camctl/tests/integration/logging_runtime/test_marker_temp_cleanup.py)核对部分写入、替换失败、临时句柄关闭未确认、目录同步与关闭组合，以及等待协程取消后同步保存拥有者实际完成清理。测试在确认生产事实之后回收故意保持打开的测试句柄。[默认装配专项](../../apps/camctl/tests/integration/bootstrap/test_log_copy_diagnostics.py)验证进度临时文件清理失败时副本仍可发布、标记可靠保存后的附属关闭错误进入日志，以及正式标记已删除后的关闭错误仍可诊断；原报告结果和已交接副本保持。

根代理逐文件探索了生产者、会话消费者、默认恢复出口、测试与规格；独立复核核对同一文件快照。临时残留、错误覆盖、取消被普通错误吞掉和关闭事实误报均有有效失败记录，修正后完整门禁从仓库根按既有 Python 3.11 命令分别顺序执行：

| 命令目标（相对于 `apps/camctl/tests`） | 实际结果 |
| --- | --- |
| `unit` | 5125 项通过、1 项跳过、2 条既有警告 |
| `integration/logging_runtime` | 53 项通过、1 条既有警告 |
| `integration/session` | 106 项通过 |
| `integration/bootstrap` | 1030 项通过、1 项跳过 |
| `unit/logging_runtime/test_marker_temp_cleanup.py` | 35 项通过 |
| `integration/logging_runtime/test_marker_temp_cleanup.py` | 8 项通过 |
| `integration/bootstrap/test_log_copy_diagnostics.py` | 33 项通过 |

以上命令退出码均为 `0`；完整目录的 stderr 日志为空，跳过及已有警告范围与 SW-02 相同。窄验证后生产及测试保持冻结，完整目录覆盖同一最终快照。完整目录记录为 `sw03-full-*-execution.json`。早期测试构造或启动器错误与有效测试结果分别记录。

本次软件证据覆盖同步保存拥有者和诊断消费者。等待协程取消后的全部日志文件责任、遗留启动检查及会话锁放行由 SW-04 承接；真实设备、ARM 部署及物理断电保持各自验收范围。

## SW-04 日志责任与启动检查验证（2026-10-14）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1。默认 `run` 取得会话锁后登记一次旧日志检查，数据库和业务推进不等待扫描线程。可靠存在但不可用的普通状态库沿最小错误会话收场；锁前资格不成立、未取得会话锁及其他命令保持原职责。实施范围见[SW-04 计划](../superpowers/plans/2026-10-13-camctl-sw04-startup-log-sweep.md)。

[启动专项](../../apps/camctl/tests/integration/bootstrap/test_startup_log_sweep.py)与[归属专项](../../apps/camctl/tests/integration/logging_runtime/test_startup_ownership.py)组合真实目录、SQLite、会话锁和工作线程。检查只删除归属明确且前任操作已结束的旧副本及临时文件，保留正式标记、本次分配的文件和非普通对象；单条失败在本轮不重试，下一次运行可处理残留。实际配置的普通日志按配置位置和文件链接目标保护，覆盖副本名、临时名、路径别名、解析后配置位置替换及其他目录同名；保护路径无法确认时保留候选并交付诊断。文件替换对照验证保护位置，未作为完整日志轮换验收。

[日志拥有者](../../apps/camctl/src/camctl/logging_runtime/ownership.py)区分排队撤回、已开始调用及结果诊断；默认线程池内的实际文件操作结束后，才消费结果及有资格的一次清理。[默认复制与恢复专项](../../apps/camctl/tests/integration/bootstrap/test_log_operation_settlement.py)验证取消后源停止先推进，复制、必要清理或恢复删除及诊断完成前两类协调资格仍保留。[副本专项](../../apps/camctl/tests/integration/logging_runtime/test_copies.py)核对关闭确认、全流程回执、异常对象和次因、已移动及位置未知分支。[默认诊断专项](../../apps/camctl/tests/integration/bootstrap/test_log_copy_diagnostics.py)核对失效原日志路径在后继等待前停用，以及扫描与本次副本／临时文件重叠时的保护。

[会话消费者](../../apps/camctl/src/camctl/session/service.py)持有原资格已经建立的有限受限流程、应急停止及一次报告任务；父等待取消与内部实际控制异常分别交付。真实 `winddown_flow`、`EmergencySettlement`、SQLite 和受控停止端口验证停止结果保存及连接关闭先于日志收场。[受限报告专项](../../apps/camctl/tests/integration/bootstrap/test_restricted_report_cancellation.py)通过真实 `WorkerSupervisor` 验证已派发生成的成功／失败结果；父取消不单独终止生成或建立新报告。组合收场继续消费后续拥有者，阶段结束后释放缓存登记；[单元专项](../../apps/camctl/tests/unit/bootstrap/test_background_flow.py)及默认运行集成分别核对控制对象、首次父取消、附注与下一阶段新责任。内部文件控制集成使用真实执行器及受控协程体，没有执行相机操作。

根代理逐修改文件探索了规格、生产者、消费者和测试上下文，独立代理复核同一候选。所有 pytest 从仓库根使用既有 Python 3.11 命令顺序运行，完整目录各为独立进程；最终结果如下：

| 命令目标（相对于 `apps/camctl/tests`） | 实际结果 |
| --- | --- |
| `unit` | 5152 项通过、1 项跳过、2 条既有警告；real 14.187 秒 |
| `integration/logging_runtime` | 96 项通过、1 条既有警告；real 2.503 秒 |
| `integration/session` | 117 项通过；real 22.487 秒 |
| `integration/bootstrap` | 完整复验 1074 项通过、1 项跳过；real 879.863 秒 |

最终四份记录的 pytest 退出码均为 `0`，stderr 为空。跳过及既有警告范围与 SW-03 相同。启动目录首次完整运行是 1064 项通过、1 项跳过、10 项测试装配错误：两处包装函数未接收新增的可选日志拥有者参数，尚未执行原业务断言。仅同步这两个测试包装器并原样转发后，原十个参数通过，随后完整启动目录通过；生产及其余测试保持不变，前三目录沿原通过快照复用。单元测试结束后的记录脚本曾在汇总元数据时报错，已补全记录，未重跑已通过的 pytest。

最终启动复验对应 `integration-bootstrap-recheck.json`。24 个生产与测试 Python 文件在最终复验前后摘要一致。文档检查和提交核验由根代理收口。

本次结果证明开发环境的软件启动、日志实际责任、取消及锁内消费；真实设备、ARM 部署和物理断电继续按各自计划验收。通用锁错误与独立资源释放由 SW-05 承接。

## SW-05 会话锁与退出收场验证（2026-10-14）

环境为 Linux WSL2 x86_64、Python 3.11.16、SQLite 3.53.1。实施范围见[SW-05 计划](../superpowers/plans/2026-10-13-camctl-sw05-session-lock-errors.md)。

[锁端口专项](../../apps/camctl/tests/unit/session/test_locks.py)验证打开、不可继承设置、加锁、探测、解锁及关闭各阶段的实际错误；稳定锁文件继续由内核锁表达资格。首次控制异常保持同一对象，没有控制异常时保留首个普通错误及有序后错；失败租约再次关闭交付保存的失败，系统关闭只尝试一次。

[会话与事务专项](../../apps/camctl/tests/integration/session/test_lock_error_classification.py)组合真实 SQLite 与锁端口，验证普通接纳在短写事务内取得、租约立即登记、事务可靠结束后进入普通执行。锁系统故障、可信锁程序异常与实际数据库故障分别形成 `lock_error`、`internal_error`、`state_db_error`；回滚失败保留原错误及未知状态，最终次错保持平坦且去重。事务结束响应在真实回滚前后丢失时均只回滚一次，已释放接纳资格保持关闭。

[默认入口专项](../../apps/camctl/tests/integration/bootstrap/test_session_lock_errors.py)验证真实 Linux 进程锁冲突、CLI 单条错误结果、未持锁时零业务副作用，以及既有受理及可信下界事实。晚到时钟交接错误沿原每目标应急资格收场，最终资源释放不新建应急。监督方、通知管道和日志的后错保留首个实际控制对象；CLI 控制退出完成副本通道、普通日志与通知 fd 的适用关闭，`stdout` 为空。[初始化专项](../../apps/camctl/tests/integration/bootstrap/test_initialization.py)另验 `init` 的 `stderr`、退出状态及实际库创建、验证和目录绑定事实。

Root 对每个修改文件亲自核对规格、生产者、消费者和测试，再由 GPT6.1-Sol 完整独立复核。复核发现日志关闭等待取消会跳过后续资源，Root 按统一关闭任务补齐：父取消保留首对象，实际操作及适用后续关闭结束后才传播。[真实日志专项](../../apps/camctl/tests/integration/logging_runtime/test_lifecycle.py)的 20 项测试覆盖两个日志分支，分别在等待监听停止、文件关闭和队列结束时取消，并核对重复取消和后错先后。CLI 对照首次复验发现 stderr 后错诊断遗漏，补齐后 2 项通过。独立复核后的这一修复由 Root 自审并执行最终门禁。

| 命令目标（相对于 `apps/camctl/tests`） | 实际结果 | 进程耗时 |
| --- | --- | --- |
| `unit` | 5230 项通过，1 项跳过，2 条既有警告 | 14.768 秒 |
| `integration/session` | 138 项通过 | 24.789 秒 |
| `integration/acceptance` | 258 项通过 | 43.679 秒 |
| `integration/persistence` | 123 项通过 | 4.006 秒 |
| `integration/bootstrap` | 1134 项通过，1 项跳过 | 884.273 秒 |
| `integration/logging_runtime` | 112 项通过，1 条既有警告 | 2.720 秒 |
| `integration/contracts` | 438 项通过 | 5.016 秒 |
| 报告工作锁单节点 | 1 项通过 | 5.486 秒 |

当时的完整目录分别启动 pytest 并串行执行，命令从仓库根使用 `apps/camctl/.venv311/bin/python -m pytest <目标> -q -rs`。2026-10-23核对当前登记的工作区时，未找到该阶段的原始日志及 `final-gate-manifest.json`；本节保留历史汇总，不能据此重新核实具体测次或代替当前版本验证。真实相机、ARM 部署及物理断电继续按各自计划验收。

两个跳过项分别为单元测试的非 Linux 守护对照和 bootstrap 的 Windows 文件系统大小写别名对照；三条警告来自既有同步测试的 asyncio 标记。最终八个进程退出码均为 `0`，18 个生产与测试 Python 文件在门禁前后摘要一致。

## SW-06 默认读取分批发现验证（2026-10-15）

环境为 Linux WSL2 x86_64、Python 3.11.16、SQLite 3.53.1、uv 0.12.10。实施范围见[SW-06 计划](../superpowers/plans/2026-10-13-camctl-sw06-bounded-read-discovery.md)。

[真实候选与事务专项](../../apps/camctl/tests/integration/outputs/test_read_discovery.py)验证设备源及录像内部输入共用全体业务顺序，旧持有者优先，坏关联、重复 READ 和多持有者不能被有限结果隐藏。逐产物资格的规划与独立守卫分别读取必要事实；[事实端口专项](../../apps/camctl/tests/integration/outputs/test_current_event_reads.py)验证原写事务、范围与限额、读取错误及已验证前序变化。[既有事件顺序反例](../../apps/camctl/tests/integration/outputs/test_product_event_sequence.py)继续拒绝未来提案冒充当前事实，静态验证与历史恢复保留原完整范围依据。完成汇总使用固定计数；取回完成及来源解析失败时的计划完成守卫逐批独立检查。

[默认入口专项](../../apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py)从真实配置、SQLite 和会话装配推进多批取回、初始化期间新提交、原绑定失败、低位候选重查、取消与内部输入检查；[默认保存消费者](../../apps/camctl/tests/integration/bootstrap/test_read_default_consumers.py)在 B=1 下覆盖正常、残留及受限工厂接手已有实际结果。[安装版主机源专项](../../apps/camctl/tests/integration/bootstrap/test_distribution.py)用 B=1 完成 RAW/JPEG 两份源及两轮独立取回。[会话通知反例](../../apps/camctl/tests/integration/session/test_session.py)在30秒轮询条件下要求流程内安排的下一页及时继续；退出仍由原短写事务重新检查持久化责任。

容量用例的候选总数 N 为14、122，批次 B 为1、3：每页最多返回 B 个候选，交付下一页前，上一页候选的弱引用全部释放，扫描结束继续位置为0；默认流程另外用3／5个取回动作生成6／10份独立副本并核对同样的详情释放。另有新增131个终态动作、132项混合结果，以及新增131个终态兄弟动作后的完成事务检查。实际查询计划包含索引查找和 `USE TEMP B-TREE FOR ORDER BY`；这些结果证明 Python 候选详情受限，不代表 SQL 扫描／排序为常数，也不构成 ARM 性能验收。原子来源／选择集合、本笔实际变化及实际操作收场各自保持原责任，发现上限不截断它们。

Root 逐文件核对规格、生产者、消费者和测试，GPT6.1-Sol 完成一次完整独立复核。两项重要问题分别是内部读取的持有者被误派给普通取回，以及按 ID 的拍摄分页改变全体启动顺序。Root 按用途归属恢复原持有者，并按完整业务键续读拍摄；真实 SQL 和实际控制调用的反例修复后通过。沿相关入口发现的启动排序及最近等待截止也改为常数探测／SQL 聚合，132／131个动作的有效红绿记录保留。

| 命令目标（相对于 `apps/camctl/tests`） | 实际结果 | 进程耗时 |
| --- | --- | --- |
| `unit` | 5238 项通过，1 项跳过，2 条既有警告 | 14.402 秒 |
| `integration/capture` | 1120 项通过 | 419.098 秒 |
| `integration/scheduling` | 136 项通过 | 6.714 秒 |
| `integration/session` | 139 项通过 | 26.838 秒 |
| `integration/bootstrap` | 1152 项通过，1 项跳过 | 922.961 秒 |
| `integration/history` | 212 项通过 | 45.546 秒 |
| `integration/persistence` | 123 项通过 | 4.598 秒 |
| `integration/contracts` | 438 项通过 | 4.955 秒 |
| `integration/logging_runtime` | 112 项通过，1 条既有警告 | 2.959 秒 |
| `integration/outputs` | 2011 项通过，2 项跳过 | 328.794 秒 |
| `配置消费检查` | 通过，退出码 0 | 0.622 秒 |

当时的命令从仓库根使用 `apps/camctl/.venv311/bin/python -m pytest <目标> -q -rs`，配置消费检查使用同一解释器运行 `scripts/check-config-consumption.py`，各完整集成目录分别启动进程并串行执行。2026-10-23核对当前登记的工作区时，未找到该阶段的完整原始日志；本节保留历史汇总，不能据此重新核实具体测次或代替当前版本验证。软件证据与真实相机、ARM 部署及物理断电的证据分别维护。

表内11个进程均退出0。跳过项包括非 Linux 守护对照、Windows 大小写别名对照，以及两类不适用参数（主机源不携带相机读取配置、内部副本不执行普通交付发布）；警告来自既有同步测试的 asyncio 标记。最终38个修改或新增的 Python 文件已完成语法与摘要核对。完整 bootstrap 复跑前仅校正另外三份既有测试的私有调用签名和候选边界，其余35份生产及测试文件摘要保持；原 bootstrap 的12项失败及其全部边界消费者定向复验、完整目录复验均保留。完整目录通过后，W-11 的默认发现、排序、详情释放、新提交、原持有者、终态后独立收场、继续唤醒及退出条件均有软件证据。 验收映射结构检查另有3项通过，本地文件链接4450项、新增标题链接6项及删除标题入链检查均通过；`git diff --check` 通过。

## 测试与分步实施方向

### Python 环境

开发与测试遵循[目标环境与部署分工](implementation.md#目标环境与部署分工)指定的 Python 3.11。执行前使用虚拟环境解释器的 `--version` 核对版本；已有环境满足要求时直接复用，否则用 uv 创建 Python 3.11 的非项目级虚拟环境，并按组件 `pyproject.toml` 和 `uv.lock` 准备生产及测试依赖。测试使用该环境的解释器显式执行，单元与集成分别运行。

验证记录应注明解释器版本、测试范围和实际结果，使每项实施门禁都有可核对的依据。

### 分类与替身

遵守[共享测试规则](../../.agents/rules/testing.md)，运行 camctl 测试前同时读取[组件测试运行指南](../../apps/camctl/tests/AGENTS.md)：

- 单元测试验证独立语义契约，隔离真实文件系统、数据库、网络、子进程、时钟及用户全局状态。用可控时钟推进窗口、超时及重试，不实际等待数秒。
- `pytest-mock` 提供 `mocker.patch`、`Mock`、`AsyncMock` 和 `spy` 等接口；根据需要使用 `autospec`、`spec_set` 约束替身。复杂状态协作者可用手写 fake（简化模拟实现）。
- 组件集成测试组合真实 SQLite、文件系统、线程、CLI 子进程及受契约约束的设备替身，验证事务、锁、文件交接和中断恢复。并发用明确同步点控制，不依靠随机等待制造竞争。
- 跨组件集成测试组合真实客户端、camctl 与 C 接入模块，验证输入、执行、报告导入及累计确认；设备侧使用受契约约束的替身，不连接真实设备；设备联调与目标硬件性能测量单独安排。
- 重要的替身隔离关系须有相应真实组合验证。正常状态与历史回放结果比较、同一报告重建字节比较，都要有独立于实现路径的契约预期。

### 推进顺序

以下说明总体推进方向；具体任务与依赖由路线图及模块计划承接：

1. 确定模块职责、状态归属、依赖方向和跨模块事务边界。
2. 定义接口、公共内部类型及契约测试，用正常、失败、取消和恢复场景走查。
3. 分批实现完整业务链：先贯通初始化、受理、报告和客户端导入；再接入采集、取回、交付；随后逐步覆盖取消、清理及恢复分支。
4. 每接入一个真实实现就验证协作者组合，持续进行集成验证。

断电恢复所需的事实、身份、进度和接口必须在前两步考虑完整，不能等正常流程写完后再决定保存哪些恢复依据。模块计划给出按行为契约先失败、再实现的任务，路线图组织阶段门禁；集成计划 I6 在实施时将每条正式验收映射到实际生产入口、测试及验证证据。

## 实施验证门禁

第一版集成验证组合真实软件组件，设备侧使用受契约约束的替身，不连接真实相机等设备。设备联调、物理断电和目标硬件性能测量单独安排，不作为本节门禁。

责任边界|单元验证|集成验证
-|-|-
事务与恢复|提交、回滚、未知、取消竞争各分区；事件前后存在性与反向顺序|真实 SQLite 的并发受理、提交边界中断、投影回放及固定 H 读取
索引与资源|分页边界、父子补齐、固定依据不漂移|查询计划与多规模数据；验证读取批次、临时排序及并发调度可以推进
数字适配|长小数、指数、布尔值、范围、倍数、相等与编码边界|输入、数据库、快照、回放、报告的精确往返
日志|水位、通知、取消和故障分类|真实 Janus、监听线程和独立写入进程；强制在触发错误后竞争轮换，验证副本包含该错误
报告与文件|身份不匹配、迟到结果、取消及资源所有权|真实管道、子进程、文件同步和停止；超时后临时文件不被提前复用

这些门禁约束后续实现。临时能力验证不计作上述生产单元或集成测试通过，设备和目标 ARM64 环境须另行验收。

## 跨模块契约检查

检查从输入和可靠保存的事实开始，沿实际操作、结果保存、失败、取消与重启恢复，直到报告或交付等可观察结果。下表定位需要组合验证的边界；状态分类和判定规则由链接中的专题定义。各模块的测试分别通过后，仍须验证整条业务流程。

场景|规则与验收入口|组合验证重点
-|-|-
配置变化后继续工作|[本地配置变化](../architecture/configuration.md#本地配置变化与未完成工作)、[设备绑定异常](../architecture/configuration.md#设备绑定异常的影响范围)|拍摄、跨设备取回、清理、取消及独立收场核对原绑定；因绑定异常而拒绝创建调用时不消耗尝试次数。新操作采用本次配置，历史次数、原身份和终态保持，其他设备继续。
延时摄影等待与完成|[完成核实](../architecture/camera-capture.md#设备自行结束时的等待与完成核实)、[相机验证](../architecture/camera-verification.md)|按驱动声明区分发送与完成，不支持查询时不创建查询预算；发送锚点、跨重启等待、取消资格、文件写完与集合齐备分别验证。
普通交付移入 ready 后，结果保存前中断|[交付恢复决策表](../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)、[产物验证](../architecture/output-verification.md)|组合真实目录、领取模块与 SQLite，在移动、同步、保存前后中断；覆盖文件已被领取并删除、源文件已清理及取消竞争。
来源结束后，取回与清理同时具备条件|[资格授予顺序](../architecture/outputs.md#延后执行时的取回与清理顺序)、[数据库一致性验收](database/consistency-verification.md#取回读取保护与清理)|交换唤醒、选择保存和查询返回顺序；覆盖动作、组、计划、显式产物 ID、自动预览及重启恢复。
取消动作自身被取消|[取消阶段与目标范围](../architecture/task-cancellation.md#取消动作自身被取消)、[取消计划](../superpowers/plans/2026-09-30-camctl-cancellation.md)|分别验证后一次取消是否直接包含原目标，以及完整目标集合包含自身的情形；设备停止、文件结束与交付撤回责任均须保留。
取消后的完整副本与录像处理中间文件|[取回文件生命周期](../architecture/obtaining-outputs.md#取回中间文件的保留与清理)、[录像处理文件生命周期](../architecture/camera-recovery.md#内部中间文件的保留与清理)、[清理预算](../architecture/file-handoff.md#中间文件清理的运行预算)|在取消、实际操作结束、成品登记及删除保存前后中断；覆盖归属未知、后续仍需使用、删除失败、跨会话继续位置及同次运行重复发现。
日志错误触发副本交付|[写入通知](logging-runtime.md#日志适配与写入完成通知)、[日志交付](../architecture/log-delivery.md)、[故障标记](../architecture/log-failure-marker.md)|组合队列、持锁写入与复制、标记及文件交接；覆盖创建或更新标记失败、取消、遗留文件清理和状态库不可用。
业务事实保存后生成历史报告|[历史与报告验收](database/consistency-verification.md#独立历史与报告重建)、[报告验证](../architecture/report-acceptance.md)|比较从初始状态回放、快照正向恢复及当前投影逆向恢复；对象及关联均取自同一完整历史边界，旧报告字节保持。
公共协议跨组件读写|[客户端适配计划](../superpowers/plans/2026-09-30-report-client-adaptation.md)、[跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md)|能力导出、计划生成、run/submit 受理、报告生成、导入及 ACK 消费同一协议和样例；覆盖精确 ID、请求复用、时间、关联与部分结果。
camctl 退出后仍有工具进程|[主程序收场契约](../host-demo/design.md#接入模块的本地进程收场责任)、[跨组件收场任务](../superpowers/plans/2026-09-30-camctl-integration.md#i2-保留退出记录分批检查原组与最终回收)|组合退出观察、原组终止、存活线程检查、最终回收和下一调用；成员退出、权限错误、信息不完整与解析失败分别处理。

具体任务及进度由[模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)跟踪；正式验收条目与实际生产入口、测试和证据的对应关系由[全量验收任务](../superpowers/plans/2026-09-30-camctl-integration.md#i6-全量契约映射软件验收与部署交接)落实。

## 源码边界审计

2026-10-08 对 `apps/camctl/src/camctl` 全部生产源码做了一次静态盘点：解析每个文件的语法树，统计模块依赖、直接外部调用入口（子进程、网络、文件系统、时钟）以及异常、默认值、跳过和清理分支的分布，再逐个入口核对所属模块的责任契约。盘点的结构结论固化为契约检查 [test_external_boundaries.py](../../apps/camctl/tests/integration/contracts/test_external_boundaries.py)：子进程唯一入口、零网络依赖、文件系统调用白名单和零裸 except 四条规则对真实源码断言，并对合成违规样本验证能够报出；该检查随每轮全量回归执行，后续新增模块若需直接外部调用，必须先扩白名单登记责任。模块依赖方向不在本节重复，由 [test_dependencies.py](../../apps/camctl/tests/integration/contracts/test_dependencies.py) 的导入图规则按[模块契约](module-contracts.md)持续检查。

外部调用入口的审计结论如下表。第一版 CLI 无网络面：生产代码不导入任何网络客户端标准库或第三方包，也不建立连接；设备的网络访问由部署侧 adb 工具承担，camctl 只经受管工具进程调用它。

入口类别|审计结论|责任依据
-|-|-
子进程|Python CLI 生产代码的受管工具唯一创建点是 `camctl.operations.process`（`asyncio.create_subprocess_exec`）；`subprocess` 模块也只有它导入，adb 传输适配经其运行接口使用子进程|工具继承 camctl 所属组，CLI 运行期间的正常退出、超时与取消收场由该模块承担。C 接入模块建立独立组，并负责 CLI 退出后的原组检查、保留退出记录及最终回收，软件证据见[接入模块验证记录](../host-demo/verification.md)。
网络|零导入、零连接建立|CLI 部署形态本身无网络面；无服务器代码
文件系统|直接调用（内建 `open`、`os` 文件操作、Path 与 Traversable 方法）只出现在九个登记责任的模块：host_files（文件交接与读取）、logging_runtime（日志与副本文件）、persistence（建库、init 目录切换、回放重建）、reporting（报告生成、发布与撤下）、outputs（取回中间文件与交付读取）、session.locks（会话锁文件）、resources（包内权威资源）、acceptance.input（受理输入读取适配器）、bootstrap（装配目录准备）|各模块对应的交接、日志副本、初始化、报告发布、取回生命周期、锁、资源与受理契约；业务流程模块经仓储连接或 host_files 端口间接访问，不直接触碰文件系统
时钟|真实时钟读取集中在 `bootstrap/clocks.py` 装配端口与 `devices/read_session.py` 端口内的无数据计时；业务流程经注入的 `wall_us`/`monotonic_ns` 端口取时，各装配接受显式注入|测试可控时间与运行假设的时间边界（完成判定、等待与超时）

分支形态的盘点结果与责任归属：

- 异常分支共 375 个 `except`，全部携带显式异常类型，无裸 except；289 个分支命名绑定了错误对象用于转换或登记。数量集中在持久化（98）、文件交接（44）与报告（39），这三处正是中断恢复决策表、交付恢复决策表与报告失败恢复契约定义失败语义的地方，各分支的类型选择与失败分类由对应模块测试背书。
- 清理分支以 `with` 资源块为主（243 个），显式 `finally` 47 个，集中在持久化连接与事务、装配收场和设备读取会话；这些位置的清理语义分别由连接生命周期、退出收场契约和读取会话端口测试覆盖。
- 跳过分支（`continue`）150 个，分布与批量处理模块一致（持久化分页与回放、装配扫描、取回条目处理），属于集合过滤与批次推进的正常控制流，不承载独立失败语义。
- 默认值：dataclass 标量 `field(default=...)` 仅 2 处（会话关闭标志初始 False、来源核实空集合），其余默认值均为 `default_factory` 的空容器或空映射，表示"尚无数据"而非替代判断；数据库读取不出现的字段不折叠为默认值，按各字段自己的状态分类（不存在、未知、未完成）表达。

本次审计未发现无语义的异常吞没、静默默认或无责任归属的外部调用入口；审计发现的唯一行为缺陷（来源解析失败分支缺兄弟齐终态同事务完成计划）已在同日组合复验中按先失败测试修复并登记。

## camctl_host 交付相关复验（2026-10-08）

Linux x86_64、CPython 3.11.16 上完成默认配置路径与真实 C 交接、首次控制残留检查、未启动动作过期与取消、迟到尝试结果及取消收场的专项验证。执行器和结果仓储分别保持动作、操作流程、尝试与设备活动的事实；取消动作结果由本次必要收场结论决定。测试按目录前台顺序执行，包含真实数据库事务恢复与历史恢复，不连接真实设备。完整分类、目录命令和结果由[执行接缝修复记录](../superpowers/plans/2026-10-08-capture-start-residual-gate.md#验证记录)维护；C 构建、包内容及根跨组件结果见[模块验证记录](../host-demo/verification.md)。

## 电机控制与单向通知验证（2026-10-08）

环境为 Linux x86_64 开发容器、CPython 3.11.16、SQLite 3.53.1、Node 24；host 使用 GCC 13.3.0、CMake 3.28.3，客户端浏览器验证使用 Chromium。范围为 [M1—M6](../superpowers/plans/2026-10-08-camctl-motor-control.md)、[HN1—HN6](../superpowers/plans/2026-10-08-camctl-host-notifications.md) 及客户端电机接入。设备侧只使用契约替身。

验证层|命令入口与结果
-|-
Python 单元|`pytest apps/camctl/tests/unit -q`：3489 通过、1 跳过。
Python 组件集成|每个目录单独启动 pytest，顺序执行：acceptance 226、bootstrap 118、cancellation 66、capture 253、contracts 188、devices 46、history 91、host_files 86、logging_runtime 28、motor 12、operations 199、outputs 1790、persistence 106、reporting 346、scheduling 133、session 86，共 3774 通过、2 跳过。
CLI 故障与恢复|`pytest tests/integration/test_camctl_motor_recovery.py -q`：21 通过；包括 11 个保存／写入／未知结果断点、3 个通道失败、过期、4 种取消寻址、未知意图取消和受理失败原参数。
CLI、host 与客户端组合|`pytest tests/integration/test_camctl_motor_notifications.py -q`：5 通过；真实导出／回调／报告领取／导入／ACK、慢回调与下一 run、相机共存、两种中断的 host 自动恢复。
客户端|`pnpm --dir apps/client typecheck` 通过；`test:unit` 530 通过；`test:integration` 370 通过。精确数字与派生恢复的独立审查另复验 166 项和 2 项 Chromium 场景，见[客户端验证](../client/verification.md#电机控制接入)。
host|Debug、Release 各 33 项 CTest 通过，其中单元入口 11 项、集成入口 22 项；包含独立安装消费者和源码包重建，见[host 验证](../host-demo/verification.md#电机通知组件交付复验2026-10-08)。
规格与检查器|协议 4 份 Schema、76 个电机共同夹具；事件 34 类／110 分支；报告依赖 23 个投影／99 条映射；数据库 31 表／3178 项断言；历史 SQL 同步检查通过。Node 的协议、事件和报告依赖检查器测试共 114 通过。

上述 Python 命令统一使用 `UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11` 前缀。3 项跳过分别为非 Linux 父守护、Windows 文件名大小写别名和不携带读取配置的主机源候选，均是既有不适用分区。保留的 3 条 pytest 警告来自既有同步测试的 asyncio 标记。

软件证据沿原请求、受理、发送意图、管道写入、回调、终态、报告及恢复逐段映射，见[电机增量验收映射](software-acceptance.md#十电机控制与主程序通知增量)。只有本次首次可靠提交意图才产生进程内许可；原键核实、历史回放与终态重入均不产生许可。数据库提交结果未知时，关闭旧连接并在同实例新连接上核实原完整请求及事务；取消生效与可靠未发送结论共同保存。测试分别观察实际写入、回调次数、事务及报告，不能用动作成功替代电机到位。

目标 TX2／ARM64 安装、真实主程序和电机、单位与业务范围、目标机性能以及物理断电仍由下节部署交接负责。进程中断测试不构成物理断电证据。客户端原数往返要求 Node 和浏览器支持原生 `JSON.rawJSON`／`JSON.isRawJSON`，其他浏览器与本次增量的 Windows 验证尚未执行。

## 媒体原申请核实验证（2026-10-09）

环境为 Linux x86_64 开发容器、CPython 3.11.16、SQLite 3.53.1。验证对象是检查结果、修复决定、修复结果、修复输出登记及修复输出完整字节的五个保存入口，生产实现位于 [CaptureRepository](../../apps/camctl/src/camctl/persistence/repositories/capture.py)。历史事务、操作键及完整边界的记录规则见[历史格式](database/history-formats.md#历史事务与事件字段)。

[原申请核实集成测试](../../apps/camctl/tests/integration/capture/test_media_original_request.py)使用公开受理、调度、真实 START／STOP 处理器与受设备接口约束的替身建立历史，再通过公开事务保存文件归属、完成事实及处理决定。25 项验证覆盖原键重入、改变处理身份、时刻、媒体观察、修复决定及依据、错误、阶段、输出扩展名、完整字节与摘要的拒绝，并覆盖检查开始键被用于结束观察，以及原对象目录关联缺失。后续检查完成、修复完成及正式产物登记之后，另核实原键仍依据原完整边界返回原结果。原申请核实前后比较完整数据库内容，确认复用和拒绝均不增加或改写事实。

本阶段在只包含提交候选及其已提交前置的独立快照中验证；整个 capture 组件集成目录 278 项通过，单元测试 3585 项通过、1 项跳过。命令分别为 `pytest apps/camctl/tests/integration/capture -q` 和 `pytest apps/camctl/tests/unit -q`，均使用上述 Python 3.11 环境，顺序执行。保留的两条警告来自既有同步测试的 asyncio 标记。

此证据覆盖仓储原申请核实，不覆盖实际媒体结果在默认正常、残留或受限运行入口之间的恢复接线，也不证明真实视频工具或相机能力。工作文件维护和这些消费者的实施进度由[实施路线图](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#分模块审查进度)跟踪。

## 双相机软件组合验证（2026-10-10）

环境为 Linux x86_64 开发容器、CPython 3.11.16，设备响应由受驱动契约约束的替身提供。两款相机各自的录像与原生延时链，以及三条原 C host 链共同运行，7 项通过；长调用及长读取期间，必要停止、取消和报告继续推进。T8 软件增量门禁完成；capture 与 outputs 全目录仍有十项既有失败，分别由原错误和配置闭合计划维护，完整数字与分批验证范围见下述 T8 记录。

双相机的四条 C host 软件组合、Action6 RAW/JPEG 必要产物和安装后样例的增量验证见[双相机实施计划 T8](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t8-容器的四条跨组件演示链与设备采集交付)。设备原始资料、安装后操作步骤及 ARM Linux 验收见[双相机资料采集](../hardware/camera-demo-validation.md)。测试 launcher 中的完整软件契约不使正式驱动的候选能力成为已支持能力；真实响应、结束和文件工具在 T9 核实后再验收正式发行物。

## Action6 的 Windows ADB 通道核验（2026-10-10）

本次由用户在 Windows PowerShell 中连接 Action6，使用 Python 3.11.15、ADB 1.0.41（platform-tools 37.0.1-15733141）和[独立采集器](../hardware/collect_call.py)保存本地 ADB 的实际 stdout、stderr、退出码与耗时。用户确认型号；固件版本尚未取得。Windows 仅用于设备资料采集，不是部署环境。

调用与受控输入|实际结果|能够确定的范围
-|-|-
`exec-out` 执行同一脚本，分别向 stdout、stderr 输出不同标记后以 `7` 退出|本地退出码为 `0`，两个标记合并到 stdout，stderr 为空|此通道不提供本次远端退出码和独立 stderr；不能据本地 `0` 判定设备命令成功
`shell -T` 执行上述脚本|本地退出码为 `7`，两个标记分别进入 stdout、stderr|本次受控退出码正确传回，两个输出流分开
`shell -T` 输出七个已知字节 `00 01 0a 0d 7f 80 ff`，并另输出 stderr 标记|本地退出码为 `0`；stdout 为 `00 01 0d 0a 0d 7f 80 ff`；stderr 标记正确|LF 被扩展为 CRLF，原始字节比较失败；该 Windows 输出不能作为原样媒体字节
`exec-out` 执行同一字节脚本|本地退出码为 `0`；stdout 为原七个字节与 stderr 标记的拼接；stderr 为空|测试字节原样保留，输出流仍然合并；不补造远端退出事实
目录分页中的 `awk`，以及单独将 `a\0b\0` 交给同一 `RS`、`ORS` 设置|目录帧缺少完整路径和末尾 NUL；受控输入得到 `aEND`，本地退出码均为 `0`，stderr 均为空|相机 `awk` 的这组 NUL 处理不满足目录帧契约；退出 `0` 不证明响应完整
将 `a\0b\0` 交给 `IFS= read -r -d ''` 循环，并逐项用 `printf` 输出|本地退出码为 `0`；stdout 精确为 `61 00 62 00`；stderr 为空|相机已有 shell 支持本次受控 NUL 记录读取
使用生产目录脚本连续读取三页，每页容量为 `3`|三页分别有 `3`、`3`、`2` 条路径，帧长度为 `222`、`222`、`155` 字节；结束标记为 `MORE`、`MORE`、`END`；本地退出码均为 `0`，stderr 均为空|三页帧完整、路径有序且游标严格前进；期间用户误触快门新增一张照片
`shell -T` 仅执行完整 `find -print0` 列举|本地退出码为 `255`；stdout 为 `528` 字节，八条路径及末尾 NUL 完整；stderr 为空|路径响应完整仍不能证明成功退出；本次 `255` 的原因未确定，保留失败记录
在上述 `find` 后保存实际退出码，向 stderr 输出诊断标记，再以原退出码结束|本地退出码和诊断标记均为 `0`；stdout 与上次逐字节相同；stderr 为 `CAMCTL_FIND_EXIT=0\r\n`|本次 `find` 正常结束且返回码正确传回；不据此补造上次调用的成功退出
保持相机不拍摄，前后分别完整列举，其间以生产脚本自动读取全部分页|前后完整列举及三页分页均返回 `0`，各得到同样的八条路径；分页为 `3`、`3`、`2` 条，依次返回 `MORE`、`MORE`、`END`；完整列举只输出预期诊断标记，分页 stderr 均为空|本次目录的分页有序、没有遗漏或重复，与前后完整路径列表一致；只核验本次实际文件名和 Windows 通道
对索引 `0006` 的 JPG 执行生产 `test -f` 与 `stat -c %s` 脚本|本地退出码为 `0`；stdout 为 `491520\r\n`，stderr 为空|本次文件存在，观察到的长度为 `491520` 字节；不据此判定文件写完
对同一 JPG 执行生产 `sha256sum < 文件` 脚本|本地退出码为 `0`；stdout 包含 64 位小写十六进制摘要、两个空格、`-` 和 CRLF，stderr 为空|已取得该文件的源端 SHA-256；Windows 行尾不作为 ARM Linux 解析证据
使用 `adb pull` 将同一 JPG 保存到独立本地目录|本地退出码为 `0`，stdout 为空；stderr 报告一个文件已下载、零个跳过；本地长度为 `491520` 字节，SHA-256 与源端相同|本次下载的完整副本与观察到的源文件一致；不代替生产 `dd` 读取和 ARM Linux 原始字节核验
将生产 `dd` 从头读取的字节在相机内交给 `sha256sum`|本地退出码与独立保存的 `dd` 退出码均为 `0`；`dd` 报告读取 `491520` 字节，摘要与源文件相同|相机的 `dd if=文件 bs=65536 skip=0` 能从头读取本次文件；媒体字节没有经过 Windows 输出通道
将生产 `dd` 跳过一个块后的字节在相机内交给 `sha256sum`|本地退出码与独立保存的 `dd` 退出码均为 `0`；`dd` 报告读取 `425984` 字节，摘要与本地副本从偏移 `65536` 开始的部分相同|相机的 `bs=65536 skip=1` 按预期跳过一个块；不代替 ARM Linux 的实际流读取及恢复核验

内置 `/mnt/media_rw/emulated/DCIM` 是实际存在的目录；最初的原始 `find -print0` 输出包含七条路径（两个 MP4、两个 LRF、三个 JPG），NUL 分隔且有末尾分隔符。后续三页累计列出八条路径，其中新增 JPG 的索引为 `0006`，用户说明其间误触了快门。静止目录比对取得同样的八条路径。SD 候选目录 `/mnt/media_rw/sd/DCIM` 明确不存在，不能按可用空目录处理。所列工具均能找到。

上述结果不证明拍摄启动、停止、文件写完或本次产物集合齐备，也不代替 OSMO 360 II 和 ARM Linux 的核验。驱动通道、实际控制响应、固件及完整设备验收继续由[双相机实施计划 T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)跟踪。

文件工具适配验证（2026-10-10，Linux x86_64 容器，Python 3.11.16）：共享调用使用 `shell -T`，保留远端退出和独立 stderr；目录脚本通过排序中的私有游标记录定位，再按 NUL 读取路径，只有读到私有结束记录才输出 `END`。测试替身在 ADB 通道边界使用支持该 shell 接口的 Bash 执行原脚本，文件访问、受管进程及帧解析均使用生产实现。

软件验证范围|实际结果
-|-
全部单元测试|4263 项通过、1 项跳过；2 条既有 asyncio 标记警告
devices 组件集成完整目录|93 项通过；覆盖六种游标分区、空目录、页容量边界、特殊文件名，以及列举、排序和记录读取失败
bootstrap 的 `test_tool_cancellation.py`、`test_recording_stop.py`、`test_timelapse_finish.py`|16 项通过；覆盖实际工具收场、录像停止与延时完成
根 `test_real_camera_demo_roundtrip.py` 与 `test_camctl_c_module_roundtrip.py`|四条双相机软件演示链和三条原 C host 链共同运行，7 项通过

本次软件门禁与上述 Windows 目录和文件访问实测分别记录；ARM Linux 的执行与字节核验仍待进行，候选相机能力未启用。2026-10-23核对当前登记的工作区时，未找到该阶段的原始 T9 诊断日志；本节保留历史汇总，不能代替当前设备诊断。

## Action6 的 Windows 普通录像试录（2026-10-10）

环境与采集器见[同日通道核验](#action6-的-windows-adb-通道核验2026-10-10)。用户所持 Action6 是屏幕损坏的调试机，不能提供界面上的模式、开始和停止观察。试录使用相机原有参数；每次调用仍明确指定 serial，拍摄前后分别完整列举内置目录，保留原文件。

调用或检查|实际结果|能够确定的范围
-|-|-
切换普通视频模式，随后在独立试录中分别发送录像启动和停止候选|三次本地退出码均为 `0`，stderr 均为空；各收到 `Resp message`，长度为 `1`，数据为 `00`|已取得三种命令的实际响应样例；尚未把 `00` 定义为通用设置成功或设备状态观察
启动调用返回后，主机等待十秒再发送停止|从启动调用返回到停止调用发起约 `10.147015` 秒|这是主机调用间隔，不能代替媒体时长或设备开始和停止观察
试录前后完整列举|原八条路径均保留，新增索引 `0007` 的 MP4、LRF 和两个对应的隐藏 `.trinf` 文件，共十二条路径|已取得本次试录新增路径；`.trinf` 的含义以及完整产物集合仍待核实
对新增 MP4 取得源长度、SHA-256，再使用 `adb pull` 下载|三次调用均返回 `0`；源端和本地均为 `29302405` 字节，SHA-256 相同；用户确认播放器可以打开该副本|已取得一致且可打开的副本，不代表整段逐帧解码通过
使用本地 `ffprobe 9.0.2-full_build` 检查该副本|版本和检查调用均返回 `0`，stderr 为空；HEVC 视频流为 `1920×1080`、`30000/1001` fps、时长 `8.875533` 秒；容器时长为 `8.896000` 秒，长度为 `29302405` 字节；另列出 AAC、data 和 MJPEG 流|按[原片检查规则](../architecture/camera-recovery.md#第一版原片检查的范围)，采用容器时长 `8.896000` 秒；未采集 MJPEG 流的用途，主机等待间隔与媒体时长的差异原因尚未确定

本次核实了上述命令组合后出现新增文件，并能取回该 MP4、用播放器打开及检查媒体属性。除上述媒体属性外，其余生效参数、设备开始和停止依据、文件写完及集合齐备依据仍未核实；本次试录不代替正式参数预设、原生延时摄影、OSMO 360 II 或 ARM Linux 的完整演示验收。

随后按[录像样例](examples/camera-demo/action6-record.json)依次采集 4K/30 fps、Pro 开启、Auto 曝光、手动白平衡 5200 K 和曝光补偿 0 EV 五条设置候选的实际响应。五次调用的本地退出码均为 `0`，stderr 均为空；各收到长度为 `1`、数据为 `00` 的 `Resp message`。白平衡和 EV 命令保留 `-S test`。这些结果只证明已取得上述设置的响应样例，设置是否生效尚未确认。

继续采集的 D-Log M、Wide 和关闭增稳命令均取得长度为 `1`、数据为 `00` 的响应。F2.8 命令 `0x26 1801` 实际发送 `18 01`，原样复测的第二次请求相同；F4.0 候选 `0x26 9001` 实际发送 `90 01`。三次光圈响应长度均为 `1`、数据均为 `e3`，请求与交接资料及生产映射一致。上述六次本地退出码均为 `0`，stderr 均为空。`e3` 的含义和出现原因尚未确定，两档光圈设置均保持未确认。

高码率候选 `simulate_device -s bitrate 2` 本地退出码为 `0`，stderr 为空。输出显示工具找到 `DeviceRecordRecSettingBitRate`、转换旧式通知、生成数据，并完成服务连接及注册，但没有提供设备码率生效确认。录像样例的全部设置命令均已取得原始返回，完整参数预设仍未通过设备核验。

`dji_mb_ctrl -h` 输出通用 `Usage`，本地退出码为 `1`，stderr 为空；帮助没有定义 `e3` 或光圈命令的适用条件。光圈的现场条件、完整命令、三次响应及待补充信息已整理为[资料询问文档](../hardware/camera-control-questions.md#action6光圈设置返回-e3)，供后续向资料提供方确认。

设备断电重启后，用户执行[统一录像诊断脚本](../hardware/action6_record_probe.py)，采集目录为 `action6/record-probe-20261010T113947.161059Z`。九条模式和设置命令通过预期 `00` 响应检查，高码率日志通过既有样例检查；脚本完成 START、十秒主机等待和 STOP 检查，并列出新增 MP4。对索引 `0009` 的 MP4 拉取后，早期源长度与副本长度不同，脚本在媒体检查前报错，没有生成最终摘要文件。原文件、本地副本及所有调用记录继续保留。

随后对同一源文件和保留副本进行只读诊断，并将新下载的文件另存。下列时刻均为 2026-10-10 UTC；STOP 调用在 `11:40:00.184611` 返回，原下载从 `11:40:00.903100` 持续至 `11:40:02.499633`，返回 `0`。

观测对象与顺序|实际长度及 SHA-256|能够确定的范围
-|-|-
原源长度查询在 `11:40:00.366790` 返回，摘要查询在 `11:40:00.833096` 返回|长度为 `64436061` 字节；摘要为 `6e48c0223643dced20c73922924698f005fc16770ebd6cd90c87a4c6ee0cfcb3`|长度与摘要来自两次独立查询，不能视为一个原子快照
原下载文件|长度为 `64637726` 字节，摘要与原源摘要相同|原脚本仅因长度相差 `201665` 字节报错，未发现摘要不一致
新下载前的源观测、新副本和新下载后的源观测|三者均为 `64637726` 字节，摘要均与原源摘要相同；两组源查询分别在 `11:48:47.787352`／`11:48:48.302837` 和 `11:48:50.424160`／`11:48:50.923065` 返回|新旧副本均与后续源观测一致；不能定位长度变化的具体时刻或原因，也不证明两次试验之间始终稳定

新副本的 `ffprobe` 结果取得 HEVC 视频流 `3840×2160`、`30000/1001` fps、时长 `8.875533` 秒，以及 MJPEG 视频流。容器时长为 `8.875533` 秒，长度为 `64637726` 字节。由此核实了本次产物的分辨率、帧率与媒体属性；其余设置、十秒有效采集、设备结束、源文件写完和完整产物集合仍未核实。后续诊断的媒体检查结果与原脚本在媒体检查前结束分别记录。

## Action6 的 Windows 原生延时采样（2026-10-10）

用户在 Windows PowerShell 中使用现有 Python 3.11 环境、ADB 和 `ffprobe`，执行[统一延时采集](../hardware/camera-demo-validation.md#action6-原生延时的统一采集)的 4K/30 fps、Auto、8 秒间隔、30 分钟和仅视频组合。设备仍为上述屏幕损坏的调试机，实际拍摄状态不能通过界面观察。

用户提供 `capture-observation.json`、START 的 `call.json` 和完整 stdout，以及前后两个 `storage.json`。下表的时刻均为 2026-10-10 UTC；计时值来自各自的单调钟记录，不以 UTC 差值替代。

调用或观察|实际结果|能够确定的范围
-|-|-
延时模式、4K/30 fps、Auto 曝光及完整预设|脚本分别记录收到预期 `00`|已取得设置响应样例，实际生效仍未知
START `dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01`|调用在 `12:16:27.467501` 发起，`12:16:27.571756` 返回；本地退出码 `0`，采集器耗时 `109000000` ns；完整响应长度 `1`、数据 `00`|START 快速返回，没有等待三十分钟任务结束；实际设备开始与响应含义仍未核实
主机采样间隔|脚本记录 START 阶段约 `0.172` 秒，距 START 发起 `1800.0` 秒；目录采样开始时刻为 `12:46:27.415314`|已执行预设长度的主机观察间隔，不代表设备实际采集了三十分钟或已经自然结束
前后存储范围|内置 DCIM 均为同样的 14 条路径；SD DCIM 均为 `absent`，目录差集为 `[]`|本次成功列举的范围内没有新增路径，不能解释为目录读取失败，也不能证明没有拍摄
MP4 后续检查|脚本报告未取得新增 MP4，在视频下载与媒体检查之前结束|本次没有可检查的新视频；实际开始、结束、写完与最终产物集合均保持未知

随后用户提供模式和预设调用的完整 stdout。模式消息为 `000200e1`，发送长度 `1`、数据 `02`；预设消息为 `0002006c`，发送长度 `17`、数据 `0400005000080700000000000000000000`。两次响应长度均为 `1`、数据均为 `00`；实际发送内容与[交接样例](../hardware/camera-control-handoff.md#action--action6-静止延时摄影)及脚本一致。

用户执行[只读复查](../hardware/camera-demo-validation.md#action6-延时采样后的只读复查)，在 `13:23:53.460080` 开始采样。新摘要保存 `observation_only=true`、原采集记录及基准引用，结果为 `sample_status=no_new_mp4`、`new_files=[]`、`video_checks=not_attempted`；内置目录仍为同样的 14 条路径，SD 范围仍为 `absent`。该时刻与 START 的主机 UTC 发起记录相隔约 67 分 26 秒，这是两份 UTC 记录的对照，不是跨两次程序运行的连续单调计时证据。复查没有再次发送拍摄控制命令，也没有进入下载或媒体检查。

原调用及目录记录继续保留。当前证据尚不能确定未生成文件的原因；现有资料缺少当前拍摄状态查询、设置读回及 Action6 延时停止接口，完整请求和待补资料见[延时资料询问项](../hardware/camera-control-questions.md#action6原生延时-start-返回-00两次采样均无新增路径)。实际开始、结束、文件写完和集合确定仍未知；本次不提供原生延时驱动启用或 ARM Linux 演示通过依据。

### 30 秒候选与完整设置顺序的试验

用户在同一 Windows 环境执行[秒级延时诊断](../hardware/camera-demo-validation.md#action6-秒级延时试验)，并确认本次试验前已经正常关机并重新开机。采集目录为 `action6/timelapse-probe-20261010T135930.908626Z`。已提供终端输出及 `summary.json`；START 原始调用和两个存储范围的完整列表尚待补齐。

调用或观察|实际结果|能够确定的范围
-|-|-
延时模式、4K/30 fps、间隔与时长、Auto、输出组合，随后 START|终端记录五项设置及 START 均通过脚本的预期 `00` 响应检查|已取得该次调用和响应检查结果；实际设置及启动效果仍未知
试验要求与候选负载|摘要记录 4K/30 fps、Auto、8 秒间隔、30 秒、仅视频；`preset_payload=04000050001e0000000000000000000000`，`preset_basis=experimental_duration`|所选要求和 17 字节候选已记录，不能证明相机接受或应用了 30 秒时长
START 阶段与主机采样间隔|摘要记录 START 阶段约 `0.813` 秒、距发起 `30.0` 秒；目录采样在 `2026-10-10T14:00:03.368182+00:00` 开始|这是脚本计时结果，START 的采集器原始耗时尚未提供；不代表设备拍摄了 30 秒或已经结束
目录差集与视频检查|`sample_status=no_new_mp4`，`new_files=[]`，`observed_mp4_files=[]`，`video_checks=not_attempted`|本次列举的范围没有新增路径，没有进入下载或媒体检查；目录差集不检查旧路径对应文件的内容变化

摘要中的实际开始、自然结束、文件写完和最终集合均为 `unknown`。本次相较原三十分钟试验同时改变了设置顺序和持续时间，不能据两次无输出判定哪项设置已经生效或哪项前置无关。尚未取得该次后续只读观察，短值支持、当前活动及产物生成依据仍未知；真机核验按下述[更换设备的安排](#手工诊断的目录观察)继续。

### 手工诊断的目录观察

用户随后提供相机 shell 中两个 DCIM 范围的 `find` 输出，并补充实际预设及 START 的完整 stdout。此次实际执行的是 8 秒间隔、1800 秒持续时间、仅视频的完整预设，不是 30 秒候选：

```sh
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400005000080700000000000000000000
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01
```

预设消息 `0002006c` 的发送长度为 `17`，数据与上述完整负载一致，序号为 `6`；START 消息 `00020001` 的发送长度为 `1`、数据为 `01`，序号为 `7`。两次响应长度均为 `1`、数据均为 `00`。手工 shell 的本地退出码及主机计时未提供；设备日志的时钟显示为 2000 年，不能用于核实主机日期或实际持续时间。

内置目录 `/mnt/media_rw/emulated/DCIM/DJI_001` 列出以下四个文件，SD 目录查询报告 `No such file or directory`：

文件名|本次取得的观察
-|-
`DJI_20000101080811_0001_D.MP4`|用户确认拍摄前不存在，在 START 后查看目录时出现
`DJI_20000101080811_0001_D.LRF`|文件路径出现在内置目录列表中
`.DJI_20000101080303_0002_D.LRF.trinf`|隐藏文件路径出现在内置目录列表中
`.DJI_20000101090012_0010_D.MP4.trinf`|隐藏文件路径出现在内置目录列表中

用户说明目录是在 START 后查看，估计经过几十秒，但未保留准确计时，该估计不用于持续时间判定。新增 MP4 的依据为用户对拍摄前后存在状态的确认，启动前的原始目录列表尚未提供；其余三个文件的新增性未核实。没有本次文件取回、源与副本比对或媒体检查结果，尚不能确认该 MP4 是有效延时视频，也不能判断设备实际结束、文件写完和集合齐备。该次手工观察与前述两次脚本无新增路径的结果分别保留，不将其表述为 30 秒候选已通过。

用户报告该调试机存在故障：延时拍摄一段时间后停止工作，无法完成任务。软件侧以模式、分辨率、一次完整预设、对应曝光和 START 的流程作为实施依据；现有设备不再承担完整延时验收。媒体检查、正常结束、文件写完及集合齐备的真机核验留待新的 Action6，到位后按正常设备任务重新采集证据。这项安排不将故障机提前停止解释为预设正常结束，也不扩展第一版的软件故障范围。

## Action6 延时设置顺序的软件验证（2026-10-10）

环境为容器开发环境和 Python 3.11.16。验证范围是[完整设置派发任务](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#action6-延时设置的完整派发顺序)：候选命令映射与实际控制入口共同执行延时模式、分辨率、所选完整预设、对应曝光设置和一次 START。一次 `6c` 同时配置间隔、持续时间和输出类型。三组任务各自的三种输出分别按完整字面量核对，共九个候选，不生成未提供的参数组合。

命令、参数和驱动专项的红阶段取得 `29 failed, 130 passed`；诊断单元先取得一个顺序反例失败，诊断集成的 49 项先取得五个失败。实施后专项 159 项通过，全量单元为 `4294 passed, 1 skipped`，另有两项既有 asyncio 标记告警，分别位于 `test_source_stream_protocol_shape` 和 `test_local_exit_is_exclusive`。驱动验证覆盖八项手动设置各自不确认、完整预设前后资格失效及期限耗尽、一次 START 返回未知。

诊断单元 43 项、[采集器组合测试](../hardware/tests/integration/test_action6_record_probe.py) 49 项通过，组合测试耗时 27.883 秒。验证覆盖 30／10／1800 秒的完整负载和单次预设、四项设置各自的退出码／stderr／非 `00`／截断响应失败、一次 START，以及旧五设置目录的 30／10 秒只读复查。原目录、下载、源与副本、媒体、摘要保存及元数据校验分区继续通过；历史复查没有控制或等待，原记录保持不变。

软件门禁由主执行者前台独占、顺序执行。组件集成的 devices 完整目录 93 项、acceptance 完整目录 239 项通过；bootstrap 的 `test_adb_camera_dispatch.py`、`test_tool_cancellation.py` 和 `test_adb_camera_products.py` 共 34 项通过。独立只读审查核对九个完整候选、Schema 同源、实际派发、资格及期限、旧记录复查和有效文档。候选定义不自动启用正式能力；普通录像、OSMO 及 START／STOP 的原有边界保持不变。

根跨组件 `test_real_camera_demo_roundtrip.py` 四项通过，耗时 48.98 秒。真实 C host、CLI、状态库和客户端串联两款相机的录像／延时、取回、报告与领取；设备事实由受接口约束的测试替身提供，该结果不代替真实相机或 ARM Linux 验收。

上述证据不代表真实相机设置生效或实际开始和完成。Windows Auto 30 秒试验的无新增文件结果及手工试验的新增 MP4 分别见[原始试验记录](#30-秒候选与完整设置顺序的试验)和[手工目录观察](#手工诊断的目录观察)。流程作为软件实施依据，完整延时与产物完成验收等新的 Action6 到位后继续；正式能力仍未启用。

## Action6 普通录像接入的软件验证（2026-10-11）

环境为 Linux x86_64 开发容器、Python 3.11.16 和 SQLite 3.53.1。验证对象是[Action6 普通录像接入](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#action6-普通录像的完整演示接入)：安装后的内置驱动按用户采用的正常设备运行假设解释完整响应，停止后实际等待五秒，再列举内置存储并登记一份 MP4 原片。设备通信由受实际命令和文件接口约束的替身提供，不连接相机。

生产定义允许分别省略光圈和码率；当前预设省略两项，受理、固定参数和实际设置均保留这一要求。显式设置未知时不能继续 START。实际响应解释区分完整单字节 `00`、拒绝、截断、多段响应、stderr 与工具失败，不以退出码单独确认设备效果。控制与参数专项先取得对应失败测试，实施后 289 项通过。

文件完成等待在原 STOP 结果可靠保存后执行。实际间隔、固定规则、当前文件观察页和首次实际等待页分别保留；后续分页、重试及重启复用原来源。单文件完成采用 `STOP_RETURN_AND_WAIT`，核对原活动、STOP、可靠页范围、原绑定、长度、观察及最终定位。原键按完整元信息申请、原时刻及原事务后状态核实，保存未知不重新列举或生成新的申请。专项覆盖等待不足、中断、非法引用、部分文件读取失败、保存未知、历史回放及旧执行定义。

主执行者前台独占、顺序执行测试。全部单元测试取得 `4501 passed, 1 skipped`，耗时 11.42 秒；两项既有 asyncio 标记告警继续保留。等待来源专项取得 44 项单元通过，等待与恢复专项取得 17 项集成通过。capture 完整目录取得 `917 passed, 5 failed`，耗时 335.56 秒；该目录没有全部通过。

五项失败均在 [test_emergency.py](../../apps/camctl/tests/integration/capture/test_emergency.py)：未尝试停止、停止仍未确认及其历史复用场景保存的公共错误缺少必需的 `details`。在改动前的干净源码快照中独立复验该文件，同样取得 `9 passed, 5 failed`，失败用例和原因相同。该项仍归[结果错误专项](../superpowers/plans/2026-10-09-camctl-result-errors-and-report-projection.md)，未通过的既有契约不计入本次正常录像演示的已完成范围。

bootstrap 完整目录取得 `940 passed, 1 skipped`，耗时 707.55 秒。history 完整目录取得 `195 passed, 10 failed`，耗时 20.59 秒；在同一干净源码快照中顺序复验，取得相同用例及原因的 `195 passed, 10 failed`，耗时 20.88 秒。其中九项因应急停止保存的公共错误缺少 `details` 而失败，一项是 [test_complete_history.py](../../apps/camctl/tests/integration/history/test_complete_history.py) 的事件验收映射遗漏 `RESULT_PAGE`。应急错误按上述专项补齐；历史验收须补入可靠结果页的归属、原键核实和回放场景，并审计同类映射遗漏。历史目录未全部通过，本次不将这两个既有缺口记为已验收。

contracts 完整目录取得 `192 passed`，耗时 3.91 秒，覆盖实际模块依赖、公共枚举、Schema 与投影边界。原 `test_real_camera_demo_roundtrip.py` 和 `test_camctl_c_module_roundtrip.py` 顺序共同运行，取得 `7 passed`，耗时 76.06 秒；四条原候选软件链与三条 C 接入链保持各自范围。

[安装版内置驱动的跨组件测试](../../tests/integration/test_action6_builtin_demo_roundtrip.py)取得 `1 passed`，耗时 29.75 秒。测试从源码外构建并安装 wheel 与锁定依赖，通过 PATH 上的 ADB 替身串联真实 C host、CLI、状态库和客户端；没有测试装配桥注入驱动。实际任务按十秒执行，STOP 后至少等待五秒才能登记正式输出。测试核对原 STOP 与文件完成页引用、唯一 MP4、旧文件和辅助文件排除、原片与副本的长度及 SHA-256、客户端可靠接受报告和主机吸收 ACK；取回后源文件仍保留。

独立交付包含 camctl 0.1.0 wheel、锁定运行依赖、C host 1.0.0 源码包、配置和计划生成器及中文操作说明。重新安装该 wheel 后实际 `describe` 只公布 Action6 普通录像，能力文件明确为占位 serial 的软件说明。主执行者核对十四个载荷的 SHA-256、wheel 中全部 201 个生产 Python 文件与最终源码逐字一致，以及 C host 包不含构建二进制。目标主机从源码包现场构建 C 模块，个人电脑客户端单独部署；目标机运行步骤不依赖仓库的 `apps/camctl` 目录。

规格与交付静态检查通过：数据库 31 表、3190 项断言，历史 SQL 与登记一致；事件登记 35 类、112 分支，报告依赖 23 投影、99 映射、123 来源；公共协议的 Schema、共享夹具及错误详情检查通过。文档本地文件链接及修改文档的标题锚点检查通过，`git diff --check` 无错误。

软件证据只证明组件按声明协作。五秒等待尚无真机验证依据，软件结果不证明实际媒体时长、分辨率、帧率或目标主机性能。ARM Linux 的十秒任务、实际文件完成及副本核验按[完整验收](../hardware/camera-demo-validation.md#arm-linux-的完整验收)执行；Action6 延时和 OSMO 另按各自设备条件继续。

## ARM 主机部署准备（2026-10-10）

本次证据由用户从目标主机回传，`date -u` 显示 `2026-10-10 18:00:38 UTC`，本记录按该 UTC 日期登记。目标为 `aarch64`、Ubuntu 18.04.6，运行账户为普通用户 `cld`。部署工具为 uv 0.13.0（aarch64-musl）、CMake 3.10.2 和 GCC 7.5.0；ADB 为 1.0.39，版本标识为 `1:8.1.0+r23-5~18.04`。设备列表中目标 serial `123456789ABCDEF` 的状态为 `device`。

用户在该主机核对交付包、验证 ADB 执行通道并安装 Python 包，从交付源码现场构建 C host。各项取得的实际结果如下：

检查对象|实际结果与证明范围
-|-
交付包校验|十四个载荷的 SHA-256 均通过；证明收到的载荷与交付清单一致
`adb shell -T` 受控退出与输出分流|实际退出码为 `7`，stdout 为 `b'OUT\n'`，stderr 为 `b'ERR\n'`；证明本机通道保留本次远端退出码及独立输出
`adb shell -T` 二进制输出|实际退出码为 `0`，stdout 为 `b'\x00\n\r\x80\xff'`，stderr 为 `b''`；证明本次已知字节序列原样返回
Python 环境与安装|用已核验的 `/home/cld/.venv/bin/python` 创建新的 `/home/cld/.camctl/venv`，实际 Python 为 3.11.17，SQLite 为 3.53.1；九个锁定运行依赖和 camctl 0.1.0 安装成功，camctl 导入来自该新环境的 site-packages
C host 目标构建与安装|交付源码以 `HOST_BUILD_TESTS=OFF` 和 Release 配置在目标主机构建、安装，进度达到 100%；安装后的 `/home/cld/.camctl/host/bin/host-demo` 为 `293640` 字节。此项证明该源码在本机工具链构建与安装成功，尚不证明运行时业务链

本记录只证明包校验、上述执行通道、Python 安装和目标主机 C 构建，不覆盖后续初始化、能力导出及完整业务链。后续部署与现场验收结论由[实施计划 T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)分别记录，不能由本条部署准备结果声明演示成功。

## 应急错误与历史报告的软件验证（2026-10-11）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1。主执行者独占、顺序运行单元和各组件目录；设备调用使用受真实接口约束的替身。本次范围由[结果错误专项](../superpowers/plans/2026-10-09-camctl-result-errors-and-report-projection.md#应急错误登记与最终补记)维护。

两类正式应急错误分别表达未开始停止尝试和已尝试但停止未确认，保存实际目标活动及原原因。纯编排保留已有次数；保存入口及事件守卫核对结果分区、完整公共错误、目标活动的相同错误和本会话固定配置。零尝试的未知配置不补值，有尝试须保存完整、相同且合法的配置。非法原因、内部次数类型、非有限或越界秒数、缺少配置及登记资源失败均按真实事务拒绝，测试核对整库事实未变及事务已退出。

真实报告测试为两个错误分别生成仍运行、结束未确认和已结束的六个分区。活动前置经正式 START 和调用返回事实建立；报告分别保留相应设备情况或省略已结束的设备情况。后续会话保存可靠停止后，以新连接重建同一冻结 H，字节、长度和摘要保持，旧历史及原动作终态不变。仓库外实际构建、安装的 wheel 也携带这两项正式登记及详情约束。

各行从仓库根目录执行，使用 `UV_PROJECT_ENVIRONMENT=/workspaces/camctl/apps/camctl/.venv uv run --frozen --project apps/camctl --group test --python 3.11 pytest <验证对象> -q`；日志重定向到各自文件。最终结果如下：

验证对象|实际结果|耗时与本机日志
-|-|-
`apps/camctl/tests/unit`|4601 passed、1 skipped；2 条既有 asyncio 标记警告|12.06 秒；`/tmp/camctl-v1-emergency-all-unit-final.log`
`apps/camctl/tests/integration/capture`|982 passed|347.85 秒；`/tmp/camctl-v1-emergency-capture-full-final.log`
`apps/camctl/tests/integration/history`|205 passed|20.11 秒；`/tmp/camctl-v1-emergency-history-full.log`
`apps/camctl/tests/integration/reporting`|373 passed|38.79 秒；`/tmp/camctl-v1-emergency-reporting-full.log`
`apps/camctl/tests/integration/operations`|200 passed|10.55 秒；`/tmp/camctl-v1-emergency-operations-full.log`
`apps/camctl/tests/integration/bootstrap/test_distribution.py`|17 passed；本专项未重跑 bootstrap 全目录|21.08 秒；`/tmp/camctl-v1-emergency-distribution.log`

RESULT_PAGE 的事件验收映射指向实际结果页历史测试，未标为综合剧本已经产生。其二十项锚点验证通过不表示新文件、设备文件及页目录的完整对象成员已在三条历史路径按独立预期全部核验；这项剩余证明由[H7 结果页验证](../superpowers/plans/2026-09-30-camctl-history.md#h7-结果页验收映射与剩余证明2026-10-11容器)跟踪。

本次完成错误登记、最终补记与上述软件验证。默认 bootstrap／session 尚未接入真实应急停止、有限尝试及最终补记，责任仍在[capture C7](../superpowers/plans/2026-09-30-camctl-capture.md#c7-有限安全收场与应急最终补记)。原键事务核实不等于默认会话已持有 UNKNOWN 保存的恢复责任。Action6 现场演示结论见[T9 验收记录](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)，本次软件通过不作为设备、ARM 运行或物理断电验收。

## 清理查询结果保存与恢复的软件验证（2026-10-11）

环境为 Linux x86_64 开发容器、Python 3.11.16、SQLite 3.53.1。验证对象是[清理查询结果保存与恢复阶段](../superpowers/plans/2026-10-10-camctl-cleanup-query-result-closure.md#阶段记录2026-10-11)。设备调用由受真实接口约束的替身提供；结果事务、连接更换和历史保存使用真实 SQLite 组件。

查询实际返回后，会话先固定原完整结果、票据、数据库身份、唯一 key 和事实时刻。结果保存未可靠完成时，成员结果、普通删除和重试锚点不能消费该观察。连接更换只重送原申请；成员和伴随流程的保存责任分别保留。新会话从已提交的原结果及完整事务恢复观察，核对观察版本、结构和成员身份，不要求当前驱动重新解释原事实。集合恢复先完成原本地责任，再安排普通设备操作。

主执行者独占、顺序执行以下命令，单元与集成分开。各行从仓库根使用 `UV_PROJECT_ENVIRONMENT=/workspaces/camctl/apps/camctl/.venv uv run --frozen --project apps/camctl --group test --python 3.11 pytest <验证对象> -q`，输出重定向到对应日志。

验证对象|实际结果|耗时与本机日志
-|-|-
`apps/camctl/tests/unit`|4694 passed、1 skipped；2 条既有 asyncio 标记警告|13.21 秒；`/tmp/camctl-cleanup-final-unit-verified.log`
`apps/camctl/tests/integration/outputs`|1911 passed、2 skipped|282.35 秒；`/tmp/camctl-cleanup-final-outputs.log`
`apps/camctl/tests/integration/bootstrap` 首次全目录|957 passed、2 failed、1 skipped；未取得该目录单次全绿|742.23 秒；`/tmp/camctl-cleanup-final-bootstrap.log`
bootstrap 的 `test_output_binding_cancellation.py`、`test_output_cleanup_recovery.py`、`test_cleanup_query_default_recovery.py` 最终复验|35 passed，包含全目录的两个失败分区|28.18 秒；`/tmp/camctl-cleanup-final-bootstrap-verified.log`
新增 outputs 文件及既有恢复、重试、计时和源文件清理组合|91 passed|60.35 秒；`/tmp/camctl-cleanup-final-focus.log`

bootstrap 的两个失败分区涉及已保存未知查询、取消生效及当前绑定缺失或不匹配。有效契约要求先使用原未知结果完成成员和伴随流程，保持 `UNCONFIRMED/delete_unconfirmed`；当前绑定异常只阻止新的设备调用。最终专项精确验证原尝试、完整结果与事件、错误详情、源文件事实、零新增调用，以及取消结算前后的目标动作状态。全目录执行后生产代码保持相同，测试按上述契约验证；未再次执行全部 bootstrap 用例，不将专项结果表述为全目录的一次通过。

静态检查取得 3754 个本地文件链接通过，修改文档的 242 个标题锚点均可解析，13 个修改 Python 文件通过 3.11 AST 检查；暂存及未暂存 diff 均通过空白检查。

本次结果不证明相机、ARM64 运行、性能或物理断电行为。QUERY-only 的共同建档、取消与限制转换、成员及伴随仓储的完整原键证明和专项历史回放仍由[剩余责任](../superpowers/plans/2026-10-10-camctl-cleanup-query-result-closure.md#尚未完成的前置与核验)跟踪，完整清理计划及第一版均未完成。阶段提交后停止；后续客户端分支合并及 Action6 现场验收结果见[演示交付状态](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)。

## 视频大小估算的协议同步验证（2026-10-11）

本次在客户端分支合并后的 Linux x86_64 容器验证 Python 同源导出及跨组件交接。环境为 Python 3.11.16、SQLite 3.53.1、uv 0.12.10、Node.js 24.20.0、pnpm 12.3.4。Python 使用 `apps/camctl/.venv`；每个集成目录或专项分别独占、顺序运行。客户端命令设置 `VITEST_MAX_WORKERS=2`，单元与集成分别执行。

所有 Python 命令均从仓库根使用以下前缀，后接表中测试范围及 `-q`：

```bash
UV_PROJECT_ENVIRONMENT=/workspaces/camctl/apps/camctl/.venv uv run --frozen --project apps/camctl --group test --python 3.11 pytest
```

范围|命令中的测试路径或检查入口|实际结果
-|-|-
Python 全部单元|`apps/camctl/tests/unit`|4723 项通过、1 项跳过，12.96 秒；保留两条既有的同步测试 asyncio 标记警告。
公共格式及精确编码|`apps/camctl/tests/integration/contracts`|438 项通过，4.22 秒；其中视频估算共同原始文本 246 项按 `encode_valid` 独立核验。
驱动及同源目录|`apps/camctl/tests/integration/devices`|131 项通过，2.38 秒；包括新增的真实参数关联、整份失败及精确数值分区。
CLI 与安装资源|`apps/camctl/tests/integration/bootstrap/test_command_output.py apps/camctl/tests/integration/bootstrap/test_package.py`|6 项通过，1.20 秒；独立安装 wheel 内的全部登记资源与根权威来源逐字节一致。
视频估算跨组件闭环|`tests/integration/test_video_size_estimate.py`|12 项通过，27.82 秒。
安装版内置驱动与 C host|`tests/integration/test_action6_builtin_demo_roundtrip.py`|1 项通过，31.82 秒；独立安装的 wheel 导出 130 Mbps 假定说明，经真实 C host 完成录像、取回、领取、客户端导入和 ACK 软件链。
公共协议检查器|`node scripts/check-protocol.mjs`、`node --test scripts/check-protocol.test.mjs`|格式检查通过，247 项脚本测试通过；结构检查与真实编码／加载分开验收。
客户端全部单元|`pnpm --dir apps/client run test:unit`|1864 项通过，36 个文件通过，13.38 秒。
客户端全部集成与浏览器|`pnpm --dir apps/client run test:integration`|972 项通过，29 个文件通过，266.06 秒；前置 `tsc --noEmit` 与 Vite 构建通过。

构建产物的主 JavaScript 包为 758.64 kB，日志保留超过 500 kB 的体积警告以及 `@base-ui/react` 依赖中 `use client` 模块指令的提示。构建与测试均成功，不能将这次结果描述为无警告。

跨组件闭环使用同源虚构驱动，经真实 `describe`、客户端 Application、HTTP 草稿和能力状态、精确解析、估算与计划导出，再由真实 CLI 受理执行并发布报告，客户端导入报告并保存累计确认。录像的一秒虚构参考值为 16,250,000 字节；延时实际执行三秒采集，其估算成片为 0.1 秒和 2,187,500 字节。两条报告都保存替身产物的实际 8192 字节，不携带估算资料；生效参数及执行定义继续使用原拍摄要求。

精确数字分区包括 `130.125` Mbps、数学整数帧数 `240`／`240.0`／`2.4e2`，以及不能被舍入为整数的 `1.00000000000000000001`。非法声明使真实 `describe` 退出 1 且 stdout 为空；客户端重新加载同一非法内容时保留此前完整说明与诊断。合法的缺失元数据、查表未覆盖及可选引用省略仍能导出计划。估算前后的既有草稿版本和内容保持一致。

内置相机使用在任务说明中标注的固定假定参考值，数值与说明由同源定义生成。当前可执行的 Action6 录像声明 130 Mbps；元数据不启用缺少任务工厂的候选类型，也不改变 START／STOP 含义及五秒文件完成运行假设。现场演示的结论与依据见[演示交付状态](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)，不将估算声明记为实际码率。

静态复核通过：本地文件链接 3883 项，修改文档的标题及显式锚点 263 项，Python 3.11 AST 检查 12 个修改文件；`git diff --check` 无空白错误。独立审查核对同源声明、候选过滤、精确 HTTP 草稿、导出及报告边界，没有未解决的阻断问题。

## 普通录像参数与客户端字段的软件验证（2026-10-11，Windows）

本次在 Windows 开发机使用 `apps/camctl/.venv311` 的 Python 3.11.15 与 SQLite 3.53.1，验证[普通录像整数秒契约](../architecture/camera-parameters.md#普通录像时长)和客户端参数字段接缝。Python 单元与每个组件集成目录分别、独占、顺序运行；客户端完整门禁和当前页面的只读证据见[客户端参数字段验证](../client/verification.md)。本次执行步骤由[参数字段实施计划](../superpowers/plans/2026-10-11-client-parameter-fields.md)维护。

普通录像的两个候选型号共用 `integer`、`minimum: 1`、`maximum: MAX_OBJECT_ID // 1000`，没有默认值或小数步进。数学整数写法 `1.0`、`1e0` 合法，非整数与非法类型拒绝；最大合法值 `9223372036854775` 秒精确转换为 `9223372036854775000` 毫秒。单元反例在修改前为 8 项失败、195 项通过，修改后 203 项通过；该专项数量不再叠加到全部单元数量。

以下命令均从仓库根目录执行。测试进程前置 Git Bash 路径并统一父子进程的 UTF-8 模式，避免本机的 WSL `bash.exe` 首命中及父子进程文本编码不一致。测试没有更改部署依赖、公共协议或设备命令。

```powershell
$env:PATH = 'C:\Program Files\Git\bin;' + $env:PATH
$env:PYTHONUTF8 = '1'
& '.\apps\camctl\.venv311\Scripts\python.exe' -m pytest <表中测试路径> -q
```

测试路径|实际结果
-|-
`apps/camctl/tests/unit`|4757 项通过，23.61 秒；保留 2 条既有 asyncio 标记警告。
`apps/camctl/tests/integration/acceptance`|239 项通过，54.86 秒。
`apps/camctl/tests/integration/bootstrap`|930 项通过、14 项跳过；16 项分发用例在同一依赖安装 fixture 准备阶段报错，731.00 秒。
`apps/camctl/tests/integration/bootstrap/test_distribution.py`|设置测试进程 `UV_OFFLINE=1` 后完整复验，16 项通过、1 项跳过，25.73 秒；此跳过已包含于整目录的 14 项。
`apps/camctl/tests/integration/cancellation`|103 项通过，11.39 秒。
`apps/camctl/tests/integration/capture`|982 项通过，253.48 秒。
`apps/camctl/tests/integration/contracts`|438 项通过，5.33 秒。
`apps/camctl/tests/integration/devices`|UTF-8 环境复验 108 项通过、23 项跳过，2.58 秒。
`apps/camctl/tests/integration/history`|209 项通过，29.79 秒。
`apps/camctl/tests/integration/host_files`|82 项通过、5 项跳过，68.25 秒。
`apps/camctl/tests/integration/logging_runtime`|28 项通过，3.40 秒；保留 1 条既有 asyncio 标记警告。
`apps/camctl/tests/integration/motor`|3 项通过、9 项跳过，0.76 秒。
`apps/camctl/tests/integration/operations`|200 项通过，14.81 秒。
`apps/camctl/tests/integration/outputs`|1911 项通过、2 项跳过，242.18 秒。
`apps/camctl/tests/integration/persistence`|106 项通过，4.94 秒。
`apps/camctl/tests/integration/reporting`|373 项通过，45.01 秒。
`apps/camctl/tests/integration/scheduling`|133 项通过，10.00 秒。
`apps/camctl/tests/integration/session`|100 项通过，18.73 秒。

组件集成按不重复的用例分区累计为 5961 项通过、53 项跳过。Bootstrap 整目录的准备错误发生于 wheel 构建、锁文件导出和临时 Python 3.11 环境创建完成之后：`uv pip install -r` 获取 PyPI 的 `jsonschema-specifications` 元数据超时，16 项用例尚未开始。随后完整分发文件用已有缓存安装相同锁定依赖并通过。此结果证明该分区已经复验，不能表述为一次完整 bootstrap 全绿。

跳过项包括 Windows 上的真实符号链接、部署通知描述符继承、POSIX 文件与命令边界、Linux 进程组及信号、目录权限和目录同步、通知管道及 `proc` 描述符检查；文件取回的两项跳过属于不适用的输入分类。单元及日志目录的警告来自既有同步测试的 asyncio 标记，测试未因警告删除或放宽。

设备软件结果页使用 POSIX 远端路径；本机临时文件仍使用 `Path`。真实产物集成断言核对完整远端目录与原文件名，整目录执行覆盖 21 个相机产物参数场景。

[本机文件工具集成回归](../../tests/integration/test_camera_demo_transport.py)共 43 项通过，0.88 秒。测试只在成功的权威摘要脚本返回完整 binary stdin marker 时，将本机 Bash 的 ` *-` 转为目标 POSIX 的 `  -`；摘要、错误、退出、stderr 和其他工具响应保持。真实文件用 CRLF、NUL、非 ASCII、`0x1a`、`0xff`、空格及引号路径验证摘要、metadata、分页、偏移读取与原字节，并独立计算期望摘要。生产摘要解析仍严格拒绝错误长度、大小写、CRLF、缺少 LF 和附加内容。

四条根相机链首次运行均在上述响应格式处失败。适配后有三条完整通过，OSMO 延时摄影在等待中间报告时超时；单独复验仍在同一步失败。状态库已有符合等待条件的确定报告正文，但发布者撤下旧 READY 报告遇到 `WinError 32`。独立文件回归证明测试观察器默认读句柄可能阻止撤下，并证明适配后的观察器允许该操作；没有把观察器推定为此前唯一的锁持有者。生产发布失败和重试规则保持。

[报告观察器集成回归](../../tests/integration/test_ready_report_observer.py)最终 19 项通过，0.14 秒。Windows 测试读取只请求已有报告的只读句柄，允许其他进程删除或重命名；正常关闭不删除报告，观察器仍只检查 READY 的 UTF-8 正文与既有动作状态。真实文件回归证明已打开的读流在路径撤下后仍取得原正文，默认换行转换和 JSON／编码错误保持。`io.open` 统一负责文件描述符返回后的包装和关闭；真实包装失败回归保留原 `LookupError`，通过失效描述符与独占重新打开证明资源释放。独立审查核对了这些失败分区、原始 RED／GREEN 输出和三个共享观察入口。

最终源码的三个共享入口串行复验为 7 项通过、1 项跳过，111.30 秒：[双相机四条链](../../tests/integration/test_real_camera_demo_roundtrip.py)与[三条 C 模块链](../../tests/integration/test_camctl_c_module_roundtrip.py)全部通过；[安装版内建 Action6 链](../../tests/integration/test_action6_builtin_demo_roundtrip.py)按已有 Linux 平台条件在 Windows 跳过。实际组合当前 C host、Python CLI、SQLite、文件工具与客户端，验证整数秒受理、原 START／SENT 与基准恢复、长读取期间后续录像停止及中间报告、源与副本摘要和字节、领取、报告导入及累计 ACK。等待条件、时限与媒体门保持原值。源文件及既有无关文件保留；此结果只覆盖受接口约束的设备替身，不证明真实相机通信或本次跳过的安装版 ADB 进程链。

本次软件替身、浏览器和开发环境结果不证明相机固件能力、目标主机性能、ARM 运行差异或物理断电行为。OSMO 360 II 保持候选能力状态，不因整数秒定义或软件演示启用未登记的生产任务工厂。
## Action6 更新发行物核验（2026-10-11）

本次从 `af94f2b` 的业务源码构建更新包，在 Linux x86_64 容器、Python 3.11.16、SQLite 3.53.1、uv 0.12.10 上核验。交付包为工作区的 `dist/action6-record-update-20261011T023816Z/action6-record-update-20261011T023816Z.tar.gz`，包含 wheel、配套运行依赖、完整能力说明示例、能力 Schema、演示资源、中文更新说明及构建记录；运行版本仍为 `0.1.0`，现场更新使用 `--reinstall-package camctl`。

构建命令为 `uv build --project apps/camctl --wheel --out-dir <交付目录>`，依赖通过 `uv export --project apps/camctl --frozen --no-dev --no-emit-project -o <交付目录>/requirements.txt` 导出。随后在源码目录外创建独立环境，按哈希安装依赖，以 `--no-deps` 安装实际交付的 wheel。wheel 内的 201 个生产 Python 文件与源码逐字节一致，23 项包资源与构建登记的全部权威来源逐字节一致；环境只安装 camctl 及运行依赖，不携带测试依赖。

安装版 `describe` 使用占位 serial 配置导出完整能力说明，并通过包内能力 Schema 校验。导出范围为 `action6 / camera_record / action6_record`，估算声明为 130 Mbps、`direct`、参数路径 `/duration_s`；十秒参考大小为 162,500,000 字节，即 162.5 MB。安装版计划生成器使用该说明成功生成十秒录像计划，参数省略光圈和码率，估算资料不进入计划参数。现场使用实际配置重新导出说明后，才由客户端加载。

从仓库根执行 `UV_PROJECT_ENVIRONMENT=/workspaces/camctl/apps/camctl/.venv uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_action6_builtin_demo_roundtrip.py -q`，取得 `1 passed`，耗时 30.48 秒。该专项通过安装版内建驱动、真实 C host、CLI、状态库和客户端完成录像、独立取回、领取及 ACK；ADB 仍使用受真实接口约束的替身。日志位于同一交付工作目录的 `.work/c-host-software.log`。

交付的 14 个载荷 SHA-256 全部通过，压缩包中的所有文件与交付目录逐字节一致，更新说明的三段 Bash 命令通过语法检查。主执行者核对文档引用、修改标题锚点和 diff 后登记本阶段；未修改业务代码。本条结果证明更新发行物本身，后续 ARM 安装与真机演示的结论见[交付状态](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)。

## ARM 更新包接收与现场状态（2026-10-11）

用户从 ARM 主机回传 `2026-10-11 02:46:51 UTC` 的现场结果。更新压缩包的 SHA-256 及解压后全部 14 个载荷的 SHA-256 均通过，证明本次主机收到的文件与更新交付一致。`adb devices -l` 只输出表头，未列出设备；运行账户的进程查询未显示 host 或 camctl，唯一匹配项是查询命令自身的 `awk`。

该次目录检查中，`/home/cld/.camctl/camera-demo` 不存在；用户列出的 `/home/cld/.camctl` 内容为 `host`、`packages`、`venv`，其中 `host` 包含 `bin`、`include`、`lib`、`share`。

随后用户完成更新安装与资源提取，并回传 Python 3.11.17、SQLite 3.53.1。uv 检查九个运行依赖后，卸载原 `camctl 0.1.0` 并安装更新目录中的同版本 wheel；实际导入来源为 `/home/cld/.camctl/venv/lib/python3.11/site-packages/camctl/__init__.py`，演示配置提取到 `/home/cld/.camctl/camera-demo/config.toml`。本次 `adb devices -l` 列出 serial `123456789ABCDEF`，状态为 `device`，USB 为 `1-2.3`，product 为 `qcs8625_ac206`，transport_id 为 `3`。

用户随后回传实际配置：`devices.action6` 使用 `dji-action6`，ADB serial 为 `123456789ABCDEF`，省略 `[paths]` 而使用运行账户默认目录。`init` 成功；现场 `describe` 导出 `/home/cld/.camctl/camera-demo/capabilities.json`，通过安装版能力 Schema 校验，范围为 `action6 / camera_record / action6_record`。估算声明为 `bitrate_mbps=130`、`duration.method=direct`、`seconds.source=parameter`、`seconds.path=/duration_s`。

该阶段已取得 ARM 配置、初始化和现场能力导出的证据；后续现场进度见[客户端计划独立取回](#arm-客户端计划独立取回进度2026-10-11)，最终结论见[现场验收](#action6-普通录像演示的现场验收2026-10-11)。

## ARM 客户端计划独立取回进度（2026-10-11）

用户在上述 ARM 部署的接续过程中回传：已在客户端生成取回计划，并成功完成独立取回。本条记录保存用户回传的现场进度；原始计划、正式状态报告及交付文件未在仓库归档。

当前演示的最终结论见[现场验收](#action6-普通录像演示的现场验收2026-10-11)。

## Action6 普通录像演示的现场验收（2026-10-11）

环境沿用上述 ARM 主机、运行账户 `/home/cld`、Python 3.11.17、SQLite 3.53.1、已安装的 camctl wheel 和 C host，以及 serial 为 `123456789ABCDEF` 的 Action6。

验收执行者为用户。用户在实际演示后确认目前一切正常；当前 Action6 普通录像演示按用户现场确认验收通过，[T9 当前演示范围](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#现场验收状态与部署入口)完成。已明确回传的业务结果包括客户端生成取回计划及成功完成独立取回。该结论采用用户总体确认；原始报告、媒体文件及确认水位未在仓库归档，不补写媒体测量值、文件摘要或逐项检查结果。

本次范围为 Action6 普通录像演示。Action6 延时、OSMO 及未支持选项按各自设备条件后续安排，其他软件缺口、目标资源测量和物理断电验收继续由各自责任计划跟踪。130 Mbps 仍是录像大小估算的固定假定参考值。

## Hasselblad 主机单张接入的软件验证（2026-10-12）

环境为 Linux x86_64 开发容器、Python 3.11.16 和 SQLite 3.53.1。验证对象是[Hasselblad 单张接入](hasselblad-driver.md)：已有 `hb_cli` 经受管进程执行单张 RAW 拍摄，原图和 JPEG 预览保存在主机，并按稳定文件身份登记。每次 `obtain` 产生独立交付；领取并删除交付后，正式源文件仍保留。测试由子代理按[组件运行指南](../../apps/camctl/tests/AGENTS.md#运行边界与结果记录)执行，同工作区共享包资源的进程通过同一文件锁协调，保留命令、会话、日志和退出码。

[受管驱动组合测试](../../apps/camctl/tests/integration/devices/test_hasselblad_managed_capture.py)使用实际进程执行器和文件端口，以可执行工具替身覆盖成功、序列号不符、部分原图后非零退出、缺预览及长度错误。[主机单张流程测试](../../apps/camctl/tests/integration/capture/test_host_photo_flow.py)组合真实 SQLite、文件系统和驱动，验证原图归档后中断、绑定变化后的本地恢复，以及保存回执未知时使用原申请和键从独立只读连接核实。结果未知时保留工作文件，不重新拍摄。

[正式配对边界](../../apps/camctl/tests/integration/capture/test_host_photo_finish_pair_boundary.py)按动作已保存的执行规格识别主机照片，成功必须共同登记完整原图、预览及唯一关联。计划、事件守卫、同键重送和新键恢复共同拒绝缺失责任或不完整配对；失败和取消可保留可靠部分原图，合法旧照片规格继续使用设备源。[历史恢复测试](../../apps/camctl/tests/integration/capture/test_host_photo_complete_history_recovery.py)核对快照正向与当前投影逆向恢复；正式源显式清理后仍沿用原永久身份。数据库首次初始化使用格式 2，格式 1 保留旧业务并拒绝新的主机照片写入，不隐式升级。

[安装包组合测试](../../apps/camctl/tests/integration/bootstrap/test_distribution.py)在源码目录外构建并安装 wheel，使用安装资源完成 `init`、`describe`、计划生成、`submit` 和 `run`。完整链核对单张、原图与预览取回的成功报告、来源和交付的长度与 SHA-256；领取并删除两份交付后再次取回原图，生成新交付且工具只执行一次 `capture`。安装版专项 3 项通过。工具替身使用 29 字节 TIFF 和 25 字节 JPEG 签名样本，结果只证明软件协作，不证明实际媒体可解码。

旧设备照片继续使用空执行定义；没有任务工厂或工厂返回完全空的同类 `CaptureTask` 均可受理。不完整的主机任务和单张不适用的字段仍被拒绝。共享替身的[照片闭环](../../tests/integration/test_camctl_capture_roundtrip.py)、[自动预览取回](../../tests/integration/test_camctl_output_roundtrip.py)和[C 接入模块拍摄、取回与清理](../../tests/integration/test_camctl_c_module_roundtrip.py)三个相关用例分别通过，核对了真实 CLI、客户端和 C host 的对应消费路径；设备仍由替身提供。兼容修复后，安装包重新构建并完成上述安装版专项及旧照片流程的 4 项补验。

全量单元测试取得 `5076 passed, 1 skipped`。16 个集成目录分别启动 pytest，全部取得完整通过结果，合计 `6209 passed, 3 skipped`。其中 `capture` 为 1078 项，`outputs` 为 1944 项，`bootstrap` 为 993 项；兼容修复后另从当前源码重建安装包并完成 4 项补验。单元的两项及集成的一项既有 asyncio 标记告警保留，均为同步函数误加标记。跳过范围分别是非 Linux 守护、Windows 文件名大小写别名、主机候选读取配置及内部副本的普通交付发布。

独立评审已核对主机成功配对、原键与新键恢复、独立只读回执核实、精确 JSON 数字和旧照片任务兼容边界。数据库结构检查通过 31 张表、3196 项断言；事件登记为 35 类、113 个分支；历史对象 SQL、配置消费、报告依赖及公共协议检查均通过。文档文件链接、改动文档的标题链接及差异空白检查通过。

已有工具与真机的接口证据见[单张拍摄核验记录](../superpowers/notes/2026-10-12-hasselblad-cfv100c-probe.md)。ARM 独立部署及真实相机业务验收结果见下一节；软件替身结果与目标设备现场结果分别记录。

## Hasselblad ARM 真实拍摄与取回验收（2026-10-12 UTC）

目标环境为 ARM64 Ubuntu 18.04.6、CPython 3.11.17 和 SQLite 3.53.1，安装与数据使用独立虚拟环境和目录，没有复用既有业务状态库。用于正式拍摄的 PID 显式前缀发行包 `camctl-0.1.0-py3-none-any.whl` 为 993360 字节，SHA-256 为 `964baf983ce1167adf1de7a980dac6eeaa03af7e22f21aec085d8a99309fb733`；安装检查逐字节比对了 239 个包文件，权威资源共 26 项，9 个运行依赖按带哈希的清单安装或核对。初始化数据库格式为 `2`，`init` 与 `describe` 均退出 `0`。

首次 USB 预检没有枚举到目标相机；设备接入后，`doctor` 和 `info` 最初因 USB 权限不足失败。重新加载既有 udev 规则并触发设备节点后，两项命令均退出 `0`，观察到的设备身份与配置相符。此处只记录最终可访问状态，不把首次预检或权限错误当成当前设备状态。

2026-10-12 16:24–16:29 UTC，受管 `submit` 和 `run` 均退出 `0`。正式计划请求 `7087489617487330885` 创建拍摄动作 `1`；动作成功并登记原图和配对预览。唯一持久化 START 尝试结果记录 `list`、`info`、`drive_mode_single`、`capture_format_raw` 和 `capture` 五步，每步 `local_exit_code=0`，工具返回码为 `0`，其中恰有一条 `capture` 步骤。次数来自持久化 START 结果；独立进程采样文件没有事件，不据此声称独立观察到操作系统进程。完整过程命令、步骤事实和输出登记均保存在下述最终证据归档中。

| 正式源 | output 与文件身份 | 归档相对路径 | 大小及 SHA-256 |
| --- | --- | --- | --- |
| 原图，`image/x-hasselblad-3fr` | output `1`，file `1` | `staging/capture-originals/1.3fr` | `211177472` 字节；`25197dd1cd197ef3cb51e56d08150c93998d6842eed7d5bdc0600a8ac2d4bcdf` |
| 预览，`image/jpeg`，关联 `preview_of_output_id=1` | output `2`，file `2` | `staging/capture-previews/2.jpg` | `646812` 字节；`900436f043360d6ecc6c62c51a4384eb48e26021b6b8fa669703c2aea47afd61` |

首次取回分别成功发布原图与预览，delivery 为 `1`、`2`，目标文件身份为 `3`、`4`；来源与交付的大小及 SHA-256 相同。验收脚本随后模拟将这两份 delivery 从 `ready` 移到 `processing` 并删除；这模拟领取和删除，不是第三方传输程序的真实领取。正式源仍保留在主机。

之后使用完全省略 `[devices]` 的离线配置运行 `describe`，设备列表为空；此配置仍指向同一状态库和文件目录。以原图请求 `4935035650787902070` 和预览请求 `1358301172749695578` 再次取回，动作 `4`、`5` 成功，新建 delivery `3`、`4` 和目标文件身份 `5`、`6`。新交付仍与正式源大小和摘要一致；两份源文件的路径、inode（279451、279452）、大小和 SHA-256 未变。数据库最终保有五条尝试：一条 START 和四条本地读取；再次取回没有调用相机。该离线配置没有设备声明，但没有物理拔线。

2026-10-12 17:14 UTC，拍摄后沿原动作通过 `CaptureRepository.conclude_activity` 的正式事务保存活动结束和占用释放，不调用相机停止命令、不重新拍摄，也不直接改写数据库。首次收场回执为 `COMPLETED/CONCLUDED`；原活动从 `ACTIVE/HELD` 转为 `ENDED/RELEASED`。随后使用同一 intent 和 operation key 再调用，回执仍为 `COMPLETED/CONCLUDED`，确认重送核实；拍摄动作的业务字段、两份正式 output 与 intermediate file 事实、唯一 START 尝试均保持不变。17:15 UTC，正常 `camctl run` 使用空设备列表的离线配置退出 `0`，生成最终报告 `status-report-12-1f03519829fd70a08408a07842cbaf67f3bfbe7939068ffe905230590f5542ac.json`。最终报告将动作 `1` 标记为 `succeeded`，包含原图与预览及正确关联，不含 `device_execution`；报告 Schema 和文件名 SHA-256 校验均退出 `0`。`ready` 中保留最新的原图交付 `3.3fr`、预览交付 `4.jpg` 和最终报告。

实际拍摄使用前述 PID 显式前缀 wheel `964baf983ce1167adf1de7a980dac6eeaa03af7e22f21aec085d8a99309fb733`。17:12 UTC 才在同一独立虚拟环境安装最终 wheel `camctl-0.1.0-py3-none-any.whl`，大小 993512 字节、SHA-256 `306117d8bffaa199a66f8177492330302857b09a1dbb5ff06c5f6f8c96e44961`；最终包的 239 个已安装文件也逐字节匹配发行物。此最终版本用于既有动作的收场回执核实及离线 `run` 生成报告，没有再次拍摄。

最终完整命令、活动收场记录、报告验证结果和只读数据库快照保存在 `.local/hasselblad-arm-acceptance/20261013-final/final-evidence.tar.gz`，大小为 `132502` 字节，SHA-256 为 `923eeb25bedfe6ad20d8cd01228d4b2d114bcdd0c8057e0c755d08106b339102`。清单含 148 项，均与解包后的证据目录大小和 SHA-256 相符。原图没有纳入本地证据包且未解码；预览副本 `.local/hasselblad-arm-acceptance/20261013-camera/capture-preview.jpg` 的摘要匹配并可正常呈现，但这不评价图像质量。该次验收没有检查相机端原片，也没有执行物理断电或真实第三方领取 delivery；当前固件的双落盘及下载契约见[驱动设计](hasselblad-driver.md#正式源的部署选择)。

此前全量软件回归仍为单元测试 `5076 passed, 1 skipped`、16 个集成目录合计 `6209 passed, 3 skipped`；最终收场修复另通过完整 `capture` 目录 `1088 passed`、主机照片流与优化模式专项 `19 passed`、收场恢复相关用例 `93 passed` 及最终 wheel 安装版拍摄、双文件取回和报告专项 `1 passed`。这些测试结果与本节目标主机的实际拍摄证据分别记录；本次文档更新未重跑测试。

## Hasselblad 双正式源的软件验证（2026-10-14）

环境为 Linux x86_64 开发容器、CPython 3.11.16 和 SQLite 3.53.1。验证对象是[相机正式源实施计划](../superpowers/plans/2026-10-14-hasselblad-camera-source.md)及[双模式驱动契约](hasselblad-driver.md#正式源的部署选择)。部署配置决定新受理动作采用主机或相机正式源，计划参数保持原结构。组件测试组合真实 SQLite、文件系统、受管进程和受工具契约约束的相机替身；安装版测试在源码目录之外构建、安装并使用正式 CLI。

[双模式拍摄测试](../../apps/camctl/tests/integration/capture/test_device_photo_flow.py)核对两种部署变更方向下的原动作定义、设备原图和配对预览、固定大小与摘要，以及产物登记前后主机副本的实际保留和回收。可靠成功结果保存后移除相机配置，仍可继续登记、报告及临时副本回收。归档、CHECKSUM 或完成事务回执未知时沿原请求和键恢复，只有一次 capture；未知拍摄保留工作文件。取消期间取得可靠双文件仍保存已有产物。持久化入口和直接事件共同拒绝空产物、单边产物、错误角色或配对，终态重送允许临时副本已经回收。相机源显式清理保存逐项错误，不形成删除尝试或相机缺席，主机源继续本地清理。

[受管下载测试](../../apps/camctl/tests/integration/devices/test_hasselblad_download.py)和[准备失败分类测试](../../apps/camctl/tests/integration/outputs/test_read_preparation_settlement.py)核对原图名的两种下载、绑定身份、非零偏移、完整大小及捕获摘要。下载准备、进程收场和文件句柄责任分别保存，未确认收场不终结 READ 或释放占用。[遗留下载维护](../../apps/camctl/tests/integration/bootstrap/test_download_cleanup.py)只处理已知 READ，并按独立额度和游标续查；[两次默认运行专项](../../apps/camctl/tests/integration/bootstrap/test_hasselblad_read_waiting_session.py)验证本地前提等待使第一次 run 有限失败退出，下一次实际 run 经维护后沿原最后票据完成，不消耗新的源失败预算。

[相机源安装版专项](../../apps/camctl/tests/integration/bootstrap/test_hasselblad_device_distribution.py)从实际 wheel 运行拍摄、临时副本回收及原图和预览各两轮独立取回，保存四份独立交付并核对捕获时的固定大小、SHA-256、配对和最终报告。后续部署改为 host 仍从原设备源下载；等长错误内容不能通过原捕获摘要核验，也不发布错误交付。既有[主机源安装版专项](../../apps/camctl/tests/integration/bootstrap/test_distribution.py)继续验证离线重复取回及显式清理。安装版三个专项均通过，结果属于下述组件回归的相应范围，不重复计入总数。

验证范围|实际结果
-|-
全量单元测试|`5199 passed, 1 skipped`；两条既有同步测试 asyncio 标记警告保留。
组件集成回归|16 个目录分别启动 pytest，合计 `6518 passed, 3 skipped`；`bootstrap` 为 1101 项，`capture` 为 1120 项，`outputs` 为 1970 项。日志目录保留一条既有同步测试 asyncio 标记警告。
安装版专项|`3 passed, 17 deselected`；从当前源码构建安装，覆盖主机和相机正式源的正式入口。
静态契约检查|数据库 31 表、3196 项断言；事件 35 类、113 分支；历史对象 SQL、配置消费、报告依赖及公共协议检查均通过。

单元测试使用 `PYTHONDONTWRITEBYTECODE=1 apps/camctl/.venv311/bin/python -m pytest apps/camctl/tests/unit -q`。集成回归遵守[组件运行边界](../../apps/camctl/tests/AGENTS.md#运行边界与结果记录)，同工作区一次只运行一个集成 pytest，按各目录分别启动进程。本地证据目录为 `.local/hasselblad-camera-source/20261014-software/`；`validation-20261014T120352Z.json` 和 `validation-20261014T121125Z.json` 分别保存单元、全部集成目录的完整命令、工作目录、解释器、UTC 时间、PID、原始日志路径及退出码，`validation-summary.json` 从实际日志汇总各目录结果。静态检查的完整命令和输出分别保存在 `static-checks.json` 与 `static-final-checks.json`。各项通过声明均已核对实际日志和退出码。

独立评审核对了部署固化、两种源模式、可靠摘要与配对、读取责任及遗留恢复，没有未解决的代码问题。文档本地文件链接与标题引用检查通过；同步测试的三条 asyncio 标记警告均来自未改动的既有测试，不影响本轮测试结果。

本次没有执行 ARM 新固件的真实相机拍摄和下载、目标存储空间测量或物理断电验收。此前主机正式源的现场证据保持[原验收范围](#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)；新增相机源模式仍须在目标固件上核验两种下载、交付内容及主机副本回收。

## 普通录像截止唤醒的软件验证（2026-10-19）

开发容器使用 Python 3.11.16、SQLite 3.53.1。修复覆盖普通录像原截止提示、背景任务完成通知、正式会话的轮询及提前检查配置、默认进程内通知器，以及通知与外部取消竞争时的取消传播。录像的原启动锚点、1000 毫秒冻结余量、STOP 预算及持久化结构保持原规则。

验证范围|实际结果
-|-
全量单元测试|5958 项通过、1 项跳过；保留两条既有同步测试 asyncio 标记警告。
bootstrap 组合专项|47 项通过，覆盖生产配置装配、30 秒轮询下的短录像截止、跨设备推进、原结果接手及清理通知。
capture 专项|82 项通过，覆盖发送确认、取消、保存延迟及失败、恢复对账、停止确认和文件等待。
session 全目录|194 项通过。
cancellation 全目录|103 项通过。
history 停止回放专项|3 项通过。

同工作区按上述范围串行启动独立 pytest，完整命令、工作目录、耗时、退出码、日志和 JUnit 位于 `.local/verification/record-duration-deadline-20261019/`，由 `run-gates.py` 和 `verified-gates.json` 对应。新增回归先复现了遗漏截止、配置未装配、保存后未发布提示，以及有限等待吞外部取消的问题。取消竞争的无期限等待原本正常，有期限等待修复后也传播 `CancelledError`。开发过程中的 capture 整目录运行在 262 项通过后主动中断，不作为完整目录通过记录；诊断取消问题时终止的 bootstrap 运行也不计入上述最终结果。

### 开发容器的独立安装包与真实原片

源码提交 `ff46061` 构建 wheel，在独立 Python 3.11 环境安装，核对六个相关生产模块与源码字节一致。wheel 的 SHA-256 为 `d376add4dfd5f79c15298b09a678eddd1214863f6b611a60558629dd52e7a0ce`；安装目录、模块摘要及解释器记录见 `.local/deployments/20261019-record-duration-deadline/manifest.json`。

通过[宿主机 ADB 入口](../hardware/camera-demo-validation.md#开发容器访问宿主机-adb-服务)串行执行两个目标 10 秒、启动余量 1 秒的正式计划。自动曝光原片为 11.277933 秒、双路各 338 帧；手动曝光原片为 11.544867 秒、双路各 346 帧。两份均为 3840×3840 双路 HEVC，帧率约 29.97 fps，源端与取回摘要一致。STOP 意图分别在可靠 START 返回后 11.075766 秒和 11.079842 秒保存；每轮 START、STOP 各一次，两次 CLI 返回 0、动作成功，最终设备状态回调为空闲。手动轮的元数据和可选预览目录查询各遇到一次超时，有限核实最终登记原 OSV，未登记 LRF；不将其表述为全部查询成功。

本地证据位于 `.local/diagnostics/20261019-record-duration-remote-adb/`，其中 `installed-auto-072337/`、`installed-manual-072728/` 保存两轮计划、配置、状态库、原片、摘要、媒体探测、相机日志及 strace；`acceptance-summary.json` 汇总修复前三轮及修复后两轮样本。此证据来自开发容器和真实相机，不代表已部署 TX2。原片均录满目标，但尚未达到此前希望的 10～11 秒范围；原来约 15 秒的条件未复现。后续验收对象由[实施计划](../superpowers/plans/2026-10-19-osmo360ii-record-duration.md#当前进度)跟踪，通俗说明见[录像时长报告](../hardware/osmo360ii-record-duration-report.md)。

后续只读复核核对了五轮界面录像计数，以及两份安装包原片的可见时间标签、轨道时间和首个视频包。界面计数与媒体时长不同；首包与视频轨道均为零基相对时间，创建标签和时间码缺少实际首帧与主机 UTC 的映射。相机私有轨未解码，未据此生成新计时依据。原始输出和条件推算的位置见[日志与媒体时间复核](../hardware/osmo360ii-record-duration-report.md#现有日志和媒体时间标签的只读复核)；本项没有新增拍摄、测试运行或驱动改动。

### OSMO 普通录像的零余量独立对照（2026-10-19 UTC）

开发容器通过同一宿主机 ADB 服务访问真实相机，使用现有官方命令映射、受管工具和生产响应解释器。独立诊断在完整可靠 START 返回后仅等待目标 10 秒再 STOP，不创建正式动作；本项不作为修改后的 CLI 或安装包验收。

自动曝光的 START 耗时 4.082253 秒，完整返回后 10.007094 秒派发 STOP，STOP 耗时 2.225110 秒。可靠停止及 30 秒文件等待后观察到空闲，新增 OSV 取回并核对源端与本地摘要一致；双路 3840×3840 HEVC 均为 13.713700 秒、各 411 帧。该样本录满目标，但未达到 10～11 秒。它与此前安装包采样属于不同相机启动批次，不能直接比较以推定余量的因果效果。

手动曝光 START 于 11:54:53.701591 UTC 可靠返回；首次 STOP 于 11:55:03.711083 UTC 派发，20 秒无输出后超时，效果未知。当时停止后续硬件操作，已保存日志最后观察到 `recording:1`。用户随后确认已持续录像 8 分 35 秒。以现场确认作为新的停止依据，于 12:04:01.469339 UTC 再发一次 STOP，12:04:03.192200 UTC 收到完整正常 `00`，本地退出 0，生产解释器确认停止；之后连续五条状态回调为空闲，boot ID 未变，进程快照未见残留 `dji_mb_ctrl`。没有再次 START。

按原 30 秒文件等待核对长录像源：新增 OSV 为 9281473728 字节，LRF 为 988994565 字节，两者均保留。没有取回整段文件、探测实际媒体时长或计算完整文件摘要；该轮不作为 10 秒时长对照。本项没有修改正式启动余量或构建新安装包，也没有新增软件门禁通过记录。后续 STOP 成功不改写首次 STOP 的未知结果。

证据位于 `.local/diagnostics/20261019-record-duration-zero-margin/`，逐轮响应、时钟、摘要、原片及失败记录由 `experiment-summary.json` 关联；`stop-after-user-confirmation-120401/` 保存现场确认后的 STOP 结果、空闲回调与长录像文件元数据。解释及接续边界见[零余量对照](../hardware/osmo360ii-record-duration-report.md#零启动余量的独立对照)。

## 工具启动期限的软件核验（2026-10-19）

代码审查发现受管工具在取得子进程句柄后才开始超时计时，未将启动交接耗时计入原调用预算。失败回归分别复现了“交接已超过期限，正常退出被记为成功”和“启动耗时后重新获得完整等待预算”。修复从启动前冻结原单调钟截止；取得句柄后只使用剩余预算，已过期时保留超时及真实退出、输出，沿原调用可靠收场。已退出不发信号或补造宽限，取消继续交接原结果。完整状态分类见[启动耗时的超时边界](../superpowers/plans/2026-10-19-osmo360ii-record-duration.md#工具启动耗时的超时边界)。

Linux x86_64 开发容器使用 CPython 3.11.16、SQLite 3.53.1，最终结果如下。组件集成目录分别启动进程，同一工作区串行执行。

| 范围 | 实际结果 |
| --- | --- |
| 全部单元测试 | 5962 项通过、1 项跳过，保留两项既有 asyncio 标记警告。 |
| operations 整目录 | 202 项通过，包含延迟交接真实子进程、正常退出及双通道排空。 |
| devices 整目录 | 206 项通过，包含真实 OSMO 驱动对迟到正常 `00` 的处理：保存超时、实际退出与响应数据，保持原未知判定。 |
| capture 开始运行时、结束及开始结果保存边界 | 52 项通过。 |
| cancellation 整目录 | 103 项通过。 |
| bootstrap 停止、唤醒配置、调用后取消和会话状态 | 20 项通过。 |
| session 整目录 | 194 项通过。 |

集成测试合计 777 项通过。devices 首轮有 5 项既有夹具失败：结果分页测试仍提供缺少必填码率、包含已移除 EV 字段的参数，受理失败后没有活动身份，尚未进入受管工具调用。夹具已改为当前合法参数，并按原冻结定义传入 SENT；修正后上述整目录通过。该过程没有放宽生产参数校验。

完整命令、环境、退出码、日志及 JUnit 位于 `.local/diagnostics/20261019-record-duration-tx2-local-adb/software-fixed/` 和 `software-fixed-recheck/`；`software/` 保存修复前的 234 项定向通过及新缺陷的失败回归。新软件缺陷与原 STOP 无输出超时的因果尚未证实，实机分层对照和正式计划核验分别由[本机 ADB 对照计划](../superpowers/plans/2026-10-19-osmo360ii-record-duration.md#tx2-本机-adb-的代码分层对照)跟踪。

## OSMO 普通录像的 TX2 本机 ADB 分层核验（2026-10-19 UTC）

目标为 TX2 ARM64、Ubuntu 18.04、CPython 3.11.17、SQLite 3.53.1，使用本机 `/usr/bin/adb` 1.0.39，不配置远程 ADB 服务。相机 serial 为 `123456789ABCDEF`。安装限于对应 USB 标识的 plugdev 权限规则后，重启本机 ADB 服务以刷新权限；未重启相机。相机 boot ID 与此前诊断一致。过程使用独立诊断状态库，不初始化部署中的原状态库。

先从未修改工具期限的基线包完成两层手动曝光诊断：标准库直接命令取得双路 11.678333 秒、各 350 帧；真实驱动及受管执行取得双路 10.610600 秒、各 318 帧。两层都在可靠 START 返回后等待 10 秒，不加额外余量，分别只发送一次 START 和 STOP；完整响应正常、停止后为空闲，源端与副本摘要一致。该对照不创建正式动作，不能与正式计划的 1 秒冻结余量混合比较。

工具期限修复的 wheel 来自 `e5229ba`，SHA-256 为 `186c2536fadc90a5012062dc88923b9f56b93d2c3078633ab64e1ef002603b72`。先安装到独立包目录，使用目标主机现有 Python 和运行依赖，通过 `PYTHONPATH` 选择该包；240 份 Python 模块与源码逐字节一致。正式 CLI 使用独立状态库、默认 500 毫秒轮询、START／STOP 各 10 秒调用期限，并将两种控制尝试上限各设为 1，避免后续尝试掩盖首次失败。目标时长冻结为 10000 毫秒、启动余量冻结为 1000 毫秒。

| 正式计划 | 执行与登记 | 原片核验 | STOP 时序 |
| --- | --- | --- | --- |
| 手动曝光 | CLI 返回 0，动作成功，登记新增 OSV 与配对 LRF；START／STOP 各一次。 | 双路 HEVC 3840×3840、30000/1001 fps，各 11.778433 秒、353 帧；201373578 字节，源端及副本 SHA-256 为 `9d3d23a96fce0cee7378bfa607f54dd179e85f7c5deeb806e07970a0f6e345a1`。 | 可靠 START 返回后 11.043087 秒保存意图，随后 39.758 毫秒实际启动 ADB。 |
| 自动曝光 | CLI 返回 0，动作成功，登记新增 OSV 与配对 LRF；START／STOP 各一次。 | 同规格双路视频，各 11.211200 秒、336 帧；192458388 字节，源端及副本 SHA-256 为 `def530ea1144b2df1c4f1b6791814609a6c818484ff989c02003deb26cc273a4`。 | 可靠 START 返回后 11.032384 秒保存意图，随后 35.323 毫秒实际启动 ADB。 |

两轮结束后均核实为空闲。正式计划的停止已确认后，分页结果核实分别持续约 172 秒和 187 秒；此时间不计入录像。手动轮采集辅助脚本在原片已取回后因遗漏导入退出，已续接同一原片完成摘要、媒体及空闲核实，没有重拍或重新取回；正式 CLI 自身的退出码为 0。两份原片均至少录满目标，仍超过希望的 10～11 秒范围。

部署前发现 TX2 原安装的四个调度模块与当前源码不一致，未包含截止唤醒修复；控制与响应模块一致。完成上述独立验证后，已备份原安装包并在现有虚拟环境更新同一 wheel。266 份包内文件与发行物逐字节一致，运行依赖检查及沿原配置的 `describe` 均返回 0。安装过程保留原配置和状态库，没有运行原计划或再次拍摄；正式计划验证使用前述独立包目录，不写成全局安装后的再次实拍。

本地证据位于 `.local/diagnostics/20261019-record-duration-tx2-local-adb/`。`tx2-evidence/` 保存四轮原片、控制返回、状态库、日志、媒体探测和正式计划 strace；`acceptance-summary.json` 从原始 trace 和状态库重建时序。两轮分层诊断仅回收控制子进程的 trace，大型父进程及取回 trace 保留在 TX2。目标目录为 `/home/cld/.camctl/diagnostics/20261019-record-duration-local-adb/`，`global-install-125252/` 保存原包备份、安装命令、依赖检查及逐文件清单。

完整控制、原片与时序解释见[分层核验报告](../hardware/osmo360ii-record-duration-report.md#相机接回-tx2-后的分层核验)。各层均保留相机源文件，旧的约 9 GB 长录像没有重新取回。新软件缺陷已由失败回归证明；本机成功样本不证明此前停止超时来自远程 ADB，也不证明偶发问题已经消失。

## OSMO 普通录像零启动余量的软件验证（2026-10-19 UTC）

按[启动余量规格](../superpowers/specs/2026-10-18-camera-startup-margin-design.md)，OSMO 新普通录像采用 `SENT + startup_margin_ms=0`；Action6 普通录像及 OSMO 原生延时仍采用各自的 1000 毫秒声明。普通录像定义允许显式零值，确认语义与余量必须共同保存，已有动作读取原冻结值；目标时长、停止后文件等待和未知结果规则继续沿用原契约。能力说明从实际任务声明取得等待规则。

开发容器使用 CPython 3.11.16、SQLite 3.53.1。失败回归先复现两项零值定义拒绝、一项 OSMO 工厂仍冻结 1000，以及真实拍摄运行时仍将截止推迟到第 11 秒；Action6 对照在原实现上通过。修改后最终门禁如下，各集成范围分别启动进程并串行运行。

| 范围 | 实际结果 |
| --- | --- |
| 全部单元测试 | 5963 项通过、1 项跳过；保留两项既有 asyncio 标记警告。 |
| devices 整目录 | 206 项通过。 |
| acceptance 的 ADB 相机定义 | 14 项通过。 |
| capture 的录像开始运行时、开始保存边界、结束、恢复对账、恢复诊断、原生完成上下文及自然等待 | 135 项通过。 |
| bootstrap 的录像停止、唤醒配置和恢复输入 | 10 项通过。 |
| OSMO 正式内置合同安装版联合链 | 4 项通过。普通录像恢复在第 9 秒不发 STOP，第 10 秒按原定义发唯一 STOP；原生延时仍在原 331 秒条件下核实，STOP 后文件等待保留真实 30 秒。 |

最终相关集成门禁共 369 项通过。零余量组合核对真实内置任务、定义构建、SQLite、历史守卫、截止通知及实际停止派发；参数化回归覆盖零／非零余量的未知 START、取消、保存延迟和重启剩余等待，保持 `started_at` 未知及原 START 唯一。安装链使用真实客户端、C host、wheel、CLI、状态库、取回和报告 ACK，设备端仅在 ADB 工具边界提供受约束替身，不提供真实媒体时长证据。

原 capture 全目录运行在 391 项通过时主动中断，退出 2；该轮不作为全目录通过或最终门禁计数。随后按本次受影响的拍摄边界完成上述定向范围。全部命令、工作目录、环境、退出码、日志及 JUnit 保存在 `.local/diagnostics/20261019-osmo-record-zero-margin/software/`，包括失败回归和中断记录。实拍与部署另按[当前实施计划](../superpowers/plans/2026-10-19-osmo360ii-record-duration.md#osmo-普通录像的零余量对照)核验；Action6 本次只运行软件对照。

## OSMO 普通录像零启动余量的 TX2 验收与部署（2026-10-19 UTC）

从 `9742950` 构建 wheel，SHA-256 为 `27d3c1d476cc55e9162bf92b3b804fd7d60cbc1a154969dec5a8ff868ac9c10a`。本地与 TX2 的独立安装包目录均核对 240 份 Python 模块；目标继续使用 CPython 3.11.17、SQLite 3.53.1 和本机 ADB 1.0.39。两种曝光按顺序通过正式 CLI 执行，分别使用新计划及独立状态库，START／STOP 尝试上限各为 1，结果查询期限为每页 60 秒。目标冻结为 10000 毫秒、启动余量明确冻结为 0，`start_return_meaning=1`；停止后文件等待仍为 30000 毫秒。

| 正式计划 | 控制与登记 | 原片测量 | 源端与副本 SHA-256 |
| --- | --- | --- | --- |
| 手动曝光 | 可靠 START 返回后 10.033056 秒保存 STOP 意图，随后 41.455 毫秒启动 ADB；START／STOP 各一次，正常响应，CLI 返回 0，登记 OSV 与配对 LRF。 | 双路 HEVC 3840×3840、30000/1001 fps，各 10.110100 秒、303 帧；OSV 为 173898325 字节。 | `dbefd83b56da0c05317b87f17005ad115f1ecccb14bb6854f4c1b2bcd06ff36b` |
| 自动曝光 | 可靠 START 返回后 10.049380 秒保存 STOP 意图，随后 38.574 毫秒启动 ADB；START／STOP 各一次，正常响应，CLI 返回 0，登记 OSV 与配对 LRF。 | 同规格双路视频，各 10.043367 秒、301 帧；OSV 为 172867919 字节。 | `f88771e1f4e93df8f02fcc5618e927c744a0b746622d61f39afc6e05f234bcc9` |

两轮分别在可靠停止、文件与空闲核实后结束，相机 boot ID 前后一致；没有重试拍摄或删除源文件。以上两份原片均至少 10 秒且落在 10～11 秒范围内，属于本次样本的实测结果，不由上一批视频减去 1 秒计算。实际首帧仍未知，旧 STOP 无输出事件不因新样本成功而改写。

随后备份并更新 TX2 原虚拟环境中的正式 camctl，266 份包内文件与同一 wheel 逐字节一致，运行依赖检查及沿原配置的 `describe` 返回 0。全局安装后的只读检查确认 OSMO 普通录像余量 0、Action6 普通录像及 OSMO 原生延时各 1000 毫秒，并取得连续五条空闲状态。原配置及状态库保留，未执行原计划；两次实拍使用前述独立包目录，全局更新后没有再次拍摄。

本地证据位于 `.local/diagnostics/20261019-osmo-record-zero-margin/`。`tx2-evidence/cli-manual-134518/` 与 `cli-auto-134912/` 保存原片、SQLite、计划、控制输出、日志和 strace；`acceptance-summary.json` 从原始证据提取时序，`local-copy-verification.json` 核对取回到开发容器的原片。TX2 的同名诊断根目录中，`global-install-135317/` 保存旧包备份及安装记录。

新的实际能力 JSON 已取回 `.local/deployments/20261019-osmo-record-zero-margin/device-capabilities.json`，包含原配置中的 `hb0` 与 `osmo360ii`，公共 Schema 校验通过，SHA-256 为 `a8ead569e366b83747132b2b31e7d0c5ae8179c34f72585756ac2e65ad140eb1`。普通录像的说明按实际零余量任务生成；客户端程序本身没有修改。Action6 本次仅有[软件对照](#osmo-普通录像零启动余量的软件验证2026-10-19-utc)，没有连接其实际设备。

## OSMO 原生延时摄影的时长核验（2026-10-19 UTC）

在 TX2 ARM64、Python 3.11.17、SQLite 3.53.1 上，使用现有正式安装版 camctl 和 TX2 本机 ADB 执行一轮独立状态库的正式延时计划。拍摄、计时和完成核实路径的八份已安装模块与源码 `a56f2af` 逐字节一致；相机仍为 serial `123456789ABCDEF` 的 OSMO 360 II。本轮参数为 Manual、标准码率、2 秒间隔、300 秒采集，驱动固定的曝光及输出规则保持。

实际 `6c` 负载为 `04070014002c01000000000000000001`，间隔窗口为 20 个十分之一秒，采集时长窗口为 300 秒。唯一 START 正常返回，未发送 STOP；从可靠 SENT 到自然核实时点之间没有 ADB 调用。冻结定义保留 1000 毫秒启动余量和 30000 毫秒自然结果余量，预计核实时点严格为原 SENT 后 331 秒，首次 RESULTS 意图实际在原 SENT 后 **331.137166 秒**保存。CLI 返回 0，拍摄成功并登记 OSV 及配对 LRF。

新原片 `CAM_20000101022634_0063_D.OSV` 为 196642046 字节。TX2 的 ffprobe 读得两路 `3840×3840` HEVC 主视频，各 151 帧、`30000/1001 fps`、**5.038367 秒**；约 5 秒成片与 2 秒间隔、300 秒采集目标相符，也与[此前 Auto 同组合的媒体结果](#五轮实机结果)一致。相机源文件、TX2 副本和开发容器副本的 SHA-256 均为 `39e1ee9d7049a44760c21f777cce2774def4a148c48019c44c75ce9330572f07`；结束后读得空闲状态，相机 boot ID 前后相同。此次媒体检查读取流信息，没有执行完整解码，也没有独立测得首帧或精确采集结束时刻。

原生延时由设备执行下发的有限目标并自行结束。启动余量只参与主机的自然结果核实时点，不改写设备采集时长，也不安排正常 STOP。本样本没有出现普通录像的同类时长偏长问题；此次冻结的 1 秒及第 331 秒核实事实保留。新任务的等待规则遵守[启动余量设计](../superpowers/specs/2026-10-18-camera-startup-margin-design.md#正常录像与自然延时)。本项没有修改驱动或重新部署，也没有访问 Action6。

软件侧使用 Python 3.11.16 和 SQLite 3.53.1 串行运行 `unit/capture/test_timelapse.py`、`unit/devices/test_adb_camera_commands.py`，158 项通过，退出 0。命令、解释器、UTC、JUnit 和日志保存于 `.local/diagnostics/20261019-osmo-timelapse-duration/`；`acceptance-summary.json` 从原始命令和状态库提取时序，`tx2-evidence/native-manual-141242/` 保存本轮计划、状态库、strace、原片及媒体信息。远端归档 SHA-256 为 `a7fdc8c4bed35dcf5ae20a619184b65b6ecce7076985d1ded6ecd0b02427cf97`，取回后 857 份文件逐一核对长度和摘要通过；原片保留在相机中。

## OSMO 原生延时零启动余量的软件验证（2026-10-19 UTC）

按[启动余量规格](../superpowers/specs/2026-10-18-camera-startup-margin-design.md)，新 OSMO 原生延时冻结 `startup_margin_ms=0`。驱动声明、执行定义及自然完成上下文接受显式零值，仍拒绝布尔值、负数、非整数和越界值；正常等待、有限取消及历史恢复使用原冻结值。设备采集目标、独立 30 秒自然结果余量和 STOP 后文件等待保持原规则，能力说明同步采用当前声明。

在 Linux x86_64、Python 3.11.16、SQLite 3.53.1 下串行验证，结果如下：

| 范围 | 实际结果 |
| --- | --- |
| unit 完整目录 | 5970 passed、1 skipped、2 条既有 asyncio 标记警告。 |
| capture 六份相关文件 | 首次 353 passed、8 failed；失败均为同一轮询测试替身缺少 `flows`。该测试与会话实现的首轮字节均与原基线相同。补齐替身的会话字段后，原 8 个参数分区复验全部通过；其余已通过分区没有改动。 |
| devices 完整目录 | 206 passed。 |
| ADB 参数受理文件 | 14 passed。 |
| bootstrap 发行物文件 | 38 passed。 |
| OSMO 正式内置合同安装版联合链 | 4 passed；原生延时恢复在第 329 秒不核实文件，第 330 秒进入结果读取，目标负载保持 300 秒、唯一 START、正常结束零 STOP。普通录像仍在第 10 秒发送唯一 STOP，STOP 后真实等待 30 秒。 |

新增单元回归在实现前有三项预期失败，分别指向执行定义拒绝零值、自然完成上下文拒绝零值和驱动仍声明 1000 毫秒；新增正常完成与有限取消集成分区也分别重现零值拒绝。修改后，正常上下文、结果页、历史回放、有限取消及等待保存未知等路径均覆盖显式零值，同时保留原 1000 毫秒冻结定义的验证。

命令、工作目录、解释器、UTC、退出码、JUnit 和日志保存在 `.local/diagnostics/20261019-osmo-timelapse-zero-margin/software/`。`fixture-failure-origin.json` 保存既有替身失败的输入摘要，`verified-results.json` 记录各次实际结果和补验覆盖，不将分批核验写成一次全目录通过。本项没有连接真实相机；前一轮 300 秒实拍仍按其原 1000 毫秒定义记载，不改写为零余量设备验收。

## OSMO 原生延时零启动余量的 TX2 部署（2026-10-19 UTC）

从干净提交 `207efb78154bca3f5ff7b71d15749d5c78db940f` 在仓库外快照离线构建 wheel，240 份 Python 模块与该提交逐字节一致。wheel SHA-256 为 `25bd19567a397678690b3e13c2bf992e78d2459208589a32a64013b79ca84a48`，大小 1147240 字节。软件门禁见[前节](#osmo-原生延时零启动余量的软件验证2026-10-19-utc)；串行只读复核没有发现生产代码阻断，旧计划的当前值与历史场景说明已同步到权威规格。

安装前确认没有 camctl 执行进程，备份旧包、原配置及状态库。在 TX2 原 Python 3.11.17／SQLite 3.53.1 虚拟环境离线替换应用包后，266 份包内文件与 wheel 一致，依赖检查及沿原配置的 `describe` 均退出 0。实际安装版的任务工厂生成 OSMO 普通录像和原生延时的零启动余量，Action6 普通录像仍为 1000 毫秒；原生延时的自然结果余量及独立 STOP 后文件等待仍各为 30000 毫秒。

原配置内容和默认软链接保持，状态库安装前后的逻辑摘要均为 `21b35a07538a6e8709b90243d8c87e1758631e7732a14f484b5fa4c438869d74`。没有执行 `init` 或推进原计划；已有未完成动作仍保存各自原冻结规则，新受理任务使用当前工厂声明。本项只执行安装与能力核验，没有发送相机命令或新增实拍。

本地发行物与能力 JSON 保存于 `.local/deployments/20261019-osmo-timelapse-zero-margin/`。能力 JSON 包含 `hb0` 和 `osmo360ii`，哈苏沿用 host 正式源；公共 Schema 校验通过，SHA-256 为 `19cbe716bb5d7c0ab004e71efd0326b6a1da926353aab3ff502cc5d980ebadf8`。与上一份能力 JSON 比较，仅原生延时的参数类型说明发生变化，其他字段保持。目标备份和安装记录位于 `/home/cld/.camctl/diagnostics/20261019-osmo-timelapse-zero-margin/install-152528/`，本地副本位于 `.local/diagnostics/20261019-osmo-timelapse-zero-margin/tx2-install/`。

## Action6 有限延时与文件能力的软件验证（2026-10-20 UTC）

本轮完成 Action6 普通录像必填参数、原生有限延时、LRF 预览、跨页结果历史及精确设备文件删除的软件实现与门禁。实施范围见[Action6 计划任务 3](../superpowers/plans/2026-10-20-action6-capture-capabilities.md#任务-3文件删除恢复与正式软件链)。环境为 Linux x86_64 开发容器、CPython 3.11.16、SQLite 3.53.1。所有组件测试均按其指南串行运行；命令原文、工作目录、UTC 起止时间和退出码保存于 `.local/verification/action6-capture-20261020/task3/`。

| 命令 | UTC 与退出码 | 结果 |
| --- | --- | --- |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q` | 07:47:55–07:48:23，0 | 6092 passed、1 skipped；2 个现有 pytest 标记警告。 |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/devices -q` | 08:03:32–08:03:44，0 | 220 passed；包含共享读取器异常传播修复后的设备集成回归。 |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/acceptance/test_adb_camera_definitions.py -q` | 07:49:10–07:49:12，0 | 14 passed。 |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture -q` | 07:49:15–08:02:48，0 | 1923 passed；覆盖跨页历史主修复。共享读取器之后的小范围异常分类变更另以相邻行所述定向目标复验。 |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/devices/test_action6_results.py -q` | 08:03:28–08:03:29，0 | 27 passed；覆盖缺少历史回调、回调调用/迭代异常原样传播、完整消费有效历史及冲突判断。 |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture/test_result_scan_runtime.py apps/camctl/tests/integration/capture/test_result_scan_recovery.py apps/camctl/tests/integration/capture/test_result_consumer_saves.py apps/camctl/tests/integration/capture/test_result_page_history.py -q` | 08:04:26–08:05:26，0 | 98 passed；异常分类修复后的结果分页、保存、恢复及消费定向回归。 |
| `uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_distribution.py apps/camctl/tests/integration/bootstrap/test_osmo_shared_recovery.py apps/camctl/tests/integration/bootstrap/test_action6_capture_completion.py apps/camctl/tests/integration/bootstrap/test_osmo_file_cleanup.py apps/camctl/tests/integration/bootstrap/test_cleanup_original_config.py apps/camctl/tests/integration/bootstrap/test_adb_camera_dispatch.py apps/camctl/tests/integration/bootstrap/test_adb_camera_products.py apps/camctl/tests/integration/bootstrap/test_timelapse_finish.py -q` | 08:05:39–08:08:50，0 | 169 passed；内部 READ 恢复分区使用保存的合法 `REQUIRED` 媒体检查决定，原恢复断言保持。 |
| `apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_demo_roundtrip.py -q` | 08:25:52–08:26:25，0 | 1 passed；当前安装版普通录像链保留 1000 毫秒启动余量、STOP 后五秒等待及原片/取回断言。 |
| `apps/camctl/.venv311/bin/python -m pytest tests/integration/test_action6_builtin_timelapse_roundtrip.py tests/integration/test_device_capability_declarations.py -q` | 08:13:15–08:14:27，0 | 7 passed；安装 wheel 经 CLI、HTTP 和客户端表单/导出核对实际必填字段、延时参数与文件能力声明。 |
| `apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_file_cleanup_roundtrip.py tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q` | 08:14:28–08:24:35，0 | 14 passed；文件清理和 OSMO 原生延时安装版回归在同一串行进程完成。 |
| `node scripts/check-doc-links.mjs` | 08:29:09，0 | 4995 个文档本地文件链接通过；脚本不检查标题锚点，另人工核对本轮新增或更新行中的 37 个本地锚点均存在。 |
| `git diff --check` | 08:29:11，0 | 无空白错误。 |

跨页历史修复让正式 ADB 结果适配器在新增设备调用前校验同一 `AttemptTicket` 下已可靠保存的前页，并通过按页读取的回调只消费历史结果引用。共享读取器只在当前预览扫描可靠完成且未找到原片时读取历史；已验证历史中若存在此前观察到的匹配原片，则返回非最终化结果冲突。`history()` 实际调用或迭代发生的本地异常保持原异常传播；缺失回调仍明确报告 `history_unavailable`，不能折叠为空历史。

首轮 bootstrap 曾以 2 failed、108 passed、59 errors 退出。Hasselblad bootstrap 夹具缺少必填 `executable` 且自定义登记遗漏已有驱动项；补全后，两项 OSMO 内部 READ 恢复夹具仍未建立媒体检查责任。最终组合改用正式仓储保存的 `REQUIRED` 决定和冻结目标，再由既有消费者建立媒体读取起点，保持恢复与进度断言，并由上表的 169 项组合通过。安装版 capability 初轮因测试重复使用已导出的只读草稿而失败；现由每种真实输出表单选项使用独立草稿并分别导出，最终联合门禁 7 项通过。普通录像首次当前源码复测还遇到并发读取 ADB 替身日志的未换行末尾片段；读取器只忽略未交接的最后片段，完整坏行仍报错，写端校验单次记录完整写入，最终安装版普通录像门禁通过。

软件门禁不包含 ARM64 部署或真实 Action6/OSMO 设备执行。本轮没有连接相机，没有验证设备固件对 STOP、自然结束、光圈/码率实际设置效果或物理删除的行为；Action6 原生延时仍受已有固件问题与真机验证暂停约束。三十秒等待是任务合同采用的运行假设，不是设备实测保证。

## 视频结果模式与 Action6 分段的软件验证（2026-10-23）

本轮完成[视频结果模式与 Action6 分段计划](../superpowers/plans/2026-10-23-video-result-modes-and-action6-segmentation.md#本版执行与验证安排)的 V1—V11 软件范围。两类相机通过每台设备的[部署配置](configuration-reference.md#设备与驱动)选择独立的 `single_file` 或 `segmented` 流程；省略配置时采用 `single_file`。模式、设备策略、覆盖目标和操作预算随动作受理保存，重启后继续消费原定义。公共层负责有序集合、范围、独立检查、共同发布、取回复用与历史，Action6 和 OSMO 各自提供身份及完成规则。

环境为 Linux x86_64 开发容器、CPython 3.11.16、SQLite 3.53.1、Node.js 24.20.0。源码侧检查显式设置所属工作树的 `PYTHONPATH`，并记录 `camctl.__file__`；安装版子进程移除源码搜索路径，从独立环境的 `site-packages` 运行。组件集成按测试指南在各自工作树内逐目录、逐进程运行。下文的 `python` 指 `apps/camctl/.venv311/bin/python`，命令以对应仓库根为起点；客户端命令在 `apps/client` 执行。

### 单元、组件和客户端门槛

最终生产基线 `33687b5` 的完整命令 `python -m pytest apps/camctl/tests/unit -q` 为 **7065 passed、1 skipped、2 条既有标记警告**，退出0。实际UTC为05:20:46—05:21:31，pytest耗时44.30秒；命令、解释器、源码位置和退出码保存在 `.local/verification/action6-full-plan/release-final-unit/`。跳过项为当前平台不适用的分支；两条警告来自既有同步函数上的 asyncio 标记。

16个组件目录均已完成完整范围。下表保留各阶段的实际结果；它们不是同一提交的一次运行，后续变更由受影响路径的组合验证补齐。完整日志、原始收据和日志摘要位于 `.local/verification/action6-full-plan/agent-evidence/`、各 `final-component-*` 目录及 `component-gates.json`。

| 完整组件目录 | 实际结果 |
| --- | --- |
| acceptance | 338 passed。 |
| bootstrap | 1346 passed、1 skipped；按同一有序集合分批完成，说明见下文。 |
| cancellation | 396 passed。 |
| capture | 2091 passed。 |
| contracts | 442 passed。 |
| devices | 220 passed。 |
| history | 345 passed。 |
| host_files | 121 passed。 |
| logging_runtime | 112 passed，保留1条既有 asyncio 标记警告。 |
| motor | 12 passed。 |
| operations | 314 passed。 |
| outputs | 2501 passed、2 skipped。 |
| persistence | 860 passed。 |
| reporting | 440 passed。 |
| scheduling | 136 passed。 |
| session | 194 passed。 |

bootstrap 首轮已通过479项并跳过1项，在第481项构建解释器准备阶段停止；明确 `UV_PYTHON` 指向部署版解释器后，按保存的1347项有序集合只运行余下867项，全部通过。首480项排除文件的SHA-256为 `5a6da13099b75ba20c4104c7976aec7186623bee174bd65eb5edf417d5ef7d50`；原失败、一次主动中断的重复运行、完整集合及最终867项日志均保留于 `agent-evidence/bootstrap-final/`，不把原失败称为完整成功运行。该目录跳过项仅适用于 Windows 不区分大小写的文件系统。outputs 两项跳过分别属于内部检查副本不走普通交付发布、主机源候选没有设备读取配置的既有分支。

共享目标、覆盖算法和判决解码现由 `contracts.video_coverage` 唯一维护，集合引用及游标由 `contracts.segment_values` 维护。设备、拍摄、持久化与报告使用相同类型；报告不导入拍摄流程。此整理之后，完整 contracts 442项、依赖边界10项、覆盖与设备单元75项通过；最终解码调整后覆盖单元16项及报告2项通过。根集成后的受影响 capture 六文件24项、outputs 三文件11项、同H历史1项分别通过，收据为 `shared-types-capture/`、`shared-types-outputs/`、`shared-types-history/`。

客户端完整 `VITEST_MAX_WORKERS=2 npm test` 通过66个单元文件3212项、40个集成及浏览器文件1559项，同时完成类型检查和实际构建，退出0。实际UTC为03:18:44—03:30:14，689.23秒；收据为 `client-final-workers2/`。公共Schema随后恢复原生延时 RUNNING／EXPIRED 的 `outputs` 数量上限，同时允许适用终态的覆盖结果；修正后完整客户端单元再次3212项通过，相关报告、知识合并和浏览器5文件213项通过，`npm run build` 再次通过。后三项分别位于 `client-schema-unit/`、`client-schema-reports/`、`client-schema-build/`。构建只有既有chunk大小提示；后续Python整理没有修改客户端或公共Schema。

### 生产路径与恢复验证

| 生产责任 | 实际可观察结果及证据 |
| --- | --- |
| 部署绑定、冻结与独立单文件流程 | 实际装配受理4项验证同驱动不同设备、配置切换、旧请求和新请求；无视频任务及仅照片任务拒绝 `segmented` 的反例先2项失败，修复后相关61项通过。原普通单文件仍要求恰好一份非空必要视频，native保留原最低格式及数量要求，异常仍进入原媒体检查和修复。 |
| OSMO结果读取 | 冻结模式贯穿 `PendingResultScan`、装配端口及驱动请求；`single_file` 使用原目录配对，`segmented` 保存原始页文件后由集合配对。普通单文件LRF保存为PREVIEW，指向同目录同stem的OSV；部署切换后重启仍读取原模式。对应单元35项、设备页6项、bootstrap模式3项、共享恢复22项及相邻拍摄81项通过。 |
| Action6正常分段 | 普通及native均登记全部合格MP4、真实LRF和独立照片；默认视频范围不含照片，正常检查拷贝与探测为零。默认、预览、明确ID和其他来源选择保持独立资格。 |
| 实际内容选段 | 普通60秒目标使用18／27／22／40秒分段，只检查前三段，确认67秒；native的300秒／2秒目标冻结为151帧，60／60／45／20帧只检查前三段，确认165帧。选中的最后一段完整交付。指标未知只保留已检查的连续前缀，完整可靠不足才确认不足；后段不补位。 |
| 生命周期及错误优先 | 实际处理器验证基准、START、绑定、STOP耗尽、时钟限制、终态重入及取消。普通取消不发布正式产物；native取消保留全部合格完成视频和照片。必要照片缺失、可靠缺口、零字节及明确无主视频流不能被已达到目标覆盖；可靠控制与覆盖未知分别判定。 |
| 检查身份和冷恢复 | 每段具有独立检查、输入copy与工具操作。部分副本重开后保留copy、目标、原预算及偏移；JSON整数秒经统一配置解码恢复为精确Decimal，显式零值保留。媒体结果已保存而回执未知时使用原结果，不重新探测。 `test_action6_budget_cold_recovery.py` 实际保存4字节后重开并续传；安装版另验证已提交1 MiB的恢复。 |
| 来源事务和等待者 | `segment_sets.published_event_id` 固定完整来源发布H；原事务恢复真正RUNNING且PENDING等待来源的取回。两个已等待取回有独立delivery、copy及目标，后到请求不绑定检查副本而读取设备。来源提交回执未知时核实原H，不重建等待集合。 |
| 读取、重拷和源失效 | 正式本地读取中源失效，经连续原重拷历史核实后，沿同一copy、delivery、target、round及剩余预算读取设备；已提交偏移保留。提交前后回执未知只产生一次换源，预算耗尽有限失败。目标摘要不匹配使用原重拷合同，不把目标错误当作检查源损坏。相关换源4项、重拷实际文件3项通过。 |
| 实际使用和清理 | 来源副本及交付目标均登记实际使用；线程、进程、会话和工具真正结束后才释放。一个等待取回结束、另一取回仍读副本时，维护器保留文件；全部关联责任结束后清理。无等待者、取消收场、实际文件保护及删除失败关闭重开重试均通过，来源终态保持。 |
| 历史和报告 | 真实异常来源固定H后实际完成delivery、保存copy／目标／公开摘要，再删除设备源和清理检查副本；不同分页批次、初始状态／快照／当前状态恢复H时报告原字节一致，不出现H后的摘要。覆盖状态、未知与零、失败来源及成功取回经根Schema、客户端知识合并、媒体上传和累计ACK分别核对。 |
| 重复顺序键 | 后段冲突保留此前可靠前缀；最早键冲突形成未知起点；跨真实仓储分页仍停在冲突组前。反例修复前2 failed、1 passed，修复后真实集成3项、纯前缀10项及OSMO集合66项通过。全部合格原片仍可按明确ID选择。 |

上述定向检查使用真实生产处理器、SQLite和正式文件操作端口；部分组件测试在媒体边界提供受约束响应。实际ffprobe、C host、安装包和客户端联合证据由下节单独说明。原失败日志和状态库保留，命令、UTC、版本与具体范围以原始收据为准。

### 安装版联合链与发行物

安装版联合链通过客户端导出、C host递交、独立wheel受理、SQLite、正式取回、报告及媒体导入和累计ACK核对。普通及native正常分段的默认取回与预览取回各有独立delivery，照片保持独立；正常检查拷贝和媒体探测为零。既有Action6演示、原生延时和正常分段三个文件共4项通过，能力声明6项通过；OSMO普通分段8场景通过。

Action6安装版恢复矩阵使用真实生成媒体及ffprobe：普通不等长达标在copy进度、检查结果保存、来源提交回执、正式本地读取四处中断后分别恢复；第三段工具退出保留45／60秒的已检查前缀并报告 `unconfirmed`；完整可靠50／60秒报告 `confirmed_short`，来源失败但前缀仍可取回。native确认165／151帧但缺必需JPEG时来源失败，视频前缀和独立取回仍可完成。模式矩阵逐项核对两设备、两模式、两动作及配置切换。8个逻辑场景均已有成功记录，两个真实媒体夹具通过；它们在分阶段修复后的候选上验证，未称为同一版本的一次完整运行。

copy进度故障点实际等待1 MiB持久化后中断，并沿同一copy、目标、round及预算从偏移1 MiB继续；source-commit故障点固定两个原等待者，恢复后后到及明确范围外取回实际读取设备；local-read故障点位于第二个等待者读取期间，第一个等待者的delivery已PREPARED、第二个仍PENDING，两取回动作在该截点仍为RUNNING。检查输入的大小及SHA-256在实际ffprobe调用时保存，不依赖副本清理后的路径或文件名。

公共Schema修正后，将9份实际普通异常报告重新导入5组全新客户端存储，reached、unconfirmed及confirmed_short均保留，实际累计ACK分别推进到16、16、12、17、17。原始报告、媒体、状态库、导入收据和ACK留在联合验证归档中。

最终生产基线 `33687b5` 在仓库外 `/tmp/camctl-action6-final-installed-33687b5-20261023` 构建并安装候选，普通source-commit恢复及native151缺照片两个代表场景再次通过，2 passed（110.89秒），退出0；实际UTC为05:23:24—05:25:15。22个实际CLI进程的解释器及模块位置均属于该独立环境，子进程没有源码 `PYTHONPATH`。OSMO同一摘要候选的单文件普通录像1项、两种native场景2项及正常分段代表1项分别通过，实际UTC为05:21:07—05:22:29、05:22:51—05:24:07和05:24:32—05:26:05，均退出0；每条链保存实际导入及ACK。原有describe子测按其原候选保留，不当作最终发行物的资源证明。

最终wheel为 `.local/verification/action6-full-plan/dist-final/camctl-0.1.0-py3-none-any.whl`，1,482,718字节，SHA-256为 `d7d056b0c03c1fe6da7fa808d0659a9ab747fb8399806582684c4a6e2d3d4952`。协调者独立核对298份生产Python及27项权威资源，共325个不同路径，当前源码／权威来源、wheel与实际安装副本三方的缺失、多余及内容差异均为0；资源中的Python脚本不重复计入生产Python。逐文件大小和SHA、原始wheel位置、独立命令及实际UTC保存在 `dist-final/source-manifest.json` 和 `release-wheel/`。此前候选保留过程证据，发行使用本段摘要。

两设备安装证据已集中复制到 `.local/verification/action6-full-plan/installed-evidence/`，协调者核对归档摘要并按清单逐文件核验内容：

- `action6-segmented-evidence.tar.gz`：477,891,894字节，SHA-256为 `870bd1302fc2657eceb91775d22bb6bff2861277f5a0c5303ae8ef0ee7840658`。978项内容及清单本身共979个文件成员全部核对；保留既有8场景、最终两场景、实际媒体、SQLite、客户端导入和ACK、原失败及命令收据。仅含一份最终wheel，排除重复虚拟环境、依赖目录和构建中间文件。
- `osmo-v10-evidence-20261023.tar.gz`：17,809,866字节，SHA-256为 `b599a57b946851a54c6ce1be9c4fb24c0056eef15020b21ea553d72116fcfa3a`。868项内容及清单本身共869个文件成员；保留最终单文件及分段代表链、原8场景和各失败候选，版本分别登记。

同目录的两份内容清单、原始归档收据及 `*-copy-verification.json` 记录复制来源和协调核验。组件门槛、最终wheel和安装证据的统一入口为 `.local/verification/action6-full-plan/delivery-manifest.json`；原始世界继续保留，不依赖源工作树重新构造验证结果。

### 公共检查与证据边界

`33687b5` 上协议、报告依赖、事件转换、数据库结构、历史SQL、配置消费及包资源只读检查均退出0，收据为 `.local/verification/action6-full-plan/release-*/`；数据库40张表、3858项断言通过。登记接缝115项、历史SQL27项及覆盖协议专项通过。类型、事件和资源由各自权威来源生成；没有维护第二套完整清单。

5311个本地文件链接、28个相关标题锚点、39条计划Python测试路径及正式文档指向临时文档的链接检查均通过，收据为 `release-doc-links/` 和 `release-doc-anchors/`；最终差异空白检查通过。独立静态复核未发现可沿生产路径确认的缺陷，范围包括共享类型归属、冻结模式、单文件及分段结果读取、两类完成恢复、取回复用清理与同H报告；该审查没有另行运行测试。文档与收据的一致性另经只读复核，两个审查记录为 `final-review.json`、`final-doc-review.json`。

软件设备替身没有验证新固件的实际分段、相机控制、物理故障或现场媒体。本轮未连接相机、未部署ARM、未执行真实拍摄或删除；三十秒等待等设备合同仍需对应现场证据。V12在设备准备后独立执行，分别保留安装、控制、文件、媒体、客户端和ACK结论。


## 部署交接与待核验项

[软件验收映射](software-acceptance.md)逐条登记适用验收与跨模块场景的结论、证据和未核验前提；当前未完成的软件任务以[实施路线图](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)为准。[集成计划 I6 的历史验证记录](../superpowers/plans/2026-09-30-camctl-integration.md#i6-验证记录2026-10-08)与 [B7 发行物记录](../superpowers/plans/2026-09-30-camctl-bootstrap.md#b7-发行物与部署检查)保存当次命令及环境。发行物在源码目录之外构建、安装与运行的要求见[部署说明](implementation.md#构建安装与运行检查)。

下表分别列出已取得的目标环境证据和后续核验边界。当前相机真机调试由接入计划 T9 跟踪；第三方实际接入、目标资源测量及物理断电保持独立验收范围，不因正常相机演示或软件替身通过而视为已验。

核验维度|已取得的证据|后续边界与通过条件|执行者
-|-|-|-
ARM64 运行库|[目标部署准备](#arm-主机部署准备2026-10-10)已确认 Python 3.11.17、SQLite 3.53.1 安装，以及 C host 的目标 Release 构建安装；[Action6 更新部署](#arm-更新包接收与现场状态2026-10-11)和 [Hasselblad 独立部署](#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)实际执行了安装后入口与业务链。|部署仍遵守[统一运行库要求](sqlite-runtime.md)。目标 SQLite 的 WAL、多连接及历史恢复专项，与 ARM64 C host 的完整组件测试分别保留验证范围；普通业务运行不代表这些专项全部执行。|B7 部署验证及 C 模块维护者
外部工具|[ARM ADB 通道](#arm-主机部署准备2026-10-10)已验证受控退出、输出分流和已知二进制字节；Action6 普通录像与 Hasselblad 的真实控制链已有各自记录。软件环境中媒体检查、修复及成品取回的证据见 B7 与组件记录。|目标媒体工具组合按实际业务单独核验，不将 WSL 的工具结果写成 ARM 结果。工具缺失或调用失败按所属错误分类处理；Hasselblad 的工具路径遵守[专用驱动配置](hasselblad-driver.md#设备与拍摄参数)。|B7 与设备联调
真实设备映射|[Action6 普通录像](#action6-普通录像演示的现场验收2026-10-11)及 [OSMO 最短延时手工演示](#osmo-360-ii-最短延时手工演示的现场验收2026-10-16)按用户现场总体确认验收；[Hasselblad 单张拍摄](#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)有真实 RAW、配对预览、两轮独立取回及最终报告证据。各场景采用所属记录中的保证和证据边界。|Action6 原生延时摄影与 OSMO 正式执行及完整业务链由[T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)及其引用的领域计划跟踪；新增设备及未覆盖场景依[设备证据清单](integration-readiness.md#设备证据与联调输入)补齐。|设备联调
目标资源|开发环境中的并发受理及历史恢复测量见[H7 记录](../superpowers/plans/2026-09-30-camctl-history.md#h7-验证记录2026-10-08windows-开发机)。|目标环境的主进程、报告进程及受管工具合计资源尚无测量记录。记录实际环境、负载和数据规模；开发环境数字不成为目标性能承诺。|部署与目标设备联调
物理断电与第三方接入|软件进程中断恢复、真实 C host 组合已有验证；Hasselblad 交付领取由脚本模拟。|目标存储物理断电、真实主程序及传输方的实际接入尚无通过记录，按[host 验收边界](../host-demo/verification.md#仍需真实环境验收)和[目标联调清单](../superpowers/plans/2026-10-11-camctl-target-integration-runbook.md)执行。普通进程终止和模拟领取分别保留自己的证明范围。|目标部署方与第三方协作方

执行者在对应验证记录中保存日期、环境和实际结果。已完成的安装、普通录像和单张拍摄不重复安排验收；新增场景、测量和专项按各自接入条件安排，不能折叠成软件层的已覆盖结论。
