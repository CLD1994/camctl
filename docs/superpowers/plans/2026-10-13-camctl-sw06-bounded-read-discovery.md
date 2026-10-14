# SW-06：默认读取发现的分批处理实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 或已经明确指派的子任务逐项执行。每阶段先验证能够证伪契约的失败，再实现，并由复核者沿默认生产入口核对查询、排序、机会归属与继续推进。

**目标：** 默认取回及录像内部读取以有限批次发现工作，既保留全体合格候选的业务排序，又限制查询返回和 Python 候选详情容量；分页、取消和重启不丢失读取责任。

**组织建议：** SQLite 在限定设备／工作范围内判断全体候选资格并排序，Python 只接收本批必要事实。机会授予事务复用同一候选规则，先恢复原持有者；批次继续位置和通知由会话级拥有者保持，详情缓存可丢弃而责任不丢弃。

**技术基础：** Python 3.11、SQLite、既有 `Page`、`SlotRequest`、读取结果收场、会话通知及配置装配。

**设计依据：** [读取发现与唤醒](../../architecture/file-copy.md#读取工作的发现与唤醒)、[候选文件顺序](../../architecture/file-copy.md#多个候选文件的选择顺序)、[取回与清理资格](../../architecture/outputs.md#延后执行时的取回与清理顺序)、[分页结果契约](../../camctl/module-contracts.md#分页结果契约)、[相机读取机会](../../camctl/database/waiting.md#相机读取机会与未完成责任)、[W-11](../../camctl/database/consistency-verification.md#相机读取机会与调度交接)。本计划承接[路线图 SW-06](2026-09-30-camctl-implementation-roadmap.md#文档复核发现的实现缺口)，顺序在[SW-05](2026-10-13-camctl-sw05-session-lock-errors.md)之后。

## 实施前入口审计与文件责任

2026-10-13：`outputs/dispatch.py::plan_device_work` 四次无界 `fetchall()`，把活动和候选累计到设备字典及元组；唯一生产消费者是 `outputs/obtain_flow.py::_advance_reads`。其绑定检查也无界收集候选；`_advance_local_reads` 一次收集全部主机源未完成拷贝。

同一排序不变量还有一个实际授予边界：`persistence/repositories/outputs.py::_SlotChangeCommand._first_candidate` 加载该设备全部拷贝，再在 Python 累计所有排序键；仅分页外层发现仍会在每次授予时全量加载。`_device_holder` 也应使用足以识别正常唯一持有者及非法多持有者的有限结果。录像内部 `capture/media_flow.py` 经同一 `grant_read_slot`，因此必须一并核对。

`scheduling/discovery.py` 的 `discover_work` 和 `BoundedCandidateCache` 是未接入这些消费者的辅助接口，不能用它们的直接测试替代实际入口验收。取回进入读取前的 `_start_due`、`_running_actions`，逐动作待选择／待发布项及 `_settle_failed_reads` 也可能先积累全部工作，须在本次读取链审计中处理，避免外层仍无界装载。

进一步核对发现：`load_obtain_facts` 会构造某动作的全部来源及条目；`_ProductReads.actions/plan_actions/members` 虽使用 `fetchmany(128)`，仍累计全部行到 `state_rows`，`has_product_predecessor` 与 `_CurrentProductReads` 的守卫随后消费完整集合。本计划必须区分“有批量取数”与“累计状态有界”，不能绕过这些实际前置消费者后宣称默认读取链容量已受限。

路径相对于 `apps/camctl`。新增模块、名称和内部签名是建议；执行时可调整组织，但任务中的生产者与消费者必须共同变更。

| 文件 | 责任 |
| --- | --- |
| `src/camctl/outputs/read_discovery.py`（建议新增） | 读取候选的限定范围、分页、全体资格／排序查询及会话级继续位置。 |
| `src/camctl/outputs/dispatch.py`、`obtain_flow.py` | 设备活动／读取发现、绑定失败检查、主机源读取和前置取回工作分批推进。 |
| `src/camctl/persistence/repositories/outputs.py` | 机会授予事务使用全体合格候选的有界查询；保留事实缺失／多持有者错误。 |
| `src/camctl/outputs/competition.py`、`obtain_summary.py` | 同产物前序判断与取回汇总逐批折叠必要事实，不累计完整候选详情；契约与独立守卫保持。 |
| `src/camctl/history/reads.py`、`validators.py`、`persistence/transaction.py` | 为相关资格守卫接入原写事务内的限定只读事实端口；其他事件守卫和回放语义保持。 |
| `src/camctl/persistence/event_reads.py` | 合并原事务 SQL 与已验证前序变化，限定允许读取的表、列、对象范围和批次。 |
| `src/camctl/capture/media_flow.py`、`bootstrap/capture_assembly.py` | 内部原片检查和修复输入消费统一读取机会，核对实际恢复及配置传递。 |
| `src/camctl/bootstrap/obtain_assembly.py`、`lifecycle.py` | 会话级发现状态与限额在各轮工厂之间共享，连接关闭不丢继续位置。 |
| `src/camctl/bootstrap/config.py`、`docs/architecture/configuration.md` | 实施阶段落实读取发现批次配置及合法性，不借用媒体字节段或报告批次含义。 |
| `tests/unit/outputs/test_read_discovery.py`（建议新增） | 页继续、范围绑定、缓存淘汰、通知和重查规则；不访问真实数据库。 |
| `tests/integration/outputs/test_read_discovery.py`（新增） | 真实 SQL 返回容量、排序、授予及完整候选分类。 |
| `tests/integration/bootstrap/test_read_discovery_default.py`（新增） | 默认装配的普通／预览／内部读取及主机源、多批次和新提交组合。 |

## 全局约束及方案接口

- 公共协议、报告格式、文件身份及历史含义不变。批次仅限制发现，不限制任务数量或缩小已经固定的来源集合。
- 当前已持有机会的读取先恢复，包括重试等待及独立停止／保存责任；同一相机最多一份真实读取。主机源交付副本不占相机机会。
- 全体合格候选按所属动作 `scheduled_at`、`plans.id`、`input_index`、首次文件登记顺序比较；普通取回和自动预览使用各自取回动作，内部检查／修复使用所属录像动作，不按用途插队。
- 使用 `scheduling/order.py::file_key` 的既有语义核对 SQL 排序。先前取得的源读取资格保持；新的删除限制不撤销旧资格，尚未授予者按原资格规则判断。排序和分页均不授予读取资格。
- 本地配置 `session.read_discovery_batch_size` 默认值为 `100`，合法值为非布尔的正整数，会话内固定，下一运行采用新值。该配置已在配置责任专题定义并接入真实装配；生产默认值由 `read_discovery.py` 的常量维护，`ConfigDefaults` 从该常量读取。
- 详情最多保留一个当前批次，容量随批次 B 受限；不额外引入无用途的缓存配置。可以不缓存详情，也不能为淘汰当前详情而结束持久化责任。每个固定流程种类保留有限游标及推进状态，不累计历次所有页。
- 建议接口为 `read_candidates(connection, scope: ReadDiscoveryScope, cursor: ReadDiscoveryCursor | None, *, limit: int) -> Page[ReadCandidate, ReadDiscoveryCursor]`。`scope` 明确设备或主机来源、查询种类、当前判断时间及扫描代次；游标携带同范围绑定和稳定排序继续位置。`ReadCandidate` 至少保存 `copy_id`、发起动作与计划身份、固定登记顺序、来源绑定及必要资格事实。
- 建议机会事务使用 `first_eligible_read(connection, device_id: str, now_us: int) -> int | None`，共享上述全体候选资格和排序查询；SQLite 内部排序不等于 Python 加载全部对象。事务内再次核对当前持有者和本请求资格，不信任外层缓存。
- 当前 `grant_read_slot(request, key, owned)` 签名和原操作键核实保持。缺少必要关联、重复 READ 流程或多个持有者不能通过 SQL 的内连接或 `LIMIT 1` 隐藏；用有限错误探测查询保留一致性错误。
- 逐产物前序规则仍以 `has_product_predecessor` 为唯一语义入口。建议把 `CompetitionReads` 的动作和成员接口改为可按批次消费的迭代接口，比较时仅保留本次键、当前批及实际阻挡事实；计划／组／来源定位采用限定查询，不能继续把所有活动动作保存在 `state_rows`。生产资格与独立守卫分别验证，不能由一个未经复核的“无前序”布尔值授权。
- 守卫需要完整当前事实时，建议由事务内核向 `EventContext` 提供限定只读端口，范围包含本次产物、发起动作和比较时刻。端口把原写事务的提交前投影与本事务已验证的前序事件变化合并，表达“该事件发生前”的状态；不能只读尚未应用前序变化的 SQL 行，更不能读取未来提案。保留静态内存事件验证的原路径，历史回放不访问今天的投影来代替原边界。先验证端口范围与守卫拒绝反例，再移除相关全量候选读入；不能删掉完整性守卫来获得有界结果。
- 上述内部端口可提供 `iter_members(table, column, identity, *, limit) -> Iterator[tuple[tuple[int, Mapping], ...]]`；允许范围由原事务可信读取依据限定，表／列不能由外部输入任意选择。每批受 B 限制，完整性错误和取消不返回伪空集合；前序事件覆盖只保留本事务实际触及的行，与全体候选详情分别管理。
- 取回完成判断可按批次折叠“来源是否全部固定、是否有未决条目、各终态结果”或用等价 SQL 聚合取得必要事实，不为汇总保留全部 `sources/items`。与汇总关联的事件完整性、终态及历史恢复仍独立核对。
- `Page` 只保存 `items` 与 `next_cursor`，`exhausted` 从后者推导。每次调用候选数量受 B 限制，不能为了凑满有效结果在内部无限续读；空有效页有继续位置时仍需推进。
- 实际数据库游标及读事务在交付本页前结束；设备及文件操作在事务外执行。不把活动 SQL 游标跨 `await` 保存，不用 `OFFSET` 加内存全量排序代替稳定继续位置。
- 测试与命令遵守[组件测试指南](../../../apps/camctl/tests/AGENTS.md)。真实相机、目标性能及物理断电仍分别验收。

## 授予与批次推进模型

一次设备授予在短事务内按以下顺序判定，批次是否已经加载不是资格。

| 已保存事实和当前条件 | 决定 |
| --- | --- |
| 原持有者仍需读取、等待重试或完成实际调用收场 | 恢复原责任，不授予新候选；详情不在缓存也不改变归属。 |
| 有拍摄或必要控制责任，驱动不允许与读取并行 | 按既有让路／兼容性规则推进，不借分批绕过设备占用。 |
| 源内容尚未完成、动作未到时／已取消／终态，或读取资格未成立 | 本候选不获得新读取机会；其他合格候选仍可竞争。 |
| 无持有者，存在多个已合格的设备源候选 | SQLite 按完整业务键选择全体最优项，授予前重新核对；不能选择最先装入内存者。 |
| 主机源拷贝，所属流程具备条件 | 分批推进独立本地副本，不申请相机机会，不借设备未配置阻止取回。 |
| 必要关联、排序依据或查询结果错误／未知 | 保留实际错误并停止依赖该判断的工作，不解释为无候选。 |

发现拥有者使用下表推进。读取结束只说明该次查询范围结束，不能说明业务责任全部结束。

| 查询、通知或会话事件 | 后续推进 |
| --- | --- |
| 有数据且有继续位置 | 处理有限本批，释放详情，明确安排下一批并让出事件循环。 |
| 有效结果为空但仍有继续位置 | 继续扫描；不能结束责任或进入无限等待。 |
| 本范围可靠结束 | 保留仍有业务责任的范围标识，等待对应来源／设备／截止时间／新输入变化。 |
| 来源完成、机会释放、重拷准备、取消生效、设备兼容性改变 | 标记相应范围需要重查；低于旧游标的新合格候选不得遗漏。 |
| 独立提交增加计划 | 按原 `latest_plan_id` 机制合并；初始化和扫描期间也检查，不能等所有旧页完成才感知。 |
| 详情淘汰、连接关闭或运行内工厂重新创建 | 详情可重读，继续责任保留在会话级拥有者；不能把工厂新对象当扫描已完成。 |
| 取消、查询失败或事务结果未知 | 按原取消／错误规则处理，保留实际已开始的读取和保存；不返回伪结束页。 |
| 会话重启 | 丢弃旧内存游标和缓存，从持久化责任重建，先恢复旧机会、身份、进度及已用次数。 |
| 会话准备正常退出 | 原写事务内重查持久化工作，后续批次或新提交仍有责任时继续，不能只看缓存为空。 |

一次扫描固定自己的比较时刻和范围代次，续页原样使用这组依据；时间推进跨过相关截止或可靠变化通知到达时，结束旧代次并重新查询，不能把新时刻与旧游标直接拼接。建议用当前已提交历史尾位置作轻量变化核对的保守依据，并结合既有 `latest_plan_id` 和按范围通知；它只使缓存失效，不授予资格。授予事务始终使用本次实际判断时刻和当前全体候选，旧页不能覆盖后来出现的更早候选。自己处理本批产生变化后也按新范围重查，不保留永久排除低位候选的游标。

重点复核：最优候选尚未装载、SQL 筛选隐藏坏关联、内部与用户读取竞争、初始化期间新提交、游标之后低位候选新获资格、主机源海量独立取回。下列阶段分别验证这些条件。

## 任务一：分页契约、限额和纯规则测试

**消费接口：** `contracts/pages.py::Page`、当前排序语义和配置装配。

**交付接口：** 上述发现范围、游标、候选与会话级拥有者；批次 B 贯通真实运行时。

- [x] 新增 `test_empty_page_with_cursor_continues`、`test_cursor_is_bound_to_scope_and_generation`、`test_eviction_keeps_discovery_responsibility`、`test_new_eligibility_invalidates_old_frontier`。纯规则专项断言继续、重查和范围绑定；真实查询错误另由 outputs 专项验证。
- [x] 配置测试覆盖省略、显式值 `1`、较大正整数、布尔值、0、负数及会话重载；先证明实际装配当前没有该限额消费。正式配置表由 `configuration.md` 定义，生产默认值及加载沿现有 `ConfigDefaults` 组织。
- [x] 先运行专项保留有效失败，再接入数据类型和配置。页字段复用 `Page`，不用独立 `exhausted` 标志或未接入生产的另一套缓存结束规则。
- [x] 工厂共享发现状态，现有待保存实际结果集合仍属于其结果拥有者，不为限制候选缓存而丢弃。缓存不持有 SQL 游标，正常重建不重置尝试额度。
- [x] 单元专项及配置测试通过后再进入真实 SQL 阶段。

## 任务二：全体排序与机会事务的有界 SQL

**消费接口：** 任务一类型与现有拷贝、动作、来源、内部处理、读取流程和机会投影。

**交付接口：** 有界候选页及共享 `first_eligible_read`；原 `grant_read_slot` 行为完整保持。

- [x] 新增 outputs 集成测试 `test_later_registered_earlier_action_wins_without_full_load`、`test_internal_and_delivery_reads_share_global_order`、`test_held_retry_copy_precedes_new_candidates`、`test_missing_candidate_basis_is_not_filtered_away`、`test_device_holder_query_detects_multiple_holders`。将批次设为 `1` 或 `2`，故意让登记 ID／枚举顺序与业务顺序相反。
- [x] 对需要分批恢复筛选的路径，把前几页都设为不合格但后页有最优候选，断言不提前结束或授予错误项。若 SQL 直接完成完整资格及排序，验证未先缓存的最优项能直接被正确选出；不要求人为保留低效扫描。
- [x] 先运行专项，确认当前返回数量或键累计无界。将资格及完整排序下推 SQLite，用有界查询返回本页和全体最优身份；外层发现与事务授予复用同一查询定义，不能分别维护完整规则列表。
- [x] 替换 `_SlotChangeCommand._first_candidate` 的全量行及排序键累计；持有者查询用有限结果同时验证“零、唯一、非法多个”三类。缺失关联／无效排序仍明确报错，不因查询优化变为空集合。
- [x] 新增 `test_product_predecessor_scan_retains_only_current_batch`、`test_guard_independently_rejects_skipped_predecessor`，并保留、复验 `test_product_competition.py::test_predecessor_discovered_after_first_batch_still_blocks`。覆盖计划、动作、组及明确产物来源，未固定选择与已固定选择、取回／清理同时间排序及已授予资格复用；保留查询失败回滚反例。
- [x] 生产资格使用 `_BoundedProductReads` 的限定迭代／查询；`_CurrentProductReads` 在该事件之前的原事务视图独立复核相同范围。新增只读端口覆盖越界、错误范围、数据库读取失败、已验证前序变化及未来提案不能冒充当前行的反例。静态验证保留 `_ProductReads` 与完整范围依据，历史恢复继续使用原边界事实。
- [x] 保留 `integration/outputs/test_read_slot.py` 的原资格、源写完、时间、重试、取消、释放实际读取及操作键重送测试，在 `test_read_discovery.py` 扩展排序、坏关联和容量反例。固定候选集合、源清理保护及取消终态保持。
- [x] 对实际查询保存 `EXPLAIN QUERY PLAN` 和所返回行数证据。SQL 内部仍可能处理大量行，记录开发环境边界；批次限制不声称固定延迟或 ARM 性能已通过。如确需新增索引，按现有显式格式／初始化边界处理，不能在日常 `run` 隐式升级旧库。

## 任务三：实际取回、内部读取与本地读取接线

**消费接口：** 任务二查询及现有设备工作、取回和媒体读取流程。

**交付接口：** 默认路径按有限批次推进，尚未处理的范围明确继续；机会由原事务确认。

- [x] 新增 `test_default_obtain_reaches_candidates_beyond_first_batch`、`test_default_binding_failures_are_paged`；既有 `test_distribution.py::test_installed_hasselblad_sample_uses_actual_unique_capability` 采用主机源 B=1。从 `build_runtime`／`execute_command` 的默认装配启动真实计划和文件，安装版另核对主机 RAW／JPEG 的两轮独立取回。
- [x] 把 `plan_device_work` 的四组事实按设备／工作范围查询并有界交付；纯 `decide_device_work` 可保留。绑定失败检查与主机源读取使用同样的范围／继续模型，不累计全部 ID 元组或设备桶。
- [x] 把 `_start_due`、`_running_actions`、逐动作待处理项、发布项及 `_settle_failed_reads` 的无界发现分批；同时审计默认 capture／residual 流程进入内部读取前的发现，限于本次读取责任链，不做无关调度器重写。
- [x] 新增 `test_obtain_summary_folds_many_items_without_full_materialization`，按独立业务分类比较分批汇总与预期终态；处理 `load_obtain_facts` 的全量 `sources/items` 构造，原选择集合、结果和原子结束保持。覆盖来源未完成、混合成功／失败、无产物、全部交付完成及取消后的实际残留。
- [x] 每个推进轮次处理有限本批后交回控制，后续批次通过拥有者推进标志与既有唤醒安排；不要在一轮中同步循环到所有页结束。设备机会释放及内部输入建档后重新查全体最优候选。
- [x] 内部检查和修复继续使用统一 `grant_read_slot`；自动预览保持独立取回责任。同一源多次取回分别建档和交付，不以缓存命中复用副本。主机 RAW／JPEG 正式源分别保留，每次生成独立副本。
- [x] 批次限额从配置经 lifecycle、取回工厂及相关 capture 工厂传递，记录实际消费者。已有 `discover_work` 可复用其有限查询思想，但不把调用辅助函数本身当完成条件。
- [x] 分别执行新增 bootstrap 专项、outputs 及 capture 目录，确认重试保留、预览让路、内部读入与本地单次预算未回归。

## 任务四：动态变化、取消、重启及容量组合

**消费接口：** 会话级发现拥有者、原通知与接纳协议、实际读取结果拥有者。

**交付接口：** 范围变化可重查，未完成页不丢责任，Python 保留量随 B 而非总任务数增长。

- [x] 新增 `test_submit_during_initial_discovery_is_not_lost`、`test_newly_eligible_low_key_is_rediscovered`、`test_evicted_read_resumes_after_restart`、`test_cancel_during_discovery_preserves_active_read_settlement`、`test_query_failure_is_not_exhaustion`。分别在分页继续、查空到等待、机会释放和初始化时设置同步点。
- [x] 接入文件拷贝专题列出的全部变化入口：新提交、来源完成、建档、机会释放、读取重试、重拷准备、兼容性变化、取消生效及实际收场。范围代次变化后重建继续位置，授予前仍在事务内验证；通知不能代替数据库事实。
- [x] 查询结束不等于动作结束。最终退出事务重新检查未处理页及实际业务责任；新提交或新责任存在时不能仅因缓存为空关闭接纳。读错误保存原错误，等待失败不返回空结束页。
- [x] 用真实默认流程对比多组总候选数量及 B，记录每页返回量、同时保留详情和有限继续状态；查询返回不超过 B，详情不随累计页数增长。单个设备持有者和最优身份查询按其常数界核对，不能只搜索是否存在 `fetchall` 字符串。
- [x] 大量终态历史加入后，发现仍限定未完成责任范围；等待重试和未来动作不会引发无进展忙循环。开发环境的查询及内存结果注明负载，目标性能不在此宣称通过。
- [x] 复验主机源两次独立取回、不同设备、同一产物旧读取保护、预览让路、内部检查／修复及取消后机会释放，分别执行 bootstrap、outputs、capture、scheduling、session、history 目录。

## 验收命令与收口

从仓库根执行；新增文件和配置已接入生产装配：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/outputs/test_read_discovery.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/outputs/test_read_discovery.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_read_discovery_default.py -q
```

完整门禁分别执行 `unit`、`integration/outputs`、`integration/capture`、`integration/scheduling`、`integration/session`、`integration/bootstrap`、`integration/history`、`integration/persistence`、`integration/contracts`，以及配置消费检查 `apps/camctl/.venv311/bin/python scripts/check-config-consumption.py`。新增只读端口须保持模块依赖和外部调用边界；配置脚本现有范围不能替代新增限额的真实消费者组合验证。

- [x] 沿普通取回、自动预览、内部检查、修复输入、重试／重拷及主机源逐入口审计有界发现、全体排序、旧机会恢复与继续推进；所有已知无界入口有明确处置。
- [x] 记录红绿、查询及容量证据、环境、负载、命令和退出码；独立复核后更新路线图 SW-06。只有实际 W-11 条件全部闭合才更新相应验收映射，不能因辅助函数通过升级结论。
- [x] 形成独立修复提交；对 SW-01—SW-06 的共同会话、日志、取消及恢复接缝进行最终组合复验，保留各专项实际证据与目标环境未核验边界。

2026-10-15：任务一至四已完成。Root 已逐文件核对行为契约、生产者、消费者和测试；GPT6.1-Sol 完成一次完整独立复核，Root 以真实 SQL 和实际控制调用的反例修复内部持有者归属及拍摄分页顺序。所有最终门禁通过，查询容量、弱引用释放、动态变化、取消、重启和独立守卫证据见[SW-06 验证记录](../../camctl/verification.md#sw-06-默认读取分批发现验证2026-10-15)。本次为开发环境软件验证，目标 ARM 性能与真实相机范围分别维护。
