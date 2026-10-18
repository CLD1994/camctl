# 跨组件集成测试

本目录用于客户端、camctl CLI 和 C 接入模块之间的真实协作验证。终端演示使用同一模块；组件内部测试放在各自目录，目前客户端测试入口为 `apps/client/tests`。

`test_device_capability_declarations.py` 在仓库外离线构建并安装 wheel 与锁定运行依赖，比较安装版和源码的正式能力导出，核对全部内置驱动、Hasselblad 两种正式源、同驱动的独立设备配置，以及任意驱动标识和无拍摄任务设备。真实 `init`、`submit` 与 SQLite 核对配置切换后旧执行定义保持，新请求使用新来源；同一份安装版 JSON 经根 Schema、wheel 资源、客户端实际加载、HTTP、参数表单和设备指南消费。非法删除声明的重载保留旧有效目录，旧说明缺字段仍为未知，有效空目录整体替换。设备工具边界禁止调用，不连接真实相机。

页面联合链使用两份实际安装版导出验证当前源变更、逐设备指南及动作／产物清理入口。非法字段和重复键重载失败后仍按旧有效目录展示；首次加载失败与有效空目录按未知保留入口。混合来源、原片、预览、修复成品和产物状态分别核对，草稿及已导出原请求保持不变。

本用例需要 Python 3.11、uv 离线缓存中的锁定依赖与构建工具，以及已按客户端运行说明安装的 Node.js 24 依赖和 Chromium。浏览器目标由用例先构建当前网页，再通过测试专用 `CAMCTL_TEST_CAPABILITIES_FILE` 注入安装版导出；客户端独立组件测试仍使用自己的合成夹具，不依赖 Python。

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_device_capability_declarations.py -q
```

`report-dependencies.test.mjs` 与 `report-dependency-conditions.test.mjs` 验证公共 Schema、数据库字段登记、规格 SQL 和检查器之间的接缝，包含错误登记反例与具体条件场景。运行入口见[检查脚本](../../scripts/README.md)；通过这些检查不表示真实 CLI 已经接入报告生成。

`test_database_history_sql.py` 在内存 SQLite 加载真实结构，验证来源及流程关联、清理状态矩阵、启动与残留停止的过期组合、独立文件历史元数据、两类目录和快照范围，并在有无统计信息时核对分页查询。它还读取统一运行库条件，验证检查脚本对主线版本、精确回移例外及非法版本输入的判定；这些版本样例不表示实际运行了所有 SQLite 版本。上述检查不代替生产入口、事件应用、历史恢复或报告字节一致性测试。

`event-transitions.test.mjs` 组合真实事件登记、SQL、枚举和报告依赖，验证列分类、分支和状态覆盖、历史归属及行权限反例。它验证设计检查器，不执行登记引用的业务校验，也不证明生产事件能够共同提交或恢复。

`test_camctl_process_recovery.py` 在 Linux 和部署版 Python 3.11 上构建当前工作区的 C 接入模块，以真实 CLI 和受管工具验证独立组、保留退出记录、原组终止、完整核验、最终回收和下一次 run。信号和扫描同步点只位于 C 系统接口边界；测试覆盖未知状态、提前回收、待启动要求、共享预算、并行 submit 以及模拟服务端脱离与终止的竞争，不使用 Python 谓词代替 C 模块。目标实际工具及第三方主程序仍单独联调。

`test_camctl_host_deployment.py` 验证默认配置与显式缺失配置下的真实 init/run 使用同一状态库，并按独立交付的 README 和 TOML 准备部署，使用真实 CLI 发布报告、C 模块同步领取及原始字节比对。它不连接设备；组件内的交付集成测试另行验证构建、安装、包内容和资料引用，两者共同覆盖第三方接入所需的配置与文件交接。

`test_camctl_directory_switch.py` 使用真实 CLI 和 C 模块完成旧目录报告发布、领取与主程序清理，再由显式 `init` 切换三个绑定目录。新会话向新 `ready` 发布报告，重新配置的 C 模块从该目录领取至新 `processing`；测试核对原始字节、数据库身份及报告编号继续增长。组件内的目录切换测试另外验证历史、ACK、绑定提交中断和目录资格。

`test_action6_builtin_demo_roundtrip.py` 在源码目录之外构建并独立安装 wheel 和锁定依赖，从安装包提取 Action6 录像预设，经真实 C host 完成十秒录像、独立取回、文件与报告领取、客户端导入及累计确认。它加载内置驱动，只在 ADB 进程边界提供受接口约束的响应和文件；测试核对 START 返回 `00` 后保存的 `SENT` 语义、未知实际开始时刻、1000 毫秒启动余量、至少 11 秒后发出的唯一 STOP、STOP 后的实际五秒等待、文件完成来源、原动作关联、源与副本摘要及源文件保留。这条链不连接真实相机，其媒体样例不证明设备的分辨率、帧率或时长，目标主机验收见[演示操作说明](../../docs/hardware/camera-demo-validation.md#arm-linux-的完整验收)。

`test_timelapse_time_parameters.py` 使用 Action6 与 OSMO 360 II 的生产候选 Schema，经测试任务工厂、真实 `describe`、Python 精确校验、Chromium 中的客户端共享模块及 Node.js 后端真实导出，遍历同源时间域并核对精确词元、严格关系、型号曝光及输出。最短参数经过受理、未启动取消、报告发布和客户端导入，验证估算信息不进入生效参数或报告；该路径不执行设备命令。生成器在临时资料副本中验证漂移检查和重新生成。此候选测试不直接调用正式 OSMO 合同；正式安装版业务链由 [OSMO 内置合同联合链](#osmo-360-ii-正式内置合同安装版联合链)验证，Action6 的正式启用条件仍单独验收。

```bash
UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_timelapse_time_parameters.py -q
apps/camctl/.venv311/bin/python apps/camctl/scripts/render_timelapse_time_parameters.py --check
```

### 正式源文件清理的安装版联合链

`test_device_file_cleanup_roundtrip.py` 从仓库外构建并独立安装 wheel，核对正式内置登记、DELETE／FILE_QUERY 工厂、端口及模块来源，经真实客户端导出、C host、CLI、SQLite、文件、报告导入及累计 ACK 验证已有独立动作 `delete_action_outputs`。该模块的 10 个分区覆盖 repaired／OSV／LRF／主机原图／主机预览逐份精确清理、不支持设备源与主机源混合、已清理产物的新请求，以及删除中断、独立预算、原配置保存、提交回执未知、终态伴随和在途取消恢复。外部 ADB 模型只识别已审阅的精确脚本及原目标，不替换生产文件工厂、解释器或状态机。

正式修复样本含两路 H.264 主视频，源与成品均实际运行 ffprobe 和全流解码；STOP 后的 30 秒等待使用真实单调钟。每例独立数据库、远端树和进程组，保存原请求／票据／结果／文件及报告事实。取消分区在结束旧组前断言目标取消已提交、DELETE 仍 RUNNING、结果事件为空、门闩进程仍活动且未发 QUERY，再由新 host 沿原尝试恢复 UNKNOWN，只核实一次，不重放删除。

此模块与原 OSMO 模块在同一命令顺序运行，合计 14 个用例。需要部署版 Python 3.11、uv 缓存及构建依赖、客户端 Node.js 依赖、C 构建工具和可用的 ffmpeg／ffprobe；缺少媒体工具会明确跳过，不能据此宣称完整验收。执行命令为：

```bash
apps/camctl/.venv311/bin/python -m pytest \
  tests/integration/test_device_file_cleanup_roundtrip.py \
  tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
```

ADB／Hasselblad 文件是受合同约束的软件样例；本链不连接真实相机，也不证明 ARM 工具、实际设备媒体或物理删除。具体软件结果及版本边界见[验证记录](../../docs/camctl/verification.md#设备文件清理的软件联合验收2026-10-18-utc)，现场结果由[清理计划 R7](../../docs/superpowers/plans/2026-10-18-device-file-cleanup.md#r7用已验发行物完成-osmo-目标设备验收)单独跟踪。

### OSMO 360 II 正式内置合同安装版联合链

`test_osmo_builtin_timelapse_roundtrip.py` 从源码目录外构建并安装当前 wheel 与锁定运行依赖，比较源码及安装版 `describe`，并核对安装 wheel 内的 OSMO 配置、普通录像和延时样例与权威资料逐字节一致。该模块共四个用例：一个源码／安装版能力与资源核对，以及三个业务组合——普通录像配 LRF、原生延时配 LRF、原生延时不配 LRF。

业务链使用真实客户端计划导出、C host、安装版 CLI、SQLite、独立取回、报告导入及累计 ACK；只替换 PATH 中的 ADB 子进程边界和已声明的业务时钟入口。测试从整秒 UTC 与单调钟冻结值开始，让同一对读数供 ADB START 响应和保存 `sent_at` 使用；恢复阶段另设 epoch（时钟状态代次）并记录 CLI PID、实际读取的业务读数、真实单调时刻及已安装 `camctl.capture.handlers` 的调用位置。普通录像使用十秒目标与 1000 毫秒启动余量，只发送一次 START；恢复进程在 `A+10/U+10` 决策点至少跨一个真实 0.5 秒轮询周期读取同一冻结值，期间不发送 STOP、不尝试结果核实、不生成结果页、文件或输出。测试随后在仍运行的同一进程中释放到 `A+11/U+11`，核对唯一正常 STOP；取得 `00` STOP 确认后，driver 仍使用真实单调钟实际等待完整 30 秒再读取文件。原生延时样例采用 2 秒间隔、300 秒设备目标和 1000 毫秒启动余量，另保留 30 秒结果余量；测试先在 `A+330/U+330` 观察恢复决策持续读取且没有 STOP、结果列举、结果页、文件或输出，再于同一进程释放到 `A+331/U+331` 后核对结果核实。ADB 日志分别保存冻结业务 UTC／单调读数和真实进程单调时刻。对原片和可选预览分别取回后，测试通过 `client_driver.ts import-media <客户端存储目录> <已领取文件路径> <输出凭据路径>` 调用客户端真实 `Files.upload`；保存回执核对报告中的 delivery、output 与源动作身份，并将导入文件大小和 SHA-256 与对应交付事实比对。测试还验证取回中断及同一请求恢复时沿用原交付和已提交偏移。

ADB 提供的 OSV／LRF 是合成字节样例，不证明真实媒体可解码、设备画质、帧率或采集时长；受控时钟边界验证也不等于真实设备计时。本链的执行命令为：

```bash
apps/camctl/.venv311/bin/python -m pytest tests/integration/test_osmo_builtin_timelapse_roundtrip.py -q
```

`client_driver.ts validate-parameters <客户端存储目录> <参数用例路径> <凭据路径>` 是测试接缝。存储目录应含真实 `describe` 输出的 `device-capabilities.json`；用例 JSON 包含 `scheduled_at` 和 `cases`，每项有 `name` 及保存完整原数字词元的 `params_text` 字符串，设备标识为 `camera`，动作类型为 `camera_timelapse`。凭据按同序记录 `browser_valid`、`shared_valid`、`export_valid`、两处未改写的参数正文，以及成功导出的完整 JSON 正文或明确的 `invalid_plan`。用例预期由调用者独立判断；测试命令不接收产品候选能力开关，也不维护设备完整参数枚举。

`test_video_size_estimate.py` 使用同源虚构驱动，经真实 `describe` 导出、客户端整份加载及 HTTP 状态交接验证视频估算，再通过客户端计划导出、camctl 受理和执行、报告发布及客户端导入核对实际文件事实。它覆盖精确小数、数学整数帧数、非法声明的整份失败和重载保留、合法但无法估算的导出，以及估算不进入生效参数或报告。`client_driver.ts` 的估算命令只提供测试凭据，没有新增产品接口。该文件无需 C 构建或真实设备；安装物资源由组件的包测试核对，真实相机的固定假定参考值另按[估算设计](../../docs/superpowers/specs/2026-10-10-video-size-estimate-design.md#实施状态与设备接入前提)解释。

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_video_size_estimate.py -q
```

跨组件验收沿计划导出、模块交接路径、CLI 执行与报告发布、同步领取到 `processing`、客户端导入追踪业务结果，契约见[接入模块验收要求](../../docs/host-demo/design.md#验收要求)。报告同步主链已由真实客户端导出、真实 C 主程序递交与领取（WSL 构建的 host-demo）、camctl CLI 执行及客户端导入串联验证；录像、照片、延时摄影、取回、取消、清理等场景已全部经同一 C 模块递交链复验（`test_camctl_c_module_roundtrip.py`，带设备链经部署装配桥接入受契约约束的设备替身），会话恢复、媒体修复与日志副本等场景经 camctl CLI 直接驱动验证，命令与环境见[集成计划验证记录](../../docs/superpowers/plans/2026-09-30-camctl-integration.md)。剩余为真实第三方主程序接入及目标环境执行，边界见[部署交接与待核验项](../../docs/camctl/verification.md#部署交接与待核验项)与[模块验证记录](../../docs/host-demo/verification.md)。

`test_camctl_motor_notifications.py` 构建真实 C 主程序替身，通过单参数回调观察位置，串联客户端导出、CLI 发送、报告领取／导入和累计确认。它还验证慢回调期间的 submit、下一次 run、相机共存，以及保存意图后和写入后中断时 host 的自动恢复。

`test_camctl_motor_recovery.py` 在真实 SQLite 事务和管道写入边界安排进程中断或提交结果未知，分别核对实际消息、发送记录、动作终态和报告；覆盖意图及结果保存前后、通道缺失／已满／断开、过期、四种取消寻址和原请求重送。`_motor_fault_entry.py` 仅提供这些测试的确定故障点，生产 CLI 与仓储仍执行真实逻辑。命令、环境与分区结果见[电机控制验证记录](../../docs/camctl/verification.md#电机控制与单向通知验证2026-10-08)。测试不操作真实电机。
