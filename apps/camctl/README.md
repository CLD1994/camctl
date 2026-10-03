# camctl CLI

本组件使用 Python，运行在 ARM Linux 主机上，负责计划受理、设备调度、不可变历史与当前状态、产物管理和报告发布。

本目录管理 `pyproject.toml`、`uv.lock`、`src/camctl` 和分类测试；协议资源使用根目录 `protocol`。代码包含 CLI 入口及部分业务模块，完整业务链的实施与验收进度以[实施路线图](../../docs/superpowers/plans/2026-09-30-camctl-implementation-roadmap.md)为准。

开发与测试使用部署指定的 Python 3.11。先核对虚拟环境解释器版本；不匹配时通过 uv 创建对应版本的非项目级虚拟环境。环境与测试分工见[软件验证](../../docs/camctl/verification.md#python-环境)，业务和部署契约见[设计入口](../../docs/camctl/README.md)。

技术选型、模块边界和接口契约见[实现规格](../../docs/camctl/implementation.md)。实施前须完成其中列明的设计事项。
