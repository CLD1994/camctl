# 普通与强制更新包本地验证（2026-11-03）

## 结果与范围

普通更新 `1.0.0 → 1.0.1` 与强制更新 `1.0.1 → 1.0.2` 均通过实际本地验证。两次使用交付的原始 ZIP，由当前 C 接入模块的 `host-demo` 调用公开 `camctl_host_update`，执行真实离线安装、SQLite 核验、能力说明同步与后续普通运行。两次更新均可靠提交，两个 host 最终退出码均为 0。

环境为 Linux x86_64、CPython 3.11.17。源码身份为 `f29553d152989ebae1a7244d27941510bd0ef1a9`；C 接入模块从同一工作区独立进行 Release 构建，版本为 `1.0.1`。基线 `camctl-1.0.0` 来自已有完整部署包，没有通过改写已安装版本来模拟旧版本。确切 wheel、ZIP 和 C 二进制摘要见[产物身份](verification-data/2026-11-03-update-packages/artifact-identities.json)。

验证使用 bubblewrap 建立独立用户、文件系统、进程与网络命名空间。强制包内的 `/root/.camctl` 和 `/var/opt/SatService` 保持原路径，在隔离根文件系统内使用同一挂载；仓库和基础运行工具只读。原有部署、客户端预览服务及真实相机未被操作。

完整材料位于 `tmp/update-validation-20261103-final/`，含 `validate.py`、逐命令日志、两个 host 的标准输出、标准错误和日志、原数据库比对、更新事务及归档。用于 C 构建的日志保存在 `tmp/update-validation-20261103/logs/`。本页引用的关键结果已经纳入版本控制。

## 包身份与调用入口

| 包 | 版本及模式 | SHA-256 |
| --- | --- | --- |
| `tmp/camctl-update-1.0.1-normal/camctl-update.zip` | `1.0.1`，普通模式 | `a1070d53cb927848d1d2993588c6cd5faac4b74192a7304e3079ec555c7e0f87` |
| `tmp/camctl-update-1.0.2-force/camctl-update.zip` | `1.0.2`，强制模式；内置用户提供的 `tmp/config.toml` | `b8b6007dbe83cb89e7575203c5d278afe41c0725f0dcbb366ecdde18330dcf48` |

维护入口由普通更新 wheel 中的发布器生成，绑定独立基础解释器及隔离实例。发布后仍核对旧版输出为 `1.0.0`。两个阶段均从 host-demo 的终端 `update <ZIP绝对路径>` 进入更新，传入独立客户端能力同步目录；不直接调用引擎内部安装函数。

旧配置与强制包配置采用同一数据库和交接目录路径，旧配置的日志级别为 WARNING，新配置为 INFO，以核对普通更新保留原配置、强制更新整体替换配置。初始计划通过公开 `submit` 保存，引用未配置的逻辑设备，动作按设备不适用进入失败状态，不产生设备操作。

外部 `sitecustomize` 门闩只暂停更新后新启动的普通 `camctl run`，用于在业务尚未打开数据库前比对数据。维护解释器以 `-I` 执行，不受门闩影响。释放门闩后实际运行生产业务，等待本次 `submit` 要求的后续 run 结束，再发送 `quit`。

## 普通更新

| 检查 | 实际结果 |
| --- | --- |
| 安装与提交 | 原路径版本从 `1.0.0` 变为 `1.0.1`，持久事务为 committed，host-demo 报告更新完成。 |
| 数据库与历史 | 完整逻辑转储摘要、数据库实例身份及目录绑定相同；计划和动作各一条。完整性与外键检查通过。 |
| 配置和文件 | 配置、原环境额外文件、staging 媒体、ready 文件、processing 原文件及客户端数据标记的内容、大小、权限和所有者均保持。 |
| 能力同步与继续运行 | 能力文件可靠发布；新 run 正常结束，原请求重送被正常受理，host 退出 0。 |

比对见[更新前](verification-data/2026-11-03-update-packages/ordinary-before.json)与[更新后、业务运行前](verification-data/2026-11-03-update-packages/ordinary-after.json)。

## 强制更新

| 检查 | 实际结果 |
| --- | --- |
| 安装与提交 | 原路径版本从 `1.0.1` 变为 `1.0.2`，持久事务为 committed，host-demo 报告更新完成。 |
| 配置与新数据库 | 配置正文与包内完整配置一致；数据库实例身份改变，业务运行前计划、动作均为零，完整性与外键检查通过。 |
| 原环境与文件重建 | 旧环境额外文件、旧 staging 媒体及旧 ready 文件移除；业务运行前 ready 为空。processing 原文件和客户端数据标记保持。 |
| 旧环境诊断归档 | 生成一份 2600877 字节、745 个成员的压缩包；旧配置和旧环境额外文件的摘要匹配，归档旧数据库完整性及逻辑内容与更新前一致。staging、ready 和 processing 的标记文件正文没有进入归档。 |
| 归档清理与能力同步 | 归档任务达到 cleaned，展开备份释放；能力说明包含三台声明设备及手动调焦输入开关。 |
| 继续运行与正常退出 | 新 run 成功；同一 host 受理计划后，其新安排的 run 也成功，最终 host 退出 0。 |

比对见[更新前](verification-data/2026-11-03-update-packages/force-before.json)、[新库业务运行前](verification-data/2026-11-03-update-packages/force-after-before-run.json)与[归档及能力复核](verification-data/2026-11-03-update-packages/final-verification.json)。完整调用结果见[结果记录](verification-data/2026-11-03-update-packages/result.json)。

## 准备过程与未验证范围

首次准备的计划样例缺少 `created_at`，CLI 按契约拒绝正文；另因将 staging 与 ready 分别挂载，报告发布遇到 EXDEV，尚未进入更新。两项均修正了验证装配，日志保留在 `tmp/update-validation-20261103/`。第二次已经完成两种更新，但监督脚本在新的 `submit` 返回后立即退出，随后 run 在导入期间收到停止信号并产生诊断，材料保留在 `tmp/update-validation-20261103-run2/`。最终从全新实例重做，等待后续 run 完成后退出，两个 host 日志均没有 error 或 abnormal 调用。

本项验证成功更新路径，没有新增或运行测试套件，没有注入安装失败、回滚或更新中断，没有运行 ARM64、第三方主程序、真实相机或物理断电场景。当前数据库为 V9，仅核对格式与完整性，不把本次结果称为跨格式迁移验收。先前故障与恢复场景的证据仍见[无人值守更新验证记录](unattended-update-verification-2026-11-02.md)，不计为这两份测试包的新验收。
