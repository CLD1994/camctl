# camctl 受理边界评审与修复计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 逐任务执行；只有明确选择子 Agent 执行方式时，才使用 `superpowers:subagent-driven-development`。复选框跟踪实际实施。

**目标：** 使一次完整输入经过身份判断、独立 ACK、分层校验、字段转换及原子保存后，形成符合契约的计划、动作、执行定义和错误事实。

**组织建议：** 保留事务外完整解析、事务内请求查询与完整提交的结构。校验阶段形成带类型的有效字段、来源判定、执行定义和错误详情，准备及持久化阶段消费这些结果。内部类型、方法名、文件拆分和任务拆分均为建议；下面的行为、状态分类及失败语义来自正式规格。

**技术基础：** Python 3.11、标准库精确 JSON、已有 `contracts.schemas` 精确 Draft 2020-12 校验器、本地 `referencing.Registry`、现有 SQLite 事务内核。默认值处理复用驱动边界；不引入新的通用校验框架。

**规格：** [输入契约](../../architecture/plan-input.md)、[请求身份与原子受理](../../architecture/plan-acceptance.md)、[参数类型与默认值](../../architecture/camera-capabilities.md)、[计划与动作字段](../../camctl/database/plans-actions.md)、[执行定义](../../camctl/database/execution-definitions.md)、[来源](../../camctl/database/sources-obtaining.md)、[自动预览](../../architecture/preview-obtaining.md)、[错误登记](../../../protocol/errors/workflow-codes.json)、[输入诊断](../../architecture/report-diagnostics.md)。实施前完整阅读所负责任务引用的规格。

## 评审范围和结论

评审从 `cli._run_session_command` 读取一次输入，沿 `acceptance.input`、`acceptance.rules`、`acceptance.links`、`acceptance.service` 到 `persistence.repositories.acceptance` 保存结果。为核对生产者和消费者，只追踪了默认静态目录装配、参数定义、动作执行定义及对应的公共错误结构。

事务内查询成功受理关联后跳过正文的安排是合理的；完整解析也使用精确 JSON，并拒绝重复键和非法 Unicode。主要问题出现在阶段之间：未经验证的身份被直接转换，完整合法计划 Schema 被用于整份拒绝，准备阶段重新解释失败输入，持久化阶段自行拼装执行定义及错误。

发现的问题涉及多个入口、持久化及派生关系，因此需要下述执行计划。本文不宣告整个 `acceptance` 模块完成评审；ACK 如何结束同步、submit 工作交接、取消后结果接手、通知和父计划后续状态转换另行评审。设备执行、目标硬件、物理断电和完整报告维护不属于本计划。

对照 `HEAD` 中相关分支，这些问题属于原有实现缺陷。前一份基础契约修复没有引入请求整数转换、根 Schema 校验、日期重新解析、开放目录或统一执行定义包装。

## 逐项问题及修复依据

### F1 请求身份在校验之前转换和查询

位置：`persistence/repositories/acceptance.py:202`。数据库已有请求 42 时，提交 `{"request_id":42}`、`"042"`、`"+42"`、`" 42 "` 或全角数字 `"４２"` 都得到 `REUSED`。缺失 ID、根数组和 `null` 则使事务回滚，未保存业务诊断；越界字符串在 SQLite 参数绑定时触发 `OverflowError`。

根因是直接使用 `int(document["request_id"])`，没有先检查根对象、字段存在性、类型、规范写法和范围。它违反“只有合法请求身份才能查询原关联”的不变量。

修复要求：复用 `json_field` 和 `parse_object_id`，分别判定根对象与请求字段；非法身份形成业务拒绝且诊断不附带合法 `request_id`。仅合法身份查询关联，查询失败仍是状态库错误。影响 run、submit、拒绝诊断及原请求复用。验收覆盖身份矩阵 M1、与 ACK 的笛卡尔组合 M2，以及缺正文的合法重送。

### F2 ACK 缺省、非法值和业务错误没有正确分开

位置：`persistence/repositories/acceptance.py:105`。已登记报告 1、覆盖水位 50 时，旧请求提交 `last_report_id: true`、数字 `1` 或 `"01"` 都吸收 ACK，水位变成 50；显式 `null` 被当成未提供。`"x"`、对象和越界 ID 使整个事务回滚。缺失请求 ID 配合法 ACK 也回滚，确认未保存。

根因是 `.get()` 折叠缺省与 `null`，随后直接 `int()`。它违反计划与 ACK 业务校验独立、合法身份转换在查询之前完成的契约。

修复要求：仅 `MISSING` 是未提供；所有其他值先按对象身份规则验证。格式错误和未知报告形成 ACK 业务错误，保留实际值及字段路径，继续计划分支。报告查询失败不能伪装成未知报告。有效 ACK 仍使用 `reporting.ack.decide_ack` 判定水位。影响新注册、原请求复用和计划拒绝三个入口，按 M2 验证共同提交及回滚。

### F3 完整合法计划校验代替了分层受理校验

位置：`acceptance/schema.py:63`、`acceptance/rules.py:65`。拍摄缺少 `params`、提供 `null`、时间类型错误、非法策略或动作额外字段时，整份计划被拒绝，合法同伴也未受理。根定义还会约束独立 ACK。计划名、动作名和组名的首尾 Unicode 空白语义却没有实现；`" plan "` 和 `" shoot "` 被接受。

根因是 `validate_plan_structure` 调用整个 `plan_schema()` 根规则，而规范已经明确要求使用 `$defs.plan_structure` 与 `$defs.action`。Schema 的名称规则明确把首尾空白留给语义校验。

修复要求：整份拒绝只承担计划公共结构、动作对象、名称、唯一性、类型及本版实现范围；全部动作检查完成后，逐动作执行公共字段和自身参数校验。使用本地 Schema 片段，不复制公共规则。名称语义使用原字符判断，不裁剪或归一化。覆盖 M3 的全部动作类型和字段状态；整份拒绝条件与动作错误混合时，交换数组顺序仍不创建实例。

### F4 有效普通列与原始例外字段的边界没有建立

位置：`acceptance/links.py:125`、`persistence/repositories/acceptance.py:482`。拍摄的 `2026-02-30 09:00:00` 在校验层已经判为动作失败，准备层却再次调用 `to_utc_micros`，整个事务因此回滚；取回同样会回滚。合法报告动作指定时间和组名后，数据库两列均为空。动作其他参数失败时，合法策略的 `max_delay_ms` 也被清空。

仓储还把完整动作原文直接存入 `input_fields_json`，合法 `name`、`type`、设备、时间和组同时出现在普通列和原始 JSON 中。正式字段契约明确要求按来源重建，不能保存两个可供读取方选择的表示。

修复要求：各公共字段独立形成有效值或原始例外，使用 M4 保存。准备阶段不再转换已经判为非法的字段；失败动作仍保留其他合法普通列。`input_fields_json` 保留原 `params`、`policy`、额外字段及非法公共字段，不重复合法普通列。影响原计划重建、调度查询、组成员、历史及报告读取；修复后必须审计这些读取入口的来源假设。非法值不补默认时间或合法组名，按矩阵验证往返重建。

### F5 生产受理目录与能力说明没有使用同一权威定义

位置：`bootstrap/lifecycle.py:48`、`bootstrap/application.py:109`、`devices/catalog.py:94`、`acceptance/rules.py:129`。describe 使用真实驱动定义，run/submit 却默认使用开放对象 Schema 的 `ConfigCapabilityCatalog`。声明一个未部署驱动后，受理仍能把未知参数类型和任意调整字段登记为合法动作。

真实 `Catalog.action_types()` 又用配置设备能力的并集表达整体动作范围。目标设备不支持录像时，若另一台设备声明了录像，受理将目标设备返回的 `None` 判成 `RuleError`；没有另一台设备时则整份拒绝。同一个设备能力不匹配会受无关设备配置影响。

修复要求：describe 与受理从同一份部署定义构建目录；未部署驱动或规则损坏是配置／内部错误，不能用开放 Schema 替代。整体动作实现范围、指定设备的动作支持、指定参数类型支持、定义取得失败必须分别表达，见 M5。参数类型选择及预览依据也从该边界取得。影响默认装配、目录端口及测试替身；验证跨设备支持差异与默认 CLI 装配。

当前 `default_driver_definitions()` 为空，拍摄处理器仍有 `NotImplementedError` 占位。修复不能凭测试替身或占位函数宣称真实驱动和处理器已经部署。无设备配置可继续处理已实现的无设备入口；相机配置引用未部署定义必须明确失败。实际设备接入是单独任务。

### F6 默认值掩盖必填字段缺失

位置：`acceptance/rules.py:163`。参数规则要求 `shots`，驱动同时提供 `shots: 1` 默认值时，调用方只提供 `type` 也能校验成功。

根因是先合并所有默认值，再只校验合并结果。它违反必填缺失不能被默认值掩盖、原输入校验不修改输入的契约。

修复要求：按选定参数类型先校验原输入，再对允许缺省的字段应用权威默认值，最后校验完整生效参数。规则／默认值造成的无效生效结果属于定义错误，不能写成用户参数错误。影响拍摄参数受理与 `devices.catalog.apply_defaults` 的同类路径；按 M6 覆盖 required、条件分支、显式值和缺省，保持原输入。

### F7 仓储没有保存各动作所属的执行定义

位置：`persistence/repositories/acceptance.py:516`。单张拍摄保存 `{"action_type":...,"driver_id":...,"effective_params":...}`，而契约要求 `{}`。取回也保存该包装，缺少 `selection_mode`。录像路径从不形成 `target_duration_ms`；既有 `CaptureDefinition.build` 没有接入这个路径。

根因是仓储使用统一字典替代动作模块拥有的派生定义，重复驱动身份与完整参数。它违反原输入、生效参数和模块执行依据分别保存的契约。

修复要求：对应动作边界构造、校验定义，仓储只保存最终对象；失败动作整列 SQL `NULL`，合法无专属字段动作保存 `{}`，录像、延时和取回按 M7 保存。合法参数无法派生完整定义是驱动／内部错误，不能补字段或归咎用户。影响受理、执行读取、恢复和历史重建；精确值、空值组合及首次值稳定性均须验证。开放占位目录中的录像输入只能证明当前仓储缺字段，不能作为真实驱动参数合法性的证据。

### F8 本计划来源的范围、失败及固定状态没有闭合

位置：`acceptance/links.py:167`、`persistence/repositories/acceptance.py:521`。本计划组和 `current_plan` 被当成跨计划来源，受理后没有成员关系；不存在的组仍为 `pending`。清理引用本计划不存在的动作也被登记为 `pending`。手动取回不存在的动作时，`invalid_source_reference` 未在动作错误登记中定义，最终缺少 `error_code`，事务回滚。

根因是来源准备仅处理取回的 `action_name`，将其他形式统一跳过；仓储又用“依赖集合是否非空”推断来源已固定。它不能表达合法空的计划来源。

修复要求：本计划动作、组、当前计划在受理时基于完整输入解析；实例和跨计划引用才延后。取回及范围清理按 M8 保存来源状态、真实计划和全部成员；有效空计划集合仍是 FIXED。使用已登记错误及完整详情，不生成占位来源。影响来源准备、来源成员事件、业务守卫及执行读取；审计 `_source_members_guard` 对范围清理的支持，不能只接入取回。实际选中文件和删除目标仍留到执行时。

### F9 自动预览缺少能力、时间与可靠关联判定

位置：`acceptance/links.py:173`、`persistence/repositories/acceptance.py:367`。来源明确不支持预览或自动取回时间不一致时，取回仍为 `pending`。来源明确支持预览时，关联行仍是 `preview_support=NULL`、`parameter_type=NULL`、`is_valid=0`；参数类型错误地从取回 `params.type` 读取。来源名不存在时，仓储强行解析其动作 ID，整个事务回滚。

修复要求：参数类型与预览依据来自来源拍摄的静态定义；时间、用途、可靠来源和全声明冲突集合按 M9 独立判断。未知来源保存 SQL `NULL`，不调用必然失败的实例查找；合法关联必须保存完整依据和 `is_valid=1`。影响自动关联、取消联动及历史报告的依据。来源拍摄自身失败不自动使取回失败；能力未知必须保留其真实原因，不能猜成不支持。

### F10 动作错误详情不符合机器协议

位置：`acceptance/rules.py:112`、`persistence/repositories/acceptance.py:568`。一般动作校验失败保存 `{"message":...}`，但 `action_validation_failed` 要求非空 `issues` 数组；实际校验这个详情片段失败。两个自动取回冲突时，每个动作的 `obtain_action_names` 仅包含自己，违反至少两个名称及完整冲突范围要求。

根因是校验结果仅携带一条文案，仓储无法取得字段存在性、原值、错误分类和完整冲突集合，于是临时拼装机器详情。

修复要求：校验及关联阶段形成结构化错误，原错误路径使用完整计划下标；缺省字段不补 `value`，显式 `null` 保留实际值。机器错误码从公共登记取得，未登记代码属于内部错误；一般文案不能替代详情结构。持久化与最终报告适配分别按同一详情规则验证。影响所有受理失败动作及相关业务守卫；审计所有 admission 错误生产者，验证一般字段错误、专用来源错误、预览不支持和完整冲突详情。

### F11 输入诊断错误阶段与真实读取步骤不匹配

位置：`acceptance/input.py:26`、`persistence/repositories/acceptance.py:290`、`cli.py:193`。读取端口把打开和读取放在同一次 `read()` 中，却没有带阶段的失败类型；所有 `OSError` 都写成 OPEN，测试甚至把“后半段读取中断”断言成打开失败。仓储保存的公共 stage 为 `input`，而报告只允许 `input_read`、`input_parse`、`admission`、`ack`；实际诊断片段校验失败。

修复要求：在执行真实文件操作的适配器中区分打开和已打开后的读取失败，完整读取仍仅一次尝试，失败不提供部分内容。内部步骤映射为公共阶段及对应错误结构，见 M10。影响 CLI reader、输入端口、输入诊断和报告适配；验证真实适配器分类、错误片段及无身份／无 ACK，并审计 decode、parse 分支的相同风险。

## 项目约束

- run 与 submit 共用受理规则；原成功关联及新注册判断均在同一写事务内。业务拒绝、合法 ACK 与应保存错误共同提交。
- 查询失败、写入失败、确认回滚和提交结果未知分别按现有状态库契约处理，不转换为不存在、业务成功或首次受理；未知结果通过原操作身份核实。
- 受理失败动作属于完整计划，执行开始标记为 0，不产生设备操作、尝试、delivery、删除意图或取消目标副作用。
- 所有数字和持久化 JSON 保持精确；不通过 float、舍入或裁剪修复非法输入。枚举、错误码和公共 Schema 不维护第二份完整清单。
- 单元测试隔离资源、文件、SQLite、线程和用户级状态；真实组合放到集成测试。测试替身受修改后的端口约束，不能让替身继续容忍生产已拒绝的状态。
- 不删除失败覆盖、不放宽 Schema 或数据库约束、不吞内部错误、不让正文拒绝阻断 ACK、不重新受理旧请求。原有计划及失败事实在重送、重启和历史回放后保持。

## 状态模型与反例矩阵

### M1 请求身份

完整读取且无歧义解析是以下所有分区的前提。未满足此前提时，只保存输入诊断，不提取身份或 ACK。

| 根及请求字段 | 关联查询 | 结果 |
| --- | --- | --- |
| 根不是对象 | 不查询 | 保存公共结构拒绝，无合法请求 ID，无法提取 ACK |
| 对象中请求字段缺省、null、错误 JSON 类型或非规范写法 | 不查询 | 保存请求字段拒绝，无合法请求 ID；独立判断 ACK |
| 请求规范字符串在 1 至 9223372036854775807 范围内 | 可靠存在 | 复用原计划，完全跳过本次正文；独立判断 ACK |
| 请求合法 | 可靠不存在 | 分层校验正文，完整注册或保存拒绝；独立判断 ACK |
| 请求合法 | 查询失败或结果不可解释 | 会话错误，同次事务不宣告部分业务处理成功 |

非法值集合至少包含缺省、null、布尔、数字、数组、对象、空串、0、前导零、正负号、空白、小数／指数文本、非 ASCII 数字及越界。合法值包含 1、42、超过 JavaScript 安全整数的值和 64 位上界。拒绝过而未成功受理的 ID 后续仍可首次受理。

### M2 计划与 ACK

| 计划分区 | ACK 缺省 | ACK 值或报告不存在 | ACK 合法，水位不推进 | ACK 合法，水位推进 |
| --- | --- | --- | --- | --- |
| 首次可受理 | 注册完整计划，保持水位 | 注册完整计划，保存 ACK 错误 | 注册完整计划，保持水位 | 注册完整计划，吸收 ACK |
| 原成功请求复用 | 复用原实例，保持水位 | 复用原实例，保存 ACK 错误 | 复用原实例，保持水位 | 复用原实例，吸收 ACK |
| 请求 ID 非法或首次整份拒绝 | 保存拒绝，保持水位 | 同一诊断保存两方错误 | 保存拒绝，保持水位 | 保存拒绝，吸收 ACK |

本表的字段判定以根对象已经可靠取得为前提。仅字段缺省是“未提供”，显式 null 属非法值。读取／解析失败或根非对象时，ACK 未能进入字段判定，应在内部结果中表示“未处理”，不冒充已经读取到的省略字段；建议为 AckDisposition 增加 NOT_PROCESSED。报告查询失败、写入失败或提交未知不属于表中的业务结果。合法但不推进的 ACK 仍可能结束同步责任；该生命周期另行评审，不能将“不推进”改成“忽略 ACK”。

### M3 校验层次

| 条件 | 处理及优先级 |
| --- | --- |
| 顶层必填字段、实际创建日期、计划名或额外顶层字段不合法 | 整份拒绝 |
| actions 缺省、非数组、空，元素非对象，动作名称缺省／非法／重复，类型未知／未实现 | 整份拒绝；不创建部分动作 |
| 已支持且名称合法的动作，自身设备、时间、group、params、policy 或额外字段非法 | 仅该动作受理失败，其他动作按各自结果保存 |
| 单动作错误与整份拒绝条件同时存在 | 整份拒绝优先，交换动作顺序不改变范围 |
| 定义、Schema、必要引用或内部转换失败 | 会话／内部错误，不归咎用户 |

每种动作的每个字段覆盖缺省、null、错误类型、格式／范围边界及合法值。另覆盖实际日期、名称 Unicode 空白、合法过去及未来时间、不支持策略和受理前无副作用。

### M4 原始字段与有效字段

| 字段分区 | 普通列 | input_fields_json | 对动作的影响 |
| --- | --- | --- | --- |
| name、type 已通过整份检查 | 保存对应列 | 不重复 | 为实例身份及规则选择提供基础 |
| 设备、时间、组适用且字段自身合法 | 保存有效值 | 不重复该字段 | 其他参数失败仍保留此有效字段 |
| 字段省略且允许省略／要求省略 | SQL NULL | 不补键 | 继续其他校验 |
| 必填字段缺省 | SQL NULL | 不补键 | 动作失败，详情不补 value |
| 字段提供但非法或类型不适用 | SQL NULL | 保留原键和值 | 动作失败 |
| params、policy 或额外字段提供 | 按所属派生列规则保存 | 保留原键和值 | 按自身规则判定 |

取消、报告提供合法时间时必须保存；省略时才是合法空值。除取回外，支持动作均可保留合法组归属。拍摄的整个 policy 合法时保存 max_delay_ms，即使其他字段失败；policy 自身非法则该列为空。生效参数、驱动绑定和执行定义只在整个动作通过校验时形成。

已受理动作的原输入读取须符合第一版公共 action 结构，并保留参数内部的必需字段和组合；该要求由首次固定定义判断，动作后续进入终态仍然适用。首次受理失败允许保留缺省或非法原值。读取只检查固定公共结构和保存事实之间的关系，不调用当前驱动定义、默认值或任务工厂，不查询来源或目标当前是否存在，也不重选已保存成员。

### M5 目录结果

| 动作、设备、参数类型与定义 | 处理 |
| --- | --- |
| 整体动作未实现 | 整份拒绝 |
| 动作已实现，设备字段非法、设备不存在或设备明确不支持此能力 | 动作失败 |
| 设备支持能力，参数类型缺省／非法／不支持 | 动作失败 |
| 能力和参数类型有效，参数值不符合有效规则 | 动作失败 |
| 配置引用未部署驱动，或声明存在的定义无法加载、Schema 损坏、必要语义不可解释 | 配置／内部错误 |
| 所有条件合法 | 取得同源 Schema、默认值、预览依据及任务依据；继续形成完整定义 |

测试须包含一台设备支持某能力、另一台不支持，以及添加无关设备前后保持同一错误范围。内部 `None` 的含义必须限定为可靠不支持，定义读取故障使用明确错误；不能兼任两者。

### M6 原参数与默认值

| 原输入条件 | 结果 |
| --- | --- |
| 有效规则要求的字段缺省，包括条件分支中的 required | 用户参数失败，即使驱动提供了默认值 |
| 可省略字段缺省且有权威默认值 | 原输入保持省略，生效参数补齐该值 |
| 可省略字段缺省且没有默认值 | 保持省略 |
| 字段显式提供，包括 null、false、0、空串 | 按实际输入校验，不用默认值替换 |
| 原输入满足有效规则，应用默认值后却违反规则或组合 | 驱动／默认值定义错误，不能把合法原输入记成用户错误 |

Schema default 是说明信息；定义中的默认值须与驱动实际规则同源。定义检查与具体参数检查均复用精确校验器；组合分支不能通过只查看根 required 数组代替完整原输入校验。

### M7 执行定义

| 首次受理结果及动作 | execution_spec_json | effective_params_json 与 driver_id |
| --- | --- | --- |
| 动作失败 | SQL NULL | SQL NULL |
| 单张拍摄合法 | `{}` | 保存合法生效参数及驱动 |
| 录像合法 | `{"target_duration_ms":正整数}` | 保存合法生效参数及驱动 |
| 延时摄影合法 | 保存任务声明所需完整字段，遵守下表 | 保存合法生效参数及驱动 |
| 取回合法，默认／预览／精确 ID | `{"selection_mode":1}`／2／3 | SQL NULL |
| 清理、取消、报告合法 | `{}` | SQL NULL |

执行定义不重复 action_type、driver_id 或完整生效参数。录像目标为 1 至 9223372036854775807 毫秒；有效小数秒精确换算，不足一毫秒、零、负数或越界不修正。用户违反有效参数规则是输入错误；合法输入无法形成合法任务依据是定义／转换错误。读取非法已保存定义是状态库错误。

延时摄影的固定任务依据必须来自驱动任务定义，不从动作名称或用户恰好提交的某个字段猜测：

| 固定依据及组合 | 合法保存规则 |
| --- | --- |
| 时长及等待适用性 | 必填 duration_based 和 wait_after_send 两个 JSON bool；条件字段的必填和省略按首次任务事实校验 |
| 按持续时间采集 | 必须保存正整数 target_duration_ms |
| 正常结束责任 | end_control 为 1（DEVICE）或 2（HOST_TIMER），必填 |
| 停止能力 | stop_supported 为 JSON bool，必填；HOST_TIMER 必须为 true |
| 启动成功返回含义 | start_return_meaning 为 1（SENT）、2（STARTED）、3（COMPLETED），必填；主机计时任务必须有及时安排停止的计时依据 |
| 完成依据 | completion_mode 为 1（DEVICE_EVIDENCE）或 2（TIME_AND_OUTPUTS），必填；HOST_TIMER 与 TIME_AND_OUTPUTS 的组合非法 |
| 发送成功后等待目标时长的任务 | 必须保存 result_wait_margin_ms，范围 0 至 9223372036854775807；明确无需余量时保存 0 |
| 不采用该等待方式 | 省略 result_wait_margin_ms；不保存 0 或 null 冒充不适用 |
| 来源定义缺失、类型非法或能力／等待组合矛盾 | 定义错误；不补缺省、不自动换结束方式或完成方式 |

本计划只形成固定定义及读取校验，不实现设备运行状态机。驱动任务事实不能包含本次单调钟截止值、实际完成结果或新加载的运行配置。旧定义在重送、配置／默认值变化、重启及历史回放后保持。

### M8 来源范围与固定状态

| 动作及来源形式 | 解析时点与结果 |
| --- | --- |
| 取回或范围清理，action_name | 受理时解析唯一拍摄来源；缺失或类型不适用使本动作失败 |
| 取回，只有 group | 受理时解析本计划组并按产物类型筛选；失败拍摄仍选入，非产物动作排除；缺组或无产物成员使取回失败 |
| 取回或范围清理，current_plan:true | 受理时固定当前计划及完整拍摄集合；合法空集合仍保存 FIXED 和真实计划 ID |
| action_instance_id，或含 plan_instance_id 的动作／组／计划来源 | 受理时只校验结构和 ID；合法动作保存 PENDING，执行时才查询及固定 |
| 精确 ID 清理 | 不建立范围来源成员；执行时核实实际目标 |
| 动作因任一自身条件受理失败 | 不建立有效执行来源集合，来源状态与计划列为空；可靠自动关联证据按 M9 另行保存 |

FIXED 与全部成员共同提交；不能以成员数量推断 PENDING、FIXED 或 FAILED。范围清理不支持组引用；取回的六种来源和清理的合法子集均须覆盖。来源查找失败不能保存为空集合或业务缺失。后续执行不按同名对象重选成员。

### M9 自动预览

这些条件互相独立；单动作可同时拥有多个已确定问题。整份拒绝仍由 M3 优先处理。

| 条件维度 | 结果与证据 |
| --- | --- |
| 用途或来源无法可靠解释 | 保存自身错误，不猜测自动关联或冲突成员 |
| 已可靠识别自动用途，但来源名不存在／不是拍摄 | 取回失败；不存在的来源 ID 为空，不创建虚假拍摄关系 |
| 来源可靠且时间不同 | 取回失败，保留两个实际时间；不改写取回时间 |
| 来源参数类型可靠且明确支持／不支持 | 保存类型及 preview_support=1／0；不支持仅使对应取回失败 |
| 来源参数类型因用户输入错误不能可靠确定 | 自动取回因无法形成必需的有效关联而受理失败，保留自身原因与可靠部分，未知能力为 SQL NULL；拍摄仅有其他参数错误且预览依据可靠时，不能从拍摄失败本身推导取回必定失败 |
| 已存在的定义无法可靠读取或解释 | 配置／内部错误，不当作明确不支持 |
| 每个可靠拍摄来源的自动声明数为 0／1／至少 2 | 无关联／继续其他校验／全部冲突取回失败；其他参数错误不把可靠声明移出冲突集合 |
| 自动取回通过全部校验 | 来源 ID、来源参数类型、支持值完整，is_valid=1 |
| 自动取回失败但存在可靠关联依据 | 保留可靠字段，is_valid=0；缺失事实不补造 |

冲突错误使用 duplicate_auto_preview，每个冲突动作的详情都包含同一来源及完整冲突名称集合；其他已知问题通过可选 issues 数组保存，结构复用 action_validation_failed 的问题列表。普通手动预览不参与。关联行、动作初始结果及完整计划共同提交。

### M10 文件步骤与公共诊断

| 实际步骤 | 公共 stage 与要求 |
| --- | --- |
| 打开未完成 | input_read；详情标明 operation=open 和实际原因 |
| 文件已经打开，完整读取中途失败 | input_read；详情标明 operation=read；丢弃部分内容，不重读 |
| 完整读取后 UTF-8 解码失败 | input_parse；保留编码事实和可编码详情 |
| JSON 语法、重复键或 Unicode 标量检查失败 | input_parse；保留实际解析原因，不提取身份或 ACK |

同次输入最多一条文件诊断，包含全部相关公共结构／ACK 问题。动作自身错误保存在动作下面。机器 stage 和已有明确 code 精确断言；一般文案只检查事实和结构。已定义读取错误采用 plan_file_read_failed，JSON 解析错误采用 invalid_json；编码错误等没有固定公共 code 的具体表达按相应契约保留事实，不自行宣称新代码已经登记。

## 接口与文件组织建议

`ParsedInput.document` 继续允许任意 `JsonValue`，不假定解析即得到合法计划。身份提取结果明确区分非对象、非法 ID 和合法 `ObjectId`；ACK 字段继续保留 `MISSING`。两者进入事务，不能替代事务内的权威查询。

建议为动作校验结果加入独立的有效设备、时间、组及策略字段，以及结构化 issues。通过全部校验的执行定义与失败结果采用不同状态表达，避免 `ok=True` 却缺少必需定义，或 `ok=False` 却被准备阶段重新执行转换。`PreparedAction` 携带最终 JSON 定义、来源状态、完整关联及错误详情，仓储不再次读取原字典决定语义。

静态目录建议分别提供整体已实现动作登记、设备能力判断、按设备／动作／参数类型选取定义，以及按生效参数取得拍摄任务依据。可靠不支持可以使用显式结果或受限 None；声明存在但获取失败须抛明确规则错误。参数类型定义包括 Schema、默认值和预览依据。拍摄任务依据由驱动静态适配提供，动作模块验证并编码 M7；不得要求所有驱动在用户 params 中使用同一任务字段名。

为使任务接缝可检查，建议约定以下纯边界，并在实施选择其他名字时同步消费者：`extract_request_identity(document: JsonValue) -> RequestIdentityDecision`，结果区分非对象、请求字段非法和合法 ObjectId；`validate_new_body(raw: JsonValue, catalog: StaticActionCatalog) -> BodyDecision` 保持现有公共入口；`prepare_plan(decision: BodyDecision, allocated: PlanIdentities) -> PreparedPlan` 消费完整动作判定。BodyDecision 中的动作结果携带有效普通字段、原始例外、完整失败详情及可用执行定义，不由仓储再次推导这些语义。来源结果明确携带解析状态、来源计划及完整成员，空成员不得代替状态。

已定位的读取边界为 `contracts/public_projection.py::project_public`，它通过 `registry/report-dependencies.json` 读取原始例外、生效参数及错误；`reporting/encoding.py::build_report_payload` 校验最终公共结构；`history/replay.py` 和 `persistence/repositories/history.py` 还原及提供指定历史边界的行事实。当前没有接入受理路径的专属执行定义读取器或独立原计划重建入口，不能把不存在的消费者写成已经验证的能力。T3 必须检查既有公共投影并用独立重建期望证明数据库事实完整；T8 为所属定义提供读取校验并通过实际类型边界消费，不借此实现新的设备运行流程。

| 预计文件 | 责任与消费者 |
| --- | --- |
| acceptance/input.py、cli.py | 一次完整读取与带步骤的失败；向统一受理入口提供完整结果 |
| acceptance/schema.py、rules.py | 本地 Schema 片段、公共语义及结构化动作判定 |
| acceptance/ports.py、devices/catalog.py、bootstrap/lifecycle.py、application.py | 同源定义、支持范围及目录装配；describe 与受理共同消费 |
| acceptance/links.py | 完整本计划索引、独立来源状态与自动预览依据 |
| capture/models.py 或相邻定义适配；outputs/sources.py 或相邻定义适配 | 所属动作的固定定义构造、编码与读取校验 |
| persistence/repositories/acceptance.py | 消费最终结果，形成历史／投影并完整提交，验证结构化错误和关联不变量 |
| contracts/public_projection.py、reporting/encoding.py、history/replay.py、persistence/repositories/history.py | 检查实际字段消费者，保持原值、原错误及历史行事实；所属动作定义适配负责 M7 的读取校验 |

新增类或文件必须有明确消费者；可以调整上述划分，但同步所有调用点和测试替身，不借此进行无关重构。

## 执行任务和门禁

建议按 T1 → T2/T3 → T4 → T5 → T6 → T7 → T8 → T9 执行。T2 和 T3 是同一交付任务的两个阶段：放行更多单动作失败后，原准备层无法安全消费缺省或非法公共字段，因此它们必须共同通过门禁，不单独提交或宣告 T2 完成。T2 的错误结构给后续任务使用；T3 的字段分工是来源和预览的前提；T4、T5 的同源参数及任务依据是预览与执行定义的前提。阶段结果不能被宣称为整模块完成。

执行命令在仓库根目录运行，使用现有非项目虚拟环境：

```bash
PYTHONPATH="$PWD/apps/camctl/src" uv run --no-project --python "$HOME/.venv/bin/python" python -m pytest <对应测试路径> -q
```

单元测试与集成测试分别执行；多个会同步包资源的集成测试会话顺序执行。每次任务先确认失败来自目标契约，再实现及回归。提交拆分是建议，未获提交指令时保留工作区改动与验证记录。

### T1 请求字段和独立 ACK

预计文件：仓储 acceptance.py、contracts 值／存在性消费者；测试为新增 `tests/unit/acceptance/test_identity.py` 与 `tests/integration/acceptance/test_acceptance.py`。纯字段提取输出合法 ObjectId 或结构化拒绝；事务内请求／报告查询仍由仓储负责。

- [x] 添加 `test_identity_rejected_before_lookup`：按 M1 非法集合验证无身份查询、无复用、原值保留。添加 `test_plan_ack_matrix`：M2 全组合各自断言计划数量、诊断内容及水位；增加缺 ID 配合法 ACK、合法新正文配非法 ACK、null ACK 三个反例。
- [x] 分别运行两个测试文件，记录目标分支的失败。报告及旧计划准备须可靠成功，不能把准备错误当失败。
- [x] 用既有身份和存在性适配消除裸 int 转换；限制捕获为用户字段错误，查询异常保留状态库语义。构造不含独立 ACK 的正文视图供校验，其他原键和值保持，不修改完整输入；原请求复用不构造或校验新正文。计划拒绝与 ACK 错误集中保存；未取得根对象时内部结果明确表示 ACK 未处理。
- [x] 重跑并覆盖合法最大 ID、先拒绝后受理、并发同 ID、确认回滚及提交未知；未知后用原操作核实，不分配第二组实例。
- [x] 将解析失败等旧结果断言同步为“ACK 未处理”，继续精确断言水位未变、无身份和无片段吸收；不是删除原有失败覆盖。
- [x] 审计 request_id、last_report_id、拒绝／复用诊断构造的全部转换点，记录 diff 和证据；建议独立提交此完整事务修复。

### T2 分层校验和结构化动作错误

预计文件：schema.py、rules.py、仓储错误适配与 admission 守卫；测试为 `tests/unit/acceptance/test_validation.py`、`test_links.py` 及集成 acceptance.py。输出包含原输入位置、全部已确定 issues 和有效错误码／详情的 ActionValidation。

- [x] 添加 `test_action_fields_do_not_reject_plan` 覆盖 M3 的全部类型和字段分区；添加名称首尾 Unicode 空白的整份／动作分支。把取回公共 group 的既有断言改成完整计划受理、仅该取回 failed、不生成有效组关系，保持覆盖。
- [x] 添加 `test_admission_details_match_registry`，用独立期望精确断言 issues 的 field、reason 和原 value；缺省不含 value。按公共详情 Schema 验证一般失败及专用来源错误。单元隔离资源读取，真实 Schema／登记组合放集成。
- [x] 先运行确认失败，再切换本地 plan_structure/action 片段；必要 Schema 版本与引用继续走共享边界，不修改公共协议来容忍当前生产错误。
- [x] 实现实际名称和时间语义；全部动作结构检查先完成。错误生产者返回结构化详情，仓储不解析文案，未登记码不默认为普通用户错误。多个独立错误都保留，错误集合不依赖数组顺序。
- [x] 先通过纯规则与错误结构的单元门禁，再完成 T3 的消费边界后共同重跑集成，审计每种动作的公共字段及所有 admission 错误生产者；保留原有 Schema 规则故障为内部错误的门禁。

### T3 有效字段、原始例外与原计划重建

预计文件：rules.py、links.py、仓储 acceptance.py，以及 contracts/public_projection.py 和 history/replay.py 中受字段分工影响的读取适配。测试为新增 `tests/unit/acceptance/test_fields.py` 与 `tests/integration/acceptance/test_fields.py`。输入使用 T2 的判定，输出遵守 M4 的各有效列和例外 JSON。

- [x] 添加 `test_invalid_date_keeps_failed_action`，拍摄、取回、清理及显式定时取消／报告均保存完整计划，非法时间只留原输入与错误；增加 `test_optional_schedule_and_group_preserved` 和 `test_valid_policy_survives_other_failure`。
- [x] 添加 `test_original_action_round_trip`：M4 每个分区按原下标重建，缺省与 null 分开，精确数值不变；合法普通字段不在 input_fields_json 重复。检查历史恢复同一值。
- [x] 先运行失败，再使校验阶段产出有效字段；准备层仅消费结果，不对失败原值调用 to_utc_micros 或构造有效组。
- [x] 保存例外 JSON 并同步消费者按字段权威来源重建。存在两个原始表示或必需依据缺失时按状态库错误处理，不挑选其中一份。
- [x] 重跑矩阵及相关历史／报告消费者测试，审计设备、时间、组、max_delay_ms 的全部持久化与恢复分支，确认失败动作不取得执行资格。

### T4 同源静态目录和支持范围

预计文件：ports.py、devices/catalog.py、bootstrap/lifecycle.py、application.py、cli.py、rules.py；测试为 unit/devices/test_catalog.py、integration/devices/test_catalog.py 与 integration/bootstrap/test_composition.py。输出能表达 M5，参数定义同时给受理和 describe 使用。

- [x] 添加 `test_acceptance_and_describe_share_definitions`，同一注入定义给两个生产入口，验证 params.type、额外调整项、默认值及能力依据一致；默认装配引用未部署驱动必须失败。添加两台设备能力不同及无关设备加入后的分区测试。
- [x] 运行目标失败后，区分整体实现登记、设备能力和参数类型选择；None 不再兼任定义故障。整体支持来自一个实施登记来源，不能把占位处理器认作实现。
- [x] 默认 run/submit 使用与 describe 相同的定义提供者；移除开放占位目录的生产入口。定义为空时明确处理未部署驱动，不增加实际相机接入。
- [x] 同步所有静态目录测试替身，并给声明存在但 Schema 损坏／读取失败的替身提供明确错误；本计划的受理集成可注入完整静态定义，不访问设备。
- [x] 重跑目录、受理与默认 CLI 装配测试；审计两种命令模式及 describe 的规则来源，记录尚未部署的实际设备能力。

### T5 原参数、默认值和拍摄任务依据

预计文件：rules.py、ports.py、devices/catalog.py，以及所属驱动静态适配；测试为 unit/acceptance/test_validation.py、unit/devices/test_catalog.py 与对应集成。输出原参数校验结果、完整生效参数及可靠固定任务依据，供 T7/T8 使用。

- [x] 添加 `test_required_field_not_filled_by_default`，包括条件 required；添加 `test_invalid_defaults_are_rule_error`、显式 null／false／0／空串及组合默认值案例。合法原输入被默认值破坏时，断言定义错误且不保存用户失败动作。
- [x] 运行失败后，先完整校验原输入，再由同源默认值边界生成新对象，随后复验生效值；原输入保持不变，不根据 default 注解隐式改输入。
- [x] 目录通过生效参数与驱动任务契约提供 T8 需要的固定事实，明确单位和适用性。任务定义错误用内部错误表达；不把框架执行字段塞回用户参数。
- [x] 重跑 M6 及参数类型分支测试，审计 apply_defaults 和 validate_capture_params 的同类处理；通过真实静态目录集成证明导出、校验和默认值一致。

### T6 本计划来源与持久化状态

预计文件：links.py、仓储 acceptance.py 及 source_members 守卫；可复用 outputs/sources.py 的成熟业务解析类型，但本计划查询适配只读取完整输入。测试为 unit/acceptance/test_links.py 与新增 integration/acceptance/test_links.py。

- [x] 添加 `test_local_source_forms_fixed_at_acceptance` 覆盖 M8：动作名、组、当前计划、合法空计划、失败拍摄、非产物成员及不存在目标；取回和范围清理均覆盖。添加合法跨计划 ID 不查询目标的替身断言。
- [x] 拆开现有“仅 group 是跨计划来源”的错误测试前提：跨计划组填写 plan_instance_id，本计划合法组准备真实拍摄成员，不存在的本计划组保留为单动作失败用例；不删除其来源覆盖。
- [x] 先运行失败，再构造显式来源状态、真实来源计划和完整成员。不能用 tuple 非空判断是否固定；局部引用解析错误使用 T2 的已登记详情。
- [x] 保存全部成员及对应事件，source_members 守卫核对所属取回／范围清理、来源计划和成员类型；精确 ID 清理不建立范围成员。
- [x] 重跑并验证后置引用、动作数组交换、其他字段失败时不生成有效来源集合、回滚无部分关系、重送／历史恢复不重选集合；审计六种取回来源及清理合法子集。

### T7 自动预览条件与可靠关联

预计文件：links.py、目录参数类型依据、仓储关联和错误适配；测试为 unit/acceptance/test_links.py 与 integration/acceptance/test_links.py。消费 T3 的时间、T4/T5 的能力依据和 T6 的完整索引，输出关联可靠部分和完整失败详情。

- [x] 按 M9 添加 `test_preview_support_and_time_matrix`、`test_missing_auto_source_keeps_failed_action`、`test_valid_auto_link_has_complete_evidence`；覆盖支持、不支持、用户错误导致未知、定义故障、时间一致／不一致及来源类型不适用。
- [x] 修正 helpers.py 的合法自动预览构造，使其 scheduled_at 与来源相同，并提供明确的同源预览支持；原有时间不一致保留为独立失败分区，不能因为原测试期望通过而改变生产规则。
- [x] 添加 `test_all_conflicts_preserve_complete_details`，每个来源 0／1／至少 2 个，包含某个冲突另有参数错误、不同来源和手动取回；每个冲突详情精确包含完整名称集合且符合公共登记。
- [x] 运行失败后，能力及参数类型从来源拍摄取得；只对可靠来源建立真实 ID，来源缺失可保存空值。合法关联保存 is_valid=1；失败关联只保留实际可靠依据。
- [x] 重跑全部组合并审计自动声明集合、冲突详情、关联有效性及取消联动读取所需依据。验证无 delivery／设备尝试，事务回滚及重送保持完整关联或完整失败。

### T8 所属动作的执行定义

预计文件：capture 的定义适配、outputs 的选择方式适配、links.py、仓储 acceptance.py，以及实际执行定义读取入口。测试为 unit/capture/test_definitions.py、unit/acceptance/test_definitions.py 和新增 integration/acceptance/test_definitions.py。输入为合法生效参数和 T5 的任务事实，输出 M7 的精确 JSON 对象。

- [x] 添加 `test_specs_match_action_contracts`，所有动作合法对象／合法空对象／受理失败 SQL NULL 分区完整覆盖；取回包括省略筛选与显式 default、手动／自动 preview 及精确 ID，保留原输入差别。
- [x] 添加录像目标精度和范围反例；延时摄影覆盖 M7 全部字段类型、结束控制与停止能力／完成方式组合、三种返回含义及等待余量适用性。合法输入无法派生定义时断言内部错误，不注册局部定义。
- [x] 运行失败后，由对应动作模块构造和校验完整定义；现有 CaptureDefinition 的字段不足以表达正式延时契约时应调整所属定义，不直接序列化它的动作类型包装。仓储保存结果，不复制完整参数。
- [x] 实现同源持久化读取校验；字段缺省、非法类型、SQL NULL／JSON null 混用和错误动作结构按状态库错误，不从新配置重算。
- [x] 重跑并组合受理→历史→读取，验证重送、重启、默认值变化及后续终态保持首次对象；审计各动作定义生产者和消费者。此门禁不宣称设备执行状态机已通过。

### T9 输入失败步骤与诊断闭合

预计文件：input.py、cli.py 读取适配器、仓储诊断适配、contracts/public_projection.py 与 reporting/encoding.py；测试为 unit/acceptance/test_input.py、integration/acceptance/test_input.py。读取端口输出完整 bytes 或带实际步骤的失败，不返回部分 bytes。

- [x] 添加 `test_read_failure_is_not_open_failure`，接口约束替身分别模拟打开失败、已打开后读取失败；保留一次尝试及无片段身份／ACK。真实适配器的阶段组合放集成测试。
- [x] 添加 `test_input_diagnostic_matches_public_schema`，M10 各分支经过真实诊断事务及实际投影片段，stage、操作事实和已有明确 code 精确正确，详情可正常编码。
- [x] 先运行失败，再由文件适配器携带步骤，读取组织层只传递真实分类；映射公共 input_read／input_parse。修正“中途读取失败是 OPEN”的既有错误断言，保留该测试的失败覆盖。
- [x] 重跑并覆盖重复键、非法 Unicode、解码、语法、缺文件及截断；原输入路径／文件名保持调用方事实，诊断不补造 request_id，ACK 不吸收。
- [x] 审计读取、解析和持久化诊断的全部出口；按后续报告实际可用程度记录组合证据，不能用手工拼装完整报告冒充真实维护闭环。

## 横切核验与最终验收

各任务完成后，沿权威输入到可观察结果重新检查：完整文件 → 无歧义 JSON → 合法身份 → 原请求复用或首次校验 → 有效字段／原始例外 → 同源参数及任务依据 → 固定定义／关联／错误 → 同一事务的历史和投影 → 旧请求、重启及历史读取。

- [x] 分别运行 acceptance 单元、contracts 单元和涉及的 devices／capture／outputs 定义单元；单元启动及执行不读取真实包资源。真实协议和登记一致性由对应集成门禁证明。
- [x] 顺序运行 acceptance、相关 contracts／devices／history／reporting 和 bootstrap 集成；保存退出码、数量和原始失败原因，不把已知其他模块缺陷当成本计划修复。
- [x] 两个真实 CLI 入口验证 M1/M2/M3 的代表组合，检查数据库中的实际计划、全部动作、ACK 和诊断；重送正文缺省／变化时不产生新实例，删除输入文件后仍能从状态库重建原内容。
- [x] 原受理事务确认回滚时，没有计划／成员／ACK 的部分变化；未知结果不对外宣称已受理或未受理，核实后只消费一次完整结果。新计划序列只由成功完整注册推进。
- [x] 检查没有设备、文件交付、清理或取消执行副作用；记录尚未接入的真实驱动、处理器及报告维护。不能把 CLI 返回 succeeded 当成旧动作已经推进或报告已经生成的证据。
- [x] 独立对照实际 diff 和验证结果审计全部同类入口，按归因区分修复回归、未闭合缺陷及其他模块原有问题；仅有测试全绿仍不能替代上述契约检查。

## 评审验证记录

2026-10-01 在非项目 Python 3.12 环境中分别执行现有 acceptance 单元测试和集成测试，结果为 **45 项通过**、**22 项通过**。这证明现有用例能运行，不能证明受理契约完整。

另外使用临时 SQLite、真实受理仓储和局部纯函数运行 **50 个输入场景的诊断探针**，在 Python 3.11 与 3.12 分别保存实际结果，两者的观察结果一致。探针是问题证据采集，不是“50 项通过”的验收测试。它没有修改生产代码；上述 F1—F11 分别列出了可重新构造的输入及观察结果。

诊断脚本和完整结果保存在 git 忽略的 `.superpowers/reviews/2026-10-01-acceptance/`。复现命令为：

```bash
PYTHONPATH="$PWD/apps/camctl/src" uv run --no-project --python "$HOME/.venv/bin/python" python .superpowers/reviews/2026-10-01-acceptance/probe.py
```

已有测试的具体缺口：`test_retry_skips_body_validation` 的正文实际仍完整，没有构造注释声称的缺 actions 重送；`test_obtain_with_group_field_is_whole_rejection` 固定了错误范围；`test_partial_read_has_no_identity` 固定了错误步骤。所谓旧工作闭环用例只检查 CLI 返回，没有断言旧动作实际推进、诊断进入真实报告或有效 ACK 结束相应责任。acceptance 单元规则测试仍读取真实包资源，应按上述测试分类分开验证。

ACK 同步结束、submit 事务内交接、取消后接手等尚未进入独立评审。相关模块其他已知失败以及真实设备部署验证均不因这份评审而成为已完成事项。

## 修复验收记录

2026-10-02，F1—F11 对应的 T1—T9 已实施。自动预览冲突详情采用可选 `issues`，延时定义采用必填 `duration_based` 和 `wait_after_send`；正式规格、权威错误登记、生产构造和读取使用相同规则。

| 验证范围 | 实际结果 |
| --- | --- |
| Python 3.11 与 3.12 的相关单元测试：acceptance、contracts、devices、capture、outputs、bootstrap | 两个版本各 605 项通过 |
| Python 3.11 的相关集成测试：acceptance、contracts、devices、bootstrap、history、reporting | 339 项通过 |
| Python 3.12 的 camctl 全量集成测试 | 511 项通过，9 项失败；失败均为此前记录的 `outputs/test_qualification.py` 问题 |
| 两个真实 CLI 入口的受理及身份／ACK／正文组合 | 15 项通过，断言实际计划、全部动作、ACK、诊断及原动作重建 |
| 根协议与报告字段依赖检查 | 协议检查通过；报告字段依赖的 29 项测试通过 |
| 独立复核中的冷启动单元检查 | 禁止真实包资源读取后，acceptance、contracts、capture、devices、bootstrap 及 `outputs/test_definitions.py` 共 560 项通过，实际资源读取为零 |

真实历史组合消费 `read_events` 与 `entity_event_links`，验证正向／逆向恢复及关闭重开数据库后的重送；默认值变化不改写首次定义或精确原输入。原事务回滚保留原计划与 ACK，提交未知后按原 `operation_key` 核实，不建立第二组实例。已受理动作缺少公共结构必需依据、专属定义无效或保存事实矛盾时，三个读取入口都报告内部状态错误；首次受理失败继续保留实际缺省及非法值。

实际驱动尚未部署，默认目录明确拒绝引用未部署驱动的配置。上述结果不证明设备动作已经执行、报告已经交付或 ACK 同步责任已经结束。全库仍有上述九项产物资格失败，取回执行结果投影中的 `failure_union` 尚未接入，既有日志关闭问题也保留在所属模块的记录中。独立只读复核确认本计划没有阻断问题；这些其他模块边界不计入本计划的完成声明。

独立复核提出的终态测试基线缺口也已核验：先确认完整快照可以读取及投影，再删除目标原输入依据。Python 3.12 最终字段集成全文件 71 项通过；非拍摄的终态证据使用执行前已取消的合法快照，不借此进入尚未接入的执行结果投影。

完整执行日志与先失败后实现的证据保存在 `.superpowers/sdd/2026-10-01-camctl-acceptance-boundaries-review/`。
