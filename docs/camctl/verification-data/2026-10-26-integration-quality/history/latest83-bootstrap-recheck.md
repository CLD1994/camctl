# 固定新装配基线的 bootstrap 单项复验

源码固定为 `f143fbbecf68e9333ef6c4db36188b2c6c642630`，包含 `83a8aac7` 与此前两项检查副本修复。工作区 `/tmp/camctl-quality-history-20261026` 使用 detached HEAD，测试前后均干净；本轮没有修改生产或测试，没有追随后续提交。

只运行 `apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py::test_submit_during_initial_discovery_is_not_lost` 一次，没有运行两个大历史目标，也没有阶段计时。保留原测试的真实提交、首次读取同步点、八次读取和四个成功终态断言及 30 秒成功预算，但此次尚未执行到这些目标断言。外部插件只记录 pytest 实际进度事件，没有包裹被测函数。CPython 3.11.17、SQLite 3.53.1；PYTHONPATH 指向本工作区，临时根、Python 缓存和 basetemp 均使用本轮私有目录。此同进程组件用例不表示整个程序内存预算验收。

结果为 **1 failed，退出 1**：pytest 总计 2.85 秒，call 0.33 秒，外部入口墙钟 4.327292504 秒。XML 的 testcase time 为 0.520 秒，包含 setup/call/teardown；不同计时范围分别保留。

失败发生于测试前态 `_save_photos` 调用 `capture(context)` 时：`bootstrap/flows.py:212` 读取 `context.database`，但共享 `test_output_binding_changes.py:155` 的 `environment` 仍创建仅有 `open_connection`、`clock` 的 `SimpleNamespace`。因此错误为 `AttributeError: 'types.SimpleNamespace' object has no attribute 'database'`。测试尚未进入随后创建默认运行装配并调用 `execute_command` 的目标路径，不能将本轮失败视为初次发现丢失新提交，也不能计作默认装配通过。

已静态核对新生产契约：`SessionContext` 在 `session/service.py:254` 要求 `open_database`，第 300 行拥有运行期 `database` 字段；`bootstrap/lifecycle.py:1134` 的工厂通过 `BusinessDatabase.open` 创建异步端口，并在第 1155 行传给真实会话；`session/service.py:780-781` 在会话锁内打开端口并赋给 `context.database`。`CaptureFlow` 及拍摄工厂已经消费该异步端口。因此此次是共享前态夹具与新接口不兼容，区别于旧 `61c67571` 的默认生产装配缺口。当前事实不需要生产修复；尚未通过运行验证新的默认装配。

遵循失败先核对并报告的边界，本轮未自行适配夹具，也未重试。原 `baseline-*`、`diagnostic-*` 和 `fixed-*` 结果独立保留，不改写其历史口径。

完整命令、解释器与环境在 `latest83-bootstrap-command.json`，结果与干净状态在 `latest83-bootstrap-result.json`；原始日志、XML、退出码、进度事件及入口脚本使用同名前缀保存。
