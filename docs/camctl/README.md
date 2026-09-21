# CLI 设计与运行资料

- [全局设计](../architecture/README.md)
- [实现规格](implementation.md)：技术选型、模块与接口契约，以及实施前须细化的事项
- [规格收口检查](specification-closure-review.md)：端到端检查结果、需要确认的行为缺口及实现准备事项
- [历史状态查询与报告重建](historical-state-query.md)：事件与两种序号的通俗解释，当前投影、历史快照、正反向恢复及一致读取，以及索引和性能验收的待细化事项
- [CLI 命令与主程序调用](../architecture/cli-commands.md)
- [配置](../architecture/configuration.md)与[初始化](../architecture/initialization.md)
- [部署示例](../architecture/deployment-example.md)
- [请求与会话](../architecture/protocol-session.md)

生产代码归属 `apps/camctl`，使用 Python。全局业务语义与跨组件协议集中在 `docs/architecture`，这里提供 CLI 使用与实现入口。
