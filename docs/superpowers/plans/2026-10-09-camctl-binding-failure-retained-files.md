# 绑定失败后的完整文件保留实施计划

> 执行 Agent 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 按任务实施。根 Agent 独占、前台、顺序运行 pytest，并核验实际失败原因、源码和日志。既定持续实施授权适用于本计划，不新增审批步骤。

**目标：** 拍摄动作因当前设备绑定异常无法继续必要处理时，在保存实际流程失败和动作终态的同一事务中登记原来已经完成且可靠归属的文件。

**责任边界：** 当前绑定决定能否继续设备工作；原文件事实决定能否登记产物。两个判断各自保持。复合申请沿既有 `FinishCaptureCommand` 保存产物、终态和父计划，原 key 重送核对完整输入并返回原业务结果。

**技术环境：** Python 3.11、现有 SQLite 事务与事件守卫、v1 RESULTS、设备文件及产物模型；不增加依赖或持久格式。

**正式依据：** [设备绑定异常的影响范围](../../architecture/configuration.md#设备绑定异常的影响范围)、[取消、失败与文件保留](../../architecture/camera-capture.md#取消失败与文件保留)、[取消动作的完成时机](../../architecture/task-cancellation.md#取消动作的完成时机)、[公共完成与失败契约](../../camctl/database/transactions.md#公共完成与失败契约)、[已提交结果与当前状态的边界](../../camctl/persistence-runtime.md#已提交结果与当前状态的边界)、[正式产物与来源](../../camctl/database/file-fields.md#正式产物与来源)。可靠 CLOSED 且不再需要设备的收尾继续遵守[本地收尾计划](2026-10-09-camctl-closed-result-local-consumption.md)。

## 起点与交付范围

起点为 `4eee248`，即照片与延时摄影本地收尾完成后的提交。2026-10-09 实际读取该提交的生产代码，并核对部署解释器为 Python 3.11.16。执行时由根 Agent 再记录 HEAD、工作区及门禁范围。

正向验收采用普通 timelapse 的可靠 UNCONFIRMED/CLOSED 输入：原实际 START、等待完成、RESULTS、完整文件及原结论 G 已保存，动作仍 running；之后通过公开取消事务生效取消，原活动没有可靠 ENDED，尚无 STOP。关闭旧连接后，以 fresh Owned（重新打开的自有连接）、空会话保存集合及 missing/mismatch 配置推进。mismatch 使用合法已登记的新驱动，不能把装配错误混入绑定异常。

共同生产边界须分类 photo/timelapse 的原 RESULTS 与已登记文件，不把“没有文件”与“文件输入不可解释”合并。当前正向门禁重点是 canceled timelapse＋CLOSED；一般 ACTIVE RESULTS 历史与普通失败须记录审计结论和待扩展验收，不以四项样例宣称整类拍摄失败完成。录像取消的内容放弃规则保持，不向它推广本阶段的文件保留生产者。

## 输入、输出与状态分类

完整申请在首次写入前固定动作、事实时刻、绑定失败详情、取消分支、原未完成责任集合、适用 STOP/CHECK 配置及全部产物草稿和目录事实。文件大小、名称、媒体类型、摘要与可靠归属来自原设备文件及历史，不由当前驱动重新解释。

| 原事实与当前条件 | 应执行的处理 |
| --- | --- |
| 旧保存责任未可靠完成 | 先核原申请及原 key；不按当前绑定重新决定。 |
| 动作已终态 | 保持原动作与产物；handler 重入不追加历史或设备调用。原 key 重送另按完整申请契约核实。 |
| 已有实际 RUNNING 调用 | 跟踪实际拥有者或沿原恢复规则保存结果；不发第二个调用，不用绑定失败代替实际结果。 |
| 依赖读取失败、身份矛盾，或已有原文件但缺少可解释 RESULTS 输入 | 停止并诊断；不形成空草稿，不保存依赖终态或部分产物。默认入口按既有状态库错误规则停止。 |
| 没有原文件，也没有原 RESULTS 责任或实际结果，且这些不存在已可靠确认 | 使用合法空产物输入；动作及必要流程仍按实际启动和取消事实收场。不存在不等于读取失败。 |
| 有合法实际 RESULTS，观察明确为空或文件可靠未完成 | 保持实际观察及诊断，不虚构产物；完整申请可包含空草稿。 |
| 普通 photo/timelapse 已有可靠 CLOSED，且无需设备 | 走已交付的本地失败收尾，登记合格文件；不改成绑定失败。 |
| 取消 timelapse 已有可靠 ENDED 或合法 STOP 终态，且无需设备 | 走已有本地取消收尾；原 STOP 失败／未知不改为成功。ENDED 加开放 STOP 的取消汇总责任另列。 |
| 取消 timelapse 仍需 STOP，当前绑定 matched | 沿原 STOP 身份、次数及间隔继续；本计划不新增设备资格。 |
| 取消 timelapse 仍需 STOP，当前绑定 missing/mismatch，原结果和文件可靠 | 不调用设备；必要 STOP 以绑定失败结束，合格文件与目标 canceled 同事务提交，原 CLOSED RESULTS 与采集结论保持。 |

正向分区的停止及文件结果如下。文件登记不证明集合成功，也不证明设备已经停止。

| 对象 | 必须保存或保持的事实 |
| --- | --- |
| 新 STOP | `responsibility_key=stop/<action_id>`，kind 为 STOP，action/activity 关联原真实 ID；按本次实际 `stop_config` 保存额度、超时、间隔。没有设备派发时 attempts_used 为 0，不新增 operation_attempts；流程为 FAILED，错误为 `device_binding_unavailable`，stage 为 `execution`。 |
| 已有未完成 STOP／其他适用责任 | 使用原固定责任集合结束；已用次数、实际尝试、效果及原配置保持，不重建已有责任。 |
| 原 RESULTS 与结论 G | 原 key、时刻、全部事件、次数、配置、错误、重试状态及集合／采集结论保持；不另建 RESULTS，不把 UNCONFIRMED 改成 SUCCEEDED/COMPLETE。 |
| 目标动作 | 取消标记保持，终态为 canceled；绑定失败由必要流程表达，不能把已生效取消改为普通 failed。 |
| 原文件与正式产物 | 每个符合登记契约的完整原文件成为正式产物，保留原身份、大小、名称、媒体类型及配对。前轮完整文件不因末轮 FAILED 且无观察而丢失。 |
| 设备活动与占用 | 保留原未知／未结束事实；绑定失败、取消终态和产物登记均不构成 ENDED 或释放证据。 |
| 取消发起者 | 按实际有限处理汇总；STOP FAILED 不能变为取消成功。软件前置须核到逐项结果及发起者失败，不以目标 canceled 代替。 |

产物、目标终态、父计划及必要 STOP 失败属于一个完整事务。原历史前缀逐行保持；本次未发起的 START、RESULTS、STOP、QUERY、READ、DELETE 均不新增尝试或消耗次数。

## 数据流与建议实现边界

数据流为：公开受理与真实 START → 原 RESULTS 实际结果及文件登记 → 原结论 G → 公开取消生效 → fresh Owned → 原申请恢复与终态／在途检查 → 当前绑定判定 → 可靠原文件输入 → 完整 `FinishBindingFailure` 申请 → 产物、目标及必要流程共同事务 → 可观察输出与取消结果。

下面列的是实现预估，接口名称和拆分允许执行者根据实际数据流调整；行为契约及验收不变。

| 文件／接口 | 建议负责的工作 |
| --- | --- |
| `apps/camctl/src/camctl/capture/handlers.py` 的 `_binding_failure_request`、`_handle_binding_failure` | 在实际调用可靠结束后，为 photo/timelapse 从原 RESULTS 和已登记文件形成完整产物输入；复用 `_saved_result_listing`、`_registered_result_files`、`_catalog_drafts`，不调用当前设备。 |
| `apps/camctl/src/camctl/persistence/repositories/capture.py` 的 `FinishBindingFailure`、`_FinishBindingFailureCommand` | 承载并核对完整目录申请；复用 `FinishCaptureCommand` 的登记和守卫，与原必要流程共同提交；修正复合事件切分及严格重送。 |
| 同文件的 `FinishCaptureCommand._reuse` | 审计共享重送的目录校验及原响应来源。可在绑定复合边界补齐所需核对；若提取共同实现，须覆盖普通及取消消费者回归，不能宣称原有全部重送缺口自动闭合。 |
| `apps/camctl/src/camctl/outputs/catalog.py` | 复用已有目录纯校验，不直接插入 outputs，不复制一套登记规则。 |
| 新 capture/bootstrap 测试及既有绑定事务门禁 | 用真实协作者验证消费者、复合仓储、默认工厂及原 key 复验。 |

建议新增内部目录输入采用有限、可完整核对的形式：非空草稿是 `tuple[OutputDraft, ...]`，只承载设备文件的 ORIGINAL/PREVIEW，`file_complete is True`、`sha256 is None`；目录 action 与请求一致，`ownership_confirmed is True`。摘要仍从可靠文件事实取得。空草稿采用 `catalog_facts=None`，保持旧空申请兼容。若采用该形式，普通分支适配既有 `FinishCapture` 时仍需形成其合法空 `OutputCatalogFacts`；不能把外层 None 直接传入而破坏普通空登记。此项是内部 API 建议，不增加公共机器协议或持久申请格式。

## 复合事件与原 key 模型

实际源码定义 `ACTION_FINISHED=8`、`OUTPUT_REGISTERED=20`、`PLAN_STATUS=9`、`OPERATION_CONFIGURED=10`。本阶段原片／预览的 capture 组为：8 首事件 → 零个或多个 20 产物事件 → 可选末尾 9 计划事件。20 的产物种类采用登记枚举，预览同事件包含 `output_origins`。该组之后才是原流程结束、既有 READ 业务组及适用新必要流程；原顺序和完整范围继续校验。

基线 `_FinishBindingFailureCommand._reuse` 只按 8/9 切分，会把首个 20 当成责任／READ 事件。修复须识别上述完整 capture 语法并复用实际登记核对，不能仅放宽事件白名单。原有 READ、机会释放和流程事件的责任、正文、时刻及顺序校验保持。

基线 `FinishCaptureCommand._reuse` 比较草稿 kind/file ID，核部分既有产物关联，但不重验目录 action/归属、完成标记及同批配对；返回的 `plan_status` 来自当前计划。这是已存在的共享缺口，本阶段新增绑定文件入口必须保证下表，不凭调用该函数声称完整核对。

| 重送条件 | 必须得到的结果 |
| --- | --- |
| fresh Owned，同原完整 request/key | COMPLETED，恢复原 action_status、plan_status、output_ids；沿既有定义返回 ALREADY，不新增事务或设备调用。 |
| 删除／追加／替换草稿，改变顺序、种类或配对，或改变目录、完成依据、绑定错误、时刻、取消分支、适用配置／责任集合 | 原键冲突被拒绝，数据库完整保持。若某字段采用前述严格形式而无法构造，则在输入边界明确拒绝，不能默许重送。 |
| 原 G 后有合法新历史，甚至同计划兄弟动作完成导致计划状态变化 | 仍返回原 G 的业务响应；不按当前计划或当前文件状态重判原登记资格。 |
| 原事务组缺少产物、必要流程或包含越界事件 | 拒绝不完整／不同责任的重送，不将部分组当作成功。 |
| 原 key 查询／原 H 恢复失败 | 停止并保留诊断；不能使用当前状态或空结果替代。 |

原 H 是本次复合 G 之前的完整事务边界；当前 C 从可靠 scope 取得。需要历史恢复时使用已有 `read_row_values_at_boundary`，不能用半个事务的事件位置。原 plan_status 可取 G 中计划事件的 after；没有计划事件时从原 H 恢复。产物响应顺序按首次分配的 output ID 恢复，不能按事件出现次序推断：`FinishCaptureCommand` 先输出原片事件，但 ID 由输入顺序分配。配对须核原 20 中的 `output_origins` 与原文件关系。

## 任务一：核有效消费者红色反例

**消费接口：** `capture_handler("camera_timelapse")`、公开取消事务、真实 `session_capture_assembly` 与 `capture_flow`。

**测试文件：** `apps/camctl/tests/integration/capture/test_binding_failure_keeps_timelapse_files.py`；`apps/camctl/tests/integration/bootstrap/test_binding_failure_retained_files.py`。

- [x] 建立上述公开 CLOSED 前置，再公开 ApplyCancel；原 STOP 不存在、活动没有结束依据。覆盖 missing/mismatch × 单轮完整文件／前轮完整末轮 FAILED 无观察，各四项。核独立 action/activity ID、原完整文件元信息、fresh Owned 和空会话集合。
- [x] 在产物断言前核零调用、STOP FAILED/0 次、目标 canceled、取消发起者实际失败、原 G 与 RESULTS／采集／文件／实际尝试保持。有效红只能在这些前置通过后失败于缺少合格正式产物。
- [x] 根独占运行两个文件，分别记录日志。fixture 的不存在列、非法输入或提前失败先修 fixture，不改产品预期。2026-10-09 capture 四项已在上述前置后失败于 outputs 为 0；bootstrap 首次不存在列错误不属于有效产品红，须修正后重验。

## 任务二：形成完整申请并保存共同事务

**消费接口：** 任务一原文件输入、`CaptureRepository.finish_binding_failure(request, key, owned)`、`FinishCaptureCommand` 和目录校验。

**实际测试入口：** `apps/camctl/tests/integration/capture/test_binding_failure_retained_file_transactions.py`；原申请恢复及读取故障另在 `test_binding_failure_file_input_recovery.py`。

- [x] 先以公开仓储建立合法完整文件申请红例，断言输出身份、元信息及配对、目标终态和 STOP 失败的 transaction ID 相同；原结论、实际调用及历史前缀保持。保留旧空申请和普通绑定失败控制。
- [x] 为共同 photo/timelapse 生产者分类：无原文件且无实际 RESULTS 是可靠空输入；有原实际观察则完整解释；已有文件却无 RESULTS 元数据、身份或归属则诊断。不得因末轮无观察回退为空，不重复保存原文件或采集结论。
- [x] 在最窄复合边界承载草稿及目录输入，复用既有登记和事件守卫。新 STOP 使用实际 action/activity、原责任键和本次配置，零派发不造尝试；原 CLOSED RESULTS 不进入需结束责任集合。
- [x] 根复验本任务和任务一 capture 文件；正常登记通过后再进入原键测试。审计一般 ACTIVE RESULTS／普通失败已有文件的共同路径，明确哪些已实现但仍缺端到端证据，哪些仍需后续生产者，不能仅按正向 fixture 的状态硬编码处理。

## 任务三：先证伪重送，再核失败与恢复

**消费接口：** 任务二完整复合 request/key、真实 `open_existing`、原历史边界恢复及仓储回执。

- [x] 在正常保存后关闭连接，以 fresh Owned 重送同申请同 key；先记录包含 20 的旧切分失败，再修 capture 组语法。断言原 output_ids 和业务响应、ALREADY、完整 dump 不变及无新事务。
- [x] 逐项证伪删除草稿、两份草稿顺序交换、合法构造的同批配对改变、目录 action/归属、完成标记、绑定错误、时刻、STOP 配置和责任集合变化。配对 fixture 用真实文件观察和守卫准备；不通过 SQL 拼造合法原事务。
- [x] 建立同计划合法未终态兄弟动作；首次复合保存后通过公开仓储结束兄弟，使父计划状态变化，再 fresh Owned 原键重送。返回首次 plan_status/output_ids，原 G 保持，不重新分配产物。修复原 H/C 响应恢复，不读取当前计划作为原结果。
- [x] 在原 RESULTS 输入读取边界注入实际错误，覆盖 missing/mismatch；断言原 G 保持、目标仍 running、STOP 和 outputs 尚未生成，无设备调用。可靠恢复后一次登记。
- [x] 在真实 outputs 投影写入后注入可确认回滚错误，覆盖 missing/mismatch；断言原异常及 ROLLED_BACK、部分 outputs 回到 0、目标／STOP／全 dump 回滚、无遗留事务。只统计故障所在事务，不能将正常只读事务的 ROLLBACK 当成故障回滚。
- [x] 对仓储完整申请注入 COMMIT 前／后 UNKNOWN，关闭旧连接以满足已有 no-late 前提，再 fresh Owned 使用原完整 request/key 核可靠不存在／存在。均须一次完整保存，原 key/时刻/目录保持；原键查询失败停止，不能再造申请。此测试由调用者保留输入，不能作为消费者 holder 已实现的证明。
- [x] 对 handler 终态重入、默认 flow 再运行断言完整 dump、产物身份和调用次数不变。根顺序复验新仓储文件及既有绑定取消事务；内部 READ 与原零文件事务不得回归。

## 任务四：默认入口、门禁与范围交接

- [x] 根复验 bootstrap 的四项；必须使用真实工厂和 flow 打开连接，不以手工 runtime 或截停候选代替。产品改动的有效红依据为 capture 四项；bootstrap 初次非法 SQL 前置不计产品红，修正后作为默认入口组合证据。
- [x] 顺序运行新 capture 文件、新 bootstrap 文件、已有 CLOSED／首次耗尽及绑定取消／事务门禁，再运行全 unit。按目录分开，每个 pytest 结束后才启动下一个；记录实际命令、exit code、计数、日期、环境和归因。
- [x] 独立核源码与日志，确认从原实际输入到产物／目标／STOP 复合事务，再到原键、回滚、UNKNOWN 仓储复验和重入的链路。按持续授权整体 checkpoint；提交拆分由实际协作决定。

根执行的建议命令如下；新增仓储测试若调整文件名，应先同步本计划的实际门禁入口。

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture/test_binding_failure_keeps_timelapse_files.py apps/camctl/tests/integration/capture/test_binding_failure_retained_file_transactions.py apps/camctl/tests/integration/capture/test_binding_failure_file_input_recovery.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_binding_failure_retained_files.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture/test_closed_result_local_consumption.py apps/camctl/tests/integration/capture/test_closed_result_local_boundaries.py apps/camctl/tests/integration/capture/test_timelapse_exhaustion_files.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_closed_result_binding_recovery.py apps/camctl/tests/integration/bootstrap/test_binding_canceled_results.py apps/camctl/tests/integration/bootstrap/test_binding_canceled_result_transactions.py apps/camctl/tests/integration/bootstrap/test_binding_transactions.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q
```

## 禁止捷径与明确未完成

不得直接插入 outputs、先提交目标终态再补产物、全局跳过绑定、用新驱动读取旧定位、将文件完整推成集合成功、改写原核实错误、补造设备结束或吞掉读取错误。原键核对不得退化为“事务存在”或只核草稿 identity 集合，不得按当前状态重新判定原登记。

生产入口审计须分别列出：本阶段 canceled timelapse＋CLOSED；一般 photo/timelapse ACTIVE 结果及普通失败的完整文件；已有本地 CLOSED 分支；录像的独立取消内容规则；内部 READ 复合业务、restricted/residual/winddown 接线。前两类共享文件输入边界，后几类保持既有资格和事务规则，不增加新业务入口。

`FinishCapture`／`FinishBindingFailure` 等消费者在 UNKNOWN 后持有完整申请的共同责任仍未闭合，留待统一保存申请阶段；本计划不新增绑定专属 holder。仓储原 key 复验绿色只证明给定原完整输入能够核实保存，不能证明 handler 在 UNKNOWN 后仍持有原输入和时刻。ENDED 加开放 STOP 的取消发起者闭合也保持独立。

普通 UNSATISFIED reason、应急错误身份、ResultSet 原申请输入身份及 RESULTS v2 均不作为本计划前置或决策对象。共同 `FinishCaptureCommand` 的其他完整输入重送缺口须如实列出，不能以本新增有限申请的严格核对宣称全部消费者完成。阶段交付仅对应实际源码和门禁，不代表 full app 验收。

## 实施与验证记录（2026-10-09）

环境为 Linux 开发容器、Python 3.11.16，使用已有 `apps/camctl/.venv`。根 Agent 独占、前台、按目录顺序执行 pytest；设备通过受真实接口约束的替身提供，不包含真实设备与物理断电验收。

共同文件生产者为 photo/timelapse 读取原 RESULTS 与文件事实，明确区分无责任、零尝试且无文件、合法实际输入和不可解释已保存文件。最新原观察尚未登记时沿原观察时刻调用现有文件登记，再按同一条目顺序形成草稿。取消 timelapse 仍需停止且本次绑定不可用时，有限目录输入随 `FinishBindingFailure` 与目标 canceled、必要 STOP FAILED/0 共同保存。原 CLOSED、错误、配置、实际尝试与活动占用保持。

非空申请限定原可靠完整设备文件的 ORIGINAL/PREVIEW、确认归属与原动作、未知输入摘要；空草稿不携带目录事实。原键按 `8 → 20* → 9?` 核 capture 组，再核既有责任与 READ 组；产物顺序按首次分配 ID 核实，全部配对按原 `output_origins` 核实。首次父计划响应从原 G 的完整末边界恢复；原 G 有计划事件时得到其 after，没有计划事件时等于原前 H 的状态。依赖读取失败停止，不使用当前计划替代。

共享草稿资格只登记完整原片，以及其原片也可登记的完整预览。原片未完成、预览完整且还有另一完整原片的真实组合证明：保留三个源文件事实，只登记另一完整原片，不因不可登记的预览拒绝整批收尾。

有效红色反例依次为：直接消费者四项缺 outputs；正常登记通过后，fresh Owned 原键四项把产物误判为 READ；目录与完整输入十三项失败、八项控制通过；公开结束同计划兄弟动作后的原响应一项失败、两项读取控制通过。bootstrap 首次四项是非法 SQL 前置，不计产品有效红，修正后验证真实默认工厂与 flow。故障代理只统计发生错误的事务，成功只读核实的 ROLLBACK 不计为故障回滚。

旧组件种子计划现在提供一致的创建行历史、对象目录、报告变化与快照维护进度。新原计划恢复曾使九项旧绑定事务暴露缺少计划目录；补齐种子后，原事务、错误、尝试及未知提交预期继续成立，没有放宽生产恢复规则。

### 实际门禁

所有 pytest 命令从仓库根目录使用 `PYTHONPATH=/workspaces/camctl/apps/camctl/src apps/camctl/.venv/bin/python -m pytest`，接下面的路径与 `-q`；完整输出重定向到对应日志。目录没有合并进同一个进程。

| 范围 | 结果 | 日志 |
| --- | --- | --- |
| 新目录和绑定文件事务两个 capture 文件 | 35 passed，20.41s，exit 0 | `/tmp/camctl-goal-binding-files-capture-catalog-final.log` |
| 三个新 capture 文件，加既有 CLOSED、首次耗尽、耗尽保存恢复、取消文件与录像重试六文件 | 135 passed，71.10s，exit 0 | `/tmp/camctl-goal-binding-files-capture-scoped-final.log` |
| 新 bootstrap 文件、CLOSED 默认恢复、旧绑定取消与事务，加内部 READ 原键、实际读取后绑定恢复及必需源摘要三文件 | 69 passed，25.29s，exit 0 | `/tmp/camctl-goal-binding-files-bootstrap-extended-final.log` |
| 整个 `apps/camctl/tests/integration/capture` | 605 passed、11 failed，165.25s，exit 1 | `/tmp/camctl-goal-binding-files-capture-all.log` |
| `apps/camctl/tests/unit` | 3897 passed、1 skipped、2 warnings，8.72s，exit 0 | `/tmp/camctl-goal-binding-files-unit-final.log` |

两项 warning 为既有同步测试的 asyncio 标记。全 capture 的十一项失败须继续分别处理，不作为本阶段绿色门禁或完整 app 已完成的证据：

| 失败入口 | 实际失败与责任 |
| --- | --- |
| `test_capture_contract::TestTimelapseHandler::test_send_wait_then_finish`；`test_timelapse_wait_runtime::test_backward_wall_clock_change_does_not_extend_current_session_wait` | v1 条目未提供集合结束依据，动作保持 running，测试却预期 succeeded；普通集合结束与 RESULTS v2 仍待确定。两个入口在此前完整 capture 日志中已有相同失败。 |
| `test_capture_failure_activity_identity::test_closed_unsatisfied_timelapse_reports_actual_activity_without_query`；`test_result_confirmation::TestResultSetConfirmation::test_unsatisfied_saves_known_failure_and_keeps_occupancy`；`test_result_consumer_saves::test_closed_result_consumers_use_saved_input_without_device_query[timelapse]`；`test_result_file_recovery::test_closed_latest_error_keeps_previously_registered_file_input[timelapse]` | 构造 UNSATISFIED 输入时错误缺 stage，被公共结构校验拒绝；正式错误 reason 及输入修正按结果错误计划推进，不补猜测值。 |
| `test_emergency::test_zero_attempts_unknown_config_saves_not_attempted`；`test_later_session_preserves_exact_old_error_and_omits_unchanged_activity[NOT_ATTEMPTED]`；同入口 `[UNCONFIRMED]`；`test_unconfirmed_with_attempts_saves_unconfirmed`；`test_unrecorded_emergency_does_not_release` | 应急生产错误缺 details，被活动结果守卫拒绝；正式错误身份与详情仍待决定。五项与结果错误计划的既有记录一致。 |

一般 photo/timelapse ACTIVE 实际结果和普通绑定失败的文件输入已经进入共同生产者，完成源码分类审计，但本阶段没有新增该分区的独立端到端矩阵；最新观察尚未登记也只核到源码责任。原申请消费者 UNKNOWN 持有、共同 FinishCapture 其他重送输入、ENDED 加开放 STOP 汇总和 ResultRunClose 的录像类型限制继续独立跟踪。仓储 UNKNOWN 反例由调用者保存完整原申请，不能代替这些消费者责任。

独立只读审查核两处生产边界、三个草稿消费者、完整目录与原计划恢复、真实公开前置和已取得日志，未发现本阶段生产阻断。阶段提交只保存本计划限定的实现与验证，不声明全部拍摄流程或完整 apps/camctl 已通过。
