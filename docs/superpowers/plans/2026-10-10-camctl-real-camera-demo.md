# ADB 相机接入与 Action6 录像演示实施计划

> **执行者要求：** 使用 `superpowers:executing-plans` 或 `superpowers:subagent-driven-development` 逐项实施。每个任务先运行能够证伪契约的测试，再实现、验证并提交。

**目标：** 完成 Action6 普通录像的真实演示：由 C host 提交计划，camctl 拍摄并登记产物，随后独立取回视频，领取文件和状态报告。两款相机的录像与原生延时摄影保留为软件接入范围；其余真机场景在各自设备条件满足后继续。

**架构：** 具体驱动负责参数、命令和设备事实，公共流程负责目录基准、尝试、等待、结果保存及恢复。复用现有受管进程、读取拥有者、SQLite 历史、文件交接和 C host；先完成容器软件闭环，最后补齐实际响应并在 ARM Linux 验收。

**技术栈：** Python 3.11、uv、pytest、SQLite、JSON Schema、ADB、现有 C 接入模块。

**设计依据：** [ADB 相机接入与 Action6 录像演示设计](../specs/2026-10-10-camctl-real-camera-demo-design.md)。行为责任分别见[文件归属与检查](../../architecture/camera-capture.md#本次任务的文件归属)、[录像](../../architecture/camera-recording.md)、[能力](../../architecture/camera-capabilities.md)、[取回](../../architecture/obtaining-outputs.md)和[历史格式](../../camctl/database/history-formats.md)。

## 全局约束

- 型号为 Action6 和 OSMO 360 II。容器用于开发和软件验证，Windows 用于 ADB 试验，部署及最终完整演示在 ARM Linux。
- 当前真机演示采用 Action6 10 秒普通录像。Action6 延时等待正常新机，OSMO 360 II 等待可用 ADB 接入，不作为当前演示的通过条件。覆盖资料中全部可用能力的软件候选定义继续保留，不把演示预设作为驱动的能力限制。
- 后续 OSMO 延时诊断的用户目标为 5 秒间隔、60 秒持续、仅视频。资料没有给出这一组合的完整命令负载，编码及设备支持待核实；原表样例和软件候选不据此改写。Action6 延时仍保留原 8 秒间隔、1800 秒完整候选及独立秒级诊断入口。
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
- [x] **区分候选与契约：** 划线项、FOV 适用范围、OSMO 90 分钟缺命令、25 秒/100 分钟与 2 小时冲突保留待设备核实；不导出猜测选项。OSMO 无增稳调节或光圈命令不生成设置。Action6 未提供延时停止命令，录像停止不能自动用于延时。任务必要类别、格式和数量规则使用驱动声明，不以间隔除时长补造数量。
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

实施完成（2026-10-10，容器，Python 3.11.16）：正式 v2 结果页、连续原页范围、原页保存拥有者及必要产物检查已接入实时调用和恢复。每轮只消耗一次 RESULTS 预算；原页和首次发现共同保存，归属沿原固定基准分批比较。扫描结束、集合确定、文件写完与设备完成分别保存；格式、声明数量和配对按首次固定任务规则判定，未知优先于缺失或不合格。原申请提交未知、页中断、新轮首批尚未返回和文件子责任恢复均已验证。共同消费者及验证结果见 T6。

**建议文件：** 修改 `apps/camctl/src/camctl/capture/result_inputs.py`、`capture/results.py`、`bootstrap/capture_assembly.py`、`persistence/repositories/capture.py` 及证据登记；新增 `apps/camctl/tests/unit/capture/test_result_pages.py`、`test_product_checks.py`，补充 `apps/camctl/tests/integration/capture/test_result_file_recovery.py`。需要新内部格式时，同步责任规格和具名守卫后才写入。

**接口：** 保留 `ResultDriver.list_results(request: ControlRequest, batch: int) -> DeviceCallResult`，游标放在 request 的驱动参数中。建议 `ResultPage(entries: tuple[ObservedFile, ...], next_cursor: DirectoryCursor | None, set_finalized: bool, completion_evidence: DeviceObservation | None, outcome: CallOutcome)`，以及 `DriverResultListing.list_page(ticket: AttemptTicket, *, cursor: DirectoryCursor | None, timeout_s: Decimal) -> ResultPage`。每次调用返回一页，T6 的轮次拥有者沿原票据保存该页后才读取下一页；现有 `list_round` 的 v1 兼容输入不提供集合确定。完成依据与扫描是否结束是两个独立维度；`completion_evidence` 仅保存实际设备观察，等待假设由框架按固定契约及原等待事实形成独立依据。

**保存交接：** 建议仓储 `save_result_page(request: ResultPageSave, key: OperationKey, owned: OwnedConnection) -> DbOutcome[ResultPageRef]`；request 包含原活动/RESULTS 尝试、页序号/游标、完整实际结果、文件输入及原时刻。每页文件观察和页进度同事务保存；`ResultPageRef` 保存原轮次、页序号、实际历史引用和下次游标。最终结果消费已保存页范围，避免累计整个目录在内存中。必要的内部页引用/进度格式集中登记并验证原活动、原轮次和连续范围，不借用 `BASELINE_CHUNK`，不在外层 evidence 增加未登记成员。数据库活动整数 ID 与 `AttemptTicket.target_id` 的规范十进制字符串精确对应。操作收场字段来自实际调用，不为多页操作虚构一个远端退出响应。

- [x] **先写失败测试：** `test_scan_end_without_finality_is_unknown` 断言 `next_cursor is None` 且 `set_finalized is False` 时 assessment 不能成功或明确缺失。旧 v1、空页有 cursor、最后非空页、错误目标/版本/成员、结果误读为空、分页中断与保存未知分别有独立用例。
- [x] **运行红灯：** `P apps/camctl/tests/unit/capture/test_result_pages.py apps/camctl/tests/unit/capture/test_product_checks.py -q`，再单独运行 capture 的恢复文件，确认目标分区失败。
- [x] **实现统一解释和保存：** 实时调用与历史恢复使用同一登记编解码器；一次 RESULTS 核实轮次只消耗一次预算，轮内各批有本次调用时限，无隐式业务重试。原页提交未知时只核实原 key，不继续读下一页；最终关闭前检查所有页可靠保存。归属比较使用 T2 的原基准及 T3 同序稳定身份流，分批合并比较和保存新文件；不能把历史文件当作本次文件。中断或读取错误的扫描不成为完整扫描，新轮次的事实不能与旧扫描混合补造完整集合。
- [x] **实现检查状态空间：** `assess_capture_files(files: CaptureFileSet, requirements: ProductRequirements) -> CaptureAssessment` 增加集合确定及任务声明的必要规则。实际集合从已保存记录分页读取，建议让 `CaptureFileSet.files` 接受可迭代输入，以计数和检查状态聚合，不为大目录累计完整文件数组；详细文件事实仍从原记录读取。建议每条规则结果枚举 PASSED/FAILED/UNKNOWN，明确必需类别、具体格式、声明数量与配对的适用条件；未知、读取错误、归属未定及未写完优先于缺失/不合格。JPEG 和 RAW 不能只靠 PHOTO 同一类别区分；不默认精确照片数、分段视频或双球文件数。
- [x] **运行绿灯并提交：** 单元及 capture 集成通过；原页输入、完整错误、已关联文件及页进度跨恢复一致。所有新内部证据类型/version/字段只维护一份登记，静态适配、驱动、恢复与替身共同使用。提交结果输入和检查，尚不把扫描结束当成动作成功。

```python
assert not scan_ended_but_unfinalized.is_complete
assert not scan_ended_but_unfinalized.explicitly_unmet
assert result_attempts_after_last_page == result_attempts_after_first_page
```

## T6 录像与延时摄影的正常收尾及原决定恢复

实施完成（2026-10-10，容器，Python 3.11.16）：录像、单张拍摄及三种合法延时结束/完成组合消费可靠 v2 页和原来源文件，保存正确的成功、空集合、不合格与未确认结果。主机计时失去连续依据时沿原有限 STOP 停止并保留 `duration_unknown`；原生 `COMPLETED` 调用使用首次固定的完整期限。原实际结束、独立文件集合事实和采集判断保持各自含义；取消或采集失败仍按资格保留完整文件。原决定、原键、页引用、实际返回时刻及完整错误跨连接恢复，当前投影与正向、逆向及快照历史恢复一致。

软件验证：全部单元 4203 项通过、1 项跳过、2 项既有 asyncio 警告；reporting 完整目录 367 项通过；capture 完整目录 851 项通过、5 项既有应急错误格式失败。这五项由[原错误责任计划任务三](2026-10-09-camctl-result-errors-and-report-projection.md#任务三结果失败语义与同类入口交接)跟踪，与前阶段精确失败节点相同，不属于双相机正常收尾增量。bootstrap 完整运行取得 884 项通过、1 项失败、1 项跳过，唯一失败是内部读取已经失败、绑定收场仍保留完整原片时的事件断言；按正式保留规则补充产物身份和共同事务检查后，该文件及相邻保留/绑定恢复共 19 项通过。未将这两次结果表述为一次完整目录全绿。仓库外发行验证 4 项、独立范围专项 49 项、共享停止与取消专项 92 项、正常拍摄和停止恢复专项 65 项通过；数据库结构 3190 项断言、事件登记、报告依赖与文档链接检查通过。双相机驱动与四条 C host 演示继续由 T7/T8 实施，具体响应、等待余量和完整调用期限由 T9 实测。

**建议文件：** 修改 `apps/camctl/src/camctl/capture/handlers.py`、`capture/recording.py`、`persistence/repositories/capture.py`；补充 `apps/camctl/tests/integration/capture/test_capture_contract.py`、`test_timelapse_wait_runtime.py`、`test_result_confirmation.py`、`test_result_error_history.py`，以及 bootstrap 的 `test_timelapse_finish.py`、`test_recording_stop.py`。

**接口：** 消费 T5 `list_page`、可靠页范围及 assessment，轮次拥有者组织读取、保存及最后关闭，接入 `_confirm_timelapse_results` 或等价共同边界。沿用 `finish_result_check(AttemptFinish, ResultSetSave, key, owned)`、`confirm_result_set(ResultSetSave, key, owned)` 和 `CaptureRuntime.save_capture_completion`；原调用、采集结论、必要文件事实、正式产物及终态按规定共同提交。`PendingCaptureCompletion` 等原申请拥有者先于依赖新配置或新设备调用恢复。

- [x] **先写失败测试：** 正常仅视频、照片加视频均取得成功，未知集合继续核实；明确空集合为 `capture_failed/no_outputs`，非空缺类别/格式/声明数量不符为 `capture_failed/invalid_outputs`，未确认且耗尽为 `capture_result_unconfirmed/outputs_unknown`。精确断言 code、登记 stage、details 的真实活动 ID，而不是动作 ID；读取错误仍保留完整原结构。
- [x] **运行红灯：** 分别运行上述 capture 与 bootstrap 文件，不能跨两个目录合为一次 pytest；确认正常成功和错误分区的失败。
- [x] **补齐正常结果的消费矩阵：** 复用已有检查规则并接入 T5 新完成输入；完成依据不足、等待未满、单文件未完、归属未定、集合未定及读取错误均属于未确认。必要事实齐备后的空/不合格错误，落实[原错误计划任务三](2026-10-09-camctl-result-errors-and-report-projection.md#任务三结果失败语义与同类入口交接)的已登记分区。三种合法结束/完成组合遵守[执行定义组合](../../camctl/database/execution-definitions.md#延时摄影执行定义的完成判定方式)，不启用 `HOST_TIMER + TIME_AND_OUTPUTS`。主机负责结束时核对原计时、实际 stop 及保存接线，所缺部分在既有路径补齐；设备负责结束时不自动发送不适用的 stop。
- [x] **接入计时与保存：** 录像启动确认的当前单调钟锚点不被准备、列举或结果保存改变，到期停止有独立调度机会；正常停止确认后沿原基准登记原片，无额外媒体拷贝。延时等待沿原发送时刻、原完成方式及适用配置恢复，可靠完成依据不重复等待；等待假设保持 `TIME_AND_OUTPUTS`，不变成设备直接观察。
- [x] **验证新输入的失败及恢复：** 沿用已有完整终态申请和原文件事实保存边界；既有终态、有效取消、明确设备失败优先，失败仍登记合格的已完成文件。保存 UNKNOWN 后原键核实且结果确定，才推进终态/产物/释放；针对新页范围和完成输入补配置/绑定、取消和窗口变化用例，不能漏存原事实、重复设备调用或改写旧错误。审计新输入涉及的全部成功、明确失败和耗尽入口，原错误计划跟踪相关构造与恢复结果。
- [x] **运行绿灯并提交：** 顺序运行 capture、bootstrap、reporting 集成目录。当前投影与同边界历史报告一致，产物及动作终态的共同保存能证伪部分提交。提交形成正常结果闭合门禁；独立旧路线图任务不借此扩大范围。

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

- [x] **先写失败测试：** `test_pending_contract_is_not_described_or_accepted` 断言缺少响应或结束依据的任务不在 describe 中、不能按已支持任务受理；`DEVICE_VERIFICATION_PENDING` 标签本身不能替代实际筛选。独立 CLI 进程与安装包必须得到同一登记，不能依赖测试预先 import。
- [x] **运行红灯：** `P apps/camctl/tests/integration/devices/test_adb_camera_registration.py -q`，确认装配及候选过滤尚不成立。
- [x] **实现端口装配：** 设置、启动、停止保留各自真实返回含义；设置失败不继续启动，设置完成不算已经启动，同一已授予 START 不能由每条设置命令重复消耗预算。实际派发前仍核对适用取消/窗口。未知启动结果不能直接再发启动。文件和读控制通过 T3 接入，必要控制不被长读取或报告维护阻塞。
- [x] **接入绑定与同源导出：** describe、submit、run 消费同一 driver_id/参数定义/工厂；运行使用动作原设备和驱动身份、本次配置的明确 ADB 绑定。缺失/不匹配按已有核对及错误规则，不自动改驱动或 serial。设备观察按原票据的实际活动身份校验，列举同源；只有设备支持传入任务标识时才下发原 `task_key`，目录基准方式不新增设备去重语义。查询“尚未实现”与设备“不支持”区分；真实契约未补齐的部分继续隔离为候选，可用性由内容决定。
- [x] **运行绿灯并提交：** 顺序运行 devices、acceptance、bootstrap、contracts 集成目录，验证包内 Schema/资源和无需源码 checkout 的启动。软件专用契约替身仅经测试装配注入，不加入生产 fake 模式或替代真机判据。提交静态/运行登记与发行验证。

```python
assert candidate_parameter_type not in described_parameter_types
assert pending_task_admission["status"] == enum_for("actions.status").FAILED
assert installed_catalog_document == checkout_catalog_document
```

T7 阶段验证（2026-10-10，容器，Python 3.11.16）：相机端口、明确 serial、多步派发检查和正常登记已经接入。全部单元 4233 项、devices 集成 84 项、acceptance 集成 239 项、contracts 集成 192 项及仓库外发行 6 项通过；完整 bootstrap 装配下的新派发分区 8 项通过。发行构建使用本机缓存验证；未完成的真实响应、结束及文件工具契约继续保持候选。完整 bootstrap 取得 883 项通过、12 项失败、1 项跳过；12 项均为旧测试夹具只清空运行登记却留下静态定义导致的装配冲突。将两份登记一起隔离后，按实际前序登记、夹具清理及全部失败入口顺序复验 16 项通过。生产登记和冲突规则保持原契约；此处不声称单次完整 bootstrap 全绿。T7 门禁完成，四条 C host 软件演示由 T8 验证。

## T8 容器的四条跨组件演示链与设备采集交付

**建议文件：** 新建 `tests/integration/test_real_camera_demo_roundtrip.py`，复用 `camctl_fixtures.py`、`_wsl_host_demo.py` 及现有 C host 链；新建 `docs/hardware/camera-demo-validation.md`，补充 `docs/camctl/verification.md` 与 `integration-readiness.md`。示例随既有发行样例机制交付，样例路径及内部生成器组织为实施选择。

**接口：** 两款摄像机的 recording/timelapse 共四个参数化场景，经现有 C host 提交真实协议计划，再通过独立 obtain 动作选择正式产物。复用实际 CLI、SQLite、host 文件领取及报告 ACK；驱动契约替身由测试 launcher 注入，数据从 T1 定义生成，不修改客户端源码。

- [x] **先写失败测试：** `test_camera_demo_capture_obtain_and_report` 对四个场景断言：capture 和 obtain 成功、输出属于原动作、源文件保留、领取文件的大小/摘要与来源相符、报告按原动作给出有效业务状态。Action6 RAW/JPEG 组合另有组件集成覆盖。收到视频与报告 ACK 分别验证，不能只检测文件存在。
- [x] **运行红灯：** `P tests/integration/test_real_camera_demo_roundtrip.py -q`，按根集成说明准备真实 host 构建和交付环境，失败须来自缺少完整链路。
- [x] **实现真实软件组合：** 使用相机端口替身控制事实和时间，等待逻辑可加速而保持原预设时长。覆盖老文件、新目录、同设备后续拍摄、原基准跨进程恢复；长读取过程中到期停止仍推进，报告维护独立推进。测试现有客户端报告消费者时保持产品源码只读。
- [x] **交付操作步骤和样例：** 提供两款设备的独立录像/延时计划、后续 obtain、配置/describe 导出及 host 领取步骤；记录样例合法值来自能力定义，不依赖开发目录布局。Windows 采集流程先检查指定 serial、型号/固件及工具，再采集前目录、设置/start/stop 的 stdout/stderr/退出/耗时、后目录、长度/摘要，延时观察自然结束和文件处理；显式清理仅操作用户指定试验文件。采集原始响应不以本地主机退出替代设备结果。
- [x] **运行绿灯并提交：** 新根集成及原 `test_camctl_c_module_roundtrip.py` 通过，顺序重跑受影响单元与组件目录；检查示例在软件契约装配中能被真实受理且各档必要输出规则一致，正式发行物的可用能力及样例在 T9 激活后重验。记录日期、容器环境、范围和实际结果。提交软件演示和真机采集交付，软件通过仍不声明设备已可演示。

```python
assert capture_status == obtain_status == "succeeded"
assert claimed_size == source_size
assert claimed_sha256 == source_sha256
assert source_still_exists
assert report_action_id == submitted_action_id
```

软件交付（2026-10-10，容器）：Action6 全部 8 个延时预设分别消费正式参数 Schema、任务工厂及收尾仓储，共 20 个必要产物分区通过；照片缺失或格式不符时动作按 `invalid_outputs` 失败，已确认 MP4 继续保留。发行物携带四份拍摄、独立取回、报告确认、双设备配置及计划生成器，实际安装环境中的 16 项受理、精确 ID 和候选能力拒绝验证通过；安装资源逐字节及同步检查 3 项通过。T8 软件演示与设备采集交付完成；真实相机的响应、结束及文件完成依据仍由 T9 核实。

最终回归验证（2026-10-10，Linux x86_64 容器，Python 3.11.16）：pytest 按目录顺序独占运行。验证范围和实际结果如下；拍摄及产物目录的既有失败不计为通过，也不属于四条正常演示链的完成声明。

| 验证范围 | 实际结果与边界 |
| --- | --- |
| 全部单元 | 4263 项通过、1 项跳过；2 条既有 asyncio 标记警告。 |
| operations、devices 组件集成 | 分别 200 项和 84 项通过，覆盖实际进程收场及设备适配。 |
| bootstrap 组件集成 | 全部 87 个文件分批覆盖，共 939 项通过、1 项跳过。前段为 426 项通过及 1 项跳过，修正的媒体取消文件 4 项通过，剩余 52 个文件 509 项通过；不将分批结果写为单次全目录通过。 |
| session 组件集成 | 100 项通过。 |
| outputs 组件集成 | 1833 项通过、5 项失败、2 项跳过。首次全目录为 1829 项通过及 4 项夹具错误；夹具登记完整集合证据后，该文件 4 项复验通过。5 项读取预算及恢复失败与[原配置闭合计划记录](2026-10-09-camctl-runtime-configuration-closure.md#task-1-验证记录与剩余责任)一致。 |
| capture 组件集成 | 851 项通过、5 项失败；失败均为[原错误计划](2026-10-09-camctl-result-errors-and-report-projection.md)登记的应急错误格式缺口。 |
| 根 C host 组合 | 四条新演示链与三条原 C host 链共同运行，7 项通过；验证完整文件领取、源摘要、报告导入与累计 ACK。 |

诊断位于工作区忽略的 `.superpowers/sdd/2026-10-10-camctl-real-camera-demo/`，最终记录使用 `t8-final-*.log`。软件演示的增量门禁已经通过；第一版整体仍保留上述十项既有失败，真实设备与 ARM Linux 验收尚未执行。

### 长读取期间的推进与收场实施细节

T8 长读取验证（2026-10-10，容器，Python 3.11.16）：四条 C host 软件组合覆盖两款相机的录像、原生延时、独立取回、源摘要校验、文件领取、报告导入和 ACK。延时恢复保留原 START、发送时间及目录基准；取回原片期间，同设备后续录像的到期停止和报告仍推进。源身份与读取票据分别使用原稳定文件身份和副本行号；路径定位与身份严格一致。取回独立推进及实际收场的单元分区 7 项、源身份及驱动单元 42 项通过；完整组件与根组合结果见上表。

T8 的长读取用例已经证伪正常装配：取回流程等待整份设备文件后才返回，会话逐个等待流程，因而下一轮拍摄停止和报告未推进。修复保持现有异步业务契约，不改变读取结果、业务取消或恢复语义。建议在 bootstrap 装配中让取回流程独立推进，并接入现有会话 `local_work` 收场接口；数据库连接由实际流程持有到读取及结果保存完成后关闭，不将连接交给已经返回的调用者清理。

| 取回流程的实际状态 | 下一轮推进与会话收场 |
| --- | --- |
| 尚无任务 | 启动一项流程，登记本地实际责任，然后立即让出调度；快速完成的流程可以直接消费结果 |
| 任务仍在推进或等待读取 | 保留同一项任务及其连接，不重复启动；拍摄、停止、报告和其他流程继续推进 |
| 任务已经正常完成 | 先消费原结果并解除该责任；下一轮才可以开始新的取回推进 |
| 任务产生错误 | 原异常进入现有会话错误分类，禁止当作空闲或成功；先按既定实际读取拥有者收场，再关闭连接 |
| 正常结束会话 | 不取消读取，等待原任务和结果保存；完成后再收场文件维护责任 |
| 致命错误、时钟受限或协程退出 | 停止新流程并取消原等待，实际读取仍由既有文件拥有者停止、等待及保存；新后台包装不得提前关闭连接或补造业务取消 |
| 收场等待者被取消 | 保留原实际任务，之后沿同一责任继续等待，不遗弃任务或重复读取 |

落实顺序：先保留 C host 长读取反例；再用无 IO 单元测试验证单任务、完成/失败交付、取消及实际清理等待；接入正常装配及组合 `local_work`；最后重跑四条根组合、原 C host 组合和受影响 bootstrap/session/outputs 门禁。仅把取回端口替换为瞬间读完的替身不能满足该门禁。

### T8 调度与实际收场的闭合步骤

2026-10-10 的总体只读评审指出三处既定契约尚未闭合：后台取回自停时可能丢失实际保存错误；工具调用的等待协程取消时没有等待实际进程及输出结束；一款相机的长调用会阻塞另一款相机下一轮停止。完整 bootstrap 回归在 322 项通过后主动中断，未形成门禁结果。下面的步骤属于 T8 的既有异步推进和实际收场要求，不增加设备故障范围。

**确定的契约：** 等待取消不代表实际执行结束。真实退出、输出、设备观察和原保存申请必须沿原身份交付；不能由取消推导未派发、业务取消、再次 START 或补造 STOP。数据库连接和文件资格必须持有到实际责任解决。不同设备的正常推进、报告和取消不得等待无关设备的长调用。

**建议的实施顺序与边界：** 以下内部组织可以根据实际数据流调整，行为分类和验收条件必须保持。

1. 先修复后台结果交付。文件拥有者保留原取消及结构化实际错误；后台任务保存可消费的实际结果，交付一次才释放句柄。组合收场先结束所有已有责任，再交付失败。
2. 再修复受管工具取消。使用原终止流和同一次退出、输出观察，重复取消不重复信号或重置宽限。追踪 `execute_tool`、ADB transport、响应解释、目录读取、控制、停止、结果分页、摘要和删除；实际结果在原消费者解释、保存后才能撤销等待。READ 继续使用既有读取拥有者，不创建第二次读取。
3. 最后让拍摄按设备跨轮推进。建议在现有 `capture_flow` 中保留各设备的任务，每项任务独立持有连接，并接入既有 `local_work`。恢复边界、目录基准、结果页、原保存申请和 operation key 跨轮保留；审计正常调度、取消、残留和收尾入口，禁止同设备重复推进。

后台收场按下表处理。表中的实际错误包括读取结束、文件关闭及原结果保存错误。

| 实际任务状态 | 拥有者已请求自停 | 收场等待者被取消 | 结果与责任 |
| --- | --- | --- | --- |
| 尚未结束 | 任意 | 否 | 等待原任务，不关闭连接或释放句柄 |
| 尚未结束 | 任意 | 是 | 只取消本次等待，原任务及未交付结果继续保留 |
| 成功或普通失败 | 任意 | 否 | 原结果或原异常交付一次，再解除责任 |
| 干净取消 | 是 | 否 | 实际收场完成后可以结束本地责任 |
| 取消并伴随实际错误 | 是 | 否 | 保留并交付结构化实际错误，不能解释为干净取消 |
| 取消 | 否 | 否 | 保留取消语义，不解释为成功 |
| 已完成但结果尚未交付 | 任意 | 是 | 原结果仍可由后续收场消费，不因任务已经结束而丢弃 |

工具调用按实际阶段处理，不以等待者取消代替进程事实。

| 取消时的实际阶段 | 必须保持的动作与结果 |
| --- | --- |
| 尚未可靠取得进程句柄 | 解决原启动责任；启动竞争中取得句柄后仍须收场，只有可靠未启动才能直接结束 |
| 进程运行，尚未终止 | 请求原终止一次，等待原宽限及必要强制终止，然后确认退出和输出结束 |
| 已退出，输出尚未结束 | 不再发信号，等待原输出读取结束 |
| 已进入终止或宽限 | 沿原终止责任等待，重复取消不增加信号、不延长期限 |
| 原结果已经形成，尚未解释或保存 | 保留原退出、输出、观察、错误和保存责任，完成原结果交接后才撤销等待 |

基准读取在 START 前结束时，实际明确错误仍按 `baseline_read_failed` 保存且 START 为零。原 START 意图已经保存或可能派发时，继续原票据、累计次数和恢复责任；不得借取消重新收基准或再次发送。停止、列表、摘要和删除继续各自原预算与重试规则。

按设备推进时，无任务且允许工作才能开始一项任务；任务进行中保留同一任务，其他设备继续下一轮；成功结果先消费，之后允许下一轮。任何已完成错误先交付并停止新业务，再收场其他进行中的任务。停止新业务后不创建任务。工作量必须包括已完成但尚未交付的结果。进入受限状态前先收场普通任务，正常结束则等待完成而不主动取消。

**红灯、实施与阶段门禁：** 每步先加入能证伪上述分区的最窄单元测试并取得失败，再实施。第一步补充取消原文件等待、实际执行或保存随后失败，以及收场等待取消与结果完成竞争的用例。第二步用 `ManagedProcess` 替身验证启动、退出、输出与重复取消，再以真实前台进程证明 PID 已回收、输出读取任务已结束；基准场景证明 START 为零，已发 START 场景证明原事实可保存且不重复派发。第三步分别挂起 A 的基准、`COMPLETED` 启动和结果分页，推进 B 到录像截止，证明 STOP 恰好一次且报告、取消仍能推进；再覆盖保存失败、会话退出和恢复入口。实际进程、文件与保存责任必须先于连接和锁结束。

每步通过适用专项后提交。最终顺序运行受影响单元及 operations、devices、bootstrap、session、outputs 组件目录，并复验四条新根链和三条原 C host 链。已有应急错误格式缺口继续在原责任计划跟踪；真实设备期限和实际响应仍由 T9 验证。

第一步验证（2026-10-10，容器，Python 3.11.16）：后台与文件拥有者专项 263 项单元测试通过，真实文件任务集成文件 12 项通过。取消原等待后，实际保存异常沿结构化异常原因保留；后台保存原任务的实际结果，交付后才解除责任。真实源文件关闭和数据库保存失败的组合证明连接最后关闭，原保存键及源文件仍保留。

第二步验证（2026-10-10，容器，Python 3.11.16）：operations、capture、devices 单元专项 1301 项通过，operations 完整集成 200 项、devices 完整集成 84 项通过；真实启动、准备和多步派发专项 15 项通过。受管工具持有原启动、终止、退出及双管道读取，等待取消不再早退；业务拥有范围先解释和保存原实际结果，再传播取消。真实目录取消保存准备错误且 START 为零；设置取消不再发送 START，START 已发送时保留实际观察和原累计次数。分页取消后不读取下一页，残留停止、确认查询和清理调用使用同一交接边界。

第三步验证（2026-10-10，容器，Python 3.11.16）：后台拥有者与 session 单元专项 129 项、跨设备及实际取消收场专项 19 项通过；全部单元 4263 项通过、1 项跳过、2 条既有 asyncio 标记警告。每台相机的原任务跨轮持有独立连接；基准、完整延时启动和结果分页等待期间，另一台相机仍按十秒目标停止，取消和报告轮次继续推进。恢复入口只推进没有普通拥有者的目标设备，不消费进行中的原结果 holder。实际调用返回后重新读取已保存取消事实，录像放弃内容，延时摄影保留合格文件；可靠未启动照片结束原核实用途。会话错误与协程退出等待实际准备收场后才释放连接和锁，不补造业务取消或启动尝试。三步交付与 T8 的最终增量门禁均已完成，完整结果及既有失败边界见上表。

## T9 设备事实补齐与 ARM Linux 完整验收

设备采集进度（2026-10-10）：Action6 已取得工具存在性、内置目录原始输出，以及 Windows 上 `exec-out` 与 `shell -T` 的受控退出、分流和字节对照。相机的受控 NUL 输入证明已有 shell 的 `read -r -d ''` 能完整读取记录；文件适配使用该接口生成分页帧，共享 ADB 调用及所有消费者使用 `shell -T`。

静止目录比对通过：前后完整列举得到同样的八条路径；生产脚本分三页按顺序列出全部路径，与排序后的完整列举一致。分页依次以 `MORE`、`MORE`、`END` 结束，所有调用返回 `0`。完整列举显式保存并返回 `find` 的实际退出码；此前一次仅执行 `find` 的调用返回 `255`，原因尚未确定，失败记录继续保留。

文件访问已核实同一张 JPG 的长度和源端 SHA-256。`adb pull` 取得的本地副本为 `491520` 字节，摘要与源端相同。生产 `dd` 在相机内从头读取或跳过一个 `65536` 字节块后读取，实际退出码均为 `0`；两次摘要分别与完整源文件和本地对应部分相同。这些结果核实了相机命令，ARM Linux 原始字节通道及实际流读取仍待验证。

普通录像试录已取得模式、启动和停止的 `00` 响应样例；使用相机原有参数后新增 MP4、LRF 和对应的隐藏 `.trinf` 文件。新增 MP4 的本地副本与源端长度及 SHA-256 一致，均为 `29302405` 字节；用户确认播放器可以打开该副本。Windows `ffprobe` 检查取得 HEVC 视频流 `1920×1080`、`30000/1001` fps、时长 `8.875533` 秒，容器时长为 `8.896000` 秒；按既有原片检查规则采用容器时长。主机等待约 `10.147015` 秒与媒体时长的差异原因尚未确定，其余参数和设备结束依据仍待核实。调试机屏幕损坏，界面观察无法取得。完整范围见[录像试录记录](../../camctl/verification.md#action6-的-windows-普通录像试录2026-10-10)。

录像样例的 4K/30 fps、Pro 开启、Auto 曝光、手动白平衡 5200 K 和曝光补偿 0 EV 已分别取得 `00` 响应；五次本地退出码均为 `0`，stderr 为空。实际生效尚未确认，范围见上述录像试录记录。

D-Log M、Wide 和关闭增稳继续取得 `00` 响应；F2.8 两次及 F4.0 一次均返回 `e3`，本地仍退出 `0` 且 stderr 为空。请求与交接资料及生产映射一致，响应含义和原因尚未确定，两档光圈均保持未确认。高码率工具返回 `0` 并输出服务连接和注册日志，未提供设备码率生效确认。录像样例全部设置命令的原始返回已取得；响应解释、命令适用条件和设置生效继续由 T9 核实。

控制工具帮助调用 `dji_mb_ctrl -h` 输出 `Usage` 并退出 `1`，未提供 `e3` 含义或光圈命令前提。光圈问题已按用户要求整理至[资料询问文档](../../hardware/camera-control-questions.md#action6光圈设置返回-e3)，保留完整请求、实际顺序和三次响应，供后续取得接口说明；光圈能力继续保持未确认。

Windows 统一采集工具为 [Action6 录像诊断脚本](../../hardware/action6_record_probe.py)，将重新配置、十秒试录、目录差集、视频拉取、源与副本的长度及摘要核对、媒体属性采集合并为一次操作；每项保留原始调用，不重发光圈。每份视频只下载一次，下载前后分别取得源观测，独立保存长度和摘要变化；副本与后续源观测不一致时保留比对记录并报错，源查询失败也直接报错。这些诊断检查不提供设置生效、设备结束或产物集合确定依据。

Windows 重启后复测已执行，记录目录为 `action6/record-probe-20261010T113947.161059Z`。模式、八项设置和高码率调用通过脚本检查；START、十秒主机等待和 STOP 后取得新增 MP4。原检查仅因早期源长度 `64436061` 与副本长度 `64637726` 不同而报错，原源摘要与副本相同。后续独立诊断取得新副本及前后源观测，长度均为 `64637726` 字节，摘要均与原副本相同；媒体检查取得 HEVC `3840×2160`、`30000/1001` fps、容器时长 `8.875533` 秒。原检查没有执行媒体检查，后续诊断取得了媒体属性，两者分别保留。长度与摘要并非原子快照，实际变化时刻、文件写完与集合依据继续属于 T9 已有核验范围，详细数据见上述录像试录记录。

诊断工具验证（2026-10-10，容器，Python 3.11.16）：17 项响应和副本比对单元测试通过。实际采集器与 ADB、ffprobe 替身的七组流程通过：正常副本及早期长度变化而匹配后续源观测时完成；副本摘要不一致或后续源查询退出 `255` 时保存可用记录并报错；设置 `e3` 或启动前目录调用退出 `255` 时不发送 START；START 响应异常时发送一次 STOP 后报错。每个进入下载阶段的流程仅拉取一次；这些结果只验证诊断工具的软件分支，不代替 Windows 复测或设备完成依据。

Action6 原生延时采集工具复用上述脚本，默认选择既定 4K/30 fps、Auto、8 秒、1800 秒、仅视频预设。诊断顺序为延时模式 → 分辨率 → 所选完整预设 → Auto → START；一次完整 `6c` 同时配置间隔、时长和输出类型。前后分别记录内置及 SD DCIM，明确区分不存在、有效空目录和读取失败。START 只发送一次，阻塞调用已耗时间计入主机观察间隔；正常和异常路径均不发送未核实的延时 STOP。START 异常立即报告原错误及设备是否开始未知。具体操作及秒级试验见[统一延时采集](../../hardware/camera-demo-validation.md#action6-原生延时的统一采集)。

延时工具验证（2026-10-10，容器，Python 3.11.16）：新增计时、停止边界和目录分类测试先观察失败，再通过全部 29 项单元测试。实际采集器、ADB/ffprobe 替身及仅在验证启动器中替换的时钟完成 15 组软件流程：保留原七组录像分支，新增内置加不存在 SD、内置加可读 SD、有效空基准、两个范围均不存在、范围不是目录、设置异常、START 异常和采样时无新增 MP4。存储范围脚本在隔离的真实目录上执行本地 `sh`、`find` 和路径检测，核对存在、空目录、缺失及非目录的实际返回。已核实延时不发送 STOP，准备失败不启动，START 异常不重发；能力未激活。软件加速不作为真实时长或自然结束证据。

Windows 延时试验（2026-10-10）：用户执行 8 秒／30 分钟／仅视频诊断，四项设置收到预期 `00`；START 本地退出 `0`、返回 `00`，采集器耗时 `0.109` 秒。主机记录距 START 发起 `1800.0` 秒后采样；内置 DCIM 仍为相同的 14 条路径，SD DCIM 仍不存在，目录差集为 `[]`，脚本因未取得新增 MP4 报错。原始调用、前后目录及计时记录已经核对；实际启动、自然结束、文件写完和最终产物集合仍未知。详细范围见[延时采样记录](../../camctl/verification.md#action6-的-windows-原生延时采样2026-10-10)，诊断工具提供沿原基准执行的[只读复查](../../hardware/camera-demo-validation.md#action6-延时采样后的只读复查)入口。

Windows 只读复查（2026-10-10）：新采样 UTC 时刻与原 START 发起记录相隔约 67 分 26 秒；内置目录仍为同样的 14 条路径，SD 范围仍不存在，摘要保存无新增路径及未尝试视频检查。原模式消息 `000200e1` 发送 `02`，原预设消息 `0002006c` 发送 17 字节完整负载，已核对与交接资料和脚本一致。两次无新增文件的观察不能确定设备实际状态。下述秒级诊断记录与[资料询问项](../../hardware/camera-control-questions.md#action6原生延时-start-返回-00两次采样均无新增路径)分别保存软件工具及接口缺口；该款延时的设备完成契约留待新的 Action6 核验。

本次诊断工具补齐顺序：

- [x] 先用失败测试定义无新增路径、新增非 MP4、新增 MP4 的结构化采样结果，保持设备完成事实未知。
- [x] 在视频检查之前保存目录差集摘要；检查报错保留实际原因和已完成结果。
- [x] 沿原模式、绑定和完整基准增加只读复查，独立保存新观察，不派发设备控制或覆盖原记录。
- [x] 独占执行单元及真实采集器组合验证，核对无文件、晚出现文件、读取与副本检查失败、无效原记录和绑定不同；独立审查后提交。

只读复查验证（2026-10-10，容器，Python 3.11.16）：新增五项采样分类单元反例先观察失败，随后全部 34 项单元测试通过；[采集器组合测试](../../hardware/tests/integration/test_action6_record_probe.py)共 32 项独占执行通过。验证覆盖无新增路径、仅非 MP4、稍后出现 MP4、旧版无摘要记录、可靠空基准、原绑定及原始目录校验、多次复查保留原文件、复查不等待也不发送控制、一次拉取及前后源观测、目录／下载／源查询／媒体失败，以及摘要写入和替换失败。摘要部分写入、检查与失败摘要补存同时报错的两项反例先复现失败；修复后原检查错误、已完成的视频和完整摘要均按各自分区保留。独立审查已经核验原基准、只读派发和错误保存边界；这些结果不代替 Windows 后续文件观察或设备完成依据。

### Action6 延时设置的完整派发顺序

输入依据为[交接资料的三组延时任务](../../hardware/camera-control-handoff.md#原表列出的准备与设置顺序)及完整预设的接口含义。一次 `6c` 同时配置间隔、持续时间和所选输出类型；`settings_for` 返回的设置依次为延时模式、4K/30 fps、所选完整预设和对应曝光设置。每个任务从视频、RAW、JPEG 三种输出中选择一种，三组共九个完整候选，不生成资料外的参数组合。实际驱动按返回顺序执行，设置全部确认后才尝试一次 START。

| 候选输入 | 曝光前选择一次完整预设 |
| --- | --- |
| 30 秒／5400 秒，手动曝光，视频、RAW 或 JPEG | 所选 D69、D76 或 D77 |
| 25 秒／6000 秒，Auto，视频、RAW 或 JPEG | 所选 D97、D95 或 D96 |
| 8 秒／1800 秒，Auto，视频、RAW 或 JPEG | 所选 D111、D109 或 D110 |

任何设置返回错误或未确认，均保留实际错误和返回事实，不派发后续设置或 START。每一步派发前检查原资格和剩余准备期限；取消、窗口结束或期限耗尽时停止后续派发。START 返回未知时不重发。该设置顺序不提供设备实际开始、结束、文件写完或集合齐备的依据；Action6 延时 STOP 和正式能力仍待核实。30 秒诊断使用一次完整实验预设，仍以 Auto、8 秒间隔和仅视频为要求；原记录的只读复查恢复原参数，不重新配置或启动。

实施与验收顺序：

- [x] 先按九个合法候选验证一次完整预设及曝光顺序；实际控制入口覆盖所有设置错误、资格失效、期限耗尽和 START 未知。诊断反例验证四项设置、30／10／1800 秒负载及每项准备错误，历史复查继续只读。
- [x] 在候选命令的权威模块按所选完整预设查表，并使 Schema 接受手动曝光的三种输出；诊断工具按同一接口含义派发。普通录像及 OSMO 不在本任务修改范围。
- [x] 主执行者独占运行单元、组件、采集器和跨组件门禁，独立核对候选仍未激活、历史观察未改写、文档有效规则一致，登记验证并提交。

软件验证按本节的一次完整预设契约执行，记录见[延时设置顺序的软件验证](../../camctl/verification.md#action6-延时设置顺序的软件验证2026-10-10)。手工试验的实际请求、启动前后目录和媒体检查分别登记；START 原始返回、设备实际开始和完成依据继续属于本任务待补输入。

### Action6 秒级延时诊断

用户要求将原生延时持续时间缩短至 30 秒或更短，方便定位启动及输出问题。原表的三个 Action6 样例在负载偏移 5、6 的字节分别为 `18 15`、`70 17`、`08 07`，按小端整数解释分别对应 5400、6000、1800 秒；据此构造短时候选。该对应关系不证明设备支持任意秒数，也不确定完整时长字段的宽度。试验保持 17 字节负载及原有 8 秒间隔、4K/30 fps、Auto、仅视频设置，仅替换这两个低位字节；正式驱动参数及能力登记不在此次试验范围内。

**诊断行为契约：** `--timelapse-duration-s N` 只用于新建 `--capture timelapse`，接受 1 至 1800 的整数，省略时使用原 1800 秒预设。这个范围限定诊断输入，不声明硬件合法范围。主机从 START 发起计时，到所选时长后采样；START 阻塞已耗时间计入，采样不提供设备自然结束的事实。四项设置依次保存原始调用，每次必须取得完整预期响应，任何设置或启动前目录失败均不发送 START；START 异常不重发、不发送未知 STOP。

每次新拍摄的 `capture-observation.json` 保存 `requested_params`、`preset_payload` 和 `preset_basis`。摘要中的 `params` 使用同一要求，1800 秒标记 `source_example`，其他值标记 `experimental_duration`；实际开始、自然结束、文件写完及集合确定均保持未知。只读复查恢复原要求，不以默认时长覆盖短时试验。

| 入口及原记录状态 | 前置检查与行为 |
| --- | --- |
| 新延时，参数为范围内整数或省略 | 构造对应完整负载，按四项设置顺序执行后尝试 START 一次 |
| 录像或只读复查带时长选项，或时长无效 | 在任何工具和设备调用之前报错 |
| 复查原记录没有三个新增字段 | 沿原版本固定的 1800 秒要求复查 |
| 复查原记录完整保存三个新增字段，且参数、负载、依据一致 | 沿原时长只读复查，不等待或发送控制命令 |
| 复查原记录只保存部分新增字段，或字段无效、不一致 | 在任何新设备调用之前报错，保留原记录 |

实施与验收顺序：

- [x] 先加入能证伪 30／10 秒负载、四步设置顺序、主机计时和摘要要求的单元及采集器组合测试，观察失败。
- [x] 使用标准库字节转换及既有采集器实施实验选项，完整记录要求和编码依据；审计新建、旧记录与短时复查入口。
- [x] 独占运行两个测试目录，覆盖设置、START、目录、视频检查及保存错误；核对文档和独立审查后提交。
- [x] 取得 Windows 独立开机后的 30 秒试验终端输出及完整摘要，登记所选负载、主机间隔和实际目录差集。
- [ ] 新的 Action6 到位后，按四项设置重新执行秒级诊断，保存 START 原始调用、前后存储范围和媒体属性；秒级值支持、正常结束及产物完成分别核验，不以候选编码、主机等待或 `00` 代替设备观察。

秒级诊断软件验证使用四项设置、一次完整 `6c` 和一次 START，覆盖 30／10／1800 秒、新旧记录只读复查，以及设置、目录、下载、源观测、媒体和摘要保存失败。执行环境、测试数字与审查范围统一见[延时设置顺序的软件验证](../../camctl/verification.md#action6-延时设置顺序的软件验证2026-10-10)。Windows 设备结果单独记录如下。

Windows 30 秒试验（2026-10-10）：用户确认正常关机再开机后执行候选，终端记录五项设置及 START 均通过预期 `00` 检查；摘要保留 8 秒间隔、30 秒时长及完整实验负载。START 阶段约 `0.813` 秒，距发起 `30.0` 秒后采样，结果为 `new_files=[]`、`no_new_mp4`，未尝试视频检查。本次完整终端和摘要已取得，尚未取得该次 START 原始记录、存储范围及后续只读观察；详情见[30 秒试验记录](../../camctl/verification.md#30-秒候选与完整设置顺序的试验)。两份硬件资料没有提供额外 Auto 设置、具体预热时长或就绪查询；实际设置、开始、结束与文件完成均保持未知，候选能力未启用。

Windows 手工试验（2026-10-10）：实际完整负载为 8 秒间隔、1800 秒、仅视频；预设和 START 的完整响应均为长度 `1`、数据 `00`。用户确认 START 后查看目录时出现拍摄前不存在的 MP4；准确采样间隔、启动前原始目录、文件取回和媒体属性尚未取得。用户报告调试机在延时拍摄一段时间后停止工作，无法完成任务。当前流程作为软件实施依据，完整延时、媒体属性及产物完成的真机验收留待新的 Action6；不要求故障机继续试验，也不把其提前停止当作正常结束。具体请求、四条路径及核验边界见[手工目录观察](../../camctl/verification.md#手工诊断的目录观察)。

上述通道调整的单元、devices 完整目录、bootstrap 专项和七条根软件链通过。实际结果与适用范围见[通道与文件工具核验记录](../../camctl/verification.md#action6-的-windows-adb-通道核验2026-10-10)。Action6 普通录像的运行契约与能力启用范围见下文；其他设置、延时响应和完成依据、固件、OSMO 360 II 及 ARM Linux 验收按各自接入条件继续。

### Action6 普通录像的完整演示接入

2026-10-11 的目标为真实 C host → camctl → Action6 普通录像 → 独立 `obtain` → 文件领取 → 报告领取与确认。用户手中的 OSMO 360 II 无法进入 ADB 调试模式，Action6 调试机也无法正常完成延时摄影；这两个场景分别等待 ADB 接入和正常新机。当前演示不要求继续试验它们。

内置 Action6 驱动按下述运行假设接入普通录像。正式发行物的 `camctl describe` 导出其普通录像能力，计划生成器仍按实际能力校验；软件验证从独立安装包加载真实驱动，仅在 ADB 进程边界替换相机通信。真实设备的既有录像证据来自独立诊断，完整 C host 真机链仍须目标主机验收。

已确定的输入行为见[Action6 的光圈要求](../../architecture/camera-parameters.md#action6-的光圈要求)和[码率要求](../../architecture/camera-parameters.md#action6-的码率要求)：普通录像可以分别省略光圈和码率，省略时不派发调整；显式值仍按原命令执行，错误或未确认时不能 START。当前预设采用 4K/30 fps、Auto、0 EV、Wide、关闭增稳、10 秒，保留相机原光圈和码率。

采用[Action6 普通录像的运行假设](../../architecture/camera-recording.md#action6-普通录像的运行假设)：正常设备下，START 的完整 `00` 确认开始采集，STOP 的完整 `00` 确认停止采集；停止后实际等待 5 秒，按本次文件全部写完继续核实。响应、规则和实际等待分别保存。该假设由用户选择，5 秒尚无真机验证依据；已有早期源长度变化及约 8.9 秒媒体时长继续保留为真实观察，不能补写为立即写完或十秒实际媒体达标。

以下步骤是本场景的接入顺序。参数与成功语义的硬性要求来自上述正式专题；文件组织和内部接口调整是建议，实施时沿实际数据流确定。

1. 先测试 Schema、命令派发、真实驱动控制及安装后样例对省略和显式光圈、码率的行为，再修改同源参数定义和模板。受理及冻结参数不得补造默认设置，OSMO 和其他必填字段的限制保持各自契约。
2. 明确 START 与 STOP 返回所保证的阶段，以及停止后的文件完成规则。如果完成依赖运行假设，应保存采用的规则、实际响应及等待事实；不得生成不存在的设备观察。停止采集时刻与文件完成时刻分开，取消、超时、保存未知、恢复及多录计算继续使用各自原始依据。
3. 将实际目录分页、MP4 识别和长度读取接入结果列举。框架向驱动显式传递已经保存的结束与完成依据；可靠末页只在该依据充分时确定集合，启动未知、停止未确认、页读取或元数据失败不能折叠为最终空集合。每页共享原剩余期限，原绑定、原页及错误一并保存；恢复不依赖进程内停止标志。
4. 本次任务工厂固定实际内置存储范围 `/mnt/media_rw/emulated/DCIM`，不存在的 SD 目录不解释为可用空目录。只把有规则支持的 MP4 登记为正式录像产物；LRF 和临时文件不凭同名推导用途。复用既有分页、读取及摘要工具，明确实际格式、文件完成声明和摘要调用期限。
5. 用未知响应、拒绝、完成等待中断、未完成结果、分页错误及恢复反例先证明门禁，再实现真实登记。正常发行版只导出具备完整正式契约的能力；独占执行受影响单元、组件集成、安装后消费者和 C host 软件链后，交付可独立安装的包、配置、能力说明及预设。
6. 在 ARM Linux 核验控制退出状态、独立 stderr、目录帧、元数据、摘要及偏移读取的原始字节。随后从 C host 执行完整链，保存录像与文件完成依据、原文件关联、领取副本大小及摘要和报告结果。软件门禁通过与真机演示完成分别登记。

接入分区须覆盖以下组合，不能以某一成功样例代替完整判断：

| 已保存的采集与完成依据 | 本页结果 | 允许的结论 |
| --- | --- | --- |
| 开始或停止采集尚未确认，或文件完成规则未满足 | 可靠读取成功，包括空末页 | 保存实际条目及页结束；不保证文件完成或集合齐备 |
| 采集已经结束且文件完成规则满足 | 可靠读取非末页 | 保存本页完成事实及游标，继续列举，不提前确定集合 |
| 采集已经结束且文件完成规则满足 | 可靠读取末页且必要元数据完整 | 保存最后一页及其完成依据，允许确定集合；仍由框架按原基准和产物要求判定结果 |
| 任一采集状态 | 目录、元数据、工具或保存失败／未知 | 保留原事实和实际错误，遵守各自失败与恢复规则，不登记可靠空集合或重新发送 START |

- [x] 完成省略光圈和码率的参数、设置和真实控制回归，以及安装后样例与受理验证。
- [x] 完成当前 Action6 配置、模板、执行说明和入口范围同步，保留其他软件候选及历史证据。
- [x] 明确并实现正式启动、停止和产物完成契约，覆盖实际响应及假设的保存和恢复边界。
- [x] 实现有持久化完成上下文的真实结果分页、内部存储范围和文件工具接线，并通过软件门禁。
- [ ] 完成 ARM Linux 通道与完整 C host 真机演示，记录实测结果后才能声明当前演示就绪。

软件验证（2026-10-11）：全部单元 `4501 passed, 1 skipped`；bootstrap 完整目录 `940 passed, 1 skipped`，contracts 完整目录 `192 passed`，原根组合七项通过。capture 完整目录 `917 passed, 5 failed`；history 完整目录 `195 passed, 10 failed`。两个失败目录分别在干净源码快照中复现相同用例及原因，缺口涉及应急错误详情和历史事件验收映射；两个目录均未全部通过。独立安装的内置 Action6 驱动经真实 C host 完成十秒录像、实际五秒等待、独立取回、文件与报告领取、客户端导入及 ACK，专项一项通过，耗时 29.75 秒。实际范围及既有失败见[验证记录](../../camctl/verification.md#action6-普通录像接入的软件验证2026-10-11)。ARM Linux 与真实相机尚未执行。

#### 文件完成依据的持久化闭合

运行假设的文件完成事实必须引用已经可靠保存的结果页，不能将主机等待保存为设备直接保证。已确定的行为契约由[文件完成依据](../../camctl/database/file-fields.md#设备文件)及[等待事实格式](../../camctl/database/history-formats.md#停止后文件完成等待的记录)定义；以下内部组织是实施建议。

| 原录像定义和文件观察 | 允许的完成保存 |
| --- | --- |
| 普通录像没有 `file_completion_wait_ms` | 保持驱动原声明的完成依据 |
| 普通录像要求等待；原结果页可靠，同活动、原 STOP 和固定等待值匹配，等待满足，文件条目完整且大小可靠 | 使用 `STOP_RETURN_AND_WAIT`，保存活动、结果页及 STOP 引用和该条目的实际观察 |
| 同页后来发生其他文件的元数据错误，但本文件已具备上述完整事实 | 保留本文件完成；错误和集合未确定分别保存 |
| 原等待未完成、提交未知、引用不匹配，或本文件未完整、大小未知 | 不取得本文件完成资格；先核实原保存或继续有限结果核实 |
| 文件已经完成，再次取得合法观察且大小相同 | 保留原完成依据，不追加重复完成事实 |
| 文件已经完成，大小或原键完整申请矛盾 | 拒绝，保留原事实及诊断 |

实施顺序如下：

1. 对完成保存的首次写入、原键核实、事件校验和分页消费者先写反例。覆盖错误活动、错误 STOP、错误结果页、未满等待、大小或文件身份不符，以及同页部分失败保留已知文件。
2. 在唯一内部枚举来源登记完成分区 `STOP_RETURN_AND_WAIT`。扩展类型化保存输入和事件证据，复用现有原页及停止历史读取；写事务和历史校验读取同一原事实，不查询设备或使用新配置补齐。实际等待页的来源字段为 `None`，后续复用页直接引用首次实际等待页；同轮分页、重试和重启均保持原来源，拒绝未来页、复制页和不同等待事实。
3. 在分页结果进入文件注册的共同入口选择该依据。只有原可靠页支持该完整文件时才能形成保存责任；保存未知保留原完整申请及键，不重新列举。
4. 新的文件完成事件保存完整元信息申请，区分省略字段与显式提供同值；原键重送比较原完整事务后状态、原申请及原时刻。覆盖合法状态转换中未改变列被过滤的场景，旧事件仍能读取和回放。重启与历史回放保留同一完成依据而不执行等待。之后顺序运行最窄测试、capture／history／contracts 组件及实际安装版 C host 完整链。

禁止用修改枚举文案代替来源核对，也不将某个文件读取失败推广为其他已知文件事实失效。源历史输入和内部辅助接口按现有事务结构选择，不能放宽事件或模块依赖门禁。

**前置输入：** 当前只补齐 Action6 普通录像的实际资料和完成契约，详情见[设备输入清单](../specs/2026-10-10-camctl-real-camera-demo-design.md#最后阶段需要采集的设备资料)。Action6 延时和 OSMO 的实际输入留待上述条件满足；此前 T1—T8 的四条软件链保留各自验证范围。

**建议文件：** 双相机驱动的实际响应解释器、T1 定义、命令测试和 device/capture 集成测试；更新 `docs/hardware/camera-control-handoff.md`、`camera-demo-validation.md`、`docs/camctl/integration-readiness.md` 和 `verification.md`。技术资料以实际适用固件和命令为范围，保密材料遵守根 AGENTS 的抽象记录边界。

**接口：** `ResponseInterpreter.interpret(raw: RawToolOutcome) -> InterpretedFacts` 消费实际响应，返回实际观察、完整错误和效果；T1 的任务工厂据已核实的结束控制及完成方式形成正式契约，T7 同源登记后再导出。无需增加新的公共动作类型。

- [x] **先写失败测试：** 以实际成功、拒绝、错误和无法解释样例测试当前 Action6 普通录像的设置/start/stop；证明没有匹配事实时保持未知。完成定义覆盖可靠开始、主机结束、文件写完/集合依据和实际必要产物。其他候选和组合按实际适用范围后续启用，未知组合不导出；样例数值不自动成为合法组合。
- [x] **运行红灯并实现解析：** 先运行对应 unit/devices 及 devices/capture 集成用例，确认解析或任务定义缺口；补齐真正需要的响应通道、文件访问和必要等待余量。不能仅用退出码 0、瞬时大小不变或文件出现保证实际启动、停止和全部写完。
- [x] **完成软件验证并提交演示发行物：** 顺序运行所有受影响单元、组件目录和 T8 根组合，检查已激活能力的实际消费者。正常 Action6 完整链通过，既有失败按上文及对应专项保留。提交演示增量，提供目标主机可独立安装的发行物、能力说明、配置和预设计划；本项不表示既有失败已经修复或第一版软件门禁全部通过。
- [ ] **执行真实时长：** ARM Linux 接入 Action6，运行 10 秒普通录像；经 C host 完成 submit → capture → 正式输出 → obtain → ready → claim，并验证完整文件和报告。不得缩短任务时长后声称对应预设已验收。
- [ ] **保存结果并提交：** 记录主机/ADB/固件、实际结束和完成依据、原文件关联、领取文件大小/摘要及报告结果；确认源文件保留。当前 Action6 普通录像链全部成立才声明演示就绪；Action6 延时、OSMO 及未支持选项分别列出后续条件。硬件发现需改代码时先增加回归，再修改、重跑受影响软件门禁、重验受影响真机场景并提交。

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
