# C 接入模块与终端演示

第三方保留现有 C 主程序，在启动、收到计划文件和准备处理待传文件时调用三个接口。模块在后台管理 camctl 进程和有限重试，调用领取入口时同步将 `ready` 文件逐个移动到 `processing`。主程序负责后续传输和文件处理。

交付以源码为基础，默认构建 `libcamctl_host.a` 和 `host-demo`。目标为 arm64 Ubuntu 18.04；当前开发环境验证情况见仓库的 `docs/host-demo/verification.md`。生产依赖源码和许可证已包含在组件内，构建不联网下载。

## 构建与安装

需要 Linux、支持 C11 的编译器、pthread 和 CMake 3.10.2 或更新版本。从本组件目录或解压后的源码根目录运行：

```sh
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j2
```

生成 `libcamctl_host.a` 和 `host-demo`。安装到指定前缀：

```sh
cmake .. -DCMAKE_INSTALL_PREFIX=/opt/camctl-host
cmake --build . --target install
```

在自己的工程中链接：

```sh
cc -std=c11 main.c -I/opt/camctl-host/include \
  /opt/camctl-host/lib/libcamctl_host.a -pthread -o main
```

也可将组件作为 CMake 子目录加入工程，再用 `target_link_libraries(main PRIVATE camctl_host)`。自行管理构建时，编译 `src` 中除 `demo.c` 外的 C 文件及 `vendor/yyjson/yyjson.c`，加入 `include`、`src`、`vendor/yyjson` 头文件目录，定义 `_GNU_SOURCE` 并链接 pthread。依赖清单和摘要见 [vendor/manifest.cmake](vendor/manifest.cmake)。

独立构建可用 `cmake --build . --target package_source` 生成源码包，`--target package` 生成当前构建平台的二进制包。x86_64 编译所得的静态库不能直接用于 arm64；应在目标系统编译，或使用匹配目标系统的交叉工具链和系统头文件、库。

## 三个调用时机

公开头文件为 [camctl_host.h](include/camctl_host.h)。以下代码放入现有主程序的对应位置；完整可编译的调用示例为 [demo.c](src/demo.c)。

```c
#include "camctl_host.h"

/* 主程序启动时执行一次。各路径由第三方部署人员准备。 */
camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
config.camctl_path = "/opt/camctl/bin/camctl";
config.ready_path = "/srv/camctl/ready";
config.processing_path = "/srv/camctl/processing";
config.log_path = "/srv/camctl/host.log";
config.config_path = NULL; /* 也可以填入 camctl 配置文件的绝对路径。 */
int initialized = camctl_host_init(&config, NULL);

/* 收到计划并完整写入、关闭文件后执行。 */
int accepted = camctl_host_submit("/srv/incoming/plan.json");

/* 主程序选定处理待传文件的时机，同步领取，然后处理 processing。 */
camctl_host_claim();
```

初始化和递交返回 `0` 只说明本地已安排启动或接收路径；返回 `-1` 时通过 `errno` 了解拒绝原因。主程序须在初始化成功后调用另两个入口。计划是否受理、动作是否成功，以 camctl 生成的状态报告为准。

模块复制初始化配置和路径字符串；接口返回后可以复用这些字符串缓冲区。计划文件应已完整写入并关闭，并由主程序短期保留。模块不复制文件正文，后续文件读取结果由 camctl 处理。

初始化的第二个参数可提供首次计划的绝对路径。初始化成功后，模块持续运行；后续调用递交接口时，无论当前是否已有执行会话，模块都会自行选择正确命令。模块最多同时运行一个 `run` 和一个 `submit`，后续提交及其启动失败重试保持接收顺序。

容量包含排队、正在提交和等待启动重试的路径。容量满返回 `-1`、`errno=EAGAIN`，原队列保持不变。非法配置或相对路径通常为 `EINVAL`，路径过长为 `ENAMETOOLONG`，未初始化为 `ENODEV`，重复初始化为 `EALREADY`，内存不足为 `ENOMEM`；其他初始化失败保留实际系统错误。

## 部署准备与共存约定

- 部署人员准备 camctl 可执行程序、交接目录、模块日志父目录和需要的 camctl 配置文件，并显式执行 camctl 的部署初始化。模块不创建业务数据库，不解释 camctl 配置内容。
- `ready` 与 `processing` 是两个不同目录，位于支持原子重命名的同一文件系统。目录中的待传对象应为按协议发布的完整普通文件。模块先完整遍历一次，再用 `renameat` 逐个移动，同名目标被原子替换。单项失败继续，目录故障结束；已移动的文件保留在目标目录。
- 一轮领取的文件名列表最多占 16 MiB；超限或遍历未完成时，本轮不开始移动。移动后同步两个目录；同步失败记录无法确认落盘，不撤回已经移动的文件。实际断电持久性仍需目标存储验收。
- 模块只等待自己保存的 PID。主程序也应只回收自己的子进程，不能用全局 `waitpid(-1, ...)` 取走模块的退出状态，也不能通过忽略 `SIGCHLD` 或设置 `SA_NOCLDWAIT` 自动丢弃退出状态。无法确认某个子进程退出时，模块保留其占位，记录诊断，避免重复启动。
- 模块不改主程序的信号处理、环境变量、工作目录或标准流；自身描述符使用 close-on-exec（执行新程序时自动关闭）。主程序不需要被 camctl 继承的其他描述符，也应使用 `FD_CLOEXEC`。camctl 子进程的标准输入指向 `/dev/null`，输出通过独立管道读取。
- 公开接口用于主程序普通线程中的顺序调用，不能从信号处理函数调用。初始化成功后随主程序持续运行；断电会丢失尚在模块内存中的输入路径和日志。

## 配置范围与日志

所有路径必须是绝对路径，长度最多为 `CAMCTL_HOST_PATH_MAX` 字节，不含末尾 NUL。`config_path` 和首次计划可以为 `NULL`；其他路径必填。默认值统一由公开头文件的 `CAMCTL_HOST_CONFIG_INIT` 提供。

| 成员 | 可配置范围 |
| --- | --- |
| `retry_limit` | `uint32_t` 的取值范围；0 表示不自动重试 |
| `retry_delay_ms` | 0 至 86400000 毫秒 |
| `plan_capacity` | 1 至 4096 份后续计划 |
| `stdout_capacity` | 每个子进程 64 字节至 16 MiB |
| `log_queue_capacity` | 256 字节至 16 MiB，必须容纳至少一条完整记录及其管理数据 |
| `log_record_capacity` | 128 字节至 64 KiB，包含截断提示和换行 |
| `log_file_size` | 至少容纳一条最大日志，最大 1 GiB |
| `log_file_count` | 1 至 64 份，包含当前文件 |

自动重试的次数由所有调用共用，成功后不清零。日志和输出容量只影响模块本地处理，不代表业务受理或执行结果。stdout 超限后继续读取并丢弃超出部分，本次结果按异常处理；JSON 解析临时内存由 stdout 上限推导，不随运行时间增长。

模块日志由专用线程写入指定文件；归档使用 `.1`、`.2` 等后缀，`.1` 为最近归档。路径和归档名称供本模块独占使用，不交给外部轮换程序同时管理。每次写入前检查大小，包含当前文件的总数受配置限制。

日志记录含调用编号、命令、PID、操作、路径和实际错误；过长记录包含 `[truncated]`。队列满时丢弃新诊断，恢复后补记 `dropped` 数量。首次打开、写入或轮换失败会停用本次运行的文件日志，故障状态留在内存中；业务接口和进程管道继续处理。单调钟或进程事件等待设施失效、无法继续处理新输入时，递交返回本地错误，文件领取仍可调用。

## 运行终端演示

camctl 和部署目录准备好后：

```sh
./host-demo --camctl /opt/camctl/bin/camctl \
  --ready /srv/camctl/ready --processing /srv/camctl/processing \
  --log /srv/camctl/host.log
```

可选参数：`--config`、`--initial-plan`、`--retry-limit`、`--retry-delay-ms`。启动参数中的空格路径按 shell 规则加引号。

运行期间输入：

```text
submit /srv/incoming/计划 有空格.json
claim
logs
help
```

`submit` 后面的整段内容是路径，终端命令中不加引号；每条命令以换行结束。`claim` 返回后，由主程序处理 `processing`。`logs` 显示当前日志末尾最多 64 KiB。终端输入结束后模块继续运行，本地演示可通过外部信号终止。

## 分类测试

测试另外需要 Python 3.6 或更新版本，用于组织真实进程集成测试。cmocka 已随源码提供，生产构建不会编译它。

```sh
mkdir -p build-tests
cd build-tests
cmake .. -DHOST_BUILD_TESTS=ON
cmake --build . -- -j2
ctest -L unit --output-on-failure
ctest -L integration --output-on-failure
```

可通过 `-DHOST_TEST_PYTHON=/absolute/path/to/python3` 指定测试解释器。单元测试使用内存状态与受接口约束的替身；集成测试使用真实线程、进程、管道和文件系统，以可控 CLI 安排结果和故障。可控 CLI 不是业务实现，不能替代真实 camctl、设备和客户端的全流程验收。
