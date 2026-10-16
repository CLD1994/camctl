# 软件验证与实施顺序

[设计入口](README.md) · [实现总览](implementation.md)

本页定义 camctl 实现的测试分工和跨职责验证要求。各业务专题中的决策表及验收场景仍须逐项落实。具体阶段与门禁见[第一版实施路线图](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)，接口、测试文件、失败用例和执行步骤见其引用的[模块计划](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口)及[跨组件集成计划](../superpowers/plans/2026-09-30-camctl-integration.md)。进入代码实施前核对所需前置交付和实际代码。

各带日期的记录保存当时的环境、范围和结果，记录中的失败及尚未完成事项不自动代表当前状态。后续修复与当前遗留项从路线图核对；本次[文档复核发现的实现缺口](../superpowers/plans/2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)也按该入口跟踪。部署和设备证据的当前交接范围见[本页末节](#部署交接与待核验项)。

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

阶段命令、日志和退出码保存在各工作区忽略目录 `.superpowers/sdd/2026-10-16-osmo360ii-timelapse/`：主树 `integrated-d1-unit`、`integrated-d1-dispatch`、`integrated-e1-p3`、`integrated-d1a-unit`、`integrated-d1a-integration`，交付树 `task-E1-chain-report.md` 及 `logs/e1-chain-verify-*`、`logs/e1-chain-review-products.log`，诊断树 `task-D1A-report.md` 及 `d1a-fix-logs`，客户端树 `task-C3-final-*`。原阶段日志与本次复验分别保留。

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

以上命令退出码均为 `0`，stderr 日志为空。单元跳过验证 Linux 之外的守护行为；bootstrap 跳过依赖 Windows 文件系统的大小写别名。两条警告来自已有同步测试的 asyncio 标记，未修改对应测试。完整命令、红绿次序、日志和退出结果保存在工作区忽略的 `.superpowers/sdd/2026-10-13-camctl-sw01-acceptance-clock-status/sw01-report.md` 及其 `logs/`；早期一轮受理回归未捕获退出码，最终扩展后的完整目录已有退出码 `0` 的记录。

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

以上命令退出码均为 `0`，stderr 日志为空。跳过范围与 SW-01 相同；三条警告均为已有同步测试携带 asyncio 标记，日志目录新增的第三条来自 `test_handler.py::TestTwoProcesses::test_two_processes_share_file`。完整命令、红绿次序、输出和退出结果保存在工作区忽略的 `.superpowers/sdd/2026-10-13-camctl-sw02-log-copy-diagnostics/sw02-report.md` 及其 `logs/`；最终目录记录为 `084`、`085`、`086`、`092`，最后仅改 bootstrap 的工厂检查后，重新执行了受影响的完整目录。随后仅新增双诊断出口失败组合，生产文件保持原版本；`093` 新用例及 `094` 完整诊断模块分别通过，均退出码为 `0`。

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

以上命令退出码均为 `0`；完整目录的 stderr 日志为空，跳过及已有警告范围与 SW-02 相同。窄验证后生产及测试保持冻结，完整目录覆盖同一最终快照。命令、红绿次序、输出、耗时、退出码和文件摘要保存在工作区忽略的 `.superpowers/sdd/2026-10-13-camctl-sw03-marker-temp-cleanup/sw03-report.md` 及相邻日志；完整目录记录为 `sw03-full-*-execution.json`。早期测试构造或启动器错误与有效测试结果分别记录。

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

完整命令、红绿与夹具错误区别、stdout／stderr、时间、退出码和文件 SHA-256 保存在工作区忽略的 `.superpowers/sdd/2026-10-13-camctl-sw04-startup-log-sweep/execution-index.md` 及 `logs/full-gate/`；最终启动复验对应 `integration-bootstrap-recheck.json`。24 个生产与测试 Python 文件在最终复验前后摘要一致。文档检查和提交核验由根代理收口。

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

所有完整目录分别启动 pytest 并串行执行。完整命令从仓库根使用 `apps/camctl/.venv311/bin/python -m pytest <目标> -q -rs`；实际命令参数、PID、UTC 时间、耗时、退出码及原始输出保存于工作区忽略的 `.superpowers/sdd/2026-10-14-camctl-sw05-session-lock-errors/resume-validation/`，最终文件摘要见 `final-gate-manifest.json`。有效红、旧测试预期同步及中间诊断遗漏分别保留，不作为最终通过测次。本次验证提供开发环境的软件证据，真实相机、ARM 部署及物理断电继续按各自计划验收。

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

命令从仓库根使用 `apps/camctl/.venv311/bin/python -m pytest <目标> -q -rs`，配置消费检查使用同一解释器运行 `scripts/check-config-consumption.py`。各完整集成目录分别启动进程并串行执行。完整命令、PID、UTC 起止时间、耗时、退出码、原始输出和文件 SHA-256 保存在工作区忽略的 `.superpowers/sdd/2026-10-14-camctl-sw06-bounded-read-discovery/`。软件证据与真实相机、ARM 部署及物理断电的证据分别维护。

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

本次软件门禁与上述 Windows 目录和文件访问实测分别记录；ARM Linux 的执行与字节核验仍待进行，候选相机能力未启用。诊断保存在工作区忽略的 `.superpowers/sdd/2026-10-10-camctl-real-camera-demo/t9-*.log`。

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
