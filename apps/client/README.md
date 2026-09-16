# 客户端

个人电脑上的本地网页与 Node.js 服务共同组成客户端。源码位于 `src`，组件测试位于 `tests/unit` 和 `tests/integration`。

- [运行、Docker、备份与恢复](../../docs/client/running.md)
- [客户端设计](../../docs/client/README.md)
- [验证范围](../../docs/client/verification.md)

在本目录执行 `npm ci`、`npm run build`、`npm test`。启动前用 `CAMCTL_DATA_DIR` 明确指定数据目录，再执行 `npm start`；完整命令见运行说明。`src/shared` 是客户端内部共享代码，公共机器协议从根 `protocol` 读取。

更新报告类型：在本目录执行 `node --import tsx src/domain/generate-report-types.ts`。Docker 构建使用仓库根上下文：`docker build -f apps/client/Dockerfile .`（在仓库根执行）。
