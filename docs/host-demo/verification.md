# C 接入模块验证记录

[返回接入入口](README.md) · [构建与调用说明](../../apps/host-demo/README.md)

本记录对应 `apps/host-demo` 第一版源码、静态库和终端演示。组件不包含业务 CLI、设备驱动或文件传输实现。

## 主程序主动停止验证（2026-10-26 UTC）

Linux x86_64 开发容器，GCC 13.3.0、CMake 3.28.3，CTest 的 Python 协作者指定 CPython 3.11.17。生产实现合入主工作区的提交为 `c465ec25`；独立审查后，在主工作区重新执行完整 Debug 构建及全部单元测试。

```sh
cmake -S apps/host-demo -B .local/host-stop-build -DHOST_BUILD_TESTS=ON \
  -DHOST_TEST_PYTHON=/workspaces/camctl/apps/camctl/.venv311/bin/python \
  -DCMAKE_BUILD_TYPE=Debug
cmake --build .local/host-stop-build -- -j4
ctest --test-dir .local/host-stop-build -L unit -V --output-on-failure
```

配置、构建和测试均退出 0；16 个单元入口、162 例全部通过。新增停止入口、写入器关闭和演示退出单元分别为 18、5、11 例；进程单元共 28 例，包含具体 PID 一次 SIGINT 及身份失效的反例。线程创建、等待、信号、进程和文件接口使用受约束的替身，没有运行真实线程或子进程。

覆盖停止后拒绝受理和启动、保留已启动提交的真实结果、时钟与等待故障不放弃在管进程、在途 API 等待、并发与重复停止、回调排空和重入、自线程停止拒绝、通知队列满及已读残块、诊断和日志排空，以及关闭错误后的阶段保留。独立审查发现的线程编号复用与线程退出尾部自停问题，通过 C11 线程局部身份判定闭合。

`node scripts/check-doc-links.mjs` 和 `git diff --check` 通过。命令、原始日志、逐入口数量及组件文件 SHA-256 保存在 `.local/verification/host-stop-20261026/`。原始单元日志包含非 UTF-8 夹具字节，原始日志完整保留；仅解析计数时替换不可解码字节。

本次按用户要求未运行集成测试，未连接设备、操作真实状态库、更新真机安装版或停止当前运行进程。安装示例的编译消费者已经同步维护，但真实线程、信号、安装示例与进程组的组合行为尚未复验，交由后续人工验证。

随后按用户要求更新现有真机验证安装。源码 `d8c3b614` 的 Release host 构建、安装到独立交付目录及最终复制均成功；最终环境中 11 个 host 文件与交付逐项一致，二进制和静态库均包含 `camctl_host_stop`，安装版 `--help` 包含退出命令。camctl 同步更新并通过 350 个包文件的安装核对。旧 host 此前已经退出，遗留 camctl 经一次 SIGINT 确认退出；更新后不自动启动服务。配置、状态库、日志和依赖保留，部署收据位于 `.local/deployments/20261026-host-stop-camera-idle/`。本次部署复用源码匹配的单元证据，没有重复运行测试或连接设备；实际启动、停止与拍摄流程留待人工验证。

## 开发容器重建后的组件与安装版验证（2026-10-24 UTC）

本次环境为 Ubuntu 24.04.5 x86_64、GCC 13.3.0、CMake 3.28.3、Python 3.11.17 和 SQLite 3.53.1。独立工作树 `.local/worktrees/host-refresh-20261025/` 在 `fdcdc92` 完成 Debug 构建及完整 CTest：35/35 通过，其中 13 个单元入口、22 个集成入口，退出 0，83.39 秒。交付集成入口同时核对源码包内容、解包后构建、安装目录、独立链接消费者与文档链接。Release 构建、安装及源码包生成均退出 0；完整命令和输出保存在该工作树的 `.local/host-refresh-verify/logs/`。

真实安装 wheel 与 Release host 进一步在各自新目录完成两条无设备检查。正向场景经 `init`、host 递交唯一的完整状态报告动作、CLI 发布和 host 领取，确认计划及动作完成，`processing` 报告的文件名摘要和全部原始字节一致。负向场景只准备交接目录，配置指向不存在的状态库；CLI 以 `state_db_error`、退出 1 结束，host 直接发布完整独立诊断，库文件仍不存在。该诊断为 1606 字节，stdout 实际读取及保存量均为 432，stderr 均为 0，没有截断或读取错误；分类、退出信息、调用上下文及两段原始字节均经解析核对。

两场景的 CLI 子进程均由 host 回收后，才向本次 host PID 发送 SIGTERM，host 结果为 -15，编排检查退出 0。命令、配置、进程收场、报告与诊断证据保存在 `.local/verification/installed-host-smoke-20261025/` 的两个独立运行目录。手工验证库与旧归档保留，没有连接真实设备。当前 Release 二进制为 x86_64，目标 ARM64 使用同次源码包在目标环境另行构建与验收；统一交付进度见[接续计划](../superpowers/plans/2026-10-25-environment-test-speed-delivery.md)。

## host 子进程失败诊断交付验证（2026-10-23 UTC）

本次在 Linux x86_64、Ubuntu 24.04.4、GCC 13.3.0、glibc 2.39、CMake 3.28.3 和 CPython 3.11.16 中，验证 host 对失败 camctl 调用的输出捕获、直接发布至 `processing` 和初始化失败清理。构建目录位于独立 worktree `/workspaces/camctl/.local/worktrees/host-exit-log-20261024`，所有相对命令均从该 worktree 根目录运行；没有连接真实 camctl 部署环境或 ADB。

| 检查 | 命令与结果 | 证据边界 |
| --- | --- | --- |
| Debug 配置和构建 | `cmake -S apps/host-demo -B .local/host-exit-log-build -DHOST_BUILD_TESTS=ON -DHOST_TEST_PYTHON=/home/zsm1695/.local/bin/python3.11 -DCMAKE_BUILD_TYPE=Debug`，随后 `cmake --build .local/host-exit-log-build -- -j4`；均退出 0。 | 日志为 `.local/host-exit-configure-final.log` 和 `.local/host-exit-build-final.log`。测试解释器为 CPython 3.11.16。 |
| 失败诊断 focused CTest | `ctest --test-dir .local/host-exit-log-build -R '^(unit_diagnostic|unit_process_unit|integration_process|integration_init_failure|integration_closed_stdio|integration_host)$' --output-on-failure`；6/6 通过，退出码 0。 | 日志为 `.local/host-exit-focused-rerun.log`。覆盖成功不发布、合法会话错误、协议异常、信号、spawn 失败、原始 NUL/非 UTF-8 字节、输出截断、并发命名、writer 等待时继续 drain、submit 上下文保留、发布故障和 no-replace 目标冲突。 |
| 首次完整 host CTest | `ctest --test-dir .local/host-exit-log-build --output-on-failure`；34/35 通过，`integration_init_failure` 在 `test_init_failure.c:130` 的 `/proc/self/task` 条目数断言失败，其余 34 项通过，命令返回非零。 | 原始输出保留在 `.local/host-exit-ctest-final.log`。首次工具会话没有保留 shell 数字退出码。原断言没有输出失败矩阵的注入类别/序号或实际条目数，因此现有证据不能确认具体注入点、实际条目数或根因；本记录不将其归因为时序问题。 |
| 初始化线程清理复测 | `ctest --test-dir .local/host-exit-log-build --repeat until-fail:50 -R '^integration_init_failure$' --output-on-failure`；50/50 通过；后续单项 focused 也通过。 | 日志为 `.local/host-exit-init-repeat.log`。当前测试在未来发生线程条目数不符时打印注入类别序号及 expected/actual；线程创建矩阵覆盖 logger、诊断 writer、callback 和 worker。源码清理路径对 writer 执行 stop、signal、join；这一复测没有恢复首次失败的具体注入与实际计数。 |
| 后续完整 host CTest | 再次运行 `ctest --test-dir .local/host-exit-log-build --output-on-failure`；35/35 通过，退出码 0。 | 日志为 `.local/host-exit-ctest-rerun.log`。后续通过说明当前快照全套测试通过；它没有解释首次 `integration_init_failure` 的原因。 |
| Linux no-replace 系统调用 | `nm -u .local/host-exit-log-build/CMakeFiles/camctl_host.dir/src/diagnostic.c.o` 与 `.../src/linux_rename.c.o`；发布封装引用 `syscall`，不引用 glibc `renameat2` 包装符号。 | 集成测试通过在最终候选文件名创建 sentinel 冲突，观察 `RENAME_NOREPLACE` 返回 `EEXIST`，并逐字节确认原文件仍为 `preserve`。这证明当前 Linux 测试内核上的 no-replace 行为；尚未在 Ubuntu 18.04 / ARM64 执行组件 CTest。 |
| 文档检查 | `node scripts/check-doc-links.mjs` 与 `git diff --check`；5369 个本地文件链接通过，差异格式检查通过，命令退出码均为 0。 | 链接脚本不校验标题锚点；新增锚点另行对照目标标题核对。链接日志为 `.local/host-exit-doc-links-final.log`。 |

最后一次 UTC 时间由 `date -u` 读取，为 2026-10-23 23:00:51 UTC。上述检查只验证软件工作区和可控 CLI；不代表真实 camctl 部署、ADB、ARM64 运行或断电持久性验收。

## 阻塞递交与原计划文件释放验证（2026-10-21 UTC）

本次在 Linux x86_64、GCC 13.3.0、CMake 3.28.3、Python 3.11.16、SQLite 3.53.1 中，验证[递交调用契约](design.md#对外调用契约)。`camctl_host_submit` 成功返回确认输入处理结果已保存，原文件可删除；业务受理或拒绝及设备动作结果仍通过报告确认。初始化入口的首次计划继续异步处理。

| 检查 | 实际结果与边界 |
| --- | --- |
| Debug 组件全量 CTest | 34 项通过，包含 12 个单元入口和 22 个集成入口；交付入口同时验证源码包、安装与独立构建。 |
| AddressSanitizer 与 UndefinedBehaviorSanitizer | 12 个单元入口及 host、正常／异常通知、初始化失败 4 个集成入口，共 16 项通过；启用泄漏检测。 |
| 真实 CLI、状态库和 C 主程序 | `test_camctl_host_deployment.py`、`test_camctl_process_recovery.py`、`test_camctl_motor_notifications.py` 共 18 项通过。 |
| 文档与差异 | 本地文件链接检查和 `git diff --check` 通过；公开接口与计划文件小节的引用锚点单独核对。 |

组件集成测试用文件门闩阻止 CLI 读取输入，确认调用保持等待；另在成功消息写出后阻止 CLI 退出，确认消息本身不会提前释放调用。文件处理完成后，即使 `run` 仍在运行，递交调用也能返回。并发测试核对各调用结果、队列容量与顺序，故障分支覆盖会话错误、结果缺失、启动重试耗尽、单调钟／`poll` 失效及调用线程取消。电机回调重入递交和其他线程领取文件保持可用。

真实部署组合分别使用默认目录和自定义交接目录：递交返回后立即查询状态库，确认未来报告动作已保存且尚待执行，然后删除原 JSON、结束测试 host 并重新启动。新的裸 `run` 从状态库恢复并完成动作，报告没有计划文件读取诊断。另一个用例确认，整份拒绝诊断在递交成功返回前已经保存，原文件同样可删除，状态库没有注册该计划。

新增等待、错误和故障用例中，6 项先在原实现上出现预期断言失败，再由修改后的实现通过；取消用例也先出现提前正常返回的失败，再补齐取消交付边界。首次联合运行中，3 项进程门闩用例仍按异步返回组织调用而超时，改为独立递交线程后通过；5 项电机联合用例因开发工作区缺少 `tsx` 在业务验证前失败，准备与锁文件一致的依赖后复验通过。

从仓库根目录执行：

```sh
cmake -S apps/host-demo -B .local/host-submit-build -DHOST_BUILD_TESTS=ON \
  -DHOST_TEST_PYTHON="$(command -v python3.11)" -DCMAKE_BUILD_TYPE=Debug
cmake --build .local/host-submit-build -- -j4
ctest --test-dir .local/host-submit-build --output-on-failure
apps/camctl/.venv311/bin/python -m pytest \
  tests/integration/test_camctl_host_deployment.py \
  tests/integration/test_camctl_process_recovery.py \
  tests/integration/test_camctl_motor_notifications.py -q
```

内存检查构建使用 `-fsanitize=address,undefined -fno-omit-frame-pointer` 和相同链接参数，测试选择 `unit_` 及上述 4 个集成入口。命令输出分别保存在 `.local/host-submit-build/ctest.log`、`cross-component-final.log` 和 `sanitize-final.log`；早期失败输出单独保留。本次没有更新 ARM64 部署、连接真实设备或进行物理断电验证。

## 交付前规格核对（2026-10-08）

源码和独立交付物已覆盖未知移动后的双目录同步、最终失败判定后的重试等待、跨运行日志保留数、源码路径过滤、同配置部署示例、领取步骤诊断、安装资料引用及最低 CMake 的版本标识。默认配置的五个路径由 CLI 配置适配器统一展开。逐项契约、失败反例及完成记录见[交付审查与修复计划](../superpowers/plans/2026-10-08-camctl-host-delivery-audit.md)。

## 开发环境与已执行检查

2026-10-08 的复验环境为 x86_64 Linux、Ubuntu 24.04、GCC 13.3.0、glibc 2.39，测试解释器为 CPython 3.11.16（SQLite 3.53.1）。组件测试使用 cmocka 和可控 CLI；O6 使用真实 C 模块、生产 CLI 和工具启动入口。依赖的确定版本及摘要见[依赖清单](../../apps/host-demo/vendor/manifest.cmake)。

| 检查 | 实际范围 |
| --- | --- |
| CMake 3.28.3 常规构建 | 静态库、终端演示、组件测试 |
| CMake 3.10.2 Release 构建 | 实际使用该版本配置、编译并运行分类测试 |
| 单元测试 | 8 个 CTest 入口全部通过，包含结果校验、调度状态、日志队列、文件领取、配置与输入队列、独立组启动、退出记录和原组扫描 |
| 集成测试 | 20 个 CTest 入口全部通过，使用真实线程、子进程、管道和文件系统；其中主程序场景与终端场景由 Python 组织 |
| AddressSanitizer 与 UndefinedBehaviorSanitizer | Release 启用内存及未定义行为检测，8 个单元入口与 20 个集成入口全部通过；实际 CMake 3.10.2 与 3.28.3 的常规 Release 也分别通过同样分类 |
| 生产交付 | 关闭测试构建，安装 8 个文件；独立链接随包示例并运行。源码包的 56 个文件逐字节核对，二进制包与安装结果一致；README 本地引用全部可定位。两类包版本均为 1.0.0 |

可从仓库根目录重现常规测试：

```sh
cmake -S apps/host-demo -B .local/host-demo-build -DHOST_BUILD_TESTS=ON \
  -DHOST_TEST_PYTHON=/absolute/path/to/python3.11
cmake --build .local/host-demo-build -- -j2
ctest --test-dir .local/host-demo-build -L unit --output-on-failure
ctest --test-dir .local/host-demo-build -L integration --output-on-failure
```

CMake 3.10.2 使用传统目录调用方式：进入独立构建目录，执行 `cmake /absolute/path/to/apps/host-demo -DHOST_BUILD_TESTS=ON -DHOST_TEST_PYTHON=/absolute/path/to/python3.11 -DCMAKE_BUILD_TYPE=Release`，随后执行 `cmake --build .` 和 `ctest -L unit`、`ctest -L integration`。测试构建保留断言，Release 不会移除验收调用。

## 已验证的契约

| 责任边界 | 覆盖的行为 |
| --- | --- |
| stdout 与进程退出 | 完整单行消息、分段读取、消息与退出码不一致、未知错误原因、字段缺失或类型错误、重复判定字段、额外输出、超限、退出事实未知；收到消息不提前认定子进程结束 |
| 命令与重试 | 首次计划保留与释放、后续 submit、共用重试次数、实际尝试前计数、成功不清零、等待期间预算耗尽、最终结果判定后重新取时、多个 needs_run 合并、false/错误保留待启动要求、重试耗尽后新提交触发启动 |
| 输入与初始化 | 配置与路径复制、队首保持与容量拒绝、未初始化调用、重复初始化、配置非法、分配/管道/两个线程创建阶段失败后的清理 |
| 进程与主程序 | 长 run 期间仍提交、持续输出不堵塞管道、明确启动失败与执行后退出 127、指定 PID 回收、主程序目录和信号处理保持、关闭标准流时模块日志不持续占用其描述符 |
| 独立组与接入检查 | exec 前建立独立组，组建立失败不执行；run、submit 和主程序的组独立，工具后代继承；初始化和启动前检查 SIGCHLD，拒绝显式忽略与 SA_NOCLDWAIT，不修改全局处置 |
| 原组收场与最终回收 | WNOWAIT 保留退出记录，有限批次核验进程及 task；僵尸主线程仍有活线程时继续收场；信号发出、部分扫描、读取拒绝、格式错误、成员变化及提前回收均不能放行，完成后才回收具体 PID；扫描描述符不跨批次占用标准流 |
| 文件领取 | 完整遍历后才移动、列表超限不部分移动、同名覆盖、inode 与原始字节保持、单项失败继续、目录故障停止、移动结果未知不回退；成功或未知移动后分别同步两个目录，首个同步失败仍尝试另一个；诊断区分检查、移动与同步 |
| 文件操作竞争 | 在首次原子移动边界安排发布新文件、撤下已列文件、移动成功但返回未知错误、目录位置变化；依据实际位置确认已移动文件和本次未领取文件 |
| 日志与业务调用 | 队列拥塞丢弃新诊断、恢复后记录丢弃数量、单条截断、轮换数量与大小、跨运行减少或增加保留数、超范围归档清理失败和不相关文件保持、日志路径不可用，以及日志写入被明确阻塞时仍能提交和领取 |
| 模块基础设施故障 | 单调钟失效后拒绝新路径、保留诊断，文件领取仍可调用 |
| 终端示例 | 公开接口调用、中文和空格路径、同名文件领取、非法和超长命令、输入结束后模块继续运行 |

并发测试使用文件门闩或系统调用边界安排事件顺序；日志轮换的文件数量与大小在暂停主程序线程后检查，避免将正常轮换期间的路径变化误判为存储故障。测试中的受控进程和替身只安排协作结果，不承担真实业务语义。

## 真实 camctl 组合复验（2026-10-08）

`uv run --project apps/camctl --python 3.11 --frozen --group test pytest tests/integration/test_camctl_process_recovery.py -q`：8 项通过。C 驱动调用同一公开接入模块，生产 CLI 执行 run/submit，工具使用 `operations.process.spawn_subprocess` 启动。覆盖正常与异常退出、终止请求后继续占位、工具停止但扫描未完、扫描权限故障恢复、提前回收后的禁止组信号、pending 与共享预算、并行 submit，以及模拟共享服务端脱离前后和脱离与终止并发。

`test_camctl_host_deployment.py`：3 项通过，覆盖默认路径、显式缺失配置及随包 TOML 的初始化、发布与实际领取。

`test_camctl_report_roundtrip.py`：6 项通过；`test_camctl_c_module_roundtrip.py`：3 项通过。两者使用当前工作区构建的 host-demo，复验客户端导出、递交、执行、领取、导入与 ACK，包含报告以及拍摄、取回、取消、清理。软件契约的映射与执行状态见[跨组件计划 I1/I2](../superpowers/plans/2026-09-30-camctl-integration.md#i1i2-验证记录2026-10-08linux-x86_64)和[软件验收映射](../camctl/software-acceptance.md)。

## camctl 执行与取消的交付回归（2026-10-08）

真实组合使用 CPython 3.11.16、SQLite 3.53.1。首次控制前重新检查设备残留；可靠未启动的 RUNNING 动作按窗口过期或本地取消收场，活动的执行事实与冲突占用分别保存。已有启动尝试、确认查询及残留停止沿原身份和累计预算恢复。取消生效共同结束普通 START；迟到尝试结果独立保存，录像与延时取消使用原停止预算，停止失败或未确认分别反映在目标事实、取消成员和取消动作汇总中。

组件验证涵盖真实 SQLite 的复合终态提交、回滚、提交确认丢失后的重开与重送，以及历史初始回放、已有边界正向恢复和当前投影逆向恢复。读取并行场景使用共同受控时钟和已保存结果作为同步点。具体反例、状态分类和各目录结果见[执行接缝修复记录](../superpowers/plans/2026-10-08-capture-start-residual-gate.md#验证记录)。这些检查不连接真实设备。

## 电机通知组件交付复验（2026-10-08）

环境为 Linux 6.6.87.2 WSL2、x86_64、Ubuntu 24.04、GCC 13.3.0、CMake 3.28.3；CTest 的 Python 协作者固定使用 CPython 3.11.16。Debug 与 Release 各 33 个 CTest 入口通过，包含 11 个单元入口和 22 个集成入口。通知同步点与容量断言补强后，相关解析、配置及真实通知目标在两个构建中分别复验通过。

| 责任边界 | 证据与结果 |
| --- | --- |
| 注册与初始化 | NULL 的 EINVAL 优先、首次注册、重复注册、成功初始化后的 EBUSY；5 个 malloc、3 个 calloc、2 个 pipe、3 个线程、4 个 mutex、2 个 cond 和 3 个 fcntl 建立步骤分别注入故障。每次失败后检查原注册、未发布实例、描述符基线、线程基线及模块分配余额。 |
| 严格接收 | 构建时读取公共 27 项通知夹具并生成静态测试数据；运行时使用真实 yyjson 接收器。覆盖分割位置、多行、重复及转义后重名字段、额外字段、ID、UTF-8／Unicode、嵌入 NUL、int 边界、数学整数小数／指数、精度反例、行容量 N−1／N／N+1、超长恢复和 EOF 残片。 |
| 测试的可证伪性 | HN2 在实现后补做可恢复变异核验：移除尾零／整数保护使 numbers 和公共 shared 用例失败；满队列覆盖最旧记录使 fifo 用例失败。恢复代码后两个目标分别通过；这不表示已经取得实施前的解析红灯。 |
| 独立管道与系统资源 | 启用 run 带独立 FD，submit 无此参数；pipe、fcntl 和 spawn 失败路径检查描述符清理。主程序关闭 0／1／2 后真实 spawn 仍取得通知和最终 stdout，主程序标准描述符保持关闭。 |
| 回调与进程管理 | 真实队列成功入队达到容量 2 后才允许 CLI 写 stderr，第一条回调仍被门闩阻塞。CLI 读取 F_GETPIPE_SZ，写入该容量加 4096 字节后记录完成；stdout、submit 与具体 PID 回收均在首次回调返回前完成。100 条相同 ID 的完整合法消息分别按序交付；逐个放行至 98 号回调阻塞、99 号仍排队时，观察旧通知实际 EOF、最终 stdout 解析和新 run 启动。正常退出与退出 7 分别执行完整状态矩阵，覆盖半行隔离与回调重入 submit。提前发布 stderr 继续信号、满队列时暂停整个进程收集两种负向核验均失败。 |
| 第三方交付 | 独立安装头文件只声明 `void motor(int position)` 的消费者成功编译、链接和注册；源码包重新配置、编译，并逐文件核验；安装 README 链接可解析。终端演示收到 position=-12，只记录通知位置。 |

单元测试不访问真实文件系统、线程或子进程；真实管道、线程、初始化清理、演示和包构建属于组件集成测试。host 不读取状态库、不发送 ACK、不缓存动作 ID。真实 camctl 的发送与持久化恢复组合由 HN5／M6 单独记录，受控 CLI 的通过不替代它。

## 电机通知真实组合复验（2026-10-08）

Linux x86_64 环境使用 CPython 3.11.16、SQLite 3.53.1、Node.js 24.20.0 和 GCC 13.3.0。根集成测试从本次源码构建 Release host 静态库，再链接只使用公开接口的 `motor_host_driver.c`。真实客户端生成请求，host 启动生产 CLI 并管理管道及具体 PID，CLI 使用真实 SQLite，报告由 C 模块领取并经客户端实际导入。

```sh
UV_PROJECT_ENVIRONMENT=/workspaces/camctl/apps/camctl/.venv311 \
  uv run --project apps/camctl --group test --python 3.11 \
  pytest tests/integration/test_camctl_motor_notifications.py -q
```

结果为 5 项集成测试通过，耗时 36.33 秒。

| 场景 | 实际观察 |
| --- | --- |
| 客户端边界位置与累计确认 | −2147483648、−1、0、2147483647 分别进入单参数回调，顺序一致；发送结果持久化为 WRITTEN，报告没有设备执行及产物字段。客户端导入报告后再导出累计确认，真实 C submit 与 CLI 将确认吸收到状态库。 |
| 慢回调与下一次 run | 首条回调被明确门闩阻塞时，原 run 完成，后续真实 submit、下一 run 及两批共 5 条发送继续。报告可领取并导入；放行回调后两批消息按原顺序调用。 |
| 相机共存 | 电机回调仍阻塞时，相机契约替身完成真实采集流程并保存照片产物；两个动作各自成功，报告只对电机表达通知已发送。 |
| 保存意图后中断 | CLI 保存意图后退出 91，真实 host 回收具体 PID，并按既有重试规则自动启动不携带首次计划的新 run。恢复动作以错误码 29 和 UNCONFIRMED 收场，回调总数为 0，恢复报告可导入。 |
| 管道写入后中断 | CLI 真实写入后退出 91，host 仍交付已到达的位置一次并自动恢复；数据库及报告表达结果未确认。恢复及相同请求再次 submit 均不增加发送或回调，恢复报告可导入。 |

中断桥只在生产事务或管道调用完成后使 CLI 进程退出一次；host、CLI 编排、数据库和客户端均未替换。断点观察包括 marker、实际写入副本、具体 PID 回收日志、自动重启参数和回调计数。完整发送断点矩阵、管道不可用、过期及取消由 M6 的另一组真实 CLI 集成测试验证，21 项通过，见[统一验证记录](../camctl/verification.md#电机控制与单向通知验证2026-10-08)。实际电机、目标主机性能与物理断电仍由部署验收。

## 用户目录部署验证（2026-10-09）

Linux x86_64 开发容器，GCC 13.3.0、CMake 3.28.3、CPython 3.11.16、SQLite 3.53.1。范围为[用户目录与路径补齐](implementation.md#用户目录与路径补齐)、默认安装、随包 TOML、终端演示及真实 CLI 文件交接。

- Debug 和 Release 重新构建后分别执行 `ctest --test-dir .local/host-demo-build --output-on-failure`、`ctest --test-dir .local/host-demo-release --output-on-failure`：各 34 项通过，包含 12 个单元入口、22 个集成入口。
- `unit_home_paths` 的 10 个用例覆盖缺省补齐、中文及空格路径、末尾分隔符、根目录、显式覆盖、重复调用、缺失及非法 home、空输出指针、完整路径长度边界和后续字段超限时不部分写入。测试不访问环境或文件系统。
- `integration_demo` 覆盖不传路径参数时从 HOME 启动默认 CLI 并领取文件、原显式参数方式及非法 home。`integration_delivery` 覆盖源码包、安装示例、C 调用方使用新接口、默认用户安装前缀和作为子目录引入时保持父项目安装前缀。另有 C++11 调用方编译、链接和实际调用通过。
- 从仓库根执行 `UV_PROJECT_ENVIRONMENT="$(pwd)/apps/camctl/.venv311" uv run --project apps/camctl --group test --python 3.11 pytest tests/integration/test_camctl_host_deployment.py -q`：4 项通过。临时 HOME 含中文及空格；真实 CLI 显式初始化，真实 host 使用默认入口启动 CLI，报告由默认目录发布、领取并逐字节比较；同时验证 TOML 和 host 共同覆盖交接目录，以及默认／显式缺失配置的 CLI 路径规则。

本节测试使用临时用户目录，没有更改运行账户的真实部署数据，也没有操作真实设备。CLI 启动桥使用指定 Python 3.11 调用生产入口。目标 ARM64 上 CPython 3.11.17、SQLite 3.53.1 和 camctl 安装版已用于 Action6 与 Hasselblad 的现场流程，见[Action6 现场验收](../camctl/verification.md#action6-普通录像演示的现场验收2026-10-11)和[Hasselblad ARM 验收](../camctl/verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)；这不表示本组件 CTest 已在 ARM64 执行。

## 当前目标环境与真机验收状态（截至 2026-10-13）

本节分别记录组件软件验证、目标机部署、真实相机演示和第三方接入证据。某一层通过不自动证明其他层已经通过。

| 验收范围 | 当前状态 | 证据边界 |
| --- | --- | --- |
| host-demo 组件软件验证 | Linux x86_64 下已有分类测试和真实进程集成记录。2026-10-09 的 Debug 与 Release 各有 34 个 CTest 入口通过，其中 12 个单元入口、22 个集成入口。 | 这是开发环境证据；更早日期的测试数量属于当时源码快照，见本记录各日期小节。ARM64 上尚未执行这套组件 CTest。 |
| ARM64 构建、安装及现场使用 | 2026-10-10，交付源码在 ARM64 Ubuntu 18.04.6 上用 CMake 3.10.2、GCC 7.5.0 以 Release 且 `HOST_BUILD_TESTS=OFF` 构建安装；2026-10-11 的 Action6 普通录像演示使用了 C host。 | 证明目标工具链构建安装及该次实际运行路径；目标机没有运行组件 CTest，也没有覆盖所有第三方主程序接入方式。详细环境见[ARM 主机部署准备](../camctl/verification.md#arm-主机部署准备2026-10-10)。 |
| Action6 普通录像 | ARM 现场演示于 2026-10-11 经用户确认通过，包含客户端生成取回计划和成功完成独立取回。 | 原始计划、正式报告、媒体文件和确认水位没有在仓库归档；不补写媒体大小、摘要、实测码率或逐项响应。完整范围见[现场验收记录](../camctl/verification.md#action6-普通录像演示的现场验收2026-10-11)。 |
| Hasselblad CFV 100C 单张拍摄 | 2026-10-12 在 ARM64 Ubuntu 18.04.6 完成真实 RAW 拍摄、原图与配对预览登记、独立取回及最终离线报告。 | 首次两份 delivery 的领取和删除由验收脚本模拟；这不是 C host 或第三方传输程序的实际领取。验收没有物理拔线或断电，相机端是否保留原片仍待设备负责人确认。完整证据见[Hasselblad ARM 验收](../camctl/verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)。 |

按真实相机演示场景核对，当前尚无通过证据的项目是 Action6 原生延时摄影，以及 OSMO 360 II 的完整真实设备接入与验收；两者仍按[相机接入计划 T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)跟踪。Action6 普通录像及 Hasselblad 单张拍摄按上表各自的实际范围记录。

### 仍需真实环境验收

上文列出的 Action6 原生延时摄影和 OSMO 360 II 完整设备接入，是当前尚缺通过证据的真实相机演示场景。本节另列目标部署联调清单中的其他执行项，避免把未执行的环境检查写成已通过；这些事项不增加第一版真实相机演示的未完成范围。架构将目标主机实际调度、传输吞吐及断电持久性安排为单独联调或测量，具体操作仍由[目标环境联调清单](../superpowers/plans/2026-10-11-camctl-target-integration-runbook.md)跟踪。

| 独立目标环境跟踪项 | 当前证据边界与责任入口 |
| --- | --- |
| `arm64 Ubuntu 18.04` | 交付源码已用 CMake 3.10.2、GCC 7.5.0 以 Release 构建安装，Action6 现场运行使用了 C host；ARM64 组件 CTest 尚未执行。目标测试及 `posix_spawn` 启动错误核对见清单 F。 |
| 第三方主程序 | 实际第三方程序的初始化、计划递交、文件领取、电机回调及双方进程和描述符责任尚未组合验证；本地终端演示和测试驱动不代表第三方程序，见清单 A、B。 |
| 第三方文件传输 | 尚无真实第三方链路的传输、失败残留及后续处理证据。host-demo 只把 `ready` 文件移至 `processing`；Hasselblad 验收脚本模拟领取和删除，不证明传输已经发生，见清单 C。 |
| 真实电机与目标资源 | 软件通知和并发组合已有验证记录，但没有真实第三方主程序驱动电机的现场证据；目标资源也没有实测。分别见清单 A、D。 |
| 主机突然断电 | 尚未在目标存储上验证断电后的文件位置和 camctl 恢复；普通进程中断不证明物理断电持久性，见清单 E。 |

上述状态是“尚无对应现场证据”，不表示软件接口或进程恢复实现未完成。步骤与通过判据见[目标环境联调清单](../superpowers/plans/2026-10-11-camctl-target-integration-runbook.md)。源码可在记录的目标机工具链构建；本地 x86_64 二进制不能作为 ARM64 交付物。
