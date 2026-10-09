# 结果核实错误与报告投影执行计划

> 执行 Agent 按任务逐项实施，使用 `superpowers:executing-plans`。本阶段仅准备计划；测试、生产修改须先经根 Agent 审查，pytest、暂存与提交由根 Agent 独占。

**目标：** 结果核实保存具有正式含义的完整错误，报告仅解释冻结历史；历史错误与报告工具、文件错误按各自责任分类。

**结构建议：** 错误生产者从公共登记取得阶段和详情契约，保存输入与事件守卫共用公共错误验证，报告生成边界将无法解释的历史字段转换为状态库前提错误。文件划分与辅助函数名称是实施建议，不固定现有模块内部结构。

**技术基础：** Python 3.11、真实 SQLite 历史与同步投影、现有精确 JSON 和 Draft 2020-12 校验器。复用包内 Schema 和本地引用注册表，不增加网络校验或依赖。

**正式依据：** [结构化事实与错误](../../camctl/database/common.md#结构化事实与错误)、[活动与采集字段](../../camctl/database/operation-fields.md)、[产物核实轮次](../../camctl/database/operation-fields.md#产物结果核实的责任与轮次)、[文件完成依据与产物检查](../../architecture/camera-capture.md#文件完成依据与产物检查)、[报告错误](../../architecture/report-format.md#错误)、[动作结束后的设备执行情况](../../architecture/report-format.md)、[报告冻结与重建](../../architecture/status-reports.md)、[报告维护失败](../../architecture/report-maintenance.md)、[公共错误登记](../../../protocol/errors/workflow-codes.json)、[公共报告 Schema](../../../protocol/schemas/status-report.schema.json)及[报告字段依赖](../../camctl/database/report-dependencies.json)。

## 项目约束与状态范围

历史为权威来源；保存同一事务更新投影，回放不执行外部操作。`error_json`、`last_error_json` 与采集结果中的适用错误使用公共 `code`、`stage`、`details` 结构。动作最终错误、实际驱动错误、结果核实错误和会话错误分别表达，不能从动作最终阶段反推实际驱动阶段。

未知驱动错误码保持开放，完整原错误不改名、不删除详情。框架生成错误使用既有公共登记，不因 Schema 允许未知字符串而自行增加公共内部码。不能由结果条目推定集合已结束，不能由读取错误推定目录为空；本计划不决定 RESULTS v2、不填补 v1 的集合完成能力。

报告按原 `report_id`、冻结 H 和覆盖水位生成；后续动作、配置、时间与设备观察不能补旧历史字段。同一合法冻结输入在 fresh Owned 下重建的字节保持一致。状态库前提失效停止本轮业务；普通报告计算工具或文件失败保留报告责任，遵守既有 `report_error` 规则。

本计划不修改错误码清单、报告外部字段、拍摄参数、读取或媒体业务资格，也不扩大为外部 SQL 修改历史的故障模型。已登记错误的阶段和详情从权威资源读取，不在测试或生产另存一套清单。

## 生产起点与问题归属

2026-10-09 的真实默认 timelapse 前置已完成公共受理、START、等待及三轮 typed RESULTS。每轮取得完整 VIDEO 文件，但 `result_files_listed/v1` 未提供集合结束依据。预算耗尽的 H35 将 RESULTS 流程收场为 UNCONFIRMED；H36 保存 `capture_json.error` 和 `last_error_json` 为仅有 `code: result_unconfirmed` 的对象。H38 动作已失败；H39 登记报告 3，冻结于 H38。H40 记录报告生成失败，Schema 指向 `plans/0/actions/0/device_execution/error` 缺少 `stage`。

上述数据来自此前对 pytest-918 状态库的只读检查及 `/tmp/camctl-goal-fixture-binding-gate.log`；原临时目录随后已清理，不作为永久验收材料。该节点没有形成本计划的新红绿门禁，后续应建立可控时钟的最窄反例，不重复依赖正常执行的长等待。

三个责任分开处理：

| 责任 | 实际生产边界 | 已观察或静态确认的问题 |
| --- | --- | --- |
| 错误含义及构造 | `capture/handlers.py::_close_check_unconfirmed`、`_confirm_timelapse_results`、`_finish_timelapse_conclusion` | 耗尽构造只有 code；UNSATISFIED 的内部采集错误也只有 code。后者不是 H40 的直接来源，具体 reason 映射还需决策。 |
| 保存结构保证 | `capture/models.py::ResultSetSave/_capture_result`、`persistence/repositories/capture.py::_ResultSetConfirmCommand/_result_check_guard` | 输入仅检查 Mapping，守卫未保证完整公共错误。合法 typed 类名并不证明对象满足 Schema；`ErrorValue` 目前也是普通 dataclass。 |
| 冻结历史解释及错误分类 | `contracts/public_projection.py::_read/_json`、`reporting/generation.py`、`reporting/encoding.py::ReportStream.section`、`reporting/worker.py`、`bootstrap/flows.py` | 原历史错误被原样投影，编码器正确拒绝；其 `SchemaValidationError` 被 worker 归为 REPORT，随后写 H40。历史解释失败应为 STATE，不能靠报告输出修补字段。 |

同类审计还包括 photo、timelapse 普通及取消路径对 `_close_check_unconfirmed` 的调用，以及应急补记向活动 `last_error_json` 写入的错误。应急构造目前缺 `details` 是静态结构发现，其公共错误身份与实际报告可达分区须单独核验；本计划不授权新增应急公共错误或修改停止语义。

保存恢复另有需要机械核实的接缝：`_finish_listing_result` 已将 compound ResultSetSave 放入原 RESULTS holder；耗尽的 `_close_check_unconfirmed` 则现场取得时刻/key 后直接调用仓储，没有对应会话 holder。仓储 `_CloseResultCheckCommand` 支持完整原键重送，并不证明消费者保留了申请。任务一的 UNKNOWN 反例必须同时覆盖这两个来源；若耗尽申请确实丢失，根 Agent 须先确认有效红再批准共享保存责任，不能以仓储重送绿色替代真实入口恢复。

## 错误含义的确定分区与待决策分区

下表只描述错误依据，不改变原调用 Outcome、集合结论或业务终态。

| 已可靠取得的依据 | 已有契约能够确定的错误 | 实施条件 |
| --- | --- | --- |
| 原有限 RESULTS 预算耗尽，仍不能确认产物要求 | `capture_result_unconfirmed`，登记阶段 `execution`，`details` 为真实活动 ID 和 `reason: outputs_unknown`。 | 可以实施；activity ID 不能用 action ID 代替。capture 与 last_error 如表达同一核实错误，应使用同一完整值。 |
| 驱动已提供明确的采集失败错误 | 原驱动 code、实际 stage、完整 details 保持；动作最终错误按已有拍摄失败规则另行构造。 | 可以实施结构验证和保真；不得用最后一个驱动错误替代独立的集合结论。 |
| 正式独立依据确认集合结束、归属范围可靠且本次没有任何要求的产物 | `capture_failed` 的 `no_outputs` 原因已有明确缺少产物的业务依据，阶段从登记读取。 | 必须有可靠空集合结论；空 v1 entries、列举失败或尚未确认的归属不能作为该依据。 |
| 正式独立依据确认集合，已有部分文件，但缺必需类别或数量不符 | 保存具体未满足规则和保留条件明确的文件；现有登记包含 `invalid_outputs`，但正式规则与评估模型未给出该分区到 reason 的唯一映射。 | 待决策：是否使用既有 `invalid_outputs`、如何区分无要求产物与存在部分要求产物，以及是否需要详情表达缺少规则。不得自行填写 `no_outputs` 或扩登记字段。 |
| 集合和归属确定，但其他必要文件检查不通过 | 原具体检查依据必须保留；不能只由 `assessment.is_complete == False` 选择统一错误。 | 待决策：逐项检查失败与部分文件缺失的错误映射，以及驱动原错误和框架采集错误的关系。 |
| 文件或集合依据未确定、文件信息读取失败 | 保持实际错误与未确认事实，在适用预算内继续，耗尽后按首行处理。 | 不生成 UNSATISFIED/KNOWN_FAILURE，不推断缺少产物。 |
| 原集合结论或动作终态已经可靠保存 | 保持原事实；报告重建只解释对应 H。 | 不改原 key 的申请，不覆盖原 Outcome，不重新列举。 |

`assess_capture_files` 只输出完整性、`explicitly_unmet`、缺少类别和未完成事实，没有输出错误 reason。其 `explicitly_unmet` 依赖独立的 `set_finalized`，当前 v1 普通路径不能从条目补该值。UNSATISFIED 待决策项只阻塞其错误生产与对应业务 fixture 修正，不阻塞已经确定的 UNCONFIRMED 和纯公共结构验证。不能把旧 `_finish_timelapse_conclusion` 的统一 `no_outputs` 分支视为正式映射来源。

## 公共结构、投影及失败矩阵

| 输入及发生边界 | 结果与保留责任 |
| --- | --- |
| error 为 None，所属分区允许无错误 | 保持空值；完成采集不得带错误，failed/unconfirmed 采集必须有错误。 |
| 完整登记错误 | 校验 `code/stage/details` 形状，并核登记的唯一 stage 和 details Schema；保持原结构与事实指向。 |
| 完整未知驱动错误 | 接受非空 code/stage、对象 details；原样保存、投影和编码。不得要求加入框架错误登记。 |
| 缺成员、空 code/stage、details 非对象、未知顶层成员 | 保存输入拒绝；事件守卫拒绝不合法事实，整个事务不提交。不得自动补空对象或猜测 stage。 |
| 完整原保存请求回滚或提交 UNKNOWN | 原完整请求、key、时刻及待存责任保持；fresh Owned 核实原事务再重送，不通过改错误成员形成同 key 新申请。 |
| 已持有或已提交的旧不合法完整请求 | 按状态前提错误保留诊断；不就地改 holder 或不可变历史。本计划不定义历史迁移。 |
| 固定 H 的必需历史字段结构或组合不合法 | 在历史解释到公共投影/报告生成边界抛已有状态前提错误，worker 返回 STATE；不记成普通生成失败，不查询设备补齐。 |
| 合法历史生成的报告遇到写入、同步、重命名或既有报告工具失败 | 保持现有 REPORT 分类和报告未完成责任；不影响已保存业务结果，不宣称文件发布。 |
| 普通参数 ValueError 或 Schema 资源/规则自身错误 | 不因为同属 ValueError 就改为 STATE。资源/规则错误不属于历史实例错误，保持所属现行分类；如发现该分类未定义，应单独报告。 |

## 审查重点

1. action 与 activity 不相等时，错误详情仍指真实活动，错误 owner 仍属原动作。
2. 实际 FAILED Outcome 可以同时含可靠文件；保存错误结构不删文件、不重列举、不改原 attempts。
3. 未知驱动码和开放 details 在写入、守卫、固定 H 投影及真实 Schema 编码中均保持。
4. 不完整历史按 STATE 停止；磁盘与报告工具错误仍按 REPORT 保留责任。
5. 既有终态、COMMIT UNKNOWN 和后续业务变化不能改旧 key、旧 H 或报告字节。

## 任务一：确定构造与保存边界

**预估文件：** 修改 `apps/camctl/src/camctl/capture/handlers.py` 的公共耗尽构造，`capture/models.py` 与仓储结果守卫；共用验证建议放在 `contracts/workflow_errors.py`。新增 `apps/camctl/tests/unit/capture/test_result_error_contract.py` 和 `apps/camctl/tests/integration/capture/test_result_error_history.py`。未决 UNSATISFIED 生产函数暂不修改。

**接口建议：** `validate_public_error(value: Mapping[str, Any]) -> None` 复用 `status-report.schema.json#/$defs/error` 和现有本地 Schema 注册表，验证完整形状；登记码附加检查正式 stage/details，未知码保留开放行为。返回值不重构、不标准化原错误。输入模型按现行 ValueError 约定拒绝，事件守卫转换为现有 EventValidationError；不改变 DbOutcome 协议。

- [ ] 写纯单元 `test_result_error_shape_rejects_incomplete_input`：缺 stage、缺 details、空 code/stage、非对象 details、额外顶层成员分别拒绝；BEGIN/COMPLETE 原阶段限制保持。
- [ ] 写 `test_registered_error_rejects_wrong_stage_or_details` 与 `test_unknown_driver_error_preserves_full_value`：使用公共登记原定义，不自建完整错误列表；未知 code/stage 与特殊字符、嵌套 details 保持。
- [ ] 根运行上述 unit 文件，确认是行为红；导入和 Schema 资源准备错误不计红。
- [ ] 写真实组件 `test_finite_results_exhaustion_saves_complete_activity_error`，参数化 photo/timelapse 与 action/activity 是否不同。复用公开 `consumer_world` 的 Acceptance、调度、真实 START 和 typed RESULTS，设置有界预算与可控等待；耗尽后核正式完整核实错误、原实际 Outcome、文件及累计尝试守恒。不能直接 SQL 写动作/集合状态。
- [ ] 在同文件写公共保存重送反例：原完整 ResultSetSave/key/T1 在 COMMIT 前后 UNKNOWN 后关闭原连接，fresh Owned 核实可靠 F，再原键重送。核历史完整组最多一份、首次响应与错误不变、零额外设备调用。该步骤沿现有责任 holder，不把新错误格式伪装成另一实际返回。
- [ ] 对上述两类来源分别从实际消费者触发故障；耗尽来源至少覆盖 photo/timelapse，再从正常默认入口消费共享责任。若需要新增 holder，建议保存完整 ResultSetSave、原 key 与首次 response，并由同会话 RuntimeDeps/三 factory 共享；仅重送已形成申请的前置保存不取得新的受限或取消业务资格。原请求可靠完成前不保存依赖终态；UNKNOWN/回滚保持申请并按 StateDbFailure 停止候选。具体集合和回调接线须依据有效反例由根协调，不自行扩大 flow 资格。
- [ ] 用合法 UNCONFIRMED 申请加完整错误建立仓储组件前提，控制事件输入的公共结构使守卫成为被测边界；拒绝后无半组历史/投影。待决策 UNSATISFIED 用例不靠更换假码提前获得绿色。
- [ ] 根确认实际红后，仅修已确定构造、共同验证和守卫。按新规范修受影响旧 typed fixture 的合法输入；不删除原业务预期。
- [ ] 审计全部 `_close_check_unconfirmed` 调用及结果集合直接保存入口，列出 UNKNOWN/终态未覆盖分区。未决 UNSATISFIED 单独提交状态表供决策后才能推进。

## 任务二：固定 H 的解释与报告分类

**预估文件：** `contracts/public_projection.py` 或 `reporting/generation.py` 的历史实例解释边界，复用现有 `PublicProjectionError`/`ConsistencyError`；`reporting/encoding.py` 保持通用编码器的 Schema 校验。建议新增 `apps/camctl/tests/integration/reporting/test_result_error_generation.py`，局部分支测试加到现有 `tests/unit/reporting/test_worker.py` 或 `tests/unit/contracts/test_public_projection.py`。

**输入输出：** 消费任务一实际保存的公共历史、`ReportingRepository.freeze_report` 的真实报告登记和原 `GenerationSpec`。对不合法历史实例返回既有状态前提异常，worker 的 `ResultFailureMessage.error_kind` 为 STATE；输出合法报告时 code/stage/details 完整，原未知错误不改写。

- [x] `test_exhausted_results_generate_schema_valid_public_errors`：公开历史先达到有限核实失败终态，再真实冻结和生成报告。按 H 时的实际活动状态核 photo 的 `still_running` 与 timelapse 的 `end_unconfirmed`，分别核设备执行错误和动作最终错误的完整来源；调用真实公共 Schema。报告不增加公开 attempts、内部预算或额外错误成员。
- [x] `test_exhausted_result_report_rebuilds_same_frozen_history_after_reopen`：保存首次字节和摘要，关闭重开 Owned，再发生合法后续业务变化，按同 report ID/H、水位重建；字节与摘要相同、无额外设备/RESULTS、原历史前缀保持。
- [x] 纯单元 `test_historical_json_projection.py`：公开 `project_public` 使用内存资源和固定 H 返回形状，核非法实例抛状态前提异常并保留字段路径；资源故障原样返回规则异常，不访问文件系统。
- [x] 真实报告组件 `test_result_error_state.py`：公共历史与冻结登记均真实，只在稳定历史读取端口返回不合法原字段。核无完整 staging/ready 发布、原 report 登记和 H 保持，真实 worker 返回 STATE；不通过 SQL 修改不可变正文制造条件。
- [x] `test_result_error_flow.py` 沿真实监督方、通信线程、管道、worker、报告 flow 和 `_drive_flows` 核 STATE 停止后续 flow。进程替身受实际接口约束，任务仍执行真实 `run_job` 并返回正式编码消息；不复制监督方分类逻辑，不启动实际子进程。
- [x] 文件故障与普通 ValueError 分类控制：合法原历史经过真实生成，实际写出及 flush 后由稳定 fsync 端口控制 OSError；worker 保持 REPORT，真实 flow 可靠记录报告失败后继续后续 flow，原冻结依据和未发布责任保持。
- [x] 根确认红后修最窄历史实例解释边界，实例错误转换成已有状态前提异常，SchemaRuleError 原样传播；没有修改 worker 对普通 ValueError 的分类。
- [x] 根独立核对固定 H、完整错误来源、半成品清理、worker 分类与 flow 停止的端到端数据流；最终目录门禁在下文记录。

### 历史实例校验的实施模型

`_read` 取得的 JSON 来自固定 H 的行值。正式依赖登记已为这些读取节点声明公共 Schema；实例结构与已登记错误含义在投影读取时核实，避免直到报告编码时才发现原历史不可解释。现有精确 JSON Schema 校验器可以直接复用，不新增依赖或复制字段、错误码清单。建议在 `_read` 共用边界按节点声明验证 JSON；本地公共错误 Schema 从权威登记生成 code、stage 和详情约束，使直接错误、媒体 error/issues 及诊断错误数组沿原 Schema 引用共同验证。错误详情中的普通同名成员没有错误 Schema 引用，仍按业务原值处理。内部函数组织属于实施建议。

| 原输入或实际故障 | 投影和报告必须执行的行为 |
| --- | --- |
| JSON 不合法，或不满足读取节点声明的实例 Schema | 抛 `PublicProjectionError`，诊断保留公开投影字段及来源列；worker 返回 STATE。 |
| 错误结构合法，但已登记 code 的 stage 或 details 不合法 | 同上；不能补值或把原登记码解释为未知驱动错误。 |
| 完整未知驱动错误 | 保持 code、stage、details 原值及调用方输入。 |
| 合法已登记错误或其他合法 JSON 字段 | 保持原公开值，使用原 H，不取得新设备观察。 |
| Schema 规则、包资源或必需引用无法解释 | 原 `SchemaRuleError` 传播；不转换成历史实例错误。原 `_registered_error` 入口也遵守此规则。 |
| 合法报告的文件写入、同步失败或普通参数 ValueError | 保持 REPORT 分类和原报告责任；不发布半成品。 |

根先运行纯投影反例，再运行真实历史读取、生成和 worker 反例；取得有效红后实施共同边界。组件替身只控制 `HistoryRepository.restore_entity` 的返回事实，其余读取委托真实仓储；不通过 SQL 修改不可变历史。生产 worker 的 STATE 结果向 supervisor、report flow 和会话候选停止的传播另行用实际接线验证。

2026-10-09，Linux 开发容器、Python 3.11.16：根独占 `/tmp/camctl-goal-result-error-state-red.log` 为 2 failed、1 passed、2.09s。真实公开耗尽历史、原冻结登记和接口约束的历史读取前提全部通过；缺 stage 在 `ReportStream` 才抛 `SchemaValidationError`，`run_job` 将同一错误返回为 REPORT。两个反例的原报告、历史前缀、ready 目录、设备调用和半成品清理断言保持。合法历史在真实写出及 flush 后实际到达 fsync 故障，OSError 保持 REPORT。这些有效红允许实施历史实例边界，不代表后续 flow 接线已验证。

根独占纯投影反例 `/tmp/camctl-goal-historical-json-unit-red.log` 为 23 failed、8 passed、0.22s；扩展对象与数组嵌套错误后的 `/tmp/camctl-goal-historical-json-expanded-red.log` 为 27 failed、12 passed、0.23s。非法结构、登记含义、已声明其他 JSON Schema、规则错误隔离和诊断路径均未满足；完整值、无 Schema 值和详情中的普通同名成员保真控制通过。真实监督方和会话 flow 的 `/tmp/camctl-goal-result-error-flow-red.log` 为 1 failed、1 passed、1.70s：同一不完整历史仍返回 REPORT，合法历史的真实 fsync 故障则可靠记录报告失败后继续后续 flow。

共同校验已实现为 `workflow_errors.validate_public_json`：独立本地报告 Schema 使用正式 workflow 登记生成错误约束，详情继续引用原登记；未知码保持公共开放结构。每个 JSON 节点按自身已声明的 Schema 验证，嵌套错误由现有校验器沿引用处理，不按成员名猜测。派生注册表缓存一份，校验器缓存最多三十二个引用；源资源与原历史不修改。`project_public` 为实例错误保留公开字段和来源列路径；`_registered_error` 使用共同公共错误验证并原样传播 `SchemaRuleError`。

局部绿色 `/tmp/camctl-goal-historical-json-unit-green.log` 为 46 passed、1 skipped、0.50s，包含三十九项纯投影及 worker 错误分类控制。完整单元、真实 reporting、flow 及后续回归的实际门禁仍须分别记录，不能由局部绿色推定完成。

独立审查发现 workflow 包资源的 decode/parse 故障和外部详情 Schema 规则尚未隔离。根独占 `/tmp/camctl-goal-historical-json-resource-red.log` 为 9 failed、40 passed、0.62s：两个入口的四种包资源故障均未返回 SchemaRuleError；read 的非法详情 type 抛 UnknownType，原 registered_error 规则校验控制通过。`_registry` 在资源读取边界转换具名资源异常并保留原 cause，派生注册表使用现有精确校验器检查每份正式详情 Schema；原 `$ref` 保持。随后 `/tmp/camctl-goal-historical-json-resource-green.log` 为 56 passed、1 skipped、0.52s，包含四十九项投影及 worker 分类控制。追加的详情非法约束和缺引用四项控制随最终单元门禁验证，不声明它们曾失败。

`/tmp/camctl-goal-historical-json-flow-gate.log` 为 6 passed、5.76s，包含两项新真实组合与既有报告 flow 四项。STATE 分区保留整个原报告登记和全部历史、删除半成品、不发布、不调用后续 flow，监督方可靠回收工作者；REPORT 分区实际到达 fsync 故障，可靠追加报告失败历史并继续后续 flow，原 H、水位、未发布事实及设备调用保持。此场景的 flow 之前没有其他业务 flow；发现 STATE 不撤销此前已完成的合法事务。

## 任务三：未决语义与同类入口交接

- [ ] 根与用户/正式规格维护者确定非空 UNSATISFIED 的 reason 分区；最少覆盖仅 OTHER、缺一类必需产物、数量不符和其他必要检查失败，以及明确驱动失败与文件同时存在。详情是否需要扩展属于公共协议决定，不能由实施者添加字段。
- [ ] 决策写入责任规格后，分别对 `_confirm_timelapse_results` 的 KNOWN_FAILURE 构造和 `_finish_timelapse_conclusion` 恢复消费写真实公开历史反例。只有独立正式集合依据才能建立该前提；保留符合条件文件，零新 listing，原 attempt 不变。
- [ ] 审计应急补记缺 details 的构造及活动错误的正式公开身份；先建立原应急资格和公开报告可达分区，再确认反例。没有正式身份映射时只报告，不补猜测的公共阶段或公开内部码。
- [ ] 汇报已闭合分区与未闭合分区；不得由 UNCONFIRMED 和形状测试绿色声明所有采集错误或报告恢复完成。

## 根 Agent 独占门禁

集成测试按各责任目录分开运行，实际结果在下文记录。

```sh
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/unit/capture/test_result_error_contract.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_result_error_history.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_result_confirmation.py apps/camctl/tests/integration/capture/test_result_consumer_saves.py apps/camctl/tests/integration/capture/test_result_file_recovery.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/reporting -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/unit -q
```

每个阶段先审有效红，再授权生产，再独立读绿色日志。集成 tests 不连接真实相机；替身遵守实际 Driver、Evidence 和 HistoryRepository 类型。根最终检查变更与状态矩阵，记录日期、Python 版本、日志与准确范围后按当前统一 checkpoint 方式提交，不要求历史拆分或额外快照。

## 当前交付与明确未完成

2026-10-09，Linux 开发容器、Python 3.11.16：root 独占运行新单元文件，`/tmp/camctl-goal-result-errors-unit-red.log` 为 26 failed、4 passed、0.10s；不完整结构、登记阶段和详情均未被拒绝，完整登记值及未知驱动值保真控制通过。真实组件文件 `/tmp/camctl-goal-result-errors-history-red.log` 为 4 failed、2.34s；photo/timelapse 与独立 activity ID 四项的公开启动、两轮实际结果、次数耗尽、动作失败及正式动作错误全部通过，活动核实错误仍只有 `code: result_unconfirmed`。这些有效红允许实施已确定的共同结构验证和耗尽构造；UNKNOWN 保存责任及报告生成还未验证。

UNCONFIRMED、公共形状、未知完整驱动错误保真和历史实例 STATE 分类有正式依据；非空 UNSATISFIED 错误 reason、必要检查失败映射及应急错误身份仍需决策或反例核验。RESULTS v2、历史格式迁移、完整媒体失败优先阶段、真实设备与物理断电验收不属于本计划完成声明。实施与验证范围见下文。

### 首批测试候选（2026-10-09）

已新增 `tests/unit/capture/test_result_error_contract.py` 的三十项候选：capture.error 与 last_error 两处分别覆盖十种不合法形状、登记 stage/details 的三个错误组合，以及完整登记错误和完整未知驱动错误的保真。单元的包资源读取由内存替身隔离，只声明本单元需要的公共 error 结构和一个登记；真实权威资源由组件测试使用，不在单元访问文件系统。

已新增 `tests/integration/capture/test_result_error_history.py` 的四项候选：公开 photo/timelapse × action/activity 同 ID 或不同 ID。真实受理、调度、START 和两轮 typed v1 RESULTS 保存完整 OTHER 文件；缺必需类别但集合未定继续有限核实，随后预算耗尽。断言活动核实错误完整且指真实 activity、原 Outcome/attempts/文件/历史前缀保持，不增加设备或 RESULTS 调用，不从 v1 推定集合已结束。没有借正常 v1 成功路径准备条件。

首批三十四项由根 Agent 独占验证，红灯及后续实施证据见下文。compound/耗尽消费者 UNKNOWN 矩阵、事件守卫及报告生成阶段单独验证。

### 公共验证的实施与红灯依据

根 Agent 独占运行的 `/tmp/camctl-goal-result-errors-unit-red.log` 为 26 failed、4 passed、0.10s；二十六项均为应拒绝输入没有抛出 ValueError，四项完整已登记/未知错误的保真通过。`/tmp/camctl-goal-result-errors-history-red.log` 为 4 failed、2.34s；公开前置、两轮原完整输入、有限耗尽及动作最终错误均通过，四项都在活动 last_error 仅为 `{code: result_unconfirmed}` 的断言失败。此证据只授权相应已确定分区，不覆盖 UNKNOWN 或报告生成。

共同验证已接入 `contracts/workflow_errors.py::validate_public_error`，通过既有精确校验器和本地注册表引用正式 `status-report.schema.json#/$defs/error`，登记码额外核唯一 stage 与 details Schema。完整未知码只校验公共结构；输入原值不改写。`ResultSetSave` 的 capture.error 与 error 复用该入口；原阶段、依据与时间规则保持。事件守卫和报告尚未修改；耗尽构造由根 Agent 独立维护。

2026-10-09，Linux 开发容器、Python 3.11.16：根独占复验公共结构单元为 30 passed、0.15s，四项公开耗尽组件为 4 passed、2.24s。耗尽错误使用正式 `capture_result_unconfirmed`、`stage=execution`、实际 activity ID 和 `reason=outputs_unknown`。较宽 capture 检查为 106 passed、3 failed；三项 UNSATISFIED 输入仍缺正式完整错误，所需分类正在等待用户决策，不能为使门禁通过自行选择 reason。新的四项耗尽保存 UNKNOWN 候选尚未执行，事件守卫、报告及历史 STATE 分类仍未闭合。用户授权将这些未完成项一并保存为本地 WIP 快照。

### 有限耗尽申请的保存与恢复

2026-10-09，根独占 `/tmp/camctl-goal-exhaustion-save-red.log` 为 4 failed、2.45s。photo/timelapse 的真实 COMMIT 前 UNKNOWN、关闭旧连接与 fresh Owned 前提通过，但下一次消费者重形成 request/key/T1；COMMIT 后 UNKNOWN 的两项则在可靠原 G 已存在时不核原键，直接继续业务。原实际 START、RESULTS 尝试和 OTHER 文件保持，故这四项是消费者保存责任缺失的有效反例。

建议新增独立 `PendingResultCheckClose(key, request: ResultSetSave)`，用于无新尝试承载的有限耗尽决定；不复用 AttemptFinish 或普通录像终态请求。首次调用仓储之前固定完整输入、key 和 T1，同会话 RuntimeDeps 与三个拍摄工厂共用集合。纯恢复只调用原 `close_result_check_unconfirmed`，不取得当前时钟、设备或新集合结论；收到可靠完整响应后清理对应等待并释放申请。handler 和默认五入口均先核已有申请，再读取新业务资格或筛选终态。此阶段不增加持久历史字段；进程退出后无会话申请时继续按持久核实事实恢复。

| 原申请及实际保存结果 | 恢复行为与依赖业务 |
| --- | --- |
| 申请仍持有，原事务可靠存在 | 使用原完整输入、key、T1 核实并复用；完成前不保存依赖终态，动作终态或绑定变化不能跳过原键。 |
| 申请仍持有，原事务可靠缺失且不会迟到提交 | 使用同一申请重送，不增加设备调用或实际尝试；可靠前保留责任。 |
| 原读取、重送或完整响应不可靠 | 保留申请并停止候选；默认入口按 StateDbFailure 传播。不得改原错误结构、key 或决定时刻。 |
| 申请可靠完成且响应完整 | 移除申请并清除原核实等待；后续业务仍按动作实际取消、终态与绑定资格执行。 |
| 没有会话申请，持久原结论已保存 | 使用持久原结论继续适用收场，不声称恢复丢失的旧 key，不重列举。 |

实施顺序为局部完整申请和实际消费者四项复验、默认共享前缀的独立反例、三个工厂及五入口接线、相关正常与取消回归。模块名称与集合组织是建议，行为表是已确定契约；不以四项绿色证明默认前缀或整个结果处理已完成。

### 有限耗尽恢复的阶段验证（2026-10-09）

Linux 开发容器、Python 3.11.16。根独占的真实消费者四项由 4 failed、2.45s 转为 4 passed、2.27s。默认普通入口在当前钟之前遗漏原键核实的反例为 1 failed、0.81s，接线后 1 passed、0.80s。扩展默认矩阵为 30 passed、14.25s：photo/timelapse × COMMIT 前后 × 五入口二十项，以及五入口再次 UNKNOWN／ROLLED_BACK 十项。首次保存前持有完整申请；fresh Owned 下核原事务；可靠前不筛选候选、不装配 factory，失败保持同一申请并传播 StateDbFailure；可靠后释放申请。

`/tmp/camctl-goal-exhaustion-capture-final-targeted.log` 为 14 passed、6.51s，覆盖新耗尽消费者、完整错误与原活动身份的保存重送。四项旧 UNCONFIRMED fixture 补完整正式错误后通过，保留原 key／时刻变化拒绝及不可变历史断言。`/tmp/camctl-goal-exhaustion-related-bootstrap.log` 为 70 passed、39.52s，原录像取消、持久发现、在途等待、原普通申请和 READ 恢复保持。取消完整目录 `/tmp/camctl-goal-withdrawal-fixture-cancellation-gate.log` 为 103 passed、8.56s：原两项测试装配真实撤回仓储及受实际文件接口约束的替身，可靠保存所有等待结果后才完成；默认生产撤回装配没有修改。

完整 capture 目录 `/tmp/camctl-goal-exhaustion-holder-capture-gate.log` 为 456 passed、11 failed、86.18s，发生在上述旧 fixture 修正之前。逐项核实为：四项 `test_record_result_retry::test_result_budget_close_uses_original_activity_on_resend` 使用旧不完整 UNCONFIRMED 输入，现已通过局部复验；四项 UNSATISFIED 输入分别位于 `test_capture_failure_activity_identity`、`test_result_confirmation`、`test_result_consumer_saves` 和 `test_result_file_recovery`，对应 reason 仍待决策；`test_capture_contract::test_send_wait_then_finish` 与 `test_timelapse_wait_runtime::test_backward_wall_clock_change_does_not_extend_current_session_wait` 依赖 v1 普通集合可立即成功的旧预期，尚未有可靠集合结束契约；`test_recording_finish::test_canceled_finish_registers_complete_files` 用录像动作期待保留取消产物，与录像取消放弃内容的规则不一致，需另核其业务前提。本阶段不删除这些测试、不选择未决语义，也不宣称完整目录已通过。

独立只读评审核三处生产改动、原仓储 key-first、完整响应、三个工厂共享、四个 handler 和默认五入口，未发现此次改动的生产阻断。公开业务终态或绑定失效后仍有耗尽申请的行为矩阵尚未新增实际反例；事件守卫、固定 H 报告分类及未决 UNSATISFIED 仍未闭合。录像 `close_unconfirmed_result_run` 的现场原申请也需单独核实，不由 ResultSetSave 这三处调用的覆盖替代。取消延时耗尽后文件保留的原有路径差异见[核实轮次计划](2026-10-09-camctl-result-round-runtime.md#取消延时摄影耗尽后的文件登记)。

最后接线后的全量单元 `/tmp/camctl-goal-exhaustion-final-unit.log` 为 3717 passed、1 skipped、2 warnings、7.80s。两个 warning 来自既有同步测试的 asyncio 标记。阶段 checkpoint 按用户授权保存全部当前工作，不为 Git 历史拆分追加验证；完整 app 目标保持，下一步按取消延时文件保留模型推进。

### 合法核实错误的真实报告生成

2026-10-09，Linux 开发容器、Python 3.11.16：根独占 `/tmp/camctl-goal-result-error-generation-gate.log` 为 3 passed、1.97s。公开 photo 和 timelapse 的实际 START、两轮 typed v1 RESULTS、有限耗尽及失败终态均由真实消费者保存，报告经真实 `freeze_report`、`generate_report_file` 和公共 Schema 校验。两处错误分别追溯到动作和设备活动；photo 的原活动为 ACTIVE，报告为 `still_running`，timelapse 的原活动为 UNKNOWN，报告为 `end_unconfirmed`。独立 action/activity 身份采用实际活动编号，没有用集合未确定推断设备已经停止。

重建场景关闭原连接，在 fresh Owned 上公开受理后续合法计划，按原 report ID、H 和水位重新生成。字节、摘要和大小相同，原 H 前缀逐行保持，设备与 RESULTS 调用没有增加。这三项只证明合法核实错误和固定 H 重建；不完整历史的 STATE 分类和文件错误的 REPORT 控制由后续独立矩阵验证，最终范围见下文。事件守卫及未决 UNSATISFIED 仍按各自任务推进。

### 历史实例校验的最终门禁与剩余边界

2026-10-09，Linux 开发容器、Python 3.11.16。最后生产修改及五十三项纯投影矩阵之后，根独占全量单元 `/tmp/camctl-goal-historical-json-final-unit.log` 为 3773 passed、1 skipped、2 warnings、8.21s；两个 warning 来自既有同步测试的 asyncio 标记。整个 reporting 目录 `/tmp/camctl-goal-historical-json-final-reporting.log` 为 367 passed、29.42s，包含合法错误报告生成、原 H 重建、真实历史实例 STATE、文件 REPORT 控制，以及实际工作进程等既有组件回归。报告 flow 的六项组合证据见上文。

公共契约目录的首次 `/tmp/camctl-goal-historical-json-contracts-gate.log` 为 180 passed、8 failed、3.36s。七项测试的相机生效参数缺 `type`，或媒体字段使用不符合正式 Schema 的空对象。合法前提补齐相机类型和未检查、时长未知的媒体结构；保留原 Decimal、父子入选、错误、checksum 和 size 断言，并增加两个真实资源的非法参数类型／空媒体路径控制。最终 `/tmp/camctl-goal-historical-json-contracts-final.log` 为 189 passed、1 failed、3.15s。

剩余一项为 `TestImportGraph.test_rules_do_not_import_adapters`：`capture.residual` 直接导入 sqlite3，只在原文件恢复 callback 的异常分类中使用。该导入在本阶段起点 `07f0e70` 已存在，本阶段没有修改 residual 或放宽依赖守卫。后续须沿 callback 的状态错误生产端与消费者闭合异常边界，保留保存责任及候选停止语义，不能以重新导出 sqlite3 或允许违规边掩盖责任分工。

独立只读审查最窄生产 diff、原资源隔离、派生错误约束及两处资源故障修复后，未发现该阶段的生产阻断。任务二的历史读取、报告生成、分类及后续 flow 停止已取得上述门禁。residual 的依赖边界已沿[残留恢复计划](2026-10-09-camctl-residual-recovery-boundary.md#实施与验证记录)迁移至装配层，公共契约目录取得 190 passed；结果保存事件守卫、未决 UNSATISFIED 和普通集合结束仍分别推进。当前全部工作按用户授权整体保存为本地 checkpoint，不声明完整 apps/camctl 或全部组件目录已通过。

## 活动与结果事件守卫的闭合执行

阶段起点为 `0f15fd1`。`ResultSetSave` 在构造时验证错误，但保存了调用方仍可修改的嵌套 Mapping；实际事件的 `_activity_guard` 和 `_result_check_guard` 没有再次核错误结构。直接 `confirm_result_set`、复合 `finish_result_check` 和耗尽 `close_result_check_unconfirmed` 均经过共同结果事件，必须在事务内验证实际写入值。合法类型的构造不能代替保存事件的验证。

确认规则来自[设备活动字段](../../camctl/database/operation-fields.md#设备活动字段)、本计划公共错误矩阵及既有正式错误登记。活动守卫接入 `DEVICE_OBSERVED` 的 CREATE、OBSERVE、RELEASE，`RESULT_SET_CONFIRMED` 的 COMPLETE、UNSATISFIED、UNCONFIRMED，以及 `EMERGENCY_RECORDED.FINAL`。新建活动检查完整 after；更新活动按当前可靠行、before 与 after 得到实际 after。没有改变错误列也要验证保留的原值；缺少判定所需字段时明确拒绝，不能将未知当作合法空值。回放仍只应用原历史，不执行这些业务守卫。

| 活动实际 after 中的字段 | 已确定的保存结果 |
| --- | --- |
| `capture_json=None`、`last_error_json=None`，所属分区允许没有结果或错误 | 保持合法空值；不补造事实。 |
| 非空 capture，status 为 running、completed 或 canceled | `error` 成员省略；保留驱动可靠提供的次数及秒数。 |
| 非空 capture，status 为 failed 或 unconfirmed | 必须有完整公共 error；保留其真实阶段、详情与指向。 |
| capture 状态与结果结论分区不符 | 拒绝；COMPLETE、UNSATISFIED、UNCONFIRMED 的新采集结果分别是 completed、failed、unconfirmed。 |
| 非空 capture 携带未知成员；次数不是安全整数、秒数不是有限非负数，或使用 null 代替未知可选值 | 拒绝；未知次数和秒数使用成员省略。 |
| 两处任一非空 error 缺成员、类型非法、空 code/stage、额外顶层成员，或登记 stage/details 不符 | 转为带 cause 的 EventValidationError，整个事务可靠回滚。 |
| 两处完整登记错误或完整未知驱动错误 | 保持全部原值，不重命名、不标准化，不将未知码降为错误。 |
| Schema、资源或引用自身不可解释 | 保持 SchemaRuleError，不混入历史实例错误。 |

实施步骤：

1. 根独占运行纯守卫反例。通过正式注册取得独立语义的活动、结果守卫，内存资源隔离公共 Schema，覆盖七个分支、新建空值、保留的非法错误、五个采集状态、两个错误位置及规则错误分类。先证明非法实例被旧守卫接受，不将资源或新接口导入错误计为有效红。
2. 真实受理、调度和设备 START 形成合法前提。合法 ResultSetSave 构造后修改调用方仍持有的嵌套字典，分别经三个公共保存入口验证缺 stage、登记 stage 错误和登记 details 错误。拒绝为 ROLLED_BACK，原 H、投影、流程、尝试及设备调用保持，没有半组历史。另核合法错误与允许空值、原 key 重送和原输入保持。
3. 建议把内部 capture 对象的共同验证放在原模型责任边界，由模型输入与活动守卫复用；内部函数和类型命名是实现建议。结果守卫核新 capture 与所属结论分区。业务错误验证复用现有 validate_public_error，并先排除 SchemaRuleError；不放宽登记或吞掉实例错误。
4. 横切审计实际错误生产者与全部活动分支。正常 START、STOP、残留与绑定收场没有新活动错误生产；已有完整有限 RESULTS 耗尽错误继续通过。UNSATISFIED 的错误 reason 及应急两个框架错误的正式身份仍待决定，不能猜测或仅补空详情。应急省略活动行时仍可能创建带错误的流程，活动守卫不代替其独立生产与组合责任。
5. 根顺序运行单元、capture 的新事务门禁与已有有限耗尽恢复门禁，必要时核相应 history 消费者。阶段报告区分守卫、生产者与历史重放的覆盖；按用户要求整体提交，不以未决分区或未执行候选宣称完整目标完成。

当前新守卫与事务反例尚未编写或执行；应急身份问题已单独提交用户决策。该未决事项只停止其错误生产与正式登记的相关实施。
