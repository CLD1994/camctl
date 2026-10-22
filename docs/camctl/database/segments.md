# 分段集合、判断与默认范围

[数据库入口](../database-schema.md) · [公共分段设计](../../superpowers/specs/2026-10-23-video-result-modes-and-segmentation-design.md) · [OSMO 特化](../../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md)

## 公共层重构边界

公共分段层负责集合、默认视频范围、来源发布和历史边界；设备特化提供顺序、完成及范围判定。单文件与多段流程均按动作中显式冻结的结果合同分派。具体行为及检查副本责任由公共设计维护。

下文记录已交付的 OSMO 存储与端口，作为公共层重构的现有接缝。设备名称解析、OSV 资格及下界重算须按冻结特化组织；公共仓储和取回依赖公共合同。实际内容检查及其副本复用关系须补充独立、可分页的归属记录，保持来源共同提交与同一历史边界读取，不能仅扩展 OSMO 的有限判断 JSON。

## 现有集合与成员存储

`segment_sets` 保存原拍摄动作、来源完整边界 H0、来源种类、发布处置、适用活动及 RESULTS 流程、当前可靠扫描、判断版本和原目标毫秒。`SegmentResultRef(action_id,set_id,version)` 表达实际集合身份；原 `SegmentSetRef` 只表达可靠完整扫描，其中 `scan_id` 是实际 `operation_attempts.id`，不使用尝试序号替代。状态依次为 COLLECTING、MEMBERS_FIXED 和 ASSESSED；成员固定后不再增减，判断版本通过新集合版本表达。正常运行永久保留全部关系，编号在写事务中按已提交最大值加一分配。

来源分为完整扫描、跨轮保留及无 RESULTS。跨轮保留使用原 run 截至 H0 的全部实际文件，不将最后一轮局部观察当成完整目录。RESULTS 流程已经建立但尚未取得尝试时，跨轮保留集合为空，当前扫描也为空；无 RESULTS 只适用于原流程不存在的情况。当前扫描只取原 run 最大实际轮次；最后一轮错误或必要元信息未知时为空，旧完整扫描仍可提供保留文件的原证明。只有当前扫描覆盖全部保留文件且资格一致，来源才是完整扫描。集合创建前出现 H0 后的新尝试、页面或文件资格变化时拒绝旧申请；同 run 建立集合后不再授予新 RESULTS 尝试。

`segment_members` 保存真实设备文件、原实际结果页和固定文件历史位置，以及设备顺序键、角色、归属、写入和预览配对分类。大小、摘要及清理事实由独立文件和原页提供，不复制到成员。无可靠顺序键时两列共同为空。集合及文件、集合及稳定身份分别唯一；同集合原片可具有相同数值键但不同真实身份，完整技术排序仍稳定续读；该情形不证明摄制顺序。原观察已提供配对的模型输入继续核对实际同源原片和设备文件关联。原页之后的扫描配对通过可空 `pairing_fact_id` 引用独立文件的可靠事实，原始观察保持原样。

成员保存、成员读取和产物候选共同使用固定文件的严格资格。使用原页差集归属方法或显式配对事实时，核原结果页、适用等待及实际完成证明；缺少证明保持未知，声明的引用损坏则报错。配对事实及两端文件事件必须位于固定文件事件所在完整边界内，事实属于申请预览，首次正向关联须与原片一致。只有预览可填写 `pairing_fact_id`；完整主干可靠但 CAM 顺序键未知时，仍可得到配对预览候选，不能因此取得顺序或默认范围资格。候选读取在同一只读事务中核双方固定事实，不按当前裸完成列提升资格，也不产生正式产物。

`segment_gaps` 按 gap_no 保存完整第二遍分类。缺失位置用起止键区间表达，无 empty_member_id；非尾零原片必须引用其真实成员。左右邻接分别保存 KNOWN、ABSENT、UNKNOWN，只有 KNOWN 引用真实同集合成员。neighbors_json 只保存这两个固定状态，不积累成员。

`segment_scope_members` 按 ordinal 保存已判断范围的实际原片成员。output_id 准备时为空，只能由录像终态事务共同发布其真实 original output；未发布的关系不能作为正式默认目录使用。UNKNOWN 与 EMPTY 分别保存，既不补选范围外原片，也不把未知解释为空。

## 逐段媒体检查责任

`segment_inspections` 是来源 action 的子记录。每条记录固定一个 `segment_sets`、真实 `segment_members`、`device_files` 原片、`target_json` 覆盖目标及首次采用的 `budget_json`；同一 action/member 只能建立一条检查。`state` 从 PENDING 只推进一次到 COMPLETED、FAILED 或 UNCONFIRMED，`result_json` 与 `result_event_id` 只在结果可靠保存时写入。检查事实通过所属 action 的事件历史恢复，不设置独立 `last_event_id` 或 `change_count`。

检查输入沿 `file_copies.inspection_id` 关联唯一读取副本。`file_copies` 仍保存固定源长度、连续可靠字节进度、原读取流程与尝试预算及摘要校验；完整字节和校验状态可靠保存前不得探测媒体。检查副本的中间文件归来源 action，用途为 RECORDING_INPUT，创建时以 REQUIRED 保留状态固定。检查终态不自动释放该保护；来源提交与等待者复用完成后，由明确的 retention release 事件转为 RELEASABLE，之后才可进入维护清理。

`result_json.metric` 保存所请求量纲上的实际主视频值或未知原因；时长来自唯一主视频流时长，帧数只使用媒体工具显式计数得到的主流 `frame_count`，不会由时长或帧率推算。`result_json.probe` 保存同一媒体探测的完整逐流事实，`tool` 保存受管工具是否执行及错误。调用方可按固定成员逐个读取已保存事实并恢复覆盖累计；来源集合完整性、累计值、连续性与中断判断仍由来源 assessment 保存，不在检查行中复制聚合结论。

所有四表都属于原 action 的自身历史，成员、缺口、范围沿 set_id 继承所有权。成员和缺口列均为创建后不变的业务列；集合身份列不变，state 单调推进，member_count、facts_json、assessment_json 和 published_event_id 只写一次；默认关系身份列不变，output_id 只从空值发布一次。id 是结构身份，没有独立派生历史元数据。实际独立 device_files 行不进入动作自身快照。新增关系由 SEGMENT_FACTS 事件保存，普通内部进度不触发报告水位。

每次保存或读取最多 128 项。成员按集合、完整设备顺序键、稳定身份和行 ID 续读；缺口按集合及 gap_no，范围按集合及 ordinal 续读。游标绑定完整 SegmentSetRef 和读取种类。只有可靠固定的集合能向判断消费者提供成员；错误不返回空页。判断以整数保存已确认缺口计数；顺序未知或可信起点矛盾时 gap_count 为 JSON null，first_gap 也为空，不建立已确认缺口行。其他未知仍运行严格二遍分类；毫秒下界使用规范十进制文本，不能让 SQLite 上限改变事实。

P2 只保存有限事实。P3 在终态事务重新核对全部合格产物、范围关联和来源终态，提供同一 H 的增长历史分页恢复。既有整动作 restore_entity 和快照种子不是分段增长集合的正式有界入口。

## 错误头与完整公开详情

动作 error_details_json 保存有限、可按公共错误登记验证的公开 header：来源动作、说明、缺口数量、默认范围状态/数量、合格原片数量及可用首个缺口。集合引用只在所属关系中保存，不写入公开错误头。完整公开详情的 gaps、default_scope.output_ids、retained_output_ids 由报告方从同一 H 的关系逐批展开，并按原计数及实际身份核对完整性；未展开 header 不能直接作为完整分段错误发布。持久化首缺口只保存稳定文件身份，不接收未经同组核实的邻接 output_id；完整态由 P6 在同 H 补齐实际产物身份。P2 负责 header 与有限片段校验；P6 负责完整公开边界。

## 字段与固定文件事实

四张表的 `id` 均为单调正整数主键；`created_event_id` 是不可变业务引用，精确引用创建本行的事件 E。它不另建对象计数。P3 可以据此在 H 分页排除尚未创建的成员。所有外键都在共同事务提交时核对，正常运行不删除行，也不改所属动作、集合或文件。

| 表 | 普通业务列及空值含义 | 唯一性与查询索引 |
| --- | --- | --- |
| `segment_sets` | 动作、版本、原目标、来源种类、H0事务及末位、发布处置必填；无 RESULTS 时 run 为空，活动按原事实可空；`current_attempt_id/current_final_page_event_id` 共同有值或为空，完整扫描来源必须有值。取消版本可引用同源前版及实际取消事件；收集时计数为空，固定后为实际数量；普通模型的两个JSON共同有值，其他分区保留为空；正式发布只绑定一次 `published_event_id` | 动作及版本唯一；取消前版及取消事件唯一；按动作/版本、run/H0及创建事件定位；published_event_id 非空时 action_id 部分唯一 |
| `segment_members` | `set_id`、`file_id`、`page_event_id`、`file_event_id`、`identity`、`role`、`ownership`、`completion`、`pairing_state` 必填；`directory_number` 和 `segment_index` 共同为空表示键未知；`pairing_identity` 只在 PAIRED 时填写真实原片身份；可空 `pairing_fact_id` 只由预览引用已有配对事实 | 集合/文件、集合/身份分别唯一；数值键冲突保留全部真实身份；完整设备顺序、配对身份、文件反查及集合/创建事件都有索引 |
| `segment_gaps` | `set_id`、连续 `gap_no`、`start_directory`、`start_index`、`end_directory`、`end_index`、`kind`、`neighbors_json` 必填；`left_member_id`、`right_member_id` 只在相应邻接为 KNOWN 时有值；`empty_member_id` 只在真实非尾零文件分支有值 | 集合/缺口序号唯一；按缺口序号及创建事件续读 |
| `segment_scope_members` | `set_id`、连续 `ordinal`、`member_id` 必填；`output_id` 准备时为空，来源终态事务将其一次关联到同源同文件的 original | 集合/序号、集合/成员分别唯一，实际 output 只关联一次；按序号、成员反查及创建事件定位 |
| `segment_inspections` | 每个实际完整原片成员至多一条；固定 `action_id`、`set_id`、`member_id`、`source_device_file_id`、媒体目标和首次读取预算；结果可靠保存后写入终态、有限指标、主流探测事实及结果事件引用 | `(action_id, member_id)` 唯一；按集合和固定原片反查；所有业务变化沿来源 action 历史恢复，不建立独立对象计数 |

`scan_id` 指向原 RESULTS 的实际尝试。`PendingResultScan` 在页间保持同一个票据，可靠保存后只推进 `page_no` 和目录游标。`SaveResultPageCommand` 更新原尝试的首末页引用并保持其 RUNNING；只有整轮结束才保存尝试结果。另一轮重试取得新的物理尝试，不将前轮页面并入本轮完整扫描依据。固定成员时逐页核对原页链和差集引用数量，不能用单页或错误页固定集合。

`file_event_id` 指向该独立文件在一个完整历史事务中的最后自身事件。这条事件可以位于多文件事务的中间；读取先验证所属完整事务和文件关联，再以该事务的末事件作为合法 H，核对该文件在同一事务内没有更晚自身变化，然后复用限定单行恢复读取文件事实。绝不把中间事件号当作完整全局 H。后来确认大小、补齐摘要、改变存在性或清理状态，都不替换固定事实。P1 的 `size_bytes` 从这个固定文件事实取得；未知时保持 `None`，原页仍独立保留实际观察。

`inspect_segment(flow, SegmentInspectionRequest(action_id, member_id, target, budget, occurred_at))` 只接受固定集合中由该动作拥有、完整写入且仍存在的实际 original 成员。它保存或恢复唯一检查责任，沿可靠读取机会取得整个输入副本，并在副本完成字节校验后调用受管主视频探测；帧数目标显式请求主流帧数，时长目标读取主流时长。返回 `SegmentInspectionStep`，阶段为 WAITING、COMPLETED、FAILED 或 UNCONFIRMED；已保存终态直接返回持久化指标和 probe 文档，不再次调用探测器。

`read_inspection_for_member(action_id, member_id, owned)` 返回 `InspectionMemberFacts(inspection, copy)`；其副本事实同时核对来源原片、集合、用途、固定路径、字节长度、完整写入及校验状态。`release_inspection_copy_pin(inspection_id, occurred_at, key, owned)` 只允许已结束的检查责任调用现有中间文件保留释放事务；调用者须在来源和依赖该副本的取回责任收场后释放。失败或未完成时，该副本仍保持原保留限制，不能由维护器清理。

保存请求 `SegmentMemberInput(file_id, page_event_id, file_event_id, role, ownership, completion, pairing_identity=None, pairing_state=NOT_APPLICABLE)` 不接收大小或摘要。提交守卫独立读取原页、实际差集文件、固定文件历史和原动作/活动/流程/尝试，核对分类及配对。P1 的完整 `SegmentMember` 是读取和纯判断结果，不是另一份文件属性权威。

## 有限核实决定

`segment_check_conclusions`经`set_id`归原动作，一集合只保存一项不可变决定。`reason`、实际来源事件、原结束／配置事件及前版决定引用各有独立职责；`current_result/current_failure_json`只记录当前扫描的结果，不复制原始错误或全部成员。字段和枚举由权威SQL及内部登记维护。

`ResultCheckConclusionSave`携带原集合及完整有限申请，`conclude_result_check`按原操作键保存，返回实际`ResultCheckConclusionRef`。无 RESULTS 分区引用实际基准失败或已经结束的 START。基准失败只接受原 H0 内可靠 RETURN 错误或 UNCONFIRMED，独立核原未派发活动和不存在 START／RESULTS 责任。启动失败核原 START 的全部实际尝试、最后结果及关闭错误，保持既定启动失败含义，集合为空且范围未知。决定保存后动作仍运行，正式产物没有变化。实际 Task 与返回交接仍由原调用拥有者负责，后续共同完成事务再核取消、窗口及适用释放。

有限结束的全部分区与配置／流程关闭顺序遵守[拍摄完成规格](../../superpowers/specs/2026-10-18-osmo360ii-capture-completion-design.md)及[分段规格](../../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md)。存储决定不等同于来源终态或正式发布。

RESULTS采用配置、适用关闭和决定通过`ResultCheckWriteScope`声明同一原申请，由既有事务内核逐事件核对实际来源及完整组。变化配置先保存既有配置事件，再关闭原流程并保存决定；不变化时引用最后一次实际采用配置的原事件。原尝试次数、结果和错误保持原记录；原键核实读取整组，不能用后来配置解释当时的预算。历史读取在同一H同时恢复配置、流程与决定。

## 生产端口与后续分工

`contracts/segment_values.py` 提供拍摄、持久化和报告共用的有限值，`contracts/segment_errors.py` 维护有限错误头及单个缺口的公开表示。文本枚举仍从权威整数登记派生。报告层读取共享契约，不依赖拍摄处理器或设备模块。

`persistence/repositories/segment_listing.py` 将已保存原页的单文件事实逐项写入临时派生索引，按设备键续读；普通录像和原生延时的完成模块消费同一仓储端口。`persistence/segment_order.py` 只管理该临时索引的连接与清理。索引不替代原页、固定文件事实或正式集合，也不提交业务状态；正式状态库的连接和提交仍由既有事务内核管理。

`create_result_set(ResultSetCreate,key,owned)` 保存统一集合，版本及实际集合 ID 由事务分配；原键比较完整原申请并恢复原 ref。`read_result_set(ref,owned)` 返回有限元信息；`read_current_segment_ref(ref,owned)` 只在原集合确有当前完整扫描时返回严格扫描引用。`read_result_member_inputs(ref,cursor,limit,owned)` 从原页索引按 file_id 去重续读，游标绑定原 ref，每页最多128项；文件资格固定在 H0，可靠完成引用原证明页，其他观察引用 H0 前最后实际页。它不产生正式产物或默认范围。

`append_result_members(ref,items,occurred_at,key,owned)` 只保存1—128项实际来源引用，独立核对原H0中的完整文件、原页和分类；`fix_result_members(ref,occurred_at,key,owned)` 按file_id双向比较全部实际来源与已保存成员，遗漏、替换、错误固定事件或分类均拒绝。成员来源仍覆盖早轮已可靠取得文件；后轮负向配对事实不会抹去保留文件的首次正向关联。当前扫描分类继续采用最后实际轮次自身的事实，不能借用保留资格补足当前集合。

取消版本绑定实际取消请求及适用原未发布版本，同前版／取消事件唯一；没有前版时按动作／取消事件唯一。版本准备不登记正式 outputs 或 scope，实际取消处置按[普通录像取消](../../architecture/camera-recording.md#camera_record-取消)及原生规则分别判断。下述完整扫描模型端口保留其原有职责；它们不能用于把跨轮保留或无 RESULTS 集合转换成完整扫描。

`SegmentRepository.create(SegmentSetCreate, key, owned)` 建立原扫描集合。`append_members(ref, inputs, occurred_at, key, owned)`、`append_gaps(ref, gaps, occurred_at, key, owned)`、`append_scope(ref, member_ids, occurred_at, key, owned)` 只接受 1—128 项元组；回执只返回原 `SegmentSetRef`。`fix_members(ref, occurred_at, key, owned)` 核对完整原页覆盖后固定成员。`save_assessment(ref, facts, assessment, occurred_at, key, owned)` 在可靠成员上重跑 P1 首遍及完整第二遍，并逐项对比保存缺口；缺项、多项、末游标、首缺口或数量不同均回滚。

`read_set(ref, owned)` 返回有限元信息；`read_members`、`read_gaps`、`read_scope` 使用 `(ref, cursor, batch, owned)` 返回 `Page`，每页最多 128 项。三种继续位置分别为 `SegmentMemberCursor`、`SegmentGapCursor` 和 `SegmentScopeCursor`，均绑定完整引用，不能跨种类或版本复用。成员完整顺序包含目录整数、index、稳定身份及行 ID；空键排在可靠键之前。`read_assessment(ref, owned)` 返回有限 P1 判断。`read_active_set(action_id, owned)` 只返回明确发布的唯一 SegmentSetRef，尚未发布返回 None，不按版本或最大ID猜测。正式 `read_scope` 要求来源已经终态、该集合具有明确 published_event_id，且全部原判断数量的 output 关联均已发布；`published=False` 仅用于内部准备及核实。

`segment_activation_rows(ref, event_id, owned)`、`segment_scope_publication_rows(ref, bindings, owned)` 和 `segment_event(ref, branch, rows, event_id, transaction_id, occurred_at, request_input=...)` 提供 P3 组合终态的有限构造基础，不自行提交完整终态事务。`bindings` 每项为 `(scope_member_id, output_id)`；PUBLISH 事件的原申请使用相同二项元组批次。`register_segment_guards()` 接入独立守卫；`SegmentReadScope` 只授权同集合的有限复核，不授权任意 SQL、设备操作或全动作驻留。

报告生成由 `reporting/bounded_generation.py` 在报告固定历史边界恢复来源动作、独立文件事实及关联实体；分段缺口、默认范围和保留产物逐批展开，再按公共错误登记流式校验和编码。动作中的有限错误头继续保持有限，报告中的完整详情引用同一边界的实际产物身份。独立 `state_sync` 通过历史归属关系读取，不要求它属于动作。实际完成与验证范围由[正式实施计划](../../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md)及[组件验证记录](../verification.md)跟踪。

完整 `SegmentAssessment` 保存及独立重算仅适用于 `osmo360ii_record_segments_v1`。原生延时 `osmo_empty_tail_classification_v1` 由 `capture/native_completion.py` 和 `segment_native.py` 使用专属空尾分类，沿原自然完成或取消依据发布合格文件。两种策略复用固定集合的 action/activity/run/实际 attempt、末页、版本、创建事件及成员 file/page/file_event 引用；普通录像下界和 N 范围只适用于普通分段策略。

公共层重构后，处理器按动作中显式冻结的单文件或多段结果合同选择独立模块。Action6 与 OSMO 360 II 都保留两种流程，各恢复入口消费同一选择。分段模块保存可靠扫描的范围结束事实，再申请释放目录占用；仓储仍独立核对控制完成、在途调用及范围结束等释放条件。保存回执未知时保留原完整申请和操作键，核实后才继续发布。

ACTIVATE 的独立守卫只接受原完成事务中已经验证的 ACTION_FINISHED、OUTPUT_REGISTERED、output_origins 及范围 PUBLISH 事实，核对实际来源/文件身份和完整数量。当前 action 已终态不能替代同组证明；已有正式集合时拒绝新键再次发布。UNKNOWN/EMPTY 的零范围关系仍有正式集合引用。发布事件可经原 SegmentReadScope 或已验证为同计划的 PlanReadScope 进入守卫；该端口只读取内核已有已验证事件，不复制增长事件数组。完整原申请、终态、产物、origin、范围和标记由同一有界整组执行器提交，原 key 的未知回执核实读取原完整申请；既有 FinishCaptureCommand 的整对象重用入口不能替代分段完成申请。

分段公共 scope/neighbor/kind 字面量由 enum-registry.json 的成员名小写生成；check-database-spec.py --write-enum-doc 同步公开 Schema 并在只读检查时核对。Python StrEnum 类同样从该登记生成，数据库使用原整数编号。事件类型与分支由 event-transitions.json 生成，动作及逐项错误编号从公共错误登记生成。

四表的报告路由只在所属集合 published_event_id 非空时成立；失败来源下新建未发布版本仍是内部事实，不推进该集合的公开水位。公开列依赖同时读取权威 projection 和 route.when 中的 column，保证正式发布条件本身改变时会通知原 action。内核按公开事件分别保留 change_seq 和目录记录，报告选择按实体合并；整个完成事务的 marker 与完整关系原子可见。

## 正式产物候选与同组闭合

`segment_output_candidate(ref, member_id, owned)` 从固定 `file_event_id` 返回一项 `SegmentOutputCandidate` 或 `None`。候选只含 `member_id`、`file_id`、既有 `OutputKind`；预览另外含 `original_member_id`、`original_file_id`，不复制大小或摘要。`SegmentRepository.read_output_candidates(ref, cursor, batch, owned)` 沿原成员技术游标返回最多128项候选；过滤后空页仍可能有继续位置，调用方必须读到 `next_cursor=None`。P3 的候选计数和事件规划使用此端口。

原片只有归属可靠、形成完整且非零时成为候选。预览除自身符合这些条件，还须可靠 PAIRED 到同集合、完整名称主干一致的合格原片；配到空尾、非尾零文件或冻结资格未知的原片时不登记预览，也不补造原片 output。零字节、未完成、未知或未配对的可选 LRF 保留原始观察；跨集合、缺失或错误名称的已声称配对仍是错误。输出逐项资格、全体候选覆盖和默认原片准备均复用这一判定；预览 origin 精确指向候选给出的原片实际 output。

每条 PUBLISH 还须证明本组最终存在同 ref/set 的唯一准确 ACTIVATE。`SqlEventReads.segment_activation_event(event_id, transaction)` 只按 marker 身份定位原事务提案的一条实际事件；它引用既有提案序列，不复制增长事件。该端口证明提案结构闭合，不将未来终态、output 或范围当成已验证资格。PUBLISH 的来源和产物仍只取此前已经验证的事件，未来 ACTIVATE 仍按正常次序完成全部独立守卫；任何一处不合法都整组回滚。仅伪造 `transaction_rows` 中的 marker 值不能通过。

完整录像成功收场保持原 PlanReadScope；SegmentReadScope 仍只授权 P2 具名事实核验。该新增单事件证明端口可由 P3 的有界事务读取实现替换，不要求业务资格改成读取未来状态。
