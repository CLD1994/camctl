# CLI 设计与运行资料

- [全局设计](../architecture/README.md)
- [CLI 命令与主程序调用](../architecture/cli-commands.md)
- [配置](../architecture/configuration.md)与[初始化](../architecture/initialization.md)
- [部署示例](../architecture/deployment-example.md)
- [请求与会话](../architecture/protocol-session.md)

生产代码归属 `apps/camctl`，使用 Python。全局业务语义与跨组件协议集中在 `docs/architecture`，这里提供 CLI 使用与实现入口。
