# 客户端

个人电脑上的本地网页与 Node.js 服务共同组成客户端。源码位于 `src`，组件测试位于 `tests/unit` 和 `tests/integration`。

- [运行、Docker、备份与恢复](../../docs/client/running.md)
- [客户端设计](../../docs/client/README.md)
- [验证范围](../../docs/client/verification.md)
- [包含客户端的完整 ZIP 部署包](../../.agents/skills/camctl-real-device-deploy/references/packaging.md)

客户端依赖由 `package-lock.json` 统一锁定。在本目录执行 `npm ci` 准备依赖，再执行 `npm run build`、`npm test`；构建和测试消费已准备的依赖。浏览器集成测试所需的 Chromium 由这份依赖中的 Playwright 安装。启动前用 `CAMCTL_DATA_DIR` 明确指定数据目录，再执行 `npm start`；完整命令见运行说明。`src/shared` 是客户端内部共享代码，公共机器协议从根 `protocol` 读取。

客户端版本由 `package.json` 定义，服务成功监听后在启动日志中记录一次版本。

更新报告类型：在本目录执行 `node --import tsx src/domain/generate-report-types.ts`。Docker 构建使用仓库根上下文：`docker build -f apps/client/Dockerfile .`（在仓库根执行）。
