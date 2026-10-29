# 新生产基线的三节点复验

源码为 `61c675710d8350a96f2f7d164f5ff1d6a220684f`，独立工作区 `/tmp/camctl-quality-history-20261026` 以 detached HEAD 执行。三项测试与上一轮源码保持一致，没有改测试、规模、断言或生产代码。CPython 3.11.17、SQLite 3.53.1，实际 camctl 模块来自本工作区。临时根、Python 缓存和 basetemp 均使用新的源码外私有目录；history 与 bootstrap 分目录顺序执行。

此轮只运行原三项一次，没有阶段计时或重复大世界。外部插件仅记录 pytest 的 node_started、phase_completed、node_finished 和 session_finished 事件，不包裹生产或测试函数。旧基线通过和耗时继续由原 baseline-/diagnostic- 文件保存，本记录独立描述新基线。

| 范围 | 当前结果 | pytest 时间 | 入口墙钟 |
| --- | --- | --- | --- |
| history 两项 | 2 passed，退出 0 | 总计 304.90 秒；报告选择 call 200.50 秒，分段容量 call 101.18 秒 | 306.0174 秒 |
| bootstrap 一项 | 1 failed，退出 1 | 总计 1.75 秒；call 0.96 秒 | 2.1158 秒 |

耗时使用 pytest 原有单调计时和外层 perf_counter；事件 UTC 原样保存，不以 UTC 时间差替代单调耗时。不同基线及同期负载不用于推断稳定提速。

报告选择保留 4200 个动作、4096 候选容量、17 条分页及 128 个真实产物。当前统计为预查询 213600 VM 步、248 页、页工作量 0..500 步、候选查询两次，前/末稀疏窗口各 200 步。

分段容量保留 256/2048 成员、7 条分页和 2MiB 磁盘缓存。源工作量为 48381/348413 VM 步，最大已观察 fetch 为 7，临时库为 610304/4562944 字节，投影行数为 777/6153，事件读取为 0。Python 峰值为 2317867/778246 字节，只作为本项分配观察，不代表整个程序及后代的内存预算验收。

## bootstrap 失败与接口核查

`test_submit_during_initial_discovery_is_not_lost` 已完成两张照片的前态和三个取回动作受理，第一次实际读取内又成功受理了新计划。默认会话随后返回 `state_db_error`，详细错误为 `流程 cleanup 异常: 'SessionContext' object has no attribute 'database'`。失败发生在 `outcome.succeeded` 断言，未耗尽原 30 秒成功预算，也没有进入后续 8 次读取和四个成功终态的断言。

失败后的只读状态核对见 `fixed-bootstrap-state.json`：两个照片动作成功且存在两个正式 output；三个旧取回中首个处于运行中，另两个待执行；新计划与其取回动作已持久化，动作仍待执行。因此不能把当前失败解释为未进入初次读取门或未成功提交新计划，也不能将部分持久化解释为完整目标通过。

新 `bootstrap/cleanup_assembly.py` 的 `_CleanupRound.__call__` 在第 633 行使用 `await self._factory(context.database)`，工厂已改为接收 `BusinessDatabase`。但生产 `session/service.py` 的 `SessionContext` 仍只有 `open_connection` 等既有字段，没有 `database`；`bootstrap/lifecycle.py` 第 1118 行的默认 `execute_command` 装配也没有创建并传入该端口。这是新清理消费接口与默认生产会话上下文没有接通，测试使用的是真实默认装配，不是测试自行构造了旧接口上下文。

后续修复应核对新异步数据库拥有者的创建、向正常和受限会话提供同一有效端口，以及关闭时的实际任务收场。单纯在测试上下文补属性不能修复真实默认生产入口。本轮遵循失败先报告的边界，没有修改生产、补测试适配或重新运行。

证据包括 fixed-*-command.json、fixed-*-result.json、fixed-*.log、fixed-*.xml、fixed-*.exit、fixed-*-progress.jsonl、fixed-metadata.json、fixed-bootstrap-state.json 及外部运行脚本。最终工作区干净。
