# 客户端协议样例

[返回设计总览](../2026-09-08-camctl-cli-design.md) · [请求受理与会话](protocol-session.md) · [状态报告与累计确认](status-reports.md)

本专题提供四组完整、可单独解析的执行计划和状态报告 JSON，以及客户端应得到的业务结果。报告按[按计划嵌套与客户端合并](status-reports.md#按计划嵌套与客户端合并)组织；业务规则由各责任专题定义，本文集中说明样例使用的机器字段及关联方式。

实际使用遵守[客户端的人工交接流程](../2026-09-08-camctl-cli-design.md#客户端的人工交接流程)：用户从网页导出计划 JSON 文本并递交给传输部门，收到该部门提供的报告 JSON 文件后，在网页上选择文件导入。样例中的 ACK 输入和补取计划也按相同方式交接。

首次建立运行环境并通过主程序检查报告交接时，可先使用[部署联调样例](deployment-example.md)中的无设备计划，再按本专题演练拍摄与产物协议。

[计划文件诊断样例](#计划文件诊断样例)另行展示文件无法解析、无法打开、ACK 校验失败和同次输入多个错误的 JSON 片段，以及重复输入和动作自身错误的区别。

客户端本地状态丢失后的请求入口、ACK 输入及较早完整报告先被确认的例子见[完整状态同步推演](status-sync.md#较早完整报告先被确认的推演)。这些例子沿用本专题的对象合并规则，不重放设备动作。

## 阅读与使用范围

四组样例分别代表四份独立的单主机数据库历史，不能将不同组的报告混合接收到同一个客户端状态库中。各组从尚无客户端业务状态、样例业务水位为 0 开始；相同数字的 `report_id` 在不同组中没有关联。ID、业务水位、文件大小、录像计时及设备结果均为演示数据，序号间距不表示只发生了对应数量的设备调用，也不规定正式 ID 的生成算法。公共输入类型和范围见[执行计划输入契约](plan-input.md)。

所有样例时间均按 UTC 解释，使用[时间字面量](plan-input.md#时间字面量)中的秒级格式。协议演练使用样例驱动的 `timed` 参数类型，以 `duration_s` 字段指定 60 秒目标时长；这组类型名称和字段仅服务于样例输入、报告和合并练习。实际相机的正式参数类型依据设备能力另行设计，真实输入和 `camctl describe` 使用其正式定义。样例驱动的定义可供协议测试替身使用，不能据这些 JSON 宣称实际设备已支持对应参数。

样例分别表达控制过程计时与未检查的实际媒体时长。样例不包含真实视频，视频的大小及重复字符组成的 SHA-256 是演示值，不能作为真实设备或视频核验的证据。

状态报告文件则使用实际 JSON 字节计算 SHA-256，并将完整摘要放入文件名。这些文件采用 UTF-8、两空格缩进和末尾一个换行，可以直接用于报告摘要校验练习。这里固定的是所提供样例的字节，不替代全部报告的最终编码、字段范围和排序规范。

四组均假定状态库、本地文件交接和会话收尾成功；动作失败不表示 camctl 会话失败。第三方主程序只中转文件，不解析下面的业务字段。样例只展示指定的报告机会；实际运行中的其他报告机会及 `ready` 替换继续遵守[报告生成点](status-reports.md#报告生成点)。

## 文件入口

| 场景 | 客户端输入 | camctl 状态报告 | 后续客户端文件 |
| --- | --- | --- | --- |
| 录像与取回成功 | [执行计划](examples/client-protocol/01-success/plan.json) | [完成报告](examples/client-protocol/01-success/status-report-1-6823c03f7a0cd92ca55f0669f329eb1a27031e45ff97247296d3489ae9518441.json) | [携带累计 ACK 的原请求重送](examples/client-protocol/01-success/ack-plan.json) |
| 单个动作参数错误 | [执行计划](examples/client-protocol/02-action-invalid/plan.json) | [受理后的报告](examples/client-protocol/02-action-invalid/status-report-1-51f95d6145546dead701a5ac80b9b26f5c7f1403db91925bdf4888db4327d059.json)、[完成后的增量报告](examples/client-protocol/02-action-invalid/status-report-2-1b8c3518727aeaaa7b90079c36a9fc99c5d03d701986e9b9021a8e125a79852a.json) | [两份报告之间的 ACK 输入](examples/client-protocol/02-action-invalid/ack-plan.json)、[合并后的计划记录](examples/client-protocol/02-action-invalid/client-merged-plan.json) |
| 组取回部分失败 | [执行计划](examples/client-protocol/03-obtain-partial/plan.json) | [完成报告](examples/client-protocol/03-obtain-partial/status-report-1-289511e228f994b7ec11f8775702bf1fe4a74c391dddfcb62bbc9f89fedc5fc6.json) | [补取失败原片的执行计划](examples/client-protocol/03-obtain-partial/retry-plan.json) |
| 组内一个录像没有产物 | [执行计划](examples/client-protocol/04-source-no-output/plan.json) | [完成报告](examples/client-protocol/04-source-no-output/status-report-1-3e7a27c2c1979ef475ab8d63aca12e14506fb21b689f20f3d9c48d320bc77001.json) | 按来源动作解释无产物失败，保留另一份成功交付 |

输入与输出均提供全部文件内容，没有省略标记。`client-merged-plan.json` 是客户端合并结果，既不是 camctl 输入，也不是待 ACK 的报告文件。

## 样例一：录像与取回成功

输入请求 `req-001` 包含两项动作：09:00 开始“主录像”，09:02 开始“取回主录像”。录像参数及允许的启动延迟由计划指定，通信预算来自部署配置。

本次历史按以下顺序成立：

1. camctl 受理为计划 `p-001`，登记录像动作 `a-001` 和取回动作 `a-002`。
2. 录像启动和停止均在首次尝试成功，确认本次控制计时达到 60 秒、对应文件已写完；同一终态事务登记原片 `o-001` 和录像成功。
3. 取回到达计划时间后复制原片，可靠保存全部字节，主机摘要与相机源端摘要一致。样例记录此时已经取得的产物摘要。
4. 交付 `d-001` 的文件 `d-001.mp4` 完成发布，取回动作成功；计划全部动作进入终态，状态为 `completed`。
5. 会话收尾报告覆盖 `(0, 100]`。本场景假定此前报告尚未被客户端累计确认，因此这份报告包含本次完整业务变化的最终快照。

报告中的所属与引用关系为：

```text
p-001
├─ a-001 主录像：succeeded
│  └─ o-001 原片：available，保留在相机
└─ a-002 取回主录像：succeeded
   └─ d-001：published，output_id = o-001
```

| 客户端取得的证据 | 对用户表达的结果 |
| --- | --- |
| `p-001.request_id = req-001` | 请求已经受理 |
| `p-001.status = completed` | 计划执行结束 |
| `a-001.status = succeeded` | 录像成功；实际媒体时长尚未检查 |
| `a-002.status = succeeded`、`d-001.status = published` | 文件已交给主程序中转 |
| 实际收到 `d-001.mp4`，且大小和 SHA-256 均与报告一致 | 客户端已收到并核验这份交付文件 |

客户端通过实际 `file_name` 查找交付记录，再关联 `output_id` 和来源动作。`display_name` 为人类提供完整名称，不是关联键。报告中的 `published` 是已完成本地交接的事实，不证明文件此刻仍在 `ready`，也不代替客户端自己的文件接收状态。

客户端完成报告校验、合并和持久化后，可以提交本组 `ack-plan.json`：它保留同一个 `request_id` 和原计划内容，仅增加 `last_report_id: 1`。camctl 复用原计划与动作，独立吸收累计确认；不会再次录像或创建新的 delivery。即使视频尚未收到，也不影响报告本身的 ACK 资格。源视频继续保留，ACK 不授权删除。

## 样例二：单个动作参数错误，其他动作继续执行

输入请求 `req-002` 中，“主录像”的参数合法；“取回录像”引用的本计划动作名为“不存在的录像”。JSON、计划公共结构和动作名称约束均合法，因此计划受理为 `p-002`，仅取回动作 `a-102` 在受理时登记为 `failed`。本场景专门验证 camctl 的防御校验，客户端通常应在生成计划时发现这类错误。

为了明确第一份报告的生成机会，本组假定主程序已通过 `submit` 受理计划，之后在 09:00 之前实际启动裸 `run`；恢复/对账后的报告包含已保存的受理事实，随后会话继续等待合法录像动作的执行时间。该场景不要求主程序解析动作错误。

第一份报告覆盖 `(0, 10]`：

```text
p-002：pending
├─ a-101 主录像：pending，尚未执行
└─ a-102 取回录像：failed，受理校验失败，尚未执行
```

`a-102.error` 给出阶段 `admission`、错误码 `source_action_not_found`、字段路径 `actions[1].params.source.action_name` 和实际错误值。路径中的动作下标从 0 开始，对应首次提交的完整计划；不能用本份报告 `actions` 子集中的排列重新计算错误位置。

此时客户端可以同时表达“计划已受理”和“取回动作参数错误”。失败动作没有设备或文件执行尝试，也没有 delivery；计划还有未结束的合法动作，因此仍是 `pending`。

本组假定第一份报告已被主程序领取并送达，客户端校验并持久化后提交 `ack-plan.json`。该输入在第二份报告冻结前被 camctl 成功吸收，确认水位推进至 10；重复请求继续复用原动作。这个明确前提使第二份报告的下界可以是 10，而不是把未确认变化错误排除。

09:00 合法录像按原参数执行并完成，登记原片 `o-101`。没有合法取回动作读取该原片，因此报告中的媒体检查为未执行，摘要为尚未取得，原片仍可供后续新请求取回。

第二份报告覆盖 `(10, 30]`，只包含发生变化的 `a-101` 及承载它的计划：

```text
p-002：completed
└─ a-101 主录像：succeeded
   └─ o-101 原片：available
```

`a-102` 没有新变化，因此不重复发送。客户端按 ID 合并后得到：

```text
p-002：completed
├─ a-101 主录像：succeeded
│  └─ o-101 原片：available
└─ a-102 取回录像：failed，仍保留原错误
```

完整预期结果见本组 `client-merged-plan.json`。客户端不能用第二份报告的 `actions` 数组替换本地整个动作集合，否则会丢失 `a-102`；也不能把计划 `completed` 显示成所有动作成功。

客户端若要纠正取回目标，应使用新的 `request_id` 提交合法取回。以原 ID 修改正文不会修正已经受理的失败动作。

## 样例三：组取回部分失败

输入请求 `req-003` 中，“第一段录像”和“第二段录像”属于“早间采集”组，分别在 09:00、09:02 开始；“取回整组”在 09:04 开始，通过 `params.source.group` 选择来源。取回动作本身没有动作公共字段 `group`。

两段录像均正常完成，分别登记相机原片 `o-201`、`o-202`。取回为每份产物创建独立交付：

| 项目 | 文件处理 | 最终结果 |
| --- | --- | --- |
| `d-201`，对应 `o-201` | 首次读取成功，完整落盘并通过摘要比较 | 发布 `d-201.mp4` |
| `d-202`，对应 `o-202` | 首次读取已可靠保存 1 MiB，随后连续 10 秒无数据；后两次从可靠进度重试，也分别连续 10 秒无数据 | 三次读取均失败，结束该文件处理；未发布 `d-202.mp4` |

本场景使用已确认的默认读取上限、无数据超时和独立重拷额度，具体约束见[按文件累计读取尝试](outputs.md#按文件累计读取尝试)、[文件读取的无数据超时](outputs.md#文件读取的无数据超时)和[摘要不一致后的有限重拷](outputs.md#摘要不一致后的有限重拷)。读取重试之间先确认旧读取已经停止，并在等待期间保留该相机的拷贝机会；重试间隔来自已固化的本地配置，遵守[通信重试间隔](scheduling-execution.md#通信重试间隔)。

`d-202.copy.committed_bytes` 为 1048576，是最后可靠保存的进度，不是整个视频长度。本场景尚未完成全片读取及摘要校验，`verification.status` 为 `not_performed`，额外重拷使用数为 0。失败文件的读取和重试已停止，半成品已清理；历史进度及文件名占用继续保留，相机上的正式原片不删除。

两个来源均已结束、全部产物准备结果均已确定后，成功文件才按组取回发布屏障发布。待 `d-201` 的本地交接完成，取回动作 `a-203` 以 `failed` 结束，最终报告覆盖 `(0, 100]`：

```text
p-003：completed
├─ a-201 第一段录像：succeeded
│  └─ o-201 原片：available
├─ a-202 第二段录像：succeeded
│  └─ o-202 原片：available
└─ a-203 取回整组：failed
   ├─ d-201：published，output_id = o-201
   └─ d-202：failed，output_id = o-202，三次读取耗尽
```

客户端应显示“两段录像成功；整组取回部分失败，一份已交给主程序，一份读取失败”。它可以独立接收并校验 `d-201.mp4`；不能因为取回整体失败就丢弃已发布文件，也不能把失败文件解释为源录像失败或原片已被删除。

如果要再次取回失败原片，客户端使用本组 `retry-plan.json` 提交新请求 `req-004`，通过 `source.action_instance_id = a-202` 和 `output_ids = [o-202]` 精确选择它。该文件假定客户端已经校验并持久化本组完成报告，因此同时携带 `last_report_id: 1`。这是新计划引用历史计划的动作及产物；引用格式在受理时校验，跨计划来源在执行时确认，不能将报告中的曾经可用理解为未来执行时仍保证可用。

新取回使用新的 delivery；不重置或继续已经终态的 `d-202`，也无须重复请求已经成功的 `o-201`。新请求在相机端实际执行读取后，仍按当时取得的结果报告，不因这份补取计划已经生成就宣告文件收到。

## 样例四：组内一个录像没有产物

输入请求 `req-004` 的两个录像动作属于“早间采集”组；取回动作仍通过 `params.source.group` 选择它们。计划公共结构和所有动作参数均合法，本组初始受理没有参数错误。

本次历史包括以下事实：

1. “第一段录像”`a-301` 开始执行，三次启动尝试均被设备明确拒绝，并可靠确认每次均未开始录像。本例假定三次尝试在允许的启动窗口内耗尽，因此动作为 `failed`，没有正式产物，也没有需要停止的持续录像效果。
2. “第二段录像”`a-302` 在自身计划时间正常执行，登记原片 `o-302` 并成功结束。
3. “取回整组”`a-303` 确认 `a-301` 已结束且没有正式产物，保存无产物失败项；不为它建立 output 或 delivery。
4. 对 `o-302` 创建 `d-302`，首次读取、完整落盘和摘要比较成功。两个来源均已完成产物判定，全部准备结果确定后，发布 `d-302.mp4`。
5. 取回因存在无产物失败项而以 `failed` 结束，计划为 `completed`。会话收尾报告覆盖 `(0, 100]`，本组此前没有已吸收的累计 ACK。

```text
p-004：completed
├─ a-301 第一段录像：failed，未产生正式产物
├─ a-302 第二段录像：succeeded
│  └─ o-302 原片：available
└─ a-303 取回整组：failed
   ├─ result.failures：来源 a-301，没有可取回产物
   └─ deliveries：d-302 已发布，引用 o-302
```

`a-303.result.failures` 中只有一项：包含 `source_action_instance_id = a-301` 和 `no_outputs` 错误，不包含 `output_id` 或 `delivery_id`。取回动作的错误说明它没有可取回的产物；来源录像动作自己的错误及启动尝试记录进一步说明为什么没有产物。客户端通过来源动作 ID 关联这两层原因，不把录像设备错误复制成取回动作的设备执行错误。

客户端应显示“第一段录像启动失败，没有可取回文件；第二段录像成功，文件已交给主程序”。它仍可接收并核验 `d-302.mp4`，不需要等待一个不存在的失败交付文件。

本组 `a-301.execution.started` 为 `true`，与样例二受理校验失败的 `false` 不同。它的停止尝试列表为空，控制计时和持续效果字段没有补造值；无产物也不以一个大小为 0 的虚假视频表示。

重新提交取回请求不会生成从未录制的第一段视频。客户端若仍需要该段内容，应根据业务需要安排新的录像计划；这与样例三针对仍然存在的原片重新取回不同。

## 样例五：取消完成后清理已有产物

本组接续样例一：客户端已完整保存报告 1，两个新计划均携带 `last_report_id: 1`。原片 `o-001` 已有正式登记及完整交付副本，本例允许由新的显式清理请求删除它。

| 文件 | 用途 |
| --- | --- |
| [capture-plan.json](examples/client-protocol/05-cancel-and-cleanup/capture-plan.json) | 新增录像 `a-501`；参数继续使用本文样例驱动，不能当作真实相机参数类型 |
| [maintenance-plan.json](examples/client-protocol/05-cancel-and-cleanup/maintenance-plan.json) | 通过原请求 `req-005` 取消该录像、显式清理原有 `o-001`，并请求进度报告 |
| [报告 2](examples/client-protocol/05-cancel-and-cleanup/status-report-2-2476f650c4debd7abdf29e72738a78614a05844e057e8bcbf878f7615acacd29.json) | 覆盖 `(100, 160]`；取消标记已经保存，停止调用仍在执行，取消动作保持 `running` |
| [报告 3](examples/client-protocol/05-cancel-and-cleanup/status-report-3-6f3d7fd5180da5a9e2ea0481587a5f0a2269205b5047193d894a974485eba5bb.json) | 覆盖 `(100, 200]`；停止与适用废弃内容清理已完成，取消成功，原有 `o-001` 的独立清理也已完成 |

客户端在生成取消计划时只需要自己分配的 `req-005`，不预先知道 `a-501`；实际关联由 camctl 执行取消时确定并报告。本例停止在清理动作到达计划时间前已确认完成，不要求相机一边录像一边删除文件。录像 `a-501` 最终为 `canceled`，没有登记正式产物；取消动作 `a-601` 为 `succeeded`，逐项结果指向 `a-501`。清理动作 `a-602` 只处理显式指定的旧产物 `o-001`，不改变其来源录像的成功终态，也不删除此前交付的独立副本。

报告动作 `a-603` 已通过发布报告 2 完成职责，报告 3 才携带其成功结果，因此 `a-603.result.report_id = 2`。这说明结果引用的报告 ID 不必等于当前文件的报告 ID。

报告 3 中原计划 `p-001` 只携带产物发生变化的录像动作。客户端保留以前收到的取回动作 `a-002` 及其 delivery，不能因为本次子集合缺项而删除它们。报告 2 尚未收到也可先处理报告 3；累计确认仍需结合已经保存的报告 1 判断完整覆盖。

## 样例六：取消已生效，但停止没有确认

[取消停止失败报告](examples/client-protocol/06-cancel-stop-failed/status-report-1-ec444e79152b0eda7b4c6cd55b17b45b0424c8b506e2a2a21b674a94bf65d458.json) 是独立数据库场景的完整快照，与其他组的报告 ID 不共享历史。相机已开始录像，取消到达后，三次有限停止尝试均失败；不再继续普通录像或重试启动，目标录像按取消规则结束。

此时 `a-501.status = canceled`，但其持续效果为 `compensation_failed`，设备事实仍为 `possibly_recording`。取消动作 `a-601.status = failed`，逐项结果保留停止次数耗尽的原因；废弃内容不能在录像状态未确认时被当作已经清理。

客户端应同时显示“录像任务已取消”和“停止相机未确认”。不能把取消任务失败理解为录像计划重新生效，也不能把目标 `canceled` 理解为设备已经停止。本例不创建正式产物或交付，不假定相机已停止，也不因停止预算耗尽重置次数。

`cancel_items_failed` 是取消逐项结果的汇总错误，阶段为 `execution`、`details` 为空对象；具体原因在 `result.items[].error` 中。样例中的设备拒绝和停止未确认错误表达由对应设备及处理边界产生，客户端即使尚未配置这些错误码的中文说明，也须保留码与结构化细节。

## 样例字段说明

下面解释本组样例采用的字段表达，完整结构、字段出现条件及枚举以[状态报告字段契约](report-format.md)和其 [JSON Schema](schemas/status-report.schema.json) 为准。公共输入类型以[执行计划输入契约](plan-input.md)为准，各动作业务结果由对应责任专题定义；样例不能代替完整的条件分类。

### 报告、计划与动作

| 位置或字段 | 含义与处理 |
| --- | --- |
| 报告 `report_id` | 逻辑报告身份，与文件名中的 ID 相同 |
| 报告 `from_wm`、`to_wm` | 本份报告覆盖的业务区间，不是时间戳；ACK 按已登记报告的上界解释 |
| 报告 `plans` | 需要更新的计划及承载变化后代的计划集合；按 `plan_instance_id` 合并 |
| 报告 `plan_file_diagnostics` | 覆盖区间内的计划文件诊断；按 `diagnostic_id` 合并，字段与生命周期见[计划文件诊断](status-reports.md#计划文件诊断) |
| 计划 `request_id`、`plan_seq`、`created_at`、`name` | 已受理请求关联、首次受理顺序及原计划字段；计划存在即可证明该请求已受理 |
| 计划 `status` | 按所属全部动作的历史计算，不仅根据本份报告出现的动作子集计算 |
| 计划 `actions` | 本份报告携带的动作子集合，按 `action_instance_id` 合并 |
| 动作 `name`、`type`、`device_id`、`scheduled_at`、`group`、`policy` | 首次受理时保存的动作公共字段；没有提供或不适用的可选字段不补造值 |
| 动作 `input_params` | 原始结构化参数，包含导致校验失败的实际输入 |
| 拍摄动作 `effective_params` | 合法受理时确定的参数类型及生效参数；从冻结历史读取 |
| 动作 `execution.started` | 是否曾持久化进入 `running`；受理校验失败为 `false`，运行后失败仍为 `true` |
| 动作 `result` | 已取得的执行结果，作为动作自身字段整体更新 |
| 取回 `result.failures` | 已确定的逐项最终失败，按[取回失败项的报告表达](outputs.md#取回失败项的报告表达)计算；为空表示尚无此类失败，不能单独据此判断动作已成功 |
| 动作 `error` | 该动作当前快照中的错误；合法可选字段缺席时，不保留旧快照同字段的值 |
| 动作 `outputs`、`deliveries` | 按各自 ID 合并的实体子集合；普通参数、结果和尝试数组则随所属对象的自身字段整体更新 |

父对象的 ID 与所在嵌套路径确定所属关系；正式产物和交付仍保留要求的显式来源字段。跨计划取回的 D 放在发起取回的动作下，通过 `output_id`、`source_action_instance_id` 引用来源，不复制到来源计划下。

### 录像结果与正式产物

| 位置或字段 | 含义与处理 |
| --- | --- |
| `result.recording.start`、`stop` | 启动、停止各自已固化的上限及尝试记录，分别计数 |
| `attempts[].attempt_no`、`status` | 本流程内已开始的尝试序号及结果；不是动作被客户端重送的次数 |
| `result.recording.control_elapsed_s` | 已可靠取得的控制过程计时，不是从视频文件测出的媒体时长 |
| `result.recording.effect.status` | 本动作持续录像效果的收场状态；`compensated` 表示该效果已完成收场，不代表相机今后始终空闲 |
| `result.repair.status` | 样例中正常录像不需要修复，取值为 `not_needed` |
| 产物 `kind`、`original_name`、`media_type`、`size` | 原片类型、原始文件名、内容类型及可靠确认的完整字节长度 |
| 产物 `availability`、`cleanup.status` | 文件可用状态与清理状态；样例原片为 `available`，未请求清理为 `not_requested` |
| 产物 `checksum` | `not_obtained` 表示尚未取得摘要；`available` 时携带已取得的 `sha256`，不能用空字符串冒充摘要 |
| 产物 `media.check_status`、`media.duration` | 样例媒体检查为 `not_performed`，实际时长为 `unknown`；不以控制时长填充 |

### 交付与文件读取

| 位置或字段 | 含义与处理 |
| --- | --- |
| 交付 `delivery_id`、`output_id`、`source_action_instance_id` | 本次交付身份、正式产物及产物来源动作；发起取回动作由嵌套位置确定 |
| 交付 `file_name`、`display_name` | 已分配且保持不变的实际文件名，以及完整可读名称 |
| 交付 `size` | 本场景已知源文件的完整大小，不是失败时的部分进度 |
| 交付 `sha256` | 完成读取及校验后可供客户端核验的完整文件摘要；未取得时不补造值 |
| 交付 `status` | `published` 为已完成本地交接；本组 `failed` 为文件处理最终失败、未发布；完整生命周期由产物与文件交接专题细化 |
| `copy.max_read_attempts`、`read_idle_timeout_s`、`max_recopies` | 本文件使用的已固化本地配置值 |
| `copy.read_attempts` | 已开始的文件读取尝试列表；本组已用次数等于列表条目数，各次失败保留自己的错误 |
| `copy.round`、`recopies_used` | 当前整片拷贝轮次及已使用的额外重拷数；初始轮次为 1，未额外重拷为 0 |
| `copy.committed_bytes` | 最后可靠保存的连续进度；半成品后来清理也不改写这个历史事实 |
| `copy.verification.status` | `matched` 表示本次完整副本的主机摘要与源端摘要一致；`not_performed` 表示尚未执行该比较 |
| `copy.work_file_cleanup.status` | 失败半成品已清理为 `completed`；成功发布且没有半成品待清理为 `not_needed`，不表示正式源文件被删除 |

### 错误表达

错误使用 `code`、`stage`、`details` 三部分。`code` 和结构化 `details` 供客户端判断，客户端根据其中的实际字段、对象和原因生成适合用户阅读的文案。输入字段路径始终针对首次提交的计划结构，取回逐项失败通过 `result.failures` 中已确认的来源动作、产物及交付身份关联对象。存在 delivery 时，其最终错误与对应失败项表达同一失败事实；各次读取尝试仍保存自己的原因，不能按尝试次数重复计算失败项。

| 本组错误码 | 阶段 | 结构化信息 | 客户端可表达的事实 |
| --- | --- | --- | --- |
| `source_action_not_found` | `admission` | `field`、`value` | 本计划不存在被引用的动作，只有该取回动作校验失败 |
| `read_idle_timeout` | `source_read` | `timeout_s`、`committed_bytes` | 该次文件读取连续无数据达到阈值，保留可靠进度 |
| `read_attempts_exhausted` | `source_read` | `max_read_attempts`、`attempts_used` | 该文件的读取尝试耗尽，文件处理最终失败 |
| `obtain_items_failed` | `execution` | 空对象；逐项原因见 `result.failures` | 本次取回因逐项最终失败而结束，已发布文件继续有效 |
| `no_outputs` | `output_selection` | 空对象；来源见同一失败项的 `source_action_instance_id` | 来源动作已结束，可靠确认没有正式产物 |
| `camera_start_rejected` | `device_start` | `recording_started: false` | 本次启动明确被拒绝，并可靠确认未开始录像 |
| `start_attempts_exhausted` | `device_start` | `max_attempts`、`attempts_used` | 录像启动尝试耗尽，没有成功启动 |

本组错误属于动作、取回失败项、交付或设备与读取尝试。输入读取失败、整份计划拒绝及 ACK 校验错误通过顶层 `plan_file_diagnostics` 表达，具体结构和归属见[计划文件诊断](status-reports.md#计划文件诊断)。动作自身错误不在该集合重复列出。

## 计划文件诊断样例

以下场景分别说明输入文件与相应诊断。输出 JSON 只截取完整报告的 `plan_file_diagnostics` 字段；其他报告字段和业务对象按既有规则生成。片段不是单独发布或 ACK 的报告，不提供报告文件名摘要。除明确说明的重复输入场景外，各例独立；`diagnostic_id` 为演示身份，错误码及细节按本节表格解释。

### 文件无法解析

主程序传入 `/data/incoming/夜间采集-20260914.json`，文件原始内容如下：

```text
{"request_id":"req-101","actions":[
```

本次不能取得合法的完整 JSON，诊断附带实际文件名，不从残缺正文或文件名提取 `request_id`：

```json
{
  "plan_file_diagnostics": [
    {
      "diagnostic_id": "diag-001",
      "file_name": "夜间采集-20260914.json",
      "errors": [
        {
          "code": "invalid_json",
          "stage": "input_parse",
          "details": {"reason": "unexpected_end"}
        }
      ]
    }
  ]
}
```

客户端可以显示具体文件无法解析，并利用它已知的文件对应关系辅助定位。本条诊断不能证明 `req-101` 被拒绝或没有受理，尚未获确认的请求仍按既有重送规则处理。

### 文件无法打开

主程序传入 `/data/incoming/plan-102.json`，打开时文件已经不存在。诊断中的文件名来自调用参数，取得它不要求文件仍存在：

```json
{
  "plan_file_diagnostics": [
    {
      "diagnostic_id": "diag-002",
      "file_name": "plan-102.json",
      "errors": [
        {
          "code": "plan_file_read_failed",
          "stage": "input_read",
          "details": {"operation": "open", "reason": "not_found"}
        }
      ]
    }
  ]
}
```

完整路径和实际系统错误继续保存在本地诊断中。本次没有读取正文，不填请求 ID，也不把文件不存在解释为某个历史计划不存在。

### 计划受理而 ACK 校验失败

输入文件 `查询状态-103.json` 内容如下；假定数据库中没有报告 9999：

```json
{
  "request_id": "req-103",
  "created_at": "2026-09-14 08:00:00",
  "name": "查询状态",
  "last_report_id": 9999,
  "actions": [
    {"name": "生成报告", "type": "report_status"}
  ]
}
```

计划正常受理并由 `plans` 表达其状态；ACK 校验失败单独形成文件诊断：

```json
{
  "plan_file_diagnostics": [
    {
      "diagnostic_id": "diag-003",
      "file_name": "查询状态-103.json",
      "request_id": "req-103",
      "errors": [
        {
          "code": "unknown_report_id",
          "stage": "ack",
          "details": {"field": "last_report_id", "value": 9999}
        }
      ]
    }
  ]
}
```

客户端可显示计划已受理，同时提示报告 9999 不存在。存在文件诊断不意味着计划拒绝，也不改变该报告动作的正常执行资格。

### 同次输入包含多个错误

输入文件 `查询状态-104.json` 内容如下；仍假定报告 9999 不存在：

```json
{
  "request_id": "req-104",
  "created_at": "明天早上",
  "name": "查询状态",
  "last_report_id": 9999,
  "actions": [
    {"name": "生成报告", "type": "report_status"}
  ]
}
```

计划公共字段非法，整份拒绝；ACK 也非法。两项错误属于同一次输入，放在一条记录中：

```json
{
  "plan_file_diagnostics": [
    {
      "diagnostic_id": "diag-004",
      "file_name": "查询状态-104.json",
      "request_id": "req-104",
      "errors": [
        {
          "code": "invalid_field_value",
          "stage": "admission",
          "details": {
            "field": "created_at",
            "value": "明天早上",
            "expected": "utc_datetime"
          }
        },
        {
          "code": "unknown_report_id",
          "stage": "ack",
          "details": {"field": "last_report_id", "value": 9999}
        }
      ]
    }
  ]
}
```

如果 ACK 合法，则仍吸收 ACK，只保留计划字段错误。若客户端修正尚未成功受理的 `req-104` 后再次提交并被接受，历史 `diag-004` 保留；客户端依据新计划中的请求关联确认受理，不让这条历史拒绝覆盖当前受理状态。

### 重复输入与动作错误的区别

再次通过命令入口提交 `查询状态-103.json`，原计划幂等复用，ACK 再次校验失败时形成新诊断，例如 `diag-005`。同一报告覆盖两次诊断时可同时携带 `diag-003` 与 `diag-005`；它们的文件名、请求 ID 和错误内容可以相同。客户端保留两次输入处理的事实，可以在界面汇总发生次数。

重新接收到含 `diag-003` 的报告只更新或确认该条记录，不增加第三次输入。报告补投和数据库恢复也不重新分配诊断 ID。集合缺席或为空时，客户端保留已经收到的诊断。

本专题“单个动作参数错误”的完整文件样例仍适用：只有取回动作的来源引用错误时，计划受理，错误在该动作下面，不产生文件诊断。如果同次输入另有 ACK 错误，则文件诊断只列 ACK 问题，动作错误仍由动作记录表达。

### 诊断样例中的错误表达

| `code` | `stage` | 本节使用的 `details` 与含义 |
| --- | --- | --- |
| `invalid_json` | `input_parse` | `reason: unexpected_end` 表示 JSON 在完整值结束前终止；其他解析原因须按实际失败表达 |
| `plan_file_read_failed` | `input_read` | `operation: open` 与 `reason: not_found` 表示打开目标文件时不存在；读取中途失败不得写成打开失败 |
| `unknown_report_id` | `ack` | `field: last_report_id` 与实际 `value`；值的格式合法，但找不到对应已登记报告 |
| `invalid_field_value` | `admission` | `field`、实际 `value` 和期望类型；本节 `expected: utc_datetime` 指[公共时间字面量](plan-input.md#时间字面量) |

本表定义所展示错误分支的机器表达，不代替全部输入校验分支的错误码契约。诊断记录使用的阶段、错误归属及缺省语义以报告专题为准。

## 客户端接收与核对

1. 按文件名取得报告 ID 与摘要，对收到的原始字节计算 SHA-256；匹配后再解析 JSON、检查结构、身份与覆盖区间。
2. 对可以应用的报告，按 ID 更新对象自身字段并合并实体子集合；同一份报告的计划及后代使用同一历史边界，客户端不能根据部分动作自行改写计划状态。
   顶层计划文件诊断按 `diagnostic_id` 保存整条记录；文件名用于定位，不作为去重键或计划身份。没有请求 ID 的记录仍能展示，历史拒绝不覆盖已有的受理证据。
3. 可靠保存合并结果及累计覆盖位置后，再在后续输入携带对应 `last_report_id`。重复报告不重复创建对象，迟到旧报告不回退已应用状态，覆盖缺口不能凭较大的报告 ID 跳过。
4. 产物文件与报告独立接收。文件先到时保留其完整文件名及字节，等报告给出映射后核验；报告先到时记录交付事实，实际收齐并核验文件后才显示客户端已收到。

应以本组文件核对以下结果：

| 核对对象 | 必须成立的结果 |
| --- | --- |
| 每份状态报告 | 文件名摘要等于文件实际字节的 SHA-256，文件名 ID 等于正文 ID |
| 样例一的 ACK 输入 | 除 ACK 外保留原请求及正文，指向本组已登记报告；不创建新计划或 delivery |
| 样例二的增量合并 | 第二份报告仅含合法录像动作，合并后仍保留失败取回动作及其错误 |
| 样例二的覆盖区间 | 第二份报告的下界只在第一份报告 ACK 已被吸收的前提下成立 |
| 样例三的逐项结果 | 两个录像动作成功，交付一项发布、一项失败，取回失败但计划完成；失败项完整关联来源、产物和交付 |
| 样例四的无产物失败 | 失败项仅关联来源动作，没有虚构产物或 delivery；另一来源的交付正常发布 |
| 失败文件的计数与清理 | 三次读取失败、额外重拷为 0；半成品清理完成不删除原片或释放交付文件名 |
| 所有样例的媒体信息 | 已登记正常原片未执行媒体检查，不伪造实际视频时长或整段解码保证；没有原片时不构造媒体信息 |

本文提供协议样例与预期结果，未调用生产实现或真实设备。生产代码、客户端实现和设备适配仍须分别验证对应契约。
