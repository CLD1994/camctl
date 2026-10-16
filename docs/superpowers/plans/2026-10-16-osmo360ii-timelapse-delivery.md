# Action6 与 OSMO 360 II 延时样例及 OSMO 验收执行计划

> **执行者要求：** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 实施；先读[统筹计划](2026-10-16-osmo360ii-timelapse.md)。按软件、安装版及真实设备分别记录结果。

**目标：** 将两款共同的最短时间目标交付到候选样例、安装包及客户端，保留各型号曝光和输出；以正式内置 OSMO 合同完成软件整链，再在 ARM 与真实相机完成拍摄、独立取回和报告确认。

**架构：** 演示输入从权威样例进入包资源，计划生成器消费安装版的实际能力说明。复用 C host、既有取回和报告链；测试合同链与正式内置合同链分别验证。

**技术栈：** Python 3.11、uv、wheel、现有资源同步器、客户端、C host、ADB、SQLite。

**规格：** [最短实机目标](../../hardware/camera-demo-validation.md#osmo-360-ii-的最短实机演示)、[取回正式产物](../../architecture/obtaining-outputs.md)、[文件交接](../../architecture/file-handoff.md)、[报告与累计确认](../../architecture/status-reports.md)、[跨组件说明](../../../tests/integration/README.md)。

## 全局约束

遵守[统筹计划](2026-10-16-osmo360ii-timelapse.md#全局约束)。最短演示样例不是参数默认值，也不使尚未闭合的候选变为可执行能力。原始源文件保留，删除仅由明确清理动作触发。

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

- [ ] 先改两款安装样例及相应测试预期，均使用 `interval_s=0.5`、`duration_s=300`、仅视频；Action6 保留原 Auto，OSMO 保留手动 ISO 800。固定模式、分辨率、快门与白平衡分别由 D 的型号契约负责。最短值只作为具体样例，不额外限定驱动参数域或切换曝光。
- [ ] 在 `test_distribution.py` 覆盖生成器对匹配能力、缺失能力、同类型不唯一及非法时间组合的结果；缺少能力时不生成待递交计划。沿精确 JSON 适配读取和编码样例，避免 Python 浮点类型进入精确参数校验。
- [ ] 调整 `camera_demo_fixtures.py` 的两款延时演示输入来源，使其消费各自最短样例或同源派生输入，不再从首条命令候选或 `duration_s == 1800` 反推目标。用 `test_real_camera_demo_roundtrip.py` 的两个延时场景验证最短时间，两条普通录像链保持原职责。
- [ ] 同步 `bootstrap/test_adb_camera_products.py` 等 Action6 候选产物测试：新请求只使用合法时间和有依据的映射，视频、RAW、JPEG 的必要产物与部分合格文件保留继续覆盖；旧5400/6000秒样例不能绕过新校验。分别验证 Action6 正式普通录像继续导出、正式延时仍关闭，候选测试合同不泄漏到生产登记。
- [ ] 分别运行 `P apps/camctl/tests/integration/bootstrap/test_distribution.py -q` 和 `P tests/integration/test_real_camera_demo_roundtrip.py -q`，确认参数、资源或夹具接缝的目标失败；命令映射未闭合的运行场景停在其前置门槛，不削弱断言。
- [ ] 更新两款权威样例、独立 OSMO 配置、资源登记和操作说明；重新生成资源。Action6 新诊断引用 D1A 的共同时间规则，旧秒级记录只保留为既有试验及只读复查。现有 OSMO TX2 十条命令及 610 秒观察安排仍属于既有记录；各新目标的负载和等待依据引用该型号的有效契约。
- [ ] 顺序运行下列命令，核对安装资源与权威输入逐字节一致。D3 前保留正式 OSMO 能力关闭的断言；D3 完成后只对完整的延时类型更新该预期，OSMO 录像候选不随之启用。

```bash
apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --root "$PWD"
apps/camctl/.venv311/bin/python apps/camctl/scripts/sync_resources.py --check --root "$PWD"
P apps/camctl/tests/integration/bootstrap/test_package.py -q
P apps/camctl/tests/integration/bootstrap/test_distribution.py -q
P apps/camctl/tests/integration/bootstrap/test_adb_camera_products.py -q
P tests/integration/test_real_camera_demo_roundtrip.py -q
```

- [ ] 检查 diff、复核注入边界并提交；Action6 与 OSMO 的候选派发链分别登记结果。记录清楚本项包含候选软件链，不能据此标注正式内置合同或真机通过。E2 在正式 OSMO 链之外仍回归两款候选软件链，Action6 延时的正式接入继续由原 T9 跟踪。

## E2 正式内置合同的安装版联合验收

**前置：** P3、C3、D3/D4、E1；正式任务已具有完整合法契约。

**交付接口：** 安装版 `describe` → 同一 Schema 的客户端加载及导出 → 实际 C host → 正式 CLI/SQLite → 独立取回 → 文件与报告领取 → 客户端导入 → ACK。候选 launcher 不用于替代该链中的内置 OSMO 合同。

- [ ] 先在建议新增的 `test_osmo_builtin_timelapse_roundtrip.py` 编写安装版场景：在源码目录之外构建和安装当前 wheel，使用明确 OSMO 配置，真实内置 `describe` 必须导出完整延时参数类型；与源码运行结果及安装资源一致。
- [ ] 该链只在真实 ADB 进程边界提供受合同约束的命令响应和源文件。建议新增 `tests/integration/_osmo_builtin_clock_entry.py` 并复制到安装版测试目录，作为独立的只注入时钟入口；从 wheel 导入正式 CLI，仅让 `lifecycle.SystemClock`、capture 的 `wall_us/monotonic_ns`、obtain 与 cleanup 的 `occurred_at/monotonic_ns` 共用同一个受控时钟源。可复用纯时钟辅助逻辑，不调用同时替换合同和传输的 `install_camera_demo`。
- [ ] 核对安装入口中的正式 `CameraContract`、任务工厂、响应解释器、结果读取器及内置登记均来自 wheel 且没有被替换；时钟推进只通过上述端口，不修改冻结目标时长或业务状态。运行记录明确该软件链的时间替身范围。
- [ ] 经客户端验证并导出最短参数，C host 递交后核对原始/生效参数的精确数值、固定执行定义、目录基准及唯一 START。非法等值和反向大小也由浏览器/后端及 CLI 同源拒绝，不产生设备调用。
- [ ] 先运行 `P tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q`，确认目标缺失会使测试失败；据失败定位实际接缝，再完成必要接线，不用测试注册替代生产登记。
- [ ] 验证拍摄产物的原动作关联及完整终态，生成独立 `obtain` 请求，C host 领取副本后核对源与副本长度及 SHA-256；源文件仍保留。报告导入和 ACK 使用客户端可靠接受的报告身份，不把文件领取等同于用户收到。
- [ ] 增加原 START 后中断、取回中断及同请求重送的必要场景，核对原基准、原输入、单次设备启动和独立交付身份。已有通用机制直接复验，失败修复仍归相应领域。
- [ ] 顺序执行新增内置链、四条候选链和通用 C host 链；最后执行 bootstrap 安装回归及公共协议检查。当前代码无关的目标资源、物理断电和性能验收不并入软件门槛。

```bash
P tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
P tests/integration/test_real_camera_demo_roundtrip.py -q
P tests/integration/test_camctl_c_module_roundtrip.py -q
P apps/camctl/tests/integration/bootstrap -q
node scripts/check-protocol.mjs
```

- [ ] 记录真实环境、日志和退出码，并明确 ADB/时钟替身范围。检查 diff、独立复核后提交，交付配套 wheel、锁定依赖、C host 及客户端版本。

## E3 ARM 最短实机演示

**前置：** E2；D2 的真实设备契约及正常设备可用。按[最短演示目标](../../hardware/camera-demo-validation.md#osmo-360-ii-的最短实机演示)执行真实采集时长，不用软件时钟替代。

**交付：** 带日期、环境和适用范围的实际设备与 ARM 业务验收记录；不回填没有取得的观测值。

- [ ] 使用普通运行账户安装配套交付物，核对实际 Python/SQLite、ADB、相机型号/固件、serial 和存储。已有部署沿原状态库与交接目录；只有首次初始化才执行 `init`，不重新初始化业务数据。
- [ ] 从已安装包提取资源，使用现场配置执行正式 `describe`，将说明交给现场客户端加载；其表单须保留时间关联选择及曝光编辑。由客户端导出唯一的新拍摄请求，检查最短目标及固定设置要求。
- [ ] C host 递交并由 camctl 执行；按 D2 的响应、等待及结束契约记录真实控制过程，核对实际设置和完成依据。无逐项回读接口时，记录已经明确接受的确认依据及其范围，不自行生成保证。
- [ ] 领取拍摄报告并由客户端导入，取得真实的原拍摄动作身份；用该身份新建独立取回请求，领取视频副本和相应报告，分别核对交付身份、长度、摘要及视频可读性。
- [ ] 客户端导入报告和视频后，以实际可靠接受的累计报告身份递交 ACK，核对主机确认水位；报告确认、视频收到和相机源文件保留分别记录。
- [ ] 任何设置错误、启动未知、断连或后续错误都先保留原始调用及原请求责任，不自动重试或补发 START，不改用其他型号 STOP。沿合同核实原任务，已经生成并符合条件的产物按原规则保留。
- [ ] 将设备事实与 ARM 业务结果写入 `docs/camctl/verification.md`；操作入口引用实际记录，未完成项只在本领域维护。未取得完整证据时保留实际通过范围，不将软件整链结果升级为实机验收。

## 审查重点与完成条件

E1 核对资源及候选边界，E2 核对正式内置合同与安装版整链，E3 核对真实设备和 ARM 业务。三项分别通过后，统筹计划才具备完整交付证据；用户若明确接受现场总体结果而不再回传材料，记录该接受范围和未归档事项，不继续索取同一批材料。
