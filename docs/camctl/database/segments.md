# 分段集合、判断与默认范围

[数据库入口](../database-schema.md) · [分段行为规格](../../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md)

`segment_sets` 保存原拍摄动作、活动、RESULTS 流程、实际尝试、可靠末页、判断版本和原目标毫秒。`scan_id` 是实际 `operation_attempts.id`，不使用尝试序号替代。状态依次为 COLLECTING、MEMBERS_FIXED 和 ASSESSED；成员固定后不再增减，判断版本通过新集合版本表达。正常运行永久保留全部关系，编号在写事务中按已提交最大值加一分配。

`segment_members` 保存真实设备文件、原实际结果页和固定文件历史位置，以及设备顺序键、角色、归属、写入和预览配对分类。大小、摘要及清理事实由独立文件和原页提供，不复制到成员。无可靠顺序键时两列共同为空。集合及文件、集合及稳定身份分别唯一；同集合原片可具有相同数值键但不同真实身份，完整技术排序仍稳定续读；该情形不证明摄制顺序。原观察已提供配对的模型输入继续核对实际同源原片和设备文件关联。原页之后的扫描配对通过可空 `pairing_fact_id` 引用独立文件的可靠事实，原始观察保持原样。

成员保存、成员读取和产物候选共同使用固定文件的严格资格。使用原页差集归属方法或显式配对事实时，核原结果页、适用等待及实际完成证明；缺少证明保持未知，声明的引用损坏则报错。配对事实及两端文件事件必须位于固定文件事件所在完整边界内，事实属于申请预览，首次正向关联须与原片一致。只有预览可填写 `pairing_fact_id`；完整主干可靠但 CAM 顺序键未知时，仍可得到配对预览候选，不能因此取得顺序或默认范围资格。候选读取在同一只读事务中核双方固定事实，不按当前裸完成列提升资格，也不产生正式产物。

`segment_gaps` 按 gap_no 保存完整第二遍分类。缺失位置用起止键区间表达，无 empty_member_id；非尾零原片必须引用其真实成员。左右邻接分别保存 KNOWN、ABSENT、UNKNOWN，只有 KNOWN 引用真实同集合成员。neighbors_json 只保存这两个固定状态，不积累成员。

`segment_scope_members` 按 ordinal 保存已判断范围的实际原片成员。output_id 准备时为空，只能由录像终态事务共同发布其真实 original output；未发布的关系不能作为正式默认目录使用。UNKNOWN 与 EMPTY 分别保存，既不补选范围外原片，也不把未知解释为空。

所有四表都属于原 action 的自身历史，成员、缺口、范围沿 set_id 继承所有权。成员和缺口列均为创建后不变的业务列；集合身份列不变，state 单调推进，member_count、facts_json、assessment_json 和 published_event_id 只写一次；默认关系身份列不变，output_id 只从空值发布一次。id 是结构身份，没有独立派生历史元数据。实际独立 device_files 行不进入动作自身快照。新增关系由 SEGMENT_FACTS 事件保存，普通内部进度不触发报告水位。

每次保存或读取最多 128 项。成员按集合、完整设备顺序键、稳定身份和行 ID 续读；缺口按集合及 gap_no，范围按集合及 ordinal 续读。游标绑定完整 SegmentSetRef 和读取种类。只有可靠固定的集合能向判断消费者提供成员；错误不返回空页。判断以整数保存已确认缺口计数；顺序未知或可信起点矛盾时 gap_count 为 JSON null，first_gap 也为空，不建立已确认缺口行。其他未知仍运行严格二遍分类；毫秒下界使用规范十进制文本，不能让 SQLite 上限改变事实。

P2 只保存有限事实。P3 在终态事务重新核对全部合格产物、范围关联和来源终态，提供同一 H 的增长历史分页恢复。既有整动作 restore_entity 和快照种子不是分段增长集合的正式有界入口。

## 错误头与完整公开详情

动作 error_details_json 保存有限、可按公共错误登记验证的公开 header：来源动作、说明、缺口数量、默认范围状态/数量、合格原片数量及可用首个缺口。集合引用只在所属关系中保存，不写入公开错误头。完整公开详情的 gaps、default_scope.output_ids、retained_output_ids 由报告方从同一 H 的关系逐批展开，并按原计数及实际身份核对完整性；未展开 header 不能直接作为完整分段错误发布。持久化首缺口只保存稳定文件身份，不接收未经同组核实的邻接 output_id；完整态由 P6 在同 H 补齐实际产物身份。P2 负责 header 与有限片段校验；P6 负责完整公开边界。

## 字段与固定文件事实

四张表的 `id` 均为单调正整数主键；`created_event_id` 是不可变业务引用，精确引用创建本行的事件 E。它不另建对象计数。P3 可以据此在 H 分页排除尚未创建的成员。所有外键都在共同事务提交时核对，正常运行不删除行，也不改所属动作、集合或文件。

| 表 | 普通业务列及空值含义 | 唯一性与查询索引 |
| --- | --- | --- |
| `segment_sets` | `action_id`、`activity_id`、`run_id`、`attempt_id`、`final_page_event_id`、`version`、`target_duration_ms` 都必填；`state` 从收集推进至成员固定及判断完成；收集时 `member_count` 为空，固定后为实际非负数量；`facts_json`、`assessment_json` 只在判断保存时共同有值；`published_event_id` 未发布时为空，同完成事务的 ACTIVATE 一次绑定自身 E | 动作及版本、实际尝试及版本分别唯一；按动作/版本和动作/创建事件定位；published_event_id 非空时 action_id 部分唯一 |
| `segment_members` | `set_id`、`file_id`、`page_event_id`、`file_event_id`、`identity`、`role`、`ownership`、`completion`、`pairing_state` 必填；`directory_number` 和 `segment_index` 共同为空表示键未知；`pairing_identity` 只在 PAIRED 时填写真实原片身份；可空 `pairing_fact_id` 只由预览引用已有配对事实 | 集合/文件、集合/身份分别唯一；数值键冲突保留全部真实身份；完整设备顺序、配对身份、文件反查及集合/创建事件都有索引 |
| `segment_gaps` | `set_id`、连续 `gap_no`、`start_directory`、`start_index`、`end_directory`、`end_index`、`kind`、`neighbors_json` 必填；`left_member_id`、`right_member_id` 只在相应邻接为 KNOWN 时有值；`empty_member_id` 只在真实非尾零文件分支有值 | 集合/缺口序号唯一；按缺口序号及创建事件续读 |
| `segment_scope_members` | `set_id`、连续 `ordinal`、`member_id` 必填；`output_id` 准备时为空，来源终态事务将其一次关联到同源同文件的 original | 集合/序号、集合/成员分别唯一，实际 output 只关联一次；按序号、成员反查及创建事件定位 |

`scan_id` 指向原 RESULTS 的实际尝试。`PendingResultScan` 在页间保持同一个票据，可靠保存后只推进 `page_no` 和目录游标。`SaveResultPageCommand` 更新原尝试的首末页引用并保持其 RUNNING；只有整轮结束才保存尝试结果。另一轮重试取得新的物理尝试，不将前轮页面并入本轮完整扫描依据。固定成员时逐页核对原页链和差集引用数量，不能用单页或错误页固定集合。

`file_event_id` 指向该独立文件在一个完整历史事务中的最后自身事件。这条事件可以位于多文件事务的中间；读取先验证所属完整事务和文件关联，再以该事务的末事件作为合法 H，核对该文件在同一事务内没有更晚自身变化，然后复用限定单行恢复读取文件事实。绝不把中间事件号当作完整全局 H。后来确认大小、补齐摘要、改变存在性或清理状态，都不替换固定事实。P1 的 `size_bytes` 从这个固定文件事实取得；未知时保持 `None`，原页仍独立保留实际观察。

保存请求 `SegmentMemberInput(file_id, page_event_id, file_event_id, role, ownership, completion, pairing_identity=None, pairing_state=NOT_APPLICABLE)` 不接收大小或摘要。提交守卫独立读取原页、实际差集文件、固定文件历史和原动作/活动/流程/尝试，核对分类及配对。P1 的完整 `SegmentMember` 是读取和纯判断结果，不是另一份文件属性权威。

## 生产端口与后续分工

`SegmentRepository.create(SegmentSetCreate, key, owned)` 建立原扫描集合。`append_members(ref, inputs, occurred_at, key, owned)`、`append_gaps(ref, gaps, occurred_at, key, owned)`、`append_scope(ref, member_ids, occurred_at, key, owned)` 只接受 1—128 项元组；回执只返回原 `SegmentSetRef`。`fix_members(ref, occurred_at, key, owned)` 核对完整原页覆盖后固定成员。`save_assessment(ref, facts, assessment, occurred_at, key, owned)` 在可靠成员上重跑 P1 首遍及完整第二遍，并逐项对比保存缺口；缺项、多项、末游标、首缺口或数量不同均回滚。

`read_set(ref, owned)` 返回有限元信息；`read_members`、`read_gaps`、`read_scope` 使用 `(ref, cursor, batch, owned)` 返回 `Page`，每页最多 128 项。三种继续位置分别为 `SegmentMemberCursor`、`SegmentGapCursor` 和 `SegmentScopeCursor`，均绑定完整引用，不能跨种类或版本复用。成员完整顺序包含目录整数、index、稳定身份及行 ID；空键排在可靠键之前。`read_assessment(ref, owned)` 返回有限 P1 判断。`read_active_set(action_id, owned)` 只返回明确发布的唯一 SegmentSetRef，尚未发布返回 None，不按版本或最大ID猜测。正式 `read_scope` 要求来源已经终态、该集合具有明确 published_event_id，且全部原判断数量的 output 关联均已发布；`published=False` 仅用于内部准备及核实。

`segment_activation_rows(ref, event_id, owned)`、`segment_scope_publication_rows(ref, bindings, owned)` 和 `segment_event(ref, branch, rows, event_id, transaction_id, occurred_at, request_input=...)` 提供 P3 组合终态的有限构造基础，不自行提交完整终态事务。`bindings` 每项为 `(scope_member_id, output_id)`；PUBLISH 事件的原申请使用相同二项元组批次。`register_segment_guards()` 接入独立守卫；`SegmentReadScope` 只授权同集合的有限复核，不授权任意 SQL、设备操作或全动作驻留。

现有报告 `registered_error` 仍将 `actions.error_details_json` 视为一棵普通错误树。关系和 `segment_error_expansion_at_same_h` 已登记数据来源，P6 必须实际接入增长展开及完整公开校验；该登记不代表当前报告已经补齐数组。P3 的完整原子发布、原申请有界核实和增长历史读取，以及 P4 的实际控制/等待资格核验，仍分别由对应任务完成。

完整 `SegmentAssessment` 保存及独立重算仅适用于 `osmo360ii_record_segments_v1`，原生延时 `osmo_empty_tail_classification_v1` 调用该入口明确拒绝。两种策略都可复用固定集合的 action/activity/run/实际 attempt、末页、版本、创建事件及成员 file/page/file_event 引用。原生延时目前只具备原始冻结事实端口；其专属空尾分类保存、时序和产物排除由 P4 接入，不调用普通录像下界或 N 范围。

ACTIVATE 的独立守卫只接受原完成事务中已经验证的 ACTION_FINISHED、OUTPUT_REGISTERED、output_origins 及范围 PUBLISH 事实，核对实际来源/文件身份和完整数量。当前 action 已终态不能替代同组证明；已有正式集合时拒绝新键再次发布。UNKNOWN/EMPTY 的零范围关系仍有正式集合引用。发布事件可经原 SegmentReadScope 或已验证为同计划的 PlanReadScope 进入守卫；该端口只读取内核已有已验证事件，不复制增长事件数组。P3 仍须将完整原申请、终态、产物、origin、范围和标记放入有界整组执行器，并负责原 key 的未知回执核实；既有 FinishCaptureCommand 的整对象重用入口不是该新完成申请的正式实现。

分段公共 scope/neighbor/kind 字面量由 enum-registry.json 的成员名小写生成；check-database-spec.py --write-enum-doc 同步公开 Schema 并在只读检查时核对。Python StrEnum 类同样从该登记生成，数据库使用原整数编号。事件类型与分支由 event-transitions.json 生成，动作及逐项错误编号从公共错误登记生成。

四表的报告路由只在所属集合 published_event_id 非空时成立；失败来源下新建未发布版本仍是内部事实，不推进该集合的公开水位。公开列依赖同时读取权威 projection 和 route.when 中的 column，保证正式发布条件本身改变时会通知原 action。内核按公开事件分别保留 change_seq 和目录记录，报告选择按实体合并；整个完成事务的 marker 与完整关系原子可见。

## 正式产物候选与同组闭合

`segment_output_candidate(ref, member_id, owned)` 从固定 `file_event_id` 返回一项 `SegmentOutputCandidate` 或 `None`。候选只含 `member_id`、`file_id`、既有 `OutputKind`；预览另外含 `original_member_id`、`original_file_id`，不复制大小或摘要。`SegmentRepository.read_output_candidates(ref, cursor, batch, owned)` 沿原成员技术游标返回最多128项候选；过滤后空页仍可能有继续位置，调用方必须读到 `next_cursor=None`。P3 的候选计数和事件规划使用此端口。

原片只有归属可靠、形成完整且非零时成为候选。预览除自身符合这些条件，还须可靠 PAIRED 到同集合、完整名称主干一致的合格原片；配到空尾、非尾零文件或冻结资格未知的原片时不登记预览，也不补造原片 output。零字节、未完成、未知或未配对的可选 LRF 保留原始观察；跨集合、缺失或错误名称的已声称配对仍是错误。输出逐项资格、全体候选覆盖和默认原片准备均复用这一判定；预览 origin 精确指向候选给出的原片实际 output。

每条 PUBLISH 还须证明本组最终存在同 ref/set 的唯一准确 ACTIVATE。`SqlEventReads.segment_activation_event(event_id, transaction)` 只按 marker 身份定位原事务提案的一条实际事件；它引用既有提案序列，不复制增长事件。该端口证明提案结构闭合，不将未来终态、output 或范围当成已验证资格。PUBLISH 的来源和产物仍只取此前已经验证的事件，未来 ACTIVATE 仍按正常次序完成全部独立守卫；任何一处不合法都整组回滚。仅伪造 `transaction_rows` 中的 marker 值不能通过。

完整录像成功收场保持原 PlanReadScope；SegmentReadScope 仍只授权 P2 具名事实核验。该新增单事件证明端口可由 P3 的有界事务读取实现替换，不要求业务资格改成读取未来状态。
