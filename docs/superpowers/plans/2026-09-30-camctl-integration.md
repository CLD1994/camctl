# camctl 跨组件集成实施计划

> 供执行 Agent 使用：按 `superpowers:executing-plans` 逐任务实施。复选框只记录实际实现和验证。内部类型、函数、测试路径及提交拆分是建议；公开协议、进程收场和持久化规则是硬性要求。

**目标：** 让现有客户端与 C 接入模块使用真实 camctl 完成计划导出、递交、执行、文件领取、报告导入及累计确认，并为第一版每项软件契约提供实现和验证归属。

**组织建议：** 本计划负责组件之间的接缝及完整业务链。camctl 模块的局部行为由各自计划实施；客户端报告字段、合并和展示沿用[客户端适配计划](2026-09-30-report-client-adaptation.md)，不另写一套相同任务。C 模块的进程身份和原组收场在 I1/I2 具体落实。

**技术基础：** 现有 CMake、CTest、cmocka、TypeScript、Vitest，以及 B1 建立的 Python 测试环境。设备使用受 D2/D4 契约约束的替身。依赖版本仍由各组件权威清单维护。

**设计依据：** [会话](../../architecture/protocol-session.md)、[请求身份](../../architecture/plan-acceptance.md#request_id)、[文件交接](../../architecture/file-handoff.md)、[C 启动和回收](../../host-demo/design.md#受管工具的启动与主程序回收约定)、[软件验收](../../camctl/verification.md)、[数据库一致性验收](../../camctl/database/consistency-verification.md)。

[路线图](2026-09-30-camctl-implementation-roadmap.md) · [共享实施契约](../../camctl/module-contracts.md)

## 边界、输入与可观察结果

客户端先可靠保存固定请求身份和正文，再提供下载；再次下载只按独立 ACK 规则更新累计确认，不改变执行意图。C 模块递交输入并管理 CLI 进程，camctl 保存业务事实；C 模块不读取状态库、不判断设备效果。camctl 发布 ready 文件，C 模块领取到 processing；客户端核验报告并可靠保存业务对象后，才推进 ACK。源文件、交付文件和报告文件各按自己的生命周期恢复。

软件集成使用真实 CLI、SQLite、文件、线程、进程及客户端服务。测试替身控制设备响应和故障，不能替换 camctl 的事务、报告生成或客户端导入。真实设备、目标 ARM64 安装、目标资源和物理断电有独立交付，不作为本计划的软件通过条件。

任务所列新增路径是建议；实施前须核对现有入口，扩展现有测试而不复制同一行为。单元测试只调用纯规则或受约束系统接口替身；进程、实际数据库及文件测试按集成测试执行。

## 组件接缝与责任

| 接缝 | 输入与输出 | 必须保持的责任 |
| --- | --- | --- |
| 客户端导出 → C 递交 | 规范请求 ID、固定正文、精确时间及独立 ACK。 | 下载前已经可靠保存原请求；重送保持身份，不把草稿身份当公共请求身份。 |
| C 启动 → camctl | 命令、输入路径、独立进程组及启动证据。 | exec 前建立组；明确未启动与程序已经执行后退出 127 分别判断。 |
| camctl 退出 → C 收场 | 原具体 PID、被保留的退出记录、原组成员及线程状态。 | 原组全部停止后才最终回收和放行新 run；输出完成不是收场证据。 |
| camctl 发布 → C 领取 | 实际文件身份、ready/processing 位置与同步事实。 | 领取竞争按实际位置确认，C 不把普通交付未知当报告补投。 |
| 客户端导入 → ACK | 文件名、原始字节、结构与历史校验、可靠保存结果。 | 任一校验或保存失败均不推进 ACK，不留下部分业务更新。 |
| ACK → camctl | 已知报告身份及冻结范围。 | ACK 与输入共同提交，同水位仍可结束适用同步；不制造新业务变化。 |

## C 退出与原组收场模型

C 模块先观察原 camctl 退出并保留其退出记录，再检查原组。建议使用 `waitid(P_PID, pid, ..., WEXITED | WNOHANG | WNOWAIT)`；最终只对保存的具体 PID 回收。主程序保持 `SIGCHLD` 回收约定，不抢先回收模块子进程。建议将 `/proc` 扫描和状态解析拆成适配器及纯函数；每次只处理有限数量的条目，并保存本轮游标，工作线程仍可接收 submit 与领取文件。

| 调用和扫描状态 | C 模块下一步 | 能否放行新的 run |
| --- | --- | --- |
| 启动前信号约定不满足，或独立组建立失败 | 保存接入错误；不启动或先收场已经创建的进程。 | 只能沿既有明确启动失败规则判断，不能退回主程序组执行。 |
| camctl 仍运行 | 继续监测具体 PID 和输出。 | 不能启动第二个 run；普通并行 submit 保持原规则。 |
| camctl 已退出，退出记录及原 PGID 可靠，扫描发现存活执行进程或线程 | 向原组发送 SIGKILL，继续检查实际结束。 | 不能放行，发送信号不是完成。 |
| camctl 已退出，扫描一轮尚未完成 | 保留管理位置，继续有界扫描。 | 不能把部分扫描当空组。 |
| 原成员查找时存在，读取时明确 ENOENT | 按已消失成员继续本轮，仍须完成其他成员检查。 | 只凭该成员消失不能放行。 |
| 读取被拒绝、资料不完整、解析失败，或成员状态无法可靠判断 | 保留管理责任和具体诊断，不解释为无成员。 | 不能放行。 |
| 主线程是僵尸，但 task 中仍有运行、睡眠、暂停或等待系统调用的线程 | 继续原组收场及检查。 | 不能放行。 |
| 发现其他代码提前回收原 camctl，或原身份无法可靠确认 | 保存接入约定失效；不根据旧数字继续发送组信号。 | 不能放行可能冲突的新调用。 |
| 原组执行工作已经全部停止，原退出记录仍由模块持有 | 最终回收具体 PID，完成输出管道处理，再调用既有调度规则。 | 按待启动要求、异常重启资格及原剩余预算决定。 |

工具和包装程序保持 camctl 原组归属及查询、终止权限。已经脱离原组的共享 ADB 服务端继续其独立生命周期；扫描结果不能证明不存在违反启动约定而脱离的工具。因此 I1 验证归属约定，I2 验证组内收场，两者共同成立。持续底层不响应由部署侧处置，不新增业务状态或以固定等待时间宣称收场成功。

## 任务依赖

| 任务 | 前置交付 | 路线图使用位置 |
| --- | --- | --- |
| I1 | 既有 C 模块及正式主程序集成约定。 | 阶段 2 前完成启动责任，供 O3/O6 使用。 |
| I2 | I1；具体 PID 退出观察与原组身份。 | 阶段 2 完成，阶段 3 随工具接入复验。 |
| I3 | B5/D1 静态能力；K1/K2 表示；客户端适配计划任务一至四。 | 阶段 2 完成首条报告链，后续按新增动作复验。 |
| I4 | B6、A6、S6、R1—R8、L5、F5、I1—I3 的当前业务范围。 | 阶段 2 无设备闭环门禁。 |
| I5 | C1—C8、X1—X11、N1—N5 及报告、历史、调度的真实生产能力。 | 阶段 3—6 逐链新增，阶段 8 收齐。 |
| I6 | B1—B6、其余模块软件门禁及 I4/I5；发行检查 B7 除外。 | 阶段 8 汇总证据，再交 B7 验证独立发行物。 |

I4 只要求其无设备范围的能力；S6/B6 随后新增处理器时持续复验。I5 的组合用例不依赖 C9/X12/N6 的“已经通过”声明，直接使用生产能力；最终验收任务共享输入事实和接口契约，不相互等待。I6 不依赖 B7，发行安装由 B7 最后补齐证据。

## 实施任务

### I1 独立进程组、明确启动结果与主程序约定

**预计文件：** `apps/host-demo/src/process.c`、`apps/host-demo/src/process.h`、`apps/host-demo/src/host.c`、`apps/host-demo/include/camctl_host.h`、`apps/host-demo/tests/unit/test_process_unit.c`、`apps/host-demo/tests/integration/test_process.c`、`apps/host-demo/tests/integration/fake_cli.c`，以及 `docs/host-demo/design.md`、`apps/host-demo/README.md`。

**接口建议：** `launch_call(command, input, options) -> launch_result` 保存具体 PID、原 PGID、启动证据和输出句柄；`check_child_reaping_contract() -> integration_status` 只检查可检查的信号设置。名称和结构按现有 C 接口调整，公开接口变更须同步调用方。

- [ ] 在纯分类函数或系统接口替身上建立 `test_group_failure_never_executes_camctl`：组建立失败时执行入口调用次数为 0。分别验证 SIG_IGN、SA_NOCLDWAIT、普通 SIG_DFL、明确未启动及已执行后退出 127；不能通过自行修改全局信号设置修复前提。
- [ ] 执行既有 CMake 配置与构建，再运行 `ctest --test-dir .local/host-demo-build -L unit --output-on-failure`；确认新增断言确实因目标能力缺失而失败。
- [ ] 在 exec 前建立独立组，使用已有标准系统接口的错误证据；设置失败拒绝启动。run 和 submit 分别持有管理记录，只等待自己的具体 PID，关闭不应被工具继承的锁及无关描述符。
- [ ] 用真实进程运行 `ctest --test-dir .local/host-demo-build -L integration --output-on-failure`，确认普通后代继承原组、同时 run/submit 组互不干扰、主程序其他子进程正常回收及输出规则成立。
- [ ] 审阅所有启动和错误清理入口，记录“其他线程不提前回收”的主程序责任及验证证据；建议提交“feat: 完成 CLI 独立组启动与接入检查”。

### I2 保留退出记录、分批检查原组与最终回收

**预计文件：** `apps/host-demo/src/process.c`、`apps/host-demo/src/process.h`、`apps/host-demo/src/scheduler.c`、`apps/host-demo/src/scheduler.h`；建议新增 `apps/host-demo/src/process_group.c`、`apps/host-demo/src/process_group.h`，并更新 `apps/host-demo/CMakeLists.txt` 的源文件登记。单元测试扩展 `apps/host-demo/tests/unit/test_process_unit.c`、`apps/host-demo/tests/unit/test_scheduler.c`，集成测试扩展 `apps/host-demo/tests/integration/test_process.c`、`apps/host-demo/tests/integration/test_host.py`、`apps/host-demo/tests/integration/host_driver.c`。

**接口建议：** `observe_exit(pid) -> exit_observation`、`scan_group_batch(identity, cursor, limit) -> group_scan_result`、`advance_reaping(call, observation) -> call_transition`。扫描返回 LIVE、COMPLETE、UNKNOWN 或本轮尚未完成，UNKNOWN 携带具体步骤；进程 stat 解析按格式处理包含空格及括号的 comm，不按空格简单切分。

- [ ] 建立 `test_kill_does_not_release_run_slot`，kill 返回成功但存活线程尚未退出时，调度器不得启动新 run。按上表逐个验证扫描未完、ENOENT、EACCES、不完整资料、解析错误、僵尸主线程仍有活线程、身份丢失及提前回收。
- [ ] 执行 `ctest --test-dir .local/host-demo-build -L unit --output-on-failure` 并确认失败。涉及实际 /proc、线程和子进程的用例只放在集成测试，系统接口替身由真实接口约束。
- [ ] 用 WNOWAIT 保留退出记录和身份保护，在可靠原身份下向原组发送 SIGKILL；有限批次检查进程及 task，全部执行工作停止后才最终回收具体 PID。停止请求后重新开始完整核验轮；成员变化或本轮未完不能沿用半轮结果宣称收场。调度器只在管理责任完成后应用既有重试和待启动规则，收场不重置预算。
- [ ] 执行 `ctest --test-dir .local/host-demo-build -L integration --output-on-failure` 及 O6 真实组合；用同步点控制父退出、成员扫描和线程退出顺序，验证成功退出仍有后代、异常退出后下一调用、并行 submit 及共享服务端独立生命周期。不得运行全局 adb kill-server。
- [ ] 审阅所有 wait、信号、扫描和放行入口，确认 C 不读业务库、不把本地停止写成设备成功；建议提交“feat: 完成原组收场与延后回收”。

### I3 客户端请求身份、时间、能力及报告消费

**预计文件：** `apps/client/src/server/application.ts`、`apps/client/src/server/database.ts`、`apps/client/src/server/models.ts`、`apps/client/src/shared/plan.ts`、`apps/client/src/shared/types.ts`，建议新增 `apps/client/src/domain/request-id.ts`、`apps/client/src/domain/protocol-time.ts`；单元测试建议为 `apps/client/tests/unit/request-id.test.ts`、`apps/client/tests/unit/capture.test.ts`，集成测试扩展 `apps/client/tests/integration/requests.test.ts` 及现有报告测试。报告字段及展示按[适配计划任务一至四](2026-09-30-report-client-adaptation.md#任务一类型及基础身份适配)执行。

**接口建议：** `newRequestId(random, usedIds: RequestIdLookup) -> CanonicalId`、`formatProtocolTime(value) -> UtcText`、现有 `exportDraft` 与 `downloadRequest`。RequestIdLookup 按单个 ID 查询索引，不加载全部历史请求；查询失败不能当作身份未使用。工程建议由 Node crypto 提供均匀随机的合法正 63 位整数，保持 BigInt 和规范字符串；与本地已保存请求冲突时有限重选。分配策略及有限重选上限由该分配器集中定义，实施前审阅；公共契约不把请求 ID 作为顺序。该建议使用随机身份来支持独立客户端，保证范围不同于主机单库分配的单调实例 ID。

- [x] 建立 `test_export_retry_preserves_request_identity`：首次导出并保存后再次下载保持 ID 和正文，新的草稿意图取得新 ID；单独验证 0、越界、最大合法值和大于 Number 安全范围的身份。随机源及 usedIds 用受约束替身，冲突重选耗尽返回导出错误，不返回默认 ID。
- [x] 运行 `pnpm --dir apps/client exec vitest run tests/unit/request-id.test.ts tests/unit/capture.test.ts`，确认失败来自真实身份或导出规则。先获取现有基线并按根因区分，不把历史失败归因于新任务。
- [x] 把请求身份分配、原请求保存及草稿状态更新放在现有同一客户端事务内；下载只在提交后发生。完整审计验证用的临时身份、请求引用、查询参数、数据库读写及排序，保证身份不经 Number。时间按公共格式输出；describe 的真实能力直接供 Ajv 和界面消费。
- [ ] 运行客户端类型检查及分类测试：`pnpm --dir apps/client typecheck`、`pnpm --dir apps/client test:unit`、`pnpm --dir apps/client test:integration`。用真实客户端库验证保存失败不下载、重启后原请求和 ACK、真实能力样例判定及报告保存后再确认。
- [ ] 核对客户端报告适配任务一至四、时间和能力的全部消费者；将旧存储与当前表示的兼容问题按客户端有效数据规则处理，不伪造默认字段；建议提交“feat: 接入客户端公共身份与能力协议”。

### I4 无设备的真实导出、报告、领取与 ACK 闭环

**预计文件：** 新增 `tests/integration/test_camctl_report_roundtrip.py`、`tests/integration/camctl_fixtures.py`；扩展 `apps/host-demo/tests/integration/host_driver.c` 和客户端集成驱动。实际生产入口复用 B6、S6、R7、F5 与客户端服务，不写第二套演示业务。

**协作输入：** 初始化后的临时部署目录、真实包、只含 report_status 的合法计划和可靠客户端存储；另一组输入为非法正文但有效 ACK。客户端驱动输出实际文件及可靠保存凭据，C 驱动使用公开接入接口。

- [x] 建立 `test_report_import_ack_roundtrip`，独立断言请求身份、计划与动作事实、报告摘要、客户端已保存对象和主机累计确认；主程序领取前后分别检查真实位置，不能仅断言“流程没有抛错”。
- [x] B1 测试环境建立后运行 `uv run --project apps/camctl --group test pytest tests/integration/test_camctl_report_roundtrip.py -q`，确认失败来自尚未贯通的接缝。
- [x] 串联真实客户端导出、C run/submit、camctl、ready→processing、客户端原字节导入及后续 ACK。测试驱动只负责递交和读取，不直接修改业务库以制造期望状态。
- [ ] 再运行同一命令，分别覆盖输入拒绝但有效 ACK、报告冻结后新提交、同请求重送、只吸收 ACK、领取即删除、报告保存失败重试、普通生成失败及数据库失效日志副本。可靠保存失败时 ACK 不推进；旧报告重建的字节保持。（八场景已覆盖五：非法正文有效 ACK、报告冻结后新提交、同请求重送、领取即删除、ACK 吸收；报告保存失败重试、普通生成失败、数据库失效日志副本随对应故障注入链路补齐。）
- [x] 审阅所有交接点的事实和失败诊断，再运行相关 C/客户端组件集成；建议提交“test: 验证无设备跨组件闭环”。

#### I4 验证记录（2026-10-07）

已建立 `tests/integration/test_camctl_report_roundtrip.py` 三个用例与共用基础设施：`Deployment(devices=False)` 无设备部署、客户端导出/导入驱动（`camctl_fixtures.py` 的 `export_plan_with_client`/`import_reports_with_client`）、WSL 构建的真实 host-demo 会话（`HostDemo`：stdin 命令协议 submit/claim，camctl 启动桥转换 /mnt 路径与 Windows 解释器）。C 模块为 POSIX 实现，按部署验证裁决在 WSL x86 Linux 用 git worktree LF 检出构建（Windows 检出的 CRLF 会破坏 vendor manifest 校验）。

先红证据：主链初次贯通失败于客户端导入拒绝报告——报告快照中出现“未执行计划包含已开始动作”（客户端 `validateReport` 拒绝 pending 计划携带 started 动作），及计划停在执行中无人推进。按计划执行状态规格（plans.status 是动作聚合：曾有动作开始且未全部终态即执行中）确认为生产缺陷并在责任边界修复：报告同步动作的开始事务与本地完成事务（`reporting/policy.py`）分别补齐计划首次开始（PLAN_STATUS_CHANGED.START）与全部终态完成（COMPLETE）事件，与拍摄、取回链的既有模式对齐。

同类缺陷审计（同一不变量的全部动作开始入口）：拍摄动作开始（`scheduling.py` StartActionCommand）与取消动作开始（`cancellation.py` _StartCancelCommand）同样不推计划首次开始，一并修复；取回动作的执行入口已于 2026-10-07 随取回链接入 run 会话按同模式保证（`outputs.py` StartObtainCommand 在计划待执行时同事务保存 PLAN_STATUS START，见[产物计划 X10 第四段](2026-09-30-camctl-outputs.md#x10-自动预览及统一发布汇总)），删除动作的执行入口已于同日随清理链接入 run 会话按同模式保证（`outputs.py` StartCleanupCommand 在计划待执行时同事务保存 PLAN_STATUS START，见[产物计划 X8 第五段](2026-09-30-camctl-outputs.md#x8-完整源清理与独立预算)）。

行为事实：主链覆盖 init、describe（`{"devices": []}` 进入客户端存储）、客户端真实导出（整数请求身份、首份无 ACK）、C 受管 submit 与 run、报告发布到 ready、C 领取移动到 processing（ready 撤空、原字节 size/sha256 核对）、客户端真实导入（`saved_report_ids`/`ack_id` 与报告身份一致）、第二份导出自动携带 `last_report_id` 并被受理接口吸收（`runtime_state` 累计确认推进到报告 `to_wm`）。扩展场景覆盖：非法正文但有效 ACK（正文被拒不建计划、ACK 仍被吸收）、同请求重送（计划与动作身份保持一次受理）、领取即删除（主链断言）与报告冻结后新提交（主链第二份即 ACK 组合）。报告保存失败重试、普通生成失败、数据库失效日志副本三场景未在本轮覆盖，随对应故障注入链路（225 行状态库不可用日志副本等）补齐。

回归证据（2026-10-07，Windows 开发机，uv CPython 3.11）：`tests/integration/test_camctl_report_roundtrip.py` 3 项通过；reporting 集成 345 项（含新增同步生命周期计划状态断言与“最后动作成功完成计划”用例）、capture 199、outputs 1787、scheduling 125、cancellation 44、session 81、bootstrap 64、acceptance 226 及其余目录全绿；apps/camctl 单元 3296 项连续两轮通过；根跨组件 capture roundtrip 2 项通过。期间若干轮出现 asyncio 事件循环创建失败的瞬时 error（WinError 10055 系统套接字缓冲区不足）：新旧代码交替对照证实与本次改动无关（stash 版同等失败），涉事测试单独运行均通过，资源回落后整目录全绿。

客户端侧修复：`apps/client/src/server/application.ts` 的 `utc()` 原 `toISOString().replace` 残留毫秒，违反公共协议秒级时间戳；改为 `formatProtocolTime` 截断到秒，配导出正文创建时间格式的集成断言。

### I5 三种采集、取回、取消及清理的跨组件组合

**预计文件：** 建议新增 `tests/integration/test_camctl_capture_roundtrip.py`、`tests/integration/test_camctl_output_roundtrip.py`、`tests/integration/test_camctl_cancellation_roundtrip.py`、`tests/integration/test_camctl_session_recovery.py`，共用受 D2/D4 约束的设备替身和真实客户端、C 驱动；新增生产适配仍归所属模块。

**输入与预期：** 每个用例给出设备能力、输入计划、注入边界、已有事实和独立期望结果。控制设备返回、线程段结束、数据库提交及报告退出的同步点；不通过随机 sleep 安排竞争。

- [x] 分别建立 `test_recording_delivery_survives_late_cancel`、`test_photo_keeps_completed_outputs`、`test_timelapse_recovers_remaining_wait`、`test_cleanup_unknown_preserves_original_request_result`。每个用例只验证所属组合分支，机器身份、状态、错误码及预算精确断言，用户文案只核对必要事实。
- [x] 每引入一条链先运行相应根集成文件，例如 `uv run --project apps/camctl --group test pytest tests/integration/test_camctl_capture_roundtrip.py -q`，取得因缺失契约而失败的证据；前序能力缺失时回到所属模块任务，不在集成驱动中补造业务行为。
- [x] 按阶段 3—6 接入真实生产能力，覆盖不同可选查询/停止能力、文件与产物登记、普通与自动取回、内部录像处理、取消四种入口、清理竞争、终态后责任及新请求接手。
- [x] 运行 `uv run --project apps/camctl --group test pytest tests/integration/test_camctl_capture_roundtrip.py tests/integration/test_camctl_output_roundtrip.py tests/integration/test_camctl_cancellation_roundtrip.py tests/integration/test_camctl_session_recovery.py -q`，交错并发 submit、关闭阶段新提交、未来动作、时钟异常、设备绑定改变、配置重载、报告失败、迟到结果、提交未知和重启恢复。核对原身份、预算、确定结果、旧报告及实际占用，不能只核对最新终态。
- [ ] 沿权威输入到用户结果审计各链所有接缝，补齐真实双方与重要替身的契约组合；建议提交“test: 验证第一版跨组件业务与恢复”。

#### I5 第一条链验证记录（2026-10-06）

已建立 `tests/integration/test_camctl_capture_roundtrip.py` 与共用基础设施：`camctl_fixtures.py`（部署目录、计划构造、剧本替身驱动与部署装配入口）、`_camctl_stub_entry.py`（进程启动阶段登记替身后进入生产 CLI 的部署装配桥）、`client_import_driver.ts`（经客户端 `Application.applyReports` 真实导入路径消费报告的 tsx 驱动）。四个指定用例中 `test_photo_keeps_completed_outputs` 与 `test_timelapse_recovers_remaining_wait` 已完成并通过；录像与清理两用例随各自链路后续建立。

先红证据：两用例初次运行失败于 `设备 cam-1 声明的驱动未部署: 'test-stub'`——真实 CLI 的受理目录与 describe 都从 `default_driver_definitions()` 取驱动定义，而该来源没有进程启动登记点，部署装配无法接入。修复在责任边界完成：新增 `camctl.devices.definitions_runtime`（登记/快照/重复拒绝/reset，与驱动端口登记 `devices.drivers.runtime` 对称的部署接入面），`default_driver_definitions()` 改为返回登记快照；describe 与受理共用同一来源。登记点配 4 项单元测试（apps/camctl/tests/unit/devices/test_definitions_runtime.py）。

两用例的行为事实：照片链覆盖 init、submit（受理保存，`needs_run` 布尔）、run（替身驱动经登记端口推进，动作与计划成功终态、photo 产物登记、活动收场）、报告发布到 ready 根目录（staging 无残留；会话内多批报告只保留最新文件）、客户端真实导入保存（`saved_report_ids`/`ackId` 与报告身份一致）、顶层 `last_report_id` 的 ACK 递交被受理接口吸收（`runtime_state` 累计确认推进到报告 `to_wm`）、完成后产物事实在无取回与清理时保持不变。延时链覆盖 run 会话中断（等待安排已保存为 CAPTURE_WAIT_CHANGED 首次安排）后，第二个 run 恢复剩余等待、保存等待完成事实并按时间与产物判定成功。

测试环境事实：本机没有 ffprobe/ffmpeg，录像链的媒体检查受管工具无法在本机组合，录像用例与真实 C 领取模块（Windows 本机无法编译 POSIX 模块）一并列入后续链路；客户端消费位置目前为 ready（C 领取环节未接入时的等价位置事实），接入 C 领取后改为 processing。

回归证据（2026-10-06，Windows 开发机，uv CPython 3.11）：`tests/integration/test_camctl_capture_roundtrip.py` 2 项通过；根 `tests/integration` 36 项+342 子测试通过；apps/camctl 单元 3296 项连续两轮通过（期间一轮 9F/34E、一轮 1E 为既有记录的 Windows 瞬时资源压力漂移，涉事测试单独运行均通过）；bootstrap 集成 64 项、acceptance 集成 226 项通过。

#### I5 第二条链验证记录（2026-10-07）

已建立 `tests/integration/test_camctl_output_roundtrip.py` 与 `tests/integration/test_camctl_cancellation_roundtrip.py`，四个指定用例全部完成。基础设施扩展：替身补 `camera_record` 能力与 `open_read`/`digest`/`delete`/`query_state` 四个端口及对应证据契约（`file_digest`、`read_returned`、`delete_returned`、`file_absent`、`file_presence`）；删除成功按契约回填文件缺席观察并移除设备内容，查询按设备内容实时报告存在性；新增 `install_client_capabilities` 把 `camctl describe` 输出写入客户端能力文件（客户端导出链的 `validatePlan` 需要非空能力）。

两用例的行为事实：录像链覆盖客户端导出（record+obtain 同计划、按名称解析取回来源）→ run（录像、检查、修复、读取拷贝与摘要、交付 ready，ready 文件字节与替身内容一致）→ ready 手动移入 processing 模拟 C 领取 → 顶层 `request_id` 目标的迟到取消（processing 交付不可撤回，取消动作成功，原动作与交付事实保持，processing 文件字节不变）→ 报告导入与 ACK 吸收。清理链覆盖删除调用持续失败且查询确认文件仍在时按删除预算耗尽失败（成员失败、产物转受限、拍摄成功与产物身份保持）→ 首批报告（`cleanup_items_failed`、产物受限如实呈现）→ 客户端从报告取得产物身份后另一请求精确删除成功（产物转已清理、旧失败成员与旧动作事实不被改写）→ 第二批增量报告只含新变化实体 → 最终 ACK 吸收。

先红证据与责任边界修复（均为既有生产缺陷，跨组件组合首次暴露）：①取回开始事务缺少 `obtain_source_selections` 表的事实预置，同计划按名称引用来源时开始事务回滚（`persistence/repositories/outputs.py` 预置空映射）；②`camera_result` 公开投影在检查、修复与清理字段全部省略时产生空对象，违反 minProperties（`report-dependencies.json` 的 when 收紧为存在处理行且有可报事实才投影，`exists` 短路保护无处理行分支）；③终态取回目标的撤回明细永不推进（TERMINAL 生效有明细时成员改保持处理中、编排对处理中成员一律结算、事件预算按是否携带结果事件动态分配，`cancellation` 仓储与编排同步修改，`test_target_types.py` 三用例改经 `apply_cancel` 真实编排驱动）；④清理成员终态时删除与存在性查询两条伴随流程的行保持待执行/执行中且带重试等待，会话的流程收尾计数无法归零，`run` 命令永不退出——新增 `StaleRunFinish` 伴随收场事务命令（`operations` 仓储逐行保存终态并清除重试等待，与尝试结束的流程收场分支同一守卫约束），清理流在每个成员终态点统一收场两条伴随流程，`already_terminal` 分支按成员已保存终态补齐收场（事务间中断的恢复路径），成员选择查询同时选中仍有伴随流程未收场的终态成员。

伴随收场实现过程中的一次方向修正：曾把预算耗尽判定提前到尝试结束事务并即时终态化成员，apps 清理链 7 项测试证明其改变既有失败时序（当轮应报 `still_present`/`query_unknown`，耗尽由下一轮开始事务拒绝时收场）；回退为伴随收场单一收口后原有语义恢复且 run 会话正常退出。另修正存在性责任键拼装（`exists/{id}` 而非 `query/{id}`），耗尽详情的已用次数如实统计。

遗留观察（归取消计划 N 链）：清理动作被取消请求标记后，`_running_cleanup_actions` 不再选中该动作，而取消结算对删除中成员等待“执行链”收场——两处组合下删除中成员无人推进，取消动作可能保持执行中；本轮用例未经过该组合，待 N 链按现实目录组合核实并修复。

回归证据（2026-10-07，Windows 开发机，uv CPython 3.11）：根 `tests/integration` 41 项+342 子测试通过（含两个新用例）；apps/camctl 全量 6691 项通过、6 项跳过；五项仓库检查（doc-links、protocol、database-spec、report-dependencies、event-transitions）及 `report-dependencies.test.mjs` 全部通过。

#### I5 第三条链验证记录（2026-10-07，覆盖面用例）

`tests/integration/test_camctl_output_roundtrip.py` 新增三个用例完成 checkbox③ 覆盖面：`test_auto_preview_obtains_registered_preview`（自动预览链）、`test_duplicate_auto_preview_fails_at_admission`（重复自动关联）、`test_plan_cancel_before_start_settles_auto_preview`（计划级取消联动，`plan_instance_id` 入口）。至此取消入口跨组件覆盖 `request_id`（录像迟到取消）与 `plan_instance_id`（执行前取消）两种，`action_instance_id` 与组入口按 N 链后续组合；终态后责任（录像迟到取消）、新请求接手（精确清理）已由前两条链覆盖。替身扩展：`photo_preview_supported` 声明面与 `preview_file(identity, paired_identity)` 列举条目（驱动配对关联表达）。

先红证据与责任边界修复（均为既有生产缺陷，跨组件组合首次暴露）：①预览列举→登记链三处未接线——`_observed_file` 不解释配对字段、`_register_observed` 对所有条目硬编码原片角色、`_finish_capture` 与取消收场路径只登记原片草稿；而 `OwnershipSave`、归属仓储、`OutputDraft` 与目录装配的预览支持全部就绪。修复：列举条目新增可选 `paired_identity`（驱动声明的同批原片关联，解释层校验非空文本），`_register_observed` 改两阶段登记（先全部保存发现与在场事实建立身份映射，再保存归属与完成事实；配对条目以预览角色归属并携带 `DRIVER_PAIRING` 配对证据），配对解析抽为 `validate_observed_pairings` 纯函数（目标缺失、自指、指向预览时整批拒绝）配单元测试；产物目录草稿构造统一为 `_catalog_drafts`（原片与预览按配对分别登记，修复成品只挂原片条目）。②取消资格装配对未开始的非拍摄目标自相矛盾：`load_eligibility_facts` 对取回/取消/清理/报告动作硬造 `dispatch=STARTED`（恒允许标记），而生效事务的停止收场分支要求目标真在执行中——待执行取回被拒绝，run 会话以 `state_db_error` 退出。修复按真实分区表达：`execution_started=0` 的非拍摄目标走未启动分区，执行前取消同事务直接终态化。③执行前取消终态化目标后，目标计划无人推进完成（I4 计划状态缺陷类的又一入口，第八十二段审计只覆盖了置 RUNNING 的三处写入点，直接终态化的 PRE_START 分支未被审计）：全部动作在执行前取消的计划按规格也应进入完成。修复在生效事务同事务补计划完成事件（目标恰好是计划最后一个未终态动作时），`_reuse` 校验相应扩展三事件形状。

三用例的行为事实：自动预览链覆盖能力声明（`preview_supported` 从驱动定义导出）→ 客户端导出（取回三字段全填、`scheduled_at` 与拍摄同一时刻）→ run（拍摄成功、原片与预览分别登记为正式产物、预览设备文件保留原片引用与配对证据、自动取回选择预览产物、交付 ready 字节为预览内容）→ 报告表达两动作成功且自动取回携带 `automation` 展示关系（来源动作实例一致）。重复关联用例覆盖受理事务直接登记两个冲突取回失败（`duplicate_auto_preview`、阶段 `admission`、详情含来源名与全部冲突动作名）、拍摄正常执行、冲突动作无交付。取消联动用例覆盖拍摄执行前的计划级取消：计划实例入口直接针对计划内动作（含自动预览取回），待执行拍摄与关联取回都按取消收场，无产物无交付，报告逐动作如实表达。

录像媒体检查与受管工具环境决策仍列后续链路（本机无 ffprobe/ffmpeg），C 领取真实组合同前。

回归证据（2026-10-07，Windows 开发机，uv CPython 3.11）：`tests/integration/test_camctl_output_roundtrip.py` 4 项通过；根 `tests/integration` 44 项通过；apps/camctl 单元 3303 项通过（含新增 `test_observed_pairings.py` 与 `test_result_listing.py` 配对解释扩展）；cancellation/persistence/capture/bootstrap 集成 386 项、outputs/acceptance/scheduling/operations/session/reporting 集成 2748 项+1 跳过、capture 分目录复跑 199 项通过；五项仓库检查全部通过（database-spec 须在统一 uv 环境运行，系统 Python 的 SQLite 版本不满足统一条件）。

#### I5 第四条链验证记录（2026-10-07，会话恢复与并发）

已建立 `tests/integration/test_camctl_session_recovery.py`，五用例完成 checkbox④ 指定面：进程重启恢复（树级终止后第二个 run 会话恢复剩余等待并完成关闭阶段迟到提交的新计划，运行间配置重载不破坏既有事实）、交错并发 submit 与同身份重送（三个 submit 进程并发、其中两份为同一计划文件，受理恰登记两份计划，run 会话两动作成功且产物恰两份）、未来动作会话驻留（到时前保持待执行，到时执行成功）与过去时刻超窗动作过期收场（EXPIRED、计划完成、产物按动作区分核对）、报告失败不阻止设备工作且责任保留到下一会话补交付（恢复 ready 目录后第二个 run 会话补发报告，客户端导入成功）、迟到结果不改写已确定终态（空列举按 no_outputs 失败后，迟到的设备文件不重开动作也不补造产物）。

先红证据与责任边界修复：并发 submit 首次运行稳定复现 run 会话永不退出，诊断（桥新增 `CAMCTL_TEST_TRACE_DISPATCH` 包装打印单动作异常）揭示一条既有生产缺陷链：驱动确认观察身份与操作目标（设备活动主键，`AttemptTicket.target_id`）不符时，`finish` 的结果校验抛 `OutcomeValidationError` 且被 `dispatch_ready` 吞掉，启动流程遗留执行中、启动尝试遗留在途；photo 处理器重入把在途尝试折叠为调用失败（`attempt[0] != SUCCEEDED`），动作失败终态后启动流程仍无人收场，会话的流程收尾计数永不归零。单动作部署里活动主键与替身固定身份恰好重合，掩盖了该链。修复分三层：

- 生产（启动结果不可采纳的收场）：`_control_call` 对结果校验拒绝按调用失败收场本次尝试（`invalid_device_result`，不保存坏观察与派发成功事实），不遗留执行中的启动流程。
- 生产（在途尝试不折叠失败、动作终态收口启动流程）：photo 处理器重入读到启动尝试在途（RUNNING）或结果未知（UNKNOWN）时，按只发送契约由产物核实证明终局（完整产物登记成功、核实轮次耗尽按无法确认失败），不折叠为调用失败；动作终态的三个分支（成功、保留文件失败、无法确认）用 `StaleRunFinish` 伴随收场仍开放的启动流程（与清理链同模式）。配契约测试三用例（坏观察收场、在途+完整产物恢复成功、在途+无产物保持核实）。
- 测试（替身身份对齐）：确认观察身份须与生产核对的操作目标一致，而控制请求不携带任务身份——替身从部署状态库读当前占用持有者的活动行对齐（授予事务先于控制调用提交、设备占用互斥保证同设备至多一行已派发待响应），新增 `CAMCTL_TEST_STATE_DB` 注入，库不可用时回退剧本身份。

测试基建事实：Windows 上 `Popen.terminate` 只终止直接子进程，报告 worker 孙进程成孤儿并持有状态库，后续连接报磁盘 I/O 错误——新增 `terminate_process_tree`（`taskkill /T /F` 树杀）；树杀后操作系统清理句柄存在短暂延迟，测试查询按短退避重试。重启用例的中断点轮询到启动责任终态（控制调用已可靠收场），终止不落在控制调用在途窗口。

遗留观察（归本计划 checkbox⑤ 审计与 D5 真实设备接入）：控制请求不携带任务身份（`task_key` 生成后未传入）而观察身份校验用活动主键、结果列举回询用动作主键，三处身份语义属 D5 驱动接入的既有接口缝隙，替身以查库对齐模拟“适配层知道任务身份”，D5 收口时统一；timelapse/record 的启动调用在途时进程被杀（在途尝试且发送事实未保存）会话恢复后重入死等，与 photo 同族的启动责任收场缺口，需要按恢复决策表“可能派发，驱动能核实原任务”路径定义核实语义，本轮用确定性中断窗口避开。

回归证据（2026-10-07，Windows 开发机，uv CPython 3.11）：四文件连跑 12 项通过（checkbox④ 命令）；根 `tests/integration` 49 项+342 子测试通过；apps/camctl 集成 3398 项通过+6 跳过；单元 3303 项通过（含 capture 契约三新用例）；五项仓库检查全部通过。

### I6 全量契约映射、软件验收与部署交接

**预计文件：** `docs/camctl/verification.md`、`apps/camctl/README.md`、`tests/integration/README.md`、`docs/client/acceptance.md`、`docs/host-demo/verification.md`；建议新增 `tests/integration/test_camctl_acceptance_map.py`，并在 `docs/camctl/software-acceptance.md` 保存实施时取得的验收映射和真实证据，链接具体测试，不复制登记的完整值清单。

**验收映射字段：** 正式条目及链接、所属模块任务、实际生产入口、具体单元/集成用例、已执行命令及结果、尚未核验的前提。数据库验收的数字条目、W/R/Q/S/O/V/E/J/F/H/P 系列逐条映射，不仅登记一个大范围。

- [ ] 先审计各专题及数据库验收，建立 `test_every_software_contract_has_evidence_owner` 对应的覆盖检查；它验证映射结构和引用，不把有一行映射当行为通过。缺少生产入口或可证伪用例时，明确返回所属模块任务。
- [ ] 先运行 `uv run --project apps/camctl --group test pytest tests/integration/test_camctl_acceptance_map.py -q`，确认缺项被识别；补齐实现及证据后再通过。随后分别执行共享契约中的全部单元、组件集成命令，以及 `uv run --project apps/camctl --group test pytest tests/integration -q`、客户端分类测试与 CTest；执行现有协议、文档和数据库规格检查。只修复已定位根因的失败，不删除测试或放宽规则。
- [ ] 独立核验实际实现、事件校验器、真实导入关系、所有外部调用及异常分支，逐项收齐 C9、X12、N6、R9、H7、F7、D5 软件部分及其他模块门禁。类型、状态、目录、缓存与报告是否遗漏成员须用独立预期核对。
- [ ] 将软件证据交 B7；B7 另行验证源码目录之外的构建和安装。记录 ARM64 运行库、外部工具、真实设备映射、目标资源和物理断电各自的检查输入、执行者和通过条件，不把软件替身的结果写成设备结论。
- [ ] 更新有效运行文档和实际进度，核对没有隐藏的未决行为或消费者缺口；建议提交“docs: 记录第一版软件验收与部署交接”。

## 执行命令与最终门禁

C 模块的构建和分类命令沿用[验证说明](../../host-demo/verification.md#开发环境与已执行检查)，测试脚本使用 B1 建立的环境。首次构建命令为：

```bash
cmake -S apps/host-demo -B .local/host-demo-build -DHOST_BUILD_TESTS=ON
cmake --build .local/host-demo-build -- -j2
ctest --test-dir .local/host-demo-build -L unit --output-on-failure
ctest --test-dir .local/host-demo-build -L integration --output-on-failure
```

最终软件门禁要求全部模块和本计划的适用用例有实际通过证据，客户端导出至 ACK 的所有用户链真实贯通，B7 独立发行物检查完成。软件集成不要求真实设备和物理断电；这些外部前提以单独的部署记录交接。验收映射的通过、生成夹具的通过以及已有组件的单独通过，都不能代替真实跨组件链。
