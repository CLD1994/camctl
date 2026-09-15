# camctl

camctl 是面向嵌入式 Linux 主机的设备控制 CLI（命令行程序）。用户提交执行计划，由它调度相机录像、管理产物取回和清理，并通过状态报告交回结果。客户端采用个人电脑上的本地网页。

仓库包含客户端 MVP（最小可行版本）的本地网页、Node.js 后端及第一版协议规格。客户端通过 JSON 文件交接计划，通过报告与视频导入展示执行结果。真实设备接入另行联调。

## 运行客户端

使用 Node.js 24.16，在仓库根目录执行：

```powershell
npm ci
npm run build
New-Item -ItemType Directory -Force data
npm start
```

打开 [本地客户端](http://localhost:4310)，核对数据目录后显式创建客户端数据。运行、Docker Compose、停机备份与恢复见[客户端操作说明](docs/client-running.md)。

## 设计资料

- 第一次了解项目：从[设计总览](docs/superpowers/specs/2026-09-08-camctl-cli-design.md)开始。
- 需要完整阅读或评审：[阅读路线与专题索引](docs/superpowers/specs/camctl/reading-guide.md)。
- 查看计划和报告 JSON：[客户端协议样例](docs/superpowers/specs/camctl/client-protocol-examples.md)。
- 核对厂商交接依据：[相机交接资料整理](docs/hardware/camera-control-handoff.md)。
