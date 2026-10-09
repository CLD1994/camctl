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

- [ ] 写 `test_result_error_generates_schema_valid_report`：公开历史先达到有限核实失败终态，再真实冻结和生成报告。核 `device_execution.status=end_unconfirmed`、其完整核实错误及动作最终错误各自来源；调用真实公共 Schema。报告不增加公开 attempts、内部预算或额外错误成员。
- [ ] 写 `test_frozen_result_error_regeneration_is_identical`：保存首次字节和摘要，关闭重开 Owned，再发生合法后续业务变化，按同 report ID/H、水位重建；字节与摘要相同、无额外设备/RESULTS、原历史前缀保持。
- [ ] 写纯单元 `test_historical_error_shape_is_state_failure`：用受真实 HistoryRepository 返回形状约束的替身提供不完整 H 字段，核投影/生成边界抛状态前提异常并保留字段路径；不通过 SQL 修改不可变正文制造条件。
- [ ] 写真实报告组件 `test_uninterpretable_frozen_error_stops_without_publish`：公共历史与冻结登记均真实，只在稳定历史读取端口返回不合法原字段。核无完整 staging/ready 发布、原 report 登记和 H 保持；worker/flow 返回 STATE，候选和设备操作不继续。不能只测 `classify_worker_error` 而省去实际 Schema 与保存边界。
- [ ] 写控制分区 `test_output_io_error_remains_report_failure` 和普通 ValueError 分类回归：合法原历史经过真实生成，稳定文件端口控制 OSError；原报告责任保留、REPORT 分类保持，不误判 STATE。
- [ ] 根确认红后修最窄历史实例解释边界，仅捕获该边界的 SchemaValidationError/明确字段结构错误，转换成已有状态前提异常；不得全局把所有 ValueError 或 SchemaRuleError 改成 STATE。
- [ ] 根独立核对固定 H、完整错误来源、半成品清理、worker 分类与 flow 停止的端到端数据流。

## 任务三：未决语义与同类入口交接

- [ ] 根与用户/正式规格维护者确定非空 UNSATISFIED 的 reason 分区；最少覆盖仅 OTHER、缺一类必需产物、数量不符和其他必要检查失败，以及明确驱动失败与文件同时存在。详情是否需要扩展属于公共协议决定，不能由实施者添加字段。
- [ ] 决策写入责任规格后，分别对 `_confirm_timelapse_results` 的 KNOWN_FAILURE 构造和 `_finish_timelapse_conclusion` 恢复消费写真实公开历史反例。只有独立正式集合依据才能建立该前提；保留符合条件文件，零新 listing，原 attempt 不变。
- [ ] 审计应急补记缺 details 的构造及活动错误的正式公开身份；先建立原应急资格和公开报告可达分区，再确认反例。没有正式身份映射时只报告，不补猜测的公共阶段或公开内部码。
- [ ] 汇报已闭合分区与未闭合分区；不得由 UNCONFIRMED 和形状测试绿色声明所有采集错误或报告恢复完成。

## 根 Agent 独占门禁

以下新文件为预估名称，创建后命令须与实际文件一致；当前没有运行记录。

```sh
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/unit/capture/test_result_error_contract.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_result_error_history.py apps/camctl/tests/integration/reporting/test_result_error_generation.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_result_confirmation.py apps/camctl/tests/integration/capture/test_result_consumer_saves.py apps/camctl/tests/integration/capture/test_result_file_recovery.py apps/camctl/tests/integration/reporting -q
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
