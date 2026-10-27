# Hasselblad 无人值守可用性恢复实施计划

## 目标、依据与实施边界

本计划把[未知活动后的无人值守恢复规格](../../camctl/hasselblad-availability-recovery.md)接入正常拍摄、DEVICE 文件获取及重启恢复。旧拍摄失败后，后续有效工作可以在原本地调用收场、固定机身身份核对和新的 doctor 检查通过后继续使用相机；旧拍摄结果、工作文件及报告中的结束未知事实保持。

计划以 `831e17e0` 的正式规格和代码为起点。实施时先核对实际集成基线；后续代码已经变化时，沿实际生产调用关系调整修改位置。规格的行为、项目规则和 HA-01—HA-12 验收是硬性要求；下文建议的模块、新文件位置、内部接口和提交拆分可以调整，不另行固定一种类层次或仓储划分。

当前是开发阶段，直接演进当前数据库、事件和包资源。新环境通过正式 `init` 创建完整结构，不增加旧程序读写、旧事件解释或旧格式运行模式。本能力不包含全仓兼容代码清理，也不包含现场状态转换。已有现场数据库、日志、工作文件、正式产物、交付、客户端状态和待执行计划均保留；它们的部署处理由 H6 单独交接。

本轮按用户要求不新增或运行集成测试。实施时使用数量适当、运行高效的隔离单元测试验证可独立判断的行为；实现后由用户进行人工验证。下文原有集成和安装版验收条件保留为尚未取得的证据，不将单元测试结果当作这些条件已通过，也不因此宣称 H1—H6 已全部完成。

下列责任专题共同约束实施。本文编排任务、依赖和验收，不复制各专题的完整状态、错误码或编号清单。

| 责任 | 实施前阅读与实现时核对的权威来源 |
| --- | --- |
| 本能力的全部状态与决策 | [恢复规格](../../camctl/hasselblad-availability-recovery.md)，尤其身份、检查目标、共享预算、检查等待与拍摄优先级、原子保存和 HA 验收。 |
| 原拍摄、准备与驱动效果 | [相机能力契约](../../architecture/camera-capabilities.md)、[相机恢复](../../architecture/camera-recovery.md)、[Hasselblad 驱动](../../camctl/hasselblad-driver.md)、[工具契约](../../hardware/hasselblad/hb_cli.md)、[拍摄准备规格](../specs/2026-10-25-capture-preparation-design.md)。 |
| 普通调度、配置与绑定 | [动作调度](../../architecture/scheduling-execution.md)、[本地配置](../../architecture/configuration.md)、[操作字段](../../camctl/database/operation-fields.md)、[等待责任](../../camctl/database/waiting.md)。 |
| 文件来源、读取与取消 | [文件拷贝](../../architecture/file-copy.md)、[取回](../../architecture/obtaining-outputs.md)、[产物责任](../../architecture/outputs.md)、[任务取消](../../architecture/task-cancellation.md)。 |
| 当前结构、历史与报告 | [公共存储规则](../../camctl/database/common.md)、[事务](../../camctl/database/transactions.md)、[历史格式](../../camctl/database/history-formats.md)、[数据库一致性验收](../../camctl/database/consistency-verification.md)、[设备执行报告](../../architecture/report-format.md#动作结束后设备是否仍在执行)。 |
| 错误、部署与交付 | [全局错误诊断](../../architecture/logging.md#全局错误诊断责任)、[公共协议](../../../protocol/README.md)、[显式初始化](../../architecture/initialization.md)、[部署要求](../../camctl/implementation.md#目标环境与部署分工)。 |
| 执行方法与证据 | [实施规则](../../../.agents/rules/implementation.md)、[测试规则](../../../.agents/rules/testing.md)、[组件测试入口](../../../apps/camctl/tests/AGENTS.md)、[跨组件测试入口](../../../tests/integration/README.md)、[隔离集成入口](../../../scripts/README.md#隔离并行集成测试)。 |

## 起点事实与需要补齐的证据

[设计前软件验证](../../camctl/hasselblad-availability-preconditions-2026-10-28.md)保存了实际源码、环境、日志和范围。其通过结果可支持接口复用判断，不能作为新业务链已经接入或 HA 条目已经完成的证据。

| 当前可复用的事实 | 对实施顺序的影响 |
| --- | --- |
| 现有活动释放、原调用收场、操作意图及重试责任已有公共入口；部分 SQLite 实验验证了可靠未派发、未知效果和输出范围的区别。 | 在原入口增加可用性释放依据和独立判断，不另建绕过原事务的修改路径。旧 UNKNOWN 的形成和原调用收场仍须由实际业务入口证明。 |
| 独立 doctor 分支 `670871aa` 有接口及替身证据，尚未接入本规格的责任、历史、调度和释放。 | 可以审阅并选择性复用代码；不能把该分支视为已完成步骤，也不能假定直接合入即可满足契约。当前基线的装配、结果拥有者和取消传播必须重新核对。 |
| DEVICE 安装版 RAW/JPEG 及公开 `open_read` 的部分失败和真实进程收场已有通过证据。 | 复用实际下载与交付路径，同时补入固定物理身份和恢复检查。原来的正常读取没有经过新检查，不替代 HA-09、HA-10。 |
| 报告登记解释证明 occupancy 不决定 `end_unconfirmed`，没有执行完整新报告生成。 | H3 必须取得真实报告，H4 再验证三条历史恢复路径和固定报告字节。 |
| 安装版缺失 START 测试到达故障门并形成两份文件，但恢复包装访问已删除的 `CaptureRuntime.owned`，目标恢复没有运行。另有 capture、start-release、reporting 用例在旧构造参数处失败。 | H1 先保持原场景与参数矩阵，迁移测试装配和观察入口；不能删除失败分区、恢复私有旧接口或把文件存在解释为恢复成功。 |

当前代码中，拍摄准备和 START、读取候选、读取机会、READ 意图及仓储守卫分散在不同入口。只给驱动增加 doctor，或者只改一处 `HELD` 判定，都不能完成恢复。DEVICE 文件身份目前也未固定完整原机身身份，必须同时修改生产登记和读取消费者。

## 推进顺序与进度维护

| 原步骤 | 必需输入与依赖 | 本步骤应取得的可观察路径 |
| --- | --- | --- |
| H1 原责任恢复与公开测试入口 | 当前公开装配、CLI 和主程序收场契约；不依赖新 doctor。 | 一次真实替身拍摄的 START 结果遗失后，原尝试沿真实边界恢复为未知，旧拍摄零重放，文件保留，报告反映原事实。 |
| H2 身份、检查接口与持久事实 | 正式规格和当前结构；独立部分不以 H1 全部通过为前置。 | 正式初始化产生新结构；公开驱动与仓储组合保存真实检查、固定目标和原子释放，并可回放同一结果。此时尚不宣称正常业务入口已经接入。 |
| H3 正常拍摄与两种来源完整接线 | H1 的可靠原责任入口，H2 的事实、事务和检查接口。 | 普通 `submit/run` 从旧 UNKNOWN 推进新拍摄或 DEVICE 获取；HOST 本地工作独立完成；有限失败、取消和基本重启同时可用。 |
| H4 交错、故障、历史和诊断闭合 | H3 的真实生产入口及其原拥有者、调度和报告。 | 通过确定的竞争和中断证明整条链保持原责任、次数、文件、优先级及报告。 |
| H5 安装版联合验收与软件交付 | H1—H4 的对应路径完成；完整 HA 映射可追踪。 | 从安装 wheel、真实 C host 和客户端完成新恢复链及既有回归，冻结可交付版本与软件证据。 |
| H6 部署交接与目标设备验收 | H5 发行物；实际环境选择和现场操作安排另行明确。 | 在隔离部署中核对目标环境与真机行为，或者明确记录尚未执行；旧现场数据和计划的处理有独立交接。 |

进度只更新到下文 H1—H6 的原步骤。每步记录实际生产入口、已取得结果、剩余接线或验证、提交与验证记录链接；只有达到该步完成条件才勾选。模块和提交可以细分，不能用另一套辅助清单替代这些完成条件。H1 与 H2 的独立工作可按实际依赖推进；共享文件必须在干净基线和独立工作区中按顺序集成。

新副作用引入时同时实现其失败与恢复语义。H4 扩展竞争和故障见证，不承担补做 H3 本应已经存在的取消、有限重试或原结果保存。若连续工作只增加基础设施却无法接通 H3，先说明具体缺口并调整正式计划，不继续添加未经论证的前置阶段。

## H1 原责任恢复与公开测试入口

- [ ] 完成 H1，并在本节记录实际恢复入口、覆盖对应关系及验证证据。

**当前进度。** 已将录像启动占用释放、哈苏原 START 恢复、拍摄契约及报告测试迁移到正式会话装配，安装版 missing START 已沿真实恢复入口取得零重放和文件保留证据，reporting 完整目录通过；提交 `afbe3109`、`d47367fb`、`ca5db88c`、`7fed2961`、`9d811033`、`d6b55655` 和[专项验证记录](../../camctl/hasselblad-availability-implementation-verification-2026-10-28.md)记录其范围。完整 capture 目录及 V1 组合验证尚未完成；旧装配失败正在迁移。

**前置与业务结果。** 原检查必须等待旧 START 的实际本地执行与保存责任收场，因此先取得这一入口的有效证据。这里不需要新可用性操作；旧拍摄仍按原契约失败并保持未知，后续新拍摄仍可被旧限制阻挡。

**实施任务。**

1. 对照前置验证的原始失败，建立“原 nodeid 与参数分区 → 公开生产入口 → 可观察结果 → 新测试位置”的覆盖对应表，随验证记录保存。区分旧构造接口失败、目标行为失败和尚未运行，保留原测试的状态、重试和报告矩阵。
2. 统一有关测试的公共装配。组件测试从当前装配工厂、驱动端口、仓储事务和恢复入口组成世界；安装版测试经真实 CLI、C host、文件与已声明的时钟或工具接缝运行。不直接构造运行时私有拥有者，也不重新增加 `CaptureRuntime.owned` 兼容入口。确实缺少公共装配契约时，只补足该场景所需的公开接口，并记录其责任。
3. 校正安装版启动观察：机器协议精确核对，面向人的启动文案按内容和结构判断；保持原故障门与中断位置。必须在一次真实替身拍摄已发生、START 结果尚未可靠保存的位置停止原执行，然后由正常重启入口获得原本地收场边界和原历史范围。
4. 分别验证边界可靠、边界缺失、同一会话原调用仍运行、保存回执未知和原结果已经保存。仅在结果缺失且适用边界可靠时，沿原操作键保存未知；已保存结果按原实际结论继续；原调用或保存尚未收场、边界缺失时等待或停止依赖工作。全部分区核对原身份、尝试计数、工作文件与零重放；不能用后来的 doctor、日志 PASS 或文件存在补写原 START 结果。
5. 迁移旧 start-release 和 reporting 装配后，重新验证可靠无效果可以释放、可能启动仍保留限制、输出范围未完成仍阻挡，以及真实报告的原错误和设备执行状态。若公开入口暴露生产缺陷，先定位违反的原契约，再修复该类问题；涉及未定义语义时按设计缺口规则处理。

**建议修改位置。** [capture 集成测试](../../../apps/camctl/tests/integration/capture)、[reporting 集成测试](../../../apps/camctl/tests/integration/reporting)、[安装版 Hasselblad 测试](../../../tests/integration/test_hasselblad_photo_roundtrip.py)及其[公共世界装配](../../../tests/integration/camctl_fixtures.py)。生产入口核对 [capture/handlers.py](../../../apps/camctl/src/camctl/capture/handlers.py)、[host_photo.py](../../../apps/camctl/src/camctl/capture/host_photo.py)、[operations/recovery.py](../../../apps/camctl/src/camctl/operations/recovery.py)和[会话装配](../../../apps/camctl/src/camctl/bootstrap/capture_assembly.py)；只修改实际确认的缺口。

**先失败测试与完成条件。** 先重现现有公开测试装配问题，再让相同业务分区运行到目标恢复。执行 V1 和有关单元测试；安装版 missing START 必须有实际恢复成功的证据，不能继续停在包装异常。通过本步只证明原责任入口，HA-01 与新检查相连的零派发约束继续在 H3、H4 验证。

## H2 身份、检查接口与持久事实

- [ ] 完成 H2，并在本节记录当前结构、公开接口及事务与历史证据。

**当前进度。** 已实现固定机身身份值和规范资源键、活动预留身份与准备绑定失败，登记独立检查证据类别和公共错误，完成独立 `list → info → doctor` 驱动检查，并固定 DEVICE 正式源的机身、长度及摘要。打开状态库时核对活动表的当前结构，拒绝缺少固定身份字段的旧结构；提交 `e7409865`、`035016f5`、`db2141e3`、`495b7d00`、`10198813`、`01bc4dbf`、`7422ad1e`、`fc003b31`、`c89e8715` 及[专项验证记录](../../camctl/hasselblad-availability-implementation-verification-2026-10-28.md)说明局部范围。检查事务、同机资源守卫、历史及驱动与事务的完整组合尚未完成。

**前置与业务结果。** 需要能够表达“原拍摄仍 UNKNOWN，但已按新检查解除调度限制”的完整事实。在第一次持久化新事实之前，结构、历史解释、独立守卫和合法状态必须同时具备。先用真实临时 SQLite 和受管工具替身验证这些公开边界，再由 H3 接入正常调度。

**实施任务。**

1. 从权威登记分配 `CHECK_DEVICE_AVAILABILITY`、检查证据和事件分支，更新当前结构及格式识别。按规格保存检查、固定目标关系、操作关联、预期机身、资源键和活动释放引用；流程状态与次数继续由原操作模型拥有。为同一触发动作与旧活动的唯一关系、同机查询及有界分页设置约束和索引。表名和内部字段名可以调整，事实归属和固定时点不能改变。
2. 同步 SQL、枚举适配、事件构造和校验、历史字段分类、快照范围、正向与逆向恢复及初始状态。检查与目标归触发动作历史，释放归旧动作历史；跨动作的一批事件只有完整事务共同可见。实现首次变更时就能回放，不把历史补接推迟到最终报告阶段。
3. 定义活动状态与占用的完整合法组合。行内约束明确处理 NULL，外键核对引用存在；跨表守卫核对成功尝试、检查目标、固定机身、调用收场、输出范围及当前冲突。保留原可靠未派发、无效果、时间与产物完成、可靠结束等释放路径；新增引用不能让 `ACTIVE` 或公共目录范围持有者借 doctor 放行。
4. 实现公开的检查创建、意图登记、结果消费和终结事务。创建只纳入原动作已失败、可能产生过启动效果、采用 TASK_SCOPE 且满足规格全部条件的 Hasselblad 单张旧活动，固定全体合格目标；登记意图共同增加次数并授予本次实际资格；成功结果、检查完成和所有仍需解除的目标共同提交。全部目标已被其他可靠路径解决时按事实完成；部分目标不再合格时保存真实结果而不部分释放。提交未知按原键核实，不另建责任。
5. 固定预期物理身份与独立观察。首次活动预留或检查创建在调用意图前保存 serial、产品标识和规范资源键；缺失原事实是状态错误，不能用新配置补齐。核对当前重复 serial 绑定规则，增加历史未完成责任与逻辑别名的冲突判断。当前工具路径可以变化，但替代机身不能消费旧责任。
6. 在 Hasselblad 驱动公开独立检查端口，严格执行 `list → info → doctor`。复用受管进程、输出完整性及错误事实接口；整次共享单调期限，不在驱动内部重试。保存每一步的原始双流、实际退出和前序结果，明确 PASS 才形成可用性观察；不改变活动查询能力，也不形成相机重启或采集结束证据。选择性复用 doctor 分支时，逐项核对当前结果类型、取消传播、进程升级终止和登记装配，再在当前基线验证。
7. 给 DEVICE 正式源的生产登记、文件身份和定位信息增加由可靠拍摄结果核对的原机身。`open_read` 同时核对原逻辑绑定、固定物理身份、文件名或路径、variant、大小、SHA-256 和偏移。更新当前文件格式及其消费者，不增加旧格式解释分支；HOST 的本地身份和本地处理不增加相机前提。
8. 在公共错误权威登记接入规格规定的两个错误及物理身份绑定原因，联动动作合法状态、逐项失败结构和报告字段依赖。允许合法受理但尚未 START 的准备失败，不用提前写入 running 取得失败保存资格。数字编号与完整类型从登记生成或读取，不在各层另存清单。

**建议修改位置。** [数据库结构](../../camctl/database/schema/README.md)、[枚举登记](../../camctl/database/enum-registry.json)、[事件登记](../../camctl/database/event-transitions.json)、[报告依赖](../../camctl/database/report-dependencies.json)、[公共错误登记](../../../protocol/errors/workflow-codes.json)；生产侧为 [persistence](../../../apps/camctl/src/camctl/persistence)、[history](../../../apps/camctl/src/camctl/history)、[operations](../../../apps/camctl/src/camctl/operations)和[Hasselblad 驱动](../../../apps/camctl/src/camctl/devices/drivers/hasselblad)。必要时新增专门检查仓储，避免把无关规则塞入一个现有大文件；公共事务边界保持。

**先失败测试与完成条件。** 先用独立预期覆盖伪造释放引用、跨设备目标、缺失身份、部分释放、SQL NULL、重复检查关系以及实际失败结果。纯分类用单元测试；真实 SQLite、事件、受管工具和文件身份往返用集成测试。V2 通过后，必须同时有正式 `init`、公开检查端口、原子事务和三种历史读取可消费的证据。新表存在或静态登记通过本身不算完成。

## H3 正常拍摄与两种来源完整接线

- [ ] 完成 H3，并在本节记录普通入口的完整成功、有限失败、取消及基本恢复证据。

**当前进度。** START 和设备 READ 意图的事务提案及独立守卫会分别核对同逻辑设备是否存在对方尚未收场的尝试；读取机会授予也核对在途 START。独立守卫核对原连接和同事务已验证前序，普通意图必须恰有一条尝试及其关联流程。等待重试的保留本身不作为在途读取，HOST 读取不占用相机。该局部检查不覆盖物理机身别名、检查尝试、读取派发再检查或完整的重试让位，生产恢复链仍未接通。单元验证范围见[专项验证记录](../../camctl/hasselblad-availability-implementation-verification-2026-10-28.md)。

**前置与业务结果。** 消费 H1 的原责任恢复和 H2 的身份、检查及事务接口。第一条正常业务链必须经过生产装配、候选发现、调度、实际派发、持久保存和报告；HOST 与 DEVICE 都在本步骤接通。

**实施任务。**

1. 在 operations 的原结果拥有者边界增加检查协调。复用原操作状态、意图、结果保存、`retry_wait` 和本地恢复机制；从目标设备已有 query 配置读取最大次数、间隔及整次期限，并把实际采用值写入尝试。把新装配入口纳入统一配置消费检查，不增加另一份默认数值。操作种类和预算独立于 `QUERY_ACTIVITY`、START 和 READ；配置变化只影响新增尝试，终态不重开。重启沿原次数和等待标记恢复，不跨会话沿用旧单调钟，也不给缺失结果的旧尝试重新分配完整正常等待期限。
2. 为正常拍摄补齐没有新活动时的候选发现。动作在普通准备资格成立后，可以针对旧合格限制创建检查；不提前进入 running，不建立照片文件责任，不增加 START 次数。检查结束后重新进入原准备和窗口规则。普通到时选择、已有预留及截止时间继续有效，不能把“进入提前区间”直接当作抢占许可。
3. 在 DEVICE 读取候选、机会授予之前发现旧未知阻挡，调用同一协调入口。同动作 original、preview 和同设备候选复用固定检查与次数；分页、排序、唤醒、重送和重启不能创建新预算。检查成功后重新核对来源、取消、清理保护和拍摄优先级，沿原 copy、delivery 和读取票据恢复；正式下载、内容校验、偏移、交付保存及临时副本清理均走原生产路径。
4. 统一读取资源事实，并让准备、START、检查意图、READ 机会与意图以及实际派发各自独立判断。等待中的检查保留责任和间隔，只有一次实际尝试持有机身执行资格，覆盖命令间隙、输出读取、原结果保存和必要收场。同机其他工作不能插入；其他机身、HOST 本地拷贝与报告继续。
5. 实现读取检查的让位与恢复。只有被普通规则选中的拍摄才建立或恢复自己的检查；不抢占在途尝试，不转移读取预算，不取消文件顺序。间隔完成即可靠清除等待标记，拍摄占用造成的等待不重复计时。其他检查已经解除全部固定目标时，开放责任无额外调用地按真实引用完成；终态检查或读取项保持终态。固定旧批次完成后仍有新未知活动时，重新处理新限制。后来出现的高优先候选只约束后续派发，不能阻止原调用结果保存或撤销其合格释放依据。
6. 将检查拥有者接入会话唤醒和关闭：同时发现准备时刻、计划时刻、窗口结束、重试、取消、资源变化及待交付结果。恢复发现包含终态后的调用或保存，不能只查开放流程。没有有效触发者且没有实际收场时不因旧 UNKNOWN 永久轮询；检查耗尽必须产生可观察的有限结果。
7. 首次派发即实现取消、过期和结果竞争。派发前复查资格，可靠未派发保存真实原因且保留已用次数；工具开始后通过受管入口收场，不执行剩余步骤或新尝试。完整合格结果已经取得时可完成原批释放，但触发动作或读取项的终态不重开。保存未知先核实原键，保留原结果和全部诊断。
8. 接通业务错误、报告和诊断消费者。拍摄检查耗尽用新的准备失败，保持尚未开始；DEVICE 检查耗尽只使依赖项失败，未派发 READ 零新增次数，源存在性不改写，其他项继续汇总。原拍摄错误与 `end_unconfirmed` 保持；绑定、工具、下载内容、状态库和取消各按实际类别保存。完整异常图及双流交给实际消费者，重送共享诊断状态，必要收场不等待日志队列空位。
9. 验证 HOST 独立路径：可靠旧成功结果在相机缺失时继续归档、登记、取回及报告；未知工作文件保持原样。普通 HOST 拷贝不进入设备检查或 READ 机会。DEVICE 源的 RAW/JPEG original/preview 都经真实生产登记、检查、下载和交付完成。

**建议修改位置。** [bootstrap/capture_assembly.py](../../../apps/camctl/src/camctl/bootstrap/capture_assembly.py)、[obtain_assembly.py](../../../apps/camctl/src/camctl/bootstrap/obtain_assembly.py)、[flows.py](../../../apps/camctl/src/camctl/bootstrap/flows.py)、[capture/preparation.py](../../../apps/camctl/src/camctl/capture/preparation.py)、[host_photo.py](../../../apps/camctl/src/camctl/capture/host_photo.py)、[scheduling/resources.py](../../../apps/camctl/src/camctl/scheduling/resources.py)、[outputs/dispatch.py](../../../apps/camctl/src/camctl/outputs/dispatch.py)、[read_discovery.py](../../../apps/camctl/src/camctl/outputs/read_discovery.py)、[read_attempts.py](../../../apps/camctl/src/camctl/outputs/read_attempts.py)，以及 persistence 中 preparation、scheduling、operations、capture、outputs 的事务守卫。共享协调可单独成模块，但必须由两条真实装配调用。

**先失败测试与完成条件。** 先建立两条被旧限制阻挡的普通 CLI 场景，一条触发新拍摄，一条触发旧 DEVICE 正式源获取。DEVICE 场景先可靠拍摄并登记正式源，再由另一拍摄留下 UNKNOWN，最后独立取回先前正式源；不能从未知工作文件补造产物。两条场景均用实际公开入口形成 UNKNOWN，不直接 UPDATE 状态。V3 必须观察到原调用先收场、唯一检查、旧 UNKNOWN 与新 RELEASED 同时成立、后续工作成功和真实报告；另外覆盖检查失败重试至耗尽、取消、窗口结束、一次重启和提交回执未知。独立驱动或协调器测试通过而生产入口没有消费时，本步保持未完成。

## H4 交错、故障、历史和诊断闭合

- [ ] 完成 H4，并在本节记录全部交错分区、历史比较和诊断收场证据。

**当前进度。** 公共诊断消费者已支持由可用性检查票据选择保留工具标准输出，提交 `cd7282fb`；[专项验证记录](../../camctl/hasselblad-availability-implementation-verification-2026-10-28.md)记录其局部测试。真实检查拥有者、全部交错及历史恢复尚未接通和验证。

**前置与业务结果。** 使用 H3 的普通业务入口，不另建只用于验收的放行路径。本步以完整规格矩阵扩展故障见证，并修复真实组合中暴露的接缝。

**实施任务。**

1. 为检查创建、意图、实际派发、每条工具返回、结果保存、释放、间隔清除和工作交接设置公开边界上的明确同步点。业务时间使用受控 UTC 与单调钟；真实子进程期限、信号与收场仍实际发生。对每个持久边界分别验证可靠回滚、提交未知、进程中断和通知丢失，记录目标操作是否真正运行。
2. 完整执行规格 HA-05 的七行交错矩阵，保留每行内的有效分区：提前准备尚不合格、已合格和到时；计时结束与选择的两种次序；list、info、doctor 及命令间隙；同目标释放与重送；双方取消、耗尽和过期；间隔与交接中断；旧目标部分解决及新未知活动。通过实际事务与派发证明零重叠、无重复调用和等待责任零新增次数，不能只断言选择函数。
3. 多活动与多文件规模至少跨过目标和候选分页边界。改变页顺序、候选次序、重复唤醒和新请求时机，核对固定集合、同动作唯一关联、独立动作预算、终态不重开和新活动的新责任。保留 ACTIVE、ENDED 但范围未结束、其他驱动公共目录限制和旧 READ 尚在收场等拒绝分区。
4. 在取消及窗口的各同步点验证实际结果拥有者。两个等待者与恢复入口共同消费同一结果时，只保存一次、诊断一次；取消一个等待者不丢掉另一拥有者的原调用。工具忽略 SIGTERM 时实际走受管入口的升级终止并核对退出；仍无法确认收场则保留责任、零新派发，不能把超时本身当作已退出。
5. 比较完整回放、快照正向恢复和当前投影逆向恢复，覆盖跨动作目标关系及同一完整历史边界。重建次数、检查终态、释放引用和固定身份；回放不调用工具、不读取新配置重判旧结果。生成真实历史报告，核对原旧报告字节不变、旧 `end_unconfirmed` 保持，占用变化不单独推动旧动作公开变化水位。
6. 从生产诊断消费者注入工具失败、解析失败、身份错误、下载失败和保存故障。验证原异常、部分双流、前序成功、退出及完整性事实不丢失；重复交付不重复记错，下一次实际失败独立诊断。真实日志队列满载和写入失败时，必要终止、结果保存和会话关闭仍推进，日志错误不覆盖原设备错误。
7. 覆盖 DEVICE 读取之前零台、多台、实际机身改变，以及读取途中超时、取消、内容替换、截短、续传和跨运行清理。检查预算与 READ 预算分别核对，原文件身份、票据和交付进度保持；同取回的 HOST 和另一机身项继续。绑定删除、驱动改变、serial／产品改变、同机逻辑别名及缺失固定历史各按规定分类。

**建议修改位置。** H2、H3 已有生产入口及其测试；历史与报告消费核对 [history](../../../apps/camctl/src/camctl/history)、[reporting 仓储](../../../apps/camctl/src/camctl/persistence/repositories/reporting.py)和[日志运行模块](../../../apps/camctl/src/camctl/logging_runtime)。新测试可按责任放入 capture、operations、outputs、scheduling、history、reporting、logging_runtime、bootstrap、devices 集成目录；实际目录以测试入口发现为准，不为本计划另造一套并行规则。

**先失败测试与完成条件。** 从上述状态分区推导独立预期，先用最窄可证伪测试定位接缝，再运行 V4。HA-01—HA-11 的全部软件分区须有实际生产入口、节点和结果映射；H5 补充安装及跨组件见证。发现无法满足现行规格时，说明具体状态、已有证据和影响，暂停依赖该决定的部分，不用放宽断言或增加临时豁免完成本步。

## H5 安装版联合验收与软件交付

- [ ] 完成 H5，并在本节记录集成版本、完整 HA 映射及软件交付证据。

**前置与业务结果。** 在 H1—H4 生产能力可用后，验证正常发布物与真实双方能够共同完成恢复。源码定向测试、旧分支证据和 Schema 检查均不能替代安装版联合链。

**实施任务。**

1. 从同一待交付源码构建独立 wheel 与锁定依赖，核对导入来源、包内 SQL、枚举、事件、公共协议及默认驱动登记。正式 `init` 创建新库；当前结构要求与格式检查共同生效。构造隔离的不支持格式或结构样本，验证程序明确拒绝、没有自动转换或重建，原内容及文件保持；不使用现场库作测试样本。
2. 在安装版既有世界中加入完整恢复链：真实客户端导出、C host、CLI、SQLite、工具替身、报告及文件领取、客户端导入和累计 ACK。读取所需正式源先由可靠成功拍摄形成，随后另一拍摄留下未知；再由独立有效工作触发检查，分别取得新拍摄和已有 DEVICE 产物读取成功。HOST 在相机离线时独立交付。按 RAW/JPEG、original/preview 和两次独立取回保留来源、摘要及交付身份。
3. 重跑 H1 的安装版 missing START，以及已保存 START、完成提交回执丢失和固定报告重建分区。完整保留既有 Hasselblad 十二组正常链及各异常恢复参数，不用新增窄例替代原矩阵。无相机、多相机、身份不符、有限失败和取消至少分别有真实业务消费者见证。
4. 核对新增错误在公共 Schema、wheel 资源、真实报告、客户端导入及展示中的一致性。按客户端现有生成入口更新派生类型；只在实际结构或消费需要时修改客户端和 C 代码。公共报告保持稳定错误和必要关联，不把原始工具路径或双流加入用户业务报告。
5. 完成 V5。失败先归因到准备、旧装配、实际业务或资源环境；适用门禁中的缺口仍需修复，不能将未到目标的失败算作目标通过。保留当前收集清单、完整节点及参数、源码身份、命令、退出码、原始日志和各层证明范围；有缺失依赖导致跳过时明确保留未验项。
6. 核对[真机验证环境入口](../../../.agents/skills/camctl-real-device-deploy/SKILL.md)和其部署脚本。如果结构、初始化、组件构建、部署配置或能力说明消费发生变化，同步受影响脚本及说明；不因该核对启动现场环境。形成软件发行物、摘要、配置用法、已知边界及 H6 所需交接资料。

**建议修改位置。** [跨组件测试](../../../tests/integration)、[包构建与组件说明](../../../apps/camctl/README.md)、[资源同步脚本](../../../apps/camctl/scripts/sync_resources.py)、[客户端](../../../apps/client/README.md)、[C 接入模块](../../../apps/host-demo/README.md)及受影响部署脚本。验证结论写入 [camctl 验证记录](../../camctl/verification.md)或其引用的带日期专项记录；本计划保留状态和链接。

**完成条件。** H1—H5 的生产路径和 HA-01—HA-12 软件部分全部有实际结果，完整门禁通过，完整 diff 与暂存范围审查后提交。报告明确区分软件替身、安装版、目标主机和真机证据。H6 的未执行项目不被改成软件通过，也不妨碍准确交付已经完成的软件范围。

## H6 部署交接与目标设备验收

- [ ] 完成 H6；实际目标未安排或尚未执行时，保留未完成状态和具体待验范围。

**前置与边界。** 本步骤使用 H5 已验发行物。编写和执行软件计划不授权改写现场库、运行真实相机、断电或重排旧计划；实际部署任务须明确目标环境及操作范围。设备运行假设已由正式规格确定，不另加人工确认才能每次继续的机制。

1. 先交付两种环境处理方式及其具体影响。独立新环境保留旧环境和数据，采用正常 `init`；它不继承旧动作 ID、计划或确认水位，也不证明原现场动作已经接续。若要求继续原实例，则另行制定一次离线状态转换，覆盖备份、原历史、固定身份、文件关系、待处理责任、完整性与失败恢复；该工作不加入日常旧格式分支。
2. 新旧环境切换前，核对同机原 host、camctl、工具进程及输出读取已实际收场。一个状态库的资源守卫不能协调第二个独立环境或外部相机软件。新环境接管设备后，旧环境保持停止，防止后来再次执行同一业务。
3. 逐项交接旧待执行计划：是否已经派发或可能有未知效果、原窗口是否仍有效、引用哪些原产物和客户端关系。需要新工作时使用明确的新请求，不重用旧请求身份、延长旧窗口或直接改状态获得拍摄机会。未经单独状态转换，新环境不得声称继续旧计划。
4. 在获准的独立目标环境核对 Python、SQLite、受管进程、当前初始化、工具版本与真实机身绑定。使用真实 doctor、后续拍摄及 DEVICE 下载验证接受的运行假设，保留原活动 UNKNOWN 和文件；实际供电由部署侧执行和记录，camctl 不推断发生过重启。
5. 依据实际上电时间、工具耗时和拍摄准备时间配置既有 query 次数、间隔、期限及准备提前量，记录实际配置与预期覆盖。验证可用、持续不可用、身份改变、供电恢复晚于窗口及有限失败退出；默认值不能承诺任意断电周期。目标性能、真实媒体、ARM 环境和物理断电持久性分别记录，不外推 Linux 替身结果。

本步骤的交付为实际部署收据和验证结论，或明确的未执行清单与环境处理决定。没有选择原实例接续时，不开展现场转换；这不会阻塞新环境的软件实施。

## 状态、失败与恢复的实施核对

下表是各步骤的横向检查入口，完整判定仍以正式规格为准。每行都需要沿“发现 → 原资格 → 调用或不调用 → 持久结果 → 报告或交付”核对真实消费者。

| 实际状态或失败位置 | 实施与恢复应观察的结果 | 主验证步骤 |
| --- | --- | --- |
| 原 START 未保存，原调用或保存仍未收场 | 原拥有者继续；无 doctor、新 START 或冲突 READ。可靠本地边界只结束主机责任，不生成相机结束事实。 | H1、H3、H4 |
| 旧成功结果已保存，归档或登记未完成 | 沿原文件责任继续，HOST 不依赖相机在线；不生成新拍摄。 | H1、H3 |
| 原绑定缺失或改变，固定事实缺失 | 绑定错误与状态库错误分开；不操作替代机身。创建调用前拒绝不消耗尝试，已提交意图不退还次数。 | H2—H4 |
| 创建或意图未提交、可靠回滚 | 不派发，无新责任或次数；以后重新判断实际资格。 | H2、H4 |
| 意图已提交但可靠未派发 | 保存实际未派发原因，保留次数；同目标已解决、取消、过期或让位均不制造工具错误。 | H3、H4 |
| 工具失败、不完整、超时、身份不符 | 保存每步实际事实；原进程和输出先收场，再按原检查剩余额度及资格处理。 | H2—H4 |
| 检查等待间隔，普通拍摄取得资格 | 读取保留原责任、次数和顺序，让出实际执行；不抢占在途调用，原间隔不因拍摄等待重复。 | H3、H4 |
| 全部目标已被其他路径解决 | 开放检查按真实引用结束，零新增调用；终态责任不重开。 | H2—H4 |
| PASS 可接受且全部释放前提成立 | 原结果、检查结论、全部必要释放同事务保存；旧活动 UNKNOWN、原动作和报告事实保持。 | H2—H5 |
| PASS 已取得，但仍有目标不合格或新限制出现 | 保存观察，禁止不合格的部分释放；旧批与新活动分别判断，实际后续工作再次核对资源。 | H2—H4 |
| 保存失败、回执未知或释放后通知丢失 | 原完整输入及键保留，先核实事务；已提交结果不重放，未可靠释放不派发冲突工作。 | H3—H5 |
| 取消或过期与完整返回竞争 | 分别保存触发终态、检查结果和实际调用收场；合格迟到结果可解除旧限制，不能重开工作。 | H3、H4 |
| 检查耗尽或最终未知 | 拍摄或受影响读取项有有限终结；未派发 START／READ 不增加其次数，源存在性不改写，其他工作继续。 | H3—H5 |
| DEVICE 下载已经开始后失败或取消 | 原 READ 票据、预算和交付进度继续拥有责任；下载进程、句柄和结果收场后才清理副本。 | H3—H5 |
| 终态后仍有真实调用、保存或诊断 | 恢复和关闭继续发现并完成责任；不因仅查询开放状态而遗失，也不因旧 UNKNOWN 永久保持空轮询。 | H3、H4 |

## HA 验收对应关系

此表只定位实施和证据层次。[正式 HA 条目及 HA-05 七行交错矩阵](../../camctl/hasselblad-availability-recovery.md#实施责任与联合验收)保持完整权威，表中的简称不减少其中任何分区。实施中将实际 nodeid、参数覆盖和验证记录链接补入对应原步骤。

| 条目 | 实施步骤 | 必需证据 |
| --- | --- | --- |
| HA-01 原责任先收场 | H1、H3、H4、H5 | 公开原恢复与安装版 missing START；活跃、边界缺失、保存未知时零新派发；零重放、原文件和身份保持。 |
| HA-02 完整检查分类 | H2、H4、H5 | 独立分类及真实受管工具；全部设备数量、身份、解析、退出、完整性和总期限分区，原始双流。 |
| HA-03 原子释放与报告 | H2、H3、H4、H5 | 真实 SQLite 的整批原子提交与拒绝反例，真实报告保持 `end_unconfirmed`，旧动作和文件不改写。 |
| HA-04 多活动与共享预算 | H2、H3、H4 | 多目标和多候选跨页，同动作共享、独立动作、自身耗尽、重送及重启；新未知活动形成新责任。 |
| HA-05 并发与其他设备 | H3、H4 | 规格七行全部交错，经正常调度、独立事务与实际派发证明互斥、优先级、原责任恢复和其他工作推进。 |
| HA-06 取消与窗口 | H3、H4、H5 | 创建、意图、派发、完整返回、释放两侧的取消和时间变化；真实退出、迟到结果及终态保持。 |
| HA-07 故障与历史 | H2、H4、H5 | 所有事务边界的中断分区、原键核实、三路径历史一致和固定报告字节，零回放设备调用。 |
| HA-08 HOST 路径 | H1、H3、H5 | 未知工作文件保留；已保存成功结果在相机离线时归档、登记、取回、报告，零相机机会。 |
| HA-09 DEVICE 路径 | H2、H3、H4、H5 | RAW/JPEG original/preview 的检查、真实下载、原摘要、续传和两次独立取回；数量、身份及内容失败。 |
| HA-10 DEVICE 失败与取消 | H3、H4、H5 | 检查耗尽与 READ 次数分离、部分成功，真实下载超时和取消收场，原票据与跨运行恢复。 |
| HA-11 诊断与关闭 | H2、H3、H4 | 真实错误消费者、重送去重、满队列、保存失败、SIGTERM 升级、终态独立收场与有限关闭。 |
| HA-12 当前结构与环境隔离 | H2、H5、H6 | 正式新库及包资源，不支持结构拒绝和样本不变；软件隔离收据、部署进程交接及真机证据分别记录。 |

## 验证入口与运行隔离

当前实施按用户要求不新增或运行集成测试。实现期间使用设计良好的单元测试验证局部契约，并控制用例数量与运行时间；V1—V5 中的集成命令和对应软件组合证据暂不执行、不记为通过。实现完成后，由用户进行人工流程验证；实际完成范围和未取得的软件组合证据仍须在验证记录中分别说明。下列集成入口保留为项目已有验证资料，不作为当前实施的执行指令。

所有命令从实施工作区根执行。先核对 `pwd`、`git rev-parse --show-toplevel`、分支和状态，明确正在验证的源码与差异。Python 使用部署版 3.11；测试依赖按 `apps/camctl` 的锁定配置准备。下列变量指向执行者已经准备好的独立测试环境和客户端依赖，示例绝对路径需要替换为真实位置：

```bash
CAMCTL_HB_PYTHON=/absolute/venv/bin/python
CAMCTL_HB_NODE_MODULES=/absolute/client/node_modules
"$CAMCTL_HB_PYTHON" --version
"$CAMCTL_HB_PYTHON" scripts/check-database-spec.py --runtime-only
"$CAMCTL_HB_PYTHON" scripts/run-integration.py --list
```

隔离入口复制源码、资源、临时状态库、安装目录、客户端构建目录和每槽可变依赖；不同组件目录仍分别启动 pytest。直接运行时，同一工作区一次只运行一个集成 pytest，不能把多个组件目录合并到一个进程。客户端 Node.js、锁定依赖、Chromium、离线构建缓存、C 工具和媒体工具按现有指南准备；缺失依赖引起的跳过不计作已验。

直接运行的 Node 检查从本工作区 `apps/client/node_modules` 读取锁定依赖，须按客户端指南在该工作区准备。`CAMCTL_HB_NODE_MODULES` 只指定隔离入口的依赖复制来源，不能替代这些直接检查的本地依赖。单元测试命令采用本工作区独立的 `.venv311`，执行前同样核对其解释器与依赖；不要同时同步资源并运行读取同一资源的测试。

下列命令省略 `--output`，由入口创建全新证据目录；需要指定时，该目录必须尚不存在。命令返回后记录入口给出的实际目录、源码摘要、收集节点、日志和退出码。`--list` 与 `--plan-only` 仅用于发现和审查，不能记成测试通过。新测试文件位置由实施者按责任选择；本计划的命令以真实目录收集，新增后须核对实际收集清单包含它们。

### V1 原恢复与旧测试迁移

先在当前公共入口修复以下既有目标的装配，保留所有参数。有关节点已存在于起点源码；安装版 missing START 是必须取得的新恢复证据。

```bash
"$CAMCTL_HB_PYTHON" scripts/run-integration.py \
  --python "$CAMCTL_HB_PYTHON" --node-modules "$CAMCTL_HB_NODE_MODULES" --jobs 2 \
  --target apps/camctl/tests/integration/capture/test_hasselblad_start_recovery.py \
  --target apps/camctl/tests/integration/capture/test_recording_start_release.py \
  --target apps/camctl/tests/integration/capture/test_capture_contract.py \
  --target apps/camctl/tests/integration/reporting/test_emergency_error_generation.py \
  --target 'tests/integration/test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_missing_start_result_never_replays'
```

其中 `test_hasselblad_start_recovery.py` 的公开预期分别覆盖相对对焦之后不重放、原可靠恢复边界、同会话仍执行，以及原请求键保持；测试迁移后这些分区继续存在。窄目标修复后按变更影响运行 capture 与 reporting 完整目录，避免只维护一个新夹具可用的场景。

### V2 当前结构、接口与历史事务

先运行新增或受影响的最窄单元测试，再执行结构及公共登记检查。生成文件只在相应权威登记改变后使用脚本的写入选项更新，随后运行默认只读检查，不直接手改派生编号。

```bash
"$CAMCTL_HB_PYTHON" scripts/sync-history-object-sql.py
"$CAMCTL_HB_PYTHON" scripts/check-database-spec.py
"$CAMCTL_HB_PYTHON" scripts/check-config-consumption.py
node scripts/check-protocol.mjs
node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs
node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs
"$CAMCTL_HB_PYTHON" scripts/run-integration.py \
  --python "$CAMCTL_HB_PYTHON" --node-modules "$CAMCTL_HB_NODE_MODULES" --jobs 3 \
  --target apps/camctl/tests/integration/devices \
  --target apps/camctl/tests/integration/operations \
  --target apps/camctl/tests/integration/history \
  --target tests/integration/test_database_history_sql.py
```

新仓储测试若归其他真实目录，把该目录加入当次目标并记录原因。节点必须实际调用新事务、守卫和历史接口；目录的旧测试全部通过不能替代新事实的见证。H2 新增的 doctor 节点只在当前工作区实际实现后计入，不能引用独立分支的通过结果充数。

### V3 正常生产接线

```bash
"$CAMCTL_HB_PYTHON" scripts/run-integration.py \
  --python "$CAMCTL_HB_PYTHON" --node-modules "$CAMCTL_HB_NODE_MODULES" --jobs 3 \
  --target apps/camctl/tests/integration/bootstrap \
  --target apps/camctl/tests/integration/capture \
  --target apps/camctl/tests/integration/outputs \
  --target apps/camctl/tests/integration/scheduling
```

在这些目录新增普通装配及真实 CLI 的阻挡、恢复、有限失败和 HOST／DEVICE 组合用例。现有 `test_hasselblad_device_distribution.py::test_installed_device_photo_reclaims_then_downloads_both_sources_twice` 的 raw、jpeg 两项继续保留；补充新检查链后分别说明原正常链与新增恢复链的结果。

### V4 竞争、取消、历史和实际诊断

```bash
"$CAMCTL_HB_PYTHON" scripts/run-integration.py \
  --python "$CAMCTL_HB_PYTHON" --node-modules "$CAMCTL_HB_NODE_MODULES" --jobs 4 \
  --target apps/camctl/tests/integration/cancellation \
  --target apps/camctl/tests/integration/operations \
  --target apps/camctl/tests/integration/history \
  --target apps/camctl/tests/integration/reporting \
  --target apps/camctl/tests/integration/logging_runtime \
  --target apps/camctl/tests/integration/devices \
  --target apps/camctl/tests/integration/scheduling \
  --target apps/camctl/tests/integration/outputs \
  --target apps/camctl/tests/integration/bootstrap
```

根据实际新节点位置补入未被本命令覆盖的目录。DEVICE 原有公开读取的内容不符、原绑定和 serial、真实超时或取消三个节点的全部参数继续保留；无设备、多设备、完整业务取消传播和忽略 SIGTERM 的见证必须新增或明确映射到真实承接节点。真实进程实验只终止本测试拥有的进程组。

### V5 完整软件门禁

先定向运行完整 Hasselblad 安装版模块定位跨组件问题，再完成单元、全部集成及规格检查。此前完整范围已在同一待交付版本通过且没有新变化或疑点时，不为凑次数重复执行。

```bash
"$CAMCTL_HB_PYTHON" scripts/run-integration.py \
  --python "$CAMCTL_HB_PYTHON" --node-modules "$CAMCTL_HB_NODE_MODULES" --jobs 4 \
  --target tests/integration/test_hasselblad_photo_roundtrip.py
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" \
  uv run --project apps/camctl --group test --python 3.11 \
  pytest apps/camctl/tests/unit -q
"$CAMCTL_HB_PYTHON" scripts/run-integration.py \
  --python "$CAMCTL_HB_PYTHON" --node-modules "$CAMCTL_HB_NODE_MODULES" --jobs 4
node scripts/check-doc-links.mjs
git diff --check
```

V5 同时完成 V2 中适用的结构、配置、协议和登记检查。隔离入口默认 `all` 包含全部组件集成、根 pytest、根 Node 和容量范围；Hasselblad 十二组正常链不被过滤。客户端或 C 生产代码有实际修改时，按各组件指南补充其受影响组件门禁；源码与安装端类型生成、构建和导入结果共同记录。

## 横切审查、提交与未决事项

每阶段提交前沿以下链路审查接缝：原绑定与 query 配置 → 固定身份和目标 → 普通候选选择 → 事务内独立守卫 → 意图及实际派发 → 原结果拥有者与收场 → 原子释放或有限失败 → 后续拍摄／文件交付 → 历史、报告、客户端和关闭。逐项核对生产者与消费者，不以每个模块单测分别通过推断整条链成立。

实施者按照 Git 协作规则在自己的工作区核对完整 diff、暂存范围和验证结果，按可独立审查的阶段提交；协调者按依赖集成后执行对应组合检查。共享守卫、历史登记、资源装配和包资源以集成基线为准，后续任务不能继续引用尚未集成的局部接口。测试覆盖对应表、失败归因和证据摘要进入版本控制中的验证记录，正式计划只维护原步骤进度与链接。

规格已经确定软件行为，本计划没有等待新的业务选择才能开始 H1—H5 的事项。实施前仍须核验目标测试依赖和当前代码基线；实施中遇到规格未覆盖的真实状态，按[设计缺口处理规则](../../../.agents/rules/design-and-planning.md#实现中发现设计缺口)说明证据并暂停依赖部分。现场选择独立新环境还是接续原实例、实际供电安排及真机验证时间属于 H6 的部署决定；它们不构成增加旧兼容实现或自动处理现场数据的依据。
