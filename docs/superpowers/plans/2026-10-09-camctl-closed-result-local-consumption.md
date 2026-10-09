# 照片与延时摄影的 CLOSED 结果本地收尾实施计划

> 执行 Agent 按任务逐项实施；使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 组织执行。根 Agent 独占、顺序运行 pytest，并独立核验红色反例、生产改动和门禁。既定持续实施授权适用于本计划，不新增审批步骤。

**目标：** photo/timelapse 已可靠保存有限核实的 UNCONFIRMED 结论和完整文件时，下一次正常运行即使缺少原设备配置或绑定了另一驱动，也能完成不依赖设备的文件登记和业务终态。

**责任边界：** 原结果保存责任与后续设备资格分别判断。原申请先按既有恢复规则可靠完成；没有旧会话申请时，从持久原 RESULTS 及文件事实恢复。本地收尾使用原绑定解释原事实，仍需设备操作的工作继续检查当前绑定。

**技术环境：** Python 3.11、现有 SQLite 事务、事件守卫和拍摄仓储；复用现有 v1 结果及文件登记模型，不增加依赖、公共字段或持久格式。

**正式依据：** [设备绑定异常的影响范围](../../architecture/configuration.md#设备绑定异常的影响范围)、[取消、失败与文件保留](../../architecture/camera-capture.md#取消失败与文件保留)、[设备任务的取消资格](../../architecture/task-cancellation.md#设备任务的取消资格)、[拍摄动作的驱动绑定](../../camctl/database/plans-actions.md#拍摄动作的驱动绑定)、[产物结果核实的责任与轮次](../../camctl/database/operation-fields.md#产物结果核实的责任与轮次)、[设备活动字段](../../camctl/database/operation-fields.md#设备活动字段)、[正式产物与来源](../../camctl/database/file-fields.md#正式产物与来源)、[已提交结果与当前状态的边界](../../camctl/persistence-runtime.md#已提交结果与当前状态的边界)。原完整申请恢复继续遵守[有限耗尽申请的保存与恢复](2026-10-09-camctl-result-errors-and-report-projection.md#有限耗尽申请的保存与恢复)。

## 起点与范围

本阶段起点为 `6db4b4a`，即录像 `ResultRunClose` 完整申请保存责任完成后的提交。2026-10-09 在实际仓库核对 HEAD 为 `6db4b4a71e7029af1dfff39c774ea014ebe7ace3`；该阶段的验证范围见[录像原申请的实施与验证](2026-10-09-camctl-result-errors-and-report-projection.md#录像原申请的实施与验证2026-10-09)。执行前由根 Agent 再记录 HEAD 和工作区状态并读取相关生产函数；接口名称随实际源码核对，既有行为契约保持。

首阶段严格限定：原 RESULTS 流程为 UNCONFIRMED，原集合结论为 UNCONFIRMED，完整实际调用结果及待登记文件已经可靠保存，动作尚未终态。旧连接已经关闭，下一次运行使用 fresh Owned（重新打开的自有连接）和全新的运行时；旧 holder（会话持有的完整申请）、结果缓存和计时缓存均不存在。每次运行的配置固定，绑定变化发生在两次运行之间。

首阶段处理普通 photo、普通 timelapse，以及停止处理已经可靠完成、无需再访问设备的合法取消 timelapse。仍需 STOP 的取消 timelapse 是资格隔离控制，不在首阶段将其改成本地收尾。普通 timelapse 首次 EXHAUSTED 的同类文件保留作为任务三独立红—绿阶段，依赖首阶段的共同文件处理；不将它混入首阶段的重启前置。其他集合结论、录像媒体处理、取回及清理不作为正向前置。涉及同一文件保留不变量的其他入口须列出审计结果，但不得用局部绿色替代其验收。

## 输入、输出与不变量

输入来自公开受理、调度、操作结果、文件观察与结论事务，不能通过直接 INSERT 或 UPDATE 拼造投影。执行者须能从原完整历史追溯下列事实。

| 输入 | 必须可靠核对的内容 |
| --- | --- |
| 原动作及活动 | 原 action/activity ID、动作类型、设备与驱动、执行定义、取消标记和非终态状态；至少使用一个 action ID 不等于 activity ID 的前置。 |
| 原 RESULTS | `results/<activity_id>` 确属原动作和活动；流程为 UNCONFIRMED，等待已清除，尝试均有可靠实际结果，没有本次仍在执行的调用。 |
| 原结论 | `result_set_state`、`result_check_json`、`capture_json`、`completion_basis` 和错误符合既有 UNCONFIRMED 契约；原完整事务 G、operation key 和决定时刻可核对。 |
| 原逐文件事实 | 原稳定身份、定位、任务归属、存在与写完依据、大小、用途和适用配对；文件已经可靠登记，不能只把 JSON 条目当作归属证明。 |
| timelapse 等待 | 普通分区已有原等待完成事件及其固定依据，不用新配置重新等待；取消分区按原取消规则判断停止责任，不用普通等待代替 STOP。 |
| 当前运行 | 有效状态库、原目录绑定、可信执行资格；设备配置为 matched、missing 或 mismatch。mismatch 使用合法且已登记的新驱动，避免把装配配置错误误当业务绑定异常。 |

首阶段的输出分类如下。完整文件集合与采集集合能否确认是独立事实；一个文件完整不能把原集合结论改为 COMPLETE。

| 业务分区 | 动作结果 | 文件及采集事实 |
| --- | --- | --- |
| 普通 photo/timelapse，可靠 UNCONFIRMED，具备全部本地收尾输入 | 按原 `capture_result_unconfirmed` 结论保存 failed，详情使用实际 activity ID 和 `reason=outputs_unknown`。 | 所有符合登记契约的原完整文件登记为正式产物；原采集与集合结论、错误和实际调用结果保持，不新增绑定失败替代原原因。 |
| 合法取消 timelapse，停止处理已可靠完成，可靠 UNCONFIRMED | 按已生效取消保存 canceled。 | 保留合格完整文件；原核实流程、原采集结论和调用错误不因动作 canceled 而重判。取消发起者仍按其实际有限处理结果汇总，不能据此声称取消请求 succeeded。 |
| 没有合格完整文件，或合法文件明确尚未完成 | 动作仍按所属失败／取消规则收尾。 | 不登记该文件为正式产物；合法空观察与读取失败必须分别表达。 |
| 原实际输入、身份、归属或结论无法可靠读取，或存在矛盾 | 停止依赖收尾，保留原非终态及可诊断错误。 | 不生成空集合，不调用设备补事实，不提交部分产物。默认入口按既有状态库错误规则停止。 |
| 动作已经终态 | 保持原结果。 | 不追加或重新登记产物；再次推进不增加业务历史或设备调用。 |

必须保持下列不变量：

1. 原 G、key、决定时刻及其完整事件不变，原历史前缀逐行保持。没有旧 holder 时不声称恢复原申请，也不为原 G 再造 key。
2. 原 RESULTS 流程的状态、错误、次数及已保存配置不变。原 START、STOP 和 RESULTS 的实际尝试结果不重分类；既有终态控制流程不重开。
3. 首阶段不新增 START、RESULTS、STOP、QUERY、READ 或 DELETE 调用、意图和名额，不让当前驱动解释旧定位。原活动 UNKNOWN／ACTIVE 不因业务收尾伪装为 ENDED。
4. 已有文件身份、归属、完成依据、大小和元信息保持。前轮可靠完整文件不能因末轮 FAILED 或无观察而消失；原错误与文件观察可以并存。
5. 正式产物与动作终态在同一事务提交；失败或未知保存停止依赖步骤，不对外留下部分产物。历史与当前投影共同满足既有守卫。
6. matched、missing、mismatch 对无设备依赖的本地收尾产生相同业务结果。非法配置、驱动未登记和状态库失败仍分别按自己的错误规则处理。

## 资格决策模型

先按原申请恢复规则核实已有 holder，再读取可靠业务事实。以下表按从上到下的顺序适用；只有通过前三项才能判断是否可以本地收尾。文件资格另外应用前述输出表，不能用它替代 STOP 资格。

| 条件 | 必须执行的行为 | 阶段归属 |
| --- | --- | --- |
| 原保存责任尚未可靠完成 | 核原 key 并保留完整申请；不进入本地收尾或当前设备资格。 | 既有完整申请恢复；本计划保持其控制。 |
| 依赖事实读取失败、缺失或矛盾 | 停止并诊断，不把它解释为 CLOSED、空文件或没有停止责任。 | 首阶段失败控制。 |
| 动作已终态 | 返回既有结果，不装配收尾申请。 | 首阶段终态控制。 |
| 普通 photo/timelapse，原 UNCONFIRMED/CLOSED 及本地输入齐全 | 消费原输入并按原失败结论登记产物和终态；不检查与本地步骤无关的设备可用性。 | 首阶段正向分区。 |
| 取消 timelapse，原 STOP 已可靠确认，或原活动已有可靠结束依据 | 按取消规则消费原 CLOSED 输入并收尾，不重新停止。 | 首阶段正向分区；主要反例采用实际成功 STOP。 |
| 取消 timelapse，原有限 STOP 已结束但仍未确认停止 | 不重开 STOP；原未知／失败及占用保持，按原取消规则处理本地文件。 | 同类审计分区；必须用已有合法登记错误准备，不能使用应急错误或猜 reason。 |
| 取消 timelapse，原 STOP 尝试仍由实际拥有者执行 | 等待实际结果，不发出第二个停止，不保存依赖终态。 | 资格隔离控制；不能当首阶段本地正向前置。 |
| 取消 timelapse，仍需发起或继续有限 STOP，绑定 matched | 沿原 STOP 身份、剩余次数与间隔继续，再判断本地收尾。 | 保持既有设备业务，不全局跳过绑定。 |
| 取消 timelapse，仍需 STOP，绑定 missing／mismatch | 不通过异常或新驱动停止；按正式绑定失败及取消责任保存实际结果。 | 单独实施和验收；首阶段不得提前输出本地收尾成功。 |
| 其他仍需设备操作，或不属于已有 UNCONFIRMED/CLOSED 的情况 | 保留所属业务的当前绑定及资格检查。 | 首阶段不改其语义；审计后逐项报告。 |

photo 的活动在当前第一版固定为不支持停止。已启动或可能启动后，取消请求应被拒绝，不能绕过取消资格直接调用 WITH_STOP 构造 photo 的取消正向分区。确认未启动的取消也不能与“已有实际 RESULTS 且耗尽”混作同一前置。

## 数据流与建议修改边界

权威数据流为：原受理与动作开始 → 实际 START → typed v1 RESULTS 与实际错误 → 文件发现、归属、写完和配对 → 原 UNCONFIRMED 事务 G → 关闭旧连接 → fresh Owned 与空会话集合 → 读取原 CLOSED 输入 → 检查终态及实际取消／STOP 资格 → 产物草稿 → 原复合登记事务 → 可观察动作及产物。

当前 `_photo_handler` 和 `_timelapse_handler` 在原申请恢复及终态检查之后调用 `_handle_binding_failure`。该边界只为录像 `_local_recording_listing` 提供本地输入放行；photo/timelapse 的 CLOSED 输入仍会进入空草稿的绑定失败收场。建议在共同的拍摄资格边界识别可靠且无需设备的 UNCONFIRMED 本地收尾，再复用既有文件与终态处理。是否增加内部函数、放置位置和函数名由实际数据流决定，不把预估接口作为正式协议。

| 源码或测试文件 | 本计划关注的责任 |
| --- | --- |
| `apps/camctl/src/camctl/capture/handlers.py` | `_photo_handler`、`_timelapse_handler`、`_handle_binding_failure`；复用 `_saved_result_listing`、`_result_file_metadata`、`_registered_result_files`、`_register_listing`、`_catalog_drafts` 和 `_finish_capture`。 |
| 同一 handlers 文件 | `_finish_timelapse_conclusion`、`_advance_canceled_capture`、`_close_canceled_timelapse` 的取消优先级、STOP 责任及 CLOSED 收尾；任务三处理普通首次 EXHAUSTED 的完整文件收尾。 |
| `apps/camctl/src/camctl/bootstrap/capture_assembly.py` | 缺失配置的 binding-only runtime，以及合法 mismatch 工厂；本地读取不需要 results/control 端口，非法装配错误继续保留。 |
| `apps/camctl/src/camctl/bootstrap/flows.py`、`bootstrap/lifecycle.py`、`capture/dispatch.py` | 默认 scheduling 前缀、当前资格及 handler 分发；不增加受限、残留或取消 flow 的新拍摄资格。 |
| `apps/camctl/src/camctl/persistence/repositories/capture.py`、`outputs/catalog.py` | 原 `FinishCapture`／`FinishCanceledCapture` 事务、文件关系核对、重复登记及终态不补写。首阶段预计无需改变 `_FinishBindingFailureCommand` 的输入。 |
| 建议新建 `apps/camctl/tests/integration/capture/test_closed_result_local_consumption.py` | 公开处理器与真实 SQLite 组合的本地收尾矩阵。 |
| 建议新建 `apps/camctl/tests/integration/capture/test_timelapse_exhaustion_files.py` | 普通 timelapse 首次 EXHAUSTED 与后续 CLOSED 的文件保留一致性。 |
| 建议新建 `apps/camctl/tests/integration/bootstrap/test_closed_result_binding_recovery.py` | 真实 `session_capture_assembly`、`capture_flow` 和 fresh Owned 的默认入口组合。 |

复用 `_saved_result_listing` 对原责任、全部已保存观察与登记文件的关系校验。不能仅从末轮 `result_json` 取条目；末轮没有观察但有原错误时，要保留前轮可靠文件。若原关系无法解释，停止而不是读取当前设备修补。

## 任务一：先建立首阶段的真实红色反例

**消费接口：** `capture_handler(action_type)` 返回的处理器、`CaptureRepository.close_result_check_unconfirmed(ResultSetSave, OperationKey, OwnedConnection)`、真实受理及调度仓储。可以复用 `result_consumer_fixtures.consumer_world`、`test_cancel_timelapse_exhaustion_files` 的合法取消准备，以及受真实接口约束的结果替身。

**交付：** 前述建议 capture 测试文件及可复用的 CLOSED 世界；生产保持不变直到根确认有效红色反例。

- [x] 准备三个业务类别：普通 photo、普通 timelapse、实际成功 STOP 已保存且取消生效的 timelapse。使用两个结果历史：末轮仍有完整文件，以及前轮完整文件、末轮 FAILED 且 observations 为空。完整文件须具有明确原名称、media_type、大小及可靠归属。
- [x] photo 沿现有消费者使用完整 OTHER 附属原文件，尚未取得必要 PHOTO 产物，避免在建立 UNCONFIRMED 前已经成功终态；该文件仍须满足正式原文件登记契约。timelapse 使用完整 VIDEO，或取消分区的完整 PHOTO，v1 条目不作为集合结束证明。若实际公开前置不接受所选文件，停止该 fixture 并报告，不改产品资格或绕过守卫。
- [x] 将有限额度设为一轮或两轮，先运行真实处理器保存实际结果及文件。仅在真实仓储已经提交原 G 并返回 COMPLETED 后截停，防止依赖终态执行；spy 不得伪造事务响应。断言原 G、UNCONFIRMED 流程、原集合及完整错误已可靠存在，动作仍 running、outputs 为空。
- [x] 取消类别通过真实取消受理、固定和生效事务建立，核原 `stop_supported=True`；通过真实 STOP 结果及活动结束事务完成其停止前置。可以在原 G 已保存之后准备合法取消，但所有准备结束后必须再关闭连接。不得靠保留旧 runtime 实现下一运行。
- [x] 重开同一状态库；重新装配全新的运行时，所有旧保存集合和内存缓存为空。冻结原历史前缀、实际尝试、RESULTS 流程、活动采集字段、文件和设备调用次数。至少使用一次独立的 action/activity ID。
- [x] 添加 `test_closed_unconfirmed_keeps_files_without_device_access`：三个业务类别 × matched/missing/mismatch × 两个结果历史，共十八项。missing 不提供设备声明；mismatch 使用合法已登记的新驱动。处理器完成后断言前述输出表、完整文件的 `output_id`／源文件 ID／元信息、产物与终态的 transaction ID 相等，以及全部不变量。expected 文件和元信息来自 fixture 固定事实，不调用生产 `_catalog_drafts` 计算预期。
- [x] 根单独运行新 capture 文件并记录日志。有效红色反例必须先通过原 G、fresh Owned、空集合、合法取消／STOP 和完整文件前置，然后失败于 missing/mismatch 的原错误或产物保留断言；matched 控制应通过。若失败来自守卫缺失、非法输入或终态过早，先修 fixture，不动生产。

建议根执行：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture/test_closed_result_local_consumption.py -q
```

## 任务二：实现本地资格并覆盖边界

**消费接口：** 任务一的十八项状态矩阵、现有 CLOSED 读取及产物登记接口。

**交付：** 无设备依赖时的统一本地收尾选择；仍需设备操作的资格与错误分类保持。

- [x] 根确认有效红后，在最窄共同边界实现资格判定。顺序必须表达原申请恢复、可靠读取、既有终态、实际取消及 STOP 责任、本地收尾与必要设备绑定。只检查 changed 字段或只以 RESULTS.status 判断本地资格都不充分。
- [x] 普通分区复用原 UNCONFIRMED 失败及完整文件输入；取消分区复用停止已结束后的 CLOSED 文件处理。不用当前绑定错误替代原采集错误，不重新保存采集／集合结论，不将 canceled 当作采集成功。
- [x] 添加 `test_closed_terminal_reentry_does_not_append_outputs`，覆盖三个业务类别及三种绑定；第一次收尾后再次推进，断言数据库 dump、设备调用及正式产物身份完全相同。
- [x] 添加 `test_closed_input_read_failure_stops_local_finish`：在 fresh Owned 的原 RESULTS 读取边界注入实际读取错误，分别覆盖普通与取消类别；原 G 保持、动作不变、outputs 为空，没有设备调用。合法空观察与不完整文件另设控制，不能通过吞掉异常取得空条目。
- [x] 添加 `test_closed_local_finish_rolls_back_outputs_and_terminal_together`：在真实终态复合事务写入期间注入可确认回滚的数据库错误，断言原 G 和文件保持、outputs 与终态均未提交；可靠恢复后一次登记，终态重入不补写。既有事务执行层承担回滚和 UNKNOWN 分类，不新增独立事务协议。
- [x] 添加 `test_canceled_closed_with_required_stop_keeps_device_qualification`：合法 timelapse 取消已生效、原 G 已保存，但没有可靠 STOP 结束依据。matched 只允许原有限 STOP；missing/mismatch 不调用任何驱动，也不能进入首阶段本地分支。若有实际拥有者仍执行 STOP，只保持等待。该控制核资格和已有可靠文件保持，不以它证明绑定失败终态的完整文件登记已经闭合。
- [x] 根顺序重跑新 capture 文件，十八项正向控制及所有边界控制通过后才进入默认入口验证。新异常若出现在任务未定义的状态，停止该分区并记录具体前置，不放宽测试。

## 任务三：普通 timelapse 首次 EXHAUSTED 保留原完整文件

**消费接口：** 真实 `_timelapse_handler` 的首次有限耗尽、原 `close_result_check_unconfirmed`，以及任务二复用的本地文件登记边界。

**交付：** 原 G 可靠完成后，首次 EXHAUSTED 与下一运行的 CLOSED 收尾保留相同合格文件；没有新集合成功语义。

- [x] 添加 `test_first_timelapse_exhaustion_keeps_saved_complete_files`。通过公开受理、调度、实际 START 和普通等待完成建立 timelapse；当前绑定 matched。实际 v1 返回完整 VIDEO 或 PHOTO，采用单轮完整文件、前轮完整文件末轮 FAILED 无观察两个历史，共四项。两种文件类型都用合法归属和完成依据，不推定集合已经确定。
- [x] 耗尽前断言动作 running、RESULTS 次数已经用完、实际结果及完整文件已保存、outputs 为空。首次 EXHAUSTED 正常调用真实结论仓储，不在 G 后截停；断言原 G 可靠存在、动作按原 UNCONFIRMED 原因 failed、全部合格文件与终态同事务登记，原实际次数及结果不变。当前实现的有效红应在这些前置及 G 已可靠保存之后，失败于文件产物缺失。
- [x] 添加合法空观察和不完整文件控制，断言不虚构正式产物；再次推进终态及重开后的再次推进均保持原数据库和调用次数。原参数要求 VIDEO 与实际取得 PHOTO 是两个事实，失败收尾保留文件不等于满足任务要求。
- [x] 根独占运行新文件，确认有效红后再实现。建议首次 close 得到可靠完整响应后从同一已保存 RESULTS 和文件事实建立收尾输入，复用首阶段处理，而不是把空 entries 作为业务输入。原 G 尚未可靠完成时不得登记终态；既有 holder、原 key/T1 和失败分类保持。
- [x] 根顺序复验新文件及任务一文件。四项正向和空／不完整控制通过，首次 EXHAUSTED 与 CLOSED 两个入口均保留原文件、原失败原因和原实际结果。普通 `FinishCapture` 自身 UNKNOWN 的完整申请责任仍见起点计划的独立缺口，不在此任务新增该协议。

建议根执行：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture/test_timelapse_exhaustion_files.py -q
```

## 任务四：核默认装配、审计同类入口并验收

**消费接口：** `session_capture_assembly(...)` 生成的 factory、`capture_flow(factory, ...)`、真实 `open_existing(...)` 及任务一的持久 CLOSED 前置。

**交付：** 首阶段从默认 scheduling 到原文件产物的组件证据，以及其他责任分区的明确交接。

- [x] 在建议 bootstrap 文件中添加 `test_default_capture_consumes_closed_unconfirmed_after_binding_change`，覆盖三个首阶段业务类别 × matched/missing/mismatch。每项从关闭旧连接、丢失所有 holder 开始，由实际工厂和 flow 打开 fresh Owned；断言动作和产物、原 G 与历史前缀、原调用次数。binding-only runtime 可以没有 control/results 端口；新驱动端口均不得被调用。
- [x] 根单独运行新 bootstrap 文件。所有前置及行为断言通过；不能只在候选之前截停，也不能替换实际 factory 返回手工 runtime 来证明默认接线。
- [x] 审计表中全部生产入口：分别记录已覆盖、原有未修复、未决或不适用。重点核任务三的普通首次 EXHAUSTED、仍需 STOP 且绑定失败、已结束但未确认停止、完整原文件及配对、最后一轮失败、终态和原持有申请。仍需设备的取消分区独立交接，不扩展成全部拍摄流程审查，也不能漏列后宣称文件保留整体完成。
- [x] 根按独占、前台、顺序规则运行新文件、现有耗尽保存恢复、取消延时文件登记、绑定取消门禁，再运行全量 unit。capture 与 bootstrap 分开执行；其他目录仅在改动确实涉及其责任时追加，不把全部目录合并为一次 pytest。
- [x] 独立评审源码及真实日志，核原输入到产物事务的完整路径。完成记录写明日期、环境、实际 commit、命令、计数和失败归因；按已授权整体 checkpoint 方式交接，不在本计划预定提交拆分。

建议局部门禁按以下命令逐条执行：

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_closed_result_binding_recovery.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/capture/test_result_exhaustion_save_recovery.py apps/camctl/tests/integration/capture/test_cancel_timelapse_exhaustion_files.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/bootstrap/test_result_exhaustion_default_recovery.py apps/camctl/tests/integration/bootstrap/test_binding_canceled_results.py apps/camctl/tests/integration/bootstrap/test_binding_canceled_result_transactions.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit -q
```

录像原申请的新门禁按其完成计划的实际文件单独复验，不猜测试文件名。若较宽目录还有已知非法 fixture 或未决语义失败，必须逐项归因；不能删除、跳过或改变状态预期来宣布完整 app 通过。

## 禁止捷径与独立分区

禁止全局删除绑定检查、给 missing 配置补默认设备、让新驱动解释旧参数或定位、重列举补事实、只保留末轮条目、把读取失败变为空集合、把 UNCONFIRMED 改成 COMPLETE／SUCCEEDED、改变原错误 reason、用已有取消标记推断 STOP 成功、在终态以后补产物、为私有转发函数机械增加独立单元测试，以及改变持久格式来保存本阶段不需要的新字段。

下列事项与首阶段确定工作隔离：

| 分区 | 状态及交接要求 |
| --- | --- |
| 仍需 STOP，当前绑定 missing/mismatch，已经有完整原文件 | 不通过其他驱动停止、原有限责任按所属规则失败、目标按取消规则结束且合格文件须保留，这些规则已确定。现有空草稿的绑定失败事务能否完整表达所需产物须独立核验；如需扩展内部申请和复合事务，另设红色反例及阶段，不以首阶段放行替代。 |
| 普通首次 EXHAUSTED 的文件收尾 | 不属于首阶段“已经 CLOSED 后下一运行”的前置；任务三在原结论可靠完成后按同一文件保留规则独立实施和验收。 |
| 原申请持有、G 缺失，但另一个合法事务已结束 RESULTS 或确定不兼容采集判定 | 相互竞争结果的处置语义未定义。先核原 key，保留申请并停止该分区；不能推定旧决定退役、重开流程或选择谁胜出。 |
| 普通 UNSATISFIED 错误 reason、应急错误身份 | 等待用户裁决；不得作为测试前置或通过选择新错误结构完成本计划。 |
| RESULTS v2、原申请输入身份及其他集合结束分区 | 按各自计划和格式决策推进；本计划不添加字段、不解释 v1 为完整集合。 |

首阶段完成的含义是：可靠 UNCONFIRMED/CLOSED、无旧 holder 且无需设备操作的 photo/timelapse，在 matched/missing/mismatch 下均能从原事实完成合格文件与业务终态的原子登记，失败控制及终态重入成立。任务三另证明普通 timelapse 首次耗尽也保留这些文件。二者不等于所有绑定失败、取消、集合结束或完整 app 的验收完成。

## 阶段实施与验证（2026-10-09）

本阶段完成任务一至四限定的 CLOSED 本地收尾及普通首次耗尽文件保留。源码起点为 `6db4b4a`；生产、测试和本记录在同一阶段提交保存。环境为 Linux 开发容器、Python 3.11.16，使用已有 `apps/camctl/.venv`，根 Agent 前台独占、按集成目录顺序执行。软件组件通过受真实接口约束的设备替身协作，不包含真实设备或物理断电验收。

`_closed_capture_is_local` 在共同绑定边界核原 RESULTS 身份与 UNCONFIRMED 状态、原 START、实际调用结果以及普通等待或取消 STOP 资格。原实际调用仍在执行时保持其拥有者责任；依赖事实缺失或读取失败时停止。文件输入由 `_saved_result_listing` 统一读取和验证，原设备绑定解释原事实。普通 timelapse 首次耗尽在结论事务可靠完成后复用 `_finish_timelapse_conclusion`。

有效红色基线为：CLOSED 消费及终态重入的二十四项绑定分区失败，普通首次耗尽的四项完整文件分区失败；同一 capture 进程共 28 failed、16 passed。真实默认工厂与 flow 为 6 failed、3 passed。读取及投影故障控制为 12 failed、3 passed，故障前原事实可靠，但原消费者没有到达本地文件输入与复合产物事务。真实 STOP 拥有者在途是资格保持控制，不将它描述为新增功能的红色反例。

所有下列命令均在仓库根目录使用前缀 `PYTHONPATH=/workspaces/camctl/apps/camctl/src apps/camctl/.venv/bin/python -m pytest`，接表中的路径及 `-q`，完整输出直接写入对应日志。不同集成目录没有合并进一个 pytest 进程。

| 验证路径 | 终态结果 | 日志 |
| --- | --- | --- |
| `apps/camctl/tests/integration/capture/test_closed_result_local_consumption.py`、`test_closed_result_local_boundaries.py`、`test_timelapse_exhaustion_files.py` | 60 passed，31.52s | `/tmp/camctl-goal-closed-result-capture-final.log` |
| `apps/camctl/tests/integration/bootstrap/test_closed_result_binding_recovery.py` | 9 passed，4.97s | `/tmp/camctl-goal-closed-result-bootstrap-green.log` |
| capture 的 `test_result_exhaustion_save_recovery.py`、`test_cancel_timelapse_exhaustion_files.py`、`test_record_result_retry.py` | 35 passed，18.14s | `/tmp/camctl-goal-closed-result-capture-gate.log` |
| bootstrap 的 `test_result_exhaustion_default_recovery.py`、`test_record_result_run_close_recovery.py`、`test_binding_canceled_results.py`、`test_binding_canceled_result_transactions.py` | 72 passed，28.72s | `/tmp/camctl-goal-closed-result-bootstrap-gate.log` |
| `apps/camctl/tests/unit/capture/test_start_gate.py` | 23 passed，0.35s | `/tmp/camctl-goal-closed-result-start-gate.log` |
| `apps/camctl/tests/unit` | 3897 passed、1 skipped、2 warnings，8.77s | `/tmp/camctl-goal-closed-result-all-unit-final.log` |

两个 warning 来自既有同步测试的 asyncio 标记。首次启动及未启动取消的单元替身现在由 `sqlite3.Connection`／`sqlite3.Cursor` 接口约束，明确返回合法空活动；原设备调用、授予和本地取消断言保持。实际投影故障代理只计故障事务的 ROLLBACK，仍核原异常、真实 ROLLED_BACK、已插入的一行产物被回滚、完整数据库内容不变以及解除故障后的单次登记。

独立只读审查核生产资格和首次耗尽改动、真实输入读取、故障事务、实际 STOP 拥有者及上述组件日志，未发现该阶段新增生产阻断。最终单元进程由根 Agent 取得 exit 0。各同类入口的状态如下。

| 入口或分区 | 本阶段结论与剩余责任 |
| --- | --- |
| 普通 photo/timelapse 的可靠 UNCONFIRMED/CLOSED | 三种绑定、末轮完整和前轮完整末轮失败两种历史，以及终态重入已组合验证；原完整文件和原失败原因保持。 |
| 取消 timelapse，实际 STOP 已确认并可靠保存 | 三种绑定下从 fresh Owned 完成本地收尾；产物与 canceled 同事务，原核实错误和文件保持。 |
| 取消仍需 STOP | matched 按原责任实际停止，missing/mismatch 不访问设备；额外真实 owner 控制证明在途时不派发第二 STOP、不提前终态或登记。 |
| 普通 timelapse 首次 EXHAUSTED | VIDEO、PHOTO、前轮完整末轮失败、空观察及不完整文件均已验证；与后续 CLOSED 保留相同合格文件。 |
| 原文件、预览配对及实际输入 | 继续由既有 `_saved_result_listing`、`_registered_result_files`、`validate_observed_pairings` 及目录事务核对；本阶段没有新增预览配对组合证据。 |
| 有限 STOP 已 FAILED／UNCONFIRMED，活动尚未确认结束 | 资格按明确原流程状态判定，原未知和占用保持；源码已独立核对，本阶段未新增该分区的正向重启矩阵。 |
| 活动 ENDED，但原 STOP 仍 PENDING／ACTIVE | 可以消费已有本地文件，原开放 STOP 不被补造为成功；取消发起者的汇总责任仍待闭合。 |
| 仍需 STOP、绑定 missing/mismatch，已有完整文件 | 必要停止按绑定错误结束的资格已验证；`FinishBindingFailure` 的完整产物登记仍须独立实施和验收。 |
| 普通 `FinishCapture` 保存 UNKNOWN | 完整申请、原 key 和决定时刻的持有仍是独立缺口。 |
| UNSATISFIED、应急错误身份、RESULTS v2 和结果申请身份登记 | 按各自计划与未决语义推进；本阶段不改变格式或选择未定 reason。 |

本阶段没有重跑或宣称全量 capture 目录通过。既有未决分区的失败记录仍有效；此处的绿色只证明表中实际运行范围。
