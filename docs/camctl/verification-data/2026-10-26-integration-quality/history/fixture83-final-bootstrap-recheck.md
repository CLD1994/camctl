# 异步数据库夹具迁移与 bootstrap 复验

基线固定为 `f143fbbecf68e9333ef6c4db36188b2c6c642630`，夹具迁移提交为 `2b58a7f35d7ab978d4d1f88a6fc07b9cea591d11`，分支 `test/quality-bootstrap-async-20261026`；独立工作区 `/tmp/camctl-quality-history-20261026` 已提交且干净。没有跟随上游新增提交，没有修改生产、目标测试或规模，没有运行两项大历史。

仅修改 `apps/camctl/tests/integration/bootstrap/test_output_binding_changes.py`，11 行新增、3 行删除。共享 `environment` 改为异步夹具，使用生产 `BusinessDatabase.open` 和原有合法连接工厂，把端口交给同一上下文。`AsyncExitStack` 登记异步 `database.close`，退出时先等待数据库线程及其连接关闭，再关闭准备与断言使用的独立连接和 runtime。共享文件的取回和清理用例在 `_save_photos` 之后继续消费该上下文，因此端口由整个夹具持有。`_save_photos` 真实受理、两张照片、驱动结果、提交和原断言保持原样；没有使用内联执行器替身或 SQL 造前态。

最终验证在同一 bootstrap 目录、同一顺序 pytest 中运行原 `test_submit_during_initial_discovery_is_not_lost` 和共享 `test_output_binding_changes.py` 的全部 13 项。结果是 **13 passed、1 failed，无 teardown error**；pytest 总计 38.33 秒，入口墙钟 39.525529190 秒。原目标 call 为 30.72 秒，在原有 30 秒成功预算触发 `TimeoutError`。未放宽预算、删断言或更改首次读取内真实提交的同步点。

原目标已完成照片准备并进入真实默认 `execute_command`，不再是夹具缺少 `database`。超时取消栈落在 `CaptureFlow._observe_windows` 的一次数据库读取；该位置不能证明其 SQL 卡住。取消收场后以只读连接核对最终数据库：3 个计划和 6 个动作均成功，包含两张照片、3 个旧取回和首次读取内新提交的取回；存在 2 个正式产物、8 个成功取回项、8 个已发布交付；两份报告覆盖到最大 change_seq 109。生产 `query_work_facts` 与 `classify_work` 对该最终快照返回 `can_exit_success`。这证明业务终态已经收敛，但不能证明超时当刻具有相同事实，也不足以定位会话未及时返回的具体原因。原目标仍应记为未通过。

最终复验证据以 `fixture83-final-bootstrap-*` 单列，命令结果保留基线、精确补丁及文件 SHA256；commit.json 关联最终提交。日志、XML、退出码和事件进度均保存。state-query.py 对应实际执行的只读查询源码，state.json 保存结果；没有为整理源码再次运行查询。外部插件仅记录 pytest 实际事件，不包裹函数或增加阶段计时；CPython 3.11.17、SQLite 3.53.1，PYTHONPATH 与私有临时根均在命令记录中。此同进程组件测试不表示整个程序内存预算验收。

此前 `latest83-bootstrap-*` 的接口不兼容结果、`fixed-*` 及更早通过结果继续按各自基线保留。最初适配试验的 `fixture83-bootstrap-*` 也独立保存，不能代替上述最终结果。
