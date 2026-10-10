# 软件验证与实施顺序

[设计入口](README.md) · [实现总览](implementation.md)

本页定义 camctl 实现的测试分工和跨职责验证要求。各业务专题中的决策表及验收场景仍须逐项落实。具体阶段与门禁见[第一版实施路线图](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)，接口、测试文件、失败用例和执行步骤见其引用的[模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)及[跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md)。进入代码实施前核对所需前置交付和实际代码。

## 测试与分步实施方向

### Python 环境

开发与测试遵循[目标环境与部署分工](implementation.md#目标环境与部署分工)指定的 Python 3.11。执行前使用虚拟环境解释器的 `--version` 核对版本；已有环境满足要求时直接复用，否则用 uv 创建 Python 3.11 的非项目级虚拟环境，并按组件 `pyproject.toml` 和 `uv.lock` 准备生产及测试依赖。测试使用该环境的解释器显式执行，单元与集成分别运行。

验证记录应注明解释器版本、测试范围和实际结果，使每项实施门禁都有可核对的依据。

### 分类与替身

遵守根目录 [AGENTS.md](../../AGENTS.md) 的测试规范：

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

| 责任边界 | 单元验证 | 集成验证 |
| --- | --- | --- |
| 事务与恢复 | 提交、回滚、未知、取消竞争各分区；事件前后存在性与反向顺序 | 真实 SQLite 的并发受理、提交边界中断、投影回放及固定 H 读取 |
| 索引与资源 | 分页边界、父子补齐、固定依据不漂移 | 查询计划与多规模数据；验证读取批次、临时排序及并发调度可以推进 |
| 数字适配 | 长小数、指数、布尔值、范围、倍数、相等与编码边界 | 输入、数据库、快照、回放、报告的精确往返 |
| 日志 | 水位、通知、取消和故障分类 | 真实 Janus、监听线程和独立写入进程；强制在触发错误后竞争轮换，验证副本包含该错误 |
| 报告与文件 | 身份不匹配、迟到结果、取消及资源所有权 | 真实管道、子进程、文件同步和停止；超时后临时文件不被提前复用 |

这些门禁约束后续实现。临时能力验证不计作上述生产单元或集成测试通过，设备和目标 ARM64 环境须另行验收。

## 跨模块契约检查

检查从输入和可靠保存的事实开始，沿实际操作、结果保存、失败、取消与重启恢复，直到报告或交付等可观察结果。下表定位需要组合验证的边界；状态分类和判定规则由链接中的专题定义。各模块的测试分别通过后，仍须验证整条业务流程。

| 场景 | 规则与验收入口 | 组合验证重点 |
| --- | --- | --- |
| 配置变化后继续工作 | [本地配置变化](../architecture/configuration.md#本地配置变化与未完成工作)、[设备绑定异常](../architecture/configuration.md#设备绑定异常的影响范围) | 拍摄、跨设备取回、清理、取消及独立收场核对原绑定；因绑定异常而拒绝创建调用时不消耗尝试次数。新操作采用本次配置，历史次数、原身份和终态保持，其他设备继续。 |
| 延时摄影等待与完成 | [完成核实](../architecture/camera-capture.md#设备自行结束时的等待与完成核实)、[相机验证](../architecture/camera-verification.md) | 按驱动声明区分发送与完成，不支持查询时不创建查询预算；发送锚点、跨重启等待、取消资格、文件写完与集合齐备分别验证。 |
| 普通交付移入 ready 后，结果保存前中断 | [交付恢复决策表](../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)、[产物验证](../architecture/output-verification.md) | 组合真实目录、领取模块与 SQLite，在移动、同步、保存前后中断；覆盖文件已被领取并删除、源文件已清理及取消竞争。 |
| 来源结束后，取回与清理同时具备条件 | [资格授予顺序](../architecture/outputs.md#延后执行时的取回与清理顺序)、[数据库一致性验收](database/consistency-verification.md#取回读取保护与清理) | 交换唤醒、选择保存和查询返回顺序；覆盖动作、组、计划、显式产物 ID、自动预览及重启恢复。 |
| 取消动作自身被取消 | [取消阶段与目标范围](../architecture/task-cancellation.md#取消动作自身被取消)、[取消计划](../superpowers/plans/2026-09-30-camctl-cancellation.md) | 分别验证后一次取消是否直接包含原目标，以及完整目标集合包含自身的情形；设备停止、文件结束与交付撤回责任均须保留。 |
| 取消后的完整副本与录像处理中间文件 | [取回文件生命周期](../architecture/obtaining-outputs.md#取回中间文件的保留与清理)、[录像处理文件生命周期](../architecture/camera-recovery.md#内部中间文件的保留与清理)、[清理预算](../architecture/file-handoff.md#中间文件清理的运行预算) | 在取消、实际操作结束、成品登记及删除保存前后中断；覆盖归属未知、后续仍需使用、删除失败、跨会话继续位置及同次运行重复发现。 |
| 日志错误触发副本交付 | [写入通知](logging-runtime.md#日志适配与写入完成通知)、[日志交付](../architecture/log-delivery.md)、[故障标记](../architecture/log-failure-marker.md) | 组合队列、持锁写入与复制、标记及文件交接；覆盖创建或更新标记失败、取消、遗留文件清理和状态库不可用。 |
| 业务事实保存后生成历史报告 | [历史与报告验收](database/consistency-verification.md#独立历史与报告重建)、[报告验证](../architecture/report-acceptance.md) | 比较从初始状态回放、快照正向恢复及当前投影逆向恢复；对象及关联均取自同一完整历史边界，旧报告字节保持。 |
| 公共协议跨组件读写 | [客户端适配计划](../superpowers/plans/2026-09-30-report-client-adaptation.md)、[跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md) | 能力导出、计划生成、run/submit 受理、报告生成、导入及 ACK 消费同一协议和样例；覆盖精确 ID、请求复用、时间、关联与部分结果。 |
| camctl 退出后仍有工具进程 | [主程序收场契约](../host-demo/design.md#接入模块的本地进程收场责任)、[跨组件收场任务](../superpowers/plans/2026-09-30-camctl-integration.md#i2-保留退出记录分批检查原组与最终回收) | 组合退出观察、原组终止、存活线程检查、最终回收和下一调用；成员退出、权限错误、信息不完整与解析失败分别处理。 |

具体任务及进度由[模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)跟踪；正式验收条目与实际生产入口、测试和证据的对应关系由[全量验收任务](../superpowers/plans/2026-09-30-camctl-integration.md#i6-全量契约映射软件验收与部署交接)落实。

## 源码边界审计

2026-10-08 对 `apps/camctl/src/camctl` 全部生产源码做了一次静态盘点：解析每个文件的语法树，统计模块依赖、直接外部调用入口（子进程、网络、文件系统、时钟）以及异常、默认值、跳过和清理分支的分布，再逐个入口核对所属模块的责任契约。盘点的结构结论固化为契约检查 [test_external_boundaries.py](../../apps/camctl/tests/integration/contracts/test_external_boundaries.py)：子进程唯一入口、零网络依赖、文件系统调用白名单和零裸 except 四条规则对真实源码断言，并对合成违规样本验证能够报出；该检查随每轮全量回归执行，后续新增模块若需直接外部调用，必须先扩白名单登记责任。模块依赖方向不在本节重复，由 [test_dependencies.py](../../apps/camctl/tests/integration/contracts/test_dependencies.py) 的导入图规则按[模块契约](module-contracts.md)持续检查。

外部调用入口的审计结论如下表。第一版 CLI 无网络面：生产代码不导入任何网络客户端标准库或第三方包，也不建立连接；设备的网络访问由部署侧 adb 工具承担，camctl 只经受管工具进程调用它。

| 入口类别 | 审计结论 | 责任依据 |
| --- | --- | --- |
| 子进程 | Python CLI 生产代码的受管工具唯一创建点是 `camctl.operations.process`（`asyncio.create_subprocess_exec`）；`subprocess` 模块也只有它导入，adb 传输适配经其运行接口使用子进程 | 工具继承 camctl 所属组，CLI 运行期间的正常退出、超时与取消收场由该模块承担。C 接入模块建立独立组，并负责 CLI 退出后的原组检查、保留退出记录及最终回收，软件证据见[接入模块验证记录](../host-demo/verification.md)。 |
| 网络 | 零导入、零连接建立 | CLI 部署形态本身无网络面；无服务器代码 |
| 文件系统 | 直接调用（内建 `open`、`os` 文件操作、Path 与 Traversable 方法）只出现在九个登记责任的模块：host_files（文件交接与读取）、logging_runtime（日志与副本文件）、persistence（建库、init 目录切换、回放重建）、reporting（报告生成、发布与撤下）、outputs（取回中间文件与交付读取）、session.locks（会话锁文件）、resources（包内权威资源）、acceptance.input（受理输入读取适配器）、bootstrap（装配目录准备） | 各模块对应的交接、日志副本、初始化、报告发布、取回生命周期、锁、资源与受理契约；业务流程模块经仓储连接或 host_files 端口间接访问，不直接触碰文件系统 |
| 时钟 | 真实时钟读取集中在 `bootstrap/clocks.py` 装配端口与 `devices/read_session.py` 端口内的无数据计时；业务流程经注入的 `wall_us`/`monotonic_ns` 端口取时，各装配接受显式注入 | 测试可控时间与运行假设的时间边界（完成判定、等待与超时） |

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

| 验证层 | 命令入口与结果 |
| --- | --- |
| Python 单元 | `pytest apps/camctl/tests/unit -q`：3489 通过、1 跳过。 |
| Python 组件集成 | 每个目录单独启动 pytest，顺序执行：acceptance 226、bootstrap 118、cancellation 66、capture 253、contracts 188、devices 46、history 91、host_files 86、logging_runtime 28、motor 12、operations 199、outputs 1790、persistence 106、reporting 346、scheduling 133、session 86，共 3774 通过、2 跳过。 |
| CLI 故障与恢复 | `pytest tests/integration/test_camctl_motor_recovery.py -q`：21 通过；包括 11 个保存／写入／未知结果断点、3 个通道失败、过期、4 种取消寻址、未知意图取消和受理失败原参数。 |
| CLI、host 与客户端组合 | `pytest tests/integration/test_camctl_motor_notifications.py -q`：5 通过；真实导出／回调／报告领取／导入／ACK、慢回调与下一 run、相机共存、两种中断的 host 自动恢复。 |
| 客户端 | `pnpm --dir apps/client typecheck` 通过；`test:unit` 530 通过；`test:integration` 370 通过。精确数字与派生恢复的独立审查另复验 166 项和 2 项 Chromium 场景，见[客户端验证](../client/verification.md#电机控制接入)。 |
| host | Debug、Release 各 33 项 CTest 通过，其中单元入口 11 项、集成入口 22 项；包含独立安装消费者和源码包重建，见[host 验证](../host-demo/verification.md#电机通知组件交付复验2026-10-08)。 |
| 规格与检查器 | 协议 4 份 Schema、76 个电机共同夹具；事件 34 类／110 分支；报告依赖 23 个投影／99 条映射；数据库 31 表／3178 项断言；历史 SQL 同步检查通过。Node 的协议、事件和报告依赖检查器测试共 114 通过。 |

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

| 调用与受控输入 | 实际结果 | 能够确定的范围 |
| --- | --- | --- |
| `exec-out` 执行同一脚本，分别向 stdout、stderr 输出不同标记后以 `7` 退出 | 本地退出码为 `0`，两个标记合并到 stdout，stderr 为空 | 此通道不提供本次远端退出码和独立 stderr；不能据本地 `0` 判定设备命令成功 |
| `shell -T` 执行上述脚本 | 本地退出码为 `7`，两个标记分别进入 stdout、stderr | 本次受控退出码正确传回，两个输出流分开 |
| `shell -T` 输出七个已知字节 `00 01 0a 0d 7f 80 ff`，并另输出 stderr 标记 | 本地退出码为 `0`；stdout 为 `00 01 0d 0a 0d 7f 80 ff`；stderr 标记正确 | LF 被扩展为 CRLF，原始字节比较失败；该 Windows 输出不能作为原样媒体字节 |
| `exec-out` 执行同一字节脚本 | 本地退出码为 `0`；stdout 为原七个字节与 stderr 标记的拼接；stderr 为空 | 测试字节原样保留，输出流仍然合并；不补造远端退出事实 |
| 目录分页中的 `awk`，以及单独将 `a\0b\0` 交给同一 `RS`、`ORS` 设置 | 目录帧缺少完整路径和末尾 NUL；受控输入得到 `aEND`，本地退出码均为 `0`，stderr 均为空 | 相机 `awk` 的这组 NUL 处理不满足目录帧契约；退出 `0` 不证明响应完整 |
| 将 `a\0b\0` 交给 `IFS= read -r -d ''` 循环，并逐项用 `printf` 输出 | 本地退出码为 `0`；stdout 精确为 `61 00 62 00`；stderr 为空 | 相机已有 shell 支持本次受控 NUL 记录读取；完整目录分页仍待核实 |

内置 `/mnt/media_rw/emulated/DCIM` 是实际存在的目录；当次原始 `find -print0` 输出包含七条路径（两个 MP4、两个 LRF、三个 JPG），NUL 分隔且有末尾分隔符。SD 候选目录 `/mnt/media_rw/sd/DCIM` 明确不存在，不能按可用空目录处理。所列工具均能找到，其所需选项、正式分页、文件长度、摘要和完整读取尚未核实。

上述结果不证明拍摄启动、停止、文件写完或本次产物集合齐备，也不代替 OSMO 360 II 和 ARM Linux 的核验。驱动通道、实际控制响应、固件及完整设备验收继续由[双相机实施计划 T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)跟踪。

文件工具适配验证（2026-10-10，Linux x86_64 容器，Python 3.11.16）：共享调用使用 `shell -T`，保留远端退出和独立 stderr；目录脚本通过排序中的私有游标记录定位，再按 NUL 读取路径，只有读到私有结束记录才输出 `END`。测试替身在 ADB 通道边界使用支持该 shell 接口的 Bash 执行原脚本，文件访问、受管进程及帧解析均使用生产实现。

| 软件验证范围 | 实际结果 |
| --- | --- |
| 全部单元测试 | 4263 项通过、1 项跳过；2 条既有 asyncio 标记警告 |
| devices 组件集成完整目录 | 93 项通过；覆盖六种游标分区、空目录、页容量边界、特殊文件名，以及列举、排序和记录读取失败 |
| bootstrap 的 `test_tool_cancellation.py`、`test_recording_stop.py`、`test_timelapse_finish.py` | 16 项通过；覆盖实际工具收场、录像停止与延时完成 |
| 根 `test_real_camera_demo_roundtrip.py` 与 `test_camctl_c_module_roundtrip.py` | 四条双相机软件演示链和三条原 C host 链共同运行，7 项通过 |

本次软件门禁不替代真机分页和 ARM Linux 字节核验；候选相机能力仍未启用。诊断保存在工作区忽略的 `.superpowers/sdd/2026-10-10-camctl-real-camera-demo/t9-*.log`。

## 部署交接与待核验项

第一版软件层验证的结论交给部署与联调执行：[软件验收映射](software-acceptance.md)逐条登记 163 条验收与十项契约场景的结论、证据和未核验前提，[集成计划 I6 验证记录](../superpowers/plans/2026-09-30-camctl-integration.md#i6-验证记录2026-10-08)保存全量命令执行的命令、环境与数字。两项是 [B7 发行物与部署检查](../superpowers/plans/2026-09-30-camctl-bootstrap.md#b7-发行物与部署检查)的输入；B7 在源码目录之外构建、安装发行物并验证 init、describe、submit 与设备替身 run，构建与安装步骤见[构建、安装与运行检查](implementation.md#构建安装与运行检查)。软件替身与开发环境的通过结果不写成设备或目标主机结论，下表逐项列出剩余核验的输入、执行者和通过条件。

| 核验维度 | 检查输入 | 执行者 | 通过条件 |
| --- | --- | --- | --- |
| ARM64 运行库 | 源码外发行物；目标部署指定的 Python 3.11 实际链接的 SQLite；[统一运行库要求](sqlite-runtime.md) | B7 部署验证 | 按部署验证裁决在 WSL x86 合规环境（SQLite ≥3.51.3 或精确回移，已核 cpython-3.11.17 链接 3.53.1）执行：init、run、submit 及报告子进程的入口检查通过；发行物自包含权威资源且不携带测试依赖。真实 ARM64 硬件的编译与运行差异（C 模块工具链、glibc）在目标硬件单独复验，不以 x86 通过代替。2026-10-08 已执行：[B7 验证记录](../superpowers/plans/2026-09-30-camctl-bootstrap.md#b7-验证记录2026-10-08)——源码目录外构建、按锁文件安装，init/describe/submit 与设备替身 run 及依赖边界在 WSL 合规环境与 Windows 开发机均通过；真实硬件复验未执行。 |
| 外部工具 | 部署环境提供的 `adb`、`ffprobe`、`ffmpeg`（[依赖与部署](../architecture/initialization.md#依赖与部署)，生产按裸名调用，无工具路径配置面） | B7 与设备联调 | 媒体检查、修复与修复成品取回在 WSL 全链通过（ffprobe/ffmpeg 6.1.1 已随软件验证通过）；工具缺失或调用失败时按登记错误分类失败，不崩溃也不冒充成功。`adb` 依赖真实设备，随设备映射联调。 |
| 真实设备映射 | [设备证据与联调输入](integration-readiness.md#设备证据与联调输入)六项接缝；[驱动契约](../architecture/camera-capabilities.md#驱动执行与产物边界)与 D5 驱动登记点 | 设备联调（真实相机最后） | 真实驱动定义经登记点接入后，六项接缝按各自的联调场景与通过条件逐项验证；受契约约束的软件替身结果只证明软件行为，不证明设备能力。 |
| 目标资源 | 软件层资源测量（[H7 验证记录](../superpowers/plans/2026-09-30-camctl-history.md#h7-验证记录2026-10-08windows-开发机)：有限容量执行器上并发受理与恢复）；目标环境实测安排 | B7 与设备联调 | 主进程、报告进程及受管工具进程的合计资源在目标环境实测，记录环境、负载与数据规模；开发环境数字不写成目标性能承诺，超限处置按实际运行证据评估。 |
| 物理断电 | 目标存储设备；数据库 `FULL` 同步等级与文件目录同步契约（[历史存储](history-storage.md#所有状态库写连接使用-full-同步)、[C 模块断电行](../host-demo/verification.md#仍需真实环境验收)） | 目标硬件联调 | 在目标存储上切断电源后验证文件与目录同步、重启后的实际文件位置与 camctl 恢复；普通进程终止测试不代替断电落盘结论。 |

本表各项的执行记录由对应执行者在 B7 与设备联调时单独保存；尚未执行的项不折叠进软件验收映射的已覆盖结论。

真实设备证据见[联调输入](integration-readiness.md#设备证据与联调输入)。
