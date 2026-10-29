from pathlib import Path
import json,sqlite3,collections
BASE=Path('/tmp/camctl-integration-all-final-20261026-u19lkvoe')
OUT=Path('/tmp/camctl-full-272f-diagnosis')
for group,task in [('reporting','009-camctl-reporting'),('cancellation','008-camctl-cancellation')]:
 p=OUT/group;a=BASE/'tasks'/task/'artifacts';s=json.loads((p/'classification-summary.json').read_text());progress=json.loads((a/'progress.json').read_text());result=json.loads((a/'result.json').read_text());source=json.loads((BASE/'source.json').read_text())
 s.update({'source_commit':source['head'],'source_snapshot_sha256':source['snapshot_sha256'],'original_artifacts':str(a),'collected':progress['collected'],'outcomes':progress['outcomes'],'finished':progress['status']=='finished','exit_code':progress['exit_code'],'elapsed_seconds':progress['elapsed_seconds'],'confirmed_merge_introduced_production_first_causes':0,'confirmed_environment_runner_first_causes':0,'business_pass_claim':False})
 (p/'summary.json').write_text(json.dumps(s,ensure_ascii=False,indent=2)+'\n')
# Preserved database, opened read-only: reporting's state assertion is after ineffective dispatch.
rdb=BASE/'tasks/009-camctl-reporting/tmp/pytest/test_failed_source_error_rebui0/state.db'
c=sqlite3.connect(f'file:{rdb}?mode=ro',uri=True);c.row_factory=sqlite3.Row
facts={'source':str(rdb),'mode':'ro','actions':[dict(x) for x in c.execute('SELECT id,type,status,execution_started,error_code FROM actions')], 'activities':[dict(x) for x in c.execute('SELECT id,action_id,dispatch_state,baseline_state FROM device_activities')], 'operation_runs':[dict(x) for x in c.execute('SELECT id,kind,status FROM operation_runs')], 'operation_attempts_count':c.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()[0]};c.close()
(OUT/'reporting/failed-source-preserved-db.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2)+'\n')
report='''首轮 reporting：280 项失败的首因诊断

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
'''
(OUT/'reporting/diagnosis.md').write_text(report)
cancel='''首轮 cancellation：231 项失败的首因诊断

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
'''
(OUT/'cancellation/diagnosis.md').write_text(cancel)
print('reporting/cancellation diagnosis and summaries saved')
