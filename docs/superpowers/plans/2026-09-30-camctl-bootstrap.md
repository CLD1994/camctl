# camctl 命令入口与组件装配模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 提供可安装的 init、describe、submit、run 入口，统一配置、资源装配和最终结果通道。

**组织建议：** cli 只解析及编码命令结果，bootstrap 按命令创建必要协作者；初始化、受理和会话仍由各自用例负责。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 argparse、tomllib、importlib.resources、uv 和组件 pyproject.toml；异步资源通过 asyncio 生命周期组织。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [CLI 命令](../../architecture/cli-commands.md)、[显式初始化](../../architecture/initialization.md)、[本地配置](../../architecture/configuration.md)、[仓库边界](../../architecture/repository-layout.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

只有 init 创建状态库；run、submit 对缺失或无效库报状态库错误。describe 不打开业务库、不加会话锁、不连接设备，默认空设备目录输出完整能力文档。run/submit 的 stdout 仅有一条 UTF-8 JSON 最终结果和换行，正常结果退出 0，会话 error 退出 1；业务拒绝不等于会话失败。配置每次启动加载、运行内固定，不能改写旧动作身份、次数和终态。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

入口的数据模型与业务模型分开。Command 只保存命令名、config 路径和可选输入路径；输出编码器不能接收任意日志记录。

| 类型 | 字段或含义 |
| --- | --- |
| `Command` | 命令类型与命令允许的选项，按 CLI 契约校验。 |
| `ConfigSnapshot` | 经默认值及文件覆盖校验的不可变本次配置，含定位、执行、历史和日志子配置。默认值只在所属权威定义维护。 |
| `SessionMessage / DescribeDocument / InitResult` | 分别采用对应命令的机器结构；submit 成功必须携带布尔 needs_run。 |
| `RuntimeDeps` | 按命令创建的时钟、仓储、锁、设备目录、文件与日志接口及关闭句柄；不跨进程传递连接或锁句柄。 |

命令结果按执行职责分类。

| 情况 | 返回与输出 |
| --- | --- |
| run/submit 完成职责，含已保存业务拒绝 | succeeded，退出 0；submit 包含 needs_run。 |
| 会话前提或职责失败 | error，退出 1；不输出成功接管判断。 |
| describe 完整生成并成功写 stdout | 能力文档及换行，退出 0。 |
| describe 生成前失败 | stdout 无能力文档，退出 1。 |
| stdout 写入失败 | 保留实际失败，不宣称完整输出；诊断仅使用可用 stderr。 |

init 结果完全采用初始化专题，不套用会话封装。参数语法错误不能保留 argparse 默认退出码而违背命令契约。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/cli.py` | 解析、命令分派及最终 stdout。 |
| `apps/camctl/src/camctl/bootstrap/config.py` | TOML 覆盖、严格校验和本次配置。 |
| `apps/camctl/src/camctl/resources.py` | 从发行包定位权威生成资源。 |
| `apps/camctl/src/camctl/bootstrap/application.py` | 按命令装配实际协作者。 |
| `apps/camctl/src/camctl/bootstrap/lifecycle.py` | 创建与有序关闭句柄。 |
| `apps/camctl/src/camctl/persistence/initialization.py` | 显式建库和已存在库验证。 |
| `apps/camctl/src/camctl/bootstrap/clocks.py` | 标准库实际 UTC 和单调时钟适配，只由装配创建。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享类型](2026-09-30-camctl-contracts.md) | B2、B3 使用 K1—K3；B1 先提供测试环境。 |
| [持久化](2026-09-30-camctl-persistence.md) | B4 使用 P1 的运行库及格式检查。 |
| [驱动](2026-09-30-camctl-devices.md) | B5 使用 D1 的静态能力目录。 |
| [受理与会话](2026-09-30-camctl-acceptance.md) | B6 连接 A4 和会话 S2—S5；命令不能自行执行业务。 |
| [日志](2026-09-30-camctl-logging.md) | B6 管理 L2、L6；B7 验证独立安装。 |

B1 建立包后才运行各模块命令。B2、B3 可先用端口替身实施，B4、B5 待依赖完成组合。B6 随阶段增加实际协作者，B7 在全部软件能力接入后验收最终发行物。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| describe 在无状态库时仍能导出。 | B5，`test_describe_without_database` |
| 已有库不会被 init 覆盖。 | B4，`test_init_preserves_existing_database` |
| 部分配置覆盖后仍检查组合。 | B2，`test_partial_override_checks_combination` |
| 日志与工具输出不进入最终 stdout。 | B3，`test_result_channel_is_single_json` |
| 安装后不再依赖仓库资源路径。 | B7，`test_distribution_works_outside_repository` |

## 实施任务

### B1 可安装包与资源来源

**预计文件：** `apps/camctl/src/camctl/resources.py`；测试为 `apps/camctl/tests/integration/bootstrap/test_package.py`。

**接口与依赖：** 提供 `resource_bytes(name: ResourceName) -> bytes`；ResourceName 从构建资源目录生成，不能由用户任意路径访问。前置交付：现有仓库结构与权威资源。

- [x] 编写失败用例。在 `test_wheel_contains_authoritative_resources` 中构建并在临时环境安装包，断言 CLI 入口存在且包资源与本次权威输入的摘要相同；`assert installed_schema == source_schema`。生产运行时从包资源读取，测试不把仓库存在当作安装资源证明。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_package.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。建立 `apps/camctl/pyproject.toml`、`uv.lock`、`src/camctl`、unit/integration 目录和 test 依赖组；用明确的构建步骤复制或生成 protocol、规格 SQL 及内部登记资源。选择能满足 Python 3.11 和所需包数据的成熟构建后端，说明实际包数据规则，不自行编写安装器。
- [x] 再运行上述命令，要求全部 PASS，并核对 独立包可以导入和定位资源，生产与测试依赖分开。
- [x] 审阅实际接口、状态分区及失败路径，检查 是否另行手工维护 Schema、依赖、枚举或事件全清单；记录门禁证据，建议以“build: 建立 camctl 包与权威资源构建”形成独立提交。

### B2 本地配置的精确覆盖与冻结

**预计文件：** `apps/camctl/src/camctl/bootstrap/config.py`；测试为 `apps/camctl/tests/unit/bootstrap/test_configuration.py` 和 `apps/camctl/tests/integration/bootstrap/test_configuration.py`。

**接口与依赖：** 提供 `load_config(document: JsonValue | None, defaults: ConfigDefaults) -> ConfigSnapshot`；文件读取在 bootstrap 适配器完成，不在此纯函数中执行。前置交付：B1、K1、K2。

- [x] 编写失败用例。建立 `test_partial_override_checks_combination`，只覆盖一个日志水位造成 L≥H，断言配置错误；对 bool 次数、0 容量、非有限秒数、非法容量单位分别拒绝。`assert cfg.copy.segment_size_bytes == 134217728` 验证既定 128 MiB 默认适配。原动作模型作为独立输入，断言加载配置没有修改其次数和结果。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/bootstrap/test_configuration.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。通过 tomllib 的 Decimal 数字读取，逐字段覆盖后校验完整组合；配置默认值和合法范围从现有专题落实到单一定义。目录切换按持久化绑定条件另由仓储验证，不由字符串比较决定。
- [x] 再运行上述命令，要求全部 PASS，并核对 本次配置不可被后续流程修改，省略与显式非法值不混用。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_configuration.py -q`，读取真实 TOML 并组合目录绑定仓储，覆盖目录有残留、无残留、检查失败及共同保存。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有配置消费者是否使用同一个 ConfigSnapshot，是否错误重算旧事实；记录门禁证据，建议以“feat: 实现本地配置加载与冻结”形成独立提交。

### B3 命令解析与机器结果编码

**预计文件：** `apps/camctl/src/camctl/cli.py`；测试为 `apps/camctl/tests/unit/bootstrap/test_command_output.py` 和 `apps/camctl/tests/integration/bootstrap/test_command_output.py`。

**接口与依赖：** 提供 `parse_command(argv: Sequence[str]) -> Command`、`encode_session_result(message: SessionMessage) -> bytes`。前置交付：B1、K2；session 拥有的 SessionOutcome 契约。

- [x] 编写失败用例。建立 `test_result_channel_is_single_json`，编码 succeeded 和 error，`assert output.endswith(b'\n')`，`assert output.count(b'\n') == 1`；特殊字符按 JSON 转义。submit 成功缺少 needs_run 必须拒绝。参数错误断言退出契约为 1，而非默认 2；日志输出使用独立替身。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/bootstrap/test_command_output.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。适配 argparse 的错误行为；业务输入拒绝与命令语法错误分开。所有正常路径只调用一次最终结果输出，设备 stdout 由受管调用捕获。
- [x] 再运行上述命令，要求全部 PASS，并核对 机器字段、退出码及输出边界符合每个命令的契约。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_command_output.py -q`，用真实 CLI 管道验证分段读取、stderr、输出错误及异常终止，消息到达后仍等待实际退出。
- [x] 审阅实际接口、状态分区及失败路径，检查 错误处理是否把诊断追加到 stdout，是否重复输出结果；记录门禁证据，建议以“feat: 实现 CLI 解析与结果通道”形成独立提交。

### B4 显式初始化与既有库验证

**预计文件：** `apps/camctl/src/camctl/persistence/initialization.py`；测试为 `apps/camctl/tests/integration/bootstrap/test_initialization.py`。

**接口与依赖：** 提供 `initialize_state(config: ConfigSnapshot, locks: SessionLocks) -> InitResult`；日常连接只通过 P1.open_existing。前置交付：P1、S2 的会话锁接口，B2。

- [x] 编写失败用例。建立 `test_init_preserves_existing_database`，对已有有效库反复 init，`assert image_after == image_before`，独立核对数据库身份、全部当前事实和历史不变；不要求 WAL 检查或检查点前后的物理文件逐字节相同。已有无效文件不得覆盖。缺失目标、目录同步失败、两个 init 竞争、run 与 init 竞争、目录绑定不符分别验证。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_initialization.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。在同一会话锁下可靠确认存在性，仅显式 init 建立完整结构与唯一初始状态；遵守初始化文件及目录落盘顺序，无虚构业务事件。
- [x] 再运行上述命令，要求全部 PASS，并核对 失败后不会出现被日常入口当作空库使用的半份数据库。
- [x] 审阅实际接口、状态分区及失败路径，检查 建库和有效性检查的所有入口是否使用存在性默认或自动修复；记录门禁证据，建议以“feat: 实现显式状态库初始化”形成独立提交。

### B5 静态能力导出

**预计文件：** `apps/camctl/src/camctl/cli.py`、`apps/camctl/src/camctl/bootstrap/application.py`；测试为 `apps/camctl/tests/unit/bootstrap/test_describe.py` 和 `apps/camctl/tests/integration/bootstrap/test_describe.py`。

**接口与依赖：** 提供 `describe(config: ConfigSnapshot, catalog: CapabilityCatalog) -> DescribeDocument`，完整校验并序列化后交给 stdout。前置交付：D1、K2、B2、B3。

- [x] 编写失败用例。建立 `test_describe_without_database`，设备目录空且 DB、锁和设备连接替身全部拒绝调用，`assert document == {'devices': []}`；配置及驱动定义无效不能导出部分说明。序列化前失败断言 stdout 尚未写入。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/bootstrap/test_describe.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。只装配静态能力目录，共用 D1 的参数 Schema、默认值与引用定义；避免创建仓储、会话或调度器。
- [x] 再运行上述命令，要求全部 PASS，并核对 完整能力文档通过公共 Schema，错误没有变成空设备目录。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_describe.py -q`，在无状态库、无真实设备的独立进程导出，检查退出 0、完整 JSON、换行及没有创建业务文件。
- [x] 审阅实际接口、状态分区及失败路径，检查 describe 的资源装配是否隐式打开数据库或日志副本交付；记录门禁证据，建议以“feat: 实现静态设备能力导出”形成独立提交。

### B6 按命令装配与资源关闭

**预计文件：** `apps/camctl/src/camctl/bootstrap/application.py`、`apps/camctl/src/camctl/bootstrap/lifecycle.py`、`apps/camctl/src/camctl/cli.py`、`apps/camctl/src/camctl/bootstrap/clocks.py`；测试为 `apps/camctl/tests/unit/bootstrap/test_composition.py` 和 `apps/camctl/tests/integration/bootstrap/test_composition.py`。

**接口与依赖：** 提供 `build_runtime(command: Command, config: ConfigSnapshot) -> RuntimeDeps`、`execute_command(command: Command, deps: RuntimeDeps) -> CommandResult`、`close_runtime(deps: RuntimeDeps) -> None`；CommandResult 是各命令结果联合类型。前置交付：A4、S2—S5、L2/L6；每阶段所需实际模块。

- [x] 编写失败用例。在 `test_submit_never_dispatches_device` 中执行 submit，`assert device_calls == []`；初始化中途失败只关闭已创建资源，重复关闭不重复处理。取消主等待后断言未完成 DB、文件或报告责任由 S5 接手，不能直接关闭它仍使用的资源。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/bootstrap/test_composition.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。按命令和阶段创建协作者，流程只依赖端口；关闭次序为停止新工作、接手实际结果、完成必要收场、关闭报告通信和数据库、结束日志生产并关闭日志，最后输出及释放会话句柄。
- [x] 再运行上述命令，要求全部 PASS，并核对 没有全局数据库、驱动或配置单例，锁与连接不泄漏到工具子进程。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_composition.py -q`，组合真实装配执行 init/describe/submit/run，注入每种资源初始化及关闭失败并核对实际结果。
- [x] 审阅实际接口、状态分区及失败路径，检查 所有初始化失败和 finally 分支是否仍可能丢弃实际任务；记录门禁证据，建议以“feat: 组装 CLI 用例与运行资源”形成独立提交。

### B7 发行物与部署检查

**预计文件：** `apps/camctl/src/camctl/resources.py`、`apps/camctl/src/camctl/bootstrap/application.py`；测试为 `apps/camctl/tests/integration/bootstrap/test_distribution.py`。

**接口与依赖：** 验证 installed camctl 入口、包资源、版本及外部工具检查；沿用模块公共接口。前置交付：I6、B1—B6 及其他模块的软件门禁；不以 B7 自身完成作为前置。

- [ ] 编写失败用例。建立 `test_distribution_works_outside_repository`，在仓库之外安装正式发行物，`assert exit_code == 0` 验证 init、describe、submit 和设备替身 run；移除源码目录可见性后仍可读取所需资源。缺失资源与不符合 SQLite 条件的解释器应明确失败。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/bootstrap/test_distribution.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。补齐构建说明、安装与运行检查文档、目标 Python/SQLite/工具条件；运行时依赖只从锁文件取得。ARM64 与真实设备结果另记，不用开发容器通过代替。
- [ ] 再运行上述命令，要求全部 PASS，并核对 发行物自包含所需权威资源且不携带测试依赖，部署待核验事项明确。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有资源定位是否仍依赖当前 cwd 或源码路径；记录门禁证据，建议以“build: 验证独立发行与部署检查”形成独立提交。

## 模块完成门禁

B1—B6 通过对应组件门禁；所有软件模块接入后 B7 在仓库外安装通过。实际 ARM64 依赖、工具和设备仍按部署清单核验，计划不承诺开发环境测量能够证明目标性能。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
