# 文件与交付字段定义

[职责及生命周期](outputs-files.md) · [结构与索引](schema/files.sql) · [跨表事务](transactions.md)

整数编号由[统一定义](enum-registry.json)提供，[编号一览](enum-values.md)从该定义生成。本页使用成员名称说明字段含义和处理规则。

## 设备文件

`device_files.id` 是内部文件身份。`observer_action_id` 是最初承担文件观察责任的拍摄动作，借该动作的固定 `device_id`、`driver_id` 取得原设备绑定；观察者创建后不改变。`source_action_id` 与 `ownership_evidence_json` 在归属未确认时同时为空，确认后同时填写。证据对象使用 `method`（`TASK_SCOPE` 表示任务独立范围，`BASELINE_DIFFERENCE` 表示固定基准差集，`DRIVER_TASK_ASSOCIATION` 表示驱动任务关联）和 `observation`（驱动结构化依据），差集另填 `activity_id`；不能把观察者自动当成可靠来源。

同一设备文件的拍摄来源只允许从未知变为可靠确认，确认后不清空或改指另一个动作。后来的观察与固定身份或来源矛盾时，按文件错误保留原依据并处理，不转移原文件的来源。该规则与永久保留的文件身份共同支持[按历史边界查找文件](../historical-state-query.md#按历史边界分批查找关联文件)。

`identity_key` 是框架对 `[device_id, driver_id, driver_file_identity]` 采用确定 JSON 编码得到的文本，全库唯一；最后一项为驱动提供的稳定文件身份。只有驱动保证同一路径不覆盖、不复用时才能将路径作为文件身份。`locator_json` 保存驱动声明的定位结构，与稳定身份分别使用。重复发现同一身份时复用原行，核对原绑定和归属；不因新的取回、查询或清理创建第二份身份。

`original_name`、`media_type` 分别是实际取得的原名称和 MIME 内容类型，未知时为空，不由扩展名猜测内容类型。`presence_state` 为 `UNKNOWN`、`PRESENT`、`ABSENT`；是否属于显式清理结果由清理记录判断，单凭 `ABSENT` 不能断言清理成功。

| `completion_state` | 大小与完成依据 | 含义 |
| --- | --- | --- |
| `UNKNOWN` | `size_bytes`、`completion_evidence_json` 为空 | 尚无完整文件依据 |
| `WRITING` | 完整大小仍为空；观察证据可以有值 | 驱动可靠观察到文件仍在形成 |
| `COMPLETE` | `size_bytes` 与完成依据都必填 | 文件已固定且完整大小可靠，后续不因删除清除这些事实 |
| `UNCONFIRMED` | 未确认的完整大小为空，实际失败证据及 `last_error_json` 必填 | 有限核实结束仍无法确认，不能按完整文件读取 |

完成依据使用 `basis` 和 `observation`，其中 `observation` 保留对应文件的实际结构化观察。

| `basis` | 适用条件与引用 |
| --- | --- |
| `DEVICE_GUARANTEE` | 驱动提供设备保证；不填写等待引用。普通录像执行定义没有文件完成等待要求时，沿用此规则 |
| `TIME_AND_OUTPUTS` | 等待与产物契约提供完成依据；另填 `activity_id`、`wait_completed_event_id`，引用对应任务已保存的等待完成事实 |
| `STOP_RETURN_AND_WAIT` | 普通录像的可靠 STOP 返回与实际完成等待共同提供运行假设依据；另填 `activity_id`、`result_page_event_id`、`stop_result_event_id`，引用原活动、包含该文件观察的可靠结果页和原停止结果 |

`STOP_RETURN_AND_WAIT` 的证据对象恰好包含 `basis`、`observation` 及表中的三个引用，不包含 `wait_completed_event_id`。原结果页必须属于同一动作、活动及固定设备绑定，并包含同一文件身份的完整观察和一致长度。页内保存的 `settlement.evidence.data.file_completion` 必须匹配原活动、原 STOP 和动作首次固定的 `file_completion_wait_ms`，且实际单调钟等待达到要求、`completed` 为 `true`。页内若复用此前完成的等待，`file_completion_source_page_event_id` 必须直接引用同活动中较早、真正执行该等待的可靠页；真正执行等待的页必须晚于原 STOP，原页自身不能再引用其他等待页，两个页的七项等待事实必须完全一致。最终保存的文件定位须与该文件在原结果页中的定位一致。文件完成等待的原事实格式见[历史格式](history-formats.md#停止后文件完成等待的记录)。这段等待表达主机运行假设，不生成设备已经观察到文件写完的记录。

结果页的调用错误可以与已经取得的可靠单文件事实并存。例如，第二份视频的元数据读取失败时，第一份视频的完整观察和已完成等待仍可保存；该页不因此取得完整集合保证。单文件未确认完整、长度未知或不一致、等待未完成、原事实未可靠保存及引用不匹配时，不能登记该文件为 `COMPLETE`。具有文件完成等待要求的录像首次完成文件时只能采用 `STOP_RETURN_AND_WAIT`。

文件已经完成后不得倒退成正在写入。重复观察同一长度时保留原完成依据；新提供的 `STOP_RETURN_AND_WAIT` 依据仍须核对原页、原 STOP 和固定要求。历史恢复沿原依据还原状态，不重新调用设备或等待，也不采用新的运行配置。后续身份或固定内容矛盾按文件错误处理，保留原事实。

每次新保存形成状态时，事件的 `evidence.completion_request` 恰好保存原可选输入 `locator`、`original_name`、`media_type`，未提供的输入也明确保存为 `null`。申请与已有元信息相同的值仍合法，事件行只保存实际改变的字段。原键重送逐项比较原输入，并按原完整事务后的状态核对形成状态、完整大小、完成依据和实际错误；不能从未改变的列猜测申请者省略了输入。旧事件没有 `completion_request` 时仍可读取和回放；原键缺少完整输入时不能补造重送依据。

`role` 为 `UNDETERMINED`、`ORIGINAL`、`PREVIEW`，保存驱动可靠确认的任务内用途，尚未确认时为 `UNDETERMINED`。预览的 `original_device_file_id` 与 `pairing_evidence_json` 同时有值或同时为空；后者包含 `method` 和 `observation`，其中 `method` 采用 `DRIVER_PAIRING` 对应的整数，表示驱动配对证据。用途已知但尚未可靠配对时，保留预览及空引用，不能任取一个原文件配对。原文件和未知用途文件不填写这两个字段；配对两端必须属于同一任务且原文件用途为 `ORIGINAL`。原文件与预览分别登记正式产物时，事务从这些可靠事实建立 `output_origins`；产物关系与设备观察各自保存其生命周期事实，不允许二者矛盾。该设计使设备先生成多份文件、框架稍后登记产物的过程可以恢复，不依赖一份内存文件清单。

`checksum_support` 为 `UNDETERMINED`、`SUPPORTED`、`UNSUPPORTED`。`sha256` 只有完整固定文件真正取得源摘要后才填写，否则为空。摘要查询失败保存 `last_error_json`，不能改成 `UNSUPPORTED`；驱动不支持摘要的合法降级由拷贝流程判定。`last_error_json` 为空表示没有尚待表达的文件观察错误；后续可靠观察可清除当前错误，历史不删除。

## 中间文件

每条 `intermediate_files` 记录通过 `owner_action_id` 或 `owner_delivery_id` 指定唯一业务责任人，两列恰好一列有值。`purpose` 为 `DELIVERY_COPY`、`RECORDING_INPUT`、`PROCESSING_TEMP`、`REPAIR_OUTPUT`；第一种属于交付，其余属于录像动作。`relative_path` 相对于所属部署的 `paths.staging`，保存创建时确定的工作位置，全库唯一；创建文件前先登记路径和责任。具体用途目录、路径格式和交接后的定位见下节。

两项 `owner_*` 关联保存创建该文件的业务责任，创建后保持。文件提升为正式产物、移入交接目录或完成清理时，保存相应生命周期事实，仍可由原责任查到该文件；不通过更换责任人表达位置或清理资格变化。

`size_bytes`、`sha256` 只表达完整文件的可靠结果，尚未完整时为空；部分拷贝进度由 `file_copies` 保存。完整修复输出登记为正式产物时继续使用原文件身份，不复制成另一个中间文件记录。

| `retention_state` | `cleanup_state` | 实际责任 |
| --- | --- | --- |
| `REQUIRED` | `NOT_NEEDED` | 后续读取、修复、登记或交接仍需要文件，不能自动清理 |
| `RELEASABLE` | `PENDING`、`RUNNING`、`COMPLETED`、`FAILED`、`UNKNOWN` | 处理决定已保存且实际使用已停止，可以进行所属流程的有限清理 |
| `PROMOTED` | `NOT_NEEDED` | 已登记为正式产物，后续只受正式产物清理规则管理 |
| `HANDED_OFF` | `NOT_NEEDED` | 取回副本已经交接，文件所有权按交接位置及撤回规则管理 |

清理失败或未知时 `last_error_json` 必填；完成时清除当前错误。文件不存在只有在可靠查询结果成立时才能记为完成。`RELEASABLE` 必须同时选择一个清理阶段，不能保留 `NOT_NEEDED`。已清理文件的行、大小、摘要和所有权历史永久保留。`PROMOTED` 后正式清理文件，只通过正式产物的 `availability` 及清理明细保存其存在和删除事实，不把中间文件自动清理责任重新打开。

## 中间文件的路径与用途目录

例如，部署的 `paths.staging` 为 `/srv/camctl/staging`，文件 28 的 `relative_path` 为 `recording-inputs/28.mp4`，实际工作位置就是 `/srv/camctl/staging/recording-inputs/28.mp4`。数据库不再附加一层 `staging`，也不按 CLI 工作目录或配置文件所在目录解释这个值。

使用前先按[目录绑定与变更](../../architecture/configuration.md#文件目录的绑定与变更)确认配置与状态库保存的根目录一致。有保留文件或依赖原目录的未完成责任时不允许切换；路径不匹配不能转化为文件不存在。符合条件的部署切换只更新元信息中的根目录，已经结束的旧文件记录、相对路径及历史保持。

每个用途使用 `paths.staging` 下固定的直接子目录。下表是中间文件用途与目录对应关系的定义来源，文件登记、恢复及清理采用同一规则。

| `purpose` | 子目录 | 保存的文件及责任 |
| --- | --- | --- |
| `DELIVERY_COPY` | `deliveries/` | 某份普通交付的准备副本；发布时移动此副本，保留源产物 |
| `RECORDING_INPUT` | `recording-inputs/` | 录像检查及修复共用的原片输入副本；按同一拷贝记录续传和恢复 |
| `PROCESSING_TEMP` | `processing-temp/` | 录像工具处理过程中需要登记和清理的临时文件；每个文件分别登记 |
| `REPAIR_OUTPUT` | `derived/` | 修复目标文件；从创建、写入到完整后登记为正式产物，保持同一文件身份和位置 |

`relative_path` 固定为 `<用途子目录>/<intermediate_files.id>[.<ext>]`。文件 ID 使用无前导零的正整数十进制表示；可选扩展名由文件生产方按工具或类型契约确定，只含非空 ASCII 字母或数字，整个文件名不超过 255 个 ASCII 字节。路径使用 `/` 分隔且恰好两段，不接受绝对路径、空段、`.`、`..`、反斜杠、NUL 或额外层级。目录须与 `purpose` 相符，文件名中的 ID 须与本行相同；不得直接拼入设备路径、用户名称或工具返回的任意路径。无法形成合格名称时，拒绝登记并保留具体错误，不先创建一个无法恢复定位的文件。

用途、责任、路径与文件 ID 在创建后保持不变；重试及重拷复用原记录和位置，只有独立的新文件才分配新 ID。修复目标可以在 `derived` 中逐步形成，完整性、后续用途及正式产物资格由记录判定。提升为正式产物只新增产物关联并保存保留状态，不移动文件、不更改路径；失败或取消留下的修复目标按中间文件的保留和清理规则处理。

| 文件所处阶段 | 定位依据与允许操作 |
| --- | --- |
| 尚未交接的准备文件、录像输入或处理临时文件 | 按 `paths.staging` 和已登记相对路径定位；实际操作还须取得所属流程的使用或清理资格 |
| 已登记为正式产物的修复文件 | 仍按原中间文件记录定位；取回创建独立副本，显式清理通过正式产物身份授权 |
| 普通交付已经保存移动意图，但交接结果尚未确定 | 同时按原工作路径及 `deliveries.file_name` 在 `ready`、`processing` 中核实，按[交接恢复规则](../../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)处理；原位置为空不证明交接成功 |
| 普通交付已有可靠交接事实 | `relative_path` 保留原工作位置，交接文件按 `deliveries.file_name` 定位；主程序领取后遵守其所有权，不能在原位置另建同一副本 |
| 已可靠完成自动清理或正式产物清理 | 永久保留路径及历史；原位置不再用于创建或恢复该文件，历史查询也不要求文件仍存在 |

用途子目录须为可确认的实际目录，已存在的登记文件须为普通文件；不能沿用途目录或文件的符号链接访问另一位置。父目录缺失、目录不可访问、对象类型不符或检查错误，分别报告实际原因；只有正确目录内的目标文件被可靠确认不存在时，才能将“不存在”交给所属业务规则判断。既不能从路径缺失直接推断清理成功，也不能覆盖身份或归属不明的已有对象。

状态报告和日志副本不登记为 `intermediate_files`。它们继续由各自流程管理，不能因位于 `staging` 而进入普通中间文件清理。历史恢复只还原路径、责任及生命周期事实，不访问或移动实际文件。

## 正式产物与来源

`outputs.source_action_id` 引用产生该产物的动作。`kind` 固定为 `ORIGINAL`、`REPAIRED`、`PREVIEW`，分别对应公共 `original`、`repaired`、`preview`。`device_file_id` 与 `intermediate_file_id` 恰好一个有值，同一物理文件至多登记一个正式产物。产物的 `original_name`、`media_type` 是登记时取得的可读元信息；后续可靠补齐时与文件观察共同保存，不改变产物身份。

`availability` 为 `AVAILABLE`、`RESTRICTED`、`CLEANED`、`MISSING`、`UNKNOWN`。它是按文件存在性及全部清理项严格派生的查询投影：可靠清理完成优先，其次保留已发生或无法排除的删除限制，再按实际文件事实判断。`MISSING`、`UNKNOWN` 必须有 `error_json`。媒体问题不自动等于文件不可读取，不能从时长不足推导文件不存在。

`media_json` 是公共 `media` 的完整结构化观察。未检查使用 `check_status: not_performed` 及 `duration.status: unknown`，不能用时长 0 代替未知；检查及工具错误使用协议中的独立分类。大小和摘要从该历史边界的实际文件记录取得，正式产物表不重复保存第二套大小和摘要。清理汇总由 `outputs.cleanup_status` 和 `outputs.cleanup_error_json` 保存，相关事实与派生值共同提交；历史查询直接恢复这两个字段。完整判定、错误选择与保存规则见[产物清理汇总](output-cleanup-state.md)。

三项对象历史元数据遵守公共规则。`output_origins.output_id` 是引用方，`original_output_id` 是原文件，二者必须不同。原文件没有引用方关系，预览及修复成品各恰有一条、且两端属于同一动作。SQL 保证引用存在与引用方唯一，事务接口验证角色、同源以及第一版同一原文件至多一份预览和一份修复成品；不能以多候选任取第一份掩盖错误。

## 普通交付

`deliveries.action_id` 引用取回动作，`output_id` 引用真实选中产物；二者组合唯一。`file_name` 是完整 `<delivery_id>.<ext>`，按安全 ASCII 扩展名规则分配并保持不变；`display_name` 是可读名称。`status` 使用以下成员：`PENDING`、`PREPARING`、`PREPARED`、`PUBLISHING`、`PUBLISHED`、`FAILED`、`CANCELED`、`WITHDRAWN`。

`publication_intent_event_id` 在移动到 `ready` 前保存，尚无意图时为空；`published_event_id` 仅在移动与必要目录同步完成、或者恢复取得等价可靠交接依据时保存。已有成功交接不因文件后来消失而清除。失败时 `error_json` 必填；其他状态保留实际需要报告的错误，不能把中间文件清理错误当成交付重新失败。大小和最终摘要从所属拷贝及目标文件取得。

| 文件准备与交接事实 | 合法记录与下一步 |
| --- | --- |
| 尚未完成副本 | `PENDING` 或 `PREPARING`；同一事务授予读取资格、建立源依赖、交付、拷贝、读取流程与中间文件，相关记录具有完整的共同建档依据 |
| 完整副本已同步、校验通过 | `PREPARED`；准备完成与取回项源依赖解除共同提交 |
| 移动意图已保存，移动结果尚待确认 | `PUBLISHING`；不能因目录中暂时没有文件直接重拷 |
| 移动及目录同步已确认，或恢复有等价证据 | `PUBLISHED`；提交交接完成后取回才能按完整结果结束 |
| 准备或交接最终失败 | `FAILED`；保留可靠大小、摘要、已发生交接意图及未知事实 |
| 取消阻止了本次未完成交付 | `CANCELED`；仍需跟踪实际读写停止和适用中间文件清理 |
| `ready` 中的文件已可靠撤回 | `WITHDRAWN`；不声称主程序或客户端从未接收过其他副本 |

`withdrawal_state` 是交付自身独立责任：`NOT_REQUESTED`、`PENDING`、`WITHDRAWN`、`NOT_RETRACTABLE`、`FAILED`、`UNKNOWN`。`FAILED`、`UNKNOWN` 必填 `withdrawal_error_json`。多个取消请求复用同一交付责任，不能分别删除同一文件；逐请求报告由 `cancel_delivery_items` 保存。确认 `processing` 或已由主程序处理才是 `NOT_RETRACTABLE`，无法确认位置是 `UNKNOWN`。交付已撤回时 `deliveries.status` 为 `WITHDRAWN`；撤回失败不否定此前可靠的发布完成。

## 文件拷贝

`file_copies` 通过 `delivery_id` 或 `processing_id` 指定唯一用途，两者恰好一个非空；前者关联普通交付，后者关联录像内部处理。`source_device_file_id` 或 `source_intermediate_file_id` 恰好一个非空，`target_file_id` 必填且不能等于本地主机源文件。唯一读取流程通过 `operation_runs.copy_id` 单向引用该拷贝；读取次数与尝试只由公共操作表保存。

| 字段 | 含义与初始值 |
| --- | --- |
| `source_size`、`source_sha256` | 首次形成读取责任时确认的固定源长度和已知源摘要；摘要未知时为空，后来取得后保存实际依据，不能将查询失败当成不支持 |
| `round`、`recopies_used` | 初始 1、0；每开始一次额外重拷共同增加 1，始终满足 `round = recopies_used + 1` |
| `max_recopies_used` | 最近一次实际判定采用的本地上限；允许小于已用次数，不是累计预算的权威值 |
| `committed_bytes` | 当前轮次可靠进度，初始 0，始终不超过固定长度；分段成功后先同步文件再保存 |
| `reset_state` | `READY` 或 `RESET_PENDING`；后者表示新轮次已经登记，目标文件尚须可靠截断或重建，不能直接续传 |
| `slot_device_id` | 初始为空；取得后保存已可靠保留的相机读取机会，解除后为空；全库非空值唯一，等候重试时继续保留 |
| `verification_state` | `NOT_PERFORMED`、`RUNNING`、`MATCHED`、`MISMATCHED`、`SOURCE_CHECKSUM_UNAVAILABLE`、`FAILED` |
| `target_sha256` | 当前轮次完整目标摘要，尚未取得时为空；不能保存 Python 哈希对象或将部分摘要当成完整摘要 |
| `verification_error_json` | 校验明确失败时必填，其他校验状态为空；摘要不一致通过 `MISMATCHED` 表示 |

取得设备读取机会时，在同一写事务内检查已有保留、排序及资格，再填入 `slot_device_id`；本地文件拷贝不占用相机机会。机会归属与未完成责任的状态分类见[读取机会](waiting.md#相机读取机会与未完成责任)，候选发现及唤醒见[读取调度](../../architecture/file-copy.md#读取工作的发现与唤醒)。来源设备由文件的原绑定取得，不能填入取回动作的虚构设备。释放机会必须先确认实际读取及其重试已结束；释放机会与解除源读取依赖是不同事实。

`MATCHED`、`MISMATCHED`、`SOURCE_CHECKSUM_UNAVAILABLE` 要求完整可靠长度与目标摘要；其中 `MATCHED` 要求源摘要相等，`MISMATCHED` 要求不等，`SOURCE_CHECKSUM_UNAVAILABLE` 仅限驱动明确不提供源摘要且可靠读取条件成立。普通交付准备完成仅允许 `MATCHED` 或 `SOURCE_CHECKSUM_UNAVAILABLE`；内部输入副本也必须满足同样字节条件。文件媒体错误另由检查表达。

重拷事务保存额外轮次消耗、`committed_bytes = 0`、`reset_state = RESET_PENDING`、清除本轮目标摘要和校验结果。随后实际截断并同步目标，保存 `READY`，再启动读取。任何中断都按原轮次继续，不能用旧文件长度跳过重置。已有读取失败保留，新增读取尝试仍消耗原文件累计预算。

## 查询与完整性检查

来源产物使用 `outputs_source` 按 ID 分页；预览及修复成品通过 `origins_reverse` 反查并验证角色唯一性。设备文件按稳定身份唯一键复用，通过 `device_files_source` 枚举本次来源文件。取回的交付使用 `delivery_action`；未完成交接及独立撤回分别使用 `delivery_recovery`、`delivery_withdrawals`，不依赖原动作是否仍在运行。

中间文件按责任外键查询，历史清理使用 `intermediate_cleanup` 及 `id > cursor` / 固定范围上界分批读取，到末尾后依预算最多绕回一次。拷贝通过交付或录像处理唯一键定位。文件操作必须在数据库事务外完成；实际结果与相关依赖、产物、交付及历史在规定的结果事务中一起保存。

跨表校验包含：来源绑定相同、正式登记的文件已经完成、产物与来源角色一致、读取流程责任一致、交付与取回项一致、准备状态对应完整校验、文件提升后不再自动清理。失败或未知不能通过清空外键消除责任。

## 拷贝与读取流程的关联

每份 `file_copies` 拷贝恰有一个读取流程。关联的目标引用仅由 `operation_runs.copy_id` 保存，流程类型为 `READ_FILE`，责任键为 `read/<copy_id>`。拷贝记录保存自己的文件、进度与校验事实；程序通过拷贝编号形成责任键，查找并核对对应读取流程的类型、目标及归属。读取尝试通过 `operation_attempts.run_id` 归属该流程。

普通交付拷贝、录像内部原片检查和修复输入拷贝遵守同一关联规则。首次建立拷贝及读取责任时，两者共同保存；读取重试、分段推进、断点续传、摘要不一致后的整片重拷和重启恢复沿用原拷贝及流程身份。取消和收场保留原责任、尝试及累计次数。

业务代码在写事务内结合可靠旧状态与拟保存的完整记录，按以下分类检查，再写入历史及投影：

| 情况 | 处理 |
| --- | --- |
| 首次建立拷贝，拟保存的读取流程唯一，类型、责任键、目标及业务归属一致 | 共同保存拷贝、读取流程及相关创建事实 |
| 已有拷贝及其唯一流程，身份和归属正确 | 复用原流程，按相应操作规则更新进度、尝试或结果 |
| 拟提交的完整状态有拷贝但缺少流程，或同一拷贝对应多个读取流程 | 拒绝保存，报告一致性错误 |
| 流程类型、责任键、目标或所属动作、交付与拷贝不一致 | 拒绝保存，报告一致性错误 |
| 恢复读取时发现应有流程缺失、重复或归属矛盾 | 按状态库错误处理，不创建替代流程、不重置预算、不任取一条记录 |
| 提交结果未知 | 按原事务操作身份核实完整提交结果，再决定后续步骤 |

普通交付的拷贝、读取流程和尝试归属交付历史；录像内部拷贝及其流程和尝试归属录像动作历史。历史恢复在同一完整边界还原相关记录后建立关联，不读取最新流程来补齐旧报告。快照及事件格式包含恢复该关联所需的目标引用和业务身份；回放不执行读取副作用。
