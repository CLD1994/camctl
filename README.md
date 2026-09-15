# camctl

camctl 是面向嵌入式 Linux 主机的设备控制 CLI（命令行程序）。用户提交执行计划，由它调度相机录像、管理产物取回和清理，并通过状态报告交回结果。客户端采用个人电脑上的本地网页。

本仓库当前提供第一版设计规格。MVP（最小可行版本）先使用相机替身验证软件流程，真实设备接入另行联调。

- 第一次了解项目：从[设计总览](docs/superpowers/specs/2026-09-08-camctl-cli-design.md)开始。
- 需要完整阅读或评审：[阅读路线与专题索引](docs/superpowers/specs/camctl/reading-guide.md)。
- 查看计划和报告 JSON：[客户端协议样例](docs/superpowers/specs/camctl/client-protocol-examples.md)。
- 核对厂商交接依据：[相机交接资料整理](docs/hardware/camera-control-handoff.md)。
