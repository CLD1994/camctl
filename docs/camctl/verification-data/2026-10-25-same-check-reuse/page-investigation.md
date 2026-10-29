# 同一次结果页关系检查的局部优化候选

基线为 `7644b7c6be42a995f3eab629721296e01309d826`。调查只读取源码和契约，并静态复核父代理外部原型的 page 部分；没有修改生产代码、测试或配置，没有运行测试、性能测量。当前候选尚未成为完成验收的实现。

最有把握的首批是 A1—A4：一次建立身份索引、仅复用文件创建事件 ID 标量、在首遍形成期望 CREATE 顺序、移除同一解码对象紧接着的第二次结构校验。它们保留所有真实来源的相互核对，不需要扩大缓存，也不需要改变业务设计。不能依据计数直接承诺耗时或内存下降。

## 必须保持的证明边界

`docs/camctl/persistence-runtime.md:302-310` 要求提案和独立守卫分别建立证明，缓存最多保存三份原 SQLite 行，命中仍解码和校验页结构；当前资格继续实际读取。`docs/camctl/database/history-formats.md:273-279,313` 要求完整 CREATE／APPEND／INDEX 事务、原页差集和独立持久关系双向一致，不能从页保存推断文件归属、完成或配对。所有候选只活在一次 `_verify_page_relations` 栈帧内。

函数先从实际尝试/流程取得拥有者，再读动作、完整原事务、文件固定事实与持久关系。独立证据至少包括：APPEND 的原观察及 `file_ids`、CREATE 的原文件行及事件身份、当前文件固定身份/创建事件、INDEX 的历史正文、`result_page_files` 的实际保存行。局部复用只合并同一来源重复读取/构造，不把这些证据折叠成一个来源。

## 首批候选

### A1：每页只建立一次身份索引

位置：`apps/camctl/src/camctl/persistence/repositories/result_pages.py:350-351,378`。原代码每个 CREATE 都执行 `dict(file_ids).get(identity)`。在循环前建立一次 `file_id_by_identity=dict(file_ids)`，循环继续从原 CREATE 严格解析的身份查找文件 ID，并照旧比较 `row.row_id`、原观察与完整预期行。`page_entries` 已经只构造一次，无须再做缓存。

设差集文件数为 n、该事务首次创建数为 c，规范页满足 `0≤c≤n≤128`。字典构造从 c 次降为至多一次，插入项从 n×c 降为 n，容量上界为 16384→128。原 CREATE 身份不属于差集、文件 ID 不符、坏身份 JSON 等分区继续原样拒绝。最窄真实见证为新旧混合页、128 文件页及损坏 CREATE 的身份/row_id。

### A2：第一次读取后只保留 created_event_id

位置：同文件 `:353-360,388-390`。第一次 `row_facts` 已验证文件存在、完整身份正确并判断创建事件所在事务。记录 `created_event_by_file[file_id]=facts["created_event_id"]`，后面仍比较它与原 CREATE 的 `event_id`，仅删除第二次 `row_facts`。不保留完整 facts 字典，不保存可变完成/归属字段。

这不是省掉一个独立来源：两次读的是同一连接同一文件的固定创建元数据，第二次期间没有生产写入。`capture.py:5821-5841` 的 `file_discovery_row` 不接收连接，只调用 `row_change` 构造原文件行；`transaction.py:1066-1073` 的 `row_change` 不写库；`capture/files.py:119-128` 的申请构造器只校验。因此构造预期行不会改变先前读到的 facts。

包括动作读取，调用次数从 `1+n+c` 降为 `1+n`，每次最多少128次。`transaction.py:1126-1129` 已把 `row_facts` 接到事务读取复用；`read_reuse.py:89-92` 命中后仍做完整 `deepcopy`。因此先省的是函数/键处理与深复制；只有未命中或未启用范围时才同时减少 SQL 和结构化列恢复。诊断必须把 wrapper、loader 和 SQL 分开计数。保留文件缺失、完整身份不符、未来创建事件、创建事件身份不同及 CREATE 覆盖错误的拒绝。

### A3：在首遍形成顺序，去掉平方扫描与纯复制

位置：同文件 `:352-363,392-397`。`expected_created` 本来就是从 `file_ids` 按原序筛选得到。可以在第一遍 `for pair in file_ids` 中，同步把三元组加入 `expected_created`，把已有的 `pair` 引用加入 `expected_create_order`；后面直接比较 `actual_created != expected_created`，保留后续具名顺序比较。无需另建 `expected_created_ids`，也无需在比较右侧重建相同三元组列表。

这样没有 `any` 的 O(n×c) 搜索，期望顺序只随第一遍形成；最多避免两轮各128个三元组复制。原代码在唯一、顺序一致的子序列下，匹配测试最多8256次。父代理原型的直接投影 `[(identity,file_id) for ...]` 语义也等价，但会新建 c 个二元组；首遍 append 原 pair 能进一步避免该构造。缺/多/顺序错误和旧文件被错误当作新 CREATE 仍通过原完整比较拒绝。

### A4：局部解码事件只做一次完整结构校验

位置：同文件 `:336,366,413,434`。`history/decoding.py:35-62` 先严格解析完整原始 JSON，再于 `:58` 执行 `validate_event_structure`。本函数 CREATE 来自 `:336`，INDEX 来自 `:413`，到后续重复校验前没有修改事件、没有 await，也没有把对象交给可修改它们的协作者。可以只删 `:366` 与 `:434`，仍保留每次 `decode_event_row` 内的全部结构校验、CREATE/INDEX 的业务关系条件及持久行比较。

`EventEnvelope`/`RowImage` 是 frozen 数据类，但内部 Mapping 不是深冻结；等价依据是上述真实局部数据流没有修改，而非仅靠 frozen。`validate_event_structure` 的对象身份、版本/分支、枚举、evidence、行权限、重复行和列集合检查全部仍在原解码入口执行。每次完整规范页可少 `c+I` 次结构校验，最多129次；涉及重复行结构访问最多 `c+n≤256`。其中 I 在非空差集时为1，否则为0。该项不会减少 `history_decode` 次数。

最窄有意义验证必须把真实历史 CREATE 和 INDEX 分别改坏版本、分支、枚举、行权限、重复行和 evidence，再从真实公开页读取入口确认拒绝。只跑正常200文件样本，不能证明这些损坏分区保留。

## 第二批与较低优先候选

A5 位于 `:401-413`：完整事务在 `:333-342` 已经按事务读取、逐事件解码并核连续性。可从该 `transaction_events` 局部序列筛选 APPEND 与 INDEX，继续在原位置检查 APPEND 唯一、INDEX 数量/位置/时刻/evidence；保留 `result_page_files` 的独立实际 SQL 与所有行的对照。每次最多少两条 history_events 筛选 SQL，非空规范页少一次 INDEX decode。该方案不跨函数保存事件，也不删除任何事件的首次结构校验，但需要单独验证多个 APPEND/INDEX、非规范尾事件、完整事务越界和零差集分区。父代理首批原型未包含这一项，不应把其理论节省计入首批结果。

A6 位于 `:355,384-385`：同一个 CREATE 的 canonical 身份编码由 `file_identity_key` 构造两次。首遍验证后可以只保留已核对的 key 文本，在权威 `file_discovery_row` 构造中复用，最多少 c 次编码；但会额外保留最多128个完整 key 引用，须比较峰值，优先级较低。原 CREATE 的 `identity_key` 是外层事件 JSON 中的另一份 JSON 文本，外层解码没有解析其内部内容，`:374` 的严格解析应继续保留。

`file_discovery_row` 与 `FileObservationSave` 每个 CREATE 只构造一次，并承担完整默认事实及输入校验；不建议手写第二份默认列清单来省它们。`SavedResultPage.file_ids` 已为 tuple，`:351` 和 `:422` 的 `tuple(...)` 通常返回同一个对象，删除第二次赋值并不等于省去一次128项复制。`:398-400` 前缀再扫描已被 `:367` 的逐项约束覆盖，可单独简化，但不是目前有证据的主要成本。`_family_fields` 在 Action6 分支逐项构造 suffix 集合可继续剖析，保持权威 profile，当前没有成本证据支持把它并入首批。

## 最窄验证及计量口径

建议只在真实持久化集成范围做以下首批见证，不为私有函数写一套复制实现的调用图测试：

- `test_segment_result_page_inputs.py::test_page_difference_indexes_reused_and_new_files_together`；补 n>0、c=0 的全旧文件页。
- `::test_128_file_page_keeps_existing_page_bound_and_index_event_bound` 与 `::test_zero_difference_is_not_an_empty_or_final_device_page`。
- `::test_page_read_rejects_corrupt_relation_without_rebuilding` 与 `::test_fix1_i3_fresh_original_key_rejects_canonical_group_corruption`。
- 最窄补充两条 CREATE 顺序反转、缺/多 CREATE、文件创建事件身份错误，以及前述 CREATE/INDEX 结构损坏分区。
- `test_page_read_cache.py::test_new_page_is_rechecked_until_its_complete_transaction_commits` 与 `::test_committed_page_is_reused_only_inside_one_read_transaction`，确认未延长证明生命周期。

上述文件均位于 `apps/camctl/tests/integration/persistence/`。执行时遵守该组件指南：Python3.11、独立源码/资源写入范围、同工作区集成 pytest 串行，记录命令、源码身份、日志和退出码。本调查没有执行这些测试。故障注入应依照业务事实/边界描述，不要求删除重复读取后仍恰好在“第X条SQL”故障。

父代理提供的 N200/B4 背景是无内存跟踪 write9.292s、原页578次调用（113次完整证明与465次原行命中后解码）、history_decode12875次、关系函数内部row_facts24001次，本调查未重跑确认。计数本身不能算出实际 c 的总和，不能直接给提速百分比。若完整函数调用数为P，首批减少 row_facts 的上界为128P，减少重复结构校验上界为129P；P=113时的14464/14577只是容量松上界。465次原行命中后的解码完全不在本函数首批候选覆盖内。

性能诊断适合固定同一200输入/同一保存申请和时刻、每档恢复数据库与WAL、关闭上一档连接，原版前后夹住候选；分别记录 write、SQL、history_decode、结构校验、row_facts wrapper/loader、返回结果及历史摘要。无 tracemalloc 的时间对照与单独峰值测量分开。外部原型只帮助判断候选是否值得实施；它不能替代独立守卫、损坏历史、未知提交和跨事务范围的实现验收。
