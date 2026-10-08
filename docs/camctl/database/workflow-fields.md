# 来源、取回、清理与取消字段

[来源与取回职责](sources-obtaining.md) · [清理与取消职责](cleanup-cancellation.md) · [结构定义](schema/workflows.sql)

本页定义关系及明细表的字段语义。取回与清理的状态及类型编号见[内部枚举登记](enum-registry.json)；各类明细的错误编号见[公共错误登记](../../../protocol/errors/workflow-codes.json)的 `item_error_ids`，不从正文排列顺序推导编号。结构同步状态见[结构与检查边界](schema/README.md#结构同步状态)；每条关系都有自己的正整数 `id`，记录永久保留。所属动作的来源解析和目标固定状态见[计划与动作](plans-actions.md#来源解析与目标集合)。

整数编号由[统一定义](enum-registry.json)提供，[编号一览](enum-values.md)从该定义生成。本页使用成员名称说明字段含义和处理规则。

## 自动预览和来源成员

`auto_preview_links.obtain_action_id` 唯一关联一个自动预览取回动作。只有可靠确认是自动用途才创建关联行；未能确定来源时 `source_action_id` 为空。`parameter_type` 保存可靠取得的来源参数类型，否则为空；`preview_support` 为 1 支持、0 明确不支持、SQL `NULL` 尚无法确定。能力未声明或无法加载不等于明确不支持，按对应配置或驱动错误处理。

`is_valid` 为 1 表示该自动关联通过全部受理校验，此时来源、参数类型和支持值必须完整；0 表示保留失败时的可靠关联依据。部分信息未知不能补齐成有效关联。部分唯一索引只限制有效关联，同一来源存在多个冲突的失败关联是合法状态，不能通过唯一约束丢掉其中一项。关联、原输入、冲突错误及来源能力证据在同一受理事务内保存，创建后只读。

`action_dependencies` 保存稳定的 `id`、`action_id`、`depends_on_action_id`。身份与成员唯一性遵守[动作依赖关系](waiting.md#动作依赖关系)，成员固定后保持。来源计划由所属动作的 `resolved_source_plan_id` 保存，成员必须属于该计划且为拍摄动作。执行时结合来源自身的完成事实与依赖方选择进度，按[来源处理规则](waiting.md#来源等待的记录与转换)判断。反向查询按被依赖动作定位关系，再结合所属动作资格及选择进度发现未完成责任。

## 每个来源的文件选择

`obtain_source_selections.dependency_id` 唯一引用一个固定来源关系，由关系取得取回动作与来源动作。`status` 为 `PENDING` 或 `FIXED`。`PENDING` 不允许有最终来源错误；`FIXED` 与全部选择项共同提交，可以合法地没有任何选中文件。

`error_code` 与 `error_details_json` 同时为空或同时有值。来源级错误为 `NO_OUTPUTS`（`no_outputs`）和 `PREVIEW_MISSING`（`preview_missing`），阶段均为 `output_selection`。前者表示该来源没有正式产物，后者表示预览方式无法取得任何对应原文件的预览目标。详情采用公共登记，已知来源 ID 必填到公共失败项；错误本身不创建虚构的产物或交付。

一份来源有多份原文件，只有其中几份缺少预览时，逐份保存下面定义的预览目标失败，其他可选文件仍照常保存。不得将多个缺失预览压成无法对应原文件的一条来源错误。

## 取回目标字段

`obtain_items.selection_id` 指向所属来源选择。`requested_output_id` 只在显式 ID 模式填写，它是原请求数值，不是外键；产物不存在或不属于来源时仍保留。`output_id` 只有确认实际选中且属于该来源时才填写。两者分别唯一约束到同一来源选择；显式合法目标要求二者相等。

| `basis` | 选择依据与必填引用 | 内部选择含义 |
| --- | --- | --- |
| `ORIGINAL` | `output_id` 指向原文件；额外来源和比较列为空 | `original` |
| `REPAIRED` | `output_id` 指向修复成品，`original_output_id` 指向原文件；预览列为空 | `repaired` |
| `PREVIEW` | 成功选择时 `output_id = preview_output_id`，并保存原文件；没有预览时保留原文件和逐项目标失败，选中产物为空 | `preview`；失败目标不进入选择数组 |
| `REPAIRED_NOT_LARGER` | 选中修复成品，并保存原文件、预览、两个比较大小 | `repaired_not_larger` |
| `EXPLICIT` | 保留请求 ID；尚未核实或无效时实际产物为空，其余来源、预览和比较列为空 | `explicit` |

`preview_size` 与 `repaired_size` 同时有值或同时为空。有值时表示选择时实际比较的完整长度；依据 `REPAIRED_NOT_LARGER` 必填且 `repaired_size <= preview_size`。依据 `PREVIEW` 在确实比较后选择预览时可以保存两项，此时修复成品更大；没有可比较修复成品时两项为空。后续文件不可用不改变原选择依据。

以下组合在完整已提交边界成立。“错误为空”表示 `error_code` 与 `error_details_json` 都为 SQL `NULL`；“错误必填”表示两列同时有值且详情符合该错误的登记。所有状态同时满足上表的选择依据约束。

| `status` | `output_id` | `delivery_id` | `source_dependency` | 本项错误 |
| --- | --- | --- | --- | --- |
| `UNRESOLVED` | 空；只适用于尚待判定的显式请求 | 空 | 0 | 空 |
| `SELECTED` | 必填，已可靠选中 | 空 | 0 | 空 |
| `DELIVERY_CREATED` | 必填，与交付的产物相同 | 必填，永久保留 | 1 表示仍受保护；0 表示已解除依赖 | 空；交付的失败由交付保存 |
| `FAILED` | 按失败前是否可靠选中保留 | 空 | 0 | 必填 |
| `CANCELED` | 按取消前是否可靠选中保留 | 空 | 0 | 空 |

取回项从 `UNRESOLVED` 完成判定后进入 `SELECTED` 或适用最终结果；`SELECTED` 通过共同建档进入 `DELIVERY_CREATED`，或在建档前结束为 `FAILED`、`CANCELED`。选择与结果可以在同一事务内确定，提交边界只保留完整结果。`FAILED`、`CANCELED` 不重新打开；`DELIVERY_CREATED` 保持该状态，其后的依赖解除不清除交付关联。状态、错误与选择依据不得相互矛盾，例如目标不存在或来源不匹配的失败不关联该请求所指的产物。

`source_dependency` 为 0 或 1，初始为 0；历史读取资格由永久 `delivery_id` 关联表达。资格授予、源依赖置 1、交付、拷贝、目标文件和读取流程共同建立。副本可靠准备完成或确认实际读取及后续重试停止后，源依赖置 0，之后不重新开启。全部合法组合、取消与恢复见[取回的读取资格与依赖](sources-obtaining.md#取回的读取资格与依赖)。

本项错误由公共登记中含 `item_error_ids.obtain_items` 的条目定义，内部名称使用公共码的大写形式。阶段与详情从同一条目取得；不存在或来源不匹配的请求不能建立产物关联。来源查询失败属于状态库错误，不登记为 `OUTPUT_NOT_FOUND`。

## 清理目标字段

明细 ID 按首次固定目标的顺序分配。精确 ID 清理保持原请求顺序，来源范围清理保持已保存的选择顺序；报告与分批恢复按该 ID 顺序读取，不按完成时间重排。集合生成规则见[字段依赖登记](report-dependencies.md#集合与逐项结果)。

`cleanup_items.action_id` 引用清理动作，`requested_output_id` 保存完整固定目标中的数值；范围清理同样填写实际选中的 ID。`output_id` 仅在确认真实产物后填写并与请求相等。删除和查询流程通过 `operation_runs.cleanup_item_id` 单向关联本项，按需创建，责任键与唯一性遵守[流程关联与按需创建](cleanup-coordination.md#流程关联与按需创建)。没有真实产物时不能建立调用流程。

清理项状态为 `UNRESOLVED`、`PENDING_DELETE`、`DELETING`、`SUCCEEDED`、`FAILED`、`CANCELED`，整数编号由统一枚举登记定义。`PENDING_DELETE` 表示已建立本项删除限制、尚未取得处理归属；当前源依赖在执行时检查。待处理阶段及其共同提交规则见[清理读取等待](cleanup-coordination.md#清理读取等待与待删除状态)；处理归属、终态与取消组合见[处理状态与归属](cleanup-coordination.md#处理状态与归属)。待删除、处理中及成功项要求真实产物；此前已完整清理可直接成功，不补造调用。失败项必填错误；取消项按实际结束原因填写错误，规则见[取消后删除结果](output-cleanup-state.md#取消后删除结果与错误的保存)。首次终态的 `final_event_id` 由[最终结果事件](output-cleanup-state.md#清理项的最终结果事件)定义。

`restriction_state` 为 `NOT_ESTABLISHED`、`ACTIVE`、`RELEASED` 或 `IRREVERSIBLE`。`NOT_ESTABLISHED` 表示尚未取得删除资格；`ACTIVE` 表示已阻止新的读取但允许已有读取结束；`RELEASED` 只用于可靠确认本请求没有发出删除时取消并解除限制；`IRREVERSIBLE` 表示已发出或不能排除发出删除，或者已可靠完成删除，本请求不再具备撤销限制的资格。后续查询确认仍存在也不因取消自动允许新取回，继续按正式清理规则处理。

本项错误由公共登记中含 `item_error_ids.cleanup_items` 的条目定义，内部名称使用公共码的大写形式，阶段和详情沿用同一条目。预算错误详情保存实际采用上限、已用次数和目标产物，原始错误在尝试中保留。批量结束根据所有清理项汇总，不能因某个文件失败清空整个集合。

### 清理项的字段组合

清理项 C 已建立限制，但设备绑定失效而未能开始删除时，可以保存 `FAILED` 和 `ACTIVE`：失败结束本次工作，限制仍在。C 若在未发出删除的阶段取消并解除限制，则保存 `CANCELED` 和 `RELEASED`。两种结果都不再执行，但对后续取回的影响不同，因此不能从终态单独推导限制。

下表限定完整已提交边界的组合。错误两列的成对规则与取回项相同；取值之外的组合均非法。允许的组合还须具备相应实际依据，不能仅通过空值检查就提交。

| `status` | `output_id` | `restriction_state` | 本项错误 | `outcome` | `final_event_id` |
| --- | --- | --- | --- | --- | --- |
| `UNRESOLVED` | 尚未确认时为空；已可靠确认但尚未建立限制时保留 | `NOT_ESTABLISHED` | 空 | 空 | 空 |
| `PENDING_DELETE` | 必填 | `ACTIVE` | 空 | 空 | 空 |
| `DELETING` | 必填 | 尚未形成不可撤销事实时为 `ACTIVE`，否则为 `IRREVERSIBLE` | 空；单次调用错误保存在尝试中 | 空 | 空 |
| `SUCCEEDED` | 必填 | `IRREVERSIBLE`，保存本项确认的清理完成事实 | 空 | 必填，按下表确定 | 必填 |
| `FAILED` | 按可靠目标事实保留；不存在的目标为空 | `NOT_ESTABLISHED`、`ACTIVE` 或 `IRREVERSIBLE`，保留失败时的事实 | 必填 | 空 | 必填 |
| `CANCELED`，本项从未建立限制 | 按已可靠确认的目标保留 | `NOT_ESTABLISHED` | 空 | 空 | 必填 |
| `CANCELED`，本项限制已完整解除 | 必填 | `RELEASED` | 空 | 空 | 必填 |
| `CANCELED`，本项留下不可撤销限制 | 必填 | `IRREVERSIBLE` | 必填，按取消收场结果保存 `FILE_DELETE_FAILED` 或 `DELETE_UNCONFIRMED` | 空 | 必填 |

产物关联为空时，限制只能为 `NOT_ESTABLISHED`，本项不得拥有删除或存在性查询流程。目标关联一旦可靠保存，失败和取消继续保留该关联。`RELEASED` 仅属于允许解除限制的取消结果；失败本身不解除限制。成功项的 `IRREVERSIBLE` 也可以来自查询确认或复用清理完成事实，不据此推断本项执行过删除。

`PENDING_DELETE` 可以进入 `DELETING`、适用失败或取消结果，也可以复用可靠完成事实直接成功。进入 `DELETING` 后只进入适用终态，查询、重试和取消收场期间保持原状态。三种终态保持状态、结果、错误及最终事件引用，不因后来请求完成清理而改写。

### 清理成功依据

`cleanup_items.outcome` 为可空整数枚举，仅 `SUCCEEDED` 必填，用于保存本清理项取得成功的依据。它与成功状态、最终结果事件和适用产物汇总共同提交，进入所属清理动作的历史及快照。程序重启或历史恢复后，直接读取本项保存的依据，不用产物当前已清理状态重新猜测本项当时怎样成功。

| `outcome` 成员 | 保存条件 | 对应公共值 |
| --- | --- | --- |
| `DELETED` | 本项删除取得可靠成功依据，并确认目标已完整清理；本项具有对应删除流程和尝试 | `deleted` |
| `ALREADY_CLEANED` | 本项使用已有的可靠产物清理完成记录确认成功；不为该确认创建调用或消耗次数 | `already_cleaned` |
| `ABSENCE_CONFIRMED` | 本项存在性查询可靠确认目标不存在，并共同保存清理完成事实；本项具有对应查询流程和尝试 | `absence_confirmed` |

查询确认不存在的结果适用于正常核实及中断恢复，不要求本项此前执行过删除。若尚有调用或必要收场责任，先按[清理协调](cleanup-coordination.md#处理状态与归属)完成责任，不能提前保存成功终态。已有终态不因后来取得其他依据而替换 `outcome`；调用结果未知、调用超时或运行假设只证明适用收场时，均不单独构成成功依据。

### 关联记录的一致性

行内约束、关联完整性和历史转换分别校验。行内字段合法不代表共同建档或外部操作条件已经满足。

| 关联范围 | 完整已提交边界的不变量 |
| --- | --- |
| 取回项与交付 | 一项至多关联一份普通交付，一份普通交付对应唯一取回项；动作、产物身份一致，且交付、拷贝、目标文件、唯一读取流程共同存在 |
| 取回项与拷贝源 | 拷贝的源文件必须是该产物关联的文件；当前源依赖按[依赖解除条件](sources-obtaining.md#交付关联与源依赖的状态组合)保存，不从交付终态或相机机会推导 |
| 清理项与调用流程 | 按 `cleanup_item_id` 关联，类型、责任键、所属动作和目标一致；每项每种流程最多一份，按需创建，已结束流程保留不表示仍持有处理归属 |
| 清理项与读取保护 | 同一产物最多一个 `DELETING` 项；该产物不得同时存在有效源依赖。`PENDING_DELETE` 可以与零个或多个源依赖并存 |
| 清理终态与实际调用 | 本项相关调用已具备适用收场依据或可靠确认不会执行，必要结果已保存；终态不得遗留仍可能执行而无人完成收场的本项调用 |
| 清理项与产物汇总 | 产物按全部相关事实计算汇总，和引起汇总变化的事实共同保存；某项的失败或取消可以与后来产物清理完成并存 |
| 终态与历史 | `final_event_id` 指向保存本项首次终态的事件，正文与快照包含完整字段；回放不重新执行操作、不使用最新文件事实改写旧结果 |

状态库查询失败、关联缺失或矛盾、相关事务提交未知分别进入既定错误或核实流程；不能通过清空关联、补建替代身份、默认无依赖或猜测成功来满足约束。SQL 的行内及唯一约束、事件应用时的跨表与转换校验，以及恢复后的完整边界校验共同落实这些规则。

## 取消目标与交付项

`cancel_items.action_id` 是取消动作，`target_action_id` 是固定的真实目标，同一对只出现一次且不能相等。`selection_basis` 为 `DIRECT`、`AUTO_PREVIEW`、`BOTH`，分别说明直接寻址、自动联动或两者同时成立；联动来源通过已保存的自动关联核对。

`status` 使用以下成员：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELED`。`cancellation_effect` 为 `NOT_APPLIED`、`APPLIED`、`NOT_REQUIRED`，初始根据目标事实确定；允许取消时与目标标记共同提交为 `APPLIED`，后续不退回 `NOT_APPLIED`。目标已终态无需动作取消时为 `NOT_REQUIRED`，但可能仍需撤回交付。

只有成功项填写 `outcome`：`CANCELED`、`ALREADY_TERMINAL`。只有失败项填写整数错误和详情，编号由公共登记的 `item_error_ids.cancel_items` 定义，阶段和详情沿用对应条目。`TASK_CANCEL_UNSUPPORTED` 必须保留 `NOT_APPLIED`，目标不被取消也不被判失败。

`cancel_delivery_items.cancel_item_id` 引用本次目标处理，`delivery_id` 必须属于该目标取回动作，同一对唯一。`status` 为 `PENDING`、`WITHDRAWN`、`NOT_RETRACTABLE`、`FAILED`，对应公共撤回状态。`FAILED` 必填错误，编号由公共登记的 `item_error_ids.cancel_delivery_items` 定义，公共阶段为 `publication`；其余错误为空。

取消动作自身被取消时，尚未结束的 `cancel_items` 变为 `CANCELED` 并保留取消效果，已结束项不变。尚未结束的交付项保留最后可靠进度；其后实际交付责任由 `deliveries` 继续承担，不为让旧报告看似完整而改写已经终态的取消请求。实际结果先于取消请求结束时，交付和本次明细共同保存结果。

## 索引、归属与事务

来源和选择通过所属外键或唯一键分页；`output_readers` 只查询仍有源依赖的取回项，`output_delete_restrictions` 查询每个产物全部有效或不可撤销限制。授予读取资格与建立删除限制都在同一串行写事务边界内检查，不能先在事务外判断再无条件写入。

`cancel_target_waiters` 和 `cancel_delivery_waiters` 定位仍待接收结果的请求；逐目标、逐交付结果可唤醒等待者，但对象事实和请求结果分别保存。任何目标终态、限制解除、读取依赖解除和交付撤回都必须与其历史及受影响投影共同提交。稳定成员身份、已固定集合和逐项终态在重启后保持不变。

## 电机发送事实

`motor_notifications` 是电机动作独有的发送记录，每个 `action_id` 至多一条。它归属于该动作的历史对象，参与事件回放和动作快照，不创建独立报告目标。完整规则见[电机动作](../../architecture/motor-control.md)；公共报告仍只呈现原位置参数、时间窗口与动作结果。

发送意图事务将动作置为 `RUNNING`、保存 `execution_started=1` 与唯一 `intent_operation_key`，并保存窗口观察。事务只在首次可靠提交后向当前流程返回发送许可；复用原操作身份只核实原输入和完整结果，不重新授予许可。取消或过期在受理后、发送意图之前生效时，没有发送记录；通道不可用产生可靠未发送结论，动作作为执行失败保存。

| 字段 | 含义与约束 |
| --- | --- |
| `id`、`action_id` | 正整数发送记录身份与唯一所属电机动作身份。 |
| `intent_at`、`intent_operation_key` | 共同为空或共同存在的发送意图时刻和原历史事务身份；首次保存后不变。没有意图仅适用于可靠未发送结论。 |
| `outcome` | `PENDING`、`NOT_SENT`、`WRITTEN`、`FAILED` 或 `UNCONFIRMED`。编号从[整数编号登记](enum-values.md)生成。 |
| `written_bytes`、`errno` | 实际写入的非负字节数和可选的正整数系统错误号；只供确定的写入结果使用。 |
| `finished_at` | 发送结论保存时刻。未决意图为空，其余状态必须存在。 |

| 发送状态 | 共同保存的动作状态 | 结果字段 |
| --- | --- | --- |
| `PENDING` | `RUNNING`，已开始。 | 具有意图；结束时刻、字节数与系统错误号为空。 |
| `WRITTEN` | `SUCCEEDED`。 | 具有意图和结束时刻，实际字节数为正数，系统错误号为空。 |
| `FAILED` | `FAILED`，错误为 `motor_notification_failed`。 | 具有意图和结束时刻，字节数必填，系统错误号与动作错误详情一致。 |
| `UNCONFIRMED` | `FAILED`，错误为 `motor_notification_unconfirmed`。 | 具有意图和结束时刻；字节数与系统错误号为空，不能从未知结果推断未发送。 |
| `NOT_SENT` | 通道不可用的 `FAILED`、`EXPIRED` 或已经可靠取消的 `CANCELED`。 | 结束时刻必填，字节数与系统错误号为空；通道设置错误号保存在动作错误详情。 |

同一意图只从 `PENDING` 进入一个最终结论，终态不再替换。发送结果、动作终态、计划汇总、不可变历史及报告变化在同一事务提交。事件守卫核对时间资格、本地许可、所属关系、完整计划成员及错误依据；SQL 负责行内状态组合和唯一约束。持有本地可靠未发送许可的取消在取消事务中共同保存 `NOT_SENT`、`cancel_requested=1` 与 `CANCELED`；`PENDING` 不得独立携带取消标记。恢复只应用保存事实，不执行通知写入；失去本地许可的既有未决意图保存 `UNCONFIRMED`。
