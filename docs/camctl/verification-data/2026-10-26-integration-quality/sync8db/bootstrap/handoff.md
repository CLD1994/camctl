当前基线的 bootstrap 窄范围验证为 37 项通过、1 项超时。工作区保持干净，没有创建新提交。

源码：70149be69088714b8574900b57efa1b16fe3fa68；分支：test/sync8db-bootstrap-ojv22ku3；Python 3.11.17，SQLite 3.53.1。所有命令的 cwd 为 /tmp/camctl-sync8db-tests-ojv22ku3/bootstrap，PYTHONPATH 显式指向本工作区源码和测试；同一工作区的四次 pytest 顺序运行。

| 测次 | 测试范围 | pytest 时间 | 退出码 | 结果 |
| --- | --- | ---: | ---: | --- |
| inspection-01 | test_inspection_read_dispatch.py | 4.667 秒 | 0 | 24 通过 |
| output-binding-01 | test_output_binding_changes.py | 7.426 秒 | 0 | 13 通过 |
| initial-discovery-01 | test_read_discovery_default.py::test_submit_during_initial_discovery_is_not_lost | 34.148 秒 | 1 | 原 30 秒成功预算超时 |
| initial-discovery-diagnostic-02 | 同一单例，外置观察插件 | 33.366 秒 | 1 | 同一超时；诊断单独计量 |

正式单例在 2026-10-25 19:45:26.445 UTC、执行收据开始后 26.611 秒且尚未发生取消时保留 SQLite 备份。请求 3 已受理；全部 3 个计划、6 个动作成功，4 个取回动作形成的 8 份交付全部 ready，8 个副本均完成校验且设备槽为空；12 个运行和 12 个尝试均终态。对该备份调用当前生产 query_work_facts，持久化 required_settlements=0、unfinished_actions=0、pending_report_changes=false，classify_work=CAN_EXIT_SUCCESS。waiting_acknowledgement=true 按既定契约不阻止退出。

诊断测次观察到请求 3 在第一次实际 open_read 返回前受理，之后实际打开 8 次读取。业务完成后，会话实际工作事实持续为 required_settlements=3：取回 BackgroundFlow 计 1，CleanupFlow 计 2，其他拥有者均为 0。取回和清理的启动、完成及消费计数持续增加，支持“完成消费后立即新建空发现任务，再被工作事实计入退出责任”的归因。诊断没有改变输入、原预算、同步点、返回值或真实数据库 worker；它仍属于带观察包装的独立测次。

当前源码位置：bootstrap/background_flow.py:52 在 _task 为空时直接新建任务，65 的 check_completed 消费后令 _task 为空；session/service.py:1320 在每个流程前调用本地 check_completed，1333 紧接着推进该流程；1205、1212 将本地任务计数叠加到持久化工作事实。取回和清理共用此拥有者，修复时应审计同类消费者，并保留已完成失败必须交付、实际操作不得提前释放、取消/停止和新提交可继续发现等既有语义。验证应保留当前单例全部规模和 30 秒预算，并补齐真实数据库下空发现可退出、新工作到来可重新驱动以及错误/取消交付的反例。生产修复由用户提供后再合并。

inspection 文件的 58 个 assert 全部保留；另外两份文件亦未修改。四轮受版本控制的生产及目标测试 Python 文件摘要完全相同。首轮后仅按既有 conftest 生成包资源副本，唯一新增 Python 资源列在 summary.json 中。各 pytest 独立进程组均无残留。

证据入口：

- summary.json：全部结果、完整命令、UTC 时间、命令经过时间、源码比较、断言数和进程组收场。
- 各测次 receipt.json / pytest.log / junit.xml / progress.jsonl：命令、解释器、源码摘要、日志、XML 与每个节点的 setup/call/teardown。
- initial-discovery-01/before-cancel-state.json 和 before-cancel-state.db：首次取消前的真实数据库事实与一致备份。
- initial-discovery-01/before-cancel-work-facts.json：对备份只读调用真实工作事实查询和分类器的结果。
- initial-discovery-diagnostic-02/diagnostic.jsonl：首次读取内受理、8 次读取、实际会话事实、各拥有者责任数与跨轮累计计数。
- run_target.py、run_diagnostic.py、bootstrap_progress.py、bootstrap_diagnostic.py：证据采集入口和外置观察实现。
