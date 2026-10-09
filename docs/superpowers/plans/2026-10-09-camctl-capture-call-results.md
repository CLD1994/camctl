# 拍摄停止、状态查询与残留收场的结果保存执行计划

本计划闭合[运行配置计划](2026-10-09-camctl-runtime-configuration-closure.md#rc3-预算调用时限与真实收场使用本次配置)中的普通 STOP、全部 `QUERY_ACTIVITY` 用途和 `STOP_RESIDUAL`。录像 START 与 START_CONFIRMATION 已有[独立计划](2026-10-09-camctl-recording-start-runtime.md)，其结果持有机制需要纳入同一个公共责任边界。正式依据是[操作结果与保存](../../camctl/database/operation-fields.md#正常返回恢复与历史保存)、[普通调用收场](../../camctl/database/operation-fields.md#普通尝试结束结果的保存边界)、[查询用途](../../camctl/database/operation-fields.md#活动查询的责任范围)、[事务接口](../../camctl/database/transactions.md)和[残留收场](../../architecture/camera-recovery.md#后续动作触发的残留收场)。

本计划不修改公共机器协议，不新增持久化调用登记表，不扩大第一版故障范围。RESULTS 的分页、轮次及 `ResultFilesPort`／`DriverResultListing` 由结果列举专题落实；文件读取与 cleanup 由其所属专题落实。共同的不变量适用于这些操作，但本计划的生产修改和验收不代替它们。

## 具体问题与目标

录像 A 的停止端口已经返回可靠停止观察，同时携带调用错误。普通 `_stop_call` 保存实际 FAILED／CONFIRMED 尝试并结束 STOP，却返回错误阶段；`SessionRecordingState` 又只把 SUCCEEDED 尝试识别为已停止。这使正常录像继续丢失可靠停止依据。取消分支另按效果识别，不能用取消分支通过代表所有调用方已经一致。

残留录像 A 已终态，动作 B 查询并停止 A。现有残留停止把尝试结果、停止流程成功和活动结束分别提交；确认查询又将查询结果、残留 STOP 结束和活动结束拆成三个事务。任何中间失败都会留下不足以代表实际返回结果的边界。普通 `CaptureRuntime.finish` 分支每次生成新 `operation_key`，保存失败仅断言完成，未持有原真实结果供同会话核实。执行前查询还以是否存在调用错误选择活动分支，可靠观察可能与错误一起被丢弃。

目标是：调用已按所属契约收场后，原执行者持有完整 `CallOutcome`、原票据、实际观察时刻和唯一结果事务身份；原结果与其需要派生的流程及活动事实共同提交。回滚后重送原结果，提交未知先核实原键，不重新执行设备。普通资格已经结束时仍保存实际观察，原流程和动作终态保持，必要设备收场独立继续。

## 权威输入与不变量

1. 已提交的 `(run_id, attempt_no)`、原 `responsibility_key`、用途及目标确定调用身份；意图和累计次数先可靠提交，才派发。`ControlRequest` 带原 ticket 和本次对应配置期限，受管端口只调用 `invoke` 一次。
2. `DeviceCallResult.outcome` 是正常生产结果权威。状态、错误、效果、观察、`settlement` 和 `call_info` 同源保存，确认同时带错误时不得把 FAILED 改成 SUCCEEDED。完整依据不能被重新构造的 `operation_returned` 覆盖。
3. `await` 返回后立即取得运行时墙钟与适用单调读数，再读取或写入数据库。后续保存、原键核实及重送都使用同一组事实读数；查询不把保存完成时刻当成实际观察时刻。
4. 保存命令在同一写事务核对原固定关系及可靠旧状态。业务资格、取消与终态由当时可靠状态决定；实际结果不依赖普通资格仍有效。派生决定与事件组合按原事务的完整边界核实，不用重送时的新投影猜测原决定。
5. 调用错误、发令责任完成、设备实际停止、文件写完和动作结果分别判断。已结束的普通流程和动作保持结果；迟到可靠停止仍更新原活动，不能因为旧 STOP FAILED 或 CANCELED 而丢弃观察。
6. 活动观察及适用释放共同保存。只有统一[释放规则](../../camctl/database/operation-fields.md#设备活动占用的释放条件)成立才释放；输出范围尚未确定时保存 ENDED 并保留 HELD，不将范围限制解释为设备仍在运行。释放不自动授予下一动作。
7. 本会话实际 await 拥有者持续跟踪原调用，不从 DB.RUNNING 推断永久在途，也不新增 `active_calls` 表。可靠启动边界与固定初始 `recovery_max_event_id` 双门只适用于旧意图；同会话已取得的原可靠结果优先于 UNKNOWN 恢复。

## 当前入口与责任归属

2026-10-09 静态读取中，源码尚无 `_record_query` 实现。ACTIVITY_OBSERVATION 和 STOP_CONFIRMATION 已有正式规格、类型、唯一责任键及仓储守卫，尚未接真实调用消费者；实施时必须接对应正式责任，不能将缺失入口写成已有验证。具体函数名和模块划分是建议，允许按实际数据流调整。

| 入口或用途 | 原责任与设备配置 | 尝试／流程的历史归属 | 取得活动观察时的归属及必要派生 |
| --- | --- | --- | --- |
| `handlers._stop_call`，普通录像、延时取消、恢复与保守收场共用 | 原动作的 STOP、原活动、原驱动，本次 stop 配置 | 原动作 | 原活动所属动作；可靠停止更新 ENDED，适用时释放，原动作按自身完成或取消规则继续。 |
| `handlers._confirm_record_start`，START_CONFIRMATION | 原 START 与活动，本次 query 配置，沿已有独立预算 | 原动作 | 启动确认与普通 START 的适用变化共同提交；取消或终态保持，新观察仍保存。 |
| 待接 ACTIVITY_OBSERVATION；`_reconcile_recording`／`advance_winddown` 的设备对账消费 | 已启动且尚未转入停止处理的原活动，本次 query 配置 | 原动作 | 保存实际归属、连续性及状态证据；不能只用历史 started_at 与当前墙钟推定断电期间持续录像或已经结束。 |
| 待接 STOP_CONFIRMATION；普通 `_stop_call` 未确认后的必要核实 | 原 STOP 与同一活动，本次 query 配置 | 原动作 | 可靠停止更新原活动并结束适用 STOP／查询；原 STOP 已终态时保持，观察独立保存。 |
| `residual._preflight_check`，BEFORE_EXECUTION | B 保存的绑定，无 activity 目标，本次 B 设备 query 配置 | B | 实际观察到 A 时只更新 A 的活动；不把 B 查询预算转给 A，不从没有活动记录推定空闲。 |
| `residual._stop_residual`，STOP_RESIDUAL | B 为 A 的固定活动建立原收场，设备及本次 residual 配置来自 A | A，沿 activity.action_id | 原活动 A 结束及适用释放；A／B 已有动作终态、A 原普通 STOP 结果保持。 |
| `residual._confirm_by_query`，RESIDUAL_STOP_CONFIRMATION | B 原残留 STOP 与 A 活动，本次 A 设备 query 配置 | B，沿 query.action_id | 原活动 A 及适用残留 STOP 共同收场；查询历史仍属于 B。 |
| `residual_flow → _recover_old_attempts` 与普通／受限 factory | 原票据、原驱动恢复声明，固定初始 H 与 RecoveryBoundary | 各原责任 | 先接手同会话持有的原结果；只有确实丢失旧结果且双门满足才生成正式恢复结果。终态下的未结束尝试也必须被发现。 |

普通 STOP 的全部真实调用方包括 `_record_handler`、`_reconcile_recording`、`advance_winddown`、`_advance_canceled_capture` 和延时取消路径。所有调用方都先按可靠停止事实继续，再保留错误诊断；不能让正常／恢复路径只识别 attempt.status，而取消路径识别 effect。

## 保存与恢复的完整分类

先按下表核对结果责任及事务，再应用用途表。数据库错误不能折叠成业务未授予或普通等待。

| 调用与保存阶段 | 必须保持的事实与允许动作 |
| --- | --- |
| 意图事务 ROLLED_BACK／UNKNOWN，未取得可靠派发许可 | 状态库错误或核实原意图；零设备调用，不退款或另建尝试。 |
| 调用仍 await 或本地尚在收场 | 尝试 RUNNING；及时保存独立取消，继续跟踪原调用，禁止冲突新调用。窗口或预算不能替代收场结束。 |
| 本次实际调用已结束，结果尚未保存 | 持有原完整结果、读数、原票据与单个结果键；形成必要原子派生，再提交。 |
| 保存结果可靠 COMPLETED | 释放该内存责任，才允许依赖步骤；真实停止与错误诊断同时保持。 |
| 保存结果 ROLLED_BACK | 原事实和原键保持；确认事务结束后重送相同结果，不重发设备。必要业务转换由新写事务的可靠取消／终态事实约束。 |
| COMMIT 前错误且连接仍有事务 | 可靠结束该事务后核实原键；事务内新值不作为已提交依据。无法结束则保留原结果并报 STATE。 |
| COMMIT 已成功，随后返回错误 | 核实原完整事务及输入后复用；不再增加结果事件、次数或外部调用。 |
| 原提交情况仍不可可靠判断或保存持续失败 | 保留实际结果与原键，停止依赖设备步骤并返回 STATE；不以空值跳过，不以下一会话 UNKNOWN 代替本次可靠结果。 |
| 同会话下一轮／另一 factory 取得该原待存结果 | 从会话共享的同一责任集合核实保存；不得生成第二份缓存权威或新键。普通、缺失绑定与受限 factory 均使用同一集合。 |
| 原键重送，结果、事实时刻、观察、原目标或请求的派生输入不同 | 拒绝，整组无新增历史及投影；不能将不同输入解释为幂等成功。 |
| 原结果已可靠保存，后来有其他观察或取消 | 原事务输入及结果保持；新事实沿自己的来源保存。原键核实使用原完整边界，不用当前行代替原边界事实。 |
| 原进程已退出、原结果未保存，可靠旧边界与原驱动适用证据及旧 H 均满足 | 使用正式 adb_foreground_recovery 记录，只结束原前台发令责任；未知设备活动继续所属核实，不重开已结束流程。 |
| 无可靠旧边界／固定 H／原证据，或 intent_event_id > H | 保留原尝试及类型化诊断，零冲突调用；不从重启、DB.RUNNING 或后来读到的新 C 推定恢复资格。 |

提交核实与重送应复用最窄公共保存责任。预计将 START 的 `PendingStartResult` 推广为类型化调用结果责任，由会话装配共享一个集合；命名及文件拆分不作为硬性要求。该对象持有外部事实和稳定身份，不持有会在重送时重新取设备结果、墙钟或猜测活动的回调。仓储复用现有尝试及活动守卫，通过事务内事件投影证明伴随流程／活动转换，不先写投影来绕过守卫。

## STOP 结果、资格与活动转换

下表在确认调用已收场、原结果及固定目标合法后使用；未知状态和其他活动不满足可靠停止。原流程终态总是保持，只有未结束流程可以按本次责任结果结束。

| 原结果与业务条件 | 流程、活动及后续处理 |
| --- | --- |
| 可靠停止，无调用错误 | 保存实际成功尝试；普通责任适用时成功；原活动 ENDED，统一规则允许时同事务 RELEASED。 |
| 可靠停止，同时有调用错误 | 尝试保存 FAILED／原错误／CONFIRMED 和完整依据；责任按停止事实完成；活动及释放同上一行，不追加停止或无必要查询。 |
| 只可靠确认发送，设备效果仍 UNKNOWN | 保留发送结果，不更新 ENDED；按驱动声明选择原 STOP_CONFIRMATION 或可安全重复的原 STOP，预算独立且保持。 |
| 调用失败／UNKNOWN，缺少可靠停止观察 | 保留原错误与 UNKNOWN，活动占用保持；只有原驱动声明安全重复且原目标、资格及次数均允许才追加 STOP。 |
| 本次可靠 NO_EFFECT | 只证明本次停止未生效，不撤销原录像或此前停止事实；不因此释放设备。 |
| 已有可靠停止或结束事实 | 直接使用，不为了满足流程形式重发或查询；未结束责任按适用依据结束。 |
| STOP 或动作已终态后迟到可靠停止 | 保持原流程／动作结果；保存原尝试真实结果及活动 ENDED／适用释放，不改写旧失败为成功。 |
| 取消在调用期间生效 | 及时保持取消；实际返回后保存真实停止事实，必要收场继续，内容和动作终态仍按取消规则。 |
| 活动可确认结束，但输出范围限制未解除 | 保存 ENDED，保留 HELD；不因为不能释放而丢弃停止观察。 |
| 调用尚未结束或原目标／绑定／事实不可解释 | 等待原调用或按绑定／状态错误处理；不停止任意活动或授予下一动作。 |

普通、取消、恢复、保守与残留入口采用同一停止事实分类。停止命令能安全重复不证明旧调用成功，也不允许绕过未结束调用；旧流程失败后的新触发者只沿正式 STOP_RESIDUAL 身份处理。

## 五类查询的完整结果模型

查询先保持已有流程终态，再判断该用途是否仍需处理，最后解释合法观察。一次响应 SUCCEEDED 不自动结束查询责任；FAILED 同时带可靠观察时，原观察仍参与业务判断。空观察仅表达没有携带活动观察，只有原驱动明确认可的响应组合才能证明空闲，不把未知、错误或未登记证据当成空闲。

| 观察与用途 | 必须保存及允许转换 |
| --- | --- |
| BEFORE_EXECUTION 可靠确认空闲，触发资格仍有效 | 保存原查询结果及适用责任变化，之后重新核对实际占用、窗口、排序和机会，不直接授予启动。 |
| BEFORE_EXECUTION 可靠确认已终态 A 的固定残留活动 | 查询仍归 B；同事务保存必要 A 观察；只有 B 仍有效、原目标归属和安全停止声明齐备才建立或继续 B 的残留收场。 |
| BEFORE_EXECUTION 发现正常占用、其他活动、归属未知或无可靠状态 | 按占用／窗口等待或相应错误处理；不停止其他录像，也不换责任刷新查询次数。 |
| START_CONFIRMATION 可靠确认原活动启动 | 保留原启动尝试结果；查询结果、适用 START 成功及活动确认共同提交，实际确认时刻来自本次查询。取消或原终态时只保存新事实。 |
| ACTIVITY_OBSERVATION 确认原活动及同一段连续录像 | 保存真实状态、归属与连续性依据，随后结合适用可信计时继续；历史墙钟差本身不证明断电期间持续录像。管理仍需继续时不结束查询责任。 |
| ACTIVITY_OBSERVATION 无法确认归属或连续性 | 保持未知或对应诊断；不据旧启动意图推测正在运行，不据过去目标时长宣称已结束。 |
| STOP_CONFIRMATION／RESIDUAL_STOP_CONFIRMATION 可靠确认目标已停止 | 查询结果、适用原停止责任完成和目标活动结束／条件释放共同提交；原停止流程已经失败或取消时保持该结果。 |
| 停止核实确认目标仍运行，包括同时带调用错误 | 保存实际观察与原错误；不报告停止成功；仍有必要资格与次数时沿原责任继续，允许重复停止与否由原驱动声明决定。 |
| 任意用途的实际结果不可靠、结构／身份不符 | 沿既定驱动诊断处理，拒绝非法事实，不改成成功／空闲／不存在；不放宽具名守卫。 |
| 任意用途仍未知且次数耗尽／契约不能继续 | 以所属失败或 UNCONFIRMED 结束查询，不换用途、目标、入口或会话取得次数；活动未知部分与必要收场保持。 |
| 取消／过期使执行前检查或活动管理不再需要 | 普通查询责任 CANCELED；已开始尝试仍保存实际返回与观察，不使用 EXPIRED 查询状态。 |
| 触发动作已结束，但原普通停止或已派发残留停止仍需确认 | 沿原必要停止核实继续及剩余次数；不被触发者终态提前结束。 |
| 查询或相关动作已有终态，随后实际返回 | 原终态保持；尝试结果及原活动新事实仍共同保存，不重开 START／STOP／查询或动作。 |

ACTIVITY_OBSERVATION 转入停止处理后不得借其剩余额度继续做 STOP_CONFIRMATION；停止核实与启动核实的预算独立。残留查询的原设备绑定取 A，查询历史取 B，必须分别验证。

## 分阶段实施与可证伪验收

### 第一阶段：原结果、原时刻与原键的公共保存责任

先写最窄失败测试，协调 Agent 核实有效红后再改生产。纯保存责任的单元测试用受仓储接口约束的 COMPLETED／ROLLED_BACK／UNKNOWN 替身；真实提交边界使用集成测试。覆盖普通 STOP 与各查询返回的完整 ASSUMED、OBSERVED、CONFIRMED+error 结果，精确断言原错误详情、settlement、call_info、票据、实际期限、时刻及单个结果键，不逐字断言诊断文案。

真实 SQLite 分别注入投影失败、COMMIT 前错误、COMMIT 成功后错误、持续无法核实；证明回滚全表保持、原键核实与重送、单次设备调用和原次数，以及失败时仍持有原对象。解除故障后在同一会话及另一 factory 保存原结果，确认不以新墙钟、UNKNOWN 恢复或新查询代替。加入回滚／未知与真实取消接缝：尚无原提交时遵守新可靠取消，已有原提交时以原完整边界复用；两者均保留原实际结果和固定目标。

实现建议是先提取一次公共持有／核实边界，迁入 START 既有路径，再接 `CaptureRuntime.finish` 普通分支。普通、受限和缺失绑定装配共用一份责任集合；所有实际调用返回后先登记持有，再进入可能失败的数据库步骤。原结果还在内存时，`recover_attempt` 优先消耗它。

#### 原结果接手后的会话计时消费

普通调度工厂取得录像启动的可靠返回，但结果事务暂不能可靠完成时，会话保留原结果、原时刻和原事务键。残留或受限工厂后来接手并保存这个结果后，普通工厂继续推进同一录像。普通工厂必须使用原启动确认单调锚点计算停止目标，不能因为结果由另一工厂保存而把同会话录像当成跨会话恢复。启动明确未生效、仍需重试时，普通工厂也必须使用原返回对应的间隔锚点，不能从自己首次读取等待标志时重新等待完整间隔。

这两项会话态分别服务于录像停止时机和原操作的重试间隔。设备结果及历史仍由状态库保存；这些单调读数只在同一会话有效，不写成跨会话权威时钟。工厂只装配本轮连接、设备端口和当前配置，不能各自创建这两项事实的第二份权威。

| 权威输入 | 保存及派生过程 | 后续真实消费者 | 必须保持的不变量 |
| --- | --- | --- | --- |
| START／START_CONFIRMATION 的原可靠确认、实际返回单调读数及已固定目标时长 | 原结果可靠保存后，按原读数和目标时长登记会话确认锚点与停止目标 | `SessionRecordingState.recording_state` 将其交给 `decide_recording_next`；普通、恢复及保守入口据此推进 | 同会话换工厂不能丢失锚点，不能用保存完成时刻重新开始计时；数据库中的动作和原流程终态保持。 |
| 原结果的重试决定、实际返回单调读数及原责任键 | 原结果可靠保存后建立原责任的 `RetryWaitGate` 锚点；流程可靠结束时清除 | `CaptureRuntime.retry_wait_remaining` 先读取可靠次数及等待标志，再计算本次配置下的剩余间隔 | 同一责任只有一份会话锚点；换工厂不重新等待；不同 STOP、查询用途及 RESULTS 责任仍按原键分别计量。 |
| 当前可靠 `retry_wait_required`、累计次数、采用上限及间隔配置 | 每次判断重新读取原流程，再应用已有会话锚点；没有同会话锚点的旧责任按正式跨会话规则处理 | 各 START／STOP／查询／RESULTS 派发入口重新核对业务资格并提交意图 | 共享计时不新增额度，不自动授予机会，也不使已结束责任恢复为可执行。 |

先判断数据库与原结果是否可靠，再按下表消费重试间隔。窗口、取消、占用和排序仍由所属意图事务判断，表中的“到时”只表示间隔不再阻挡。

| 原流程及本次配置 | 会话计时结果 |
| --- | --- |
| 原结果还未可靠保存 | 保留实际结果与原键；不把新工厂首次观察当成已建立等待，也不派发冲突新调用。 |
| 原结果已保存，流程仍有等待责任，已用次数小于本次上限，间隔为正且尚未到时 | 沿原责任、原锚点和本次间隔等待；不能从接手或读取时刻重新计量。 |
| 同上，原锚点至当前单调读数已经达到间隔，包含恰好相等 | 间隔已完成；之后仍须核对其他资格，不能因换工厂重新等待。 |
| 已用次数达到或超过本次上限 | 不为间隔延迟预算耗尽处理；累计次数保持，不新增尝试。 |
| 本次间隔为零或该操作不适用间隔 | 间隔不阻挡；其他资格和原预算保持。 |
| 流程没有等待责任，包括尚无尝试或责任已经结束 | 不从共享锚点补造等待；原可靠状态决定后续行为。 |

建议只由会话装配共享录像确认锚点表和 `RetryWaitGate`，同时继续共用原待存结果集合；不要共享持有本轮数据库连接或设备端口的整个 `CaptureRuntime`／`SessionRecordingState`。字段名和是否封装为会话对象属于实现建议。其他缓存、READ 拥有者及延时截止的共享范围须由各自实际消费者与反例确认，不因为本次计时问题而统一复制全部运行时状态。

先执行 `integration/bootstrap/test_capture_call_session_state.py` 的跨普通／残留／受限真实工厂反例。测试从合法 START 意图和真实返回开始，注入结果 COMMIT 持续失败，由另一工厂保存同一原对象，再由普通工厂消费原锚点和间隔。确认有效红后才能修改这两项会话态的共享边界。随后复验公共结果保存、既有 START 计时、取消接缝和装配门禁；共享集合的对象身份检查不能代替上述实际消费者验证。

### 第二阶段：STOP 与派生活动的事务闭合

沿所有 `_stop_call` 调用方分别证伪正常、恢复、保守、取消及延时取消的 CONFIRMED+error，禁止补发。再覆盖 STOP_RESIDUAL、旧 FAILED／CANCELED 流程下迟到确认、范围限制保留 HELD、未知／NO_EFFECT 不结束活动、在途取消不提前结束、预算变化不清零。用事件 transaction_id 证明结果、适用流程、实际活动观察及条件释放属于同一事务。

复用仓储的具名尝试和活动守卫。停止依据不能只看流程 status=SUCCEEDED：原流程终态保持时，也必须能用本次合法可靠停止证据证明活动结束。若输出限制未解除，保存 ENDED 不受释放拒绝阻挡。审核 `_ActivityConcludeCommand`、`_activity_guard` 及同类释放入口，避免只改 handler 返回标签。

### 第三阶段：五种查询真实消费者与恢复接缝

先为 BEFORE_EXECUTION、START_CONFIRMATION、ACTIVITY_OBSERVATION、STOP_CONFIRMATION、RESIDUAL_STOP_CONFIRMATION 列出独立责任和合法证据场景，再写窄红。对每种用途覆盖可靠满足、可靠仍未满足、UNKNOWN、CONFIRMED+error、额度耗尽、取消／已有终态及真实 await；同时验证不同用途次数独立、同一用途重入不换键、原驱动绑定与历史 owner 分开。

ACTIVITY_OBSERVATION 与 STOP_CONFIRMATION 接线必须通过真实消费者验收，不能只执行仓储创建记录。以原驱动声明允许的必要查询接恢复及停止核实，不添加拍摄期间的后台周期查询。可靠直接停止已经满足责任时零额外查询；确认归属和连续性不足时不进入墙钟成功分支。原驱动没有可表达必要证据的契约时，停止该分区并报告准确接入缺口，不虚构观察字段或从当前配置补造事实。

分别在同会话新 await、旧意图可靠双门、无可靠边界、新 intent_event_id > H、原证据不适用和已有可靠结果分区，通过真正 `residual_flow`／普通推进消费者验证。扫描包含已终态流程下的未结束尝试，按既有稳定 ID 有界分批；不全量加载永久历史，不自动创建新的 STOP 或后台任务。

### 第四阶段：历史与可观察结果

同一结果事务前后的完整 H 分别恢复尝试、查询／停止流程、原活动及触发动作。验证直接回放、快照正向和当前投影逆向一致；旧 H 报告不含后来观察，新的原活动公开设备执行提示随真实事实变化，A／B 归属和原动作终态保持。软件集成不连接真实相机，不宣称验证物理断电或目标主机性能。

建议测试文件为 `integration/capture/test_call_result_save.py`、`test_recording_queries.py` 和 `integration/bootstrap/test_residual_result_transactions.py`；文件名可调整，状态矩阵不能省略。先逐文件运行红—绿，再顺序回归：

```bash
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/unit/capture apps/camctl/tests/unit/operations -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/capture -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/operations -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/cancellation -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/scheduling -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/bootstrap -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/history -q
PYTHONPATH=apps/camctl/src apps/camctl/.venv/bin/python -m pytest apps/camctl/tests/integration/reporting -q
```

所有 pytest 由协调 Agent 按 `apps/camctl/tests/AGENTS.md` 前台独占顺序执行，解释器为部署 Python 3.11。生产实施前根 Agent 审计划和有效红；每阶段实际 diff 与验证由独立评审核对。发现未定义证据、状态或数据流与计划冲突时停止相应分区并报告，不以局部门禁通过代替完整矩阵。

## 当前证据与未完成范围

2026-10-09 已静态读取 `devices.ports`／`managed_port`、CaptureRuntime 与全部 STOP 调用方、残留检查／停止／确认／扫描、operations 身份与原键核实、查询纯规则、活动收场／释放守卫、会话拍摄装配及对应正式规格。已确认现有 START 结果持有与独立取消查询门禁不覆盖上述普通分支。

第一阶段的公共保存生产边界使用 `PendingCallResult`，实际返回先登记持有，普通保存、START 复合结果与恢复接手共用原键及事实时刻。`RuntimeDeps.capture_call_results` 传入普通、残留和受限三个工厂，缺失绑定运行时也引用同一集合。2026-10-09，Linux x86_64、Python 3.11.16，协调 Agent 独占复验 `integration/capture/test_call_result_save.py` 与 `test_recording_start_save_boundary.py`，32 项通过，日志为 `/tmp/camctl-goal-call-result-current.log`。该范围覆盖普通 STOP 保存的读取／投影／COMMIT 故障、五种 QUERY 的公共保存及 START／START_CONFIRMATION 在派生读取前持有；它不代替第三阶段尚缺的真实查询消费者，也不证明跨工厂计时消费已经闭合。

上述测试精确核对原 ASSUMED／OBSERVED 依据、错误、观察、`call_info`、票据、次数、实际时刻和单个事务键；故障解除后在默认无可靠恢复边界且没有恢复 H 的同一运行时调用 `recover_attempt`，要求原实际结果优先保存。跨工厂计时消费的新 10 项反例位于 `integration/bootstrap/test_capture_call_session_state.py`。首次执行在契约断言前被缺失驱动部署定义阻断，日志 `/tmp/camctl-goal-capture-session-state-red.log` 为 10 failed、4.15s，不属于有效行为红。fixture 使用真实 `Catalog` 和现有合法驱动定义后，root 独占复跑得到 `/tmp/camctl-goal-capture-session-state-red-2.log`，6 failed、4 passed，4.36s：两个接手入口都丢失普通消费者的录像锚点，并在原等待尚余一秒或恰好到期时重置等待。根确认有效红后，生产通过 `RuntimeDeps.capture_recording_anchors` 和 `capture_retry_gate` 将两个权威会话对象传给三个工厂；原登记时刻、预算算法和持有集合保持。root 独占复跑该文件，日志 `/tmp/camctl-goal-capture-session-state-current.log` 为 10 passed、4.28s；该范围证明两个接手入口后的原确认锚点与有限等待消费，整体独立审查仍须完成。RESULTS 与 cleanup 的各自完整结果保存仍由其他专题验收。
