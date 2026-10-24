# host 子进程失败诊断实施计划

**目标：** `camctl_host` 在 camctl 调用被判为失败或确定未能启动后，将本次调用的 stdout、stderr、实际进程结果和调用上下文作为独立文件交给 `processing`。

**架构：** `host_child` 在现有管道收集期间有界保存 stdout 和 stderr 前缀，同时继续读取两条管道。进程组收场、具体子进程最终回收、stdout/stderr 处理完成并完成结果分类后，host 将失败尝试交给专用写入线程。线程在 `processing` 下的私有准备目录中写完整文件、同步并关闭，再通过 Linux `SYS_renameat2(RENAME_NOREPLACE)` 将其原子发布到 `processing` 根。每次尝试有独立 `attempt_id`；排队等待的快照留在对应 child 槽，直到写入结束。

**技术栈：** C11、POSIX 子进程与文件 API、Linux 系统调用、pthread、CMake/CTest、Python 3.11 可控 CLI 集成测试。

**契约依据：** `docs/host-demo/design.md`、`docs/host-demo/implementation.md`、`docs/architecture/log-delivery.md` 与本次 host 失败诊断契约。

**构建与回归命令：**

```sh
cmake -S apps/host-demo -B .local/host-exit-log-build -DHOST_BUILD_TESTS=ON \
  -DHOST_TEST_PYTHON="$(uv python find 3.11)" -DCMAKE_BUILD_TYPE=Debug
cmake --build .local/host-exit-log-build -- -j4
ctest --test-dir .local/host-exit-log-build --output-on-failure
```

## 全局约束

- 诊断内容来自 host 从本次 camctl 调用 stdout/stderr 管道读取的原始字节、实际退出状态及调用上下文；camctl 普通日志继续由 camctl 的现有写入和轮换流程管理。
- `HOST_RESULT_SUCCESS` 不生成诊断；其他已确定结果分类及确定未创建子进程的尝试各生成一份独立诊断。
- 已启动调用只有在进程组收场、具体子进程回收、stdout/stderr 处理完成及分类完成后才能发布诊断；若最终回收后管道仍打开并返回 `EAGAIN`，对应 `*_read_error` 标明输出不完整或 EOF 未知。
- stdout 最多保存配置的 `stdout_capacity`；stderr 最多保存 `min(stdout_capacity, 256 KiB)`。超出部分继续读取，并记录实际读取量、保存量及截断标志。
- 调用上下文保存 host 内部 `call_id`、每次尝试唯一的 `attempt_id`、命令、camctl/config/计划路径、spawn 时继承的工作目录、PID/PGID、重试上下文、分类、退出码或信号、spawn errno。`call_id` 不是计划 JSON 的 `request_id`。
- `processing` 下的隐藏私有准备目录保存半成品；完整文件同步并关闭后才原子发布到 `processing` 根。文件直接进入 `processing`，不参与 `ready → processing` 领取。
- 使用 `SYS_renameat2` 与 `RENAME_NOREPLACE` 保证不覆盖已有文件；不依赖较新的 glibc `renameat2` 包装符号，不以覆盖式改名作为后备。
- 诊断队列最多有两个未完成位置，队列满时快照仍留在 child 槽，host 延后复用该槽并继续 drain 另一条活跃命令，不丢弃诊断快照。
- 文件创建、写入或发布失败只记 host 本地日志，不改变 CLI 分类、原重试资格或业务返回值，不循环重试文件操作。失败的同步 `submit` 在诊断写入成功或本次写入尝试失败后，按原结果释放提交记录。
- host 退出或断电时，尚留在 child 槽的内存诊断不保证落盘；本次不增加启动扫描或恢复流程。

## 复核重点

- 两条管道输出超过各自上限时仍继续读取，记录准确标出已读量、保存量和截断状态。
- stdout/stderr 字节长度可准确解析，并保留 NUL 与非 UTF-8 字节；文件头路径使用十六进制编码。
- 成功、合法 `error`、非零退出、信号、协议异常和确定启动失败的分类及实际退出信息准确；原重试路径和业务结果不变。
- run 与 submit 并发、submit 自动重试时 `attempt_id` 和文件名不冲突，child 槽及快照上下文在写入结束前保持有效。
- processing 消费端不能看到根目录半成品；原子发布使用 no-replace 语义，现存目标文件内容不被覆盖。
- writer 等待、队列背压或写入失败不阻塞另一条活跃 CLI 的管道 drain 和进程组收场；初始化新增线程失败时 stop/join 与资源回收完整。

---

### 任务 1：有界捕获 stdout 与 stderr

**文件：**

- 修改 `apps/host-demo/src/process.h`、`apps/host-demo/src/process.c`、`apps/host-demo/src/host.c`
- 修改 `apps/host-demo/tests/unit/test_process_unit.c`、`apps/host-demo/tests/integration/test_process.c`

**接口：**

- `host_child` 保存两条流的实际读取量、已保存长度、截断和读取错误。
- `host_child_init` 接收两条流的独立缓冲区及容量，调用方负责生命周期。

**步骤：**

- [x] 先增加 stderr 限额和持续排空断言。原实现的预期失败记录见 `.local/host-exit-red-process-capture.log`。
- [x] 按 `min(stdout_capacity, 256 KiB)` 保留 stderr 前缀，超限后持续 drain；stdout 容量与结果判定保持原规则。
- [x] 运行 `ctest --test-dir .local/host-exit-log-build -R '^(unit_process_unit|integration_process)$' --output-on-failure`；最终 focused 回归日志见 `.local/host-exit-focused-rerun.log`。

### 任务 2：独立诊断写入器与原子发布

**文件：**

- 新增 `apps/host-demo/src/diagnostic.h`、`apps/host-demo/src/diagnostic.c`、`apps/host-demo/src/linux_rename.c`、`apps/host-demo/tests/unit/test_diagnostic.c`
- 修改 `apps/host-demo/CMakeLists.txt`、`apps/host-demo/src/host.c`、`apps/host-demo/tests/integration/host_driver.c`、`apps/host-demo/tests/integration/test_host.py`

**接口：**

- writer 最多接收两个未完成快照。快照复用对应 `host_child` 缓冲区，child 槽在写入结束前保持占用；未入队快照在该槽保持 pending。
- 文件头记录版本、host 内部 `call_id`、`attempt_id`、命令、调用路径和工作目录、PID/PGID、重试上下文、分类、退出类型和值、流读取量与保存量、截断及读取错误；随后按记录的长度保存 stdout/stderr 原始字节。
- 准备目录采用 `processing/.camctl-host-diagnostic-prepare-<host-pid>-<attempt-id>-<random>`；最终文件名采用 `camctl-host-diagnostic-<host-pid>-<attempt-id>-<random>.log`，两者复用随机后缀。
- 发布函数通过单独的 Linux 系统调用封装调用 `SYS_renameat2(RENAME_NOREPLACE)`，便于在实际系统调用边界注入写入失败及目标冲突。

**步骤：**

- [x] 为并发唯一命名、原始字节长度、processing 根目录无半成品、no-replace 冲突和准备目录清理增加集成断言。原实现的预期失败记录见 `.local/host-exit-red-session-error.log`。
- [x] 写入器在私有目录创建 0700 子目录和 0600 临时文件；完整写入、`fsync` 并关闭后再原子发布，失败只通过现有 host logger 记录一次。
- [x] writer 与工作线程分离；快照等待期间不占用工作线程，child 槽阻止上下文悬空或被重用。
- [x] 确认诊断发布完成或失败后清理本次准备目录；no-replace 冲突保留原有目标字节。
- [x] `integration_host` 通过，覆盖 writer 阻塞时另一命令继续 drain、排队快照保留上下文、publish 失败及 no-replace 冲突。

### 任务 3：接入结果分类与尝试生命周期

**文件：**

- 修改 `apps/host-demo/src/host.c`、`apps/host-demo/src/process.h`、`apps/host-demo/src/process.c`
- 修改 `apps/host-demo/tests/integration/controlled_cli.py`、`apps/host-demo/tests/integration/host_driver.c`、`apps/host-demo/tests/integration/test_host.py`、`apps/host-demo/tests/integration/test_init_failure.c`

**接口：**

- 每次 spawn 前分配新的 `attempt_id`；submit 自动重试保留原输入调用 ID，但每次尝试使用新 attempt ID。
- 已启动结果记录为 `success`、`error` 或 `abnormal`；确定未创建子进程的尝试记录 `not_started` 和 errno，输出区段长度为零。

**步骤：**

- [x] 可控 CLI 覆盖成功、state_db_error、协议异常、信号、非零退出、原始字节和超限。
- [x] 将非成功分类及确定未启动尝试接入诊断发布；成功不生成文件。
- [x] 保持原 scheduler 决策及 submit 返回值；submit 等待本次诊断写入尝试结束再释放原输入记录。
- [x] 初始化失败矩阵覆盖新增 stderr 分配、诊断 writer 线程及后续 callback/worker 线程清理。
- [x] focused CTest 通过，日志见 `.local/host-exit-focused-rerun.log`。

### 任务 4：文档、回归与交接

**文件：**

- 修改 `apps/host-demo/README.md`、`docs/host-demo/design.md`、`docs/host-demo/implementation.md`、`docs/host-demo/verification.md`、`docs/architecture/log-delivery.md` 及本计划。

**步骤：**

- [x] 文档记录触发分类、文件格式、容量、上下文、processing 发布、失败边界及原日志职责。
- [x] 运行完整 host CTest（后续快照 35/35 通过）、文档本地链接检查（5369 个本地文件链接）和 `git diff --check`；完整测试首次运行及复测的边界记录在 `docs/host-demo/verification.md`。
- [x] 检查完整 diff、保留首次全量失败证据，确认工作区归属并提交独立分支；实际命令、cwd、Python 3.11、UTC 和退出码见验证记录及交接。

## 当前交付

新开发容器使用 Python 3.11.17 完成完整 CTest，35/35 通过；Release 构建、安装及源码包生成通过。统一安装版进一步验证真实 CLI 的报告领取和状态库缺失时的独立失败诊断，旧记录与新环境结果分别保留。当前安装入口及目标硬件边界见[组件验证记录](../../host-demo/verification.md)和[统一交付记录](../../camctl/verification.md#独立安装与手工验证入口)。
