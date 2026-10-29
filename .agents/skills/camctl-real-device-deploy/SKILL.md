---
name: camctl-real-device-deploy
description: 为 camctl 创建独立的真机验证环境，或准备用户测试部署包的安装入口、配置和说明。构建并安装组件、初始化数据并生成设备能力说明；不用于迁移或覆盖已有环境。
---

# 准备真机验证部署

本地新建环境使用下面的部署脚本继承已有配置；向用户交付安装包时，使用本文末尾的安装入口自动发现设备并生成 TOML。配置字段以[配置项参考](../../../docs/camctl/configuration-reference.md)为准。

## 本地新建环境

用[部署脚本](scripts/deploy.py)执行重复工作。先遵守仓库 `AGENTS.md` 和相关组件说明，确认源码目录、现有设备配置和新目录；用户已指定时直接采用。需要继承现有 ADB 路由时提供对应的 `environment.sh`。Hasselblad 仅需客户端能力声明且尚无真实绑定时，使用实际需要的设备 ID 显式传入 `--hasselblad-id`，不要虚构序列号或可执行工具路径。

在所选仓库根目录执行；需要 Linux、Python 3.11、uv、CMake、C 编译工具、Node.js、npm 和 Git。

```bash
python3.11 .agents/skills/camctl-real-device-deploy/scripts/deploy.py \
  --repo "$(pwd)" \
  --config /path/to/existing/config.toml \
  --target /path/to/new-environment \
  --environment /path/to/existing/environment.sh \
  --hasselblad-id hasselbla-01
```

`--environment` 和 `--hasselblad-id` 是按需选项；`--python` 可指定 Python 3.11 解释器。目标目录必须不存在；位于仓库内时必须放在 `tmp` 目录下。具体参数和前提以 `--help` 为准。脚本构建并安装三组件，依据配置创建独立状态库和交接目录，生成客户端能力说明及启动入口，留下日志和部署收据。脚本不迁移旧状态库、客户端数据或媒体，也不启动 host 或操作真实设备。失败时保留诊断，处理原因后使用新的目标目录。

部署后根据收据报告源码版本、目标路径、设备配置与额外客户端声明、启动入口及实际执行的检查。用户需要客户端运行时启动 `start-client.sh` 并检查一次网页入口；启动 host 前确认其他环境没有运行中的 host 和 camctl，以免同时操作相同设备。需要 ADB 时报告当前连接结果。构建与初始化不等于集成测试或真实设备验收，不要反复执行已完成的检查。保留原环境的数据库及文件，既有环境更新和目录迁移应遵循仓库部署及目录切换规则，不套用新建流程。

## 用户测试部署包

交付包的主机安装入口直接复制 [install-host.py](scripts/install-host.py)、[deploy_support.py](scripts/deploy_support.py) 和 [camera_discovery.py](scripts/camera_discovery.py)，三者置于包根目录。包内同时提供 `camctl/` 下的一个 wheel 与锁定的 `requirements.txt`、`host/` 下的一个 CPack `*-Source.tar.gz` 源码包，以及描述实际组件版本与验证范围的 `RELEASE.json`。客户端仍按其组件说明独立交付。

安装器从当前环境自动获取相机绑定，生成 `<target>/config.toml`。`--hb-cli` 指定现场工具的路径；省略时从 PATH 查找 `hb_cli`。识别成功的设备无需手填序列号；未识别型号生成待修改占位项。

自动生成的 Hasselblad 设备配置由 [camera_discovery.py](scripts/camera_discovery.py) 显式设置拍摄准备提前量和启动控制超时，均为 30 秒；已识别设备与占位设备使用同一部署策略。继承已有配置的本地新建环境保留原配置。

调用示例为：

```bash
python3 install-host.py \
  --target "$HOME/.camctl/new-device-test" \
  --hb-cli /path/to/hb_cli
```

新建自动配置的设备实例 ID 使用型号前缀和至少两位编号，各型号分别从 01 开始递增。编号只用于设备实例，驱动标识及驱动配置字段不随实例名称改变；继承已有配置时保留原设备 ID。

安装入口最低支持 Linux 上的 Python 3.6。目标机须先提供 uv、CMake 3.10.2 或以上、C11 编译器及 pthread；camctl 和安装实现仍使用 Python 3.11。入口在其他 Python 版本下通过 `uv run --python 3.11` 自动查找或下载运行环境，无须手动预装 3.11，不修改系统 Python。首次缺少对应运行环境时需要联网；下载受阻时明确失败，不改用旧版本运行 camctl。uv 的解释器管理行为见[官方说明](https://docs.astral.sh/uv/concepts/python-versions/)。

配置与构建使用共享脚本中的 CMake 调用，保持组件声明的最低版本兼容。安装器在新虚拟环境安装 wheel 后执行身份发现，复用该发行物的 Hasselblad 响应解析器；仅查询身份，不设置参数、拍摄、删除文件或停止设备。

安装器使用 `adb devices -l` 的精确产品标识匹配相机，映射只在 `camera_discovery.py` 中维护。依据见 [Action6 现场记录](../../../docs/camctl/verification.md#arm-更新包接收与现场状态2026-10-11)及 [OSMO 现场记录](../../../docs/camctl/verification.md#osmo-360-ii-全量真机验证2026-10-17)。Hasselblad 通过 `hb_cli --pid 0x000a list` 核对 USB 身份，再用同一 PID 的 `info` 获取 serial；格式与工具选择边界见[驱动说明](../../../docs/camctl/hasselblad-driver.md)。

| 检测结果 | 安装处理 |
| --- | --- |
| ADB 状态为 `device` 且产品标识精确匹配 | 写入完整 serial 及对应驱动；同型号按 serial 排序后从 01 递增编号，实例 ID 采用 `action6-01`、`osmo360ii-01` 等形式，并全部配置。 |
| ADB 设备离线、未授权、无权限，或产品未知/缺失 | 报告原始设备行与未配置原因，不猜测驱动；其他可识别设备继续检测。 |
| 单台匹配的 CFV 100C，且 `info` 可解析 | 实例 ID 为 `hasselbla-01`，写入工具绝对路径、实际 serial 和 product ID；正式源与预算由发现脚本中的部署策略提供。 |
| 多台匹配的 CFV 100C | 停止安装；工具只能按 PID 选择，无法唯一绑定。报告数量与 USB 位置，要求仅连接当前实例所用相机。 |
| 某种工具未找到 | 明确报告该通道未检测；可以配置其他通道已识别的相机。显式提供的 hb_cli 路径无效时在创建目标前拒绝。 |
| 某种相机未识别到，包括工具缺失、设备未连接或只有未知/不可用设备 | 已识别的设备保留真实配置，缺失型号生成编号为 01 的 `REPLACE_*` 占位项；Hasselblad 工具已找到时保留实际路径，否则生成明确的工具路径占位符。继续安装、能力导出与初始化，逐项报告待修改字段。 |
| 查询失败、超时或响应无效 | 停止安装并保存检测事实与错误，不初始化状态库。 |

安装器把 `paths` 设置到新实例，并用生成的同一份 TOML 执行 `describe`、`init` 和生成启动入口。检测继承当前 `ADB_SERVER_SOCKET`；已设置时将其保存到检测报告并写入启动脚本，使后续调用使用相同 ADB 服务。

生成的 TOML 每台设备使用一个 `[devices.<id>]` 节，设备内部采用 `adb.serial`、`hasselblad.executable`、`recording.start_timeout_s` 等点分键。其他顶层配置各用一个节；字面含点或空格的名称保留引号，空表及精确数值保留原语义。

终端须汇报工具路径、每台相机的驱动、设备 ID、serial、适用设备的正式源选择与所有显式策略，以及未配置设备的原因。未显式写入的策略采用 camctl 内置默认值，身份检测不推断设备的拍摄设置或固件结果模式。`camera-discovery.json` 保留查询参数、stdout/stderr 文本及可还原原字节的 Base64、退出码或超时事实和生成的设备配置；`install-receipt.json` 包含同一份检测结果。安装后将新导出的 `device-capabilities.json` 交给客户端。安装不启动主程序；失败时保留安装日志及收据，使用新的目标目录重试。

`placeholder_devices` 在检测报告及安装收据中列出占位设备和待修改字段，终端同时展示。包含占位项时可以完成软件安装，能力说明中的这些条目只提供对应驱动的配置结构，不代表现场设备已绑定；使用前须填写真实值并重新执行 `describe --config <target>/config.toml` 导出能力说明，再交给客户端。

交付前在仓库之外解压并实际执行安装入口，检查配置、状态库、能力说明和启动路径；最低 CMake 版本的兼容验证与目标 ARM64 部署分别记录。生成文件校验清单，并将配置方式及实际验证范围写入交付说明。

修改共享脚本后，使用已安装 camctl wheel 的 Python 3.11 环境运行技能自身的集成测试，以核对实际发行物的响应解析器：

```bash
/path/to/deployment/venv/bin/python -m unittest discover \
  -s .agents/skills/camctl-real-device-deploy/tests/integration -v
```
