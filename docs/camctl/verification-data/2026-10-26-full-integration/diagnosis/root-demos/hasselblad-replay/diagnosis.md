Hasselblad 重试：4 项失败的实际首因

本次只读诊断覆盖 replay 的 shard-2（003）、shard-3（004）及最终完成的 shard-1（005）。受测源码为 354e38f95de04508a00d64d53bd17e213915548d，快照 768f6bb2b8a7b25d6c8b4c1a7f2bb2db0017fe26664711b4f949f185d6a68aae。三组全部自然结束，合计收集 29 项，25 passed、4 failed，失败均在 call。没有运行新测试，没有修改源码、测试或规格。

每项 nodeid、原 traceback 行号、结构化子进程错误和保留数据库事实见 classified-cases.json、facts-*.json。不能用修复首因后可能继续运行代替业务验收。

| 原 nodeid 末段 | 失败前已经发生什么 | 实际首因及分类 |
| --- | --- | --- |
| test_installed_hasselblad_complete_commit_receipt_loss_reuses_request[jpeg-host-0.000667] | 两份文件的完整大小/摘要已经在 history transaction 12、event 17 提交；注入器保留原 request/key，将回执改为 UNKNOWN | 核实经过注入 verify:485，断言独立连接且 query_only=1 失败。当前正式 HBF-02 要求独立只读核实，实际代码下一 worker 使用普通写连接。这是继承的生产/正式契约缺口 |
| test_installed_hasselblad_complete_commit_receipt_loss_reuses_request[jpeg-device-0.000333] | 同样完成原两份文件事务 12/event 17；原 request/key 和文件身份均保留 | 与上一项同一 verify 断言、同一生产路径；同一继承缺口 |
| test_installed_hasselblad_missing_start_result_never_replays | 原 START 意图已经保存；原真实 run 进入恢复钩子 | 注入 recover:438 访问 CaptureRuntime.owned，实际对象已经通过 database 端口访问持久化，抛 AttributeError 并使 run 退出。属于旧内部故障注入接口；原不重放与未知活动业务断言尚未完整验证 |
| test_installed_hasselblad_report_rebuild_keeps_frozen_bytes | 拍摄动作已经成功，原图/预览两份 outputs 存在 | 注入 intent 同步调用 async DbPublicationSession.record_intent 后直接读 coroutine.kind。真实进程先报告 report_error 并自然退出，测试仍等待 report-before-publish 90s，最后超时。属于旧异步故障注入接口；不是现场没有给够时间 |

完整提交回执丢失的生产边界

正式 docs/camctl/database/consistency-verification.md:113 HBF-02 要求准备或完整字节事务 UNKNOWN 后，结束原写事务，再用独立只读连接核实原键及完整申请。persistence-runtime.md 的线程状态表要求 UNKNOWN 停用原连接，没有取消该业务的只读要求。

实际流程是 capture/host_photo.py::_request_result 先经 database.write 保存，收到 UNKNOWN 后进入 _verify。_verify 直接提交 database.read 回调；DbExecutor._execute 对 UNKNOWN 写结果关闭旧连接，后续工作由 factory 新开连接。但普通 READ 在同一 worker 的下一有效连接上执行，不另开只读连接、不启用 query_only；bootstrap.lifecycle::_open_connection 使用 DbOpenMode.EXISTING_RW，runtime.open_existing 以 mode=rw 打开，未设置 query_only。原写连接保留在注入器 write_connections 中，因此后续新对象不应被 Python 身份复用掩盖。

原异常仅打印 `assert owned.connection is not original and query_only == 1`，没有打印两个操作数；单凭 traceback 无法知道 conjunction 的哪一半失败。本诊断不把推导出的 query_only 值写成现场已记录值。旧连接关闭、下一连接身份独立以及普通写方式打开来自实际执行路径的固定源码补证；两项保存标记分别证明提交已完成、原写事务已退出及原 request/key/文件身份。没有 complete-verifier 标记，说明注入器在实际核实结果返回之前拒绝了这条路径。该正式契约缺口应单独处理，不能直接删除只读断言来宣布通过。

报告门等待的先因

003 的 test_installed_hasselblad_repo0/deployment/camctl.log:37 已记录 `报告处理失败: AttributeError: 'coroutine' object has no attribute 'kind'`，随后 :42 记录 run 会话失败 report_error。module.log:9 记录 record_intent was never awaited，:15-16 记录第一次 run 自然 error；后续自动 run 同样失败。原 _fault_marker 没有 failure 参数，故只能在 90s 门截止时暴露最终失败。故障注入还继续依赖旧 self._owned，并有同步 save_publication 包装；只给第一个调用加 await 不能证明后面的原边界全部有效。本次未改这些注入接口，也未删任何冻结字节或历史断言。

合并归因与剩余验证

source-comparison.json 比较 a7755d81、272fda66、9fbb734c、354e38f9 四个固定版本。_install_fault、三个失败测试函数、host_photo._verify/_request_result、DbExecutor._execute、连接打开方法、record_intent/save_publication、CaptureRuntime.recover_attempt 及 HBF-02 原文四版全部相同。此次发现不是新的合并接缝；两项是继承的正式生产/契约缺口，两项是旧故障注入接口。没有证据把这些首因归为 runner、资源或环境故障。

这里的 25 项通过是既有真实重试结果；4 项失败各自仍缺少相应恢复、拒绝重放、完整申请复用或冻结报告重建的完成证据。后续应依现有公开入口和受控故障端口保留原业务断言，并单独解决只读核实契约，不能将旧夹具首因等价为功能通过。
