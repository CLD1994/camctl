首轮 reporting：280 项失败的首因诊断

受测源码为 9fbb734ca18e56934ba32942009e9ea46eccd089，快照 cf4f504b7648bb5004fd20e1d66bfb57ab6a2956a33b6203724b6f0262591950。009-camctl-reporting 已自然结束，452 项收集，172 passed、280 failed；全部失败发生在 call，没有 setup/runner 中断。未运行任何新测试，也没有修改源码或测试。

逐 nodeid 的原日志位置、阶段、原错误及分类见 classified-cases.json。不是将整个模块概括为一个错误：280 项均保留独立身份。

| 数量 | 实际首因 | 已发生与未发生的业务边界 |
| --- | --- | --- |
| 266 | accept_input 的调用方传 OwnedConnection，实际接口等待 database.write；在 acceptance/service.py:121 抛 AttributeError | 输入尚未经过该接口可靠受理，后续冻结、生成、发布、恢复断言尚未验证 |
| 11 | 捕获测试夹具向 CaptureRuntime 传旧 owned 参数 | 运行对象构造即被拒绝；紧急结果报告断言尚未验证 |
| 2 | test_generation 向 ObtainRuntime 传旧 owned 参数 | 取回运行对象构造即被拒绝；失败来源报告断言尚未验证 |
| 1 | _save_failed_source_obtain 把历史测试的异步上下文管理器 _runtime 直接当运行对象；忽略 dispatch_ready 返回的逐动作错误 | 测试自己已保存 source RUNNING，但没有完成真实捕获失败；所以期待 FAILED=4 时仍为 RUNNING=2 |

最后一项不是由冻结边界重建错误造成。test_generation.py:159-163 调用 _runtime(owned)，没有 async with；实际导入 history/test_report_scope.py:205 的 @asynccontextmanager。dispatch_ready 捕获一般异常并返回结果列表，调用处不检查它。保留数据库显示 source 仍为 status=2，只有活动准备记录，没有操作流程或操作尝试；详细事实在 failed-source-preserved-db.json。这里只以源码和数据库确认调用未完成，日志未直接给出被 dispatch_ready 捕获的具体异常，因此不补造其错误文本。

合并归因以 a7755d81、272fda66、9fbb734c 三个固定版本比较。accept_input 的异步数据库端口相同；99 个失败函数中 95 个三版完全相同，其余 4 个冻结版保持 a775 内容，差异发生在尚未到达的后续测试体。_runtime 在 a775 已是异步上下文管理器，272 上游保留更早的 owned 构造；合并保留 a775，因此该 reporting 调用接缝并非新生产变更。比较数据见 failed-test-source-comparison.json 与 interface-source-comparison.json。

本轮没有确认 reporting 生产功能首因或环境/runner 首因，也不能据此确认全部报告功能通过。按当前集成规则，后续适配应使用模块公开业务入口，并保留冻结历史、错误、发布、重建的原业务断言；不应为旧 OwnedConnection 或私有运行对象扩大生产兼容接口。
