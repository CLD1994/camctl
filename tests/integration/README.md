# 跨组件集成测试

本目录用于客户端、camctl CLI 和 C 接入模块之间的真实协作验证。终端演示使用同一模块；组件内部测试放在各自目录，目前客户端测试入口为 `apps/client/tests`。

`report-dependencies.test.mjs` 与 `report-dependency-conditions.test.mjs` 验证公共 Schema、数据库字段登记、规格 SQL 和检查器之间的接缝，包含错误登记反例与具体条件场景。运行入口见[检查脚本](../../scripts/README.md)；通过这些检查不表示真实 CLI 已经接入报告生成。

`test_database_history_sql.py` 在内存 SQLite 加载真实结构，验证来源及流程关联、清理状态矩阵、启动与残留停止的过期组合、独立文件历史元数据、两类目录和快照范围，并在有无统计信息时核对分页查询。它还读取统一运行库条件，验证检查脚本对主线版本、精确回移例外及非法版本输入的判定；这些版本样例不表示实际运行了所有 SQLite 版本。上述检查不代替生产入口、事件应用、历史恢复或报告字节一致性测试。

`event-transitions.test.mjs` 组合真实事件登记、SQL、枚举和报告依赖，验证列分类、分支和状态覆盖、历史归属及行权限反例。它验证设计检查器，不执行登记引用的业务校验，也不证明生产事件能够共同提交或恢复。

`test_camctl_process_recovery.py` 在 Linux 和部署版 Python 3.11 上构建当前工作区的 C 接入模块，以真实 CLI 和受管工具验证独立组、保留退出记录、原组终止、完整核验、最终回收和下一次 run。信号和扫描同步点只位于 C 系统接口边界；测试覆盖未知状态、提前回收、待启动要求、共享预算、并行 submit 以及模拟服务端脱离与终止的竞争，不使用 Python 谓词代替 C 模块。目标实际工具及第三方主程序仍单独联调。

`test_camctl_host_deployment.py` 验证默认配置与显式缺失配置下的真实 init/run 使用同一状态库，并按独立交付的 README 和 TOML 准备部署，使用真实 CLI 发布报告、C 模块同步领取及原始字节比对。它不连接设备；组件内的交付集成测试另行验证构建、安装、包内容和资料引用，两者共同覆盖第三方接入所需的配置与文件交接。

`test_camctl_directory_switch.py` 使用真实 CLI 和 C 模块完成旧目录报告发布、领取与主程序清理，再由显式 `init` 切换三个绑定目录。新会话向新 `ready` 发布报告，重新配置的 C 模块从该目录领取至新 `processing`；测试核对原始字节、数据库身份及报告编号继续增长。组件内的目录切换测试另外验证历史、ACK、绑定提交中断和目录资格。

`test_action6_builtin_demo_roundtrip.py` 在源码目录之外构建并独立安装 wheel 和锁定依赖，从安装包提取 Action6 录像预设，经真实 C host 完成十秒录像、独立取回、文件与报告领取、客户端导入及累计确认。它加载内置驱动，只在 ADB 进程边界提供受接口约束的响应和文件；测试核对 STOP 后的实际五秒等待、文件完成来源、原动作关联、源与副本摘要及源文件保留。这条链不连接真实相机，其媒体样例不证明设备的分辨率、帧率或时长，目标主机验收见[演示操作说明](../../docs/hardware/camera-demo-validation.md#arm-linux-的完整验收)。

`test_timelapse_time_parameters.py` 使用 Action6 与 OSMO 360 II 的生产候选 Schema，经测试任务工厂、真实 `describe`、Python 精确校验、Chromium 中的客户端共享模块及 Node.js 后端真实导出，遍历同源时间域并核对精确词元、严格关系、型号曝光及输出。最短参数经过受理、未启动取消、报告发布和客户端导入，验证估算信息不进入生效参数或报告；该路径不执行设备命令。生成器在临时资料副本中验证漂移检查和重新生成。此候选测试不直接调用正式 OSMO 合同；正式安装版业务链由 [OSMO 内置合同联合链](#osmo-360-ii-正式内置合同安装版联合链)验证，Action6 的正式启用条件仍单独验收。

```bash
UV_PROJECT_ENVIRONMENT="$PWD/apps/camctl/.venv311" uv run --frozen --project apps/camctl --group test --python 3.11 pytest tests/integration/test_timelapse_time_parameters.py -q
apps/camctl/.venv311/bin/python apps/camctl/scripts/render_timelapse_time_parameters.py --check
```

### OSMO 360 II 正式内置合同安装版联合链

`test_osmo_builtin_timelapse_roundtrip.py` 从源码目录外构建并安装当前 wheel 与锁定运行依赖，比较源码及安装版 `describe`，并核对安装 wheel 内的 OSMO 配置、普通录像和延时样例与权威资料逐字节一致。该模块共四个用例：一个源码／安装版能力与资源核对，以及三个业务组合——普通录像配 LRF、原生延时配 LRF、原生延时不配 LRF。

业务链使用真实客户端计划导出、C host、安装版 CLI、SQLite、独立取回、报告导入及累计 ACK；只替换 PATH 中的 ADB 子进程边界和受控时钟入口。普通录像只发送一次 START，沿生产主机计时流程并通过受控时钟推进目标；取得 `00` STOP 确认后实际等待完整 30 秒再读取文件。原生延时只发送一次 START、不派发 STOP，通过受控时钟推进 300 秒目标和 30 秒自然结束余量，并核对原 START、等待来源及结果页的先后关系。对原片和可选预览分别取回后，测试通过 `client_driver.ts import-media <客户端存储目录> <已领取文件路径> <输出凭据路径>` 调用客户端真实 `Files.upload`；保存回执核对报告中的 delivery、output 与源动作身份，并将导入文件大小和 SHA-256 与对应交付事实比对。测试还验证取回中断及同一请求恢复时沿用原交付和已提交偏移。

ADB 提供的 OSV／LRF 是合成字节样例，不证明真实媒体可解码、设备画质、帧率或采集时长；受控时钟也不等于真实设备计时。本链的执行命令为：

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
