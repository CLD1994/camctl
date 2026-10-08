# camctl host 主程序通知实施计划

> 供执行 Agent 使用：使用 `superpowers:executing-plans` 按任务实施。复选框记录实际实现与验证；公开回调及通知契约必须保持，新增内部文件、类型、容量初值和任务拆分均为实施建议。

**目标：** 让主程序注册只接收 `int position` 的电机回调，host 从每次 run 的独立管道接收 NDJSON，完成有界解析、排队与异步调用，同时维持原有提交及进程管理。

**组织建议：** 在进程工作线程中读取通知，由独立解析器和有界 FIFO（先进先出队列）保存已解析值；专用回调线程执行主程序函数。进程回收与通知排空分别跟踪，所有 run 共用一条回调队列。

**技术基础：** C11、POSIX pthread／pipe／poll／posix_spawn、现有 yyjson、cmocka、CMake 与 CTest；目标为 arm64 Ubuntu 18.04。不新增主程序的 JSON 依赖或业务确认协议。

**规格：** [通知协议](../../../protocol/host-notifications.md)、[回调注册](../../host-demo/implementation.md#按消息类型注册回调)、[主程序接入约定](../../host-demo/design.md)、[电机动作](../../architecture/motor-control.md)。

[实施路线图](2026-09-30-camctl-implementation-roadmap.md#电机控制与主程序通知接入) · [camctl 配套计划](2026-10-08-camctl-motor-control.md)

## 范围与实施起点

截至 2026-10-08，`apps/host-demo/include/camctl_host.h` 已提供单参数电机回调注册接口。注册、解析、独立通知管道、回调线程及第三方交付已有软件实现与组件验证；真实 camctl 组合由 HN5 与 M6 共同记录。

| 现有文件，均相对 `apps/host-demo/` | 当前责任与新增边界 |
| --- | --- |
| `src/host.c` | 模块实例、初始化清理、进程工作循环与最多一个 run／submit；接入注册状态、回调线程和通知读事件。 |
| `src/process.c`、`src/process.h` | `host_child_spawn`、`host_child_collect`、`host_child_done` 管理启动、stdout/stderr、原组收场与回收；新增通知 fd 和独立排空状态。 |
| `src/result.c` | yyjson 解析最终 stdout 会话结果；保留此责任，通知解析不复用“命令结束后一次解析”的流程。 |
| `src/scheduler.c`、`src/input.c` | 调用位置、待启动要求、提交顺序和共享重试预算；通知积压不能放行第二个 run，也不能阻止 submit。 |
| `src/config.c`、`include/camctl_host.h` | 配置与公开默认值；加入必要的有界资源配置并保持旧调用方使用默认宏的方式。 |
| `src/demo.c`、`CMakeLists.txt`、`cmake/package-source.cmake` | 演示、静态库、源码包与安装资料；同步新的接入示例及交付测试。 |

预计新增 `src/notification.c/.h`、`src/callback_queue.c/.h`、`src/callbacks.c/.h`，分别负责纯消息解析与行状态、有界队列及注册／分发生命周期。优先复用已有系统适配和日志队列接口，但回调队列与日志队列分开管理。

## 全局约束与两端依赖

- 公开接口固定为 `typedef void (*camctl_host_motor_control_callback)(int position);` 和 `int camctl_host_register_motor_control_callback(camctl_host_motor_control_callback callback);`。不增加上下文、动作 ID 或通用 JSON 参数。
- 注册前后状态与 errno 以[注册表](../../host-demo/implementation.md#按消息类型注册回调)为准；初始化失败保留已注册函数，成功后集合不可改。
- 未注册电机回调时不启用通知通道；已注册时资源必须完整建立，不能静默降级为无法分发的实例。
- 只有 run 继承通知写端。主程序的环境、工作目录、标准流及信号处置保持既有契约；stdout 最终结果继续独立解析。
- host 不读状态库、不发送 ACK、不按动作 ID 去重或重放；每条完整合法消息按接收顺序分发一次。断电丢失内存队列不建立补发责任。
- 队列满只暂停通知读取。回调执行不持有进程管理锁；原组收场和具体 PID 回收不等待回调返回。
- 系统接口替身用于单元测试；真实管道、线程、进程和交付物分别进入组件或根跨组件集成测试。

公共格式、共同夹具及描述符交接由 [M1](2026-10-08-camctl-motor-control.md#m1-公共机器定义与两端接口基线)交付。HN1 完成内部接口与容量细化后，HN2 和 HN3 可分别推进；二者完成后 HN4 组合运行，HN5 与 M6 共用真实双方验收，HN6 完成交付。HN4 不等待 M6 的完成声明，可以使用受契约约束的 CLI 替身验收组件。

## 资源与生命周期验收模型

容量具体值在 HN1 写入公开配置及责任专题前仅为建议。建议单行上限 4096 字节（含 LF）、队列 64 项；读取块固定有界，解析内存按单行上限预分配，每条已解析电机记录只持有值与必要关联信息。计算“行缓冲＋未消费读取块＋解析池＋队列＋一条执行中记录”的总上界，并对配置乘法与相加溢出先检查。默认值仅由公开头文件提供，测试引用同一来源并另验非法配置边界。

| 状态分区 | 必须观察到的结果 | 任务 |
| --- | --- | --- |
| 注册 NULL；初始化前首次非空；重复非空；成功初始化后非空 | 依次 EINVAL、成功、EALREADY、EBUSY；原函数与其他状态不被失败注册覆盖。 | HN1 |
| 已注册且初始化资源建立中失败 | 无运行中的半初始化实例；资源回收、原注册保留，可再次初始化。 | HN1、HN4 |
| 半行；多行；非法完整行；超长行及后续合法行；EOF 残片 | 半行等待，多行顺序处理，非法行拒绝，超长丢弃至 LF 后恢复，EOF 不补换行。 | HN2 |
| 队列已满，读取块中还有数据 | 暂停通知读取并保留有界剩余数据；stdout、stderr、submit 与退出管理继续；恢复后不丢失或重复已接纳消息。 | HN2、HN4 |
| CLI 活着且回调阻塞 | 不持进程管理锁调用回调；其他管理事件继续，后续回调保持 FIFO。 | HN4 |
| CLI 已退出且原组完成收场，通知尚未排空 | 可最终回收具体 PID，但保留原 run 调用位置及通知状态，不另启 run 累积旧管道。 | HN3、HN4 |
| 进程已回收，通知也排空，但队列仍有旧消息 | 依既有调度规则允许下一次 run；使用新行缓冲，旧消息与新消息共用 FIFO，旧消息不被退出码撤销。 | HN4、HN5 |
| 关闭读端、CLI 异常退出、发送只留下前缀 | 已接收完整消息仍分发；残片拒绝，记录阶段与原因；不改写 camctl 结果或触发补发。 | HN3、HN5 |

**重点复核：** C 数值转换与重复字段（HN2）；标准流已关闭时的 fd 重映射（HN3）；队列满时 CLI 退出造成的未读数据丢失（HN3/HN4）；初始化失败后的线程／描述符泄漏（HN1/HN4）；源码包与安装包遗漏新接口或源文件（HN6）。这些风险均须有针对性用例。

## 实施任务

### HN1 注册接口、配置与资源所有权

**预计文件：** `include/camctl_host.h`、`src/host.c`、`src/config.c`、新增 `src/callbacks.c/.h`；`tests/unit/test_callbacks.c`、`tests/integration/test_init_failure.c`；`docs/host-demo/implementation.md`、`apps/host-demo/README.md`。

**输入／输出：** 消费 M1 接口基线；提供精确公开回调签名、只在成功 init 固定的注册集合、容量配置与逐资源清理责任。建议内部 `host_callbacks` 保存注册函数及初始化状态，避免给公开接口增加上下文参数。

- [x] 在注册状态的纯内部边界编写表驱动单元测试，覆盖 NULL 在各状态的优先级、首次注册、重复、init 失败保留与成功后拒绝。先运行 CTest unit，确认目标行为尚缺失。
- [x] 实现注册与公开声明；注册本身不创建线程、管道或调用函数。配置缺省、合法最小值、零、超限及容量计算溢出分别验证，依据上文总内存计算确定实际上限并登记责任专题。
- [x] 明确管道资源分配时机：初始化准备首个 run 的通知资源，后续每次 run 使用新的管道；每次 spawn 前的建立失败走已有明确未启动分类。未启用通知的初始化不创建该功能资源。注册信息独立于可回滚的实例资源。
- [x] 扩展真实初始化故障注入：分配、管道、互斥量／条件变量、回调线程和进程线程任一步失败均回收已取得资源，未发布实例不能调用主程序。重试初始化只创建一份有效实例且原注册仍可用。
- [x] 执行注册单元与 `integration_init_failure`，审计初始化失败标签及 errno 保留；建议提交注册与配置接入。

### HN2 NDJSON 分段、严格解析与队列值

**预计文件：** 新增 `src/notification.c/.h`、`src/callback_queue.c/.h`；`tests/unit/test_notification.c`、`tests/unit/test_callback_queue.c`，CMake 单元目标与共同夹具适配。

**接口建议：** `host_notification_parse(data, length, out)` 只解析完整行并返回有限错误分类；`host_notification_feed(state, data, length, sink)` 返回已消费字节数及继续／队列满状态。`host_notification` 是按消息类型约束的值记录，电机分支保存 `int position`；JSON 文档释放后队列仍拥有全部所需数据。

- [x] 编写失败测试：所有分割位置、多行、空行、重复字段（含转义后重名）、多余字段、未知类型、非对象、错误 ID、非法 UTF-8／Unicode、嵌入 NUL、INT_MIN／INT_MAX、越界、布尔与非整数；消费 M1 的共同夹具，数值不得经浮点近似再转 int。另验类型合法但未注册时只记录诊断，以及两条相同 action ID 的合法行仍各自分发，不能偷偷加入 host 去重。
- [x] 验证 `unit_notification` 与 `unit_callback_queue` 可以证伪精确整数和满队列保护，记录实际先失败或可恢复变异证据。核对仓库内 yyjson 对严格读取、整数／原始数值、重复成员枚举、固定内存池的能力；复用解析器，只实现消息字段校验和精确业务适配，不自行实现通用 JSON 解析器。
- [x] 实现长度感知的字段比较与完整结构校验；规范消息直接检查整数类型及范围，其他数字写法严格按 M1 从协议导出的共同夹具处理，不能用 double 舍入判断合法性。共同夹具必须明确 `1.0`、`1e2`、`1.0000000000000000001` 的输入受理结果和通知接收结果；协议没有覆盖的分区须先补齐责任专题，不能凭 yyjson 默认行为决定。
- [x] 实现有界行状态与值队列；超长整行进入丢弃至 LF 状态，EOF 残片拒绝。队列满时保留未消费字节，不丢弃已经接纳的记录，不重复解析并再次入队同一条消息。
- [x] 运行 `unit_notification`、`unit_callback_queue`；补充容量 N−1／N／N+1、超长后多条合法消息、坏行夹在好行间、JSON 缓冲释放后值仍正确的断言。审阅所有错误分类与内存上界，建议提交接收核心。

### HN3 每次 run 的独立管道与进程状态

**预计文件：** `src/process.c/.h`、`src/host.c`、`tests/unit/test_spawn.c`、`test_process_unit.c`、`tests/integration/test_process.c`、`fake_cli.c`、`test_closed_stdio.c`。

**接口建议：** 扩展 `host_child` 保存通知 fd 与排空状态；`host_child_spawn` 接收已准备的通知资源及 M1 定义的命令参数。`host_child_collect` 按通知剩余容量读取；具体 PID 回收条件仍由原组收场负责，`host_child_done` 另外要求通知处理结束。

- [x] 用系统调用替身先验证：启用 run 只继承本次写端，submit 不继承；pipe／fcntl／spawn 操作失败不泄漏；标准 fd 0/1/2 原来关闭时映射仍正确，关闭动作不能关闭刚映射的通知 fd。
- [x] 运行相关 unit 目标确认新增行为失败，再实现仅子进程生效的描述符交接。父进程写端在 spawn 后关闭，读端不传给子进程，保留全局环境与标准流的既有行为。
- [x] 扩展收集状态：stdout/stderr 与通知采用各自有界处理，通知 EOF 由真实读取决定。不能把 stdout 的“进程已结束且暂时无数据”处理直接套用到暂停读取的通知通道；队列恢复后必须继续处理内核中已到达的完整消息。
- [x] 真实进程测试覆盖不同退出码、EOF 前后、尚有未读数据时最终回收、通知关闭与其他输出独立、下一次 run 的半行状态清空。保留现有进程组、未知回收结果和共享重试预算测试。
- [x] 运行 `integration_process`、`integration_closed_stdio` 和全部进程单元目标，审计所有启动与回收分支；建议提交管道生命周期。

### HN4 回调线程、背压与工作循环组合

**预计文件：** `src/host.c`、`src/callbacks.c`、`src/callback_queue.c`、`src/scheduler.c`；新增 `tests/integration/test_notifications.c`，扩展 `tests/integration/host_driver.c`、`controlled_cli.py`、`test_host.py`。

**输入／输出：** 消费 HN1 注册与资源、HN2 值队列、HN3 通道状态；输出主程序收到的单参数调用及持续可用的进程管理。队列出队后以现有唤醒管道或同等有界机制通知工作线程恢复读取。

- [x] 先建立使用真实线程与受控 CLI 的测试：回调卡在同步点，连续发送填满队列；此时 submit、stdout/stderr 和具体 PID 回收仍能推进。断言下一次 run 在旧通知未排空时不能开始。
- [x] 实现专用回调线程；取出值后释放队列／实例／进程锁再调用主程序。回调返回释放记录并继续 FIFO，不向 CLI 回写。使用固定队列和单个执行中记录限制内存，不为积压创建新线程。
- [x] 接入 poll 可读集合与每轮读取配额，队列满时只移除通知读取兴趣；已读缓冲剩余数据优先继续消费，避免等待永远不会再到来的新 POLLIN。一次回调释放容量后恢复读取且不忙循环。
- [x] 组合 CLI 已回收、原通知排空和队列未空三种独立事实；旧消息继续调用，新 run 取得新通道但沿用共享 FIFO。回调内部调用 `camctl_host_submit` 的集成用例应完成，证明没有持相关锁调用主程序。
- [x] 运行 `integration_notifications`、`integration_host`、`integration_init_failure` 与 CTest unit 全量；测试无回调模式保持原行为，诊断仍异步且失败不会改写 CLI 结果。审核并记录资源上界后，交付 M6 可运行 host，建议提交运行闭环。

### HN5 与真实 camctl 的组合验收

**预计文件：** 新增 `tests/integration/motor_host_driver.c`，与 M6 共用 `tests/integration/test_camctl_motor_notifications.py`；更新 `docs/host-demo/verification.md`。

**前置：** HN4 与 M5 的生产能力。C 驱动只记录调用位置及顺序，提供测试同步点；不解析 JSON、不替代 host 接收，也不模拟电机已到位。

- [x] 给 M6 提供编译后的 C 驱动与清楚的输入／输出约定，以真实公共注册函数接入；覆盖负值、零及两端 int 值，证明传入的只有 position。
- [x] 在真实计划与数据库下分别观察消息写入、回调开始和报告成功；延迟回调后报告仍只表达通知已发送，CLI 异常退出不清除已接纳队列。
- [x] 与 M6 共同执行发送意图和结果提交中断矩阵，验证自动重启不重复回调；这里的“不重复”来自 CLI 不重发，不在 host 新增按 ID 缓存或重放机制。
- [x] 运行根组合命令，记录两端版本、Linux 环境、同步点与结果；客户端导出、导入与累计确认使用真实生产入口。主程序业务范围、真实电机动作和 TX2 性能仍属于部署联调。
- [x] 审核无 ACK、无数据库依赖及内存队列故障边界，和 M6 共用证据后勾选本任务。

### HN6 演示与第三方交付

**预计文件：** `src/demo.c`、`include/camctl_host.h`、`README.md`、`CMakeLists.txt`、`cmake/package-source.cmake`、`tests/integration/test_demo.py`、`test_delivery.py`；`docs/host-demo/README.md`、`implementation.md`、`verification.md`。

**输入／输出：** 消费已通过 HN1—HN5 的公开接口，交付可独立编译的静态库、头文件、源码包及初始化前注册示例。

- [x] 扩展并执行交付测试：从安装头文件编译只含 `void motor(int position)` 的主程序并链接静态库；源码包重新配置和构建时包含全部新源文件。测试必须能识别缺失接口或源文件。
- [x] 在演示中先注册回调再初始化；演示仅记录收到的位置，不把日志写成实际电机执行成功。调用方自行定义单位、业务范围和线程安全，资料明确回调可能排队及成功含义。
- [x] 同步默认配置宏、配置范围、安装说明及包内文档链接。独立消费者不依赖根 protocol、仓库相对头文件或客户端资源才能编译运行。
- [x] 执行 `integration_demo`、`integration_delivery` 和全部 CTest；在 Release 构建复验，断言不因 NDEBUG 消失。记录开发 Linux 软件验证，ARM64 兼容性及真实主程序回收约定另列待核验。
- [x] 沿注册、初始化、spawn、读取、队列、回调、回收与下一次 run 审计同类错误路径，更新组件验证记录并向统筹 Agent 提供路线图登记依据，建议提交交付资料。

## 验证命令与完成条件

从仓库根执行，新增 CTest 名称由对应任务接入 CMake 后使用。先运行目标测试观察失败，再实现和复跑。无需硬件，也不以 C 主程序替身通过宣称实际电机控制通过。

```bash
cmake -S apps/host-demo -B .local/host-demo-build -DHOST_BUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Debug \
  -DHOST_TEST_PYTHON=/absolute/path/to/python3.11
cmake --build .local/host-demo-build
ctest --test-dir .local/host-demo-build -L unit --output-on-failure
ctest --test-dir .local/host-demo-build -R integration_notifications --output-on-failure
ctest --test-dir .local/host-demo-build -L integration --output-on-failure
UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest tests/integration/test_camctl_motor_notifications.py -q
cmake -S apps/host-demo -B .local/host-demo-release -DHOST_BUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Release \
  -DHOST_TEST_PYTHON=/absolute/path/to/python3.11
cmake --build .local/host-demo-release
ctest --test-dir .local/host-demo-release --output-on-failure
```

完成条件是 HN1—HN6 均有可复查证据，公开接口恰好一个 `int position`，所有资源与生命周期分区通过，M6 真实双方组合通过，独立交付可用。客户端业务链与目标部署进度由路线图另行表达；组件复验命令与范围见本计划验证记录；真实组合进度以 HN5／M6 为准。

## 组件验证记录（2026-10-08）

通知数据存储默认上界为 61956 字节，最大合法配置为 938244 字节，固定结构、分配器与线程开销另计；具体计算和生命周期见[资源上限与输出处理](../../host-demo/implementation.md#资源上限与输出处理)。单元测试在构建时消费公共通知夹具，运行时不读取文件。

HN2 的解析与队列测试在实现后补做可恢复变异核验：移除十进制尾零／整数保护后，`numbers` 与公共 `shared` 用例失败；把满队列改为覆盖最旧记录后，`fifo` 用例失败。恢复实现后相应目标全部通过。这是实施后的可证伪证据，不表示已经取得实施前的解析红灯记录。

第一版只有电机一种消息类型。未注册电机回调时不会创建通知通道，因此运行实例不存在“已启用通道但该合法类型未注册”的配置；未来新增其他类型时仍须覆盖公共接收表中的该分区。无回调模式由既有 `integration_host` 证明正常运行。

HN5 的 C 驱动提供实际公共注册、回调开始／返回观察和同步门闩。根 `test_camctl_motor_notifications.py` 在 CPython 3.11.16 指定环境下 5 项通过（36.33 秒），覆盖真实客户端边界位置导出、通知发送、回调、报告领取／导入与累计确认；慢回调期间并行 submit 及下一 run；相机共存；保存意图后／写入后退出 91 的真实 host 自动恢复。两种中断分别产生 0／1 次回调，恢复后原请求再次受理不增加发送，结果为错误码 29，报告可导入。M6 的 `test_camctl_motor_recovery.py` 另有 21 项通过，覆盖 11 个保存／写入／提交未知断点、通道失败、过期及取消；各断点分别核对消息、发送事实和报告，未知意图、同请求重送与报告重建不增加发送。完整结果见[统一验证记录](../../camctl/verification.md#电机控制与单向通知验证2026-10-08)。同步点和环境见[真实组合复验](../../host-demo/verification.md#电机通知真实组合复验2026-10-08)。

Debug 与 Release 在 CPython 3.11.16 协作者下各 33 项 CTest 通过；其中 11 项为单元、22 项为集成。独立交付与当前通知契约的详细证据见[组件验证记录](../../host-demo/verification.md#电机通知组件交付复验2026-10-08)。

HN4 状态矩阵复验：真实队列达到容量 2 后才发布 stderr 继续信号，CLI 写入 F_GETPIPE_SZ 所得容量加 4096 字节。首次回调保持阻塞时，stderr、stdout、submit 和具体 PID 回收完成；逐个放行至旧 98 号回调阻塞、99 号仍排队时，旧 EOF、结果解析与新 run 已观察。退出 0／7 分别通过；提前继续和错误暂停全部收集的负向模式分别失败。Debug／Release 各 33 项 CTest 再次全量通过，含独立交付。
