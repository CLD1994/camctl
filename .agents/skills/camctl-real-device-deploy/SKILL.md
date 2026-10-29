---
name: camctl-real-device-deploy
description: 为 camctl 创建独立的真机验证环境，或准备用户测试部署包的安装入口、配置和说明。构建并安装组件、初始化数据并生成设备能力说明；不用于迁移或覆盖已有环境。
---

# 准备真机验证部署

本地新建环境使用下面的部署脚本；向用户交付安装包时使用本文末尾的安装入口。两种方式均以一份 TOML 文件提供设备绑定与本地策略，新实例只重设独立的数据路径。配置字段及三类相机示例以[配置项参考](../../../docs/camctl/configuration-reference.md)为准。

## 本地新建环境

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

## 用户测试部署包

交付包的主机安装入口直接复制 [install-host.py](scripts/install-host.py) 和它依赖的 [deploy_support.py](scripts/deploy_support.py)，两者置于包根目录。包内同时提供 `camctl/` 下的一个 wheel 与锁定的 `requirements.txt`、`host/` 下的一个 CPack `*-Source.tar.gz` 源码包，以及描述实际组件版本与验证范围的 `RELEASE.json`。客户端仍按其组件说明独立交付。

将 [camctl.toml 配置模板](assets/camctl.toml)复制到包根目录。模板按节列出 Action6、OSMO 360 II 和 Hasselblad；部署人员填入现场序列号、工具路径和策略，只保留实际接入的设备，也可直接使用已有的完整 TOML 配置。安装器统一读取配置文件，调用示例为：

```bash
python3.11 install-host.py \
  --target "$HOME/.camctl/new-device-test" \
  --config ./camctl.toml
```

目标机需要 Linux、Python 3.11、uv、CMake 3.10.2 或以上、C11 编译器及 pthread。配置与构建必须使用共享脚本中的 CMake 调用，保持组件声明的最低版本兼容。安装器保留输入中的设备绑定和其他策略，把 `paths` 设置到新实例，在 `<target>/config.toml` 写出按节排版的 TOML，再用该文件执行 `describe`、`init` 和生成启动入口。安装后将新导出的 `device-capabilities.json` 交给客户端。安装过程不启动主程序或操作相机；失败时保留安装日志及收据，使用新的目标目录重试。

交付前在仓库之外解压并实际执行安装入口，检查配置、状态库、能力说明和启动路径；最低 CMake 版本的兼容验证与目标 ARM64 部署分别记录。生成文件校验清单，并将配置方式及实际验证范围写入交付说明。

修改共享脚本后可使用 Python 3.11 运行技能自身的集成测试：

```bash
python3.11 -m unittest discover \
  -s .agents/skills/camctl-real-device-deploy/tests/integration -v
```
