---
name: camctl-real-device-deploy
description: 为 camctl 创建独立的本地真机验证环境，构建并安装 camctl、host-demo 和客户端，初始化数据并生成设备能力说明。用于要求新建真机验证环境，不用于迁移或覆盖已有环境。
---

# 创建真机验证环境

用[部署脚本](scripts/deploy.py)执行重复工作。先遵守仓库 `AGENTS.md` 和相关组件说明，确认源码目录、现有设备配置和新目录；用户已指定时直接采用。需要继承现有 ADB 路由时提供对应的 `environment.sh`。Hasselblad 仅需客户端能力声明且尚无真实绑定时，使用实际需要的设备 ID 显式传入 `--hasselblad-id`，不要虚构序列号或可执行工具路径。

在所选仓库根目录执行；需要 Linux、Python 3.11、uv、CMake、C 编译工具、Node.js、npm 和 Git。

```bash
python3.11 .agents/skills/camctl-real-device-deploy/scripts/deploy.py \
  --repo "$(pwd)" \
  --config /path/to/existing/config.toml \
  --target /path/to/new-environment \
  --environment /path/to/existing/environment.sh \
  --hasselblad-id hasselblad
```

`--environment` 和 `--hasselblad-id` 是按需选项；`--python` 可指定 Python 3.11 解释器。目标目录必须不存在。具体参数和前提以 `--help` 为准。脚本构建并安装三组件，依据配置创建独立状态库和交接目录，生成客户端能力说明及启动入口，留下日志和部署收据。脚本不迁移旧状态库、客户端数据或媒体，也不启动 host 或操作真实设备。失败时保留诊断，处理原因后使用新的目标目录。

部署后根据收据报告源码版本、目标路径、设备配置与额外客户端声明、启动入口及实际执行的检查。用户需要客户端运行时启动 `start-client.sh` 并检查一次网页入口；启动 host 前确认其他环境没有运行中的 host 和 camctl，以免同时操作相同设备。需要 ADB 时报告当前连接结果。构建与初始化不等于集成测试或真实设备验收，不要反复执行已完成的检查。保留原环境的数据库及文件，既有环境更新和目录迁移应遵循仓库部署及目录切换规则，不套用新建流程。
