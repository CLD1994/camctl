首轮 cancellation：231 项失败的首因诊断

受测源码与首轮 reporting 同为 9fbb734ca18e56934ba32942009e9ea46eccd089。008-camctl-cancellation 已自然结束，332 项收集，101 passed、231 failed；全部失败发生在 call，没有 setup/runner 中断。这里只读取既有输出、固定版本源码及保留文件，没有重新运行测试。

每个 nodeid、阶段、原堆栈位置均保存在 classified-cases.json。

| 数量 | 实际首因 | 分类 |
| --- | --- | --- |
| 182 | _world 返回未等待的 session_capture_assembly.factory 协程；140 项直接访问 runtime 成员被拒，42 项在等待不会到达的 transport.entered 门时超时 | 旧异步测试夹具 |
| 20 | TargetSettlement（11 项）或 CancellationRuntime（9 项）仍使用旧 owned 构造参数 | 旧内部构造接口 |
| 12 | 报告发布准备向 DbPublicationSession 传 OwnedConnection，publication.py:272 等待 database.write 时失败 | 旧数据库端口 |
| 8 | load_eligibility_facts 仍传 motor_permits，实际参数为 motor_permit | 旧内部查询参数 |
| 5 | CancellationRepository 仍以 start_entries 参数构造，实际没有该构造接口 | 旧内部构造接口 |
| 4 | 夹具将录像动作 13 直接种为执行过且成功，但没有它的设备活动；后续目标应用被一致性守卫拒绝，完成测试忽略前置失败后继续断言 | 不完整持久事实夹具 |

42 项 TimeoutError 的共同首因不是资源不足。31 项经 test_hasselblad_preview_fixed_boundary.py:51 的 world 等待；另 11 项分别在 mixed_scope.py:79、preview_fixed_boundary.py:242、start_boundary.py:120/170/252 等待。所有路径先调用同一个 capture/test_host_photo_flow.py::_world。该函数 :434 直接 runtime = factory(owned, "cam-1")，而实际 factory 是 async def。capture_handler 在真正使用传输端口前即需访问 resume_capture_completions，因运行对象仍是协程而不能抵达 transport.entered。原日志只呈现门等待的 TimeoutError，没有直接记录这些后台任务的异常全文；这一先因由共享夹具和实际调用顺序证明，不把推断写成已采集的后台异常。

4 项状态类失败来自同一 pipeline：test_effects.py:63-103 插入 action 13 为 status=3、execution_started=1，仅给 action 11 创建活动。首个 test_terminal_target_succeeds_without_rewriting 取得 ROLLED_BACK/ConsistencyError（已执行拍摄缺少设备活动:13）；两项 finish 测试忽略 apply_cancel_target 的结果后，cancel_item 93 仍为 PENDING，真实 finish 拒绝“取消成员尚未全部终态”；最后一项忽略失败的首次 finish 后读取 None.disposition。生产 capture_facts.load_start_facts/start_facts_from_summary 明确拒绝执行过却没有活动的拍摄事实。设备活动登记规格（scheduling-execution.md:431、waiting.md:115-119）要求持久保存原活动归属，不能靠终态绕过缺失事实。这四项应补足合法历史，不应删除业务终态/取消幂等断言。

a7755d81、272fda66、9fbb734c 对比：79 个失败测试函数三版全部相同；共享 _world、async factory、取消 runtime/settlement、motor_permit 签名、CancellationRepository、START 分区守卫三版均保持一致。因此本轮没有确认合并新引入的生产首因或环境/runner 首因。原跨组件 cancellation roundtrip 另有通过结果，但不能替代这 231 项尚未穿过夹具入口的业务覆盖。适配时应通过公开入口保留原边界场景，不把内部接口替换本身计为业务通过。
