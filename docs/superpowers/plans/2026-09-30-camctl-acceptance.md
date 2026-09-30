# camctl 输入受理模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 使 run 与 submit 共用精确解析、首次原子受理、请求复用和独立 ACK 处理。

**组织建议：** 文件读取与解析在写事务外完成，业务结构校验采用纯函数；请求是否已受理和 ACK 依据在完整事务中再次取得。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用 K2 精确 JSON、Draft 2020-12 jsonschema 及本地 referencing.Registry；规则和默认值来自驱动静态目录。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [输入契约](../../architecture/plan-input.md)、[受理与身份](../../architecture/plan-acceptance.md)、[类型适配](../../camctl/data-types.md)、[自动预览](../../architecture/preview-obtaining.md)、[ACK](../../architecture/status-reports.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

每次输入仅一次完整文件读取尝试；失败或 JSON 不完整时不得从片段取得请求或 ACK。整份拒绝优先于单动作失败。已成功受理的 request_id 复用原实例和正文，跳过本次正文校验，仍独立处理 ACK。首次受理的计划、全部动作、合法关联、失败诊断及受理序列共同提交；动作校验失败不产生执行事实或尝试。ACK 有效与否不阻断计划处理。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

原输入与有效执行定义分别保存。静态规则无效属于规则处理错误，状态库读取失败属于状态库错误；二者不能作为用户参数错误。

| 类型 | 字段或含义 |
| --- | --- |
| `InputRead / ParsedInput` | 完整输入或带阶段的读取/解析失败；只有成功 ParsedInput 才含可检查字段。 |
| `AcceptanceContext` | 命令模式、不可变配置、静态驱动定义及可选 submit 交接探测端口。 |
| `BodyDecision / ActionValidation` | 整份拒绝或完整可受理动作集合；每个动作保留原输入、有效参数、关联与本动作失败。 |
| `AckDecision / AcceptanceResult` | ACK 未提供、有效或无效；计划首次受理、复用或拒绝及完整提交凭据分别表达。 |
| `AcceptanceRepository` | 原子处理输入的端口，在事务内决定请求复用、正文校验、独立 ACK 和需要的交接判断。 |

整份 JSON 读取或解析失败时不进入下表，保存输入诊断且不吸收 ACK。成功解析后先查请求成功受理关联，再按下表组合。

| 计划结果 | ACK 有效 | ACK 无效 | ACK 未提供 |
| --- | --- | --- | --- |
| 首次可受理或原请求复用 | 保存或复用全计划，并单调吸收 ACK | 保存或复用全计划，保持确认位置并保存 ACK 错误 | 保存或复用全计划，确认位置不变 |
| 首次整份拒绝或请求 ID 非法 | 保存拒绝，并单调吸收 ACK | 保存两个独立诊断，确认位置不变 | 保存拒绝，确认位置不变 |

ACK 有效但水位不推进仍可满足同步责任。状态库失败或结果未知不输出上述已完成结果；先沿原操作核实。计划 pending/running/completed 按全部动作终态与是否曾开始的完整决策表计算。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/acceptance/input.py` | 一次读取结果与解析组织。 |
| `apps/camctl/src/camctl/acceptance/schema.py` | 精确数字 Schema 适配和本地引用。 |
| `apps/camctl/src/camctl/acceptance/rules.py` | 公共结构、动作范围及错误优先级。 |
| `apps/camctl/src/camctl/acceptance/links.py` | 本计划来源、组及自动预览关联。 |
| `apps/camctl/src/camctl/acceptance/service.py` | 统一原子输入用例。 |
| `apps/camctl/src/camctl/acceptance/ports.py` | 窄仓储与静态目录接口。 |
| `apps/camctl/src/camctl/persistence/repositories/acceptance.py` | 请求复用、注册、ACK 及交接事务。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享类型](2026-09-30-camctl-contracts.md) | K1—K3 精确值、存在性及边界。 |
| [驱动](2026-09-30-camctl-devices.md) | D1 静态参数规则；不调用设备。 |
| [持久化与历史](2026-09-30-camctl-persistence.md) | P3/P4 与 H1/H2 提供完整提交与核实。 |
| [会话](2026-09-30-camctl-session.md) | A4 使用 S4 的事务内接纳探测；只需端口和规则先完成。 |
| [报告](2026-09-30-camctl-reporting.md) | R6 定义 ACK 和同步结束规则，由本次输入事务使用。 |

A1—A3 先用接口约束的静态目录与仓储替身测试。A4 必须组合完整事务及 S4 接纳规则；A5、A6 验证通知、旧请求和真实文件入口。ACK 规则先提供纯函数，不等待报告生成进程实施完。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 部分输入不能提取 request_id 或 ACK。 | A1，`test_partial_read_has_no_identity` |
| 规则损坏不能写成用户参数错误。 | A2，`test_invalid_schema_is_rule_error` |
| 自动预览冲突使全部冲突取回失败。 | A3，`test_all_duplicate_previews_fail` |
| 已受理 ID 携带非法正文仍复用。 | A4，`test_retry_skips_body_validation` |
| 正文拒绝时有效 ACK 仍生效。 | A4，`test_rejection_does_not_block_ack` |

## 实施任务

### A1 一次完整读取及解析

**预计文件：** `apps/camctl/src/camctl/acceptance/input.py`；测试为 `apps/camctl/tests/unit/acceptance/test_input.py` 和 `apps/camctl/tests/integration/acceptance/test_input.py`。

**接口与依赖：** 提供 `parse_input(read: InputRead) -> ParsedInput | InputDiagnostic`；异步 `read_input(path: Path, files: InputFileReader) -> InputRead` 只组织一次打开及完整读取。前置交付：K2、F1 的输入文件读取端口；可以先用端口替身。

- [ ] 编写失败用例。建立 `test_partial_read_has_no_identity`，前段含 request_id 和 ACK、后段读取错误，`assert diagnostic.request_id is None` 且 ACK 未处理；打开错误、编码错误、重复键、非法 Unicode 及非 JSON 常量分别分类。读中断 `assert open_count == 1`，不重读。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/acceptance/test_input.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。保留路径、打开/读取/解析阶段及实际错误；完整解析成功才交给字段提取；诊断中的不可编码输入用转义表示。
- [ ] 再运行上述命令，要求全部 PASS，并核对 读取错误不注册计划、不推进 latest_plan_id、不改变 ACK。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/acceptance/test_input.py -q`，真实文件、CLI 和诊断事务组合，验证不存在文件及解析失败仍可驱动已有任务。
- [ ] 审阅实际接口、状态分区及失败路径，检查 run 与 submit 两个入口是否依据片段或异常默认取得身份；记录门禁证据，建议以“feat: 实现完整输入读取与诊断”形成独立提交。

### A2 精确 Schema 及分层校验

**预计文件：** `apps/camctl/src/camctl/acceptance/schema.py`、`apps/camctl/src/camctl/acceptance/rules.py`；测试为 `apps/camctl/tests/unit/acceptance/test_validation.py`。

**接口与依赖：** 提供 `validate_new_body(raw: JsonValue, catalog: CapabilityCatalog) -> BodyDecision`、`validate_capture_params(raw: JsonValue, definition: ParameterDefinition) -> ActionValidation`。前置交付：K1/K2、D1。

- [ ] 编写失败用例。建立 `test_invalid_schema_is_rule_error`，无效 Schema 或缺本地引用抛规则错误；1.0 和 1e0 满足整数，接近整数的长小数不满足。未知动作、MCU 未支持动作、名称重复与本动作参数错误混合，`assert decision.is_whole_rejection is True`；改变数组顺序结果范围不变。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/acceptance/test_validation.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。先检查公共结构与完整动作名称，再对支持动作校验自身字段；精确类型适配覆盖 integer、multipleOf、范围、const/enum/uniqueItems。默认值由 D1 单一定义应用，原值保持。
- [ ] 再运行上述命令，要求全部 PASS，并核对 整份拒绝与本动作失败没有相互误分类，Schema 处理不会联网。
- [ ] 审阅实际接口、状态分区及失败路径，检查 各动作的时间、policy、group、设备及参数是否遗漏自身校验；记录门禁证据，建议以“feat: 实现分层受理与精确参数校验”形成独立提交。

### A3 完整本计划关联与执行定义

**预计文件：** `apps/camctl/src/camctl/acceptance/links.py`；测试为 `apps/camctl/tests/unit/acceptance/test_links.py`。

**接口与依赖：** 提供 `prepare_plan(decision: BodyDecision, allocated: PlanIdentities) -> PreparedPlan`；PlanIdentities 含本次计划和全部动作身份，PreparedPlan 含完整成员、定义及关联。前置交付：A2、K1；C1/X2 拥有的执行定义类型。

- [ ] 编写失败用例。建立 `test_all_duplicate_previews_fail`，同拍摄被多个自动取回引用，`assert failed_auto_ids == all_conflicting_ids`；手动取回和拍摄不受该冲突影响。引用后置动作合法、组中失败拍摄仍按类型作为来源、取回填写公共 group 失败且不成为成员。跨计划来源不在受理时查询。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/acceptance/test_links.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按完整输入建立名称及组索引，保留失败动作及原字段；生成只读执行定义，可靠局部来源和自动关联随整笔受理保存。
- [ ] 再运行上述命令，要求全部 PASS，并核对 没有按数组顺序漏引用，没有把非法输入修正成合法执行定义。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部来源形式及自动预览冲突是否只影响定义范围；记录门禁证据，建议以“feat: 准备完整动作定义与来源关联”形成独立提交。

### A4 请求复用、独立 ACK 与原子提交

**预计文件：** `apps/camctl/src/camctl/acceptance/service.py`、`apps/camctl/src/camctl/acceptance/ports.py`、`apps/camctl/src/camctl/persistence/repositories/acceptance.py`；测试为 `apps/camctl/tests/integration/acceptance/test_acceptance.py`。

**接口与依赖：** 提供异步 `accept_input(input: ParsedInput | InputDiagnostic, context: AcceptanceContext, key: OperationKey) -> AcceptanceResult`；内部仓储 `process_input(command: ProcessInput, key: OperationKey) -> DbOutcome[AcceptanceResult]` 完成唯一事务。前置交付：P3/P4、H1/H2、A1—A3、S4/R6 的纯规则及端口。

- [ ] 编写失败用例。建立 `test_retry_skips_body_validation`，已受理 ID 改正文为缺失/非数组 actions，`assert plan_id_after == original_id` 且无新动作；建立 `test_rejection_does_not_block_ack`，非法正文配真实有效报告 ACK，`assert ack_watermark_after == report.coverage_end`。按六分区及并发同 ID、提交未知、全失败动作原子注册验证。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/acceptance/test_acceptance.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。事务内权威查请求，已存在立即复用并跳过正文；首次依完整规则注册或拒绝，ACK 独立判定及同步结束共同保存。submit 在同事务中执行 S4 的工作/锁判断；不能在第二次事务补交接。
- [ ] 再运行上述命令，要求全部 PASS，并核对 计划、全部成员、诊断、ACK 与必要交接没有部分保存。
- [ ] 审阅实际接口、状态分区及失败路径，检查 run/submit 是否各自维护一套受理或 ACK 逻辑，错误是否包装为业务拒绝；记录门禁证据，建议以“feat: 实现请求复用与原子输入处理”形成独立提交。

### A5 父计划状态及提交通知

**预计文件：** `apps/camctl/src/camctl/acceptance/rules.py`、`apps/camctl/src/camctl/acceptance/service.py`；测试为 `apps/camctl/tests/unit/acceptance/test_parent_notification.py` 和 `apps/camctl/tests/integration/acceptance/test_parent_notification.py`。

**接口与依赖：** 提供 `derive_plan_state(actions: Sequence[ActionManagement]) -> PlanState`；提交后的 `notify_acceptance(result: AcceptanceResult, notifier: WorkNotifier) -> None` 使用 Q3 端口。前置交付：A4、Q3；ActionManagement 含状态及是否实际开始。

- [ ] 编写失败用例。在 `test_plan_state_uses_started_fact` 中部分动作未经执行取消、剩余 pending，`assert state is PlanState.PENDING`；曾执行过且其他未来动作 pending 仍 RUNNING，全部终态 COMPLETED。取消受理等待后实际提交成功，`assert notifications == 1`；回滚不得发成功通知。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/acceptance/test_parent_notification.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。依据持久化执行开始事实派生父状态；接手方消费完整提交结果后通知，退出前通知丢失由新会话的持久化发现接续。
- [ ] 再运行上述命令，要求全部 PASS，并核对 全部四种状态维度组合成立，通知不依赖原等待者仍存在。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/acceptance/test_parent_notification.py -q`，真实并发 submit、事件回放和调度发现组合，覆盖提交后通知前进程退出。
- [ ] 审阅实际接口、状态分区及失败路径，检查 后续动作结果仓储是否同事务派生父状态而非单独修改；记录门禁证据，建议以“feat: 派生计划状态并交接受理结果”形成独立提交。

### A6 两个入口及原输入恢复组合

**预计文件：** `apps/camctl/src/camctl/acceptance/service.py`；测试为 `apps/camctl/tests/integration/acceptance/test_entrypoints.py`。

**接口与依赖：** 使用 B6 的真实 run/submit 与 A4，不新增旁路受理入口。前置交付：S3/S4/S6、R7、B6；先验收无设备范围，新增处理器后继续复验。

- [ ] 编写失败用例。在 `test_bad_new_input_preserves_existing_work` 中已有待执行任务配坏新文件，`assert old_action_progressed is True` 且新增诊断进入报告；删除主程序原输入后重启仍按持久化状态恢复。有效 ACK、无效 ACK、状态库错误与坏正文各组合验证实际 CLI 结果。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/acceptance/test_entrypoints.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。补齐输入→完整事务→调度通知→报告→再次输入有效 ACK 的组件集成，保存业务与会话结果的独立预期；真实客户端确认由 I4 验证。
- [ ] 再运行上述命令，要求全部 PASS，并核对 原请求永久保存且恢复不依赖输入文件，所有受理验收有测试归属。
- [ ] 审阅实际接口、状态分区及失败路径，检查 全部读取、复用、拒绝和 ACK 分支的同类风险；记录门禁证据，建议以“test: 验证真实入口的输入受理闭环”形成独立提交。

## 模块完成门禁

A1—A5 的纯规则及真实受理事务通过；A6 用两个真实入口证明旧工作、输入诊断与独立 ACK 的协作。输入、规则、状态库错误各有分类，首次受理及重送保持相同身份和原事实。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
