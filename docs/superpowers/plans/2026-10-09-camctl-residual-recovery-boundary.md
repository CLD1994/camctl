# 残留收场恢复的异常边界执行计划

本计划沿[模块依赖与装配](../../camctl/module-contracts.md#依赖与装配)及[原调用保存与恢复](2026-10-09-camctl-result-round-runtime.md)处理残留收场的恢复前置。业务流程接收恢复端口，SQLite 的异常转换由装配层承担；没有可靠保存原事实时，后续旧尝试恢复、候选读取和设备装配均不能开始。

## 起点与行为契约

起点为 `0404537`。公共契约目录实际为 189 passed、1 failed，失败是 `capture.residual → sqlite3` 的导入关系。`capture.residual.residual_flow` 仅在 READ、文件观察和媒体恢复回调的异常分类中使用该模块。`bootstrap.flows._resume_actual_file_facts` 已提供相同的回调顺序和状态库异常转换，可以直接复用。

装配入口继续接受原三个回调。它先恢复 READ，再恢复文件观察，最后等待媒体结果恢复；每步可靠完成后才能进入下一步。残留业务流程取得的 owned 连接仍由业务流程在 `finally` 中关闭。原操作键、请求、时刻、未知保存责任和既有业务终态保持；已经可靠完成的前置保存不因后一步失败而撤销。

| 前置结果 | 必须执行的行为 |
| --- | --- |
| 没有待恢复事实，或所有适用回调可靠完成 | 沿原旧尝试恢复、孤立查询结算和残留候选流程继续。 |
| 回调抛 SQLite 错误或 ConsistencyError | 装配层转换为 StateDbFailure，保留原 cause；停止后续回调和业务步骤，关闭 owned，原保存责任由原拥有者保持。 |
| 回调已经抛 StateDbFailure | 原异常直接传播，不重复转换；停止后续业务步骤并关闭 owned。 |
| 调用方取消当前协程 | 原 CancelledError 传播，停止后续步骤并关闭 owned；不把协程取消解释为业务取消或保存成功。 |
| 回调抛其他异常 | 保留原异常类别，不解释为 SQLite 错误；后续步骤不执行，owned 仍关闭。 |

普通运行和残留入口继续使用原普通 READ 资格；受限 winddown 继续使用原受限恢复资格。取消入口的原专用恢复边界不改变。

## 实施步骤与门禁

1. 使用公开 `bootstrap.residual_flow` 和内存端口替身建立原恢复行为控制，覆盖各回调错误、先后次序、查询及 factory 阻断、owned 关闭，以及已有状态异常、协程取消和普通异常的保真。单元不访问真实数据库、文件系统或线程。结构反例使用现有 AST 导入关系测试，不用字符串搜索替代实际依赖验证。
2. 建议装配层把现有共同恢复 helper 封装为一个异步端口，传给残留业务流程；业务流程只等待端口完成，删除原生 SQLite 的导入和异常转换。内部参数名及函数组织属于实现建议，原 bootstrap 外部回调签名和上述行为是要求。
3. 根独占运行局部单元与公共契约目录，确认结构反例转绿、原父子投影和报告字段校验保持。不得通过重新导出 sqlite3、放宽允许边、删除守卫或吞掉异常关闭门禁。
4. 根按 bootstrap 单目录复验原 READ 默认保存门、有限 RESULTS 五入口恢复及残留行为。实际 COMMIT UNKNOWN、ROLLED_BACK、原 request/key/T1 和 zero候选的证据以当前具体命令为准，不从历史总数量反推本轮范围。
5. 独立核最窄生产 diff 与实际证据后，按用户授权整体提交当前阶段变更。结果保存事件守卫、未决集合语义及完整 apps/camctl 目标继续分别推进。

## 实施与验证记录

2026-10-09，Linux 开发容器、Python 3.11.16。用户要求直接整体保存当前本地变更，`fb52335` 保存本计划和未执行的十一项控制测试，未声明生产迁移或测试完成。随后装配层保留原三个回调，使用既有 `_resume_actual_file_facts` 构成一个异步恢复端口；残留业务流程等待该端口，不再直接依赖 SQLite 异常。后续旧尝试恢复、查询、工厂和连接关闭代码保持原责任。

根 Agent 独占运行，所有进程均已取得终止状态：

| 范围 | 实际结果 | 日志 |
| --- | --- | --- |
| 迁移前的实际 AST 导入图 | 1 failed，唯一禁止边为 `capture.residual → sqlite3`。 | `/tmp/camctl-goal-residual-boundary-dependency-red.log` |
| 十一项公开残留恢复控制，迁移前与迁移后 | 分别 11 passed，0.20s 与 0.21s；这些是行为保持控制，结构红由 AST 门禁提供。 | `/tmp/camctl-goal-residual-boundary-before-unit.log`、`/tmp/camctl-goal-residual-boundary-after-unit.log` |
| 公共契约集成目录 | 190 passed，3.20s；包含实际 AST 依赖检查。 | `/tmp/camctl-goal-residual-boundary-contracts.log` |
| 全量单元 | 3784 passed、1 skipped、2 warnings，8.27s；warning 为既有同步测试的 asyncio 标记。 | `/tmp/camctl-goal-residual-boundary-all-unit.log` |
| 原保存前置及残留、受限收场的七个 bootstrap 文件 | 121 passed、1 failed，126.85s。唯一失败为录像恢复后处理等待动作成功时超时，动作仍为执行中。 | `/tmp/camctl-goal-residual-boundary-bootstrap.log` |
| 对唯一失败使用迁移前 `fb52335` 的两份生产文件单独核验 | 同一用例 1 failed，17.49s；原动作仍为执行中，不是这次端口迁移引入的失败。核验后恢复迁移代码。 | `/tmp/camctl-goal-residual-boundary-preexisting-baseline.log` |

上述 bootstrap 范围仅为 `tests/integration/bootstrap/` 中的 `test_read_default_save_gate.py`、`test_media_saved_result_consumers.py`、`test_file_fact_consumers.py`、`test_result_exhaustion_default_recovery.py`、`test_read_default_consumers.py`、`test_residual_winddown.py` 和 `test_restricted_winddown.py`，不是整个 bootstrap 目录。共同命令前缀为 `PYTHONPATH=/workspaces/camctl/apps/camctl/src apps/camctl/.venv/bin/python -m pytest`，后接上述七个完整文件路径与 `-q`。单独基线节点为 `test_restricted_winddown.py::TestNormalSessionResumesProgress::test_normal_session_continues_post_processing`。

独立只读审查实际生产 diff、资格接线、异常转换范围及无回调消费者后，未发现此次迁移的生产阻断。录像后处理的既有超时继续单独定位；结果保存事件守卫、未决集合语义及完整 apps/camctl 目标仍未完成。此记录不声明所有软件回归均已通过。
