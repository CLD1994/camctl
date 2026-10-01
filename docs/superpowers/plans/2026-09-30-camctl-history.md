# camctl 历史事件与状态查询模块实施计划

> 供执行 Agent 使用：实施时按 `superpowers:executing-plans` 逐任务执行；明确选用 subagent（子 Agent）执行方式时，可使用 `superpowers:subagent-driven-development`。复选框只跟踪实际实施，不因计划已经编写而勾选。

**目标：** 实现可逆事件、完整目录、同一历史边界查询和有限快照维护，使过去状态及报告能够可靠重建。

**组织建议：** 事件解释与状态应用保持纯函数，SQL 目录和快照保存由窄仓储完成；历史读取返回有界、独立的数据，报告进程在事务外恢复与编码。所有内部类型、签名、文件和提交拆分均为建议，行为契约及项目规则是硬性要求。

**技术基础：** 使用既有事件转换、枚举及报告依赖登记，sqlite3 短事务和精确 JSON；不自行维护第二套事件规则。版本与依赖以组件锁文件及现有权威登记为准。

**设计依据：** [事件转换](../../camctl/database/event-transitions.md)、[历史格式](../../camctl/database/history-formats.md)、[历史查询](../../camctl/historical-state-query.md)、[快照](../../camctl/history-snapshots.md)、[一致性验收](../../camctl/database/consistency-verification.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 行为契约与实施边界

不可变事件是权威，历史回放不访问设备、不重新决策、不分配新身份。历史对象目录与实际公开变化目录职责不同；具名业务校验、跨表归属和整笔事务完整性不能由行权限或 SQL 外键替代。报告统一恢复到完整 H，每批当前投影与自身 C 同时读取。文件有独立历史，父对象快照只保存自身成员；快照范围从统一对象登记取得。

统一的精确数字、单一登记来源、完整事务、取消后接手、测试分类及执行命令采用[共享实施契约](../../camctl/module-contracts.md#实施范围与统一执行方式)。本计划只覆盖第一版已经定义的故障范围；软件集成不连接真实设备。

## 接口与数据建议

事件及对象事实的精确结构采用版本 1 格式。生产验证器只接受已经实现的类型、版本和分支；未知组合明确报错，不能跳过。

| 类型 | 字段或含义 |
| --- | --- |
| `EventEnvelope / EventRule` | 事务、事件、对象身份、版本、分支、前后值和引用；EventRule 来自统一登记。 |
| `EntityImage / RestoreSeed` | 对象自身字段及自身成员、存在性、完整边界与累计计数；RestoreSeed 为初始、可靠快照或绑定 C 的当前投影。 |
| `EntityReadRequest / EntityBatch` | 数据库身份、固定 H、对象范围、游标及批量参数；结果含本批种子及必要事件，游标关闭后才返回。 |
| `FileHistoryRequest / FileHistoryPage` | 所属活动/拷贝/产物等限定范围、H、固定候选上界和最后文件 ID；筛选有效成员后仍保留候选继续位置。 |
| `SnapshotCandidate / PreparedSnapshot` | 对象类型及身份、原 S、S 处次数、格式和完整自身内容；维护状态只属于本次会话。 |

恢复路径按对象的可靠状态分类，不能把未知折叠为未出生。

| H 与对象事实 | 查询结果或路径 |
| --- | --- |
| 可靠确认对象在 H 尚未出生 | 返回明确不存在，不补造空对象。 |
| 有可靠当前投影及完整 C≥H | 可以从 C 逆向恢复至 H。 |
| 有适用可靠快照 | 可以沿登记事件从快照恢复至 H。 |
| 没有适用快照但权威历史完整 | 从初始状态按范围正向回放。 |
| 存在记录但必要事件、引用或成员不完整 | 状态库解释错误，不选择看似更方便的空结果。 |

每条路径的最终结果必须覆盖对象自身完整成员；独立文件和公开关联在同 H 另查。实际路径按已有成本规则选择，不以路径相等代替独立正确性预期。

## 文件职责建议

| 预计生产文件 | 责任 |
| --- | --- |
| `apps/camctl/src/camctl/history/events.py` | 版本化事件解释及前后状态校验。 |
| `apps/camctl/src/camctl/history/validators.py` | 具名业务校验接入与整笔事件审计。 |
| `apps/camctl/src/camctl/history/changes.py` | 对象归属、公开变化和维护计数。 |
| `apps/camctl/src/camctl/history/replay.py` | 正逆应用与恢复路径。 |
| `apps/camctl/src/camctl/history/queries.py` | 固定 H 批次与候选继续规则。 |
| `apps/camctl/src/camctl/history/snapshots.py` | 候选、准备、保存和会话停用。 |
| `apps/camctl/src/camctl/persistence/repositories/history.py` | 短事务读取、目录重建及快照事务。 |

涉及 SQLite 的用例通过窄仓储接口接入；事件、投影、目录和维护进度在同一完整事务内保存。测试文件在对应任务列出；尚不存在的路径是实施位置建议。

## 任务依赖与交付

| 前置或组合计划 | 需要的交付 |
| --- | --- |
| [共享类型](2026-09-30-camctl-contracts.md) | K1—K4 提供精确值、边界及纯公开投影。 |
| [持久化](2026-09-30-camctl-persistence.md) | H1/H2 与 P3 同期建立；H4 之后使用 P5。 |
| [报告](2026-09-30-camctl-reporting.md) | R3 消费 H4/H5；H7 核验同报告字节。 |
| [业务模块](2026-09-30-camctl-implementation-roadmap.md#模块计划与任务入口) | 每个新原子操作同步接入事件校验、恢复与字段依赖。 |

H1—H3 是受理事务的基础；H4 在首条报告链前完成。H5/H6 可在固定边界接口稳定后实施，其全部对象门禁在采集、取回、清理及取消接入后完成。H7 是全部事件覆盖与规模门禁。

## 审阅重点

以下五项均落实到具体失败用例；它们与任务内的其他用例共同约束实施。

| 易遗漏的条件及预期 | 负责的任务和用例 |
| --- | --- |
| 目录重建用事件发生时的关系。 | H2，`test_rebuild_uses_historical_relationship` |
| 逆向应用整笔事务后才得到 H。 | H3，`test_reverse_stops_at_complete_boundary` |
| 当前关系变化不改变历史文件集合。 | H5，`test_old_files_use_same_h` |
| 零有效文件页仍继续候选。 | H5，`test_filtered_empty_page_continues` |
| 准备期间新增变化不被快照吞掉。 | H6，`test_snapshot_preserves_later_changes` |

## 实施任务

### H1 事件版本与具名校验消费者

**预计文件：** `apps/camctl/src/camctl/history/events.py`、`apps/camctl/src/camctl/history/validators.py`；测试为 `apps/camctl/tests/unit/history/test_events.py` 和 `apps/camctl/tests/integration/history/test_events.py`。

**接口与依赖：** 提供 `validate_event(event: EventEnvelope, context: EventContext, registry: EventRegistry) -> ValidatedEvent`；EventContext 含事务范围、可靠前状态、实际关系和已取得证据，注册表由包资源生成。前置交付：K1—K3、B1；先实现首次受理及报告所需分支。

- [x] 编写失败用例。建立 `test_unimplemented_validator_rejects_write`，登记存在但具名校验未接入，断言拒绝；前后列越权、未知版本、错误引用 E/L、错误对象归属、缺少共同事件分别失败。`assert validated.references == expected_exact_references`，期望独立从事务范围构造。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/history/test_events.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。读取 event-transitions 的列权限、状态模型和具名校验，再执行对应业务纯校验；未知类型、版本、分支或未实现校验都不能默认接受。各业务增加事件时增加本任务的相应实现和反例。
- [x] 再运行上述命令，要求全部 PASS，并核对 每个已支持分支的业务不变量有明确校验入口。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/history/test_events.py -q`，P3 实际写事务拒绝缺项及跨表错误，即使行内 SQL 接受也不能提交。
- [ ] 审阅实际接口、状态分区及失败路径，检查 事件应用、修复、导入和目录重建是否存在绕过校验的入口；记录门禁证据，建议以“feat: 实现事件契约的生产校验”形成独立提交。必要字段真实变化、多状态转换及证据成员校验仍按[结构余项](2026-10-02-camctl-history-read-review.md#事件结构余项的实施顺序)完成后验收。

### H2 历史归属、公开变化与计数

**预计文件：** `apps/camctl/src/camctl/history/changes.py`；测试为 `apps/camctl/tests/unit/history/test_changes.py` 和 `apps/camctl/tests/integration/history/test_changes.py`。

**接口与依赖：** 提供 `derive_changes(events: tuple[ValidatedEvent, ...], before: StateSlice, after: StateSlice) -> ChangeSet`；StateSlice 含同事务前后事实及关系，不含当前设备信息。前置交付：H1、K4。

- [x] 编写失败用例。建立 `test_rebuild_uses_historical_relationship`，文件后来更改关系，重建旧事件仍关联旧对象；内部尝试变化不生成业务水位，公开交付变化补齐正确动作和计划。`assert changed_entities == expected_entities`；同一对象一笔事务多处变化按规格计数。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/history/test_changes.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。从事件版本和当时关系形成历史对象目录；调用纯公开投影判断实际报告变化，维护对象计数及快照进度。用同一规则正常写入和重建目录，重建核对原业务序号。
- [x] 再运行上述命令，要求全部 PASS，并核对 两类目录、父子补齐及对象计数具有独立预期。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/history/test_changes.py -q`，真实事务共同保存事件、投影、目录和进度，并核验 J 系列引用规则。
- [x] 审阅实际接口、状态分区及失败路径，检查 父对象依赖变化是否因自身未改而漏报，是否用当前关系重新解释旧历史；记录门禁证据，建议以“feat: 实现历史目录与公开变化派生”形成独立提交。

### H3 正逆应用与独立恢复预期

**预计文件：** `apps/camctl/src/camctl/history/replay.py`；测试为 `apps/camctl/tests/unit/history/test_replay.py`。

**接口与依赖：** 提供 `apply_forward(image: EntityImage, event: ValidatedEvent) -> EntityImage`、`apply_reverse(image: EntityImage, event: ValidatedEvent) -> EntityImage`、`restore(seed: RestoreSeed, events: Iterable[ValidatedEvent], target: HistoryBoundary) -> EntityImage`。前置交付：H1/H2、K3。

- [x] 编写失败用例。建立 `test_reverse_stops_at_complete_boundary`，一笔事务多个成员变化，`assert restored == independent_image_at_h`；覆盖创建、修改、成员存在性、精确数值、引用与同事务反向顺序。缺少中间事件拒绝；外部端口替身断言没有设备或文件调用。
- [x] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/history/test_replay.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [x] 实施本任务。正向按原事件顺序应用，逆向按相反顺序恢复前值；核对前后存在性和完整范围，只使用原事实，不重新执行预算、时钟或副作用判断。
- [x] 再运行上述命令，要求全部 PASS，并核对 初始回放、快照正向和投影逆向分别符合独立预期。
- [x] 审阅实际接口、状态分区及失败路径，检查 每种新增事件是否只有正向消费者，逆向是否遗漏自身子记录；记录门禁证据，建议以“feat: 实现可逆历史状态恢复”形成独立提交。

### H4 短读事务与固定 H 分批查询

**预计文件：** `apps/camctl/src/camctl/history/queries.py`、`apps/camctl/src/camctl/persistence/repositories/history.py`；测试为 `apps/camctl/tests/integration/history/test_queries.py`。

**接口与依赖：** 提供 `read_entities(request: EntityReadRequest) -> EntityBatch`、`read_events(request: EventReadRequest) -> Page[EventEnvelope, int]`；EventReadRequest 含对象、恢复方向、完整固定范围、最后事件位置和上限。分页结果遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)。前置交付：P5、H3。

- [ ] 编写失败用例。在 `test_each_projection_batch_binds_its_c` 中批间插入新提交，`assert restore(batch, h) == expected_at_h`；游标和事务在返回前关闭。长对象、事件跨批、父对象跨批补齐、不同批量和空范围分别验证。读取错误不能返回空 Page。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/history/test_queries.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。在同一短读事务中取得投影及 C，转换为独立数据并结束游标和读事务后返回；实体、事件批量分别使用本次配置，按稳定 ID/事件位置续读。仅在可靠确认范围结束时返回空继续位置，最后一批仍交付其中的数据。
- [ ] 再运行上述命令，要求全部 PASS，并核对 所有实体恢复到同 H，长期读事务不会陪同 JSON 编码。
- [ ] 审阅实际接口、状态分区及失败路径，检查 读取接口是否返回活动 cursor、整棵子树或混合不同边界；记录门禁证据，建议以“feat: 实现固定历史边界分批查询”形成独立提交。

分项状态：

- [x] [全局事件分页基础](2026-10-02-camctl-history-read-review.md)：核实完整 H、事务分组、严格范围和连续事件位置；正反向分页在返回前释放读资源，共用版本、分支及行权限校验。
- [ ] 对象读取请求绑定数据库身份、对象、H、方向及恢复范围；当前投影与 C 联合读取，并组合正逆恢复。
- [ ] 固定 H 的报告入选范围、关联候选优化、对象自身成员分批及父子补齐。
- [ ] 事件结构余项：必要字段真实变化、空更新、多状态转换与证据成员校验；详见上述审查计划。

### H5 独立文件历史与关联候选

**预计文件：** `apps/camctl/src/camctl/history/queries.py`、`apps/camctl/src/camctl/persistence/repositories/history.py`；测试为 `apps/camctl/tests/integration/history/test_file_history.py`。

**接口与依赖：** 提供 `read_files_at_h(request: FileHistoryRequest) -> FileHistoryPage`；分页部分采用 K3 提供的 Page，并遵守[分页结果契约](../../camctl/module-contracts.md#分页结果契约)。前置交付：H4、H1 的独立文件及关系事件；X1/X4 接入后补齐实际文件消费者验证。

- [ ] 编写失败用例。建立 `test_old_files_use_same_h`，H 后文件清理、归属补齐或关系变化，`assert file_ids_at_h == expected_old_ids`；建立 `test_filtered_empty_page_continues`，首页候选在 H 均无效而后页有效，断言首页有继续位置且结束属性为 False，后页仍返回并被消费者处理。分别覆盖末页有数据、末页为空、固定上界、读取失败及 device_file/intermediate_file 独立恢复。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/history/test_file_history.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。按规格 SQL 从限定历史目录获得候选，绑定最大候选 ID，逐候选恢复同 H 后筛选；继续位置取已检查候选，不取最后有效结果。
- [ ] 再运行上述命令，要求全部 PASS，并核对 H-01—H-06 全部分区落实，当前清理状态不替代旧事实。
- [ ] 审阅实际接口、状态分区及失败路径，检查 产物、取回、交付及内部处理的全部关联文件入口；记录门禁证据，建议以“feat: 实现关联文件的同边界历史查询”形成独立提交。

### H6 快照维护与会话退出

**预计文件：** `apps/camctl/src/camctl/history/snapshots.py`、`apps/camctl/src/camctl/persistence/repositories/history.py`；测试为 `apps/camctl/tests/unit/history/test_snapshots.py` 和 `apps/camctl/tests/integration/history/test_snapshots.py`。

**接口与依赖：** 提供 `prepare_snapshot(seed: EntityImage) -> PreparedSnapshot`、异步 `maintain_snapshots(context: MaintenanceContext) -> MaintenanceResult`；context 含本次阈值、批量、会话状态和窄仓储。前置交付：H4、P2/P3、S5。

- [ ] 编写失败用例。建立 `test_snapshot_preserves_later_changes`，S 处次数 5，保存前最新次数 8，`assert remaining_changes == 3`；入队前 9 秒超时只停用本次维护，已入队或实际 DB 错误不得用此降级。新提交、空位和迟到通知不能重新启用；退出按准备、入队前、排队、已开始分别处理。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/unit/history/test_snapshots.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。从统一对象登记取得资格及完整自身成员，保存原 S 和原计数，再按写事务最新计数更新 Δ。业务优先，批量后让出；停用状态只在会话内保存，维护不延长会话或产生 needs_run。
- [ ] 再运行上述命令，要求全部 PASS，并核对 快照未提交不能查询，S 后变化与原历史始终保留。

随后运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/history/test_snapshots.py -q`，真实线程和 SQLite 组合并交错准备与业务写入，覆盖全部快照对象及同一会话停用。
- [ ] 审阅实际接口、状态分区及失败路径，检查 文件自身成员、动作处理记录及报告依赖是否被错误复制到父快照或漏掉；记录门禁证据，建议以“feat: 实现有限历史快照维护”形成独立提交。

### H7 全部事件覆盖与查询规模

**预计文件：** `apps/camctl/src/camctl/history/queries.py`、`apps/camctl/src/camctl/history/replay.py`；测试为 `apps/camctl/tests/integration/history/test_complete_history.py`。

**接口与依赖：** 使用 H1—H6 与 R3 的真实接口；建立登记分支到具体用例的证据映射。前置交付：H1—H6、C1—C8、X1—X11、N1—N5、R1—R8 的生产能力；不依赖消费者的最终验收声明。

- [ ] 编写失败用例。对每种生产事件建立 `test_every_event_matches_independent_history`，`assert restored == independently_expected_image`；三条路径相等之外仍检查每个自身成员及引用。改变批次、缓存与方向后 `assert bytes_a == bytes_b`。代表性数据 ANALYZE 后核对首批/续读实际索引、去重、稀疏变化及候选峰值。
- [ ] 运行 `uv run --project apps/camctl --group test pytest apps/camctl/tests/integration/history/test_complete_history.py -q`，确认 FAIL 来自本任务的目标行为缺失；依赖缺失或测试准备错误不能算有效失败。
- [ ] 实施本任务。补齐全部事件和文件生命周期的恢复反例、成员完整性与实际查询测量；资源测量注明开发环境、数据规模和主/报告进程合计，不写成目标性能承诺。
- [ ] 再运行上述命令，要求全部 PASS，并核对 验收 52—68、H、P、J 相关条目逐项有实际入口。
- [ ] 审阅实际接口、状态分区及失败路径，检查 所有读取分支、缓存依赖及未定义历史版本的处理；记录门禁证据，建议以“test: 验证全部历史恢复与分页规模”形成独立提交。

## 模块完成门禁

H1/H2 每个已实现事件均有具名校验及原子目录；H3—H6 全部对象与文件恢复符合独立预期；H7 给出完整验收映射与实际规模证据。报告重建使用同 H，回放不会执行外部副作用。

完成时核对本计划所有任务、引用的正式验收条目及消费者组合证据。已有测试全部通过仍不能替代遗漏需求检查；尚未核验的设备和部署前提单独记录。
