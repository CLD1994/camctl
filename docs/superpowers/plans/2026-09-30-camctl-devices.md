# camctl 设备能力与驱动适配模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 提供静态能力、分类操作证据和受控读取接口，使业务模块能够按设备实际能力执行及恢复。

**组织建议：** 驱动按控制、查询、结果列举、读取、摘要和删除分别提供端口；框架只消费类型化事实，厂商命令和响应留在具体适配器。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 复用本地参数 Schema、标准库类型、O3 受管调用及既定 ADB 范围；不新增 MCU 实际通信。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [能力与边界](../../architecture/camera-capabilities.md)、[设备及文件接口](../../camctl/file-runtime.md)、[现有相机参数](../../architecture/camera-parameters.md)、[设备接入证据](../../camctl/integration-readiness.md#设备证据与联调输入)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

静态导出与受理校验共用参数权威定义。首次选定的设备、driver_id 和有效参数保存后不因新配置改写；操作前检查本次绑定，绑定错误只影响相应任务及收场。设备不统一要求查询或停止。发送、启动、采集完成、文件归属、单文件完成和集合齐备各有明确保证范围；驱动不写业务数据库、控制重试次数或决定动作终态。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

参数和能力接口可先完成软件实施。具体厂商的响应映射必须取得接口证据后才作为正式驱动实现；软件集成用受下表端口约束的替身，不编造设备命令。

| 类型 | 字段或含义 |
| --- | --- |
| `CapabilityCatalog / ParameterDefinition` | 设备、驱动、动作能力、参数类型、Draft 2020-12 Schema、默认值、完成方式、兼容性及必要余量的同源定义。 |
| `DeviceBinding / BindingResult` | 已保存 device_id、driver_id 和本次配置；匹配、配置缺失、驱动不一致或不可可靠读取分别表达。 |
| `EvidenceRegistry / DeviceObservation` | 操作证据类型、版本、结构及保证范围；观察关联原任务或文件身份，数量受本次操作契约限制。 |
| `ControlDriver / StateQueryDriver / ResultDriver` | 明确支持的单次控制、状态查询与分批结果列举端口；能力缺失与调用失败分开。 |
| `ReadSession / ReadControl` | 绑定源身份、固定长度及偏移的连续读取端；线程读取，事件循环可请求停止并确认实际结束，不能并发消费同一个会话。 |

调用和能力分别分类。

| 能力及真实结果 | 对上层表达 |
| --- | --- |
| 明确不提供某可选能力 | 静态声明不支持，上层选择已有合法路径。 |
| 声明支持但查询/执行失败 | 返回实际错误或未知，不降级为不支持。 |
| 返回只确认发送 | 仅发送事实；锚点按该任务规定取得。 |
| 返回能确认本次启动或原生任务完成 | 只保存对应保证及实际计时依据。 |
| 已得可靠事实，随后通信异常 | 事实与错误同时返回。 |
| 证据无法解释或版本未知 | 明确错误，不能推定空文件、已停止或无效果。 |

记录归属、文件写完及集合齐备必须分别有依据。能力变化不重新解释原动作和旧报告。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/devices/catalog.py` | 静态能力及参数定义。 |
| `apps/camctl/src/camctl/devices/bindings.py` | 原绑定与本次配置的核对。 |
| `apps/camctl/src/camctl/devices/ports.py` | 按能力划分的公共端口。 |
| `apps/camctl/src/camctl/devices/evidence.py` | 证据类型、版本及校验单一来源。 |
| `apps/camctl/src/camctl/devices/adb_transport.py` | 一次受管调用与实际原始响应。 |
| `apps/camctl/src/camctl/devices/read_session.py` | 线程读取与独立停止控制。 |
| `apps/camctl/src/camctl/devices/drivers/` | 取得实际接口证据后的厂商映射。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享类型](2026-09-30-camctl-contracts.md) | K1/K2 精确 Schema 值与单位。 |
| [操作](2026-09-30-camctl-operations.md) | O1/O3 提供单次调用、收场和返回信息。 |
| [文件](2026-09-30-camctl-host-files.md) | F3 在线程消费 ReadSession。 |
| [采集与产物](2026-09-30-camctl-capture.md) | C1/C6、X4/X8 验证每种能力实际消费者。 |

D1/D2 的端口、静态定义及证据结构先完成，使其他模块可以用受约束替身推进。D3/D4 的通用执行与读取接口属于软件范围；具体厂商命令、二进制通道和响应映射在取得外部证据后接入 D5，设备实测不是软件集成前提。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 不支持状态查询不应自动成为失败。 | D2，`test_absent_query_is_declared_capability` |
| Schema 默认值不会修改原输入。 | D1，`test_defaults_preserve_raw_input` |
| 来源动作终态后仍使用其原绑定。 | D2，`test_terminal_source_keeps_binding` |
| 读取控制不阻止必要录像停止。 | D4，`test_read_control_allows_stop` |
| 声明支持但摘要失败不能跳过。 | D3，`test_failed_hash_is_not_unsupported` |

## 实施任务

### D1 同源静态能力与参数规则

**预计文件：** `apps/camctl/src/camctl/devices/catalog.py`；测试为 `apps/camctl/tests/unit/devices/test_catalog.py` 和 `apps/camctl/tests/integration/devices/test_catalog.py`。

**接口与依赖：** 提供 `build_catalog(config: ConfigSnapshot, definitions: DriverDefinitions) -> CapabilityCatalog`、`apply_defaults(raw: JsonValue, definition: ParameterDefinition) -> EffectiveParams`；DriverDefinitions 是已部署驱动单一静态来源。前置交付：K1/K2、B2。

- [x] 编写失败用例。建立 `test_defaults_preserve_raw_input`，省略合法可默认字段，`assert raw == original` 且有效参数包含规定默认值；显式 null、非法值不被默认覆盖。缺版本或引用、无效组合规则、定义缺失必须失败；空设备目录合法。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/devices/test_catalog.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。同一参数定义供导出及受理使用，包含完整 Schema 引用与必要说明；只导出已部署且实际声明能力，不把预留名称当实现。
- [x] 再运行上述命令，要求全部 PASS，并核对 静态目录无设备连接，B5/A2 使用同源规则。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/devices/test_catalog.py -q`，真实 describe、受理与公共合法/非法样例验证同源参数；客户端 Ajv 的真实消费由 I3 验证。
- [x] 审阅实际接口、状态分区及失败路径，检查 Schema、默认值与执行转换是否各自维护一份完整定义；记录门禁证据，建议以“feat: 实现同源设备能力目录”形成独立提交。

### D2 可选能力、绑定与证据类型

**预计文件：** `apps/camctl/src/camctl/devices/ports.py`、`apps/camctl/src/camctl/devices/bindings.py`、`apps/camctl/src/camctl/devices/evidence.py`；测试为 `apps/camctl/tests/unit/devices/test_evidence.py` 和 `apps/camctl/tests/integration/devices/test_evidence.py`。

**接口与依赖：** 提供 `check_binding(saved: DeviceBinding, current: ConfigSnapshot) -> BindingResult`、`validate_observation(value: DeviceObservation, contract: EvidenceContract) -> None`；EvidenceContract 定义类型、版本、操作及结构。前置交付：D1、K1/K2；先定义本模块的观察类型，O1 随后验证外层结果。

- [x] 编写失败用例。建立 `test_absent_query_is_declared_capability`，无查询能力但有时间产物完成方式，`assert declaration.query_supported is False` 且其他能力有效；建立 `test_terminal_source_keeps_binding`，取回/删除从原文件及动作读原驱动。未知证据版本、错误身份、设备缺失及驱动改变分别拒绝实际调用。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/devices/test_evidence.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。分开定义控制、停止、查询、结果、读取、摘要和删除 Protocol；证据登记同源用于生产验证与替身，有限数组数量及字段只保留必要事实。
- [x] 再运行上述命令，要求全部 PASS，并核对 无统一万能驱动接口，绑定错误不增加尝试次数。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/devices/test_evidence.py -q`，真实持久化加受约束驱动替身，覆盖拍摄、跨设备取回、清理及独立收场的绑定错误。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有设备访问是否从原关联取得绑定而非当前默认驱动；记录门禁证据，建议以“feat: 定义能力与设备证据接口”形成独立提交。

### D3 一次设备操作及原始返回适配

**预计文件：** `apps/camctl/src/camctl/devices/adb_transport.py`；测试为 `apps/camctl/tests/unit/devices/test_transport.py` 和 `apps/camctl/tests/integration/devices/test_transport.py`。

**接口与依赖：** 提供异步 `invoke(command: DeviceCommand, call: AttemptTicket, transport: ManagedTransport) -> CallOutcome`；DeviceCommand 是具体驱动已经确认的单次操作，ManagedTransport 采用 O3。前置交付：D2、O3；具体命令必须来自已核验驱动定义。

- [x] 编写失败用例。建立 `test_failed_hash_is_not_unsupported`，声明源摘要能力而本次调用失败，`assert outcome.error is not None` 且能力保持支持；发送成功、启动成功、任务完成后返回、明确拒绝、超时及成功后错误分别符合保证范围。`assert implicit_retries == 0`。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/devices/test_transport.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。调用一次明确操作并交出类型化事实，业务预算及重试留给流程；共享 ADB 服务端启动等待包含在本次调用期限，不恢复服务端后暗自重发业务命令。
- [x] 再运行上述命令，要求全部 PASS，并核对 本地退出和原始文本不会直接生成设备成功事实。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/devices/test_transport.py -q`，用真实受管本地工具和受接口约束的响应源验证期限、退出及分类。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有隐藏重试、异常默认和 stdout 解析分支；记录门禁证据，建议以“feat: 接入单次设备调用适配”形成独立提交。

### D4 源读取会话与独立停止控制

**预计文件：** `apps/camctl/src/camctl/devices/read_session.py`、`apps/camctl/src/camctl/devices/ports.py`；测试为 `apps/camctl/tests/unit/devices/test_read_session.py` 和 `apps/camctl/tests/integration/devices/test_read_session.py`。

**接口与依赖：** 提供异步 `open_read(source: SourceFile, offset: int, ticket: AttemptTicket) -> ReadSession`；会话同步 `read_chunk(limit: int) -> ReadChunk`、线程安全 `request_stop() -> None`、异步 `wait_stopped() -> ReadEnd`；ReadChunk 含字节或明确 EOF/错误，ReadEnd 为实际停止证据。前置交付：D2/D3、O3；SourceFile 含原身份、定位及固定长度。

- [x] 编写失败用例。建立 `test_read_control_allows_stop`，线程正等待源数据，事件循环可发出读取停止及必要录像停止；`assert read_resources_closed is False` 直到读取实际结束。连续数据及时重置无数据计时，设备日志不能重置；同会话并发 read_chunk 拒绝，偏移及短读精确。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/devices/test_read_session.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。字节在读取端与分段线程流动，控制通道独立可用；不要求等待整段返回才能观察停止或数据。源定位信息留在驱动，主机路径另由 F1 管理。
- [x] 再运行上述命令，要求全部 PASS，并核对 线程跨段复用会话安全，实际停止前拷贝机会仍保留。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/devices/test_read_session.py -q`，真实受控流和默认线程池验证取消、无数据、偏移、必要控制及 O3 收场。
- [x] 审阅实际接口、状态分区及失败路径，检查 句柄关闭、读取错误及数据到达的所有线程边界；记录门禁证据，建议以“feat: 实现可停止的设备读取会话”形成独立提交。

### D5 驱动契约测试与实际设备接入交付

**预计文件：** `apps/camctl/src/camctl/devices/drivers/`、`apps/camctl/src/camctl/devices/evidence.py`；测试为 `apps/camctl/tests/integration/devices/test_driver_contract.py`。

**接口与依赖：** 每个具体驱动实现 D1—D4 的适用端口；软件替身也执行同一契约测试。前置交付：软件依赖 D1—D4；正式厂商映射另需接入证据清单。

- [x] 编写失败用例。建立 `test_driver_declared_evidence_matches_consumers`，按各声明向 C6、X4、X8 提供合法、错误及边界结果，`assert accepted_facts == contract_facts`；不支持接口不可被消费者调用。实际相机联调另外核对响应、归属、固定内容、读取偏移和停止保证。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/devices/test_driver_contract.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。先保存抽象接口证据及可复查输入，具体厂商映射取得证据后编写；第一版软件门禁只验真实业务消费者与受约束替身的协作。对未核验的设备设置和响应不写猜测实现，不宣称真实驱动通过。
- [x] 再运行上述命令，要求全部 PASS，并核对 软件契约和实际设备验收分别有状态与证据，MCU 不增加运行依赖。
- [x] 审阅实际接口、状态分区及失败路径，检查 全部驱动保证、返回格式和文件关系是否超出实际证据；记录门禁证据，建议以“test: 验证驱动契约与接入范围”形成独立提交。

#### D5 跨组件暴露的身份语义接缝（2026-10-07 记录，真实设备接入时统一收口）

跨组件集成（I5 并发组合）首次暴露三处独立演化的任务身份语义，软件层各自成立但未统一：

| 接缝 | 现状语义 | 依据 |
| --- | --- | --- |
| 任务标识下发 | `device_activities.task_key` 在首次派发前生成 UUID4，但 `ControlRequest` 只有 operation/binding/params，驱动无法得知任务标识；operation-fields.md“驱动支持传入任务标识时使用该值”未接入任何下发通道 | `scheduling.py` 活动创建与 `ports.py` ControlRequest |
| 确认观察身份校验 | `validate_outcome` 按 `AttemptTicket.target_id`（设备活动主键的规范十进制字符串）核对确认观察 `data.activity_id`；驱动无法自行得知该主键，进程内适配层需查库对齐 | `operations/validation.py` 与 `scheduling.py` grant 事务 |
| 结果列举回询 | `DriverResultListing` 以 `params={"activity_id": str(action_id)}`（动作主键）回询，与确认观察校验的活动主键是两个不同身份 | `capture_assembly.py` 结果端口 |

同部署内动作与活动主键递增不必然对齐（清理等非拍摄动作不创建活动，先行时会错位），单动作部署的主键重合掩盖了区分。测试替身以查库对齐模拟“适配层知道任务身份”的前提：启动控制调用读已派发待响应（dispatch_state=2）的活动行，停止调用读执行中（占用且进行中）的活动行——停止请求同样不携带任务身份，2026-10-08 组合用例暴露替身按剧本身份回退在第二个拍摄动作上必然错位，停止确认观察被生产校验拒绝（该拒绝路径的收场缺陷同日修复，见 outputs 计划 X10 收口记录）。

真实设备接入时的收口决策（须与接入证据一起定）：统一以 `task_key` 为驱动可见的任务标识（下发通道加入控制请求或绑定），确认观察与列举回询身份随之切到同一来源；或维持适配层映射并把它写成正式接入要求。在此之前三处语义保持现状，不互相冒充。

## 模块完成门禁

D1—D4 的软件接口及 D5 契约替身组合通过；每个正式厂商适配另有实际证据及目标联调结果。框架能够组合不同完成方式，不以单一相机的能力冒充通用保证。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
