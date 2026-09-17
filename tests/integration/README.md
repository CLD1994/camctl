# 跨组件集成测试

本目录用于客户端、camctl CLI 和 C 接入模块之间的真实协作验证。终端演示使用同一模块；组件内部测试放在各自目录，目前客户端测试入口为 `apps/client/tests`。

跨组件验收沿计划导出、模块交接路径、CLI 执行与报告发布、同步领取到 `processing`、客户端导入追踪业务结果，契约见[接入模块验收要求](../../docs/host-demo/design.md#验收要求)。[C 接入模块和终端演示](../../apps/host-demo/README.md)已提供，组件测试使用可控 CLI 验证进程与文件边界。真实 CLI 尚未实现，当前不声明这条真实业务链路已通过；目标系统与设备验收边界见[模块验证记录](../../docs/host-demo/verification.md)。
