# Action6 与 OSMO 360 II 录像和延时摄影实施计划

> **执行者要求：** 使用 `superpowers:executing-plans` 或 `superpowers:subagent-driven-development` 逐项实施。每个任务先运行能够证伪契约的测试，再实现、验证并提交。

**目标：** 按既有责任计划补齐双相机接入，两款相机分别完成录像和原生延时摄影，经 camctl host 提交计划，登记产物、独立取回、领取文件并生成状态报告。

**架构：** 具体驱动负责参数、命令和设备事实，公共流程负责目录基准、尝试、等待、结果保存及恢复。复用现有受管进程、读取拥有者、SQLite 历史、文件交接和 C host；先完成容器软件闭环，最后补齐实际响应并在 ARM Linux 验收。

**技术栈：** Python 3.11、uv、pytest、SQLite、JSON Schema、ADB、现有 C 接入模块。

**设计依据：** [双相机接入设计](../specs/2026-10-10-camctl-real-camera-demo-design.md)。行为责任分别见[文件归属与检查](../../architecture/camera-capture.md#本次任务的文件归属)、[录像](../../architecture/camera-recording.md)、[能力](../../architecture/camera-capabilities.md)、[取回](../../architecture/obtaining-outputs.md)和[历史格式](../../camctl/database/history-formats.md)。

## 全局约束

- 型号为 Action6 和 OSMO 360 II。容器用于开发和软件验证，Windows 用于 ADB 试验，部署及最终完整演示在 ARM Linux。
- 录像预设均为 10 秒；Action6 延时为 8 秒间隔、30 分钟、仅视频；OSMO 360 II 延时为 30 秒间隔、10 分钟、仅视频。覆盖资料中全部可用能力，不把预设作为能力限制。
- 用户保证受管范围内路径不复用、没有其他来源拍摄；camctl 阻止自身冲突拍摄。两种拍摄均使用完整目录基准，设备和相应输出范围从准备到归属及集合确定保持必要独占。
- 启动前目录读取明确失败时，动作直接失败，保留实际错误，启动尝试为 0，不增加基准重试预算。状态库保存失败和提交未知按原责任处理。
- 基准固定与启动意图保存均可靠成功后才派发；固定基准不替换。会话中断后的未固定收集遵守既有重收规则，不能将一次明确读取失败当作未完成收集继续重试。
- 目录扫描结束、设备活动结束、单文件写完和产物集合确定分别保存。等待假设不伪装成设备观察，`result_files_listed/v1` 不提供集合确定依据。
- 首次受理保存生效参数及任务契约；之后不因查询失败切换结束或完成方式。保存未知先核实原申请，保留 key、时刻、完整输入和决定，再推进依赖业务。
- 不修改客户端产品代码，不安装相机端工具；沿用公共协议与既有错误登记。必要检查由任务声明，不默认精确照片数或全面媒体解码；正常录像不为核验时长单独拷贝原片。
- 候选命令和未完成契约不得导出为可用能力。软件替身证据与设备证据分别记录；源文件保留，删除仅由明确清理动作触发。
- 文件划分、新内部类型、接口签名和提交拆分均为建议。改变建议时同步提供者、消费者和测试；已确定的行为契约及项目规则保持不变。

## 审查重点

1. 候选设置、缺少结束契约或响应解析的任务不会因登记状态标签而出现在正式能力中；由 T1、T7、T9 验证。
2. 旧文件、新目录、特殊字符、空页和末页不会丢失或混入本次产物；读取失败不会成为可靠空目录；由 T2、T3、T4、T5 验证。
3. 动作终态或活动 `ENDED` 但占用仍 `HELD` 时，冲突拍摄继续等待，其他设备和兼容读取仍能推进；由 T4、T8 验证。
4. 列举结束、部分文件或未知完成依据不会提前成功，也不会误报 `no_outputs` 或 `invalid_outputs`；由 T5、T6 验证。
5. 保存未知及恢复时，原基准、调用结果、错误、产物和完成决定不会因新绑定、配置、取消或窗口而被覆盖、漏存或重发；由 T2、T4、T5、T6、T7 验证。

## 生产入口、依赖与门禁

### 既有任务归属与接入增量

本计划编排既有模块在双相机演示中的交付顺序。下表的完成状态来自对应计划记录，不是此次重新运行测试的结论。已交付基础能力直接消费；原计划已有未完成项仍在原责任计划登记细项进度，本文复选框跟踪双相机增量及其接入门禁。

| 既有计划及状态 | 本计划消费或补齐的范围 |
| --- | --- |
| [devices D1—D4](2026-09-30-camctl-devices.md#实施任务) 的同源目录、证据/绑定、调用适配与读取会话已有完成记录；[D5](2026-09-30-camctl-devices.md#d5-驱动契约测试与实际设备接入交付) 软件契约完成，具体厂商映射另需设备证据 | T1、T3、T7、T9 只新增两款相机的参数/命令、ADB 文件适配、正式登记及实际响应，复用已交付端口和生命周期 |
| [capture C1/C2/C3/C5/C6](2026-09-30-camctl-capture.md#实施任务) 及 [scheduling Q4/Q6](2026-09-30-camctl-scheduling.md#实施任务) 已有定义、控制、等待、归属及释放的基础完成记录 | T1 固定具体任务事实；T4 将实际基准准备接入既有授予和释放；T6 补正常集合完成的消费接线，不重建录像、等待或调度模块 |
| [history H7 的验证记录](2026-09-30-camctl-history.md#h7-验证记录2026-10-08windows-开发机) 明确 `BASELINE_CHUNK` 尚无生产写入方 | T2 是基准追加、固定、范围读取和对应历史验证的实际补齐，T4 是共同启动准备的新增生产接线 |
| [产物核实与恢复](2026-10-09-camctl-result-round-runtime.md#条件模型) 已覆盖完整调用结果、原文件事实及 CLOSED 本地消费；[分页格式](2026-10-09-camctl-result-round-runtime.md#分页格式的待定事项) 和普通延时成功仍未闭合 | T5/T6 承接该计划的多批、集合依据及正常成功子集，保留已完成的原输入与文件事实保存责任 |
| [结果错误计划任务三](2026-10-09-camctl-result-errors-and-report-projection.md#任务三结果失败语义与同类入口交接) 的正式错误分区已登记，UNSATISFIED 生产与恢复反例待实施；公共耗尽错误和历史报告已有交付 | T6 依赖并落实该任务中的产物失败分区；错误细项进度仍在原计划，独立应急错误身份不列入正常演示任务 |
| [完整终态申请恢复](2026-10-09-camctl-capture-completion-save-recovery.md#task-1-四类完整申请与处理器恢复) 及其默认入口、START 外层、STOP 收尾已有完成记录；[本次配置与原绑定](2026-10-09-camctl-runtime-configuration-closure.md#任务与依赖) 定义运行时消费边界 | T2/T4/T5 对新增申请延用现有拥有者模式；T6/T7 复用既有终态恢复和绑定核对，不再次实现整套保存或配置机制 |
| [integration I5](2026-09-30-camctl-integration.md#i5-三种采集取回取消及清理的跨组件组合)、outputs 取回/交付和 [bootstrap B7](2026-09-30-camctl-bootstrap.md#b7-发行物与部署检查) 已有软件交付；B7 默认拍摄装配复验仍有待执行节点 | T7 只验证新驱动在现有发行物的登记与资源；T8 在现有 C host/取回/报告测试链添加四个具体设备场景，消费并完成适用装配复验 |
| [设备证据清单](../../camctl/integration-readiness.md#设备证据与联调输入) 与路线图部署联调已有责任 | T8 提供可执行的 Windows 采集步骤；T9 将既有设备输入和 ARM 联调落实为两款相机的实际资料及四条演示证据 |

新需求的具体增量是两款驱动及全部可用选项、两类拍摄的完整目录基准、已登记任务契约下的正常集合收尾，以及指定预设的真实演示。既有计划中未完成但与这些增量共享同一契约的事项按依赖承接，不增加同范围的平行执行目标。

### 已有生产入口与执行次序

2026-10-10 的生产入口是 `SchedulingRepository.start_action` 创建动作和活动，`CaptureRuntime.grant` 调用 `SchedulingRepository.grant_start` 保存启动意图及次数。录像经 `start_recording` 调用，延时经 `capture/handlers.py` 的控制路径调用；不存在可直接复用的 `prepare_capture`。当前 `activity_capabilities` 固定返回任务独立范围，尚无基准写入生产者；不能仅改一个枚举值而不接入完整准备流程。

`DriverResultListing.list_round(ticket, *, timeout_s)` 当前只解释一批 v1 条目。延时正常列举后继续等待，已有 `_confirm_timelapse_results` 未接入；`assess_capture_files` 尚未把集合确定作为成功条件。静态定义和运行登记均为空；`DriverEntry.status` 本身不阻止访问候选端口。

任务顺序为 T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9。T1—T3 可以独立开发，但 T4 消费三者，统一签名后再接线。门禁按交付物判断：T4 证明基准后才能启动；T6 证明正常结果闭合；T8 证明真实软件组合；T9 证明真实设备演示。

执行前核对工作区和 Python 3.11 环境。在仓库根定义以下测试命令，建议复用已有且版本匹配的环境：

```bash
P() {
  UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv" uv run --project apps/camctl --group test --python 3.11 pytest "$@"
}
```

下文使用 `P <测试路径> -q` 执行测试。所有 pytest 由主执行者前台独占、顺序运行；组件集成按目录分进程，规则见 [tests/AGENTS.md](../../../apps/camctl/tests/AGENTS.md)。跨组件先读[根集成说明](../../../tests/integration/README.md)。每步红灯必须来自目标契约，而不是导入、依赖或测试装配错误；每项提交前运行 `git diff --check`。

## T1 参数、候选命令与固定任务事实

**建议文件：** 新建 `apps/camctl/src/camctl/devices/drivers/adb_cameras/definitions.py`、`commands.py`；修改同组件的 `devices/tasks.py`、`capture/models.py`、`devices/parameter_schemas.py`、`acceptance/definitions.py`，以及必要的[执行定义登记](../../camctl/database/execution-definitions.md)。新增 `apps/camctl/tests/unit/devices/test_adb_camera_parameters.py`、`test_adb_camera_commands.py`，补充受理集成测试。

**接口：** 消费 `ActionCapability` 和 `CaptureTaskFactory.__call__(effective_params) -> CaptureTask`。建议提供 `candidate_capabilities(driver_id: str) -> tuple[ActionCapability, ...]`、`settings_for(driver_id: str, action_type: str, params: Mapping[str, JsonValue]) -> tuple[tuple[str, ...], ...]`、`start_for(driver_id: str, action_type: str, params: Mapping[str, JsonValue]) -> tuple[str, ...]`；新增名字只在驱动内部使用。`CaptureTask` 补充归属方式和非空输出范围，固定执行定义保存该声明，实际基准不写入执行定义。

- [x] **先写失败测试：** 合法参数经能力 Schema 与受理获得相同生效值；测试每个离散选项及互斥分支。录像 10 秒固定为 `target_duration_ms == 10000`，不从资料的 5—20 秒样例建立时长上限。命令预期独立取自[交接资料](../../hardware/camera-control-handoff.md)，例如 Action6 ISO 800 的 argv 末两项为 `("2a", "06")`，不能由编码器生成测试预期。
- [x] **运行红灯：** `P apps/camctl/tests/unit/devices/test_adb_camera_parameters.py apps/camctl/tests/unit/devices/test_adb_camera_commands.py -q`，确认目标映射或任务事实尚不成立。
- [x] **实现同源定义：** 精确查询已给出的完整字面量，不推导任意编码。Action6 覆盖 8K/4K30、三种 FOV、两种增稳、M/Auto、六种 ISO、固定快门/WB、三个 Auto EV、光圈及码率候选；延时逐个覆盖三组完整间隔/时长及视频、RAW、JPEG输出。OSMO 覆盖全景 8K30、M/Auto、已有曝光/码率及全部完整延时组合。手动曝光与 Auto 选项按[参数规则](../../architecture/camera-parameters.md)互斥，不作笛卡尔积扩张。
- [x] **区分候选与契约：** 划线项、未说明输出的载荷、FOV 适用范围、90 分钟缺命令、25 秒/100 分钟与 2 小时冲突保留待设备核实；不导出猜测选项。OSMO 无增稳调节或光圈命令不生成设置。Action6 未提供延时停止命令，录像停止不能自动用于延时。任务必要类别、格式和数量规则使用驱动声明，不以间隔除时长补造数量。
- [x] **运行绿灯并提交：** 上述单元测试通过；顺序运行 `P apps/camctl/tests/integration/acceptance -q`，验证冻结定义、显式值/缺省值及原请求重送。提交本项定义和测试。

建议测试中的关键断言如下；各任务的测试 fixture 提供实际输入和边界替身，不另建通用测试框架：

```python
assert recording_spec["target_duration_ms"] == 10000
assert iso_800_command[-2:] == ("2a", "06")
assert admitted_effective_params == described_valid_params
```

T1 软件验证（2026-10-10，容器，Python 3.11.16）：devices、capture、acceptance 单元目录共 930 项通过；acceptance 组件集成目录独占执行通过。候选 Schema 未登记到正常驱动目录，结束和响应契约由测试替身单独提供。实际目录准备与必要产物检查分别由 T4、T5 接线。

## T2 分批基准的保存、固定与读取

**建议文件：** 修改 `apps/camctl/src/camctl/persistence/repositories/capture.py`、`capture/models.py` 及具名守卫；新增 `apps/camctl/tests/integration/capture/test_baseline_history.py`，补充历史恢复集成用例。复用已登记的 `BASELINE_CHUNK` 三个分支。

**接口：** 建议 `CaptureRepository.append_baseline(request: BaselineChunkSave, key: OperationKey, owned: OwnedConnection) -> DbOutcome[BaselineChunkResult]`、`fix_baseline(request: BaselineFixSave, key: OperationKey, owned: OwnedConnection) -> DbOutcome[BaselineRef]`、`read_baseline(ref: BaselineRef, cursor: int | None, batch: int) -> Page[BaselineChunk, int]`。`BaselineChunkSave` 保存活动 ID、批号、1—128 个身份及原时刻；`BaselineChunkResult` 返回原活动、批号和实际事件 ID；`BaselineChunk` 是一个完整历史批次，含事件 ID、批号和身份项，读取游标为事件 ID，batch 限定批次数，不在同一事件的成员中间截页。`BaselineFixSave` 保存活动 ID、计数及原时刻；`BaselineRef` 保存活动 ID、固定状态、首尾事件及计数。与现有仓储一样，key 和 owned 使用现有类型，不新增同义类型。

- [x] **先写失败测试：** `test_fixed_empty_differs_from_collecting` 断言可靠空基准为 `FIXED`、两个引用为 `None`、计数为 0；收集中读取不能返回相同“空基准”。另测 128/129 项批界、批号缺口、重复或乱序身份、其他活动混入、旧收集范围排除、固定后替换拒绝以及原键改变输入拒绝。
- [x] **运行红灯：** `P apps/camctl/tests/integration/capture/test_baseline_history.py -q`，核对目标写入和读取行为的失败。
- [x] **实现事务生产者和读取：** 活动必须已可靠存在，归属方式和范围匹配，收集期间占用为 `HELD`；完整连续范围才可固定。空基准用 `FIX_EMPTY`，不追加空批次。分页只读该活动当前收集的历史范围；条目不创建 `device_files`。固定后不可替换，未派发的未固定重收按原历史规则换范围并保留旧历史。
- [x] **实现新增基准申请的保存交接：** 复用现有完整申请拥有者模式，保留完整 chunk/fix 申请、key、时刻；原结果可靠前不读下一页、不固定、不授予启动。测试“追加已提交但返回未知”及“固定已提交但返回未知”，原键重送只产生原事实；不得生成第二套申请。
- [x] **运行绿灯并提交：** 新用例通过后顺序运行 capture、history 两个集成目录，各自一个 pytest 进程；验证正向、逆向和快照恢复在同边界得到同一基准。提交仓储、守卫及测试。

```python
assert fixed_empty["baseline_state"] == enum_for("device_activities.baseline_state").FIXED
assert fixed_empty["baseline_first_event_id"] is None
assert fixed_empty["baseline_last_event_id"] is None
assert original_key_resend.event_id == first_save.event_id
```

T2 软件验证（2026-10-10，容器，Python 3.11.16）：新增基准集成 21 项通过；相关单元目录 1607 项通过，回执交接补充 6 项通过；调度回归及事件覆盖登记检查通过。capture 目录 665 项通过、11 项失败，history 目录 196 项通过、9 项失败；全部失败在 T1 提交的独立源码副本中同样复现。T2 门禁采用基准专项、调度和独立历史三路径验证，未把目录回归报告为全绿。延时收尾和产物错误由 T5/T6 承接；独立应急错误和原有快照一致性失败仍按各自责任计划跟踪。

## T3 受管 ADB、目录与文件适配

**建议文件：** 新建 `apps/camctl/src/camctl/devices/drivers/adb_cameras/transport.py`、`filesystem.py`；修改 `devices/adb_transport.py`、必要的 `operations/process.py` 和读取适配；新增 `apps/camctl/tests/unit/devices/test_adb_camera_filesystem.py`、`apps/camctl/tests/integration/devices/test_adb_camera_transport.py`。

**接口：** `ManagedTransport.run(spec: ToolSpec, stop: StopSignal) -> RawToolOutcome` 复用 `execute_tool`。建议目录端口 `async read_directory(request: DirectoryRequest, *, stop: StopSignal) -> DirectoryRead`；request 包含原 `DeviceBinding`、输出范围、分页游标、batch 和本次期限，返回实际调用结果、读取错误及 `Page[FileIdentity, DirectoryCursor] | None`。`FileIdentity` 是包含原绑定作用域和完整设备路径的不可变定位结构，可精确编码为基准项；`DirectoryCursor` 是绑定本次目录范围且能校验进度的不透明游标。该端口用于只读基准准备，不冒用要求 RESULTS 票据的 `list_results`，也不消耗 START 次数。

- [x] **先写失败测试：** `test_directory_read_failure_is_not_empty` 断言 `error is not None`、`page is None`，即使此前有部分输出；旧文件与新子目录的完整路径仍可区分。覆盖空页有后续游标、末页非空、重复页、游标不前进、截断输出、空格/引号/换行文件名及错误绑定。真实进程、文件和管道测试放集成目录。
- [x] **运行红灯：** 先运行上述单元文件，再单独运行新 devices 集成文件，确认故障分类及受管收场尚未满足。
- [x] **接入已有受管调用：** 将具体 ADB transport 接到现有 `execute_tool`，使用明确 serial；相机路径按 shell 参数规则传递，主机用 argv。单次调用没有隐藏重试。保留实际退出、可靠观察与错误；截断或无法解释不算成功。目录按同一稳定身份顺序输出跨页有序流，基准和后续列举可分批合并比较。当前 stdout 有容量上限且 stderr 被丢弃，大输出必须分批或流式处理并显式识别不完整输入；需要响应通道时在原受管边界扩展有界采集，不在驱动另建无拥有者进程。
- [x] **实现文件端口：** 按已有接口提供目录/存在性/必要元数据、源 SHA-256、按 offset 连续读取和指定文件删除；复用 `SourceFile`、`ReadSession` 的真实停止责任。工具是否存在、输出格式、按位置读取及远端退出含义是 T9 的设备输入；容器测试用受真实端口约束的工具替身，未知工具能力不作为已支持声明。摘要读取失败保留错误，不以主机摘要代替源摘要。
- [x] **运行绿灯并提交：** 顺序运行 devices、operations 集成目录；验证停止请求后等实际退出、进程留在原组、共享 ADB 服务端不被清理，以及读取停止不伪装读完。提交适配及测试；无需引入通用插件框架。

```python
assert failed_directory.error is not None
assert failed_directory.page is None
assert final_nonempty_page.items and final_nonempty_page.next_cursor is None
```

T3 软件验证（2026-10-10，容器，Python 3.11.16）：目录解释单元 21 项通过，具体文件适配和真实受管进程组合 14 项通过；devices、operations、host_files 单元目录及 devices、operations 组件集成目录顺序回归通过。验证包括双通道超限、输出消费者故障主动终止、进程组继承、特殊路径分页、源摘要、指定删除、大于捕获上限的连续读取和实际停止。ShellFileTools 仍为待设备核实的候选组合，未激活真实相机能力。

## T4 共用启动准备、范围独占及事务守卫

**建议文件：** 新建 `apps/camctl/src/camctl/capture/baseline.py`；修改 `bootstrap/flows.py`、`capture/handlers.py`、`capture/recording.py`、`persistence/repositories/scheduling.py`、`scheduling/resources.py`、`outputs/dispatch.py`；新增 `apps/camctl/tests/integration/capture/test_baseline_start.py`，补充 scheduling/bootstrap 测试。

**接口：** 消费 T1 固定任务事实、T2 基准仓储、T3 目录端口。建议 `async prepare_baseline(action_id: int, activity_id: int, *, runtime: CaptureRuntime) -> BaselinePreparation`，结果枚举区分 READY、FAILED、PENDING；FAILED 含实际读取错误，PENDING 含原保存责任。保留 `start_action(request: StartActionRequest, key, owned)` 和 `grant_start(request: GrantRequest, key, owned)` 的现有返回契约，新增共同守卫，不让具体处理器绕过。

- [x] **先写失败测试：** `test_baseline_read_failure_consumes_no_start` 在两种拍摄入口断言动作失败、原错误保留、START 次数为 0、启动命令未调用。`test_fixed_baseline_and_intent_precede_dispatch` 断言最后可靠固定和原意图都成功后才派发；这是副作用契约，可以断言顺序。
- [x] **运行红灯：** `P apps/camctl/tests/integration/capture/test_baseline_start.py -q`，确认失败来自缺失准备或事务守卫。
- [x] **实现资格到派发的交接：** 按现有排序检查到时、窗口、取消及设备/输出范围资格；只为取得资格的候选建立准备活动，避免所有等待候选先占用导致互锁。活动可靠建立后才追加基准。`BASELINE_COMPARISON` 初始为 `COLLECTING`，独立范围保持原合法声明；grant 的最后事务检查可靠 FIXED 及未变的活动、范围和占用，再保存意图/次数。准备后重查窗口与取消，不延长窗口。
- [x] **实现失败与恢复：** 中间页读取明确失败同样结束动作；本地调用完成收场和准备责任解决后按可靠未派发释放，不伪造 `ENDED`。保存未知先恢复原申请；已有固定基准直接复用；已派发/可能派发不得重收或再次启动。覆盖数据库失败、准备后取消/窗口过期、基准固定后重启、派发后关联保存前重启。
- [x] **统一占用消费者：** 审计 `current_start_holder`、候选授予、结果/文件调度与全部释放入口；终态和 `ENDED` 不跳过 `HELD` 活动。释放还须检查准备、归属及集合限制；可靠未派发不用等待不存在的产物。测试同设备冲突阻塞、不同设备推进、兼容读取继续及非兼容读取让路，不新增全局设备串行锁。
- [x] **运行绿灯并提交：** 顺序运行 capture、scheduling、outputs、bootstrap 集成目录。两种入口均不能绕过最后事务守卫，未完成调用责任不会随占用释放消失。提交本项，形成“基准后启动”门禁。

```python
assert failed_action["status"] == enum_for("actions.status").FAILED
assert start_attempt_count == 0
assert start_command_calls == []
assert saved_read_error == original_read_error
```

### T4 基准读取失败的公开原因

基准读取失败采用 `capture_failed.details.reason=baseline_read_failed`，stage 为 `execution`，活动身份及完整内部错误遵守[文件归属规格](../../architecture/camera-capture.md#本次任务的文件归属)和[公共错误登记](../../../protocol/errors/workflow-codes.json)。两种拍摄入口均须覆盖首批、中间批失败、实际活动身份、原错误保留及原申请提交未知恢复。

T4 软件验证（2026-10-10，容器，Python 3.11.16）：两种拍摄入口覆盖基准读取失败零 START、固定及意图先于派发、原页和原终态申请提交未知、准备取消及过期、终态后实际错误保留、范围资格和跨设备推进。单元目录 2117 项、scheduling 集成 133 项、基准专项 56 项及文件调度专项 5 项通过。完整 capture、outputs、bootstrap 目录分别为 687 项通过／11 项失败、1833 项通过／5 项失败／2 项跳过、878 项通过／6 项失败／1 项跳过；失败均已在修改前源码逐项复现。延时和结果正常收尾由 T5/T6 继续完成，其余应急与读取预算问题留在原责任计划，不将这些目录声明为全绿。准备释放证据及基准错误公开原因已正式登记；真实相机仍未激活。

## T5 分页结果、完成依据与必要检查

实施状态（2026-10-10，容器 Python 3.11.16）：结果页解释、必要产物检查、原页拥有者及历史保存已接入。页和首次文件发现共同提交，基准差集按已保存位置分批比较；页范围分别保留每次实际返回，不增加轮内预算。原页提交未知后可关闭连接，再核实原完整申请。结果消费和动作收尾仍待 T6 接线，T5 暂不标为整体完成。扩大运行 capture 目录取得 676 项通过、54 项失败，其中 11 项与 T4 失败节点一致，43 项涉及集合确定约束下尚未接入的原拍摄及媒体消费者；继续按 T6 的正常结果和恢复矩阵验证。

**建议文件：** 修改 `apps/camctl/src/camctl/capture/result_inputs.py`、`capture/results.py`、`bootstrap/capture_assembly.py`、`persistence/repositories/capture.py` 及证据登记；新增 `apps/camctl/tests/unit/capture/test_result_pages.py`、`test_product_checks.py`，补充 `apps/camctl/tests/integration/capture/test_result_file_recovery.py`。需要新内部格式时，同步责任规格和具名守卫后才写入。

**接口：** 保留 `ResultDriver.list_results(request: ControlRequest, batch: int) -> DeviceCallResult`，游标放在 request 的驱动参数中。建议 `ResultPage(entries: tuple[ObservedFile, ...], next_cursor: DirectoryCursor | None, set_finalized: bool, completion_evidence: DeviceObservation | None, outcome: CallOutcome)`，以及 `DriverResultListing.list_page(ticket: AttemptTicket, *, cursor: DirectoryCursor | None, timeout_s: Decimal) -> ResultPage`。每次调用返回一页，T6 的轮次拥有者沿原票据保存该页后才读取下一页；现有 `list_round` 的 v1 兼容输入不提供集合确定。完成依据与扫描是否结束是两个独立维度；`completion_evidence` 仅保存实际设备观察，等待假设由框架按固定契约及原等待事实形成独立依据。

**保存交接：** 建议仓储 `save_result_page(request: ResultPageSave, key: OperationKey, owned: OwnedConnection) -> DbOutcome[ResultPageRef]`；request 包含原活动/RESULTS 尝试、页序号/游标、完整实际结果、文件输入及原时刻。每页文件观察和页进度同事务保存；`ResultPageRef` 保存原轮次、页序号、实际历史引用和下次游标。最终结果消费已保存页范围，避免累计整个目录在内存中。必要的内部页引用/进度格式集中登记并验证原活动、原轮次和连续范围，不借用 `BASELINE_CHUNK`，不在外层 evidence 增加未登记成员。数据库活动整数 ID 与 `AttemptTicket.target_id` 的规范十进制字符串精确对应。操作收场字段来自实际调用，不为多页操作虚构一个远端退出响应。

- [ ] **先写失败测试：** `test_scan_end_without_finality_is_unknown` 断言 `next_cursor is None` 且 `set_finalized is False` 时 assessment 不能成功或明确缺失。旧 v1、空页有 cursor、最后非空页、错误目标/版本/成员、结果误读为空、分页中断与保存未知分别有独立用例。
- [ ] **运行红灯：** `P apps/camctl/tests/unit/capture/test_result_pages.py apps/camctl/tests/unit/capture/test_product_checks.py -q`，再单独运行 capture 的恢复文件，确认目标分区失败。
- [ ] **实现统一解释和保存：** 实时调用与历史恢复使用同一登记编解码器；一次 RESULTS 核实轮次只消耗一次预算，轮内各批有本次调用时限，无隐式业务重试。原页提交未知时只核实原 key，不继续读下一页；最终关闭前检查所有页可靠保存。归属比较使用 T2 的原基准及 T3 同序稳定身份流，分批合并比较和保存新文件；不能把历史文件当作本次文件。中断或读取错误的扫描不成为完整扫描，新轮次的事实不能与旧扫描混合补造完整集合。
- [ ] **实现检查状态空间：** `assess_capture_files(files: CaptureFileSet, requirements: ProductRequirements) -> CaptureAssessment` 增加集合确定及任务声明的必要规则。实际集合从已保存记录分页读取，建议让 `CaptureFileSet.files` 接受可迭代输入，以计数和检查状态聚合，不为大目录累计完整文件数组；详细文件事实仍从原记录读取。建议每条规则结果枚举 PASSED/FAILED/UNKNOWN，明确必需类别、具体格式、声明数量与配对的适用条件；未知、读取错误、归属未定及未写完优先于缺失/不合格。JPEG 和 RAW 不能只靠 PHOTO 同一类别区分；不默认精确照片数、分段视频或双球文件数。
- [ ] **运行绿灯并提交：** 单元及 capture 集成通过；原页输入、完整错误、已关联文件及页进度跨恢复一致。所有新内部证据类型/version/字段只维护一份登记，静态适配、驱动、恢复与替身共同使用。提交结果输入和检查，尚不把扫描结束当成动作成功。

```python
assert not scan_ended_but_unfinalized.is_complete
assert not scan_ended_but_unfinalized.explicitly_unmet
assert result_attempts_after_last_page == result_attempts_after_first_page
```

## T6 录像与延时摄影的正常收尾及原决定恢复

阶段验证（2026-10-10，容器 Python 3.11.16）：同一 RESULTS 尝试逐页读取并可靠保存，关闭流程后从原页范围消费；原页回执未知先核实原键。录像与 `DEVICE + TIME_AND_OUTPUTS` 延时摄影已接入集合确定、类别及固定格式和数量要求，明确空集合和不合格产物保存已登记的公共错误。文件归属使用原固定基准或任务范围；跨页预览在原片归属保存后配对；后轮读取失败不抹去前轮已经确认完成的文件。2166 项相关单元、97 项页与基准专项集成、2 项 bootstrap 共同恢复集成通过。这是阶段提交依据，T6 仍待接入另外两种合法延时完成组合、其余共有消费入口和恢复矩阵，并运行 capture、bootstrap、reporting 完整门禁；T5/T6 尚不标为整体完成。

**建议文件：** 修改 `apps/camctl/src/camctl/capture/handlers.py`、`capture/recording.py`、`persistence/repositories/capture.py`；补充 `apps/camctl/tests/integration/capture/test_capture_contract.py`、`test_timelapse_wait_runtime.py`、`test_result_confirmation.py`、`test_result_error_history.py`，以及 bootstrap 的 `test_timelapse_finish.py`、`test_recording_stop.py`。

**接口：** 消费 T5 `list_page`、可靠页范围及 assessment，轮次拥有者组织读取、保存及最后关闭，接入 `_confirm_timelapse_results` 或等价共同边界。沿用 `finish_result_check(AttemptFinish, ResultSetSave, key, owned)`、`confirm_result_set(ResultSetSave, key, owned)` 和 `CaptureRuntime.save_capture_completion`；原调用、采集结论、必要文件事实、正式产物及终态按规定共同提交。`PendingCaptureCompletion` 等原申请拥有者先于依赖新配置或新设备调用恢复。

- [ ] **先写失败测试：** 正常仅视频、照片加视频均取得成功，未知集合继续核实；明确空集合为 `capture_failed/no_outputs`，非空缺类别/格式/声明数量不符为 `capture_failed/invalid_outputs`，未确认且耗尽为 `capture_result_unconfirmed/outputs_unknown`。精确断言 code、登记 stage、details 的真实活动 ID，而不是动作 ID；读取错误仍保留完整原结构。
- [ ] **运行红灯：** 分别运行上述 capture 与 bootstrap 文件，不能跨两个目录合为一次 pytest；确认正常成功和错误分区的失败。
- [ ] **补齐正常结果的消费矩阵：** 复用已有检查规则并接入 T5 新完成输入；完成依据不足、等待未满、单文件未完、归属未定、集合未定及读取错误均属于未确认。必要事实齐备后的空/不合格错误，落实[原错误计划任务三](2026-10-09-camctl-result-errors-and-report-projection.md#任务三结果失败语义与同类入口交接)的已登记分区。三种合法结束/完成组合遵守[执行定义组合](../../camctl/database/execution-definitions.md#延时摄影执行定义的完成判定方式)，不启用 `HOST_TIMER + TIME_AND_OUTPUTS`。主机负责结束时核对原计时、实际 stop 及保存接线，所缺部分在既有路径补齐；设备负责结束时不自动发送不适用的 stop。
- [ ] **接入计时与保存：** 录像启动确认的当前单调钟锚点不被准备、列举或结果保存改变，到期停止有独立调度机会；正常停止确认后沿原基准登记原片，无额外媒体拷贝。延时等待沿原发送时刻、原完成方式及适用配置恢复，可靠完成依据不重复等待；等待假设保持 `TIME_AND_OUTPUTS`，不变成设备直接观察。
- [ ] **验证新输入的失败及恢复：** 沿用已有完整终态申请和原文件事实保存边界；既有终态、有效取消、明确设备失败优先，失败仍登记合格的已完成文件。保存 UNKNOWN 后原键核实且结果确定，才推进终态/产物/释放；针对新页范围和完成输入补配置/绑定、取消和窗口变化用例，不能漏存原事实、重复设备调用或改写旧错误。审计新输入涉及的全部成功、明确失败和耗尽入口，原错误计划跟踪相关构造与恢复结果。
- [ ] **运行绿灯并提交：** 顺序运行 capture、bootstrap、reporting 集成目录。当前投影与同边界历史报告一致，产物及动作终态的共同保存能证伪部分提交。提交形成正常结果闭合门禁；独立旧路线图任务不借此扩大范围。

```python
assert empty_error["code"] == "capture_failed"
assert empty_error["details"]["reason"] == "no_outputs"
assert invalid_error["details"]["reason"] == "invalid_outputs"
assert unknown_error["code"] == "capture_result_unconfirmed"
assert unknown_error["details"]["reason"] == "outputs_unknown"
assert unknown_error["details"]["activity_id"] == original_activity_id
```

## T7 双相机驱动与 CLI、发行物的一致登记

**建议文件：** 新建 `apps/camctl/src/camctl/devices/drivers/adb_cameras/driver.py`、`registration.py`；修改 `devices/catalog.py`、`devices/drivers/runtime.py`、`bootstrap/capture_assembly.py`、正常 CLI 装配和配置绑定读取；新增 `apps/camctl/tests/integration/devices/test_adb_camera_registration.py`，补充 `apps/camctl/tests/integration/bootstrap/test_distribution.py`、`test_package.py`。

**接口：** 正常驱动实现既有 control/stop/result/read/digest/delete 端口；有可靠契约才声明 query。建议 `register_builtin_camera_drivers(config: ConfigSnapshot) -> None` 在正常装配统一登记静态定义与运行工厂，幂等处理同一身份，不登记空 actions。`command_for(ControlRequest, int | None) -> DeviceCommand` 或等价内部映射使用 T1 候选字面量和 T3 受管调用；可用定义来自已完整的任务契约。

- [ ] **先写失败测试：** `test_pending_contract_is_not_described_or_accepted` 断言缺少响应或结束依据的任务不在 describe 中、不能按已支持任务受理；`DEVICE_VERIFICATION_PENDING` 标签本身不能替代实际筛选。独立 CLI 进程与安装包必须得到同一登记，不能依赖测试预先 import。
- [ ] **运行红灯：** `P apps/camctl/tests/integration/devices/test_adb_camera_registration.py -q`，确认装配及候选过滤尚不成立。
- [ ] **实现端口装配：** 设置、启动、停止保留各自真实返回含义；设置失败不继续启动，设置完成不算已经启动，同一已授予 START 不能由每条设置命令重复消耗预算。实际派发前仍核对适用取消/窗口。未知启动结果不能直接再发启动。文件和读控制通过 T3 接入，必要控制不被长读取或报告维护阻塞。
- [ ] **接入绑定与同源导出：** describe、submit、run 消费同一 driver_id/参数定义/工厂；运行使用动作原设备和驱动身份、本次配置的明确 ADB 绑定。缺失/不匹配按已有核对及错误规则，不自动改驱动或 serial。设备观察按原票据的实际活动身份校验，列举同源；只有设备支持传入任务标识时才下发原 `task_key`，目录基准方式不新增设备去重语义。查询“尚未实现”与设备“不支持”区分；真实契约未补齐的部分继续隔离为候选，可用性由内容决定。
- [ ] **运行绿灯并提交：** 顺序运行 devices、acceptance、bootstrap、contracts 集成目录，验证包内 Schema/资源和无需源码 checkout 的启动。软件专用契约替身仅经测试装配注入，不加入生产 fake 模式或替代真机判据。提交静态/运行登记与发行验证。

```python
assert candidate_parameter_type not in described_parameter_types
assert pending_task_admission["status"] == enum_for("actions.status").FAILED
assert installed_catalog_document == checkout_catalog_document
```

## T8 容器的四条跨组件演示链与设备采集交付

**建议文件：** 新建 `tests/integration/test_real_camera_demo_roundtrip.py`，复用 `camctl_fixtures.py`、`_wsl_host_demo.py` 及现有 C host 链；新建 `docs/hardware/camera-demo-validation.md`，补充 `docs/camctl/verification.md` 与 `integration-readiness.md`。示例随既有发行样例机制交付，样例路径及内部生成器组织为实施选择。

**接口：** 两款摄像机的 recording/timelapse 共四个参数化场景，经现有 C host 提交真实协议计划，再通过独立 obtain 动作选择正式产物。复用实际 CLI、SQLite、host 文件领取及报告 ACK；驱动契约替身由测试 launcher 注入，数据从 T1 定义生成，不修改客户端源码。

- [ ] **先写失败测试：** `test_camera_demo_capture_obtain_and_report` 对四个场景断言：capture 和 obtain 成功、输出属于原动作、源文件保留、领取文件的大小/摘要与来源相符、报告按原动作给出有效业务状态。Action6 RAW/JPEG 组合另有组件集成覆盖。收到视频与报告 ACK 分别验证，不能只检测文件存在。
- [ ] **运行红灯：** `P tests/integration/test_real_camera_demo_roundtrip.py -q`，按根集成说明准备真实 host 构建和交付环境，失败须来自缺少完整链路。
- [ ] **实现真实软件组合：** 使用相机端口替身控制事实和时间，等待逻辑可加速而保持原预设时长。覆盖老文件、新目录、同设备后续拍摄、原基准跨进程恢复；长读取过程中到期停止仍推进，报告维护独立推进。测试现有客户端报告消费者时保持产品源码只读。
- [ ] **交付操作步骤和样例：** 提供两款设备的独立录像/延时计划、后续 obtain、配置/describe 导出及 host 领取步骤；记录样例合法值来自能力定义，不依赖开发目录布局。Windows 采集流程先检查指定 serial、型号/固件及工具，再采集前目录、设置/start/stop 的 stdout/stderr/退出/耗时、后目录、长度/摘要，延时观察自然结束和文件处理；显式清理仅操作用户指定试验文件。采集原始响应不以本地主机退出替代设备结果。
- [ ] **运行绿灯并提交：** 新根集成及原 `test_camctl_c_module_roundtrip.py` 通过，顺序重跑受影响单元与组件目录；检查示例在软件契约装配中能被真实受理且各档必要输出规则一致，正式发行物的可用能力及样例在 T9 激活后重验。记录日期、容器环境、范围和实际结果。提交软件演示和真机采集交付，软件通过仍不声明设备已可演示。

```python
assert capture_status == obtain_status == "succeeded"
assert claimed_size == source_size
assert claimed_sha256 == source_sha256
assert source_still_exists
assert report_action_id == submitted_action_id
```

## T9 设备事实补齐与 ARM Linux 完整验收

**前置输入：** T8 交付后，由用户在 Windows 或目标主机获取实际资料，逐款补齐[设备输入清单](../specs/2026-10-10-camctl-real-camera-demo-design.md#最后阶段需要采集的设备资料)。此前 T1—T8 不依赖相机在线；缺少某款输入时只暂停该款的契约完成和真机验收。

**建议文件：** 双相机驱动的实际响应解释器、T1 定义、命令测试和 device/capture 集成测试；更新 `docs/hardware/camera-control-handoff.md`、`camera-demo-validation.md`、`docs/camctl/integration-readiness.md` 和 `verification.md`。技术资料以实际适用固件和命令为范围，保密材料遵守根 AGENTS 的抽象记录边界。

**接口：** `ResponseInterpreter.interpret(raw: RawToolOutcome) -> InterpretedFacts` 消费实际响应，返回实际观察、完整错误和效果；T1 的任务工厂据已核实的结束控制及完成方式形成正式契约，T7 同源登记后再导出。无需增加新的公共动作类型。

- [ ] **先写失败测试：** 以实际成功、拒绝、错误和无法解释样例测试设置/start/stop；证明没有匹配事实时保持未知。逐项核实资料中的设置和完整组合，按实际适用范围启用全部可用选项，不只启用四条演示预设。完成定义覆盖已核实的自然结束或主机结束、文件写完/集合依据和实际必要产物，未知组合不导出。划线和冲突项逐项据试验确定，样例数值不自动成为合法组合。
- [ ] **运行红灯并实现解析：** 先运行对应 unit/devices 及 devices/capture 集成用例，确认解析或任务定义缺口；补齐真正需要的响应通道、文件访问和必要等待余量。不能仅用退出码 0、瞬时大小不变或文件出现保证实际启动、停止和全部写完。
- [ ] **运行软件绿灯并提交：** 顺序运行所有受影响单元、组件目录和 T8 根组合，检查已激活能力的实际消费者。必要调整落实后提交，提供目标主机可独立安装的发行物、能力说明、配置和预设计划。
- [ ] **执行真实时长：** ARM Linux 接入两款相机，分别运行 10 秒录像及 Action6 30 分钟/OSMO 10 分钟延时；经 C host 完成 submit → capture → 正式输出 → obtain → ready → claim，并验证完整文件和报告。不得缩短任务时长后声称对应预设已验收。
- [ ] **保存结果并提交：** 记录主机/ADB/固件、实际结束和完成依据、原文件关联、领取文件大小/摘要及报告结果；确认源文件保留。四条真实链全部成立才声明演示就绪；未支持选项和待验证输入按实际状态列出。硬件发现需改代码时先增加回归，再修改、重跑受影响软件门禁、重验受影响真机场景并提交。

```python
assert unknown_response_facts.observations == ()
assert unknown_response_facts.effect is EffectState.UNKNOWN
assert verified_task.completion_mode == expected_verified_completion_mode
```

## 横切契约检查与停止条件

| 数据或控制交接 | 权威输入、检查及可观察结果 | 责任任务 |
| --- | --- | --- |
| 用户参数到执行 | 同源 Schema → 受理固定要求/绑定/执行定义 → 原命令及原任务规则；新配置不改旧要求 | T1、T7、T9 |
| 候选到实际启动 | 排序和资格 → 活动及范围占用 → 分批基准可靠固定 → 原意图和次数可靠保存 → 真实派发；准备错误保持 START 为 0 | T2—T4、T7 |
| 调用到产物 | 原实际结果/等待 → 原基准差集 → 独立归属、写完和集合依据 → 必要检查 → 同事务正式输出和结果 | T3、T5、T6 |
| 未知保存到恢复 | 原完整页/决定、key、时刻 → 原事务核实 → 已保存事实恢复 → 依赖业务；不换输入补发副作用 | T2、T4—T7 |
| 源文件到用户 | 正式输出 → 独立 obtain 和源摘要校验 → 完整 ready 文件 → host claim；报告使用同边界历史，ACK 独立 | T7—T9 |
| 占用到下一动作 | 实际结束或可靠未派发/适用完成依据 → 剩余准备和文件限制 → 可靠 RELEASED → 下一候选重查资格 | T4、T6、T8 |

执行中出现规格未定义的行为或需要扩大故障范围时，停止相关任务，给出具体入口、状态和证据后再确定契约；不要用候选响应、空列表或默认值绕过。原有独立问题记录回对应计划；若它阻断上述链路或破坏同一不变量，则先说明依赖并纳入所需修复，不因旧路线图仍有未完成项自动扩大本计划。

每项完成后更新本计划的接入步骤与验证记录，并进行本地提交。原计划已有缺口的细项完成状态只在该责任计划登记；本文记录该前置交付是否满足双相机接入门禁。T8 的软件完成与 T9 的真机完成分别报告，进度按实际交付和未补设备输入表达。
