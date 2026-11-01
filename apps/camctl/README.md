# camctl CLI

本组件使用 Python，运行在 ARM Linux 主机上，负责计划受理、设备调度、不可变历史与当前状态、产物管理和报告发布。

本目录管理 `pyproject.toml`、`uv.lock`、`src/camctl` 和分类测试；协议资源使用根目录 `protocol`。代码包含 CLI 入口及第一版各业务模块，完整业务链的实施与验收进度以[实施路线图](../../docs/superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)为准；源码目录之外的构建、安装与部署检查归 B7，边界见[部署交接与待核验项](../../docs/camctl/verification.md#部署交接与待核验项)。

开发与测试使用部署指定的 Python 3.11。先核对虚拟环境解释器版本；不匹配时通过 uv 创建对应版本的非项目级虚拟环境。环境与测试分工见[软件验证](../../docs/camctl/verification.md#python-环境)，业务和部署契约见[设计入口](../../docs/camctl/README.md)。

`camctl --version` 输出当前安装的发行版本。`run`、`submit`、`init` 和 `describe` 在日志通道建立后各记录一次启动版本；日志文件或队列不可用时尝试通过 stderr 交付。

软件更新、数据库迁移、回滚和中断恢复由本组件的维护引擎负责。Python 专用更新包只交付 wheel、离线依赖和元数据；构建入口为 `scripts/build_update.py`。部署时由 wheel 中的发布器准备虚拟环境外的维护入口，C 主程序通过 `camctl_host_update` 调用。入口与冻结引擎、启动资格及配置来源的契约见[更新接入规格](../../docs/architecture/software-update-interface.md)，进度见[实施计划](../../docs/superpowers/plans/2026-10-31-camctl-owned-update-api.md)。

发布人员和现场操作者按[版本发布与现场更新操作](../../docs/camctl/software-updates.md)完成版本递增、包构建、首次引导、更新、恢复及重启。

技术选型、模块边界和接口契约见[实现规格](../../docs/camctl/implementation.md)。实施前须完成其中列明的设计事项。

部署配置的全部字段、默认值、合法范围及主程序路径对应关系见[配置项参考](../../docs/camctl/configuration-reference.md)；首次安装和联调步骤见[部署样例](../../docs/architecture/deployment-example.md)。

Hasselblad CFV 100C 单张 RAW／JPEG 驱动的设备身份、完整拍摄参数、`hb_cli` 调用及正式源部署选择见[专项驱动说明](../../docs/camctl/hasselblad-driver.md)；公共拍摄与正式产物规则见[相机拍摄规格](../../docs/architecture/camera-capture.md)和[正式产物规格](../../docs/architecture/outputs.md)。
