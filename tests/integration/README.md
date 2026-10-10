# 跨组件集成测试

本目录用于客户端、camctl CLI 和 C 接入模块之间的真实协作验证。终端演示使用同一模块；组件内部测试放在各自目录，目前客户端测试入口为 `apps/client/tests`。

`report-dependencies.test.mjs` 与 `report-dependency-conditions.test.mjs` 验证公共 Schema、数据库字段登记、规格 SQL 和检查器之间的接缝，包含错误登记反例与具体条件场景。运行入口见[检查脚本](../../scripts/README.md)；通过这些检查不表示真实 CLI 已经接入报告生成。

`test_database_history_sql.py` 在内存 SQLite 加载真实结构，验证来源及流程关联、清理状态矩阵、启动与残留停止的过期组合、独立文件历史元数据、两类目录和快照范围，并在有无统计信息时核对分页查询。它还读取统一运行库条件，验证检查脚本对主线版本、精确回移例外及非法版本输入的判定；这些版本样例不表示实际运行了所有 SQLite 版本。上述检查不代替生产入口、事件应用、历史恢复或报告字节一致性测试。

`event-transitions.test.mjs` 组合真实事件登记、SQL、枚举和报告依赖，验证列分类、分支和状态覆盖、历史归属及行权限反例。它验证设计检查器，不执行登记引用的业务校验，也不证明生产事件能够共同提交或恢复。

`test_camctl_process_recovery.py` 在 Linux 和部署版 Python 3.11 上构建当前工作区的 C 接入模块，以真实 CLI 和受管工具验证独立组、保留退出记录、原组终止、完整核验、最终回收和下一次 run。信号和扫描同步点只位于 C 系统接口边界；测试覆盖未知状态、提前回收、待启动要求、共享预算、并行 submit 以及模拟服务端脱离与终止的竞争，不使用 Python 谓词代替 C 模块。目标实际工具及第三方主程序仍单独联调。

`test_camctl_host_deployment.py` 验证默认配置与显式缺失配置下的真实 init/run 使用同一状态库，并按独立交付的 README 和 TOML 准备部署，使用真实 CLI 发布报告、C 模块同步领取及原始字节比对。它不连接设备；组件内的交付集成测试另行验证构建、安装、包内容和资料引用，两者共同覆盖第三方接入所需的配置与文件交接。

`test_camctl_directory_switch.py` 使用真实 CLI 和 C 模块完成旧目录报告发布、领取与主程序清理，再由显式 `init` 切换三个绑定目录。新会话向新 `ready` 发布报告，重新配置的 C 模块从该目录领取至新 `processing`；测试核对原始字节、数据库身份及报告编号继续增长。组件内的目录切换测试另外验证历史、ACK、绑定提交中断和目录资格。

`test_action6_builtin_demo_roundtrip.py` 在源码目录之外构建并独立安装 wheel 和锁定依赖，从安装包提取 Action6 录像预设，经真实 C host 完成十秒录像、独立取回、文件与报告领取、客户端导入及累计确认。它加载内置驱动，只在 ADB 进程边界提供受接口约束的响应和文件；测试核对 STOP 后的实际五秒等待、文件完成来源、原动作关联、源与副本摘要及源文件保留。这条链不连接真实相机，其媒体样例不证明设备的分辨率、帧率或时长，目标主机验收见[演示操作说明](../../docs/hardware/camera-demo-validation.md#arm-linux-的完整验收)。

跨组件验收沿计划导出、模块交接路径、CLI 执行与报告发布、同步领取到 `processing`、客户端导入追踪业务结果，契约见[接入模块验收要求](../../docs/host-demo/design.md#验收要求)。报告同步主链已由真实客户端导出、真实 C 主程序递交与领取（WSL 构建的 host-demo）、camctl CLI 执行及客户端导入串联验证；录像、照片、延时摄影、取回、取消、清理等场景已全部经同一 C 模块递交链复验（`test_camctl_c_module_roundtrip.py`，带设备链经部署装配桥接入受契约约束的设备替身），会话恢复、媒体修复与日志副本等场景经 camctl CLI 直接驱动验证，命令与环境见[集成计划验证记录](../../docs/superpowers/plans/2026-09-30-camctl-integration.md)。剩余为真实第三方主程序接入及目标环境执行，边界见[部署交接与待核验项](../../docs/camctl/verification.md#部署交接与待核验项)与[模块验证记录](../../docs/host-demo/verification.md)。

`test_camctl_motor_notifications.py` 构建真实 C 主程序替身，通过单参数回调观察位置，串联客户端导出、CLI 发送、报告领取／导入和累计确认。它还验证慢回调期间的 submit、下一次 run、相机共存，以及保存意图后和写入后中断时 host 的自动恢复。

`test_camctl_motor_recovery.py` 在真实 SQLite 事务和管道写入边界安排进程中断或提交结果未知，分别核对实际消息、发送记录、动作终态和报告；覆盖意图及结果保存前后、通道缺失／已满／断开、过期、四种取消寻址和原请求重送。`_motor_fault_entry.py` 仅提供这些测试的确定故障点，生产 CLI 与仓储仍执行真实逻辑。命令、环境与分区结果见[电机控制验证记录](../../docs/camctl/verification.md#电机控制与单向通知验证2026-10-08)。测试不操作真实电机。
