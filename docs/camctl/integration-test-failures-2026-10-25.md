# 44 项集成测试失败诊断（2026-10-25）

后续实现及实际复测结果见[集成测试失败修复记录](integration-test-repairs-2026-10-25.md)。本文保留诊断阶段的源码、结果和历史证据边界。

原完整回归的 44 项失败中，41 项的首个阻断点已定位到测试装配、替身、断言或依赖环境，2 项涉及 Action6 生产实现缺口，1 项历史会话错误仍缺少可确定底层原因的证据。这里统计的是各身份的历史首因；测试在准备阶段停止时，后面的业务行为仍未验证。

本轮只诊断和保存证据，未修改仓库中的生产代码、测试或运行配置。历史数据库以只读方式提取；用于验证候选原因的写入只发生在独立副本。第 30 项另做一次历史源码窄复现，只在临时诊断副本加入异常链记录，没有重现原会话错误。

## 范围、源码和结果口径

| 证据阶段 | 源码与范围 | 结果 |
| --- | --- | --- |
| 原完整回归 | `48d2ee640f3c1243d0a38f1697aaa7e4a74081e1`；camctl 组件集成、根跨组件 pytest 和 Node 测试；4 个并行任务槽 | 10,290 通过、44 失败、5 跳过；入口墙钟 2,820.160 秒，约 47 分钟。 |
| 此前定向补测 | 多个明确登记源码的补测；按完整 nodeid（测试身份）与原 44 项取交集 | 原失败中的 14 项已有同身份通过证据。本轮没有重跑这 14 项。 |
| 本轮集中诊断复测 | 从 `implement_camctl` 固定 `1973dc05c0d517a890b92e5dcfc1ee32da9266dd`，使用独立源码副本；只选其余 30 个原失败身份；4 个任务槽、根测试按文件分组 | 2 通过、28 失败，退出码 1；入口墙钟 **186.908 秒**。通过的是 ID19、ID42。 |
| ID30 唯一历史窄复现 | 独立临时副本，以原 `48d2ee64` 加诊断记录钩子运行同一身份一次 | 63.553 秒，退出码 1；本次先发生 `window_missed`，没有 START 尝试，未重现原 `state_db_error`。 |

本轮复测使用 Python 3.11.17、SQLite 3.53.1、pytest 9.1.1、Node v24.22.1。详细命令和环境见[调用收据](verification-data/2026-10-25-integration-failure-diagnosis/current/invocation.json)、[集中运行结果](verification-data/2026-10-25-integration-failure-diagnosis/current/summary.json)与[源码快照清单](verification-data/2026-10-25-integration-failure-diagnosis/current/source.json)。

历史 14 项通过和本轮 2 项通过来自不同源码、不同阶段，不能相加后宣称在当前源码一次运行了全部 44 项，也不能替代新的全量通过结果。本报告所称“本轮源码”始终指固定的 `1973dc05`；报告所在工作树基于 `100738f7`，诊断复测没有追随主分支后续变化。后续设计契约复核另比对了 `implement_camctl` 的 `a543720c`，比对范围和证据见下文。

44 项的完整路径、历史首因、逐阶段结果、责任归属和原始收据统一保存在[逐项清单 JSON](verification-data/2026-10-25-integration-failure-diagnosis/case-ledger.json)，便于筛选的版本为[逐项清单 CSV](verification-data/2026-10-25-integration-failure-diagnosis/case-ledger.csv)。本报告的 ID 按原完整回归失败清单顺序固定，不是错误码。

## 首因分类

| 分类 | 数量 | ID | 已确认的具体原因 |
| --- | --- | --- | --- |
| 测试准备消耗启动窗口 | 16 | 17、18、22—29、31、34、35、37、38、40 | 排期确定后仍执行客户端导出、初始化和报告准备，真正准备派发时已超出原窗口；ID18 是照片已成功、后两种拍摄等待派发时耗尽窗口。 |
| OSMO 共享替身接口漂移 | 11 | 5—15 | 历史替身给批量元数据调用返回裸长度，缺少协议帧；本轮目录改走流式接口后，替身更早把目录流当成故障 READ。 |
| Playwright 与浏览器缓存不匹配 | 5 | 21、41—44 | 网页构建隐式按另一锁文件重装依赖，从 1.63.0 切换到 1.64.1，实际要求的 Chromium 1248 未安装；同槽后续测试受影响。 |
| 数据库格式断言过时 | 2 | 1、2 | 测试写死格式 2；历史发行物实际支持并初始化为 3，本轮发行物为 5。 |
| 测试工具解释器绑定错误 | 2 | 3、4 | 假 `hb_cli` 使用测试解释器，找不到隔离安装环境里的 `camctl`。已有测试侧修正及同身份通过证据。 |
| 连接夹具丢失元信息 | 2 | 32、33 | 夹具重新包装连接时明确传入 `metadata=None`，会话恢复初始化访问 `format_version` 时失败。 |
| 日志子串断言过时 | 2 | 36、39 | 日志新增 `attempt` 字段，测试仍查找要求旧字段紧邻的连续子串。真实旧组回收和新 run 启动顺序正确。 |
| 替身重复记录偏移 | 1 | 16 | 子类与父类各记录同一次 `open_read`，得到 `[0,0]`；真实数据库只有一次 READ。 |
| 生产维护资格发现缺口 | 1 | 19 | 绑定取回结束后，没有重新发现来源检查输入取得的清理资格。本轮额外 run 才完成清理，原缺口仍在。 |
| 生产 READ 归属分类缺失 | 1 | 20 | 恢复入口只关联 `recording_processing`，排除了合法的 inspection 副本，抛出 `state_db_error`。 |
| 历史底层异常未决 | 1 | 30 | 原运行在 START 意图提交后发生 `state_db_error`，未保存底层异常链；本轮另暴露控制计时字段的旧断言。 |
| 合计 | **44** | 1—44 | 测试／环境首因 41 项，生产缺口 2 项，历史精确原因未决 1 项。 |

## 测试与环境问题的证据和处理建议

### 启动窗口：准备耗时进入了业务有效期

16 项中，12 项已有此前同身份通过证据；本轮仍失败的是 ID18、26、29、40。数据库中的过期原因、真实 START 次数及来源／取回状态见[时钟组证据](verification-data/2026-10-25-integration-failure-diagnosis/groups/clock/findings.json)和[进程组证据](verification-data/2026-10-25-integration-failure-diagnosis/groups/process/findings.json)。

- ID17 在真实网页预设、重开、应用和导出上耗时 93.153 秒，但排期仍取准备开始时刻，窗口为 60 秒；受理时已晚于截止约 34.274 秒。浏览器步骤成功，拍摄没有 START。
- ID18 三种拍摄共用一台相机和同一 5 秒窗口。照片已经成功，后两项已在窗口内保存观察并进入 RUNNING，但尚未派发 START 就保存 `window_exhausted`。本轮仍是这一结果。
- ID26 两份并发提交及重复身份核对已完成，但两份照片都 `window_missed`。ID29 三种来源均 `window_missed`，后续取回的 `no_outputs`／错误 21 是次生结果。
- ID40 历史准备过程中耗尽 5 秒窗口；本轮首次可信 run 读数已比截止晚约 1 秒。操作、产物和清理记录均为空，因此没有进入预期的 DELETE 提交未知与取消分支。

这些成功／恢复场景应在测试装配层显式采用已有受控业务钟：从同一世界的业务 UTC 生成排期，准备完成后冻结在原排期，取得原契约要求的真实 START 或既定在途门闩后，再从同一对 UTC／单调读数推进。保留原窗口和真实调用，父进程及故障门仍按真实单调钟超时。专门验证过期、未来等待和驱动真实等待的测试另行保留。

验收必须继续到原目标分支：ID18 的三种来源、两份交付及指定清理；ID26 的并发身份与实际拍摄；ID29 的三来源和选择性取回；ID40 的真实 DELETE 尝试、提交未知、取消以及原请求结果。轮询应同时观察会话错误与不相容终态，避免成功已不可能后仍等待 60—180 秒。不能仅把最终过期断言改为接受失败。

### OSMO：恢复前置事实由错误的替身响应生成

历史 `Channel.run()` 对批量元数据脚本返回 `b"50\n"`，生产解码器要求 `CAMCTL-METADATA/1` 或 `/2` 帧。原数据库实际保存 `adb_metadata_failed / invalid_response_frame / invalid_header`。所谓部分页已带错误，恢复保存 FAILED 符合实际事实。6 个 READ 场景尚未完成来源准备，更未进入缺失、错配、边界、上界、授权及提交未知的目标分支。

本轮目录扫描改为 `run_stream()` 后，替身无条件交付 `b"part"` 并返回退出码 7 和 `source interrupted`，首错前移为 `adb_directory_failed / call_failed`。ID13 的合法新 RESULTS 目录票据还会触发替身的“原 READ 不能重开源流”断言。ID15 在关闭数据库前已保存失败结论；恢复沿用同一结论、且没有新增设备调用，不能要求它将已有失败改成成功。

证据见[11 项逐例记录](verification-data/2026-10-25-integration-failure-diagnosis/groups/osmo/findings.json)、[数据库摘录](verification-data/2026-10-25-integration-failure-diagnosis/groups/osmo/db-evidence.json)和[独立协议探针](verification-data/2026-10-25-integration-failure-diagnosis/groups/osmo/probe.json)。这 11 项在优化前计时基线已经失败。

修复应先让共享替身区分目录流、批量／单路径元数据与源 READ，按真实帧和对应票据返回结果，再建立无错误的 partial／terminal 前置事实。当前目录流是完整扫描快照；需要测试分页时，应真实跨过当前页边界或使用明确的已保存结果页接缝，并保留真实页面生产端口的组合见证。随后重跑各原身份，核对原票据与可靠进度、不重放旧调用、拒绝场景不变、原预算不重置。ID15 应先证明保存的是成功结论，再验证重开后唯一结论与原集合原样发布。修正前置夹具后是否还有下一层业务问题尚未验证。

### 浏览器：构建准备改变了同槽后续任务的依赖

`package-lock.json` 锁定 Playwright 1.63.0，对应 Chromium 1243；`pnpm-lock.yaml` 锁定 1.64.1，对应 1248。依赖准备提供了前者及 1243 浏览器。但能力测试的 `client_build` 执行 `pnpm run build` 时，日志明确显示构建前自动安装 153 个包并切换到 1.64.1；虽然随后有 `ERR_PNPM_IGNORED_BUILDS` 警告，构建仍继续并退出 0。

历史和本轮均保存了该自动安装过程。本轮 task005 改写 worker1 的依赖，task012 随后复用 worker1；ID21、41、43、44 都在 Chromium 启动时缺少 1248。ID21 的 108 个已注册 Vitest 用例未执行，后续 `undefined.close` 是清理阶段的次生噪声。ID42 在 worker0 保持 1.63.0／1243，本轮完成全部 56 项参数比较及安装版受理，结果通过。

原始证据见[依赖变更时间线](verification-data/2026-10-25-integration-failure-diagnosis/groups/browser/dependency-timeline.txt)、[构建命令收据](verification-data/2026-10-25-integration-failure-diagnosis/current/browser-build-command.json)与[五项逐例记录](verification-data/2026-10-25-integration-failure-diagnosis/groups/browser/findings.json)。这说明失败与任务安排和之前执行的构建有关，不宜只笼统归为缓存缺失。

后续应统一权威锁文件及其配套浏览器，让网页构建消费已经准备的依赖，并核验构建前后实际解析身份。验收按“能力网页构建在先、Hasselblad 和两种延时参数在后”的顺序执行，确认同槽后续任务依赖保持一致；保留全部网页、三端校验、安装受理、重载与重启断言。现有四项浏览器未启动的结果不能证明其网页行为通过。

### 其余局部夹具与断言

| ID | 证据与责任边界 | 修复建议及验收 |
| --- | --- | --- |
| 1、2 | `assert format_version == 2` 与权威登记漂移。历史安装枚举及库为 V3，本轮为 V5；发行物资源与对应源码逐字节一致，后续拍摄尚未执行。 | 从被测安装发行物的权威登记取得当前格式，单独保留最低格式资格断言。完整执行 raw/jpeg 的拍摄、两种来源及离线重复取回；保留旧库不隐式升级契约。 |
| 3、4 | 原 `hb_cli` stderr 为 `ModuleNotFoundError: No module named 'camctl'`；shebang 指向测试解释器。 | 此前已让工具使用 `installed_python`，并有 `4728a4cf` 同身份 raw/jpeg 完整通过记录。本轮无需据此修改生产导入或重复安装。 |
| 16 | 子类记录偏移后调用也会记录偏移的父类，单次调用被记录两遍。原库只有一次 READ、可靠进度 4/10 字节。失败发生在数据库关闭重开之前。 | 测试侧只保留一次观察记录；完整执行重开、原 copy 续传、保存预算解码、提交未知核实及无重复源读取断言，才能声称冷恢复通过。 |
| 32、33 | `_owned(environment)` 把真实有效连接重新包装为 `metadata=None`；`_initialize_recovery()` 按格式决定是否读取 `software_recoveries`，访问其 `format_version` 时失败。 | 保留真实元信息或使用符合连接契约的夹具；两型号完整核对打开会话复位设置、目录只读操作不复位及原调用顺序。 |
| 36、39 | 原连续子串为 `call=3 command=run operation=spawn`；实际为 `call=3 attempt=3 command=run operation=spawn`。日志和进程跟踪均证明旧组先回收、新 run 后启动。 | 按字段边界与 PID 识别日志，保留先后关系、退出码 0/7 和真实 SIGKILL／扫描门。另核对后续同类断言；不能删除回收先于启动的约束。 |

前四组的数据库、资源与源码对照见[组件逐例证据](verification-data/2026-10-25-integration-failure-diagnosis/groups/components/findings.json)；进程两项见[进程逐例证据](verification-data/2026-10-25-integration-failure-diagnosis/groups/process/findings.json)。进程测试还存在输入计划缺少 `created_at` 的独立装配观察：submit 保存了输入诊断，`needs_run=1` 合法产生，但不能把它描述为已经接纳业务计划；这不是本次 `StopIteration` 的首因。

## 两项 Action6 生产缺口

直接责任文件在优化前 `ea52a0d0`、历史 `48d2ee64` 和本轮 `1973dc05` 中字节相同：`bootstrap/work_file_assembly.py`、`capture/media_flow.py`、`capture/segment_inspections.py`、`outputs/work_files.py`。清理用途判定所在的 `persistence/repositories/outputs.py` 另有读取机会资格变化，但不改变本次遗漏的等待者关联发现及 READ 归属查询。因此这两处属于原有实现缺口。字节对照、四个测试世界的查询、生命周期事件与日志见[Action6 证据](verification-data/2026-10-25-integration-failure-diagnosis/groups/action6/findings.json)。

### ID19：绑定取回结束后漏掉来源输入的首次清理机会

历史来源动作和两个绑定取回均成功，四份独立交付及实际客户端导入已通过；三个检查输入仍为 `REQUIRED/NOT_NEEDED`，实际文件还在，清理等待 180 秒后超时。全部 READ 已结束，读取机会也已释放。

生产实现存在以下合法调度缺口：维护器先消费来源终态时，绑定取回尚未结束，输入未加入首次处理集合；等取回终态时，`_discover()` 只按该取回自己的 `owner_action_id` 查找文件，未沿来源依赖重新发现来源检查输入。输入在本次 run 中新建，也不属于运行开始时固定的历史扫描范围。只读探针已确认这种调度下的遗漏；历史那次运行的实际内存游标没有保存。现有用途判定还需逐副本区分，范围外、不能复用的副本不应被笼统视为仍有相同用途。

本轮该测试虽然通过，原因是 submit 返回 `needs_run=1`，host 在 run1 之后额外启动 run5，三个文件的释放／清理事件都发生在 run5 的读钟区间。历史 submit 为 `needs_run=0`，没有后续 run。当前通过没有关闭“本次运行新取得清理资格却未发现”的缺口。

后续生产修复应单独建立实施计划。在现有发现边界沿绑定等待者与来源关联发现新资格，或可靠保留尚不能处理的责任；保持来源提交时固定的取回集合、真实操作收场、历史预算及每文件每 run 一次尝试。确定性交错验收应让维护器先消费来源终态，再结束最后一个绑定取回；需要的输入此前必须保留，取得资格后应在同一 run 清理，不依赖新 submit。分别覆盖失败／取消的绑定等待者、后到取回、仍有实际读取、无复用用途副本和清理失败留责。

证据限制是历史没有保存维护器内存中的发现游标，无法指认消费某事件的精确时刻；状态、顺序和静态发现链已足以定位遗漏的关联边界，修复仍需确定性反例验收。

### ID20：合法 inspection READ End 被错误地要求具有 processing 归属

原生 `video_jpeg` 缺 JPEG 的 `capture_failed/invalid_outputs` 是用例预期业务结果，已保留的视频前缀仍允许取回。意外错误是 run1 退出 1 并报告 `state_db_error`，详情为“原内部读取结束缺少处理、源观察者与动作归属”；本轮同点重现。

`resume_binding_internal_read_ends()` 处理已持有的读取结束事实时，通过 `JOIN recording_processing p ON p.id=c.processing_id` 找动作。但合法检查副本有 `inspection_id`、`processing_id=NULL`，该关联必然没有结果，于是在尚未检查原 READ、取消或绑定分类前抛出一致性错误。普通调度和残留恢复都会进入此入口，不只发生在进程重启。

后续 run5 完成来源失败发布及六份独立交付，说明数据仍可继续处理；它不能消除 run1 的错误。三个检查输入最终仍未清理，另命中 ID19 的维护问题。日志未保存当时触发的具体 copy ID，不能从三个候选中猜选一份。

后续应按真实唯一归属分派共享结束事实。可以扩展公共分类，也可以保留该函数的 processing 专属职责，由入口将 inspection 交回其原拥有者；具体函数划分属于实现选择。inspection 仍须沿原 copy、ticket、检查动作、成员、设备源和观察者核对，再续接原本地核验及保存；错误动作、成员、观察者或票据继续拒绝。确定性验收应暂停在真实干净 ReadEnd 已持有而本地结束尚未保存的窗口，再分别进入普通／残留入口；保留原身份、预算、取消和提交未知核实，不重开 READ。与 ID19 的清理验收组合后，要求同一 run 完成六份交付和三个输入的首次清理，且无意外会话错误；后续继续核对客户端导入及累计 ACK，不要求原 run 等待后续确认输入。此次没有实施该修复。

## 设计契约与测试前提复核

2026-10-25 对两项归因分别进行独立复核，先查阅正式规格和生命周期规则，再检查测试输入、生产者与消费者，最后用原数据库执行只读分支探针。结论是：**两项均有明确的设计依据，属于生产实现缺口；没有发现能将其解释为测试前提非法或断言过强的证据。** 本次没有新增 pytest 运行或 CLI `run`，没有修改生产代码、测试、配置或原测试数据库。环境为 Python 3.11.17、SQLite 3.53.1。

### ID19：清理责任与运行预算均支持原断言

[分段公共规格](../superpowers/specs/2026-10-23-video-result-modes-and-segmentation-design.md#检查副本与等待取回的复用)明确规定：来源可靠提交、相关绑定取回结束且实际读写和工具收场后，清理检查副本并保存结果；无绑定消费者的完整副本以及原检查已经结束的部分副本，也应在操作收场后清理。[运行预算](../architecture/file-handoff.md#中间文件清理的运行预算)进一步要求，本次运行中新形成的可清理责任，由业务流程及时进行首次清理，不占历史额度。

这一要求受“每文件每次 run 最多一次实际检查和清理尝试”约束，但本案的早期资格初筛不构成该尝试：`_add_owned_files()` 只读取事实，资格未成立时没有进入实际维护，也没有记录 `processed` 或清理继续位置。新输入亦在启动后创建，不属于固定历史范围。因此，历史额度耗尽、已经实际检查过或仍有 worker 活动，都不能解释历史运行正常收场后的三份残留。测试的 180 秒只是观察上限，不是从设计中推导出的清理时限。

[清理探针结果](verification-data/2026-10-25-integration-failure-diagnosis/contracts/cleanup-probe.json)以原库真实发布截点 H=174 为已消费游标，显式建立“当时等待者未结束、尚未实际清理、历史上界为 0”这一合法调度条件。真实 `_discover()` 扫描后续事件直到 263，首次集合仍为空，`processed={}`；同一原库的真实用途分类却认定 files 1、2、3 均可删除、操作已收场。显式按来源动作重新发现后，三份文件立即进入首次集合。这证明遗漏的是等待者终态到来源输入的关联发现。它没有还原历史进程的实际游标。

还应按副本分别核对用途：第三份输入的检查结果为 `UNCONFIRMED`，不在默认范围，也未被四份绑定交付使用。原检查结束、来源可靠提交且实际操作收场后，它不应等待无关消费者。本项修复需同时检查逐副本用途判断，不能只补一次全局扫描。详细依据、原世界事实与源码对照见[清理复核记录](verification-data/2026-10-25-integration-failure-diagnosis/contracts/cleanup-audit.json)。

### ID20：检查副本及其实际 READ 结束事实都合法

[文件拷贝设计](database/outputs-files.md#文件拷贝记录)明确列出逐段检查的独立 `inspection_id` 归属，并要求它不与 `recording_processing` 共用可变输入槽；[分段存储设计](database/segments.md)及实施计划 V6 亦明确这一身份。[SQL 结构](database/schema/files.sql)和独立 `_copy_guard` 都按 delivery、processing、inspection 恰一个非空核验。`processing_id=NULL` 因而是合法生产状态，由 `segment_inspections._copy_candidate()` 正式创建。

[实际读取结束规则](../architecture/file-copy.md)要求拥有者先保留干净 ReadEnd，再完成本地完整性核验及原结果保存。检查流程确实通过生产 `hold_read_end()` 持有该事实，并保留自己的续接回调。随后主机 hash 和源摘要查询会异步让出执行，普通／残留事实入口可以在这个窗口合法看到共享集合；并不要求先发生重启或绑定失效。消费者却在状态和绑定判断之前，用只支持 processing 的关联解释所有成员，误将 inspection 判为状态库损坏。

[读取探针结果](verification-data/2026-10-25-integration-failure-diagnosis/contracts/read-probe.json)核对原库三份检查副本的 member、source、action、observer 和 ticket 关系，外键检查无异常。再用生产函数按真实身份构造 HeldReadEnd，分别调用原消费者，三份都在绑定检查前得到同一 `ConsistencyError`；绑定检查调用数和数据库改动数均为 0。探针使用的是最终已终态的数据库和新构造的内存标记，只证明无条件查询排除这一分支，不能当作原运行交错重放。原会话错误由原日志独立证明，合法交错的可达性由生产调用链独立证明；原触发 copy 仍未知。完整核验与修复边界见[读取复核记录](verification-data/2026-10-25-integration-failure-diagnosis/contracts/read-audit.json)。

### 测试装配、文档差异与源码范围

两个原失败身份都未启用仓储观察包装器或读取门闩：普通 `unknown-prefix` 的 boundary 为 `none`，native 缺照片用例的 `observer_stage` 保持默认 `None`。两者通过真实安装版 CLI、C host、客户端计划和 SQLite 建立业务事实，没有直接注入来源、副本、取回或清理行。业务钟在装配边界推进，父进程和 host 仍使用真实超时；设备替身处理 ADB 进程边界并读取真实文件。普通用例有意令第三次媒体工具失败以形成未确认覆盖；native 用例三次均调用真实 ffprobe 成功，未伪造帧数。缺少必需 JPEG 时来源失败、已发布视频仍可取回，符合[拍摄产物与失败规则](../architecture/camera-capture.md)及分段规格。这些测试验证软件协作，不承担真实相机画质和固件行为验收。

发现一处需要同步的文档差异：[字段页](database/file-fields.md#文件拷贝)第 150、174 行仍只描述 delivery／processing 两类，遗漏 inspection。正式分段规格、专门存储设计、V6 计划、SQL 和独立守卫对三类归属一致，因此将其登记为字段说明漏同步；不能用这段旧说明把正式检查流程产生的记录判为非法。后续修复应同步该页，并保持唯一归属模型；本次只记录差异。

本次还将固定诊断源 `1973dc05` 与 `implement_camctl` 的 `a543720ceb427312125a329b4323f404f8fb32c6` 比对：维护发现、READ 消费者、检查生产者、清理实现、仓储文件、原测试及相关契约页在所列范围内均无差异。报告工作树 `100738f7` 的仓储文件整体有其他资格变化，但本案用途分类及候选函数内容相同；没有把整文件误记为一致。具体文件摘要、契约摘录、已确认范围和未知事项统一保存在[复核来源记录](verification-data/2026-10-25-integration-failure-diagnosis/contracts/source-contract-references.json)。后续实施仍需分别建立确定性交错反例；本次复核不构成修复后通过或全量回归结论。

## ID30：历史异常仍有证据缺口，当前另有确定的断言错误

原 `test_excess_recording_repairs_without_probing` 已提交窗口内观察、RUNNING 和一次 START 意图，意图只比 `window_end` 早 180 微秒。之后 CLI 保存 `state_db_error`，没有 START 结果、`sent_at` 或 `started_at`，动作仍为 RUNNING；测试等不到 `started_at`，60 秒后超时。不能把这次状态写成已经 EXPIRED，也不能仅据时间接近就认定某个过期事务存在缺陷。

原日志缺少底层异常链。在原库完整副本中，候选的“派发前窗口结束 → NOT_DISPATCHED 与动作过期共同保存”路径可以提交；这只排除了该输入必然失败的猜测。一次带异常链钩子的历史窄复现又先发生 `window_missed`，根本没有进入原意图后的分支，没有取得新异常链。因此，**历史 `state_db_error` 的精确触发点仍未查明**。

本轮固定 `1973dc05` 的相同测试则实际完成 START、中断、恢复、控制计时多录修复和取回，两个动作均成功，随后测试在第 353 行读取 `basis["actual_duration_s"]` 抛出 KeyError。真实 repair basis 为：

```json
{"reason":2,"target_duration_ms":3000,"threshold_s":3.5,"control_elapsed_ns":8434007000}
```

控制计时依据使用 `control_elapsed_ns`；`actual_duration_s` 用于媒体探测得到的时长。当前可确认的测试修复是按纳秒精确换算并比较原门槛，保留这两类事实的语义区别，随后继续修复成品、编码、交付及报告断言。历史异常另保留为未闭合项；以后重现时必须保存完整底层异常链和本次进程输出，不能用修正字段后的通过覆盖它。

相关证据见[ID30 逐例记录](verification-data/2026-10-25-integration-failure-diagnosis/groups/clock/findings.json)、[当前原始失败输出](verification-data/2026-10-25-integration-failure-diagnosis/current/tasks/006-root-test_camctl_media_roundtrip/output.log)、[历史窄复现收据](verification-data/2026-10-25-integration-failure-diagnosis/groups/clock/id30-once/receipt.json)及其诊断差异与原始输出。

## 后续安排建议

1. 先处理依赖一致性和共享替身，分别复核 5 个浏览器身份与 11 个 OSMO 身份，确保真正进入目标分支。
2. 在测试侧处理格式断言、连接夹具、偏移观察、日志匹配、剩余启动窗口及 ID30 当前字段断言；完整保留原业务验收。已通过的 14 项不重复计为本轮新增成果。
3. 两项 Action6 生产修复单独立项，先以确定性交错建立反例，再按已确认契约实施和复核；与测试耗时优化分开授权、提交和验证。
4. ID30 历史异常保持未闭合状态，补充异常可观察性后按原阶段追踪。窄验收闭合后，再安排一次统一源码和依赖的完整回归，取得可声明全量通过的证据。

本次没有删除测试、放宽超时或运行窗口，也没有把尚未执行的恢复、清理或浏览器行为登记为通过。

## 44 项逐项对照

下表由逐项清单生成，不单独维护；身份省略目录前缀。完整 nodeid、每例根因、已执行／未执行阶段与验收条件以[逐项清单](verification-data/2026-10-25-integration-failure-diagnosis/case-ledger.json)及其引用的分组证据为准。

| ID | 测试身份（目录前缀见逐项清单） | 历史首因分类 | 后续证据 |
| --- | --- | --- | --- |
| 1 | `test_distribution.py::test_installed_hasselblad_photo_pair_obtain_and_source_reuse[raw]` | 硬编码格式断言过时 | 本轮失败：同类首因 |
| 2 | `test_distribution.py::test_installed_hasselblad_photo_pair_obtain_and_source_reuse[jpeg]` | 硬编码格式断言过时 | 本轮失败：同类首因 |
| 3 | `test_hasselblad_device_distribution.py::test_installed_device_photo_reclaims_then_downloads_both_sources_twice[raw]` | 测试工具解释器绑定错误 | 此前定向补测通过，本轮未重跑 |
| 4 | `test_hasselblad_device_distribution.py::test_installed_device_photo_reclaims_then_downloads_both_sources_twice[jpeg]` | 测试工具解释器绑定错误 | 此前定向补测通过，本轮未重跑 |
| 5 | `test_osmo_shared_recovery.py::test_original_result_pages_are_consumed_before_new_listing[partial]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 6 | `test_osmo_shared_recovery.py::test_original_result_pages_are_consumed_before_new_listing[terminal]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 7 | `test_osmo_shared_recovery.py::test_unavailable_original_read_keeps_progress_and_uses_own_consumer[missing-False]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 8 | `test_osmo_shared_recovery.py::test_unavailable_original_read_keeps_progress_and_uses_own_consumer[mismatch-False]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 9 | `test_osmo_shared_recovery.py::test_original_recovery_requires_boundary_horizon_and_authorization[boundary-UNCONFIRMED_BOUNDARY-read]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 10 | `test_osmo_shared_recovery.py::test_original_recovery_requires_boundary_horizon_and_authorization[horizon-MISSING_HORIZON-read]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 11 | `test_osmo_shared_recovery.py::test_original_recovery_requires_boundary_horizon_and_authorization[authorization-EVIDENCE_UNAVAILABLE-read]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 12 | `test_osmo_shared_recovery.py::test_formal_read_recovery_reuses_original_finish_after_real_unknown_commit` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 13 | `test_osmo_shared_recovery.py::test_new_result_round_has_new_ticket_and_keeps_original_budget_and_pages[none]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 14 | `test_osmo_shared_recovery.py::test_new_result_round_has_new_ticket_and_keeps_original_budget_and_pages[partial]` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 15 | `test_osmo_video_mode_completion.py::test_osmo_native_segment_resume_publishes_saved_conclusion_without_device_calls` | OSMO替身接口漂移 | 本轮失败：首错已前移 |
| 16 | `test_action6_budget_cold_recovery.py::test_action6_cold_recovery_decodes_saved_budget_and_continues_same_copy` | 替身重复记录偏移 | 本轮失败：同类首因 |
| 17 | `test_hasselblad_photo_roundtrip.py::test_installed_hasselblad_photo_roundtrip[jpeg-device-0.000333]` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 18 | `test_camctl_c_module_roundtrip.py::test_capture_obtain_cleanup_roundtrip_via_c_module` | 窗口被测试准备耗尽 | 本轮失败：同类首因 |
| 19 | `test_action6_segmented_recovery_roundtrip.py::test_installed_action6_ordinary_recovery_keeps_scope_waiters_and_copy_ownership[unknown-prefix]` | 生产：清理资格发现缺口 | 本轮通过，清理仍依赖额外run |
| 20 | `test_action6_segmented_recovery_roundtrip.py::test_installed_action6_native_required_photo_failure_keeps_reached_video_scope_and_waiters` | 生产：inspection READ归属分类缺失 | 本轮失败：同类首因 |
| 21 | `test_device_capability_declarations.py::test_installed_metadata_reaches_parameter_form_and_device_guide` | Playwright与浏览器不匹配 | 本轮失败：同类首因 |
| 22 | `test_video_size_estimate.py::test_record_estimate_stays_out_of_execution_and_report` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 23 | `test_camctl_output_roundtrip.py::test_cross_plan_reference_and_multi_request` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 24 | `test_camctl_output_roundtrip.py::test_recording_delivery_survives_late_cancel` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 25 | `test_camctl_session_recovery.py::test_report_failure_preserves_device_work_and_redelivers` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 26 | `test_camctl_session_recovery.py::test_concurrent_submits_and_duplicate_identity` | 窗口被测试准备耗尽 | 本轮失败：同类首因 |
| 27 | `test_camctl_output_roundtrip.py::test_repeated_obtain_of_same_output_yields_independent_deliveries` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 28 | `test_camctl_output_roundtrip.py::test_auto_preview_obtains_registered_preview` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 29 | `test_camctl_output_roundtrip.py::test_three_capture_kinds_partial_obtain` | 窗口被测试准备耗尽 | 本轮失败：同类首因 |
| 30 | `test_camctl_media_roundtrip.py::test_excess_recording_repairs_without_probing` | 历史异常未决；当前字段断言过时 | 本轮失败：现为字段KeyError |
| 31 | `test_camctl_media_roundtrip.py::test_insufficient_timing_checks_repairs_and_delivers` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 32 | `test_adb_camera_registration.py::test_real_session_open_resets_settings_but_catalog_reads_do_not[dji-action6]` | 夹具丢失连接元信息 | 本轮失败：同类首因 |
| 33 | `test_adb_camera_registration.py::test_real_session_open_resets_settings_but_catalog_reads_do_not[dji-osmo360-ii]` | 夹具丢失连接元信息 | 本轮失败：同类首因 |
| 34 | `test_camctl_capture_roundtrip.py::test_timelapse_recovers_remaining_wait` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 35 | `test_camctl_session_recovery.py::test_process_restart_resumes_interrupted_work` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 36 | `test_camctl_process_recovery.py::test_next_run_waits_for_old_group_settlement[0]` | 日志子串断言过时 | 本轮失败：同类首因 |
| 37 | `test_camctl_session_recovery.py::test_start_call_interruption_settles_unconfirmed` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 38 | `test_camctl_c_module_roundtrip.py::test_auto_preview_roundtrip_via_c_module` | 窗口被测试准备耗尽 | 此前定向补测通过，本轮未重跑 |
| 39 | `test_camctl_process_recovery.py::test_next_run_waits_for_old_group_settlement[7]` | 日志子串断言过时 | 本轮失败：同类首因 |
| 40 | `test_camctl_cancellation_roundtrip.py::test_cleanup_unknown_preserves_original_request_result` | 窗口被测试准备耗尽 | 本轮失败：同类首因 |
| 41 | `test_device_capability_declarations.py::test_installed_sources_control_browser_cleanup_across_reload_and_restart` | Playwright与浏览器不匹配 | 本轮失败：同类首因 |
| 42 | `test_hasselblad_photo_parameters.py::test_installed_hasselblad_parameter_validation_agrees_across_consumers` | Playwright与浏览器不匹配 | 本轮通过，依赖匹配 |
| 43 | `test_timelapse_time_parameters.py::test_describe_python_browser_and_node_export_agree[dji-action6]` | Playwright与浏览器不匹配 | 本轮失败：同类首因 |
| 44 | `test_timelapse_time_parameters.py::test_describe_python_browser_and_node_export_agree[dji-osmo360-ii]` | Playwright与浏览器不匹配 | 本轮失败：同类首因 |

## 证据归档

[原始文件归档清单](verification-data/2026-10-25-integration-failure-diagnosis/archive-manifest.json)记录来源路径、归档路径、字节数与 SHA-256；[源码片段](verification-data/2026-10-25-integration-failure-diagnosis/source-excerpts.json)保留所用行号与完整文件摘要。来源路径只作溯源标识，阅读本报告不依赖临时目录存活。数据库保留结构化只读摘录，未把整个测试数据库或设备媒体加入仓库。

完整回归的全量身份与计时仍以[原优化报告](integration-test-optimization-2026-10-25.md)及其既有 CSV／收据为权威来源；本目录只补充本次 44 项失败的原因和定向复测证据。
