# camctl

camctl 项目包含个人电脑客户端、嵌入式 Linux 上的设备控制 CLI（命令行程序）及主程序调用示例。客户端导出计划，主程序交给 camctl 调度设备，再通过文件交接报告与拍摄产物。

## 组件入口

| 组件 | 技术与责任 | 当前状态 |
| --- | --- | --- |
| [客户端](apps/client/README.md) | TypeScript、React、Node.js；计划编辑、报告与媒体导入 | 已实现 MVP |
| [camctl CLI](apps/camctl/README.md) | Python；调度、设备通信、持久化、产物与报告 | 已定义设计，待实现 |
| [主程序 demo](apps/host-demo/README.md) | C；供对接方参考的 Linux 进程调用与文件交接示例 | 已定义职责，待实现 |

## 启动客户端

需要 Node.js 24.16。在仓库根目录的 PowerShell 执行：

```powershell
New-Item -ItemType Directory -Force data
$env:CAMCTL_DATA_DIR = (Resolve-Path data).Path
Set-Location apps/client
npm ci
npm run build
npm start
```

打开 [本地客户端](http://localhost:4310)。首次使用时核对数据目录，再显式创建客户端数据。已有数据继续使用原目录。Docker、备份与恢复见[客户端运行说明](docs/client/running.md)。

## 资料与开发

- [全局设计](docs/architecture/README.md)与[阅读路线](docs/architecture/reading-guide.md)。
- [目录与组件边界](docs/architecture/repository-layout.md)。
- [公共协议](protocol/README.md)：Schema 与标准样例。
- [客户端设计](docs/client/README.md)、[CLI 设计入口](docs/camctl/README.md)、[主程序 demo 对接入口](docs/host-demo/README.md)。
- [完整演示](demos/README.md)、[硬件交接资料](docs/hardware/camera-control-handoff.md)。
- [跨组件测试](tests/integration/README.md)与[仓库检查脚本](scripts/README.md)。

各组件独立管理依赖与构建。客户端测试在 `apps/client` 执行 `npm test`；根目录不承担 npm 应用入口。日常数据 `data/`、本地验收 `.local/` 和临时资料 `tmp/` 不提交。
