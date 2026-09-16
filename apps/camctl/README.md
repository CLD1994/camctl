# camctl CLI

本组件使用 Python，运行在 ARM Linux 主机上，负责计划受理、设备调度、不可变历史与当前状态、产物管理和报告发布。

目前已有[设计与部署规范](../../docs/camctl/README.md)，尚无可运行的 CLI 实现。实现时在本目录管理 `pyproject.toml`、`uv.lock`、`src/camctl` 和分类测试；协议资源使用根目录 `protocol`。
