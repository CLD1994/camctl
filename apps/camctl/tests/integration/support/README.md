# 组件集成测试的共用支持

本目录集中维护真实资源的准备、正式入口调用和关闭，不复制生产流程。各测试仍说明输入、参与模块与可观察结果；调用了辅助函数不等于已经进入公开接口。

原测试的质量问题、此次重构的处理方式及后续场景设计示例见[重构复盘](../../../../../docs/camctl/integration-test-lessons-2026-10-27.md)；通用约束仍以[共享测试规则](../../../../../.agents/rules/testing.md)为准。

`database.py` 的 `create_test_database` 按权威 SQL 准备有效状态库，供存储格式及事务测试使用。`business_database(path)` 打开真实 `BusinessDatabase`，在数据库线程中创建和关闭连接；调用方使用 `async with` 覆盖整个业务操作。已有准备连接可以用 `database_path(owned)` 取得路径，须先提交准备。业务结果通过公开读取或属于测试线程的独立观察连接核对，不能把 worker 的连接或游标带回测试线程。

受理场景通过正式 `accept_input` 和实际 `AcceptanceRepository` 执行，成功表示完整结果已提交。专门验证仓储事务的用例可以调用公开仓储命令及规定的存储格式，不能把全部受理功能场景改为仓储测试来绕过业务入口。

拍摄功能沿 `session_capture_assembly`、`capture_flow` 及实际会话推进；完整会话通过 `build_runtime`、`execute_command`、`close_runtime` 执行。驱动、文件与时钟在已有端口提供受契约约束的替身。不能在测试中直接拼装 `CaptureRuntime`、调用私有推进或修改待保存集合来替代业务过程。

共享录制准备 `consumer_world` 可以接收测试持有的 `CaseClock`，在创建正式工厂前传入时间回调，后续只推进这个测试对象。`start=True` 的自定义工厂或 `runtime_options` 时间回调须与准备函数使用同一个时钟；不要让准备函数推进一个对象、实际工厂却读取另一个固定回调。`start=False` 仍按原方式调用严格的 `runtime_factory(owned, driver=...)`；该回调可以返回 `None` 作为尚未装配的占位，`consumer_world` 自身仍返回原四元组。录像、取消及延时摄影分别保持各自 UTC、单调钟和真实 worker 重开的原边界，不能统一推进。

报告生成文件、发布子模块和完整报告会话分别使用所属正式入口。现有生产会话通过 `report_flow`、报告 worker 和文件发布链执行，未被生产装配使用的通用辅助函数不能代替这些流程的验证。

`apps/camctl/tests/support/business_database.py` 的内联执行器服务局部单元测试，不提供真实线程组合。功能集成测试使用本目录的真实数据库支持；取消、未知提交、线程结束及文件生命周期继续在真实边界验证。

`segment_inspection_cases.py` 通过真实受理、拍摄及分段仓储准备小规模 Action6 原片成员；文件存在性和长度由正式仓储保存，设备读取替身返回相同长度的内容。测试独立管理 `MediaFlow` 的数据库及文件任务，并等待文件责任结束。
`segment_interrupted_read.py` 只用于验证真实进程读取中断：子进程在驱动读取入口发出标记并等待，由测试终止该进程，再重新打开数据库核对恢复边界。
