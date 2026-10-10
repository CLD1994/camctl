# camctl 本次运行配置与原设备绑定闭合实施计划

> 供执行 Agent 使用：使用 `superpowers:executing-plans` 逐任务执行，或按已明确指派的子任务使用 `superpowers:subagent-driven-development`。复选框只记录实际实施；计划编写完成不表示软件行为通过。

**目标：** 让当前运行的合法配置实际决定未完成工作的后续操作，同时保持原设备绑定、历史次数、已保存决定与终态，并以两次真实软件会话证明配置更新后的业务链成立。

**组织建议：** 配置加载统一校验并产生本次运行内固定的值，装配层把这些值传入业务运行时。业务流程在取得实际执行资格后核对原绑定，仓储把采用依据与对应事实共同保存；读取、删除、控制调用及等待恢复分别由已有责任边界执行。内部类型、文件划分及新增接口都是实现建议，正式行为契约和项目规则是硬性要求。

**技术基础：** Python 3.11、现有 `Decimal`、`AttemptConfig`、`OperationConfig`、受管工具与读取会话、真实 SQLite 及受真实驱动端口约束的测试替身；不增加第三方依赖或真实设备前提。

**设计依据：** [本地配置与未完成工作](../../architecture/configuration.md#本地配置变化与未完成工作)、[设备绑定异常的影响范围](../../architecture/configuration.md#设备绑定异常的影响范围)、[执行依据与历史报告](../../architecture/configuration.md#执行依据与历史报告)、[查询与核实配置](../../architecture/configuration.md#状态查询与产物核实的配置)、[录像尝试上限](../../architecture/camera-recording.md#启动与停止的尝试上限)、[读取次数](../../architecture/file-copy.md#按文件累计读取尝试)、[有限重拷](../../architecture/file-copy.md#摘要不一致后的有限重拷)、[操作字段](../../camctl/database/operation-fields.md)、[拍摄绑定](../../camctl/database/plans-actions.md#拍摄动作的驱动绑定)、[动作错误](../../camctl/database/action-errors.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [软件验收映射](../../camctl/software-acceptance.md#九跨模块契约场景) · [模块契约](../../camctl/module-contracts.md)

## 全局约束与执行前输入

- 本计划接续路线图阶段 3 的配置收口、阶段 4 的跨模块组合与结果核实；不把模块级单独通过当作组合验收。
- 配置每次 CLI 启动加载，本次进程内不热更新；部署更新须在已有 `run`、`submit` 退出后进行。新请求和旧未完成工作采用本次值，原请求重送不重新受理。
- 原设备身份、`driver_id`、用户要求和生效拍摄参数均来自已保存事实；当前声明不能改写这些事实。历史回放和旧报告重建不加载当前配置重算。
- 动作、流程、尝试、设备与文件事实保持各自状态；动作失败不证明调用已经结束，终止信号发出也不证明实际停止。
- 单元测试隔离真实 IO；组件集成按目录、前台、顺序运行。执行前核对 `apps/camctl/.venv311/bin/python --version` 为 Python 3.11，并阅读 `apps/camctl/tests/AGENTS.md`。
- `device_binding_unavailable` 已登记为执行错误。受影响动作或明细、原事实、流程、父计划聚合与历史按对应完整事务保存，不新增一套错误码清单。
- 录像启动与停止分别采用 `devices.<id>.recording.max_start_attempts` 与 `max_stop_attempts`，默认各 3 次，正常、取消及恢复共享原停止计数。字段及合法范围由[录像尝试上限](../../architecture/camera-recording.md#启动与停止的尝试上限)定义。
- 不改写驱动任务身份及观察身份语义；相关接入裁决由[设备计划](2026-09-30-camctl-devices.md)承接。新增调用参数若影响驱动端口，应先给出所有消费者和替身的兼容方案。

## 当前代码起点与证据边界

2026-10-09 静态核对发现以下入口需要组合验证。以下内容定位实施起点，不表示已执行失败测试。

| 责任 | 生产起点 | 已有测试及当前证明范围 |
| --- | --- | --- |
| 日常目录绑定 | `persistence/runtime.py::verify_directory_binding`、`bootstrap/application.py`、`bootstrap/lifecycle.py`、`session/service.py`、`reporting/worker.py` | 原绑定检查当前只在 `persistence/initialization.py` 导入和调用，日常会话及报告工作入口尚缺与本次三路径的实际比较。捕获 `ConfigurationFailure` 不能证明有人执行了检查。 |
| 配置加载 | `bootstrap/config.py` 的 `load_config`、`_validate_devices`、`_validated_recording`、`_validated_seconds_subtable`、`_validated_attempts_subtable` | `unit/bootstrap/test_configuration.py` 覆盖已接入字段；设备子表部分字段按原样冻结，尚不能证明所有正式字段的范围校验和实际消费。 |
| 原绑定核对 | `devices/bindings.py::check_binding` | `integration/devices/test_evidence.py::test_binding_errors_cover_capture_retrieval_cleanup_and_shutdown` 直接调用核对函数及停止替身。对全部生产源码的语法树检查未找到该函数的导入或调用，测试没有证明实际执行入口拒绝异常绑定。 |
| 拍摄及独立收场 | `bootstrap/flows.py::capture_flow`、`bootstrap/capture_assembly.py::session_capture_assembly`、`capture/handlers.py::CaptureRuntime.grant`、`_control_call`、`_stop_call`、`capture/residual.py::pass_residual_gate`、`residual_flow` | `integration/bootstrap/test_recording_stop.py`、`test_residual_winddown.py` 覆盖默认配置的正常、失败、取消和残留路径。装配缺失设备时返回 `None`；启动采用固定 1 次／30 秒，停止采用固定 3 次／10 秒。 |
| 跨设备取回 | `bootstrap/obtain_assembly.py::session_obtain_assembly`、`outputs/obtain_flow.py::_device_source_candidate`、`advance_obtain` | `integration/bootstrap/test_obtain_flow.py` 与 `integration/outputs/test_read_recovery.py` 覆盖既有取回和原身份恢复。运行时按当前声明选择读取驱动，尚缺配置变化前后及异常绑定的逐项组合。 |
| 跨设备清理 | `bootstrap/cleanup_assembly.py::session_cleanup_assembly`、`_member_binding`、`outputs/cleanup_flow.py::delete_source_file`、`advance_cleanup` | `integration/bootstrap/test_cleanup_flow.py` 覆盖设备和主机成品清理。工厂当前只返回第一台可装配设备的运行时；成员绑定取当前驱动且未读取原 `driver_id`，无绑定时保留等待。 |
| 延时等待 | `bootstrap/capture_assembly.py::execution_wait_config`、`capture/handlers.py::_timelapse_handler`、`persistence/repositories/timelapse.py::TimelapseRepository.reconfigure_wait` | `integration/capture/test_timelapse.py::test_restart_reconfigure_uses_current_extra_wait` 直接调用仓储。生产语法树没有 `reconfigure_wait` 调用，等待装配只读执行定义且未取本次 `capture.extra_wait_ms`。 |
| 结果核实 | `bootstrap/capture_assembly.py::DriverResultListing.list_files`、`capture/handlers.py::_listing_round`、`_confirm_timelapse_results` | `integration/bootstrap/test_timelapse_finish.py` 已覆盖成功、缺必需类型、未写完后成功、轮次耗尽、列举错误与两类取消；`integration/capture/test_result_confirmation.py` 覆盖保存分类。装配仍固定 3 轮／10 秒，尚缺合法覆盖和跨运行组合。 |

## 状态与输入矩阵

表中按“已有终态、取消、未来时间、窗口、绑定、原调用、预算”的顺序判断。适用行确定后再判断后续条件，不能把所有状态折叠为当前声明缺失。

| 当前状态或输入分区 | 必须发生的行为 | 不变量与可观察结果 |
| --- | --- | --- |
| `run`、`submit` 或报告子进程的本次规范三路径与状态库任一保存值不同 | 在使用业务状态或文件前拒绝，报告 `configuration_error`，指出每个变更字段、原路径和新路径 | 不受理计划、不启动设备或报告工作、不修改绑定；`describe` 不新增状态库依赖。 |
| 日常三路径全部匹配 | 按所属入口继续，其他错误按各自规则处理 | 不扫描全部旧目录，不把绑定检查变成显式切换；库缺失或无效仍按状态库错误。 |
| 动作、处理项或责任已经终态 | 保留原终态、结果及决定；只继续该终态仍要求的独立收场 | 提高上限、修正配置和同请求重送均不重开普通工作；旧报告字节保持。 |
| 已取消且尚有必要停止、读取结束、删除核实或交付撤回责任 | 先保存取消事实，沿原责任收场；停止或查询需要异常绑定时保存该收场失败 | 取消终态不被改写为普通执行失败；原次数、未知效果、限制及保护保留。 |
| 动作计划时间尚未来到 | 继续等待原时间，不因发现异常绑定而提前执行或失败 | 无新增设备调用、尝试和执行标记。 |
| 拍摄窗口已满足过期条件 | 按既有错过／耗尽分类保存过期 | 不用绑定错误覆盖过期原因；在途尝试仍独立收场。 |
| 当前设备缺失或驱动不匹配，且工作已具备执行资格 | 相关拍摄保存执行失败；取回／清理按受影响项失败；其他设备及主机工作继续 | 错误采用 `device_binding_unavailable` 并含原设备、预期驱动、已知实际驱动及原因；不新增尝试，不把失败解释为未执行或已停止。 |
| 设备声明无法可靠使用，或声明指定的驱动未登记 | 停止依赖该配置的会话工作，报告 `configuration_error` | 使用内部 `DeviceConfigurationError` 保留设备及驱动诊断；不伪造合法 `missing`／`mismatch` 的逐项业务结果。 |
| 绑定匹配，但原调用在途或结果未知 | 先观察并完成原调用、核实及适用收场 | 增加额度不授权重复未知副作用；已有实际成功与诊断分别保存。 |
| 新触发动作按本次驱动 B 受理并且自身绑定匹配，同一设备仍有由驱动 A 保存的未解决活动 | 对原活动按 A 的绑定核对；A 绑定异常时，原停止／查询流程失败并保持活动事实，不能把原定位信息交给 B | 新触发动作按既有 `device_activity_unresolved` 执行失败，详情指向原 `activity_id` 与 `device_id`；设备调用为零，不能把自身匹配的动作记为驱动不匹配。 |
| 需要新增尝试、原累计为 `used`、本次上限为 `limit`，且 `used < limit` | 继续核对窗口、间隔、取消及设备条件，全部成立才提交新尝试 | 剩余名额为 `limit - used`；原责任、身份和次数不重置。 |
| 需要新增尝试且 `used >= limit` | 保存所属责任的额度耗尽结论；不再新增尝试 | `used > limit` 是合法历史，不裁剪旧记录或重建流程。 |
| 恢复已登记的未结束尝试或重拷轮次 | 恢复原身份与可靠进度，不重复计数；后续新操作采用本次配置 | 原次数、文件、交付及关联保持；可靠未派发也不退还已消耗次数。 |
| 修复或其他业务决定已经提交 | 按原决定继续；决定后的读取、等待及重试采用本次配置 | 新门槛不能重算或推翻旧决定；提交未知先核实原事务。 |
| 未完成延时等待，本次额外等待改变 | 保留原 `sent_at`、首次目标时长与驱动必要余量，重新保存 `extra_wait_ms_used`、`expected_check_at` | 从原锚点推导；配置更新或数据库耗时不生成新发送时间。 |
| 已保存等待完成或结果集合结论 | 沿原完成事实及结论收尾，不重新等待或重开核实轮次 | 新等待值和上限不能改变原结论；没有设备状态观察时不补造 `ENDED`。 |
| 配置文件本身非法，或状态库／锁／时钟等会话前提失效 | 按所属会话错误处理 | 局部绑定失败策略不能掩盖会话错误，缺失状态库不能重建为空库。 |

已明确字段的合法性分别验证：正整数次数拒绝布尔值、非整数、零和负数；重拷次数与 `capture.extra_wait_ms` 接受整数零；秒数采用精确数值，调用时限必须有限且正，间隔允许有限非负。缺省与空表采用所属规格默认值；未知字段按配置契约拒绝，驱动专属配置按其权威定义校验。

## 任务与依赖

RC0 保证日常入口使用当前部署绑定；RC1 提供合法、固定的本次配置；RC2 以原设备绑定建立执行前提。RC3 与 RC4 在 RC0—RC2 之后推进，RC5 最后验证所有接缝。预计涉及现有大文件，执行者可在不改变责任和协议的前提下拆分；不得用计划中的建议文件划分限制必要的数据流调整。

### RC0 日常会话与报告工作的目录绑定门禁

**预计文件：** `persistence/runtime.py::verify_directory_binding`、`bootstrap/application.py`、`bootstrap/lifecycle.py`、`session/service.py`、`reporting/worker.py` 及报告任务消息；测试为 `integration/bootstrap/test_configuration.py`、`integration/session/test_session.py`、`integration/reporting/test_worker.py` 和根 CLI 组合。

**接口与交付：** 在每个状态库连接打开并可靠读取元信息后、业务受理及文件副作用之前，把本次规范化三路径与元信息整体比较。复用与显式初始化相同的规范化语义；数据库不存在或损坏仍属于状态库错误，匹配失败属于配置错误，不能用捕获所有 `StateDatabaseError` 实现分类。报告任务需携带或可靠取得同一份本次规范绑定，若现有消息缺少输入，应先列出生产者、子进程消费者与消息测试的必要调整，不从报告临时文件路径推测完整三路径。

日常装配先用既有状态库的只读连接核验，失败时保存最终会话机器结果。目录不匹配时不启动业务日志与报告协作者；状态库不可用时仍沿独立日志通道记录会话错误并有序关闭。有效装配保留本次规范化 `ConfigSnapshot`；会话及流程重新打开业务连接时，仍在受理或处理前核对该份输入。规范化共享初始化的主机词法绝对路径规则，不解析根软链接，也不改变初始化对真实根位置的核验。

报告 `JobMessage` 的生产者为 `bootstrap/flows.py::report_flow`，消息编解码及子进程 `worker.py::run_job` 是消费者。消息必需携带 `staging_root`、`ready_root`、`processing_root`，值来自本次规范配置；数据库身份可取已核实连接的元信息，但不能用元信息的旧路径替代本次配置。子进程先可靠打开库、核对实例与这三路径，再生成任何文件。内部消息测试和所有构造者同步调整，公共报告格式不变。

| 失败发生位置或分类 | 最终行为 |
| --- | --- |
| `run`／`submit` 装配及业务连接发现路径不匹配 | 最终机器结果为 `configuration_error`，未开始受理或报告／设备处理，诊断逐项包含变更字段及原／新路径。 |
| 子进程发现路径不匹配 | 返回 `ErrorKind.REPORT` 与精确 `error_code="configuration_error"`；主进程转换为配置前提错误，不保存普通报告失败或继续设备流程。 |
| 正常报告流程或受限会话的一次报告收到配置前提错误 | 正常会话停止后续流程；受限会话保留配置主错误，不被普通报告捕获或 `clock_invalid` 覆盖。 |
| 任务因期限、发送、等待或协议错误开始收场，随后送达同一任务及实例的配置错误 | 监督方保存该错误；生成结果仍携带配置错误，不能因迟到丢成普通报告故障。不同身份的结果保持协议错误。 |
| 显式停止收到未决任务的配置错误 | `WorkerShutdown.configuration_failure` 保留事实。最终会话的成功或普通报告错误升级为配置错误；已有其他致命错误保持主错误并追加诊断。 |
| 同次收场也收到状态库错误 | 原有 STATE 优先链保持：状态库错误主导生成分类，最终会话沿原状态库主／次错误规则处理。 |
| 状态库缺失、无效或元信息不可读 | 最终机器结果为 `state_db_error`，不建库、不建立新绑定。普通文件生成及交接错误仍按报告错误。 |

2026-10-09，Python 3.11.16：CLI 窄门禁得到 `10 failed, 4 passed`，失败为八种目录变更缺少配置错误及两个缺库入口没有机器结果；消息与监督方窄门禁六项均失败，分别暴露本次三路径丢失和迟到配置错误丢失。实际 worker 三项目录变更均错误地生成了文件，缺库保护通过。上述结果只授权相应 RC0 分区的实施，后续真实 worker 与会话消费证据仍需单独核验。

相应生产接线后，CLI 原窄门禁 14 项通过，正常／受限报告消费者及最终关闭分类的组合共 24 项通过，消息与迟到结果单元共 68 项通过，实际 worker 文件 11 项通过。reporting 单目录独立回归 352 项通过，覆盖历史分页与 RC0 消息／worker 组合。非法相对路径、NUL、展开后词法规范化、报告开库错误分类及 bootstrap 完整目录仍须保留各自证据；本节不以 reporting 目录通过替代日常会话或整个配置目标的验收。

- [ ] 添加 `test_run_and_submit_reject_each_changed_directory_before_acceptance`：分别只改变 staging、ready、processing 及改变全部三项，真实 CLI 返回 `configuration_error`；诊断包含变更字段和原／新路径，库、受理事件、动作、设备调用与业务文件保持。路径书写不同但实际规范对象相同的输入通过。
- [ ] 添加 `test_report_worker_rejects_mismatched_binding_before_generation`：主进程检查之后、子进程开库之前受控改变绑定输入，子进程拒绝且不创建或覆盖临时文件，不读取历史生成报告；通过既有报告通信保留配置错误所属范围，不把合法配置不匹配冒充状态库损坏。
- [ ] 添加 `test_directory_gate_keeps_database_errors_and_describe_scope`：库缺失／无效返回原状态库错误，`describe` 无状态库也能执行；已经有文件的正常匹配运行不额外扫描原目录，日常入口不能更新绑定。
- [ ] 先确认失败，再接入门禁及精确错误分类。RC0 只比较已保存元信息，不执行目录切换；显示路径错误和诊断须沿正式协议分类。门禁：所有日常入口及报告子进程都具有实际生产调用证据，显式 `init` 的切换行为仍由独立目录切换计划验收。

### RC1 已明确设备配置字段的校验与传递

**预计文件：** `bootstrap/config.py`、`capture_assembly.py`、`obtain_assembly.py`、`cleanup_assembly.py`、`lifecycle.py`；测试为 `unit/bootstrap/test_configuration.py`、建议新增 `unit/bootstrap/test_runtime_device_config.py`。

**接口与交付：** 保留 `load_config(document, defaults) -> ConfigSnapshot` 为配置入口。使用已存在的 `AttemptConfig`／`OperationConfig` 向运行时传递相应操作的本次上限、超时与间隔；原执行定义只提供业务要求和驱动必要余量。具体归一化辅助函数签名由实施者根据当前装配确认，必须保持一处权威默认值，不能在流程、仓储和测试中各维护一份完整字段清单。

- [ ] 先添加 `test_known_device_fields_reject_invalid_values`：逐字段覆盖 `recording.start_timeout_s`、`stop_timeout_s`、`max_start_attempts`、`max_stop_attempts`、`result_check.max_attempts`、`call_timeout_s`、`copy.max_read_attempts`、`max_recopies`、`read_idle_timeout_s`、`capture.extra_wait_ms`；精确断言合法值归一化及非法值抛 `ConfigError`。
- [ ] 添加 `test_runtime_policies_use_current_device_values`：输入两台设备的不同值，断言相应运行时采用各自值；省略字段和空表取默认值，修改原配置输入不能改变已加载快照。
- [ ] 单独运行上述单元失败测试，确认红来自字段未校验或未消费；再实现字段校验和配置传递，复跑至绿。不得用原样冻结、静默忽略或统一默认值满足该任务。
- [ ] 门禁：完整执行 `unit/bootstrap`；审计所有已明确设备子表字段的生产消费者，记录已经接入和仍待字段裁决的项目。

### RC2 原绑定核对与局部失败的完整事务

**预计文件：** `devices/bindings.py`、`bootstrap/flows.py`、`capture_assembly.py`、`obtain_assembly.py`、`cleanup_assembly.py`、`capture/handlers.py`、`capture/residual.py`、`outputs/obtain_flow.py`、`outputs/cleanup_flow.py` 及对应窄仓储。建议测试为 `integration/bootstrap/test_binding_changes.py`；已有 `integration/devices/test_evidence.py` 作为核对函数依据，不代替真实执行组合。

**接口与交付：** 消费已存在的 `DeviceBinding`、`BindingResult`、`check_binding(saved, current)`；拍摄从动作取得原绑定，设备文件从原观察者动作取得原绑定。当前声明只选择匹配驱动的运行端口，不能生成或覆盖原绑定。提供类型化绑定失败结果给对应拍摄、取回项、清理项及独立收场的事务入口，普通缺失配置不抛成全会话状态库错误。

- [ ] 添加 `test_changed_binding_fails_only_due_device_work`：先经真实受理保存 A、B 两台设备动作，下一次会话移除 A 或把 A 改绑另一个已登记驱动。断言 A 已到期工作失败且新增设备调用、尝试均为零，B 正常执行；A 的未来、过期、已有终态与已取消分别满足状态矩阵。
- [ ] 添加 `test_cross_device_items_and_host_copy_continue_after_binding_failure`：一个取回和一个清理各涉及 A、B；A 异常项失败，B 项继续；主机完整副本继续发布及本地成品继续清理。清理工厂必须实际处理各设备，不能只处理首个可装配设备。
- [ ] 添加 `test_binding_failure_preserves_unknown_effect_and_cancel_winddown`：已有未知活动及调用责任时，取消事实正常保存，异常绑定的停止／核实失败，其他设备独立收场；原未知效果、保护和限制不能凭局部失败解除。
- [ ] 添加 `test_new_driver_trigger_preserves_old_driver_residual`：真实建立驱动 A 的残留活动及独立收场责任，再以驱动 B 受理同设备的新触发动作。本次配置为 B；原责任保存 A 绑定失败，零设备调用且原活动事实保持，新触发动作按 `device_activity_unresolved` 失败，其自身原绑定仍为 B。
- [ ] 对相应事务注入提交前失败、明确回滚、提交结果未知与提交后通知丢失；核实原请求和完整事务，未完成结果不产生部分项、重复计数或错误的父计划终态。
- [ ] 确认失败后接入统一原绑定核对与分支事务，逐项复验。门禁：配置修正及同请求重送保持原失败，全部本地责任完成后 `run` 正常退出，无新责任时 `needs_run: false`；状态库错误仍进入会话错误。

#### RC2 拍摄恢复及取消的事务分区

拍摄动作已经保存启动意图后，绑定核对不能只覆盖首次调用。处理器先读取原启动事实；本会话内发起并仍在等待结束的调用继续由原 `await` 的拥有者跟踪，不发出新的停止、查询或结果列举，也不改写尝试结果。数据库中的 `RUNNING` 只说明结束结果尚未保存，并不单独证明本会话仍有实际调用。跨会话恢复按[未完成调用的正式结果规则](../../camctl/database/operation-fields.md#第一版-adb-未完成调用的恢复记录)使用旧本地执行工作已经可靠收场的边界；仅有程序重新启动这一事实不构成依据。原调用取得或可靠恢复结束结果后，仍需异常绑定的工作按下表结束。零次调用失败与未知启动失败都保留原 `actions.driver_id`、尝试编号、效果、设备活动及占用；本地业务结束不提供设备已经停止的依据。

会话在取得会话锁及首个可靠状态视图后固定历史边界 H，并在受理及本会话的新事务开始前保存该值。所有拍摄运行时共享这一个 H；工厂不重新查询当前 C 作为恢复起点。`recovery_boundary` 明确区分未确认、host 可靠收场及主机重新上电。`recovery_evidence_for(saved_binding, operation)` 从原 `actions.driver_id` 的登记项取得适用声明与证据；当前配置缺失或改绑 B 时仍不能改用 B 的声明。

| 未结束尝试与恢复依据 | 原尝试及后续工作 |
| --- | --- |
| 意图事件在 H 之后，属于本会话新发起调用 | 保留原调用管理责任；绑定失败不能提前写尝试、流程或动作终态。 |
| 意图事件不晚于 H，但旧本地执行收场边界未确认，或原操作未声明适用普通前台假设 | 保留未完成责任和诊断，零新设备调用；不把未知结果转为可靠未派发。 |
| 意图事件不晚于 H，旧本地执行已可靠收场，原操作适用普通前台假设，且不是具备续传资格的读取 | 沿原 ticket、次数和责任保存 UNKNOWN、实际已知效果、原结果未可靠保存的错误，以及 `adb_foreground_recovery`/v1、ASSUMED、空 data；没有原退出信息或可靠观察时不生成 `call_info` 或观察。随后执行绑定失败事务。 |
| 原尝试已有可靠结束结果 | 保留原结果及冻结历史，不生成恢复结果覆盖它。 |
| 原读取符合续传资格 | 沿原身份继续读取；不能仅因旧本地连接已结束而提前终态化。 |

本会话已经取得的完整 START 返回结果优先于上述假设恢复。`PendingStartResult` 按原 `(run_id, attempt_no)` 保留正式结果、派生观察、事务键及实际返回锚点；同一会话的拍摄工厂为正常及绑定失败运行时提供同一缓存。保存失败时先核实原事务并保存该实际结果，不能生成 UNKNOWN 覆盖它，也不能因为下一轮重新构造 Runtime 而丢失。多个设备共享工厂时仍按原尝试身份区分，不能按设备清空其他尝试。

独立残留消费者按有界批次查找尚为 RUNNING 的旧 START、普通 STOP、活动 QUERY、结果核实及残留 STOP 尝试，不依赖动作或流程仍未终态。每个原 ticket 继续经过固定 H、可靠旧本地执行边界与原驱动声明；本会话的新意图保持原实际拥有者。扫描只保存原调用的正式恢复事实，不生成新的启动或停止，也不改写原动作、流程终态及设备活动。恢复之后，仍适用的必要责任沿其原消费者推进。

| 原动作及必要收场条件 | 动作与流程结果 |
| --- | --- |
| 原动作已有终态 | 返回原结果，不更新原流程终态，也不增加绑定失败事件。 |
| 取消已生效，启动可靠未派发或全部尝试可靠无效果 | 继续原未启动取消事务及适用释放，不因绑定异常新增停止责任。 |
| 取消已生效，活动已有可靠结束事实或 STOP 已有终态 | 使用原停止或无需停止依据，保持原终态及错误，不重开停止责任。 |
| 取消已生效，活动效果未知或仍在执行，驱动不支持停止 | 沿该动作既有无停止能力的取消规则处理，不凭绑定异常创建 STOP 或声称设备已停止。 |
| 取消已生效，必要停止仍需执行，原驱动支持停止，STOP 尚未建立 | 同一事务建立 `stop/<action_id>` 的零尝试责任，先保存 `OPERATION_CONFIGURED.CREATE` 的 PENDING，再保存 `FINISH` 的 FAILED 和绑定错误，最后保存目标 CANCELED 及计划聚合。采用本次停止配置；不创建尝试或设备观察。 |
| 取消已生效，必要停止仍需执行，原 STOP 尚未结束 | 同一事务保存该 STOP 与相关未完成核实流程的绑定失败、目标 CANCELED 及计划聚合。已有次数及尝试结果保持；取消结果由原 STOP 的失败事实判定。 |
| 尚未取消，仍需异常绑定推进启动、停止、查询或结果列举 | 同一事务保存动作 FAILED、相应未完成流程 FAILED、计划聚合及所属历史；不增加调用或尝试。 |
| 同设备的新触发绑定匹配，原残留活动的驱动与本次配置不同 | 原残留流程保存原绑定失败且保留活动，新触发动作保存 `device_activity_unresolved`，包含原 activity_id/device_id。不能把新触发动作的匹配绑定写成 mismatch。 |

建议增加类型化 `FinishBindingFailure` 输入，在 `CaptureRepository` 的单次 `commit_operation` 内组合终态命令及流程事件。动作和计划的事件归属于各自对象；普通 START、STOP、查询和核实流程沿原 `action_id` 归属，残留停止沿目标活动所属动作归属。所有子计划只读取同一个事务开始状态，事件编号连续且不同；外层 `scope.allocate` 只调用一次，子范围不得嵌套提交。新 STOP 的 CREATE 和 FINISH 依次更新同一行，创建行全部业务字段与后续变更均受正式事件守卫检查。

原操作键重送核对完整事件组：动作身份、原绑定、取消分支、事实时间、绑定错误详情、流程身份及结果、新建 STOP 的配置全部一致。不得只核对动作终态或只核对某一个子计划；不得在重新读取现投影时用新的配置或新流程集合替代原输入。完整组包含动作／计划段与流程段时，按正式事件类型划分后分别核实，并拒绝未知、重复、缺失或属于其他责任的事件。共享 `state_rows` 的同一行事实必须一致，完整读取范围只声明真实完成的查询。

##### 取消延时摄影的停止与文件核实分别汇总

延时摄影 T 已经或可能启动后，合法取消要求有限停止及适用文件核实。普通 STOP 的成功仅证明停止责任成功，不证明已取得本次全部适用产物；STOP 的失败也不消除对已拍完文件的有限核实。以下分类依据[取消及文件保留](../../architecture/camera-capture.md#取消失败与文件保留)、[取消动作完成时机](../../architecture/task-cancellation.md#取消动作的完成时机)和[绑定异常影响范围](../../architecture/configuration.md#设备绑定异常的影响范围)。先检查已保存终态、取消合法性和未结束实际调用，然后分别核对停止责任与 `results/<activity_id>`。

| 原停止、启动和文件核实事实 | 目标与本次取消结果 |
| --- | --- |
| 目标已有终态 | 保持原目标和产物；只等待仍适用的独立调用收场，不因新配置重开文件核实或取消结果。 |
| 启动可靠未发生，全部原尝试已结束且无效果 | 本地保存未启动 CANCELED，不创建 STOP 或 RESULTS；本次取消没有停止或文件核实失败。 |
| 原启动、STOP 或 RESULTS 尚未保存可靠结束结果 | 先保存本会话实际返回结果，或按旧调用恢复双门保存适用结果；条件不足时保留责任，零新调用，不用业务终态代替调用结束。 |
| 可能启动，原能力不支持停止且取消尚未生效 | 按取消资格拒绝本次取消，目标保留原普通执行；异常绑定按尚未取消工作的失败规则处理。 |
| 可能启动且取消已生效，但原能力不支持停止 | 先核对已提交取消是否具有可靠未启动等合法依据；事实矛盾属于状态库错误，不创建虚构 STOP，也不调用本次驱动修正旧能力。 |
| 原 STOP 未结束或尚未建立，文件仍需设备核实，绑定异常 | 同一完整事务保存适用 STOP 与 RESULTS 的失败及目标 CANCELED、计划聚合；实际调用、效果、活动与次数保持。本次取消因必要处理失败而失败。 |
| 原 STOP 已 SUCCEEDED，或活动已有可靠结束事实；RESULTS 尚未建立或仍未结束，且取得文件事实需要异常绑定 | 不重开 STOP，不改变其成功结果；保存本次必要文件核实的绑定失败及目标 CANCELED、计划聚合。本次取消因该文件核实失败而失败，不能单凭 STOP 成功判取消成功。 |
| 原 STOP 已 FAILED 或 UNCONFIRMED；RESULTS 尚未建立或仍未结束且需要异常绑定 | 保持原 STOP 失败及未知设备事实，保存适用 RESULTS 绑定失败并结束目标；本次取消失败，不刷新停止或核实预算。 |
| 原 RESULTS 已 FAILED 或 UNCONFIRMED，且不允许继续；STOP 已完成适用有限处理 | 采用原核实及停止结论本地结束目标，不重开 RESULTS。本次取消包含原必要处理的失败或无法确认结果。 |
| 已可靠取得本次适用文件事实，只剩本地产物登记；STOP 已完成适用有限处理 | 使用已保存事实登记符合条件的完整产物，与 CANCELED 共同提交；不因源绑定变化再调用设备。本次取消分别采用原停止与核实结果。 |

已确认的不变量是目标取消、必要文件核实失败和发起取消逐项失败均须可恢复地表达，并共同保存所属本地事实。建议复用 `FinishBindingFailure`，在原结果核实责任已经存在时结束固定 RESULTS；必要文件核实尚未建立时，用其本次 `check_config` 保存零尝试 `OPERATION_CONFIGURED.CREATE` 的 PENDING，再保存 FINISH 的 FAILED。这个新增责任只记录无法执行的适用核实，不创建尝试、观察、文件不存在或集合齐备结论。它与新建必要 STOP 使用不同流程 ID，归属于 T；动作与计划段保持正式 owner。具体输入字段与事件顺序属于实现建议，实施前须以正式守卫及窄失败测试验证，不能先只放宽空事件组的原键核实。

“已有完整本地输入”分区依赖 RC4 将实际列举结果、文件观察及原键共同保存。RESULTS 的 SUCCEEDED 本身不是完整本地产物输入：只保存 identity 集合结论而尚未保存文件观察时，不能登记空产物或以当前驱动重新取得原文件。维护者须保留该接缝的诊断与原结果责任，由 RC4 的实际结果保存及恢复消费者完成；RC2 的缺／ACTIVE RESULTS 门禁不覆盖这一分区，也不能据其通过声称取消文件核实全部闭合。

原键采用完整固定责任集合及本次实际新建责任的类别、身份和配置；重复请求保持原完整组，改变配置、时间、错误或集合须拒绝。不需要新建 RESULTS 的本地完成分支不采用该绑定失败事务；不能把仅有 CANCELED 动作段当成必要核实失败的完整证据。取消消费者逐项读取原 STOP 与本次必要 RESULTS 的终态：任一仍未结束则保持等待，任一必要处理失败或未知则返回失败；新观察和当前绑定不撤回原处理结论。

先通过真实 timelapse START 返回、合法取消仓储和真实停止结果保存建立 STOP 已终态的前提；分别覆盖 RESULTS 尚未建立及已有结束尝试但流程未终态、missing/mismatch，以及 STOP 成功／失败。断言零新调用与尝试、原 STOP 和活动保持、目标 CANCELED、本次取消逐项 FAILED。再覆盖可靠未启动、已有完整核实及原 RESULTS 终态的本地分支，并对新责任复合事务补回滚、COMMIT-before/after UNKNOWN、关闭重开和原键改变输入。只有这些分区均有证据时，才能将取消收场门禁记为完成。

##### 已有残留流程的原绑定失败

录像 A 的普通 STOP 已经失败，活动仍保持执行及占用。触发动作 T 建立 `followup/<T>/<activity_id>` 并已发出一次停止；该调用已经结束，但设备效果尚未确认。T 后来取消，原残留流程仍沿原有限责任继续。新动作 B 使用本次配置的匹配驱动，既有残留停止及确认查询仍必须沿 A 保存的驱动解释和调用。

| 本轮推进入口与已有事实 | 本地事务及外部调用 |
| --- | --- |
| 本轮触发动作自身取消、终态或不具备时间资格 | 先按其原规则处理，不借新触发资格发出普通启动。已发出的独立残留责任仍由 `residual_flow` 推进。 |
| 本轮触发动作自身绑定缺失或不匹配 | 按自身绑定错误进入拍摄失败责任；不将其分类为另一个活动的占用错误。 |
| 原残留已有可靠成功终态或本轮触发已有可靠空闲检查结果 | 先使用已有本地依据继续既有收场或放行，不因原配置变更撤回确定结果。 |
| 已有残留 STOP 或确认 QUERY 未保存原调用结束结果 | 先按固定 H、可靠旧执行边界及原驱动适用声明恢复；条件不足时保留责任且零新调用。 |
| 原残留流程未结束，原调用均已结束，A 的绑定异常；本轮匹配的新动作 B 已开始执行 | 同一事务保存固定残留 STOP 及其未完成确认 QUERY 的 FAILED 和 A 绑定错误，再保存 B 的 FAILED、`device_activity_unresolved` 及计划聚合。A 的活动、原动作终态、累计次数及已有尝试结果保持，设备调用为零。 |
| 原残留流程未结束，原调用均已结束，A 的绑定异常；独立推进时 T 已终态 | 同一事务只结束固定残留 STOP 及其未完成确认 QUERY，保留 T 与 A 的终态和活动；不将局部绑定失败改成新的触发资格。 |
| 原残留 STOP 或确认 QUERY 已终态 | 保持原结果，不重开责任；固定失败事务只能包含本次仍未结束的责任。 |

建议增加 `FinishResidualBindingFailure`，输入包括可选的新触发动作、固定活动身份、A 的绑定错误、事实时间及完整责任键集合。事务内复核活动所属 A 的设备／驱动、T 与目标活动关联、允许的残留类型和查询用途，以及 B 的设备身份及执行资格。流程事件拥有者继续按目标活动及正式流程规则登记，动作／计划段归属于 B。原键必须核实有无动作段、完整固定流程集合、目标活动、错误详情及时间；仅有动作段或改变集合不能被接受为相同请求。

当前阶段先实现已有 `STOP_RESIDUAL` 的以上分区。尚未建立残留流程时，维护者须沿执行前查询、已知未解决活动和安全停止能力核实是否需要新的责任；该分类不直接套用普通取消 STOP 的零尝试创建规则。独立收场的类型扩展须先以正式规格给出依据及失败验收。

验收先真实建立 A 的确认启动、原 STOP 失败、T 的执行前查询及第一次残留停止结果，再取消 T，并以 B 的新受理绑定建立后续会话。分别用已结束但效果未知的正常停止响应和调用错误触发原确认／重复停止路径，证明两个路径均为零设备调用。协议另验证执行前 QUERY、残留 STOP 和确认 QUERY 实际收到原 `ticket` 及本次配置期限，不以数据库保存配置代替端口传递。随后覆盖投影失败、COMMIT 前后丢失及原键请求改变，并核对完整事件组和责任拥有者。

先以真实授予、UNKNOWN 结果保存及取消仓储建立恢复前提，证明 missing/mismatch 下零新调用、零新尝试、原活动与原未知结果保持。以仅保存意图而没有可靠恢复边界的六项场景验证责任保留；另以固定 H 及可靠 host 边界验证原尝试结束并继续绑定失败，不能将两者合并成一个“在途”分区。再注入投影更新失败、COMMIT 前失败、COMMIT 已完成后返回丢失，关闭重开连接并用原键核实完整组；失败时动作、流程和计划均无部分提交。增加真实调用尚未返回、可靠无效果、停止不支持、停止已终态和已结束活动的保护用例。生产实现按这些窄门禁分组接入，不能根据首次调用的十项通过声称 RC2 已闭合。

#### RC2 取回与清理的独立交接

这一分工消费已经成功加载且在本次运行内固定的 `devices` 配置。权威设备绑定来自设备文件的 `observer_action_id → actions.device_id/driver_id`，不能使用取回或清理动作的设备字段，也不能从本次驱动端口反推原绑定。主机中间文件及已经完整核实的主机交付副本不依赖源设备配置。绑定核对、取消／终态判断与所需副作用分别由下表决定；invalid ConfigSnapshot 和未知驱动登记属于装配或配置前提错误，不伪造公共 `missing`／`mismatch`。

现有生产入口及缺口如下：

- `bootstrap/obtain_assembly.py::session_obtain_assembly` 构造 `DeviceReadAssembly.binding` 时使用当前声明的 driver。`outputs/obtain_flow.py::_device_source_candidate` 只查询原观察者的 device_id；未装配设备抛 `ConsistencyError`，会把业务缺失扩大成会话错误。`_qualify_selected_items` 和 `_advance_reads/_advance_read` 分别覆盖未建交付及已有读取责任，二者都必须接入原绑定核对。
- `bootstrap/cleanup_assembly.py::session_cleanup_assembly` 从第一个可用设备返回运行时；`_member_binding` 只允许该设备，并用当前 driver 构造绑定。`outputs/cleanup_flow.py::delete_source_file`、取消删除分支及恢复核实必须按每个成员选择匹配端口。即使设备目录为空，也必须能够装配 staging 的主机文件处理。
- `OutputsRepository._GrantFileCommand._reject_item` 已能保存未建交付项的最终失败；`fail_read_delivery` 只接受已失败读取流程的次数耗尽原因，不能直接当作任意绑定失败入口。`fail_cleanup_item` 维护清理限制及产物聚合，但伴随流程收场目前另行提交。需要窄仓储输入共同保存绑定失败与适用责任，不能按函数名认为现有事务已经覆盖。

| 动作／成员与源状态 | 必须执行的行为 |
| --- | --- |
| 动作未来、尚未取得来源或处理资格 | 保持原等待与时间资格，不因扫描历史提前写绑定失败。 |
| 动作或成员已有终态 | 返回原结果，配置修正及原请求重送不重开成员。 |
| 未取消的已选取回项，尚无交付，源设备绑定异常 | 保存该 obtain_item 的绑定失败，不建立交付、拷贝或读取尝试；继续其他项。先按既有规则判断源可用性及删除限制，不能绕过限制来发读取。 |
| 未取消的取回项已有交付／未完成主机拷贝，下一步仍需异常设备读取或源端摘要 | 同事务保存受影响项／交付的失败及原 READ_FILE 等责任结束；不新增尝试、不退已用次数，不发布未核实副本。已有进度、未知效果和适用读取保护保留。 |
| 设备来源的交付副本已经完整核实，只需本地发布或撤回 | 使用原已核实副本完成本地工作，不因源设备绑定异常改成失败或重新读取／校验。 |
| 取回源本身是主机正式中间文件 | 继续本地复制、核实及交付，不要求设备目录存在。 |
| 未取消的清理项需要异常绑定删除或存在性查询 | 保存该 cleanup_item 的绑定失败及其未完成 DELETE_FILE／CHECK_FILE_EXISTS 责任结果，按实际删除事实维护原限制、输出及源存在性；没有调用时不新增尝试，不能报告文件已删除。 |
| 清理项已有可靠缺席事实，只需补齐本地结果保存 | 使用原可靠事实完成，不再调用设备，也不凭新配置改写该事实。 |
| 清理对象是主机派生成品 | 继续本地删除／查询，零设备目录也不能阻止它。 |
| 一个动作同时涉及异常设备 A、匹配设备 B 和主机文件 | 逐项使用各自端口及原绑定；A 失败，B 和主机项继续。全部适用项结束后按既有逐项汇总保存动作与计划结果。 |
| 取消已生效，原读取／删除未发出或已经实际结束 | 保持目标取消；本地撤回、保护与限制处理按所属规则完成，不因绑定异常恢复普通读取／删除。 |
| 取消已生效，仍需异常绑定核实已发出的删除等未知效果 | 所需核实以失败结束，保留原未知及限制；目标取消终态和发起取消动作的逐项失败分别汇总。不能把未核实解释为删除未发生。 |
| 原设备调用仍有本会话实际拥有者 | 先跟踪实际结果及适用收场，不用成员失败作为实际结束证据，不先释放读取名额、保护、文件或通道。跨会话只有持久化 RUNNING 时的恢复前提须单独核验。 |
| 任一所需状态查询／事务回滚／提交未知不能可靠确认 | 保留责任并进入状态库错误，不能以绑定可以局部失败为由跳过数据库结果。 |

建议按以下依赖实施，内部接口名称与文件拆分属于建议：

1. 完整读取 `configuration.md#设备绑定异常的影响范围`、`obtaining-outputs.md`、`file-copy.md`、`output-cleanup.md`、`task-cancellation.md` 和相关数据库字段／事件规则。沿 selection → item → delivery/copy/read_run 及 cleanup_item → restriction → delete/query_run 追踪身份、取消和终态；原绑定核对与端口选择分开表达。
2. 先加入生产装配下的 A missing/mismatch、B 匹配、主机副本／主机源组合测试。分别证明未建交付项失败、已有未完成读取失败、完整副本成功发布、跨设备清理和空设备目录的本地清理。使用真实 SQLite 与文件协作者，设备替身采用实际 ReadDriver、DeleteDriver、StateQueryDriver 形状；由主 Agent 前台独占确认窄红。
3. 建立类型化逐项绑定失败输入。首次提交在同一个写事务内复核原文件绑定、动作／成员资格、既有拷贝及删除事实；完整保存项、交付、相关流程、保护／限制及输出聚合。只使用已有正式事件类型及正确 owner；多段事件不同 ID、一次外层分配，不嵌套 commit。某一项失败不要求立即把还有可处理项的父动作终态化。
4. 重送固定输入包含成员、源文件、原 device/driver、原因、实际本次 driver、事实时间及适用责任集合。按完整原键事件组核实，不因当前投影终态复用缺少流程段的事务。补投影阶段故障、确认回滚、COMMIT-before/after UNKNOWN→关闭重开→原键核实及改变任一有效输入的反例。
5. 将门禁接入首次资格与恢复读取、删除、查询、取消入口；清理装配支持逐成员设备端口和独立主机协作者。绑定异常分支不调用新的驱动，不循环等待配置修正；实际调用收场、文件名额与未知删除限制不得提前释放。
6. 复验原请求重送／配置修正保持旧终态，其他设备与主机工作继续；全部本地责任结束后验证正常关闭接纳和 `needs_run: false`。对状态库错误补真实 `session._drive_flows` 测试，确认后续设备流程不启动。最终 root 顺序执行 bootstrap、outputs、operations、history 相关目录，并独立核验报告逐项汇总及原副本字节。

影响范围限制为 obtain/cleanup 的生产装配、逐项流程和窄仓储，以及对应测试。`capture/handlers.py`、`capture/residual.py`、`persistence/repositories/capture.py` 由拍摄分工维护；`bootstrap/config.py`、录像 START 重试及 RC3 调用时限不在这个并行分工内。与本地取消或公共事件规则冲突时先说明实际分区及缺失定义，不自行增加错误码、放宽守卫或改变终态。

已有交付的项保持 `DELIVERY_CREATED` 和永久交付关联。绑定失败保存到原交付及相关读取流程，不复制交付错误到项的错误字段；逐项结果及动作汇总沿原交付取得。最终失败且实际读取和后续重试已经可靠结束时，同事务结束读取责任、保存交付失败、解除 `source_dependency` 并归还读取机会。此前实际已保存的字节进度、拷贝轮次及尝试结果保持不变。没有实际结束依据时保留保护及机会，不能仅凭最终失败标记释放。

已有 READ_FILE 的恢复先读取原尝试及流程，再决定本轮是否需要源设备。读取尝试跨断电继续的规则由 [file-copy.md](../../architecture/file-copy.md#按文件累计读取尝试)定义；仅有进程重新启动不生成 `read_returned`、读取错误或新的尝试次数。本会话配置固定，同一读取协程等待实际结束期间不另行扫描它来重复调用；跨会话没有结束结果的原尝试仍按下表分类。

| 原交付／副本及读取事实 | 绑定核对后的处理 |
| --- | --- |
| 交付已有失败、取消或撤回终态 | 保留交付结果；原尝试若尚未结束，继续独立收场责任，不重新发布或重开读取。 |
| 副本已完整核实，只余本地发布或撤回 | 使用原完整副本，不调用源设备，不改写为绑定失败。 |
| 尚需读取；原尝试未结束且 missing/mismatch；旧本地工作可靠收场、固定 H 包含原意图，原驱动明确声明 read 适用普通前台恢复且正式证据可用 | 按原 ticket 保存 `UNKNOWN/result_not_saved` 和 `assumed/adb_foreground_recovery/v1/{}`，不造读取错误或观察；确认原结果提交后，再完成既有局部绑定失败事务。零新调用、零新尝试。 |
| 尚需读取；原尝试未结束且 missing/mismatch；上述恢复依据任一不足 | 保留原尝试、流程、字节进度、源保护和名额，保存原 run/attempt 的 `RecoveryDiagnostic`；本轮零新设备调用，不作为 `STATE` 错误，也不以无诊断无限重扫替代会话门禁。 |
| 尚需读取；原尝试已可靠结束，原 READ 仍未终态 | matched 按原身份及本次配置继续；missing/mismatch 使用完整绑定失败事务，原尝试及次数保持。 |
| 原 READ 已终态失败，交付尚未保存相应失败 | 沿原流程错误完成其既有本地交付收场，不用新绑定错误覆盖原失败，也不把流程失败作为尚未结束尝试的停止证明。 |
| 必需身份、完整记录、查询或事务结果不可可靠解释 | 保留责任并按状态库错误结束依赖执行；不得将合法未完成与损坏事实合并。 |

消费者须在调用严格绑定失败仓储之前识别合法的未完成尝试；仓储仍拒绝没有可靠结束依据的失败事务。对应窄门禁用真实 `begin_attempt` 建立没有结果的原读取，验证消费者返回等待而不抛状态库错误，并同时证明仓储直接拒绝、所有持久化事实不变、零新调用。读取恢复需要可靠结束结果的后续扩展不得绕过上述专题规则。

取消已生效的设备清理先使用实际删除事实，绑定异常不能替代存在性结论。下面的“调用已结束”指原尝试已有正式结束结果；本会话仍有实际调用拥有者时先跟踪该调用。

| 删除事实与原责任 | 取消收场及原键核实 |
| --- | --- |
| 尚未发出删除 | 按原未发出取消解除可撤销限制，不采用绑定失败输入，不创建删除或查询尝试。 |
| 已有可靠缺席事实 | 保存原成功依据，保持原删除结果，不查询设备。 |
| 原删除之后已有可靠在场事实 | 保存 `CANCELED + file_delete_failed`，保留不可撤销限制，不查询设备。 |
| 调用已结束、效果未知、DELETE 已终态，且没有未完成 QUERY | 保存 `CANCELED + delete_unconfirmed + IRREVERSIBLE`，原文件未知及尝试结果保持。不开零尝试查询责任，也不将本次绑定核对纳入事务采用输入。原键按原成员、未知删除依据及事实时刻核实，不重算当前设备配置。 |
| 调用已结束、效果未知，且 DELETE 或 QUERY 仍未终态，需要异常绑定继续推进 | 在同一事务保存 `CANCELED + delete_unconfirmed + IRREVERSIBLE` 与原未完成流程的 `device_binding_unavailable` 失败。固定责任集合与原绑定属于此复合事务输入；原键核对完整成员、投影和流程事件组。 |
| 调用仍有实际拥有者或结束结果尚不能可靠确认 | 保留原责任、不可逆限制及未知文件事实，先跟踪实际结束；不以成员终态或配置异常结束实际调用。 |

清理的每个设备请求还须携带对应已提交 `AttemptTicket` 和本次删除／查询各自的 `timeout_s`，使受管驱动能够限制实际调用。主机成品使用独立本地协作者，不借用第一个设备的端口、证据或超时配置。装配遇到不可靠声明或未登记驱动时抛 `DeviceConfigurationError`，普通及受限会话按 `configuration_error` 结束，不把装配前提失效写成公共 missing/mismatch。

清理取消的输入与结果由成员和调用事实共同决定。`CancelCleanupItem` 的采用输入为原成员、事实时刻、错误名称及详情；错误详情中的产物身份必须是原成员指向的产物。第一次提交沿原成员状态、限制、调用和文件事实选择下表分支。重送按已保存的完整成员／产物事件组核实同一输入，不重新读取当前设备配置来改选分支；调用与文件的后续可靠事实也不改写原取消结果。

| 原成员及适用责任 | 输入核实及取消消费者结果 |
| --- | --- |
| 成员尚未发出删除，原成员为 `UNRESOLVED` 或 `PENDING_DELETE` | 输入不携带错误或详情。保存取消并按原限制是否已建立决定不建立或解除限制。必要处理可靠结束后，发起取消可成功。 |
| 已发出删除，实际调用尚未可靠结束 | 保留责任并等待真实结束，不以 `CancelCleanupItem` 的终态输入代替调用结果。 |
| 删除已结束且仍未知，没有需要绑定失败结束的开放责任 | 输入为 `delete_unconfirmed` 及原 `output_id`，保留不可逆限制和未知事实；发起取消按未知收场失败。此输入不含本次配置或 `BindingResult`。 |
| 原删除后已有可靠在场事实 | 输入为 `file_delete_failed` 及原 `output_id`，保留不可逆限制；发起取消按明确收场失败。 |
| 删除已结束且仍未知，存在需要异常绑定结束的开放责任 | 使用复合绑定失败事务，固定原成员、原文件、采用的绑定失败、完整责任集合和时间；其原键不能被省略流程输入的单项取消请求复用。目标仍为 `CANCELED`，发起取消失败。 |
| 已确认删除成功，或尚未发生删除的取消已经可靠完成 | 保留原逐项事实，发起取消仅汇总本次适用处理，不把已经成功删除解释为取消失败。 |

`TargetSettlement._settle_cleanup` 须读取成员终态及其已保存错误。`FAILED` 或携带 `delete_unconfirmed`／`file_delete_failed` 的 `CANCELED` 都表示本次必要处理未成功；没有错误的 `CANCELED` 和原 `SUCCEEDED` 可正常结束本次等待。读取或提交不可靠时停止依赖执行，不能把数据库失败写成成员业务失败。原键核实要逐项拒绝改变成员、时间、错误、详情、分支或完整组，不只比较成员编号；保存的错误和未知依据独立于目标动作的 `CANCELED` 状态。

第一次提交 `CancelCleanupItem` 还须在同一事务核对实际依据：适用 DELETE／QUERY 的原尝试都已经可靠结束；可靠缺席使用既有成功分支；删除之后可靠在场只采用 `file_delete_failed`；仍未知只采用 `delete_unconfirmed`。错误名称不是调用方代替实际依据的声明。复合绑定失败事务同样核对这些原事实，再保存固定的开放流程集合。原键重送使用首次已保存的完整组，不重新采用后来取得的文件观察。

跨会话原 DELETE／QUERY 只有意图而尚无结果时，数据库记录可以完全合法。没有可靠旧工作边界、固定 H 或原驱动适用声明时，普通及取消消费者保留原尝试、成员、不可逆限制和输出事实，零新调用并保留恢复诊断；不能将仓储拒绝错误转换成 `STATE` 或直接保存取消。双门具备时，以原 ticket 按正式恢复规则保存 UNKNOWN，再按实际删除事实完成取消或局部绑定失败。直接仓储仍拒绝没有可靠结束依据的成员终态输入。

2026-10-09，Linux 容器、Python 3.11.16：生产装配的首批绑定反例在补齐真实删除收场证据后得到 5 项有效失败、3 项保护通过。后续逐项绑定、完整副本保护、未完成读取／清理、请求 ticket 和时限共 13 项通过；读取失败的投影回滚、COMMIT 前后 UNKNOWN→重开原键核实及输入身份共 10 项通过。清理取消 11 项与读取／清理复合事务 21 项由根 Agent 独占复验通过；没有可靠原结果的读取保护及恢复诊断、设备绑定异常恢复仍按本节及[读取执行计划](2026-10-09-camctl-read-runtime-configuration.md)继续。上述窄证据不代表 RC2 或 bootstrap 全目录完成。

### RC3 预算、调用时限与真实收场使用本次配置

普通取回及录像内部输入的读取采用[文件读取的运行配置与原尝试恢复](2026-10-09-camctl-read-runtime-configuration.md)。该子计划闭合原 ticket、真实无数据计时、按文件次数、有限重拷及配置改变后的恢复；两个入口须共同通过，装配字段通过不能替代实际读取端口和历史事务证据。

录像真实 START 的输入、状态分类、恢复边界来源和分工按[录像 START 执行计划](2026-10-09-camctl-recording-start-runtime.md)落实。该子计划覆盖一次启动骨架与真实处理器接缝、原完整结果保留、确认锚点、共同提交、可靠无效果重试和有限启动核实。

**预计文件：** `capture/handlers.py::CaptureRuntime.grant`、`_control_call`、`_stop_call`、`_listing_round`、`capture/residual.py`、`outputs/obtain_flow.py`、`outputs/cleanup_flow.py`、`operations` 相关入口及装配。测试在 `integration/capture/test_recording_start.py`、`test_recording_finish.py`、`test_retry_intervals.py` 和 `integration/bootstrap` 新增配置恢复用例。

**接口与交付：** 仓储意图继续消费本次 `AttemptConfig`，新增尝试保存本次采用依据，原 `operation_runs`、尝试和拷贝轮次保持。调用时限须到达实际受管调用 `devices.adb_transport.invoke` 的 `DeviceCommand.timeout_s`／`ToolSpec.timeout_s` 或等价的有明确所有权的执行边界；不能只保存超时字段却不限制调用。调整 `ControlRequest` 等内部端口前先列出真实消费者及全部受约束替身。

内部 `ControlRequest` 携带原 `AttemptTicket` 与本次 `timeout_s`。`ManagedDeviceDriver` 接收部署提供的具体命令映射及响应解释，复用既有 `invoke` 和受管工具执行，不另写进程终止机制或添加隐藏重试。控制、停止、查询、列举、删除分别核对原操作类别和设备绑定；本次期限覆盖命令模板值。`DeviceCallResult.outcome` 保留同一完整 `CallOutcome`，兼容观察和错误视图须与其一致，不能从视图重新推定效果、收场或退出信息。

| 真实调用责任 | 请求生产者与当前接线范围 |
| --- | --- |
| 录像 START | `capture/handlers.py::_record_start_once` 沿原启动票据传递本次配置，调用完成后共同保存实际结果与确认事实。 |
| 普通 STOP | `capture/handlers.py::_stop_call` 沿原停止票据传递本次停止期限；单次请求不产生隐含重试。 |
| 执行前查询、残留停止及停止确认查询 | `capture/residual.py` 的三个请求分别消费本次 query／residual_stop 配置，原残留活动使用原驱动绑定。 |
| 产物核实、设备删除及存在性查询 | `DriverResultListing`、cleanup 适配器仍须补齐真实尝试与期限传递、完整结果保存及恢复消费者门禁。已通过配置装配测试不代表这些调用完成验收。 |
| 源端整片摘要及连续内容读取 | 沿所属独立责任处理，不套用元数据查询时限。 |

旧调用恢复还需同时满足固定初始 H、调用方提供的旧本地执行收场边界及原驱动逐操作适用声明。`build_runtime` 默认 `UNCONFIRMED`；正式 CLI `run` 依既有部署启动契约传递 `HOST_LOCAL_SETTLED`。`SessionContext.on_session_open` 在会话锁内、首次可靠打开后固定 H，先于受理及时钟事务。三个拍摄工厂只消费同一 H，不重新读取当前 C。意图在 H 之后的本会话调用保持实际管理责任。

2026-10-09，Linux 容器、Python 3.11.16：完整结果及实际期限的端口单元 6 项、控制结果保留 2 项、逐操作恢复声明 4 项通过。真实受管子进程组合 2 项验证超时及超时前启动确认保留；普通 STOP 请求集成 1 项通过。会话初始恢复回调 2 项、三个生产工厂共用恢复输入 2 项通过，CLI 边界及既有输出单元组合 24 项通过。录像真实 START 初始矩阵 12 项通过，完整事务未知提交、终态遗留调用及结果核实尚待后续门禁；上述证据不代表 RC3 或 RC5 完成。

- [ ] 添加 `test_changed_limit_preserves_used_attempts_and_terminal_runs`：第一运行已用 2 次，第二运行上限 5 时最多新增 3 次；第二运行上限 1 时无新尝试且原已用仍为 2。成功、失败、取消终态都不重开，未知结果先核实，原在途尝试不重复计数。分别覆盖启动、停止、读取、重拷、删除、查询与核实责任。
- [ ] 添加 `test_current_timeout_reaches_managed_call_and_settlement`：两次运行使用不同精确超时值，观察真实受管调用输入及保存依据；超时后继续实际收场，部分输出不刷新期限、迟到可靠效果不被抹去，停止前不开始冲突操作或复用文件。单元用受控时钟和真实接口替身，真实子进程组合放集成层。
- [ ] 添加 `test_retry_wait_restarts_with_current_interval_without_refunding_budget`：跨运行使用新间隔和新无数据阈值；同一运行内固定。等待不消耗次数，窗口、取消和调用实际结束分别决定资格。
- [ ] 先确认上述用例失败，再接入本次值及相应安全重试状态机。录像启动默认 3 次必须沿可靠无效果、未知效果核实、成功确认、窗口和调用收场完整判定下一次启动。
- [ ] 门禁：新增尝试历史精确保留本次配置，旧尝试、结果、决定与累计次数保持；回放不调用设备、不加载新配置。禁止删除旧尝试、重建流程、放宽数据库约束或隐藏重试。

### RC4 延时等待重配置与结果核实矩阵

结果列举端口、原完整结果、文件输入保存及 CLOSED 恢复的实施依据由[产物核实执行计划](2026-10-09-camctl-result-round-runtime.md)维护。该计划单列分页格式的待定事项；既有等待及局部结果核实门禁不代表多批实际调用与跨会话文件恢复已经完成。

等待接线首先验证第一版已经启用的发送后等待方式。发送成功返回时取得日期时间和单调钟锚点，在尝试结果与活动保存之前保留这两个实际读数。日期时间推导持久化的预计检查时间；本次会话共享未完成等待的单调截止，后续推进不因墙钟改变而提前完成或延长等待。重启首次推进时从原发送日期时间和本次额外等待推导剩余时间，保存适用的重配置，再以本次单调钟等待；已完成等待直接核实结果。活动按 `action_id` 关联，不能假定活动与动作的主键相同。等待完成或动作终态后释放会话缓存。

2026-10-09 的窄反例 `integration/capture/test_timelapse_wait_runtime.py`：两种重配置、墙钟前移和后移、发送返回与数据库延迟共 5 项有效失败，已完成等待保护 1 项通过。异步测试标记缺失的初始运行不计为行为反例。

**预计文件：** `capture_assembly.py::execution_wait_config`、`session_capture_assembly`、`DriverResultListing.list_files`、`capture/handlers.py::_timelapse_handler`、`_listing_round`、`_finish_timelapse_conclusion`、`TimelapseRepository.reconfigure_wait`。测试为 `integration/bootstrap/test_timelapse_finish.py`、`integration/capture/test_timelapse.py`、`test_result_confirmation.py`。

**接口与交付：** `CaptureWaitConfig` 保留原目标时长及驱动必要余量，额外等待取本次对应设备的 `capture.extra_wait_ms`。重启后的未完成等待通过原发送锚点计算并可靠保存新等待；已有完成或集合结论直接恢复收尾。结果核实使用本次 `result_check` 值，每轮多批查询不额外消耗轮数，每次查询独立遵守调用时限。

- [ ] 添加 `test_two_runs_reconfigure_unfinished_wait_from_original_send_anchor`：第一运行已保存发送和等待，第二运行增加或减少额外等待；断言发送时间不变，预计检查及采用值符合精确独立预期。重复同值无新历史，等待已完成时不重等；新配置下重建旧报告字节保持。
- [ ] 在生产装配下覆盖单文件、多文件、合法空列举、缺必需类型、未写完文件、集合尚未齐备、读取／列举错误、轮数耗尽及跨运行上调／下调。完整成功保存实际观察并释放占用；明确不满足保存已知失败且不凭时间释放；耗尽保存无法核实结论。取消及无停止能力沿已有动作契约处理。
- [ ] 先确认失败再连接等待重配置和完整结果矩阵；不得把空观察、错误或尚未齐备统一解释为成功、合法空集合或暂时无结果。驱动的文件写完、归属和集合齐备保证分别核对，不自拟设备观察。
- [ ] 门禁：核实结论、尝试结束和流程结果原子提交，重入不新增轮次或改变原结论；在结论保存后、动作终态前中断时，实际恢复结果必须与原结论一致。

### RC5 两次会话、历史与报告组合验收

**预计文件：** 建议 `integration/bootstrap/test_runtime_configuration_recovery.py`；跨组件需要真实 CLI 时放根 `tests/integration/test_camctl_configuration_recovery.py`，先阅读根测试运行说明。证据更新到本计划和已有软件验收映射，不另建重复进度页。

- [ ] 同一真实状态库先 `submit` 受理原请求，经第一运行保存未完成责任后可靠停止，再修改配置并运行第二次；组合两台设备、跨设备取回／清理、完整主机副本、取消和报告维护。时钟通过既有可注入端口控制，不等待真实拍摄时长。
- [ ] 在各次采用新配置、保存判断、开始调用、结果保存及取消提交前后使用明确同步点注入中断；恢复原完整事务及原身份，验证没有重复副作用、重复名额或遗漏独立收场。
- [ ] 对配置更新前后分别冻结完整 H，以初始回放、快照正向、当前投影逆向恢复对照独立预期；旧报告重建字节一致，当前报告表达实际逐项失败／成功及原绑定事实，不新增无需求依据的内部过程字段。
- [ ] 逐目录顺序执行受影响组件集成，再运行根配置组合；源码导入图和 AST（抽象语法树）检查验证执行入口实际消费统一核对及配置接口，不能只搜索函数名称。
- [ ] 最终独立审阅全部共享不变量的入口：普通拍摄、取消停止、恢复核实、后续残留收场、普通取回、内部输入拷贝、设备及主机清理、结果核实、报告。记录实际环境、命令、结果与尚未裁决字段，只有全部已确认要求得到行为证据才更新对应覆盖结论。

## 验收执行方式

各命令从仓库根运行。先使用最窄用例证伪，再按下列目录顺序完整执行；每次等待前一个 pytest 进程结束，不合并多个集成目录。

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/bootstrap -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/outputs -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/devices -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/session -q
```

若 RC3 修改受管调用或仓储，再单独执行相应 `operations`、`persistence`、`history` 目录；RC5 新增根测试后单独执行该文件。文档核验运行 `node scripts/check-doc-links.mjs` 与 `git diff --check`，并核对新增链接锚点。文档检查不作为生产行为验证。

## 状态与剩余范围

2026-10-09，Linux 容器、Python 3.11.16：已明确字段的加载校验与发送后等待接线已有窄门禁。配置加载相关单元 133 项通过；等待装配与结果列举单元 14 项通过；真实等待处理 6 项通过；capture 组件集成 259 项通过。录像启动、停止和结果核实的当前预算装配及启动授予保存另有 7 项单元通过；这项结果只验证配置交付，不证明安全重试或实际调用计时。日常入口绑定、逐设备失败、录像调用预算与期限、完整两次会话组合尚未收齐门禁，按 RC0—RC5 继续实施。软件组合使用受契约约束的设备替身，真实设备、目标 ARM64 性能与物理断电单独验收。

## Task 1: 清理重试阶段与本次额度

本任务落实 RC3 中已有的删除、查询重试及配置变化契约，不增加第一版业务范围。正式依据为[设备清理调用](../../architecture/configuration.md#设备文件删除与查询的计时)。

**预计文件：** 修改 `apps/camctl/src/camctl/outputs/cleanup_flow.py`；新增组件集成 `apps/camctl/tests/integration/outputs/test_cleanup_retry_phases.py`。沿用 `AttemptConfig`、`RetryWaitGate` 和已有尝试意图顺序，不改变驱动接口、状态库格式或流程身份。

**输入与输出：** `delete_source_file(runtime, item_id)` 消费本次删除／查询配置、成员和目标产物的可靠事实，返回已有 `CleanupStep`。等待期间不新增尝试；具备资格后沿原流程累计次数；达到本次上限立即进入既有耗尽事务。新尝试保存本次配置，已有历史保持原值。

以下分类在已有终态、取消、绑定、在途调用及可靠保存资格判定之后适用。查询和删除预算分别判断；删除预算耗尽不跳过未知删除的必要核实。

| 可靠事实与需要执行的操作 | 时间门槛与结果 |
| --- | --- |
| 尚无删除尝试，需要首次删除 | 不等待重试间隔。 |
| 新删除之后尚无本成员核实尝试，需要首次查询 | 根据保存的意图顺序识别本次核实，清除旧查询时间锚点，不等待上一轮查询间隔；原查询次数保持。 |
| 本次查询没有可靠在场结论，需要再次查询，且本次预算有余 | 保存结果后建立查询时间锚点；跨运行从本次首次具备资格时按本次间隔重新计时。 |
| 查询可靠确认文件仍在，允许再次删除 | 必要查询返回并可靠保存后建立删除时间锚点；查询耗时不计入删除间隔。原查询流程仍允许后续删除后的新核实。 |
| 对应累计次数达到或超过本次上限 | 不等待时间门槛，交给意图事务按本次上限拒绝，并保存所属耗尽结论。 |
| 删除可靠完成或查询可靠确认缺席 | 沿已有成员终态和伴随流程收场，不新增重试。 |

**实施步骤与验收：** 以下命令从仓库根执行，解释器为已核实的 Python 3.11.16。所有 pytest 前台独占、按目录顺序运行。

- [x] 先写查询耗时 5 秒／删除间隔 3 秒、新删除后的首次核实、同库重开后上限由 1 调为 3 或由 3 调为 1 的 6 项反例。运行 `PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/outputs/test_cleanup_retry_phases.py -q`，结果为 6 项行为断言失败；诊断见 `/tmp/camctl-goal-cleanup-phases-red.log`。
- [x] `_retry_wait_step(runtime, responsibility, config: AttemptConfig, phase)` 使用本次上限和间隔；首次核实从既有意图顺序识别，未知删除不提前启动删除间隔；可靠在场后再建立删除锚点。保留流程的 `retry_wait_required`，不能以清除该标志使后续核实失去资格。
- [x] 同命令运行新增文件，预期 6 项通过；核对原流程身份、累计次数和不可变历史前缀。
- [x] 顺序运行 `integration/outputs` 全目录、`integration/bootstrap/test_cleanup_flow.py` 和 `tests/unit`，命令统一为 `PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest <路径> -q`。预期受影响组件与单元门禁通过；任何失败单独定位、记录，不能把旧的其他目录失败当作通过。全目录存在下述 5 项基线失败，本项只记录检查已执行。
- [x] 独立审阅本阶段变更、必要核实前后的时间门槛、配置上调／下调、共享产物事实及取消入口。运行 `node scripts/check-doc-links.mjs` 和 `git diff --check`，记录实际范围与未完成事项，然后按用户授权统一提交本阶段本地变更。

**执行优先级：** 用户已授权持续实现和阶段性统一提交；不重复要求实施方法或提交许可。仓库的按目录独占测试规则优先于 skill 的单进程全仓测试建议，根测试规范的受约束替身策略优先于 skill 的默认真实协作者偏好。验收只证明本任务，不表示 RC3、RC5 或第一版整体完成。

### Task 1 验证记录与剩余责任

2026-10-09，Linux 容器、Python 3.11.16。新增反例为 6 项有效失败，实施后 6 项通过（0.79 秒）；`integration/outputs` 为 1831 项通过、5 项失败、2 项跳过（176.86 秒）；默认生产装配的清理组合为 7 项通过（6.97 秒）；全部单元为 3925 项通过、1 项跳过及 2 项既有异步标记警告（9.33 秒）。本地文件链接 3569 项通过；新增清理规格链接的标题锚点已单独核对。诊断分别保存在 `/tmp/camctl-goal-cleanup-phases-{green,outputs,bootstrap,unit-final,docs}.log`。

输出目录的失败节点如下。用提交 `89fb0b4` 的独立源码副本运行这些节点，同样得到 5 项失败（0.88 秒），节点和异常断言逐项一致；诊断为 `/tmp/camctl-goal-cleanup-phases-baseline-read.log`。不能据此把输出全目录记为通过。

- `test_copy_complete.py::test_read_budget_independent_from_recopy`：读取耗尽事务拒绝缺少原明确失败的输入。
- `test_copy_resume.py::test_failed_attempt_requires_new_legal_attempt[delivery]` 及 `[internal]`：同样的读取耗尽输入被拒绝。
- `test_copy_segments.py::test_resume_read_guard_rejects_non_read_flow[delivery]` 及 `[internal]`：实际首个校验阶段为 `CONFIGURE`，用例预期 `RESUME_READ`。

独立只读审阅未发现本阶段新增的严重缺陷或主要缺陷。删除意图按同一产物全部成员读取，查询意图和预算仍归本成员；这是已有产物共享规则的实现方式。直接组合“另一成员执行新删除，原成员立即核实”的测试尚缺，作为非阻断覆盖建议保留；六项新增用例不宣称覆盖这一组合。

原有 `_run_query` 在查询结果事务未得到 `COMPLETED` 时仍返回设备观察；三个消费者 `_verify_before_delete`、`_verify_after_delete`、`_settle_canceling_member` 可能继续推进。这项可靠保存责任仍需闭合，本任务只在保存完成时更新计时锚点，不声明该问题得到修复。后续需先以在场／缺席／未知观察及保存完成／拒绝／结果未知的矩阵证伪，再保留原完整保存输入和票据，不能把保存失败解释为普通查询未知。

**阶段裁决：** 依用户的统一提交授权提交本阶段，同时明确保留基线读取失败、原查询保存责任及跨成员测试缺口。代价是第一版整体和 RC3／RC5 仍不可声明通过；本任务的清理计时证据不能替代两次真实 CLI 会话、历史回放及报告组合验收。完整第一版范围由有效业务规格确定，新增缺陷的实施拆分不增加完成度的分母。

2026-10-10，读取预算与续传测试的前置、事件定位及实际消费者门禁已完成，验证范围见[读取计划记录](2026-10-09-camctl-read-runtime-configuration.md#读取预算与续传事务的验证记录2026-10-10)。上述五项失败不再属于当前未完成项。查询结果保存责任及跨成员组合仍分别待闭合，RC3／RC5 的完整门禁保持未完成。

2026-10-11，查询原结果持有、可靠保存门槛、fresh 连接重送、成员和伴随结果续接、持久化原观察恢复及集合恢复顺序已进入阶段交付，实际范围与剩余任务见[清理查询阶段记录](2026-10-10-camctl-cleanup-query-result-closure.md#阶段记录2026-10-11)。相关清理组合 91 项通过。QUERY-only 取消与共同建档、成员和伴随仓储的完整原键证明、专项历史回放及 RC3／RC5 的其余门禁仍未完成。本阶段验证、提交后停止软件推进；接续任务为客户端分支合并及 Action6 真机演示恢复。
