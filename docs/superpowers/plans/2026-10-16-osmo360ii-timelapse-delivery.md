# Action6 与 OSMO 360 II 延时样例及 OSMO 验收执行计划

> **执行者要求：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 实施；先读[统筹计划](2026-10-16-osmo360ii-timelapse.md)。按软件、安装版及真实设备分别记录结果。

**目标：** 将两款共同的最短时间目标交付到候选样例、安装包及客户端，保留各型号曝光和输出；以正式内置 OSMO 合同完成软件整链，再在 ARM 与真实相机完成拍摄、独立取回和报告确认。

**架构：** 演示输入从权威样例进入包资源，计划生成器消费安装版的实际能力说明。复用 C host、既有取回和报告链；测试合同链与正式内置合同链分别验证。

**技术栈：** Python 3.11、uv、wheel、现有资源同步器、客户端、C host、ADB、SQLite。

**规格：** [最短实机目标](../../hardware/camera-demo-validation.md#osmo-360-ii-的最短实机演示)、[取回正式产物](../../architecture/obtaining-outputs.md)、[文件交接](../../architecture/file-handoff.md)、[报告与累计确认](../../architecture/status-reports.md)、[跨组件说明](../../../tests/integration/README.md)。

## 全局约束

遵守[统筹计划](2026-10-16-osmo360ii-timelapse.md#全局约束)。最短演示样例不是参数默认值，也不替代设备证据；OSMO 正式合同和软件进度见[完成契约计划](2026-10-18-osmo360ii-capture-completion.md)。原始源文件保留，删除仅由明确清理动作触发。

## 上下文与建议文件

- 权威样例：`docs/camctl/examples/camera-demo/action6-timelapse.json` 和 `osmo360ii-timelapse.json`；生成器 `prepare-plan.py` 要求实际能力说明存在唯一匹配类型，不以候选规则兜底。
- 建议新增同目录 `osmo360ii-config.toml`，只定义该设备及需要填写的 serial，沿用户目录默认路径。已有部署合并设备项，避免替换已有设备、状态库和交接路径。
- 资源来源登记：`apps/camctl/scripts/sync_resources.py`；包内 `_resources` 为生成副本，禁止手工改写。
- 安装测试：`apps/camctl/tests/integration/bootstrap/test_package.py`、`test_distribution.py`。
- 注入合同的组合链：`tests/integration/test_real_camera_demo_roundtrip.py`、`camera_demo_fixtures.py`。通用 C host 回归：`test_camctl_c_module_roundtrip.py`。
- 正式内置链的参照：`tests/integration/test_action6_builtin_demo_roundtrip.py`；建议新增 `test_osmo_builtin_timelapse_roundtrip.py`。
- 现场入口：[相机演示说明](../../hardware/camera-demo-validation.md)、[独立安装和 C host 联调](../../../apps/host-demo/docs/camctl-integration.md)。

## E1 最短样例、候选软件链及安装资源

**前置：** P1/P2；采集命令实际派发用例分别需要 D1 对应型号的最短映射。纯样例、校验和打包测试可先进行；每款派发链分别记录，不因另一款缺口暂停其已满足条件的任务。

**交付接口：** 继续使用两款 `examples/camera-demo/*-timelapse.json` 资源名和 `prepare-plan.py` 命令接口；配置样例新增资源登记。测试合同通过已有 launcher 显式注入，生产登记不变。D1A 交付的 Action6 独立诊断资源另作同源核对，不作为生产能力凭据。

- [x] 先改两款安装样例及相应测试预期，均使用 `interval_s=1`、`duration_s=300`、仅视频；Action6 保留原 Auto，OSMO 保留手动 ISO 800。固定模式、分辨率、快门与白平衡分别由 D 的型号契约负责。最短值只作为具体样例，不额外限定驱动参数域或切换曝光。
- [x] 在 `test_distribution.py` 覆盖生成器对匹配能力、缺失能力、同类型不唯一及非法时间组合的结果；缺少能力时不生成待递交计划。沿精确 JSON 适配读取和编码样例，避免 Python 浮点类型进入精确参数校验。
- [x] 调整 `camera_demo_fixtures.py` 的两款延时演示输入来源，使其消费各自最短样例或同源派生输入，不再从首条命令候选或 `duration_s == 1800` 反推目标。用 `test_real_camera_demo_roundtrip.py` 的两个延时场景验证最短时间，两条普通录像链保持原职责。
- [x] 同步 `bootstrap/test_adb_camera_products.py` 等 Action6 候选产物测试：新请求只使用合法时间和有依据的映射，视频、RAW、JPEG 的必要产物与部分合格文件保留继续覆盖；旧5400/6000秒样例不能绕过新校验。分别验证 Action6 正式普通录像继续导出、正式延时仍关闭，候选测试合同不泄漏到生产登记。
- [x] 分别运行 `P apps/camctl/tests/integration/bootstrap/test_distribution.py -q` 和 `P tests/integration/test_real_camera_demo_roundtrip.py -q`，确认参数、资源或夹具接缝的目标失败；命令映射未闭合的运行场景停在其前置门槛，不削弱断言。
- [x] 更新两款权威样例、独立 OSMO 配置、资源登记和操作说明；重新生成资源。Action6 新诊断引用 D1A 的共同时间规则，旧秒级记录只保留为既有试验及只读复查。现有 OSMO TX2 十条命令及 610 秒观察安排仍属于既有记录；各新目标的负载和等待依据引用该型号的有效契约。
- [x] 顺序运行下列命令，核对安装资源与权威输入逐字节一致。D3 前保留正式 OSMO 能力关闭的断言；D3 完成后只对完整的延时类型更新该预期，OSMO 录像候选不随之启用。

```bash
apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --root "$PWD"
apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --check --root "$PWD"
P apps/camctl/tests/integration/bootstrap/test_package.py -q
P apps/camctl/tests/integration/bootstrap/test_distribution.py -q
P apps/camctl/tests/integration/bootstrap/test_adb_camera_products.py -q
P tests/integration/test_real_camera_demo_roundtrip.py -q
```

- [x] 检查 diff、复核注入边界并提交；Action6 与 OSMO 的候选派发链分别登记结果。记录清楚本项包含候选软件链，不能据此标注正式内置合同或真机通过。E2 在正式 OSMO 链之外仍回归两款候选软件链，Action6 延时的正式接入继续由原 T9 跟踪。

2026-10-16，E1 与 D1A 合入后的组合检查通过：两项同源生成检查、包资源逐字节检查及诊断帮助退出 0，硬件诊断单元 43 项、脚本集成 83 项通过。两款最短候选链、产物矩阵和安装生成器的软件结果见[组件验证记录](../../camctl/verification.md#延时参数与候选软件验证2026-10-16)。本阶段只交付候选软件及诊断资源；正式 OSMO 合同和设备完成依据仍按 D2—D4、E2—E3 验收。

## E2 正式内置合同的安装版联合验收

**前置：** P3、C3、D3/D4、E1；正式任务已具有完整合法契约。

**交付接口：** 安装版 `describe` → 同一 Schema 的客户端加载及导出 → 实际 C host → 正式 CLI/SQLite → 独立取回 → 文件与报告领取 → 客户端导入 → ACK。候选 launcher 不用于替代该链中的内置 OSMO 合同。

**当前状态：** E2 安装版 OSMO 联合链及对应软件门禁已通过；实际快照、结果与执行证据见[组件验证记录](../../camctl/verification.md#正式软件接入验证2026-10-18)。E2 的安装物、正式 `describe`、C host、客户端导入／ACK、恢复和替身边界按[完成契约计划任务 5](2026-10-18-osmo360ii-capture-completion.md#任务-5安装版正式业务联合链)及[跨组件测试说明](../../../tests/integration/README.md#osmo-360-ii-正式内置合同安装版联合链)维护；本节保留 E2 的验收范围，不重复登记软件进度。

验收范围|当前权威依据
-|-
安装版 wheel 的正式内置能力、资源及 `describe` 与源码的一致性；合同、工厂、解释器、结果读取器与登记确实来自 wheel。|任务 5、跨组件测试说明中的安装物来源和资源比对范围。
业务逻辑不可由测试注入替换；仅允许在 PATH 的 ADB 子进程边界和时钟端口替换。|任务 5、跨组件测试说明中的替身范围；不得用候选 launcher 或同时替换合同和传输的夹具完成 E2。
真实客户端计划、C host、安装版 CLI、SQLite、正式合同及响应／结果边界|任务 5、跨组件测试说明中的业务组合范围；不得以替换正式合同或业务传输结果的测试夹具代替。
普通录像与原生延时各自的 START、等待、STOP 和结果页顺序|10/18 完成设计、任务 5 及跨组件测试说明；受控时钟只证明软件路径，不表示真实设备计时。
独立取回、文件领取、真实客户端媒体导入、报告导入及累计 ACK|任务 5、跨组件测试说明及软件验证记录；媒体身份、长度和摘要沿各自交付事实核对。
取回中断、同请求恢复及设备替身范围|任务 5 与跨组件测试说明；合成媒体和受控时钟不提升为实机或媒体解码证据。

E2 软件门禁、安装版快照与实际执行记录只以当前完成计划和组件验证记录为状态来源。ARM wheel、锁定依赖、C host 及客户端版本的目标环境核对由 E3/任务 6 负责，不能由 E2 软件通过推导。

## E3 ARM 最短实机演示

**前置：** E2；D2 的真实设备契约及正常设备可用。按[最短演示目标](../../hardware/camera-demo-validation.md#osmo-360-ii-的最短实机演示)执行真实采集时长，不用软件时钟替代。

**交付：** 带日期、环境和适用范围的实际设备与 ARM 业务验收记录；不回填没有取得的观测值。

本节正式业务链之外，TX2 手工执行最短延时组合已有[用户现场整体确认通过记录](../../camctl/verification.md#osmo-360-ii-最短延时手工演示的现场验收2026-10-16)，仍只代表当时的手工请求确认。E3 的新代码 TX2／ARM 业务验收现已按[完成契约计划任务 6](2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)完成；现场场景、目标及取消终态和媒体交付范围见[TX2 正式业务验收记录](../../camctl/verification.md#tx2-正式业务验收2026-10-18-起)。Auto 1 秒／300 秒样片的实际采样间隔没有独立读回，工具默认响应上限仍保持未知；不据此扩大设备保证。该计划的验收范围映射指向任务 6，不维护第二份进度复选框。

**验收范围映射（不另行维护进度复选框）：**

范围|必须取得的证据|进度来源
-|-|-
目标环境与安装|沿原账户、配置、状态库和交接目录安装交付物；核对 Python／SQLite、ADB、型号、固件、serial 与存储，不重新初始化既有业务数据。|任务 6；实际部署证据见组件验证记录。
正式能力与客户端请求|从已安装发行物提取资源，以现场配置导出正式 `describe`；客户端加载能力后导出唯一新请求，并核对参数与时间编辑入口。|任务 6；运行规则见 10/18 完成设计。
真实拍摄及完成判定|经 C host 和 camctl 执行，按 D2 与完成设计记录设置、单次 START、适用 STOP、自然余量、文件等待及结果证据；无回读接口时不虚构设置回读。|任务 6；D2 保存原始设备特有证据。
独立取回与客户端确认|由真实拍摄动作身份创建独立取回，分别核对正式产物、交付身份、源／副本长度与摘要、可读性、客户端文件导入、报告导入及累计 ACK；源文件和状态库保留。|任务 6；实际结果只记入组件验证记录。
错误与恢复|保留原始调用和原请求责任，不自动重试或补发 START，不跨型号借用 STOP；核实低功耗中断、重启恢复、有限取消及目标／取消动作的独立终态。|任务 6；运行语义见 10/18 完成设计。
结案与状态同步|完成后仅更新当前计划、组件验证记录及本节引用；已确认和未完成范围分开记录。|任务 6 与组件验证记录。

## 审查重点与完成条件

E1 核对资源及候选边界，E2 核对正式内置合同与安装版整链，E3 核对真实设备和 ARM 业务。三项分别通过后，统筹计划才具备完整交付证据；用户若明确接受现场总体结果而不再回传材料，记录该接受范围和未归档事项，不继续索取同一批材料。
