# camctl 电机控制实施计划

> 供执行 Agent 使用：使用 `superpowers:executing-plans` 按任务实施，复选框只记录实际实现和验证。行为契约与项目规则是硬性要求；下文的新增文件、内部接口、数据库结构及提交拆分是实施建议，调整时须同步消费者和对应验证。

**目标：** 让 camctl 受理 `motor_control`，在有效时间窗口内向主程序发送一次位置通知，可靠保存发送事实，并完成取消、恢复、历史与报告闭环。

**组织建议：** 新增独立电机动作流程，复用公共受理、会话、事务、时间与报告能力。发送意图先于管道写入保存；恢复只解释已有事实，不重新取得发送许可。公共机器定义在根 `protocol` 维护，本计划跟踪交付，host 通过配套计划消费。

**技术基础：** Python 3.11、现有精确 JSON 与 jsonschema 适配、SQLite 历史事务、POSIX 管道及 pytest；优先复用标准库和已有依赖。

**规格：** [电机动作](../../architecture/motor-control.md)、[通知协议](../../../protocol/host-notifications.md)、[计划输入](../../architecture/plan-input.md)、[调度](../../architecture/scheduling-execution.md)、[取消](../../architecture/task-cancellation.md)、[数据库事务](../../camctl/database/transactions.md)。

[实施路线图](2026-09-30-camctl-implementation-roadmap.md#电机控制与主程序通知接入) · [host 配套计划](2026-10-08-camctl-host-notifications.md)

## 范围与实施起点

截至 2026-10-08，公共机器定义、受理、执行、取消、恢复、报告与发行资源均已接入 `motor_control`。下表定位实际生产入口；下方任务复选框与验证记录共同说明完成范围。

| 已核对的代码位置 | 现有责任与本次接入点 |
| --- | --- |
| `apps/camctl/src/camctl/cli.py`、`bootstrap/lifecycle.py` | `Command` 与 `parse_command` 解析命令，生命周期装配正常与受限会话；接入仅供 run 使用的通知描述符。 |
| `acceptance/input.py`、`acceptance/schema.py`、`acceptance/definitions.py` | 精确输入与分层受理；`build_action_spec`、`read_action_spec` 识别电机动作，保存原参数和空执行定义。 |
| `bootstrap/flows.py`、`scheduling/rules.py`、`persistence/repositories/scheduling.py` | 相机开始事务与拍摄派发保持独立；`bootstrap/motor_assembly.py` 装配电机流程，复用时间规则，不建立相机活动或通信重试。 |
| `bootstrap/application.py`、`session/work.py` | `query_work_facts` 和 `classify_work` 决定驻留、接管与退出；电机使用公共动作责任，未来唤醒已纳入 type 8；会话集成测试覆盖驻留和退出。 |
| `cancellation/rules.py`、`cancellation/service.py`、`persistence/repositories/cancellation.py` | 取消资格与事务生效；已接入电机发送阶段和四种公共寻址入口；可靠未发送时共同保存取消终态与专属事实。 |
| `persistence/transaction.py`、`history/`、`contracts/public_projection.py`、`reporting/` | 历史、投影、报告与结果重用；接入专属发送事实和新的公共动作类型。 |

表中省略的 Python 路径均相对 `apps/camctl/src/camctl/`。预计新增 `motor/models.py`、`motor/notification.py`、`motor/service.py`、`bootstrap/motor_assembly.py` 和 `persistence/repositories/motor.py`，分别负责纯分类、管道适配、动作流程、装配与原子保存；按实际职责合并或拆分时不得复制规则。

客户端表单及展示由[路线图的客户端接入项](2026-09-30-camctl-implementation-roadmap.md#电机控制与主程序通知接入)跟踪；本计划负责为其提供机器定义和真实报告。客户端已接入，整条用户业务链的证据由 M6 与 HN5 共用。

## 全局约束

- `params` 仅有 `position`，表示范围为 -2147483648～2147483647；数学整数按公共精确数值规则受理，通知输出规范十进制整数字面量。位置单位与业务范围由主程序定义。
- 时间窗口两端包含；只有正常 run 可以发送。submit、历史回放、报告重建和原请求重送均不新增发送。
- 动作成功只证明完整消息写入；不等待 ACK，不记录回调执行或电机到位事实。
- 意图与结果由专属记录保存，历史和投影共同提交；结果与动作终态、父计划完成及报告变化同事务保存。数据库记录永久保留。
- 状态库缺失或无效时不重建为空库；读取或提交未知时不以默认值补造发送资格。恢复中的未知意图不重发。
- 测试使用部署版 Python 3.11；单元测试无真实 IO，SQLite、管道与进程属于集成测试。组件集成测试按目录前台顺序执行，见[测试规则](../../../apps/camctl/tests/AGENTS.md)。

## 两份计划的交付接缝

下表定位 M1 已交付、两端共同消费的接口；字段、数值和通道规则的权威定义保留在通知协议与 CLI 专题。

| 接缝 | 已交付的接口与责任 | 消费任务 |
| --- | --- | --- |
| 描述符交接 | 采用仅 run 接受的 `--host-notification-fd FD`。写端必须为可写管道、非标准流，缺省为未接入；显式非法参数按 CLI 语法错误，失效描述符按通道不可用处理。在[通知协议](../../../protocol/host-notifications.md)与[CLI 规格](../../architecture/cli-commands.md)登记最终名称、所有权、关闭时机和错误分类。 | M3、HN3 |
| 参数与消息 Schema | 新增 `protocol/schemas/host-notification.schema.json`，其中唯一的电机参数定义供计划 Schema 引用；同一组合法／非法夹具供 Python 与 C 验证，C 不增加运行时 Schema 依赖。 | M2、M3、HN1、HN2、客户端 |
| 写入结果 | `NotificationWriteResult.kind` 为有限类型，区分完整写入、明确失败及部分写入导致通道停用，保存实际 errno 和已写字节事实。异常不转成完整写入；后续动作不能复用已有消息前缀的通道。 | M3、M4 |
| 运行责任 | 一个 run 持有一个发送器；消息串行写入，描述符不得进入工具或报告子进程。host 自行排队与回调，不参与动作数据库事务。 | M3、HN3、HN4 |
| 真实双方验收 | M6 负责根目录跨组件用例，HN5 提供记录 `int position` 的 C 主程序替身及可控阻塞点；两端共用测试，不各自维护一份“对端已成功”的假设。 | M6、HN5 |

M1 → M2、M3；M2 与 M3 → M4 → M5 → M6。HN1/HN2 在 M1 的机器格式基线完成后推进；M6 需要 HN4 的可运行组件，不依赖 HN5 的最终完成声明，因此不存在互等。

## 状态与横切验收模型

下表是[权威状态表](../../architecture/motor-control.md#状态判断与恢复)的测试映射，判定优先级以该表为准。动作数据库事实、当前流程是否持有本次许可、时间、取消和通道状态须分别建模。

| 输入分区 | 必须可证伪的结果 | 任务 |
| --- | --- | --- |
| 已有终态；或恢复读到意图但没有可靠结果 | 终态保持；未知意图以失败收场且写入次数为 0，即使此时窗口已经结束或取消已到达。 | M2、M4、M6 |
| 无意图且未来到期；窗口开始；窗口末端；刚超过末端 | 分别等待、可发送、可发送、过期；`max_delay_ms = 0` 只在准确时点取得资格。 | M4 |
| 无意图且取消已可靠生效；本次持有意图但最后检查可靠未发送 | 不写管道；取消或过期事实与终态原子保存，首次观察正确区分过期原因。 | M2、M4 |
| 通道未接入或已失效；意图保存失败或提交未知 | 前者按动作失败处理，其他动作继续；后者核实状态库结果，未确认许可前零次写入。 | M2、M3、M4 |
| 已进入发送时取消；完整写入后回调仍未执行 | 本次取消逐项失败，不撤回通知；完整写入仍可保存成功，不等待回调。 | M4、M6 |
| 写满管道、关闭读端、错误或短写 | 有界返回，动作失败且不重试；短写关闭并停用发送通道，不能拼接下一条消息。 | M3、M6 |
| 最后检查墙钟失去可信性，或状态库前提失效 | 不开始发送；按会话规则处理。没有可靠未发送结论时，恢复按未知意图收场。 | M4、M6 |
| 结果提交前、提交中或提交后中断 | 原操作键核实完整事务；已提交终态复用，否则恢复未知，管道写入次数不增加。 | M2、M6 |

**重点复核：** 精确数值受理后到 C 参数是否失真（M1/M3）；提交未知是否错误重授发送许可（M2/M6）；取消与最后写入之间是否竞态（M4）；未来动作与结束阶段 submit 是否漏接管（M4/M6）；报告重建或 wheel 缺少资源是否造成恢复偏差（M5/M6）。每项均须有对应测试，不只做人工检查。

## 实施任务

### M1 公共机器定义与两端接口基线

**预计文件：** `protocol/schemas/plan.schema.json`、`status-report.schema.json`、新增通知 Schema 与 `protocol/examples/host-notifications/`；`protocol/errors/workflow-codes.json`；`scripts/check-protocol.mjs`；`apps/camctl/scripts/sync_resources.py`、`contracts/schemas.py`；通知及 CLI 责任专题。

**输入／输出：** 输入为权威动作与通知规格；输出为可离线解析引用的 Schema、共同夹具、规范消息和描述符交接定义。分别登记通道不可用、明确发送失败、恢复未知三个错误原因，具体名称与编号只在已有权威登记维护。

- [x] 新增协议正反例：位置为负数、零、两端边界、`1.0`、`1e2`；缺字段、布尔、null、字符串、非整数、越界、额外字段、错误 `device_id`、缺时间策略。分别断言整份拒绝与动作失败的既有边界，合法数学整数通知编码为整数。
- [x] 运行 `node scripts/check-protocol.mjs`，确认新增合法样例因未支持动作失败；检查器本身增加对应反例测试，不能只更新样例使其自证。
- [x] 实现 Schema 引用与错误登记，扩展 Python 包资源映射及 Schema 加载器的引用注册；在责任专题登记上表的通道细节。用现有标准库与 yyjson 能力评估发送／接收方案，不引入第二个 JSON 框架。
- [x] 再运行协议检查；在 `apps/camctl/tests/integration/contracts/test_motor_schema.py` 验证生产加载器可解析新引用，运行下文组件集成命令的 `contracts` 目录。检查客户端现有 Ajv 加载入口是否需同步注册共享 Schema，并将该改动交给客户端消费任务。
- [x] 审核两端消费的字段、参数、错误与夹具完全一致，再交付 M2/M3/HN1；建议独立提交公共机器契约。

### M2 受理、专属发送事实与历史事务

**预计文件：** `acceptance/definitions.py`、`acceptance/rules.py`、`devices/catalog.py`；新增 `motor/models.py`、`persistence/repositories/motor.py`；`docs/camctl/database/schema/core.sql`、`workflows.sql`、`enum-registry.json`、`event-transitions.json`、`report-dependencies.json`；`history/` 与相应生成资料。

**接口建议：** `MotorRepository.prepare_send(request, operation_key, owned) -> DbOutcome` 保存唯一意图；`finish_send(request, operation_key, owned) -> DbOutcome` 原子收场。请求用具名不可变类型携带动作 ID、事实时刻与发送结果；已存在的意图查询结果不等于本次流程可用的许可。

- [x] 在 `tests/unit/motor/test_models.py` 验证状态分类及参数精确转换；在 `tests/integration/persistence/test_motor_transactions.py` 建立真实受理、意图唯一性、结果共同提交、同键复用与反例，先运行并确认目标断言失败。
- [x] 接入 `Catalog.action_types()` 的内置动作支持，验证空设备配置仍能受理电机；不要求相机驱动、设备能力或已注册 host 回调。建议新增以 action ID 唯一关联的发送记录，显式区分意图未决、可靠未发送、完整写入与明确失败。已有终态禁止新增意图；无记录仅对尚未发送阶段合法，已开始却缺必要记录必须报一致性错误。追加登记而不重排既有编号；执行定义为 `{}`，原始参数保持权威。
- [x] 在一个事务保存意图及开始事实；另一个事务共同保存结果、终态、父计划汇总和报告变化。取消或过期的可靠未发送结论也共同保存。沿 `commit_operation` 的实际结果核实接口处理提交未知；同键重用必须核对原动作、输入和完整结果，不能直接获得第二次发送许可。
- [x] 为新增历史对象接入事件守卫、正逆回放、同一历史边界读取与报告依赖。运行 `persistence`、`acceptance`、`history` 三个目录各自的集成命令；断言回放中发送端口零调用、历史与投影一致、SQL 拒绝非法组合、并列最后动作使父计划完成。
- [x] 同步生成枚举／事件／历史 SQL 资料，执行下文规格检查；审计受理及历史全部动作类型分支。记录物理表与字段落点，再建议提交本任务。

### M3 独立通知发送器与 CLI 装配

**预计文件：** 新增 `motor/notification.py`；`cli.py`、`bootstrap/lifecycle.py`、`operations/process.py`、`reporting/worker.py`；`tests/unit/motor/test_notification.py`、`tests/integration/motor/test_notification_pipe.py` 及 CLI 参数测试。

**接口建议：** `encode_motor_notification(action_id: ObjectId, position: int) -> bytes` 返回一条带 LF 的规范消息；`NotificationWriter.send(message: bytes) -> NotificationWriteResult` 只负责本次传输，不改数据库；`close()` 释放自有描述符。一个会话只有一个发送器实例。

- [x] 用受约束 write／close 替身验证规范字节、串行调用、完整写入、EAGAIN、EPIPE、EINTR 和短写分类；任何失败后不重发该动作，短写后下一次发送不能写入。先运行单元测试确认缺失行为。
- [x] 建议使用非阻塞管道的一次 `os.write`，不为容量不足等待或重新发送；合法电机消息长度应在写前核对管道原子写入限额。保留短写防御分支，不假定任意描述符都满足管道保证。先读出已有标志再设置需要的标志；不修改标准流。
- [x] 按 M1 的接口接入 run，取得描述符后立即设为不继承；submit 不接入。核对现有工具启动、报告进程及异常退出清理，确认工具启动使用 `close_fds`，报告进程使用 `spawn`；通过真实子进程验证写端不被继承。
- [x] 在真实管道测试中填满缓冲、关闭读端、连续发送及结束会话；断言有界完成、只有通知管道收到消息、stdout 仍只有最终会话 JSON。启动实际工具／报告子进程核对写端不泄漏，避免 host 等不到 EOF。
- [x] 运行 `unit/motor`、`unit/bootstrap` 及 `integration/motor`、`integration/bootstrap` 各自命令；审阅所有描述符退出分支，建议提交发送器与命令接入。

### M4 调度、最后资格检查与取消协调

**预计文件：** 新增 `motor/service.py`、`bootstrap/motor_assembly.py`；`bootstrap/lifecycle.py`、`bootstrap/application.py`、`scheduling/`、`cancellation/` 及取消仓储；新增 `tests/unit/motor/test_decisions.py`、`tests/integration/motor/test_execution.py`。

**接口建议：** `motor_flow(runtime_factory)` 提供现有会话流程签名；`advance_motor(action_id, runtime)` 消费 M2 仓储、M3 发送器、可信墙钟和取消事实。恢复分类与本次新许可分开；动作级协调边界覆盖最后检查至发起写入，所有取消入口使用同一边界。

- [x] 按上文状态矩阵逐行建立断言，先验证失败：精确两端与零窗口、首次观察后耗尽、从未观察窗口、取消先后、通道缺失、未知意图、终态重入、时钟失信，以及意图提交后最后检查发现过期。
- [x] 实现有界候选读取与未来唤醒，复用时间表示和过期原因计算；不复用会创建相机活动的开始事务。先保存意图，再重新核对可信时钟、取消和本次许可，随后发起唯一写入并保存真实结果。
- [x] 接入计划、动作、组及请求 ID 取消；正在写入或可能已发送的目标返回不支持撤回，可靠未发送目标可取消。在事务核实与动作级协调中封闭竞态，不能用普通协程取消代替实际写入结果。
- [x] 接入正常会话、启动恢复、工作发现与退出判断；受限会话禁止新发送，仍按既有允许范围推进取消及报告。验证未来电机维持驻留、并发 submit 被接手、电机失败不阻塞普通相机、相机重试与 host 重启不增加发送次数。
- [x] 运行 `unit/motor`、`unit/cancellation`、`unit/scheduling`；依次运行 `integration/motor`、`cancellation`、`session`、`bootstrap`。审计所有共享取消资格及会话责任入口后，建议提交动作闭环。

### M5 公开报告、历史重建与发行资源

**预计文件：** `contracts/public_projection.py`、`reporting/encoding.py`、`history/`、`apps/camctl/scripts/sync_resources.py`；新增 `tests/integration/motor/test_reporting.py`，扩展既有发行集成测试。

**输入／输出：** 消费 M2/M4 的可靠事实，生成现有公共报告；新增动作只提供公共字段、原始参数、policy 与适用错误／过期原因，不输出执行进度专属记录。

- [x] 建立独立报告预期：成功、过期、取消、通道不可用、发送失败、恢复未知及受理失败。断言没有 `effective_params`、`device_execution`、产物或实际位置；按组取回不把电机列作产物来源。以独立预期核对各报告分区。
- [x] 接入投影与历史字段解释，在相同 H（完整历史边界）比较实时报告、历史重建和快照恢复；重建过程发送端口零调用。错误保持独立机器原因，详情保留实际失败步骤。
- [x] 在仓库外安装 wheel，验证新 Schema 及引用、SQL、登记资源齐全，真实 submit/run 与报告通过公共校验；不得依赖工作目录中的根 protocol 才能运行。
- [x] 运行 `integration/motor`、`history`、`reporting`、`bootstrap` 的独立命令，交付客户端可导入的报告夹具及成功含义；建议提交报告与发行接入。

### M6 真实 CLI 与 host 组合、恢复和交接

**预计文件：** 新增 `tests/integration/test_camctl_motor_notifications.py`，复用根测试的真实 CLI 配置与状态库辅助工具；HN5 提供 C 驱动。更新 `tests/integration/README.md`、`docs/camctl/verification.md` 和本计划验证记录。

**前置：** M1—M5 与 HN4 的生产能力。测试不替换真实受理、SQLite 事务、管道、host 解析或报告；只替换主程序电机控制与设备接口。

- [x] 新增真实组合测试，使用两端生产能力及确定故障点验证契约。以管道写入计数、回调记录和数据库事实分别观察三个阶段，不能只检查最后状态。
- [x] 在意图提交前／后、写入前／后、结果提交前／后设置受控中断点；包括“数据库已提交但调用方未获结果”。重启真实 CLI：无意图按窗口判断，有未知意图零次重发，终态及同请求重送零次重发；历史回放和报告重建也为零次。
- [x] 组合慢回调与并行 submit、CLI 异常退出、窗口结束、取消、管道满、下一次 run 和已有相机动作；证明 host 可回收 CLI，旧管道未处理完仍占 run 位置，通知顺序不乱且整体内存有界。
- [x] 运行下文根组合命令、camctl 单元全量及所有受影响组件集成目录。组合客户端真实导出／报告导入，断言展示“控制通知已发送”的事实而不推断到位。
- [x] 审计从原输入、原子意图、外部写入、结果、取消、报告到恢复的全部接缝，记录日期、环境、命令与结果。更新 M1—M6、路线图及软件验收映射；TX2、真实电机和物理断电分别保留部署待核验项。

## 验证命令与完成条件

以下命令从仓库根执行。任务中的预计文件和内部接口属于实施建议，实际测试入口以验证记录为准。复选框表示对应行为及门禁已落实，具体命令与结果见文末。

```bash
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 python -c 'import sys, sqlite3; print(sys.version); print(sqlite3.sqlite_version)'
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/unit/motor -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest apps/camctl/tests/integration/motor -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest tests/integration/test_camctl_motor_notifications.py -q
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest tests/integration/test_camctl_motor_recovery.py -q
node scripts/check-protocol.mjs
node --disable-warning=ExperimentalWarning scripts/check-event-transitions.mjs
node --disable-warning=ExperimentalWarning scripts/check-report-dependencies.mjs
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 python scripts/check-database-spec.py
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 python scripts/sync-history-object-sql.py
```

其他组件集成目录使用同一 pytest 命令替换末尾目录，一次一个进程；单元全量将目标改为 `apps/camctl/tests/unit`。生成命令与静态检查见[脚本说明](../../../scripts/README.md)，生成前先审阅权威登记，不手改生成区段。SQLite 须满足[统一运行库条件](../../camctl/sqlite-runtime.md)，根组合还需要 Linux、CMake 及 C 编译器。

完成条件是：M1—M6 的范围逐项有真实证据，不重发矩阵全部通过，公共机器检查与相关原有业务回归通过，独立发行物可运行。客户端业务接入、HN5/HN6 及部署联调各自跟踪，不因 camctl 局部通过而改成完成。

## 验证记录（2026-10-08）

实际实现由 `motor/models.py` 保存请求、许可和结果类型，`motor/rules.py` 分类状态，`motor/service.py` 推进单次副作用，`motor/notification.py` 管理管道写端，`bootstrap/motor_assembly.py` 读取有界候选并装配流程，`persistence/repositories/motor.py` 保存及核实事务。持久化为唯一关联动作的 `motor_notifications`；动作编号 8、事件编号 34 和错误编号 27—29 均来自权威登记。

公共 Schema 与发送事实模型已有实施前失败用例；服务原键核实的三个反例在接入前失败，接入后通过。部分真实跨组件用例在两端能力可运行后建立，证据为确定故障注入与独立结果断言。host 解析、队列及背压另有可恢复变异核验，具体记录由 HN 计划维护。

| 工作包 | 实际验证与闭合依据 |
| --- | --- |
| M1 | 4 份公共 Schema 和 76 个电机原数夹具通过 Python、C 与客户端消费；协议检查器有独立正反例。 |
| M2 | `test_motor_transactions.py` 验证唯一意图、共同终态、同键复用、完整历史核实及非法状态；取消 APPLY／RESULT 以原请求和键在新连接只读核实。验收不存在绕过事务内核的业务写入。 |
| M3 | `test_notification.py`、`test_notification_pipe.py` 验证单次非阻塞写入、EAGAIN／EPIPE／EINTR、短写关闭、真实工具及 spawn 报告进程不继承写端、配置失败提前关闭。 |
| M4 | `test_decisions.py`、`test_service.py`、`test_motor_cancel.py` 与真实会话覆盖窗口、最后资格、许可消费、四种取消入口、取消共同终态、时钟失信及 UNKNOWN 处理。 |
| M5 | `test_history_reports.py` 比较同 H 初始回放、快照正向和当前投影逆向恢复，并核对分组来源排除电机；仓库外安装 wheel 后真实运行和报告校验通过。客户端保留失败原输入的精确数值，成功只表示通知已发送。 |
| M6 | 根 `test_camctl_motor_recovery.py` 的 21 项、`test_camctl_motor_notifications.py` 的 5 项测试通过；真实 host 与客户端组合结果见[统一验证记录](../../camctl/verification.md#电机控制与单向通知验证2026-10-08)。 |

11 个故障断点分别为意图保存前／后、写入前／后、结果保存前／后、窗口观察已提交但返回未知、意图已提交但返回未知、结果已提交但返回未知、意图可靠未提交但返回未知、结果可靠未提交但返回未知。无可靠意图时后续 run 重新判断窗口；已保存意图且无可靠结果时以 UNCONFIRMED 收场；已提交结果复用终态。每个分区均核对消息数量、发送记录、原请求重送及报告重建没有额外副作用。

Python 单元 3489 通过、1 跳过；16 个组件集成目录逐一执行，共 3774 通过、2 跳过。公共协议、事件、报告依赖、数据库结构及历史 SQL 检查通过，Node 检查器测试 114 通过。环境、各目录计数、跳过原因与客户端／host 证据统一记录在[软件验证](../../camctl/verification.md#电机控制与单向通知验证2026-10-08)，不以规格检查替代生产验证。

软件验收之外仍保留 TX2／ARM64、真实主程序与电机、单位和业务范围、目标性能及物理断电联调。其归属与输入见[部署交接](../../camctl/verification.md#部署交接与待核验项)。
