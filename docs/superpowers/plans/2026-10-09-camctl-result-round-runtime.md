# 产物核实的实际结果与恢复执行计划

目标是让照片、录像及延时摄影的正常核实和取消收场沿原 `results/<activity_id>` 保存完整结果。已经取得的文件观察、集合结论和实际调用错误共同保持；保存或返回中断后只读取原事实，不重新查询设备来补写结果。

正式依据是[产物核实轮次](../../camctl/database/operation-fields.md#产物结果核实的责任与轮次)、[配置和历史](../../camctl/database/operation-fields.md#查询和结果核实怎样保存配置与历史)、[实际结果保存](../../camctl/database/operation-fields.md#正常返回恢复与历史保存)、[任务的文件完成依据](../../architecture/camera-capture.md#文件完成依据与产物检查)及[设备端口](../../camctl/file-runtime.md#设备驱动接口契约)。本计划接续[配置计划 RC4](2026-10-09-camctl-runtime-configuration-closure.md#rc4-延时等待重配置与结果核实矩阵)。内部接口、文件划分及类名是实施建议，正式行为和状态分区是约束。

## 生产起点

2026-10-09 静态核对发现，`DriverResultListing.list_files` 创建的 `ControlRequest` 没有原 ticket 和实际调用期限，返回值只保留条目。`_listing_round` 把原异常改成泛化 `device_error`，调用方又用 `_round_outcome` 生成无观察的新结果。驱动已经提供的错误详情、收场及退出信息因此丢失。适配器还以 action ID 代替原活动 ID；两者不能假定相等。

`_confirm_timelapse_results` 原子保存尝试和集合结论，但依据只包含文件 identity。文件行在之后的 `_finish_capture` 才保存；`_finish_timelapse_conclusion` 恢复时重新访问设备。照片、录像及取消中的 CLOSED 回退也直接列举，取消异常甚至变为空集合。这些入口没有同一个可恢复的原文件输入。`listing_cache` 仅属于当前会话，不能证明跨会话完整结果已经保存。

## 原输入与实际消费者的数据流

`_begin_check_round` 根据动作的原活动提交 `results/<activity_id>` 意图。调用必须使用该事务返回的原票据和本次 `check_config.timeout_s`；查询在途与等待资格也必须使用同一活动责任。驱动返回后，完整 `CallOutcome` 及返回墙钟、单调读数先进入公共待存集合，再进行任何仓储读取、业务判定或文件副作用。结果错误与可靠文件观察可以同时存在，不能通过是否抛异常选择另一份结果。

建议以 `_listing_round` 作为普通入口的最窄共同持有边界，让其返回原票据、完整结果、原时刻与已验证的文件输入。条目是原登记观察的解释结果，不建立独立于完整观察的第二个权威缓存。保守收场的 `_save_winddown_progress` 也须走原责任与同一边界。调用者随后确定适用的结束、等待或集合请求，用原结果对象和单个结果事务 key 继续保存；这些接口与划分是实现建议，完整事实保存是硬性契约。

| 消费入口 | 原事实的作用 | 中断后必须继续的输入 |
| --- | --- | --- |
| 照片 `_photo_handler` | 原启动结局和原文件观察共同决定产物处理；实际核实错误保持为原失败尝试。 | CLOSED 后读取原观察与已保存的业务依据，继续归属、文件完成、产物登记和终态，不列举设备。 |
| 录像 `_advance_recording_outcome` | 原核实文件先与已保存处理责任关联，再按检查、修复和控制依据推进。 | 媒体链等待或动作尚未收尾时，使用原文件输入和已有处理事实；新 runtime 没有内存缓存也不能重新查询来补原结果。 |
| 普通延时摄影 `_timelapse_handler` | 原等待完成事件、逐文件事实和独立集合依据分别参与判定。 | 已有正式集合结论时，`_finish_timelapse_conclusion` 只读取原保存输入；v1 条目本身不提供集合结束依据。 |
| 取消 `_close_canceled_timelapse` | 已生效取消保持，实际核实错误与取得的文件仍保存；完成文件可沿原责任登记。 | CLOSED 后继续原输入，不重开普通资格；本地读取失败留下具体诊断，不变成空集合。 |
| 保守收场 `_save_winddown_progress` | 沿原核实责任保存实际结果和文件，再确定是否能建立原片与待处理阶段。 | 已保存停止事实保持，核实尚无可靠输入时保留责任和诊断；已存输入可在普通会话继续使用。 |

持久化的 `operation_attempts.result_json` 已能保存完整 `result_files_listed/v1` 观察。建议由共同本地读取函数按原票据、观察版本和原观察者绑定解码为同一文件输入，复用已有归属、配对、文件完成与结果守卫。当前配置和新驱动不重新解释原定位信息。正式存储载体是否足以作为跨事务完整输入还须沿守卫与历史验证；不能仅凭 JSON 有条目就绕过关系核对。

| 保存边界 | 恢复责任 |
| --- | --- |
| 原返回尚未可靠保存 | 会话集合持有原完整对象、时刻和 key；沿同一请求重送，禁止下一次设备列举替代。 |
| 原尝试结果已保存，文件登记尚未完成 | 从持久化原观察恢复相同条目；实际错误与文件并存，不补造成功结果。 |
| 原文件输入和适用集合结论已保存，动作尚未收尾 | 只执行所缺的本地归属、媒体、产物和终态步骤；零设备调用、零新核实名额。 |
| CLOSED 但本地输入缺失或无法按原合同解释 | 停止依赖步骤，保留缺失责任及诊断；禁止直接列举或以空元组替代。 |

复合事务须保存原尝试与所采用完整文件输入及适用集合结论；文件发现、归属、完成和配对继续使用正式事件及守卫。整轮结束时完整输入须已可靠保存，集合与适用业务结果不能先成立再补必要事实。后续原片关联、媒体处理及正式产物登记属于其各自尚未完成的步骤，必须从同一原输入继续，不能以临时缓存或重查设备代替持久化不变量。适用守卫要求同事务的文件关系、活动释放或动作结果，继续由原复合仓储边界共同保存。

## 条件模型

先判断原结果是否可靠保存，再判断实际调用是否结束，最后判断普通核实资格、集合与逐文件事实。已有终态保持；提高预算或换入口不会重新开启责任。

| 原事实与执行阶段 | 保存和后续行为 |
| --- | --- |
| 意图未可靠提交 | 不调用设备；按回滚或 UNKNOWN 核实原意图，不另建尝试退款。 |
| 原尝试仍由本次实际拥有者执行 | 继续等待及必要收场，保持 RUNNING；取消及时保存，不能发起冲突的新轮次。 |
| 旧未结束结果没有本次拥有者 | 按固定初始 H、旧本地收场边界及原驱动声明恢复；缺依据时保持原责任和诊断。 |
| 同一轮还有后续批次 | 每批沿原 ticket 和本次单次期限调用，不增加轮数；已取得观察分批保存，整轮仍未结束。 |
| 全部批次完成且契约证明必要集合和文件要求满足 | 保存原完整结果和采用依据；结束核实责任，按原拍摄契约登记产物及继续动作。 |
| 集合已确定，但必要类别等要求明确不满足 | 保存明确不满足及已知文件，不循环等待一个已确定集合；按所属失败或取消规则处理。 |
| 集合尚未确定，或文件写完依据不足 | 保存本轮真实结果及已取得文件；仍有资格和额度时下一次检查增加一个轮次。 |
| 本轮调用带错误，且已经取得可靠文件观察 | 同时保存原错误、观察、收场和调用信息；观察不能被错误抹去。能否形成集合结论仍由任务契约决定。 |
| 本轮调用带错误且没有可靠观察 | 保存真实空观察及错误；空观察不是目录为空，不补造集合完成或设备结束。 |
| 分页、观察版本、结构或目标身份不合法 | 保留实际诊断并按所属端口错误处理；拒绝非法事实，不改成成功或暂时无文件。 |
| 普通资格结束，但实际返回仍需保存 | 保存实际尝试及文件事实，保持原业务终态；必要取消及收场沿原独立责任继续。 |
| 结果事务确认回滚 | 持有原完整对象、时刻和结果 key，重送同一事实；不重新查询设备。 |
| 结果提交未知 | 先可靠结束当前事务并核实原完整 key；连接内的新投影不是已提交依据。 |
| 结果已经提交，仅返回或后续产物保存中断 | 从原持久化观察恢复同一文件输入和结论；零设备查询、零新尝试，旧终态保持。 |
| 所属责任 CLOSED 且没有完整本地输入 | 保留具体诊断和缺失责任，不直接列举，不把读取失败解释为空集合。 |

原绑定与当前配置只决定后续设备操作资格。已有完整本地观察可继续登记、发布和报告；失效绑定不能使已保存观察消失，也不能使新的驱动解释旧定位信息。是否集合确定、单个文件写完及归属是独立事实，禁止固定 `set_finalized=True`。

## 分页格式的待定事项

现有驱动结果 `result_files_listed/v1` 仅登记 `activity_id` 和 `entries`，不能表达下一批及集合确定，也不独立证明完整目录扫描。双相机的分页、独立完成依据及普通延时成功由[接入计划 T5/T6](2026-10-10-camctl-real-camera-demo.md#t5-分页结果完成依据与必要检查)承接本计划的对应未完成范围。具体内部类型、版本和成员属于实现选择，写入前须在责任登记中定义，并同时覆盖实时解释、保存和原输入恢复；不得用空页、条目数量或相同文件名推定扫描结束。

多批结果需要保持每批实际调用结果，不能只保存最后一批的错误或 `call_info`，也不能把多个调用包装成一个伪造的本地退出。优先复用现有文件事实分批保存及正式 result 外层；若现有登记无法完整表达批次结果，先提出有具体字段和保证范围的登记方案，不在 JSON 中加入未登记成员。

## 实施顺序与门禁

1. 给实际适配器和真实 RESULTS 意图写窄红。原 ticket 的 `target_id` 决定活动身份，本次 `check_config.timeout_s` 传给真实受管调用；CONFIRMED 加错误、ASSUMED 收场、真实 `call_info` 原样保存。替身受正式接口约束，不能继续以裸 tuple 表示生产调用结果。
2. 复用[公共调用结果保存责任](2026-10-09-camctl-capture-call-results.md#第一阶段原结果原时刻与原键的公共保存责任)。本次完整结果与实际返回时刻先持有再提交，普通、取消、绑定失败及受限工厂使用同一原尝试集合；同会话可靠结果优先于 UNKNOWN 恢复。
3. 沿照片、录像、延时摄影和取消入口共同保存持久化文件输入及适用集合结论。复用现有文件发现、归属、配对、完成和结果守卫，使用事务内事件派生事实，不先写当前投影绕过守卫。完整输入可分批，但不能出现已经认定整轮完成却找不到其文件事实及保证的边界。
4. CLOSED 及已有集合结论的消费者只读原保存输入。覆盖结果已提交、文件登记尚未完成，动作终态尚未保存，以及取消／失效绑定同时发生的真实中断。原 key 复用核对请求全文、完整事件段、原票据及实际时刻；不依赖后来投影反推原结果。
5. 分页格式确定后，分别验证空的合法中间页、多页、最后一页、未确定集合、每批失败和迟到结果；每次查询使用原 ticket 和独立期限，整轮只占一次额度，批内不隐式重试。
6. 分别注入投影失败、COMMIT 前／后错误及持续保存失败。证明保留原对象、原 key、全部真实观察和错误、单次设备调用，不重复轮数；解除故障后继续原结果。取消先可靠保存，结果迟到不重开业务终态。
7. 在完整事务边界 H 对照初始回放、快照正向、当前逆向；恢复不访问设备。原报告字节保持，当前报告只采用已有公共业务字段。

解释器使用部署 Python 3.11，所有 pytest 由 root 前台独占执行。先逐文件红—绿，再分别运行 capture、bootstrap、operations、history、cancellation，每个集成目录单独一个进程；随后运行单元全量。分页待定、普通查询消费者及文件运行拥有者各自保持明确未完成范围，不能以其中一个目录通过代替完整闭环。

## 验证记录与未完成项

2026-10-09，Linux x86_64、Python 3.11.16：适配器保留原票据、单次期限及完整结果的 4 项单元检查，与既有适配器的 12 项检查共同通过，日志为 `/tmp/camctl-goal-result-round-adapter-green-2.log`。这仅证明 `DriverResultListing.list_round` 的边界行为。

真实 RESULTS 消费者新增两项反例，分别返回带文件观察的调用错误和无文件观察的调用错误。原动作身份为 12，原活动身份为 71。root 前台独占确认实际请求仍为 `activity_id=12`、`ticket=None`、`timeout_s=None`，日志为 `/tmp/camctl-goal-result-round-consumer-red.log`。真实意图已经沿 `results/71` 保存，但消费者没有将原票据交给驱动；这两项仍处于有效失败测试阶段。

后续实施须沿上述实际责任传入当前单次期限，并在任何结果后读取或保存之前持有完整原返回。带错误的可靠文件观察仍参与所属任务判定；没有观察的错误保存真实错误、收场及退出信息。照片、录像、延时摄影、取消与 CLOSED 恢复的共同持久化输入尚未闭合，分页格式也仍等待既有决策。适配器通过不构成该阶段或完整拍摄链完成的证据。

新增消费者十四项由 root 确认有效红，日志 `/tmp/camctl-goal-result-consumers-red-2.log` 为 14 failed、1.54s。生产随后接入 `_listing_round` 的完整原返回持有和消费者实际处置保存；`capture/result_inputs.py` 提供 v1 原输入及持久化完整 Outcome 解码，照片、录像、延时摄影、取消和保守收场共同消费原结果。typed 替身按正式接口补齐，原业务预期保持。

root 前台独占共同消费者门禁，`/tmp/camctl-goal-results-consumers-current.log` 为 16 passed、1.64s；包含原活动身份／票据两项、五入口有文件与无观察实际失败十项，以及四个 CLOSED 本地恢复分区。该结果不证明 v1 集合结束或分页实现；普通延时摄影的集合成功仍等待正式依据。

### 多轮文件与子事务责任的后续分区

完整原 RESULTS 尝试是不可变调用结果的权威，`device_files` 的归属、配对及完成事件是已登记文件状态的权威。后轮错误和缺少观察没有撤销此前可靠文件事实的效力。CLOSED 本地输入需要同时覆盖原调用与此前仍有效的文件，不只解码最新一次返回；是否从原历史观察合并或从文件事实恢复，是待真实数据流核验的实现建议。

文件登记的每个独立保存请求在首次提交前确定完整输入、原事实时刻和唯一 key。UNKNOWN 必须沿该原请求核实，COMMIT 成功后的重送需要保留首次创建响应，才能继续相应在场登记；新 key 的重复发现不能代替原请求核实。请求保持的建议载体是原公共结果责任中的文件派生阶段，可靠保存后释放，各 factory 继续同一会话集合。

| 已取得事实与文件保存状态 | 必须继续的动作 |
| --- | --- |
| 前轮文件已可靠登记，后轮 FAILED 且无文件观察，所属责任 CLOSED | 保留后轮完整错误；已有文件继续本地归属、产物与业务收尾，不增加设备列举。 |
| 原文件发现事务回滚 | 原完整发现命令及 key 保持；可靠结束事务后重送，不重新列举。 |
| 文件发现 COMMIT 已成功但响应未知 | 核实原完整 key 并恢复首次响应；必要在场、归属、完成及配对继续保存。 |
| 本地原文件事实缺失、关系或定位不可解释 | 停止依赖步骤并诊断，不以空集合或设备重查替代。 |

`integration/capture/test_result_file_recovery.py` 新增四个多轮 CLOSED 分区和六个真实 SQLite 文件发现故障分区，等待 root 独占确认有效红。延时摄影使用已有正式独立 UNSATISFIED 结论；测试不赋予 v1 集合结束含义。执行者仅核验模块实际可导入和 diff，无 pytest、无新增生产修改。

完整历史前提复核：root 新十项日志 `/tmp/camctl-goal-result-file-recovery-red.log` 为 10 failed、1.31s。四项多轮文件丢失和四项投影／COMMIT 后原键差异属于行为失败；COMMIT 前两项尚处未知连接事务准备失败。共享世界需要能从真实 CREATE 历史重建原状态，已新增 `result_consumer_fixtures.py`，通过公开初始化、受理、调度、实际 START／STOP、活动结束、检查决定与取消仓储构造完整前提。未知连接实际关闭、重开并核对 metadata，原内存责任保持。十六项与新十项等待 root 重新验证；此前十六项绿色仅代表其原测试世界的消费者行为，不是完整原历史守卫的验收证据。

### 文件责任的生产接线与待验范围

2026-10-09，root 复核完整公开历史和正式守卫后，`/tmp/camctl-goal-results-real-world-current-4.log` 为 16 passed、10 failed，12.64s。十个新反例全部进入实际行为断言：四个 CLOSED 消费者丢失前轮可靠文件，六个发现阶段变更原申请 key。root 随后授权两类生产修复。

`PendingFileObservation` 使用独立于原 RESULTS 结果收场的生命周期。首次写入前保存完整 `FileObservationSave`、原 key、原同轮条目及已可靠登记映射；`RuntimeDeps.capture_file_observations` 由普通、残留、受限工厂注入相同集合。入口在终态、绑定与轮次筛选之前推进待存文件责任。文件事务回滚或 UNKNOWN 时核实原请求，不能把当前投影当作原提交证明。

后续在场、归属与完成使用各自 `PendingFileFact`，按 `FileFactStage` 保存完整申请、唯一 key 和可靠响应。每一子阶段独立保持，后面的错误不能清理前面的未知责任，也不能使已经可靠保存的阶段重新取得 key。整批文件事实完成后释放该文件责任，并把可靠登记映射交给原 raw RESULTS 结果；原实际 Outcome、结果事务身份和所属流程处分保持。

| 在场事实与原保存阶段 | 后续动作 |
| --- | --- |
| 没有待核在场申请，已有可靠 PRESENT | 复用事实，不保存没有状态变化的观察。 |
| 没有待核在场申请，当前 UNKNOWN 或 ABSENT，原实际条目确认在场 | 写前固定完整 PRESENT 申请、原条目时刻与 key，再提交实际状态变化。 |
| 原在场申请尚未可靠保存，包括当前行已经 PRESENT | 沿原完整申请与 key 核实，不能仅凭行值跳过未知提交。 |
| 原在场阶段已可靠完成，归属或完成尚未可靠保存 | 直接继续各自原申请和 key，不重新形成在场阶段。 |
| 发现返回 ALREADY 且 created=False | 只证明发现存在；在场及来源关系仍按各自可靠事实和待存阶段判断。 |
| 动作已终态且仍持有实际文件事实 | 保存原事实及必要派生，保持原 RESULTS 和动作结果。 |

`_saved_result_listing` 保留最新完整 Outcome，按原观察者绑定核对 `device_files` 身份、定位、来源、配对与完成依据，并合并此前仍有效的文件。`_register_listing` 直接消费可靠文件映射，避免把历史观察重新写成当前在场事实；尚未登记的原 v1 条目仍沿原实际时刻登记。文件状态和原调用结果分别有权威来源，最新错误不撤销已登记文件，v1 条目仍不提供集合结束依据。

后续子事实矩阵 `test_result_file_fact_saves.py` 覆盖照片／录像 × 在场／归属／完成 × 投影／COMMIT 前／COMMIT 后共十八项。root 日志 `/tmp/camctl-goal-result-file-facts-red.log` 为 18 failed、9.18s，故障均实际触发；在场原 key 改变及归属／完成恢复时的无变化在场请求属于实际恢复缺陷。root 授权后才接入上述逐阶段保存。生产导入、AST 和 diff 检查通过。root 独占十六项、十项、十八项，日志 `/tmp/camctl-goal-results-file-facts-current.log` 为 44 passed、20.48s。执行者实际读取日志，没有运行 pytest、暂存或提交。


### 原 v1 元数据与可靠文件状态的共同装载

原已保存 RESULTS 输入提供文件类别、原名称、媒体类型及配对元数据；可靠 `device_files` 事实提供来源、定位、配对关系、完成状态和大小。两类权威分别保持，文件行缺少类别字段不能解释为 OTHER。CLOSED 装载读取同一原 RESULTS 流程的已结束尝试，保留仍有效文件的完整 v1 元数据；最新 FAILED 没有观察时仍使用此前原已保存输入。最新完整 Outcome 和原尝试身份保持。

`_registered_result_files` 核对原绑定、定位与配对，随后只覆盖原条目中明确由文件行保存的完成状态和大小。缺少原元数据或关系矛盾时停止并保留诊断。装载和本地消费不取得设备端口，不从 v1 推定集合完成。此实现仍以 `handlers.py` 共同 Loader 为最窄边界，READ 资格守卫和仓储保持。

`test_result_file_metadata.py` 两项分别验证最新结果含原 v1 观察，以及最新 FAILED 没有观察时的原片与预览元数据。类别、名称、媒体类型、配对与大小保持，恢复零设备查询。root 修复前的 READ 日志 `/tmp/camctl-goal-read-digest-local-current-2.log` 为 13 passed、2 failed；修复后 `/tmp/camctl-goal-read-digest-local-current-3.log` 为 15 passed、9.80s，元数据日志 `/tmp/camctl-goal-result-file-metadata-current.log` 为 2 passed、1.34s。执行者已实际读取日志。

当前生产与测试已冻结，完整 unit、capture、bootstrap 目录及端到端独立审查由 root 执行。44 项文件责任、15 项 READ 与 2 项元数据仅证明上述分区；分页格式及普通延时摄影集合成功仍等待既有正式决策。


### 结构化 JSON 原申请的同一性

完整原申请包括 JSON 字段的类型和值。JSON `true` 与数字 `1` 不同，对象成员顺序不影响同一性。文件发现定位、原条目的定位和完整观察依据、归属及配对依据、完成观察及错误、CLOSED 定位核对都遵守这一规则。类型化标量字段仍按其正式类型比较；JSON 字段复用项目 `contracts.json_values.json_equal`，不增加通用序列化器。

| 原申请与本次输入 | 必须结果 |
| --- | --- |
| 完整字段相同，包括 JSON 类型和值 | 保留原请求与 key；阶段已有可靠响应时直接复用，不调用仓储。 |
| 仅 JSON 对象成员顺序不同 | 视为同一输入，复用原请求、key 与可靠响应。 |
| 任意 JSON 字段的布尔与数字不同 | 拒绝替换，原持有责任不变，不能调用仓储。 |
| 其他类型化字段或 JSON 值改变 | 拒绝替换并保留原责任。 |

`test_file_fact_identity.py` 两项纯单元候选围绕 `_save_file_fact` 验证可靠归属响应的原申请同一性；仓储与其他端口受正式接口的 autospec 约束，没有真实 IO。root 独占两项日志 `/tmp/camctl-goal-file-fact-identity-red.log` 为 1 failed、1 passed、0.19s，布尔改数字没有拒绝的分区形成有效红。root 授权后，`_same_file_request` 明确核对四类文件命令的 JSON 字段，其他字段完整保持；`_same_observed_files` 核对条目顺序、全部非 JSON 元数据和精确定位／观察，两个 Loader 定位核对也使用项目 `json_equal`。纯单元矩阵扩展到十一项，覆盖在场、归属配对、完成观察／错误／定位、发现定位与条目观察，以及非 JSON 原时刻改变。实际模块导入、生产导入与 diff 检查通过，等待 root 独占复验。


### 后续阶段：default 入口前置保存与照片未齐备责任

完整目录证据为 root 独占 `/tmp/camctl-goal-combined-capture-current.log` 的 404 passed、7 failed、59.06s，以及 `/tmp/camctl-goal-combined-bootstrap-current.log` 的 501 passed、1 skipped、16 failed、543.56s。完整单元目录由 root 报告 3673 passed、1 skipped。门禁失败按具体责任定位，当前阶段只实施已授权 JSON 比较；不据此修改 v1 集合语义或仓储守卫。

建议实施顺序如下，每项须以真实前提形成有效红后单独授权：

1. 构造 default 正常调度、取消、受限收场乘发现、在场、归属、完成四阶段的保存责任矩阵。原实际 Outcome、条目、T0、完整申请和 key 经公开受理、调度及设备替身形成；故障连接真实关闭重开，并保持同会话集合。COMMIT 后旧 F 只证明可靠已存业务终态；COMMIT 前取消入口须先保存既有实际文件事实，再保存取消生效。不得直接 SQL 制造终态作为准备。
2. 在设备或业务资格筛选前接手共享文件责任。建议由 `session_capture_assembly` 提供仅保存原文件事实的 callback，在 normal `capture_flow`、`cancel_flow`、restricted `winddown_flow` 及适用 residual 入口打开连接后执行，保留现有 WF callback。该步骤不得调用驱动、重新取得条目时刻、处分 raw RESULTS 或重开业务终态；持续失败仍保留各自原申请。
3. 照片可靠完成响应与文件齐备分别判断。文件为空或尚未完成时，原核实责任按已确定预算和间隔继续；后轮增加原责任尝试。当前首轮为空却先 CLOSED 的模型须以真实公开历史覆盖，旧测试的零新增轮次及直接列举不作为目标契约。完成文件的可靠状态改变另设前后两轮输入矩阵，不与空列表案例合并。
4. 两个延时结论恢复用例使用实际持久化 v1 文件输入和公开独立结论，验证零设备查询；存在结论而原完整输入缺失时仍诊断。普通 v1 集合成功保持既有未决事项。
5. 元数据装载沿正常有效状态库的正式投影读取边界核验原 run、活动与已保存尝试关联。具体新增历史正文或引用校验须先由正式资料和实际保证范围定义，不增加任意外部 SQL 篡改假设。

bootstrap 十六项需按对应业务责任继续定位：两项报告配置错误在 `deps.work_files=None` 时读取 callback；分发物替身没有提供完整真实 RESULTS 返回；其余录像、取回协作、受限启动与延时分支目前以超时或已失败业务状态暴露，未逐项证明其根因。它们与 default 文件前置保存以及未决集合成功不能混写成同一缺陷，必须保留当前日志及逐项证据。

### 照片文件齐备的有限核实

本步骤落实既有“采集完成响应不自动证明文件已经写完”和原 `results/<activity_id>` 有限轮次规则，不改变 v1 集合格式。`decide_photo` 的完成响应证明采集已结束，文件评估证明现有任务所需的文件已经齐备。两项共同满足时才允许普通成功收场；动作终态、取消和明确调用失败仍优先使用原分区。

| 普通照片的完成依据 | 必要文件评估 | 下一步 |
| --- | --- | --- |
| 完成后返回契约尚未取得完成响应 | 任意 | 等待原响应，不用文件替代采集完成证据。 |
| 已取得完成响应 | 文件为空、缺少必需照片类别或仍有未完成文件 | 保存本轮实际 RESULTS 与重试等待；保留原流程和次数，间隔到达且有额度后登记新尝试。 |
| 已取得完成响应 | 必需类别、归属与写入完成均满足现有评估 | 结束核实并登记正式产物及动作成功，不再访问设备。 |
| 只发送契约 | 必要文件尚未满足现有评估 | 继续原有限核实，不伪造完成响应。 |
| 只发送契约 | 必要文件满足现有评估 | 使用原文件依据继续正常收场，不增加停止或录像计时。 |
| 原有限核实预算用尽，仍未满足要求 | 已有任意可靠文件事实 | 原责任以无法确认结束，动作失败；保留此前完整且可靠归属的实际文件，不重新查询或重置额度。 |

建议在现有纯判定与 `_photo_handler` 完成本修复，复用 `assess_capture_files` 判定必需照片类别、归属和写入完成，避免两个地方各维护一套齐备条件。

- [x] 为完成响应与文件评估的两个独立维度增加窄单元反例；用公开受理和实际 START 构造空列表、缺少照片类别、未写完到写完、预算耗尽的组件反例。测试逐轮保存真实 v1 输入，不直接修改流程或终态投影。
- [x] root 按测试目录独占确认有效红，失败落在提前关闭 RESULTS、丢失前轮可靠文件或预算结论后的业务收场，不以缺少测试前提代替行为失败。
- [x] 纯判定保留完成响应等待分区，文件不齐时返回继续核实；处理器使用共同评估并保存原结果等待。下一轮沿原责任新增尝试，原结束尝试的完整输入保持。
- [x] 预算结束先装载原已保存 RESULTS 和可靠文件事实，再沿已有无法确认及失败事务收场。仅完整且归属可靠的文件登记为可用产物。
- [x] 空列表恢复用例按现行重试间隔和新轮次计数验证；没有不保存意图的直接列举，也不重开 CLOSED 责任。
- [x] 运行照片定向、既有 RESULTS 文件责任测试及完整单元目录，记录范围和仍失败的验收项。检查点提交由 root 统一执行。

`_photo_handler` 使用共同文件评估及原 RESULTS 元数据，合并前轮仍有效的文件。`_result_file_metadata` 与 CLOSED 装载共用同一原流程的已保存输入，当前实际条目只补入当前消费者评估，不替换原 Outcome。预算耗尽时装载可靠文件；已有 CLOSED/UNCONFIRMED 结论只继续所属业务失败收场，不重开或重复保存该结论。实际失败结果没有文件观察时，完整错误与未知文件情况保持，不能解释为已确认空集合；没有可解释结果且缺少原文件输入时仍诊断。

2026-10-09，Linux x86_64、Python 3.11.16、SQLite 3.53.1：单元反例日志 `/tmp/camctl-goal-photo-unit-red.log` 为 1 failed、10 passed；初始公开照片矩阵日志 `/tmp/camctl-goal-photo-copy-red.log` 包含五个有效照片失败及一项读取结束前提失败。后续预算实际错误日志 `/tmp/camctl-goal-photo-errors-red.log` 为 1 failed、5 passed；原结论后业务收场日志 `/tmp/camctl-goal-photo-conclusion-red.log` 为 2 failed、6 passed。

root 最终独占 `/tmp/camctl-goal-recovery-phase-capture-final.log` 为 107 passed、46.11s，覆盖照片新九项、照片原执行链、内部读取输入、RESULTS 原结果保存／文件四阶段／元数据与媒体原申请。完整单元最终 `/tmp/camctl-goal-recovery-phase-unit-final.log` 为 3687 passed、1 skipped、两项既有 asyncio 标记警告，7.92s。

完整 capture 目录较早日志 `/tmp/camctl-goal-recovery-phase-capture.log` 为 414 passed、6 failed、64.08s。其中完成响应但未核实文件的判定用例已经采用正式继续核实分区，并在最终定向门禁通过；其余两项延时普通成功、原文件未知保存后恢复及两项结论输入准备尚未闭合。上述定向证据不代表完整 capture、完整 bootstrap 或整项第一版实现通过。

### 默认流程对原文件事实的前置保存

`RuntimeDeps.capture_file_observations` 持有已取得文件观察的完整发现申请、原 key、首次响应及尚未完成的在场、归属、完成申请。默认流程每次打开新的 Owned 连接后，先接手这个集合，再检查时钟、动作状态、当前设备或绑定以及本次取消资格。保存已有事实不依赖 `capture_factory` 的设备解析，也不要求原动作仍可被 dispatch。

| 原文件阶段与当前业务事实 | 前置步骤 | 后续业务处理 |
| --- | --- | --- |
| 所选阶段 COMMIT 已完成，但执行者未取得可靠响应；原动作后来经公开取消进入终态 | 新连接沿原完整申请和 key 只读核实首次事务，继续尚未保存的原文件事实，保留原 T0。 | 原动作、取消和 RESULTS 终态保持；不能查询设备或产生新尝试。 |
| 所选阶段 COMMIT 未完成；默认取消尚未生效 | 新连接沿原申请和 key 保存已有实际事实，各子阶段可靠完成后才允许取消入口继续。 | `ApplyCancelTarget` 在前置保存之后执行，不能先造终态再保存未提交事实。 |
| 原请求仍 UNKNOWN 或被仓储拒绝 | 保留原 holder、申请、key、可靠首次响应和已完成子阶段；报 STATE 并停止依赖步骤。 | 不执行取消生效、设备派发或新的 RESULTS，不能用新 key 从头登记。 |
| 当前绑定缺失、配置不匹配或时钟仅允许受限收场，但有原待存文件事实 | 仍先保存原文件事实，不创建或解析驱动。 | 后续设备和业务资格沿各自既有规则判断。 |
| 集合为空或全部事实已可靠保存 | 不增加文件事件或设备操作。 | 正常继续该入口的既有流程。 |

执行步骤如下：

1. 在 `bootstrap/test_file_fact_consumers.py` 沿公开受理、调度、真实 START／STOP 和完整实际 RESULTS 返回建立前提；原文件四阶段分别在真实 SQLite COMMIT 前后返回未知。保留同一会话集合并真正关闭、重开状态库，核对 metadata；不得直接修改动作终态或文件投影。
2. 覆盖 normal、residual、restricted 的四阶段终态后接手，以及 cancel 的四阶段提交前／后顺序。取消终态由公开取消与拍摄收场形成；提交前保持 ACTIVE，由 default cancel 实际生效。用缺失驱动及受限时间证明保存独立于当前设备资格，验证原命令、key、T0、首次响应和完整最终文件事实；设备及 RESULTS 调用次数保持。
3. root 独占确认有效红后，在共同原文件保存 helper 增加只需 Owned、共享 holder 和原仓储的前置接手入口。不得依赖 CaptureRuntime 的驱动构造，不处分 raw RESULTS 或推进普通业务。现有 `_register_observed` 的逐阶段原 key 核实继续复用。
4. normal scheduling、residual、restricted winddown、normal／restricted cancel 均在打开连接后、任何业务筛选及 `ApplyCancelTarget` 前注入同一保存入口。保留 WF／READ 的最新参数、callback 和责任集合，空集合也共用原对象。
5. 持续 UNKNOWN／拒绝分别验证原持有物仍保留、STATE 与零取消生效；单次提交前／后故障恢复验证仅完成必要子阶段。root 分目录执行新矩阵及相关 bootstrap、capture 门禁后独立 review，执行者不运行 pytest 或提交。

原 v1 集合结束、照片齐备模型和历史正文校验属于各自范围，前置文件保存不替代这些契约。

前置保存反例由 root 独占确认，`/tmp/camctl-goal-file-default-consumers-red.log` 为 28 failed、14.43s。正常和取消入口在原责任尚未保存时读取业务墙钟，残留与受限入口直接筛掉已终态对象，没有追加原 key 核实；公开历史、故障和重开前提均已完成。该范围只证明独立文件保存责任，不据此认定完整录像 RESULTS 处分闭合。

root 授权后，既有逐阶段文件保存方法集中到 `handlers.py` 的 `_FileObservationSaves`，`CaptureRuntime` 继续共用这些方法。独立 `resume_file_observations` 只接收 fresh Owned、原文件集合和原调用结果集合，逐原动作保存已取得事实，不构造驱动或取得新时刻。`lifecycle.py` 为 normal、residual、restricted winddown 及 normal／restricted cancel 注入同一集合的前置 callback；`flows.py` 与 `capture/residual.py` 在打开连接后，先执行文件 callback，再执行已有媒体 callback，随后才进入业务筛选。两类 callback 的状态错误保留原责任并报 `StateDbFailure`。

四个生产模块实际导入和 `git diff --check` 通过。root 独占二十八项门禁 `/tmp/camctl-goal-file-default-consumers-green-1.log` 为 28 passed、15.02s。文件前置保存、照片状态机、普通 RESULTS 处分和未决分页分别按各自范围验收。

独立静态审查后，`test_file_fact_consumers.py` 追加受限取消入口的验收，主矩阵增加四阶段乘 COMMIT 前／后八项，UNKNOWN／拒绝矩阵增加四阶段乘两种失败八项，总数为四十四项。未排期取消仍由公开受理产生，受限入口使用实际 `context.restricted_flows['cancel']`；与普通取消共用原请求、key、T0、Apply 顺序及业务／RESULTS 守恒断言。生产接线保持不变，追加十六项属于首次覆盖验证，不记录为已有有效红绿循环。

root 独占装配组合 `/tmp/camctl-goal-recovery-phase-bootstrap.log` 为 108 passed、57.69s，包含四十四项文件矩阵、目录绑定、媒体原申请／取消／退出、读取摘要／绑定以及两项普通读取让路／并行。独立静态审查检查了真实接线、原文件保存方法和 `CaptureRuntime` 字段，未发现剩余确认缺陷；补充的受限取消及照片前轮文件与后轮错误组合均在最终门禁通过。这些证据不包括目标 ARM64、真实设备或物理断电验收，也不表示所有默认工厂的 READ 持有集合已经共享。

本阶段最窄复验入口如下，两个集成目录分别执行：

```sh
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_photo_result_retry.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/bootstrap/test_file_fact_consumers.py -q
```

### 同会话文件登记中断用例的恢复前提

原 INSERT 故障必须精确产生 `ConsistencyError`，原实际结果、票据、原时刻、结果 key、文件完整申请与 key 仍保留。新 Owned 真正关闭、重开后沿同一 `pending_start_results`、`pending_file_observations` 和 `retry_gate` 恢复；没有 holder 的新 runtime 属于另一恢复分区，不能用新设备返回补原尝试。

root 新鲜日志 `/tmp/camctl-goal-recovery-fixture-red.log` 证明旧中断用例恢复动作仍为 ACTIVE。`TestInterruptionRecovery.test_fault_at_file_registration_rolls_back_and_recovers` 改用公开 `consumer_world(photo)`、真实 typed RESULTS 和仓储 spy，核对原输入、原 key／T0、一次设备列举、尝试 ID 守恒及产物恢复。精确捕获故障后真正关闭、重开连接，显式核对三个共享对象和原 holder；生产与其他 fixture 保持。实际模块导入和 diff 检查通过，等待 root 独占用例验证。

### 录像控制、媒体结果与文件核实责任的共同收场

本阶段让正常录像和后续处理分别保存自己的可靠结果，并在必要文件已经齐备时结束原核实责任。正式依据是[录像成功标准](../../architecture/camera-recording.md#录像成功标准)、[文件完成依据与产物检查](../../architecture/camera-capture.md#文件完成依据与产物检查)、[一次查询结束与整项核实结束](../../camctl/database/operation-fields.md#一次查询结束与整项核实结束)及[状态查询与产物核实的配置](../../architecture/configuration.md#状态查询与产物核实的配置)。本节不定义新的结果观察格式，不改变已保存媒体失败、取消或已有终态的语义。

原 `result_files_listed/v1` 和真实 `CallOutcome` 提供本次调用的条目、错误、观察、收场和退出信息。原已保存 RESULTS 元数据及仍有效的文件事实共同提供前轮文件；当前返回没有观察或者带有错误，都不能撤销此前可靠归属及完成事实。文件评估只核对必要 `VIDEO` 类别、逐文件归属及写入完成，不把 v1 当作集合结束证明，也不生成 `set_finalized`、集合 `COMPLETE` 或延时摄影的时间与产物完成结论。

判定使用三个独立维度：原 RESULTS 尝试的实际调用结果、控制及媒体处理的业务判定、可靠文件评估。实际尝试 `FAILED` 与媒体处理 `FAILED` 含义不同：前者保留本次设备调用错误；后者来自已经可靠保存的检查或修复结果。可靠观察可能和本次调用错误同时存在，因此不能用一个“失败”标志替代三个维度。`decide_recording_result` 继续负责既有控制及媒体判定，文件门槛在消费结果的责任边界共同判断；函数划分属于实施建议。

先按下表从上到下处理责任及恢复状态。表中的“继续当前业务判定”才进入后面的控制／媒体与文件决策表。

| 原责任与保存状态 | 必须保存及后续处理 |
| --- | --- |
| 动作已有终态或业务取消已经生效 | 沿[照片有限核实](#照片文件齐备的有限核实)及本计划原条件模型处理原实际返回和文件责任；保持原业务终态，不新增普通核实资格。录像必要停止仍由原停止责任处理。 |
| 原尝试仍在执行，或者实际返回的完整原申请尚未可靠保存 | 等待真实调用及适用收场，或者先沿原 holder、T0、请求全文与 key 完成保存；不以当前投影或新返回替换，不开始下一次 RESULTS。 |
| 已保存重试等待，但本次单调钟尚未到间隔 | 原核实责任保持 `ACTIVE`，不增加尝试；等待不阻塞其他工作。 |
| 原核实责任已有 `UNCONFIRMED`，动作尚未收尾 | 只装载原完整输入及可靠文件，继续业务失败与符合规则的产物登记；不重复关闭原责任，不查询设备，不因新预算重开责任。 |
| 原核实责任已成功结束，必要本地输入完整可解释 | 从原已保存输入继续媒体、产物及动作收场；零新 RESULTS 调用，原尝试与责任终态保持。 |
| 原核实责任已经结束，但原输入不可解释或不能支持后续收场 | 保留诊断和未完成责任，不改写旧终态，不重开原责任，也不直接查询设备补写。 |
| 原责任未结束，实际返回已收场且可以处理完整输入 | 继续当前业务判定；本轮实际状态、错误、观察、收场及 `call_info` 始终原样保存。 |

以下有效分区只适用于上一表允许继续的原未结束责任。文件齐备是 `assess_capture_files` 对可靠合并文件及必要 `VIDEO` 的评估；预算指原 `results/<activity_id>` 在本次配置下的剩余轮次。媒体处理的明确失败优先沿既有失败收场，不为了文件等待增加额外轮次。

| 控制及媒体的业务判定 | 文件评估 | 原预算与真实返回 | 原核实责任及动作结果 |
| --- | --- | --- | --- |
| 已有可靠媒体失败，既有判定为 `FAILED` | 任意；只保留完整且归属可靠的可用文件 | 原实际返回已取得，或者既有可靠本地输入足以继续原失败分区 | 按现有明确失败规则结束原核实责任，登记符合规则的文件并保存既有业务失败；不额外耗尽轮次，不改写实际尝试错误。 |
| 必要检查或修复尚未结束，既有判定为 `PENDING` | 任意 | 本轮实际返回已收场 | 保存原尝试结果及适用等待，原责任继续承担尚未完成工作；已有可靠源输入直接用于媒体推进，不以媒体等待刷新轮次或提前关闭责任。 |
| 既有控制或媒体成功依据成立，判定为 `SUCCEEDED` | 必需类别、归属与完成全部满足 | 本轮实际 `SUCCEEDED`，或者实际 `FAILED` 同时带有足够可靠文件观察 | 原核实责任保存 `SUCCEEDED`，动作沿已有成功判定收场。实际 `FAILED` 尝试仍为 `FAILED`，错误、观察、收场及退出信息保持。 |
| 既有控制或媒体成功依据成立，判定为 `SUCCEEDED` | 空列表、缺少 `VIDEO`、文件未写完或归属未定 | 本轮实际已结束，原责任尚有后续轮次 | 只保存原尝试及 `retry_wait`，不保存成功 `run_finish`；原责任保持 `ACTIVE`。间隔到达且资格成立后，在同一原 run 登记下一次尝试。 |
| 仍需新观察才可完成文件核实，没有先行媒体失败 | 文件要求尚未满足 | 原次数达到本次上限，或最后一次实际失败仍缺必要观察 | 先保存最后一次真实尝试，然后将原核实责任保存为 `UNCONFIRMED`；动作保存 `capture_result_unconfirmed`，保留此前完整可用文件。次数、旧尝试、控制事实及媒体结果保持。 |
| 尚无足以支持成功的控制或媒体依据，也没有明确失败 | 任意 | 原责任仍需继续 | 保持既有待定分区及相应恢复责任，不以文件类别或字节数补造控制完成。 |

保存失败沿本计划照片及[默认文件前置保存](#默认流程对原文件事实的前置保存)的原规则：文件子事务和尝试结果分别保留完整原申请及 key；COMMIT 前回滚与 COMMIT 后 UNKNOWN 都先核实原 key。后轮元数据合并只改变消费者的可靠文件视图，不修改原 `CallOutcome`、原结果 JSON、原返回时刻或已确定处分。必要文件不齐时，不得先关闭原 run 再依靠 `_finish_capture` 的 `files_incomplete` 返回继续等待，也不得为相同活动另建 run。

实施建议集中在 `handlers.py::_advance_recording_outcome` 及其现有 Loader、文件登记和结果保存接口。审计同一不变量的 `_photo_handler`、`_finish_capture`、`_save_winddown_progress`、CLOSED 和预算入口；只在真实数据流需要时调整内部辅助函数，不改变 `decide_recording_result` 的既有媒体失败语义，不放宽仓储守卫。具体机械步骤如下：

1. 在 `integration/capture/test_record_result_retry.py` 用公开 `consumer_world('record')` 建立完整受理、START、STOP、活动结束和控制成功依据。空列表、未完成 VIDEO、完整 OTHER 三分区分别验证第一轮实际成功保存后原 run 保持 `ACTIVE`，等待 3 秒前零新调用，到等号后同一 run 的第二次真实返回满足 VIDEO 并结束。逐轮核对原尝试 JSON、事件引用、次数和文件元数据守恒。
2. 同三分区以两轮上限验证：两次真实观察仍不能满足文件要求时，第三次推进只关闭原责任为 `UNCONFIRMED` 并保存动作失败；零第三次调用，完整 OTHER 保留，未完成 VIDEO 不成为正式产物。再以此前完整 OTHER 加后一轮实际 `FAILED` 分别验证可靠 VIDEO 观察使业务成功，以及无观察使预算失败；后轮错误、假定收场及真实退出码完整保持，前轮文件继续存在。
3. root 在部署 Python 3.11 下独占运行上述新文件，先确认失败来自提前关闭责任或丢失可靠文件，而不是公开历史、守卫、绑定或时钟前提。执行者不运行 pytest。命令为 `PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_record_result_retry.py -q`。
4. root 确认有效红后授权生产修复。消费者先登记并合并原 v1 元数据及可靠文件事实，再按上表确定本轮原处分。只有文件门槛与成功业务判定同时成立才保存成功 `run_finish`；文件未齐保存原 `retry_wait`。预算入口和既有 CLOSED/UNCONFIRMED 入口共同从原已保存输入恢复完整可用文件，再执行相应失败收场。
5. `bootstrap/test_recording_stop.py::TestNormalStopAtTarget` 在真实 STOP 成功后等待原 `results/1` 已保存 `(ACTIVE, attempts_used=1, retry_wait_required=True)`，再加入迟到完整 VIDEO，并将实际受控单调钟推进 3 秒。验证 RESULTS 恰好调用两次、原责任累计两次、停止仍恰好一次，动作与正式产物成功；不能将第一轮改成预先已有完整文件。
6. root 分目录执行新录像矩阵、既有照片矩阵、原 RESULTS 保存／文件四阶段／CLOSED 元数据及媒体保存、取消和退出分区。bootstrap 另起进程运行正常停止、停止重试、预算耗尽、录像取消和受限默认装配；已有未决 v2 分区保持单独记录。实际失败及持续保存故障继续使用原已有反例，不通过删除断言或补造成功结果取得绿色。
7. root 审核实际改动与每个验证范围，追加有效红绿证据，并按用户授权统一提交本阶段所有本地工作。不宣称普通延时摄影集合、全部 READ 工厂交接、完整 bootstrap 或真实设备已经完成。

- [x] 新录像八个组件分区建立完整公开历史，root 确认有效红。
- [x] 八个分区验证录像文件门槛、前轮事实合并及预算失败时的文件保留。
- [ ] 媒体等待后结束原 ACTIVE RESULTS，以及既有明确媒体失败入口，按下述独立阶段核验。
- [x] 正常 STOP 用例等待原重试事实后推进 3 秒，定向 bootstrap 通过。
- [x] 独立只读审查文件门槛、前轮事实及媒体接缝，分别记录证据范围和未完成事项。
- [x] root 完成相关分目录回归；阶段产物纳入本地统一提交范围。

2026-10-09，root 的新鲜两项 bootstrap 日志 `/tmp/camctl-goal-bootstrap-recovery-fixture-red.log` 为 2 failed、33.03s。只读失败库分别证明：正常录像 START/STOP 已可靠成功，第一轮空 v1 输入已被保存且原 RESULTS 已结束，动作仍未收尾；受限默认装配前的正常会话因原驱动与本次配置不一致，没有发起 START。后者的受理目录前提独立修正；本节只处理前者的核实责任闭合，不将二者归为同一生产问题。新增矩阵与生产门禁的结果见下述验证记录。

2026-10-09，root 的 `/tmp/camctl-goal-record-result-red.log` 为八项有效失败，失败来自原责任提前结束或预算收场丢失前轮文件。定向复验日志 `/tmp/camctl-goal-record-result-green-1.log` 为 17 passed、8.30s，执行者已实际读取。新八项使用无需检查的控制完成前提；它们证明文件门槛、有限预算及前轮可靠文件保留，不证明媒体等待与核实责任共同收场。媒体接缝按以下独立阶段继续。

### 后续阶段：媒体等待后的核实收场与明确失败入口核验

本阶段已建立公开组件反例，生产与保存故障验证继续推进。正式规则来源是[录像成功标准](../../architecture/camera-recording.md#录像成功标准)、[录像动作的结束时点](../../architecture/camera-recording.md#录像动作的结束时点)、[一次查询结束与整项核实结束](../../camctl/database/operation-fields.md#一次查询结束与整项核实结束)、[产物核实轮次](../../camctl/database/operation-fields.md#产物结果核实的责任与轮次)、[配置与历史的共同保存](../../camctl/database/operation-fields.md#查询和结果核实怎样保存配置与历史)和[查询与产物核实配置](../../architecture/configuration.md#状态查询与产物核实的配置)。这些规则要求可靠结果足以满足责任时沿原责任收场，保持已结束尝试；明确媒体错误采用既有录像失败判定，必要媒体处理未结束时保持待定。具体接口与文件划分仍是实施建议。

只读审查分别记录两个问题，不能合并为一个已确认生产故障：

| 审查对象 | 当前证据与归因 | 后续交付 |
| --- | --- | --- |
| 完整文件缓存后，媒体先待定、后完成 | 既有缓存路径在首次媒体 PENDING 时保存真实 `AttemptFinish(retry_wait=True)`，可靠保存后释放 raw holder。后次缓存消费没有 held ticket，`_finish_capture` 仅保存动作及 READ 收场，不更新仍 ACTIVE 的 RESULTS。这是静态确认的原有潜伏接缝，尚须公开组件反例证明实际可达；不是文件门槛修复引入的回归。 | 证明媒体等待后沿原 run 完成本地收场，保持原尝试和原处分；以独立权威流程结束入口保存后续决定。 |
| 已有明确媒体失败，但消费者先进入 RESULTS 列举 | 既有非缓存路径先调用 `_listing_round`，在 RETRY_WAIT／IN_FLIGHT 返回时尚未判定已保存媒体结果。是否存在原本地事实已经足够、却增加查询或掩盖失败的合法输入，尚无有效反例；不能将静态推测写成确认故障，也尚不能归为本阶段回归。 | 先证明原输入与检查失败合法、充分，再核消费者是否遵守已批准的明确失败规则；不可达或输入不足的案例保留为边界验证。 |

先区分原实际调用、原核实 run、媒体处理和文件输入四个状态，再选择下面的有效分区。媒体结果由既有 `decide_recording_result` 判定，不能只凭 check／repair 的状态名判动作失败；例如修复失败不自动否定已经成立的控制或时长依据。

| 原责任与保存情况 | 文件与媒体情况 | 应验证的结果 |
| --- | --- | --- |
| 原实际调用未完成适用收场，或已有 raw／文件／媒体完整申请尚未可靠保存 | 任意 | 先完成原调用或沿原完整请求、原时刻与 key 核实保存；不增加 RESULTS，不改原已固定处分。 |
| 原尝试和真实 retry 处分已可靠保存，run ACTIVE | 完整 VIDEO 且其他已观察文件均完整；媒体仍 PENDING | 复用原文件推进必要媒体，动作保持未结束；媒体等待不产生新 RESULTS、run 或名额。 |
| 同上 | 文件齐备；媒体随后 SUCCEEDED | 由可靠已保存事实确定独立后续流程结束决定，并按正式事务边界保存原 RESULTS 收场、产物与动作成功；原尝试、结果 JSON、T0 与 retry 处分历史保持。 |
| 同上 | 媒体随后按既有判定 FAILED | 保存既有业务失败并保留完整可靠文件；原 RESULTS 的结束状态及错误按正式责任规则核实，不能机械复制媒体状态或猜测映射。 |
| 原 run ACTIVE，原调用已可靠结束 | 完整 VIDEO 加未完成 OTHER；媒体尚未结束 | 不建立“全部文件齐备”的缓存。尚需新的文件观察时遵守原预算和间隔；不以完整 VIDEO 忽略其他已观察文件。 |
| 原 run ACTIVE，重试间隔已到，无原未保存调用 | 原可靠输入足以支持既有明确媒体 FAILED；当前内存没有缓存 | 核验能否直接按原输入失败收场，不为形式完整增加 RESULTS；这是待有效反例验证的分区。原输入不足时仍按既有诊断或必要观察规则处理。 |
| 原 run ACTIVE，已有真实 retry 要求且间隔未到 | 没有足以结束责任的已保存决定，仍需新的设备观察 | 保持 ACTIVE 与原等待，零新尝试；不得把这个合法等待当成媒体失败优先缺陷。 |
| 原 run 已 SUCCEEDED 或 UNCONFIRMED | 原完整输入可解释，动作尚未收尾 | 保持原 run、错误、配置和次数，只继续所属本地成功或未确认失败收场；不重开流程。 |
| 原输入不能解释，或不能证明所选本地收场资格 | 任意 | 保留诊断和责任，不能用空文件、默认媒体成功或重新列举来补原输入。 |
| 取消已经生效，或动作已有终态 | 有原待存事实或已形成的后续完整申请 | 按原保存、取消和终态规则处理；不重开普通核实或媒体业务，不改业务终态。 |

独立后续决定必须与原尝试处分分开。`RunFinish` 当前是 `AttemptFinish` 的组成部分；原尝试可靠保存后，不能沿原 key 将 `retry_wait=True` 改成 `run_finish`。`OperationRepository.finish_stale_runs(StaleRunFinish, key, owned)` 能保存伴随流程终态，`CaptureRepository.close_start` 能共同保存 START 和动作，后者明确要求 START 责任。这些接口的存在不等于它们已经满足录像 RESULTS 的共同事务边界。执行者先核对实际守卫、原 activity 身份、状态／错误映射、动作和产物共同保存要求，以及 COMMIT 未知时的完整请求重送；如果现有正式入口不足，须把所缺输入、事务成员和语义提交给 root 确认，在确认前不实现新入口、不用分散事务绕过。

建议新增 `apps/camctl/tests/integration/capture/test_record_media_result_settlement.py`，独立验证真实仓储与录像消费者。生产预估涉及 `capture/handlers.py::_advance_recording_outcome`、必要的 `operations/attempts.py` 类型和 `persistence/repositories/capture.py` 事务入口；只有新增后续申请确需跨 runtime 持有时，才评估对应共同集合及 bootstrap 接线。不得顺带修改照片、READ／WF 的既有守卫、v1 观察格式或未决 v2。

#### 任务一：媒体等待后原 ACTIVE 核实责任的收场

2026-10-09，Linux 容器、Python 3.11.16：root 的 `/tmp/camctl-goal-media-settlement-red-2.log` 为 4 failed、2.56s。公开受理、实际 START／STOP、完整文件和真实 READ 已保存；工具第一次未执行，随后返回 61 秒或 5 秒的真实检查观察。同一 runtime 和真正重开 Owned／runtime 两种入口都保存了正确业务终态、完整原片且零新增设备调用，原尝试和历史前缀保持；唯一失败是原 RESULTS 仍为 ACTIVE、retry 为 1。首次日志因测试查询不存在的历史列失败，不计行为红。

只读核对 `operation-fields.md` 与 `transactions.md` 后，完整可靠文件且合法媒体终局的两个分区具有唯一结果：RESULTS 为 `SUCCEEDED`、无流程错误，业务按原媒体判定成功或失败。root 允许新增内部窄复合请求 `FinishRecordingResults(capture: FinishCapture, run_id: int)` 与 `finish_recording_results`，共同保存原流程结束、动作及适用产物；具体实现仍可调整。原尝试已保存的 retry 处分不改。后续申请使用新 key 和 T1，必须核唯一 action／activity／run、原调用已实际结束、文件与媒体资格，并在重送时核完整复合输入。已有 run 终态保持，取消与业务终态按原保存优先级处理。现有通用 `_reuse` 未核的同批原片关联与 responsibility key，不能替代新复合分区的完整输入核实。

root 的 `/tmp/camctl-goal-media-settlement-save-red-2.log` 为 4 failed、10 passed、8.22s：共同事务四项和原键改变 run、action、T1、归属资格、完成资格及摘要的六项通过。四个提交前／后 UNKNOWN × 媒体成功／失败分区均使用真实 COMMIT 故障代理，关闭原连接后由 fresh Owned 证明原 key 的可靠保存状态。提交前重入生成了新 T1／key，提交后因已有业务终态遗漏原 key 核实；因此完整请求的持有与恢复尚未闭合。首次保存故障日志只先暴露旧 AssertionError；第二次测试继续核原可靠事务与恢复，并保留最终必须报告 ConsistencyError 的断言，不放宽通过条件。

#### 当前交付范围与验证

后续收场请求在首次仓储写入前保存为 `PendingRecordingResults(key, request)`，原 key、完整 `FinishRecordingResults` 与 T1 保持；原 AttemptFinish 的 T0、实际结果和 retry 处分不改。`CaptureRuntime`、默认普通／残留／受限工厂共用同一会话集合。`_record_handler` 在取消、绑定和终态判断前核原请求；三个默认入口在读取候选和构造 runtime 前，通过现有保存前缀调用纯仓储恢复，不取得新时刻或设备、媒体资格。只有可靠保存完成才清除 holder 与原 retry gate。

复合仓储同时核原 action／activity／RESULTS、完整文件、源文件和适用媒体终局，沿原 `check_basis` 解释控制依据，调用公共 `decide_recording_result` 判定业务结果。请求的 failure 必须与该判定的 code／details 一致；媒体失败不改变完整文件核实的 `SUCCEEDED`。根新增两项首次申请反例 `/tmp/camctl-goal-media-settlement-business-red.log` 为 2 failed、1.49s，证明 5 秒原片不能提交业务成功，61 秒原片不能提交 `recording_too_short`；它们与原键重送改变输入的六项分别覆盖首次决定和已保存身份。

默认入口的新三项由真实普通工厂产生原请求与提交后 UNKNOWN，关闭原连接后确认动作已终态，再分别调用普通、残留和受限 flow。`/tmp/camctl-goal-recording-default-settlement-red.log` 为 3 failed、2.13s，均遗漏原 key 核实；前置接线后，在原候选不再包含动作且没有构造新 factory 的情况下完成同一完整请求与 key 的核实。

2026-10-09，Linux 容器、Python 3.11.16，root 前台独占且 capture 与 bootstrap 分进程执行：

- `/tmp/camctl-goal-media-settlement-capture-green.log` 为 46 passed、20.75s。组成是新媒体十六项（普通收场四项、COMMIT 前后 UNKNOWN 四项、原键六种输入变化、首次业务输入相反两项）、活动身份四项、录像轮次及原键十七项、媒体接线五项、正式结论恢复四项。
- `/tmp/camctl-goal-recording-settlement-bootstrap-green.log` 为 50 passed、36.93s。组成是新默认入口三项、既有默认 READ 交接十五项与保存门六项、媒体原申请十二项、媒体取消保存顺序四项、本地媒体收场六项、正常停止四项。
- `/tmp/camctl-goal-recording-settlement-unit.log` 为 3687 passed、1 skipped、2 warnings、7.74s。两个 warning 是既有同步测试的 asyncio 标记，分别位于 `unit/devices/test_read_session.py` 和 `unit/operations/test_process.py`。
- 全 capture 回归 `/tmp/camctl-goal-recording-settlement-capture-full.log` 为 457 passed、2 failed、82.83s。失败仍是 `TestTimelapseHandler::test_send_wait_then_finish` 与 `test_backward_wall_clock_change_does_not_extend_current_session_wait`，正常集合成功语义属于未确定的结果格式范围；本次未新增失败，也不把整个目录声明为通过。

独立只读审查核对新源文件、原 run 身份、原尝试守恒、共同事务、实际媒体判定、完整请求的保存失败与恢复及默认保存前缀，有限范围内未发现阻断项。取消入口尚未接入录像申请的保存前缀；原后续事务不存在而取消先保存的相遇分区、默认入口提交前／媒体失败／持续保存拒绝、复合预览或修复产物关联重送仍须独立验证。任务二的明确媒体失败优先路径、完整 VIDEO 加未完成 OTHER 的新媒体控制分区、原 raw READ End 与 v2 均未在此范围内完成。上述绿色不作为整个媒体核实计划完成的依据。

- [ ] 读取上述正式规则、`AttemptFinish`／`RunFinish`／`StaleRunFinish` 及其真实事务守卫，记录独立结束入口能否同时覆盖 RESULTS、适用动作与产物。如果状态映射或事务成员没有唯一正式依据，先报告未决，停止生产设计。
- [ ] 沿公开 Acceptance、Scheduling、实际 START／STOP、活动结束及正式处理仓储建立需要媒体处理的录像。可复用 `media_retry_fixtures.py::media_pipeline` 的完整历史方式，不能直接修改 processing 状态或移除守卫。使用真实媒体端口与受接口约束的工具替身，让完整 VIDEO 的第一轮 RESULTS 已保存、媒体仍 PENDING；另设完整 VIDEO 加未完成 OTHER 的文件控制分区。
- [ ] 在 PENDING 点保存原 activity／run／ticket、原结果事务 key 和全文、事件引用、T0、次数、配置与等待状态快照。确认 run ACTIVE、真实 retry 已可靠保存、raw holder 已释放。后次通过真实媒体链得到合法 SUCCEEDED 或既有判定 FAILED，分别断言无新 START／STOP／RESULTS、无新 run／attempt、正式文件与业务结果正确，原尝试及原处分不变；原 ACTIVE RESULTS 必须沿已确认入口结束并清除等待。
- [ ] 对同一 runtime 继续与 fresh Owned／runtime 本地装载分别覆盖。真正关闭和重开连接，核对 metadata；同会话继续保留实际 pending 集合。没有 holder 的跨进程路径只能使用已可靠历史，不能伪造原 RawOutcome 或比较旧进程单调钟。
- [ ] root 独占运行候选反例，确认失败确实是原 ACTIVE RESULTS 未收场，或本地恢复新增查询；fixture／导入／守卫错误不能记为行为红。root 确认有效红与正式接口后，才授权最窄生产修改。
- [ ] 独立后续结束申请首次确定时持有完整输入、新责任 key 与该决定时刻 T1；原 RESULTS 的 T0 始终保持。以已有故障代理分别注入提交前回滚、提交后 UNKNOWN，再用 fresh Owned 沿同一完整申请及 key 恢复。断言原 attempt 不变、原后续 key 对应唯一事务、失败时保留责任并报 STATE、恢复零设备调用；不能在未知时重新取得 T1 或新建 key。
- [ ] root 独占绿灯后审计缓存与无缓存本地路径，并检查“结果收场已保存、动作尚未收尾”和“动作已终态但原申请仍需核实”窗口，证明共同事务边界及终态保持。单一缓存案例通过不作为恢复链完成证据。

#### 任务二：明确媒体失败入口的有效反例核验

- [ ] 先用公开媒体链保存真实检查结果，分别构造时长不足与明确媒体问题，使既有 `decide_recording_result` 确实返回 FAILED。同时核对 source、原文件身份、完整／归属、原 RESULT 输入与历史引用；无需检查的正常控制世界不能直接改为已检查失败。
- [ ] 在原 run ACTIVE、间隔已到、无待存调用和无内存缓存的前提下，核验原可靠输入是否足以本地收场。若检查需要的 host copy 已完成其责任，必须用正式清理／保留入口建立该状态并证明仍合法；若合法流程无法形成候选输入，记录不可达依据，不用直接 SQL 制造缺失 READ 资格。
- [ ] 对合法且充分的输入，用真实 `DriverResultListing` 与受契约约束的 driver spy 验证零新查询；再设先前已取得的完整实际 FAILED（无文件观察）等待原 key 保存的分区，核对完整错误、收场、退出信息和 T0 不丢失，已有媒体失败与完整可靠文件保持。不能为了提供 current FAILED 再主动查询，也不能把它解释为空集合。
- [ ] 保留三个控制分区：原输入不足且确需观察、原调用仍在执行／保存未定、已 CLOSED／UNCONFIRMED。分别断言既有诊断／调用收场／终态规则不被媒体优先绕过；修复失败而已有独立成功依据的案例继续采用既有判定。
- [ ] root 确认真实前提并运行反例。失败若证明充分原输入仍被新查询阻挡，才登记确认缺陷、区分原路径潜伏问题与文件门槛改变产生的可达影响，并授权生产修改；首次即通过则记录该分区已经满足规则。不能预先给该项填写红绿结论。
- [ ] 授权后在共同消费边界按原可靠处理结果与文件事实选择合法收场分区，复用任务一核实的独立权威入口；实际未保存请求先核原 key，当前业务判断不能改变其已固定处分。保留原取消、终态、预算、READ／WF 与明确媒体失败语义。

两个任务的最窄集成门禁建议如下，由 root 在部署 Python 3.11 下独占执行；新文件须先建立再运行：

```sh
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_record_media_result_settlement.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_record_result_retry.py apps/camctl/tests/integration/capture/test_recording_media_link.py -q
```

- [ ] root 根据实际生产影响补跑原 RESULTS 保存、文件四阶段、元数据／CLOSED、媒体原申请和取消／退出门禁；若修改共享 runtime 或默认装配，再独立执行对应 bootstrap 目录，不与 capture 混在一次进程中。
- [ ] 独立审查沿原输入、媒体派生、独立流程结束、保存失败、同会话／已保存历史恢复直到原 run 与动作的可观察终态，分别记录两任务证据与仍未决接口。执行者不运行 pytest 或提交；root 按用户授权统一验证和提交。

完成条件是任务一已用真实媒体等待后完成及保存故障证明原责任收场，任务二已对合法充分输入形成有效反例与修复证据，或者形成可核验的不可达／已满足规则结论。正常文件门槛八项绿色不能替代上述证据；第一版集合结束、分页／v2、全部 READ 工厂恢复及真实设备验收仍属于各自未完成范围。

### 预算收场的动作、活动与原责任身份

拍摄动作的编号和设备活动的编号属于不同对象。例如，公开受理先建立一个报告动作，再建立拍摄动作时，拍摄动作可以为 2，其唯一设备活动为 1。预算收场请求携带 `action_id`，仓储先通过该动作的唯一活动取得真实 `activity_id`，再定位 `results/<activity_id>`；不能把动作编号直接拼进核实责任键。

正式依据是[操作流程的身份与参数](../../camctl/database/operation-fields.md#操作流程的身份与参数)、[产物核实责任与轮次](../../camctl/database/operation-fields.md#产物结果核实的责任与轮次)、[一次查询结束与整项核实结束](../../camctl/database/operation-fields.md#一次查询结束与整项核实结束)及[数据归属与原子更新](../../camctl/database/common.md#数据归属与原子更新)。核实 run 的 `kind` 必须为 `CHECK_CAPTURE_RESULTS`（第一版存储值 7），`action_id` 必须是活动所属动作，`activity_id` 必须是所定位的实际活动；流程错误和动作错误里的 `details.activity_id` 指该实际活动，历史 owner（归属对象）仍指拍摄动作。活动身份、业务动作和历史归属分别核对。

| 请求与当前事实 | 仓储处理及应保持的事实 |
| --- | --- |
| 原预算已耗尽，原 run 仍 PENDING／ACTIVE，同 action 的活动与 role 7 责任一致 | 关闭原 run 为 UNCONFIRMED，并清除 retry 要求；错误指实际活动，次数、尝试和配置保持。 |
| 照片或延时摄影采用适用集合收场入口 | `_CloseResultCheckCommand` 将原 run 与正式 UNCONFIRMED 集合结论共同提交；不将录像送入此分区。 |
| 录像采用仅流程收场入口 | `_ResultRunCloseCommand` 仅结束原 run，不生成集合完成／未确认结论，录像采集判定字段保持原规则。 |
| 同 key 的完整申请已经提交，原 run 已终态 | 先核原事务，恢复首次可靠结果；零新事件、零新尝试、零设备查询，不因终态重复执行首次关闭。 |
| 同 key 重送改变原事实时刻 T0 | 拒绝该申请，原历史、错误、尝试与终态保持。 |
| 首次保存不能找到所属活动的 role 7 run，或 action／activity／kind 关系不一致 | 保留诊断并拒绝写入，不以同编号、任意其他 run 或新责任代替。 |
| 原 run 已终态，但申请没有可复用的原事务 | 维持已有终态及次数，不能作为首次关闭再次保存。 |

现有照片预算入口、普通延时摄影预算入口和取消延时摄影的适用核实入口都经过 `_close_check_unconfirmed`，使用 `_CloseResultCheckCommand`；录像预算入口使用 `_ResultRunCloseCommand`。这两个旧仓储入口共同依赖同一 action→activity→原 RESULTS 身份不变量。原错误构造将两个编号视为相同，属于旧有潜伏缺陷；同编号前提不能证明该不变量。

root 的实现将定位收敛到 `persistence/repositories/capture.py::_result_run_of_action(connection, action_id)`。该 helper 调用 `load_activity_of_action` 后核对原 run 的 action、activity 和 kind；两个预算收场命令复用该定位。流程错误按真实 activity 构造，首次事件的 run owner 仍为所属 action。录像仅流程收场的同 key 重送还核对保存事件中的 run ID；集合收场的重送复用正式集合申请核对。`handlers.py::_unconfirmed_failure(runtime, action_id)` 同样从原活动取得错误 ID，照片、录像和延时预算直接失败调用使用此 helper。此处记录实际责任边界，不将 helper 名称作为正式规格。

执行与验证按以下粒度跟踪：

- [x] 公开 `consumer_world(..., independent_activity=True)` 在拍摄前受理报告动作，沿实际 START／STOP 和 RESULTS 建立动作 2、活动 1 的合法历史，无直接投影补写。
- [x] 录像空文件、未完成 VIDEO、完整 OTHER 三个独立活动预算反例进入真实消费者，root 确认有效红：`/tmp/camctl-goal-record-activity-red.log` 为 3 failed、8 passed、5.67s。三项均在已保存两轮后关闭原责任时错误地查找 `results/2`。
- [x] root 在两个预算仓储入口及预算动作错误 helper 实施真实活动定位；独立只读审查核对原责任、kind 7、动作历史归属、终态前的原事务重送及 changed T0 拒绝。已读取 `/tmp/camctl-goal-record-activity-green-2.log`：35 passed、7.35s。
- [x] `test_record_result_retry.py::test_result_budget_close_uses_original_activity_on_resend` 新增照片／延时摄影／录像乘同编号／独立活动六个公开组件分区。真实 `_listing_round` 保存原尝试和 retry 后，使用原 T0 与单个预算收场 key；断言真实活动错误、同 key 复用完成、改变 T0 拒绝、历史与原尝试守恒、一次 RESULTS。这六项属于新增首次覆盖，不能记录为六项既有红绿循环。
- [x] 六项首次覆盖通过；`/tmp/camctl-goal-read-recording-capture-target.log` 为 138 passed、52.94s，覆盖录像文件门槛、预算、原身份重送、结论恢复、照片有限核实、原 RESULTS 与文件事实、媒体原申请及输入取得。直接仓储六项不等于全部拍摄消费者或恢复收场已经通过。
- [x] 已保存 UNCONFIRMED 后的延时摄影本地业务收场补入独立活动反例：公开建立活动、正式原文件与未确认结论，fresh Owned 后零新查询，核原 run／尝试不变和动作错误里的实际 activity。`/tmp/camctl-goal-conclusion-activity-red.log` 为 1 failed、3 passed、2.27s，确认独立活动的业务错误编号不符；该消费者复用原活动错误构造后，`/tmp/camctl-goal-record-final-narrow.log` 为 26 passed、10.55s，包含四项同编号／独立活动的正式结论恢复、十七项录像核实及五项原媒体接线。
- [ ] 剩余四处启动／照片／CLOSED 明确失败错误按下述独立组件矩阵核实真实活动字段；它们不是预算定位或六项首次覆盖的完成证据。

最窄验收命令由 root 独占执行：

```sh
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_record_result_retry.py -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_result_confirmation.py::TestConclusionRecovery -q
```

本段完成范围是两个预算仓储入口的原责任身份定位和已核验的直接预算错误；原文件与尝试保存、动作后续收场、媒体等待后的独立 RunFinish、普通延时摄影集合完成分别沿各自阶段验收。生产与测试由 root 维护，本次独立审查只追加计划。

#### 剩余四个失败分支的实际活动身份

本阶段只核对错误对象身份。规则来源是本节的动作／活动关联、[录像启动事实](../../architecture/camera-recording.md#录像启动的历史事实)、[延时摄影的发送与等待](../../architecture/camera-capture.md#命令返回含义与等待起点)、[产物完成依据](../../architecture/camera-capture.md#文件完成依据与产物检查)和公共 `protocol/errors/workflow-codes.json` 中的既有错误定义。原错误 code、reason、动作失败语义和实际调用结果均保持，目标是让 `details.activity_id` 指向实际 activity，而不是保存这些错误的 action。

`test_capture_failure_activity_identity.py` 作为唯一新增组件测试文件，同时容纳此矩阵所需的公开前提。测试受理一个报告动作和一个拍摄动作，使拍摄 action 为 2、activity 为 1；随后公开观察窗口和开始动作，由真实 `_control_call` 与受 `ControlDriver` 约束的替身产生 START 返回。已保存 START 不再改写。第三、四分区经真实 `DriverResultListing` 消费原完整 `CallOutcome`，文件登记与业务终态使用真实仓储。

| 生产分支与有效输入 | 原错误与失败语义 | 原事实与恢复断言 |
| --- | --- | --- |
| `_settle_start_without_sent_at` 的 FAILED 分支：延时 START 实际 FAILED、效果 UNKNOWN、无确认观察，活动没有 sent_at | 动作 FAILED，`capture_failed`（公共动作错误 13），reason 为 `device_failed`；保持实际通信错误和启动 run 的既有 FAILED。 | 错误 activity 为 1；原 START 尝试全文、时刻与历史保持；不执行第二次 START 或首次 RESULTS，不补发送时间，不释放未知占用。 |
| 同 helper 的未确认分支：START 实际 SUCCEEDED，但效果 UNKNOWN、没有 timelapse_sent 观察且没有 sent_at | 动作 FAILED，`capture_result_unconfirmed`（12），reason 为 `start_unknown`；原开放 START 按既有规则 UNCONFIRMED，原成功调用不改为失败尝试。 | 错误 activity 为 1；保持实际成功响应、UNKNOWN 效果和退出信息；零新调用和尝试，原发送时间仍为空，占用保持。此输入不是伪造的 RUNNING 调用，也不替换原 Outcome。 |
| `_photo_handler` 的 FAILED_KEEP_FILES 分支：照片实际 START FAILED、效果 UNKNOWN，随后 RESULTS 带真实完整 PHOTO | 动作 FAILED，`capture_failed`（13），reason 为 `device_failed`；真实完整且可靠归属的文件成为正式产物，已有通信失败不被文件成功覆盖。 | 错误 activity 为 1；第一次 RESULTS ticket 目标为 1，原 START 与 RESULTS 内容保持，文件归属 action 2；终态重入零第二次列举／启动和新历史。 |
| `_finish_timelapse_conclusion` 的明确不满足分支：实际 START、正式等待完成、原 RESULTS 文件已保存，独立正式 ResultSetSave 为 UNSATISFIED | 动作 FAILED，`capture_failed`（13），reason 为 `no_outputs`；不改正式结论、原 run 或其尝试，仍保留符合规则的完整文件。 | fresh Owned／runtime 消费原已保存结论，零新设备查询；错误 activity 为 1、产物归属 action 2；原完整尝试、T0、结论及历史前缀保持。独立结论与原返回共同保存，不由 v1 推导集合结束。 |

机械执行步骤如下，测试准备错误不作为行为红：

- [x] 实际读取根规则、测试守卫、四个消费分支及公共错误定义，核清上述四个输入和机器错误值；测试通过改变错误中的活动目标，可以独立证伪编号混用。
- [x] 新文件显式注册 acceptance、window、operation、capture、outputs 和 timelapse 守卫，沿公开事务建立前提。无发送时刻的两个用例在 START 已可靠保存后 snapshot 全部 `operation_attempts` 与历史，再真正关闭、重开 Owned 并核 metadata，最后调用真实延时 handler。
- [x] 照片用例保存实际失败 START 后，提供一次真实完整 PHOTO 列举；核原尝试和历史前缀、真实文件完成／产物关系，再重开连接验证终态重入无副作用。延时用例经正式 ScheduleWait／WaitCompletedSave 建立等待完成，再以原 listing T0、原结果 key 保存 UNSATISFIED／known_failure 与尝试；新 runtime 仅本地收场。
- [x] 新模块在部署解释器的独立 Python 进程实际导入成功，未调用 fixture 或测试 case；`git diff --check` 通过。此项只证明准备可导入，不是四项行为红或绿色。
- [x] root 独占执行四项，核对错误 code／reason 已正确而实际活动编号仍为 2，或明确指出其他真实业务失败。若 fixture、导入或守卫错误先失败，先修前提并复跑；四个编号错误分别取得有效红后才授权生产。
- [x] 授权后仅在四个错误构造的共同身份边界使用原 `_activity_id_of`，保持原错误结构、原因、原 Outcome、动作结果、文件保留和占用语义；不得新增错误字段、改写既有 START／RESULTS、重查设备或推断 v1 集合结束。执行者不自行改其他业务分支。
- [x] root 独占复验新四项及既有预算身份／正式结论恢复，核原尝试与原历史不可变、动作错误和历史 owner 分别指 activity／action。静态审计相同错误对象的其余构造点；若发现其他可达身份混用，另列准确分区和有效反例，不扩大 v2 或故障模型。
- [x] 独立 review 核对每个有效红、实际窄 diff 和绿色范围，root 更新证据并纳入当前统一提交范围。

新增四项的准确门禁为：

```sh
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture/test_capture_failure_activity_identity.py -q
```

2026-10-09，Linux 容器、Python 3.11.16：首轮 `/tmp/camctl-goal-failure-activity-red.log` 的照片和 CLOSED 分区为有效身份红；两个 START 分区因测试未包含已登记 `format_version: 1` 而先失败，不计行为红。补齐完整输入断言后，root 的 `/tmp/camctl-goal-failure-activity-red-2.log` 为四项有效活动身份错误，4 failed、2.16s。生产四处错误仅取得实际 activity。root 的 `/tmp/camctl-goal-failure-activity-green.log` 为 25 passed、11.84s，包含新四项、录像轮次与原键重送十七项及正式结论恢复四项；原尝试、文件与历史保持验证通过。独立审查继续核相同身份不变量，不将此范围扩大为全部媒体或 READ 恢复完成。

2026-10-09，Linux 容器、Python 3.11.16：录像八项文件反例在 `/tmp/camctl-goal-record-result-red.log` 均因原责任提前结束失败；首轮录像／照片组合 `/tmp/camctl-goal-record-result-green-1.log` 为 17 passed、8.30s。正常录像停止与受限默认装配 `/tmp/camctl-goal-recording-stop-green-1.log` 为 5 passed、13.96s。公开结论恢复的事实时刻沿原 listing T0，完成依据引用真实等待完成事件；同会话中断恢复保留原调用、文件申请及 key。

相关范围扩大后的 `/tmp/camctl-goal-recording-capture-full.log` 为 426 passed、2 failed、67.16s。两个未通过用例是 `TestTimelapseHandler::test_send_wait_then_finish` 和 `test_backward_wall_clock_change_does_not_extend_current_session_wait`，其正常集合成功需要本计划仍未确定的结果格式。该全目录结果早于新增独立活动及直接重送九项，也早于随后 READ 接线；最新窄门禁补充上述新增分区，不能把两者合称最终全目录通过。全单元 `/tmp/camctl-goal-read-recording-unit.log` 为 3687 passed、1 skipped、2 warnings、7.95s；两个 warning 来自既有同步函数的 asyncio 标记。最终 bootstrap 组合 `/tmp/camctl-goal-read-recording-bootstrap-final.log` 为 155 passed、92.60s，覆盖默认 READ 交接与保存门、原读取执行、必要摘要和绑定、文件前置保存、媒体原申请与取消、正常停止及受限默认装配。媒体缓存等待后的独立责任收场及完整 READ 恢复仍按各自未完成任务推进。

### 已形成录像终态申请的取消恢复

本节只处理已经形成的 `PendingRecordingResults(key, request)`。实际 RESULTS 尝试及其完整结果、文件事实和适用媒体结果已经可靠保存，随后拥有者形成完整 `FinishRecordingResults(capture, run_id)`；这份后续申请准备共同结束原 RESULTS、登记正式产物和保存业务终态。原申请的 T1 与 key 在首次保存前固定，原尝试的 T0、真实结果和等待处分属于另一项已经保存的责任。本节的“退役”表示放弃这份尚未提交、已经失去普通完成资格的后续申请，不表示丢弃实际调用结果，也不表示取消收场已经完成。

正式依据是[事务的公共完成与失败契约](../../camctl/database/transactions.md#公共完成与失败契约)、[持久化调用结果分类](../../camctl/persistence-runtime.md#持久化接口契约)、[录像动作的结束时点](../../architecture/camera-recording.md#录像动作的结束时点)、[camera_record 取消](../../architecture/camera-recording.md#camera_record-取消)、[取消动作的完成时机](../../architecture/task-cancellation.md#取消动作的完成时机)及[一次查询结束与整项核实结束](../../camctl/database/operation-fields.md#一次查询结束与整项核实结束)。这些规则已经确定：原事务可靠存在时核对完整输入并复用；原事务可靠不存在且不会迟到提交时，按原业务资格恢复。目标取消先可靠保存时，尚未提交的普通录像结束不登记产物；已有业务终态保持。取消计划被受理不代表目标取消生效，不能仅凭出现新取消计划退役普通申请。内部 disposition（处理结果标识）和 callback（回调）名称是实现建议，不新增业务状态或历史格式。

#### 原键、目标事实与申请处理

下表的 F 表示 holder 持有的原后续 key。可靠缺失要求原数据库工作已经结束、旧事务不会迟到提交，并在可用连接的同一写事务中核对 F 与目标当前事实。仅在旧连接上查不到 F，或仅因原等待协程退出，不能证明可靠缺失。表内目标必须是请求所属的原录像动作，run 必须是其实际活动的唯一 RESULTS；结构、归属或原键输入不一致先拒绝，不能套用退役。

| F 与数据库工作情况 | 目标当前事实 | 处理申请及 holder | 原事实与后续资格 |
| --- | --- | --- | --- |
| F 可靠存在，完整输入、T1、阶段及目标均相同 | 当前终态与原完整事务一致 | 先严格复用原事务，可靠完成后移除 holder。 | 保留原终态、正式产物、RESULTS 和原尝试；不取得新 T1 或 key，不重复外部工作。 |
| F 可靠存在，但输入、T1、阶段、目标或事务成员不一致 | 任意可可靠读取的状态 | 保留 holder，报告状态库错误，不按取消或终态跳过原键核实。 | 不改变原事务或另建替代申请。 |
| F 可靠存在，但投影声称目标仍 RUNNING，或声称原终态后来被其他终态覆盖 | 与完整事务不一致 | 按一致性错误停止，保留申请供诊断。 | 这不是正常业务分区；终态不能被普通完成或取消重新打开、覆盖。 |
| F 的存在性、完整事务或目标事实无法可靠读取 | 任意状态 | 保留完整申请，报告状态库错误，停止依赖步骤。 | 不补造结果，不发布或按废弃内容删除文件。 |
| 暂未查到 F，但原数据库工作仍可能迟到提交 | 任意状态 | 继续核实原实际数据库结果，不退役、不重做。 | 调用协程取消不代表事务未执行。 |
| F 可靠缺失且不会迟到提交 | RUNNING，取消标记为 0，原普通完成资格仍成立 | 沿原完整申请、T1 和 F 保存共同终态；可靠完成后移除 holder。 | 原尝试、次数、配置和媒体观察保持；RESULTS、适用产物和业务终态仍共同提交。新取消仅被受理时也使用这一分区。 |
| F 可靠缺失且不会迟到提交 | RUNNING，取消标记为 1 | 返回可靠退役结果，移除这份普通 holder。 | 不登记产物，不把 RESULTS 改为 SUCCEEDED，不清除原等待；交原取消拥有者推进停止及适用收场。 |
| F 可靠缺失且不会迟到提交 | 已有 SUCCEEDED、FAILED、EXPIRED 或 CANCELED 终态 | 返回可靠退役结果，保留现有动作、父计划和产物结果，移除这份普通 holder。 | 不要求旧普通申请与其他权威事务的终态或文件集合相同；不重新结束原 run，也不从取消文件生成正式产物。仍适用的独立收场继续由原责任负责。 |
| F 可靠缺失且不会迟到提交 | PENDING、未知状态，或 action／activity／run 身份不符合本节前提 | 拒绝这份不符合原录像后续责任的申请，保留诊断。 | 不能将身份错误或不合法状态当作取消退役。 |

F 可靠缺失、已有终态时，内部复合入口可以统一返回 `FinishDisposition.RETIRED`，并交付当前 action／plan 状态及现有 output IDs。该标识只确认旧普通申请已经失去资格；不能表达“F 已保存”或“F 已存在”。通用 `FinishCaptureCommand` 对新键且终态、文件集合一致的 `ALREADY` 业务结果恢复继续保持原契约；本节不修改它。同一 F 已保存时仍严格核原完整事务，不因当前终态而提前退役。退役自身只读取可靠事实，不产生新历史或变化目录；原取消或原终态事件仍是失去资格的权威依据。

#### 默认入口与执行顺序

五个默认入口都必须在新业务资格、候选过滤及 factory 构造前处理已有 recording holder。专用录像恢复仅使用仓储、原完整申请和原 key，不取当前时钟、驱动、源文件或媒体执行资格。新取消尚未生效时，它可以沿表中 RUNNING／cancel=0 分区保存原普通申请；随后取消按目标已终态的规则处理。新取消已经生效时，它只退役旧普通申请，然后让原取消链继续。

| 默认入口 | 原申请核实位置 | 核实或退役后的业务处理 |
| --- | --- | --- |
| 普通 scheduling flow | 现有 `_resume_read_requests` 保存前缀中的录像恢复，在普通拍摄候选及 factory 之前。 | 已终态目标不重新构造 runtime；RUNNING／cancel=1 目标继续原取消拥有者。 |
| residual flow | 同一保存前缀，在残留候选和 factory 之前。 | 仅继续原残留责任；退役不恢复普通启动或 RESULT 查询资格。 |
| restricted winddown flow | 同一保存前缀，在受限候选及 factory 之前。 | 仅执行既有安全收场；不重新读取、核验或修复视频。保存原已形成申请不重新形成媒体决定。 |
| 普通 cancel flow | 新增专用 recording 恢复 callback，在取消动作候选、开始和目标生效之前。 | 已保存普通终态保持；已生效取消交原拥有者收场，取消请求自身按逐项实际结果完成。 |
| restricted cancel flow | 复用相同专用 callback，在未排期取消候选之前。 | 不扩大受限执行范围；目标事实和原键不可靠时阻止新增取消结果。 |

直接 `_record_handler` 仍在 terminal／binding／cancel 判断前恢复原 recording holder。cancel 接线使用专用 `resume_recording_results` callback，不同时接入尚未定义的 READ 取消前置模型。数据库结果不是 COMPLETED 时保留 holder 并由既有边界转换为 `StateDbFailure`；可靠 RETIRED 时移除 holder，但不因普通申请退役清除 retry gate 或写入原 run。已保存 F 的正常核实仍按原可靠流程结束清除等待。

#### 任务一：原普通申请的可靠退役与五个入口

生产建议范围为 `persistence/repositories/capture.py::_FinishRecordingResultsCommand`、`capture/handlers.py::resume_recording_results`、`bootstrap/flows.py::cancel_flow` 及 `bootstrap/lifecycle.py` 两种 cancel 接线。结果标识可使用现有 `FinishDisposition` 增加 `RETIRED`；如果采用等价类型，必须保留“原事务已保存”与“原事务未保存但失去资格”的区别。root 新增 `integration/bootstrap/test_recording_results_cancellation.py`，既有控制测试使用 `integration/capture/test_record_media_result_settlement.py` 和 `integration/bootstrap/test_recording_results_saved_consumers.py`，不另建影子仓储或改生产判定。

2026-10-09，Linux 容器、Python 3.11.16：root 的 `/tmp/camctl-goal-recording-retirement-red.log` 为 10 failed、6.54s。五个默认入口分别覆盖 RUNNING／cancel=1 与 CANCELED；首次完整申请实际命中 COMMIT 前 UNKNOWN，关闭原连接后由 fresh Owned 确认 F 缺失，再通过公共取消仓储建立目标状态。普通、残留、受限入口因取消标记或不同终态拒绝原普通申请并报告状态库错误；两个取消入口未重送原 F 就进入候选。独立审查核对完整测试及上述日志，确认前提、原尝试和完整申请均已形成，查询代理只观察候选边界、不修改投影。这十项只验证候选前可靠退役和事实守恒，在候选边界主动结束，不能证明后续拥有者已经完成取消或结束 RESULTS。

- [ ] 在真实公开媒体完成之后对原共同申请注入实际 COMMIT 前 UNKNOWN，记录完整输入、F、T1、原尝试、run 和媒体；真正关闭旧连接，再由 fresh Owned 核同库身份、证明 F 缺失和无迟到事务。通过公共取消仓储只保存目标 cancel=1、保持 RUNNING，再调用原 handler 或默认普通／受限拥有者。有效红必须是旧普通申请阻止取消继续，而非 fixture 或守卫失败。
- [ ] 同一真实保存故障世界中，通过公共 `ApplyCancelTarget` 与 `FinishCanceledCapture` 建立目标 CANCELED，再分别调用普通、残留、受限、普通取消和受限取消五个默认入口。断言 F 始终缺失、目标与产物事实不变、holder 可靠退役、无新 factory／设备／媒体调用；默认终态入口不能因候选过滤遗漏原 holder。`_cancel_and_end_original` 提供该公共链，但它不能代替上一项 RUNNING／cancel=1 反例。
- [ ] 增加控制分区：F 已实际 COMMIT 后 UNKNOWN 仍核原完整输入；F 未提交且目标 cancel=0 的原申请仍用原 F／T1 完成；仅受理新取消仍未生效时普通决定可以先完成；持续读取或保存失败保留 holder 并停止依赖取消。改变原输入或 T1 的已存 F 必须拒绝，不能退役绕过。
- [ ] root 独占部署 Python 3.11 的反例门禁，确认取消入口缺少原键核实、可靠缺失的普通决定未退役分别取得有效红，再实施。仓储在同一写事务内先查 F、核严格原身份；存在时走完整复用，不存在时按上表分流。退役不经过普通 `FinishCaptureCommand._recover` 的结果相同校验，不重新判定媒体或写入任何业务事实。
- [ ] helper 仅在可靠保存或可靠退役之后移除 holder。默认 cancel 新增 recording 专用前缀，复用本会话共同 map，沿既有异常边界处理真实失败；不扩 raw READ End、跨进程 holder 或 v2。
- [ ] root 独占复验新分区、原共同收场、完整输入重送、默认消费者及媒体取消保存顺序。独立审查逐格核 key-first、T1／T0、无迟到提交、可靠退役与旧事实守恒；按用户已授权的统一 checkpoint 提交。绿色仅完成本任务，不宣称下一项取消 RESULTS 已闭合。

#### 任务二：取消拥有者结束原录像 RESULTS

2026-10-09，root 的 `/tmp/camctl-goal-canceled-recording-results-red.log` 为 6 failed、4.00s。公开取消已经生效，实际 handler、普通 scheduling、受限 winddown 分别保存动作 CANCELED，随后真实 cancel flow 成功；原 RESULTS 仍 ACTIVE、retry 为 1。各入口分别覆盖无普通 holder 及实际 COMMIT 前 UNKNOWN 后可靠退役的普通 holder，原尝试、媒体、文件及全部外部调用守恒断言通过，仅原 run 最终收场断言失败。

以下实现分工是按实际代码确定的建议，不改变正式行为。先为 `finish_canceled_capture` 的录像分支组合原取消动作事务与可选原 RESULTS 的 CANCELED 事件；沿原活动核对唯一责任，无 run 不建立，已有终态保持。复用现有 `FinishCanceledCapture` 完整输入和共享 `PendingRecordingResults`，按请求类型派发普通或取消保存。先核普通原键、可靠退役后才形成取消申请；取消申请在第一次仓储调用前固定自身 key、T1 和完整输入，UNKNOWN／回滚时保留，handler 与所有默认入口在终态过滤前核实。

已保存动作 CANCELED、同会话没有 holder 而原 run 仍 PENDING／ACTIVE 时，默认前置按持久责任发现该项。新申请固定原动作取消事件的 `occurred_at`，使用新 key；这是补存已生效的用途结束事实，不声称恢复丢失的旧申请。无需取得当前时钟、驱动、RESULTS、READ 或媒体资格。取消汇总独立核对原 RESULTS 尝试：run 可先保存 CANCELED，但实际调用或结果保存尚未结束时不得汇总成功。正式 `operation-fields.md` 允许这种 run 终态与 attempt 在途组合，不能额外要求所有尝试已结束才允许用途取消。

后续步骤依次为：六项有效红的共同事务修复；COMMIT 前后 UNKNOWN 的实际消费者恢复；终态过滤及 fresh Owned 的持久责任发现；无 run、已有最终结果、在途尝试控制分区；原普通核实／取消／READ 默认消费者回归。每组以根独占真实运行记录结论，不从六项通过推定所有恢复分区完成。

任务二使用上述六项公开反例验证取消动作、原 RESULTS 用途和取消请求之间的责任，不能以动作 CANCELED 单独证明原调用已经结束。

正式规则要求用途因取消而不再承担核实责任时，原 run 保存 CANCELED、retry 为 0；原实际尝试、次数和文件观察保持。已有任何 run 终态均保持，不能将已存 SUCCEEDED／FAILED／UNCONFIRMED 改成 CANCELED。普通申请退役只解除旧申请的阻塞，不能完成此责任，也不能凭此使取消请求成功。

| 原录像 RESULTS 事实 | 取消拥有者的处理 |
| --- | --- |
| 原活动尚未建立 RESULTS，且没有已开始调用或未保存结果 | 不为形式完整新建 RESULTS。 |
| 原 RESULTS PENDING／ACTIVE，取消已经生效且用途已放弃 | 沿原 action／activity／run 结束为 CANCELED，共同清除 retry；次数、配置、原尝试和实际观察不变，不新查询文件。 |
| 原 RESULTS 调用仍在执行，或实际结果／保存责任未确认，run 可以已经 CANCELED | 保存并核实原实际结果，保留责任，取消请求继续等待；不能伪造尝试失败、提前结束必要保存或借退役跳过。 |
| 原 RESULTS 已有最终结果，包括 UNCONFIRMED | 保留原最终结果、错误、配置与次数；迟到尝试仅保存其实际结果，不重开或覆盖原 run。 |
| 原 run 身份、历史或保存结果不可靠 | 按状态库错误停止，保留原取消及保存责任，不汇总取消成功。 |

下一步建议沿最窄录像取消拥有者边界组织原 run 的 CANCELED 收场。可以复用 `StaleRunFinish` 的既有身份守卫和结束事件；是否与 `FinishCanceledCapture` 组合取决于实际公共事务模型，须在有效红后核对。新增取消收场申请与保存故障必须持有自己的完整输入、key 和决定时刻，不能修改旧普通 F、复用原 AttemptFinish key 或把其 T1 当取消事实时刻。需覆盖直接普通 handler、restricted winddown 及目标已 CANCELED 后仍有原 run 责任的恢复，避免候选过滤再次遗漏独立责任。

- [ ] root 决定下一有界阶段后，先从公开 START／STOP、真实 RESULTS 与媒体等待建立原 ACTIVE／retry=1 世界，公共取消生效后执行实际 `_record_handler` 或默认 scheduling／winddown，随后执行真实 cancel flow。不能只调用 fixture 的 `FinishCanceledCapture` 证明拥有者闭合。
- [ ] 断言旧普通 F 未提交且已退役，原 run 同一身份变为 CANCELED／retry=0，原尝试、T0、次数、配置、媒体与文件观察保持，零新增 RESULTS／READ／工具调用，取消请求等待适用责任可靠结束再完成。直接取消但没有 ordinary holder 的同类世界也应覆盖，以区分旧取消缺口与本任务退役引入的回归。
- [ ] root 取得有效红后，核原已保存 attempt／run 终态控制分区、保存前／后 UNKNOWN、同会话与 fresh Owned 恢复，再授权取消 run 生产修改；此前保留任务二为未完成，不将其绿色前提假定成立。

任务二的完整输入核验还有一项静态缺口，尚无行为红：`_FinishCanceledCaptureCommand._reuse` 将 `unstarted` 核验交给 `FinishCaptureCommand._reuse`，而后者只在重送输入为 `unstarted=True` 时验证可靠未启动。原合法未启动取消申请 G 已提交后，用相同 action、key、T1 将 `unstarted` 改为 False，会跳过该核验；原取消事件及无产物输入仍满足其余复用条件。这个限制来自原公共完成命令，新取消 wrapper 继续使用它；当前已启动录像的 COMMIT 前后 UNKNOWN 恢复不能证明未启动标记的完整输入核验。

最窄公开前置为：真实受理录像动作，公开 `start_action` 将业务转为执行中，但不授予设备启动、不建立实际启动尝试；通过取消仓储公开开始、固定并以 PRE_START 生效取消；实际取消拥有者形成 `FinishCanceledCapture(unstarted=True)`，记录其 G／T1／完整输入。保存或 COMMIT 后响应未知时关闭原连接，以 fresh Owned 核实原 G；同键仅改变 `unstarted`，其余输入和目标保持，观察当前复用行为，再核原 True 输入合法重送、历史与终态不变。前提不得使用 SQL 改业务状态，也不得伪造 End、活动或设备结果。反向 False→True 须从公开合法事实另行构造，不能拿已经确认启动的世界代替合法未启动输入。

下一阶段先界定 `unstarted` 是原请求身份的一部分，还是只提供派生的执行资格证明，再确定可证伪预期及最窄核验方式。若属于身份，应在原 G 的共同输入核验边界可靠固定并核原值；若属于派生证明，应明确同键复用时核对原事实的规则。不能从当前 `not_started` 或某条释放事件推测首次请求的布尔值，不能为通过局部用例临时补历史字段。需要新 history evidence 时，先提出具体方案并完成登记决策。影响范围为取消公共仓储、无 RESULTS run 的未启动录像和共享 G replay；修复后审计同样使用 `FinishCaptureCommand` 的完成入口与改变输入保护。此项不修改原 attempt，不新增 RESULT／READ／媒体端口资格，行为红、正式字段方案和实现仍未完成。

本节分别记录任务一和任务二的验证范围；任务二仍有上述未启动输入身份缺口。第一版普通延时集合结束、raw READ 技术校验、READ 取消前置及 v2 不在本节范围内。

#### 取消恢复任务二的当前验证与提交范围

2026-10-09，Linux 开发容器、Python 3.11.16：`/tmp/camctl-goal-recording-read-final-bootstrap.log` 为 70 passed、39.72s，覆盖共同取消事务、五个默认入口的 COMMIT 前后 UNKNOWN、无会话申请的持久责任发现、在途尝试等待、原普通申请及 READ 恢复。另五项有效反例证明迟到实际 RESULTS 保存会更新 action 的 `last_event_id`；持久责任发现通过实体历史关联取得唯一 ACTION_FINISHED 取消事件及其原时刻，不能把最后拥有记录当作动作取消事件。

全量单元在最后一次持久发现修改之前为 3717 passed、1 skipped、2 warnings，日志为 `/tmp/camctl-goal-cancel-and-errors-unit-green.log`；不将此记录声明为最后修改后的完整门禁。较宽 capture 检查为 106 passed、3 failed，均涉及仍待确定的 UNSATISFIED 错误详情。cancellation 检查为 101 passed、2 failed，两个入口缺少 `withdrawal_execute`；用修改前 settlement 模块单独运行同两项仍失败，此对照不代表旧版本全量验收。

用户授权将全部当前变更合并为本地 WIP 快照，包括未完成项，不为提交拆分或历史整理追加门禁。新 `test_result_exhaustion_save_recovery.py` 的四项 photo/timelapse × COMMIT 前后 UNKNOWN 候选尚未运行；下一步核实有限耗尽申请的原 request、key、T1 保存责任。此快照不代表任务二、capture 或完整 apps/camctl 已完成。


#### 取消恢复任务一的当前验证与提交范围

2026-10-09，Linux 开发容器、Python 3.11.16：root 的 `/tmp/camctl-goal-recording-cancel-prefix-red.log` 为 2 failed、3 passed、3.25s，两个取消入口在目标已经终态后遗漏原完整申请。`/tmp/camctl-goal-recording-retirement-red.log` 为 10 failed、6.54s；真实 COMMIT 前 UNKNOWN、关闭旧连接、fresh Owned 确认原 F 缺失和公共取消前提全部通过。六个普通／残留／受限分区被旧普通申请的保存拒绝阻挡，四个取消入口遗漏前置核实。

共同仓储先核原完整 F，再核原录像身份。F 可靠缺失且已取消或已有终态时返回 RETIRED，交付当前 action／plan／outputs，不修改原 run 或 retry gate。两个取消入口只接专用 recording 恢复。独立只读审查核对此最窄 diff、严格原输入、T1、事务顺序和未知传播，未发现阻断项。

`/tmp/camctl-goal-recording-cancel-checkpoint-bootstrap.log` 为 34 passed、19.68s：原 F 已提交后 UNKNOWN 的五入口，以及五入口中原键读取错误后可靠回滚／继续 UNKNOWN，共十五项；F 未提交后仅受理取消、取消已生效、目标已取消的五入口组合，共十五项；媒体原申请与取消保存顺序四项。未提交且只受理取消时，原 F／T1 仍共同保存 RESULTS SUCCEEDED、原片及动作成功；已生效取消时退役不写新历史。候选边界反例只证明前置恢复，不证明任务二的实际取消拥有者已经关闭 RESULTS。

`/tmp/camctl-goal-recording-cancel-checkpoint-capture.log` 为 16 passed、9.30s，原媒体共同事务、首次业务判定、原键输入变化及 COMMIT 前后恢复保持。`/tmp/camctl-goal-bulk-checkpoint-unit.log` 为 3687 passed、1 skipped、2 warnings、7.72s。两个 warning 来自既有同步测试的 asyncio 标记。本次统一保存当前所有本地工作，READ 尚未完成反例与实现一起纳入 checkpoint；不宣称完整 READ、bootstrap 或全部拍摄计划通过。任务二继续独立推进。

### 取消延时摄影耗尽后的文件登记

正式依据为[取消、失败与文件保留](../../architecture/camera-capture.md#取消失败与文件保留)：有限处理结束后，已经拍完、确认属于本动作且写入完成的文件须与取消终态共同登记；集合未确定不取消这些文件的独立资格。录像取消放弃内容遵守自身规则，不套用本节。

最初的只读审查发现 `_close_canceled_timelapse` 的 EXHAUSTED 分支保存核实 UNCONFIRMED 后直接取消，没有传 drafts；同一结论已可靠保存后重入 CLOSED 分支却沿原文件登记 drafts。此差异在 `9ad78be` 已存在，不属于有限耗尽 holder 改动引入的回归。行为反例和实施结果见下文。

2026-10-09，Linux 开发容器、Python 3.11.16：根独占 `/tmp/camctl-goal-cancel-timelapse-exhaustion-red.log` 为 1 failed、1 passed、1.38s。公开 timelapse 的实际一轮 typed v1 PHOTO、有限预算、公共取消和实际 STOP 均可靠保存；直接 EXHAUSTED 分支在 CANCELED、原 UNCONFIRMED run、尝试／文件／历史守恒与零重复调用之后，仅缺正式产物。可靠 close 后重开且没有会话申请的 CLOSED 控制通过。这是一项已存在的文件保留缺陷，不是保存 holder 引入的回归。

| 原保存与目标状态 | 文件事实 | 必须执行的行为 |
| --- | --- | --- |
| 耗尽申请仍持有，原键读取或保存不可靠 | 任意 | 保留完整申请，停止取消终态及产物提交；不能查询设备补输入。 |
| 原事务可靠缺失且不会迟到提交 | 任意 | 原 key／T1 重送，可靠之前停止依赖业务。 |
| 耗尽结论可靠完成，取消已生效且动作未终态 | 有已拍完、确认归属且写入完成的文件 | 消费持久原文件；各合法原片与 canceled 同事务登记，不增加 RESULTS。 |
| 同上 | 没有满足登记条件的文件 | 保存取消终态及实际诊断，不创建虚构产物，不把未知解释为不存在。 |
| 动作已经终态 | 任意 | 保持原终态与产物；仍有会话申请时先核原 key，不补登记后来文件。 |

建议实施步骤如下，内部函数组织可以按真实数据流调整。

1. 复用公开 timelapse 消费者、实际 START／适用 STOP 与 typed v1 完整文件保存，建立取消已经生效、集合仍未确定且有限 RESULTS 次数用尽的真实前置。覆盖可登记的 PHOTO／VIDEO、无符合条件文件、未完成文件；不同文件事实的预期独立推导，不写 SQL 改业务状态，不伪造集合结束。
2. 对相同原事实分别测试 close 直接成功、COMMIT 前 UNKNOWN、COMMIT 后 UNKNOWN、可靠 close 后关闭重开且没有会话 holder。有效红应是合法正式产物缺失或不同，不能是 fixture、驱动证据或状态守卫错误；后三项只在原连接关闭、原事务不会迟到提交后重送。
3. 取得有效红后，将 EXHAUSTED 和持久 CLOSED／UNCONFIRMED 的取消收尾统一到原文件事实消费者。原 close key／T1、Outcome、attempts、配置和历史前缀保持；实际需要停止时仍沿原责任推进，恢复不新增设备调用或结果查询。未可靠 close 时不能提交依赖取消事务；产物与 canceled 始终共同保存。
4. 验证四种保存分区得到相同合法产物和 canceled，重复恢复不重复登记；无合格文件及已有终态保持规则。随后审计 `_close_canceled_timelapse`、`_finish_canceled_capture`、普通 timelapse 的已保存结论消费，以及同类 photo 文件保留入口。录像取消不登记产物的独立行为须有控制分区。
5. 根独占部署 Python 3.11 的局部反例及相关 capture／默认取消回归，独立核实际 diff 与证据，按用户授权合并当前阶段工作为本地 checkpoint。未决普通集合结束、UNSATISFIED reason 和完整 app 验收继续分别跟踪。

#### 文件保留的实施与阶段门禁

2026-10-09，Linux 开发容器、Python 3.11.16：`_close_canceled_timelapse` 在 EXHAUSTED 时先可靠保存原耗尽决定，再装载 `_saved_result_listing`，与已有 CLOSED 分支共用持久文件登记及取消终态事务。该 listing 已保存，后续列举完成入口不把原 UNCONFIRMED run 改成 SUCCEEDED；原尝试、错误和次数保持。

`/tmp/camctl-goal-cancel-timelapse-exhaustion-green.log` 为 14 passed、7.58s：完整 PHOTO／VIDEO × 四种保存恢复分区八项；空观察与未完成文件 × direct／无会话申请重启四项；前轮完整文件、末轮 FAILED 且无文件观察 × 两个分区两项。UNKNOWN 保留原 request／key／T1，关闭旧连接后以 fresh Owned 核原事务再恢复；只有可靠 close 后的重启分区没有会话申请。全部核原 attempts／文件／历史前缀、零重复 RESULTS／STOP／control、产物与 CANCELED 同事务，以及终态重复推进只读。

`/tmp/camctl-goal-cancel-output-fixture-gate.log` 的整个 `test_recording_finish.py` 为 10 passed、1.62s。旧文件保留用例使用真实 timelapse 前置，保留原键及新键恢复、产物身份及同事务断言；独立录像控制拒绝 drafts、取消时不登记产物，源文件事实保持。`/tmp/camctl-goal-cancel-timelapse-files-unit.log` 为 3717 passed、1 skipped、2 warnings、7.81s；两个 warning 仍来自既有同步测试的 asyncio 标记。

`/tmp/camctl-goal-cancel-timelapse-related-capture.log` 为 41 passed、5.24s，覆盖已保存取消结果消费、末轮报错前文件恢复、迟到 START 和录像开始查询取消。合法核实错误另经三项真实报告生成与固定 H 重建验证，范围见[报告投影计划](2026-10-09-camctl-result-errors-and-report-projection.md#合法核实错误的真实报告生成)。

独立只读审查核这五行生产改动、实际文件消费者和十四项候选结构，未发现该修复的生产阻断。此门禁只闭合有限耗尽决定可靠前后、文件资格和取消登记之间的差异；取消终态申请自身的保存 UNKNOWN、普通集合结束、UNSATISFIED、事件守卫及完整报告链仍按各自计划推进，不由此推定已完成。
