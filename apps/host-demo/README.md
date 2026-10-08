# C 接入模块与终端演示

第三方保留现有 C 主程序，在启动、收到计划文件和准备处理待传文件时调用三个接口。模块在后台管理 camctl 进程和有限重试，调用领取入口时同步将 `ready` 文件逐个移动到 `processing`。主程序负责后续传输和文件处理。

交付以源码为基础，默认构建 `libcamctl_host.a` 和 `host-demo`。目标为 arm64 Ubuntu 18.04；当前开发环境验证情况见仓库的 `docs/host-demo/verification.md`。生产依赖源码和许可证已包含在组件内，构建不联网下载。

## 构建与安装

需要 Linux、支持 C11 的编译器、pthread 和 CMake 3.10.2 或更新版本。从本组件目录或解压后的源码根目录运行。源码路径可以包含空格或特殊字符；构建目录使用普通路径，供 CMake 与 CPack 收集构建和打包产物：

```sh
HOST_SOURCE_DIR="$(pwd)"
HOST_BUILD_DIR="$(mktemp -d /tmp/camctl-host-build.XXXXXX)"
cd "$HOST_BUILD_DIR"
cmake "$HOST_SOURCE_DIR" -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j2
```

生成 `libcamctl_host.a` 和 `host-demo`。默认安装前缀为运行构建命令账户的 `$HOME/.camctl/host`，也可显式指定。以下命令把头文件、静态库与演示安装到用户目录：

```sh
cmake "$HOST_SOURCE_DIR" -DCMAKE_INSTALL_PREFIX="$HOME/.camctl/host" -DCMAKE_INSTALL_LIBDIR=lib
cmake --build . --target install
```

在自己的工程中链接：

```sh
cc -std=c11 main.c -I"$HOME/.camctl/host/include" \
  "$HOME/.camctl/host/lib/libcamctl_host.a" -pthread -o main
```

也可将组件作为 CMake 子目录加入工程，再用 `target_link_libraries(main PRIVATE camctl_host)`。自行管理构建时，编译 `src` 中除 `demo.c` 外的 C 文件及 `vendor/yyjson/yyjson.c`，加入 `include`、`src`、`vendor/yyjson` 头文件目录，定义 `_GNU_SOURCE` 并链接 pthread。依赖清单和摘要见 [vendor/manifest.cmake](vendor/manifest.cmake)。

在构建目录生成源码包与当前平台的二进制包：

```sh
cpack --config CPackSourceConfig.cmake
cpack --config CPackConfig.cmake
```

包版本由 CMake 项目版本派生。x86_64 编译所得的静态库不能直接用于 arm64；应在目标系统编译，或使用匹配目标系统的交叉工具链和系统头文件、库。

## 准备同一份部署配置

camctl 与主程序以同一个账户运行，默认部署目录为该账户的 `$HOME/.camctl`。按照[真实 camctl 联调指南](docs/camctl-integration.md)人工复制独立交付的 `requirements.txt` 和 camctl wheel，使用 uv 安装并显式初始化。该指南从已安装的 host-demo 出发，逐步验证电机通知回调、状态报告领取和重复提交，所需文件与命令均面向目标主机。

安装后入口为 `$HOME/.camctl/venv/bin/camctl`，可直接执行，无需激活虚拟环境。配置示例为 [examples/config.toml](examples/config.toml)，省略 `[paths]` 时，状态库、日志及 `staging`、`ready`、`processing` 使用 CLI 内置的 home 默认值。`init` 负责创建状态库及交接目录；日常 host 启动不代替部署初始化。设备绑定由实际部署补充。已有部署保留配置、状态库和历史，目录变更继续遵守 camctl 的目录切换流程。

`config_path = NULL` 时，CLI 读取运行账户的 `$HOME/.camctl/config.toml`；指定路径时只读取指定配置，文件不存在时采用内置默认值。指定配置文件不会自动改变其他路径。若 TOML 覆盖交接目录，主程序须同时覆盖 `ready_path`、`processing_path`，保证双方操作相同的目录。

## 三个调用时机

公开头文件为 [camctl_host.h](include/camctl_host.h)。以下代码放入现有主程序的对应位置；完整可编译的调用示例为 [demo.c](src/demo.c)。

```c
#include "camctl_host.h"
#include <stdlib.h>

void motor(int position); /* 主程序提供定义。 */

/* 主程序启动时执行一次；先完成上述部署初始化。 */
camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
camctl_host_paths paths;
if (camctl_host_config_set_home_paths(&config, &paths, getenv("HOME")) != 0) {
    /* 报告 errno 并结束本次初始化。 */
    return -1;
}
/* 如需显式覆盖路径或数值，在这里设置 config 对应成员。 */
/* 接收电机通知时，在初始化前注册主程序自己的 void motor(int position)。 */
int registered = camctl_host_register_motor_control_callback(motor);
int initialized = registered == 0 ? camctl_host_init(&config, NULL) : -1;

/* 收到计划并完整写入、关闭文件后，传入该文件的绝对路径 plan_path。 */
int accepted = camctl_host_submit(plan_path);

/* 主程序选定处理待传文件的时机，同步领取，然后处理 processing。 */
camctl_host_claim();
```

初始化和递交返回 `0` 只说明本地已安排启动或接收路径；返回 `-1` 时通过 `errno` 了解拒绝原因。主程序须在初始化成功后调用另两个入口。计划是否受理、动作是否成功，以 camctl 生成的状态报告为准。

路径补齐函数只填入为 `NULL` 的四个必填路径，保留已有配置和 `config_path`；传入的 home 必须是绝对路径。它不分配堆内存、不创建文件或目录，错误时保留配置与缓冲区，返回 `-1` 并设置 `EINVAL` 或 `ENAMETOOLONG`。`paths` 由调用方持有，生成的指针引用该缓冲区，必须保持有效且不移动，直至 `camctl_host_init` 返回。初始化复制配置和路径字符串，返回后可以复用缓冲区。计划文件应已完整写入并关闭，并由主程序短期保留。模块不复制文件正文，后续文件读取结果由 camctl 处理。

初始化的第二个参数可提供首次计划的绝对路径。初始化成功后，模块持续运行；后续调用递交接口时，无论当前是否已有执行会话，模块都会自行选择正确命令。模块最多同时运行一个 `run` 和一个 `submit`，后续提交及其启动失败重试保持接收顺序。

容量包含排队、正在提交和等待启动重试的路径。容量满返回 `-1`、`errno=EAGAIN`，原队列保持不变。非法配置或相对路径通常为 `EINVAL`，路径过长为 `ENAMETOOLONG`，未初始化为 `ENODEV`，重复初始化为 `EALREADY`，内存不足为 `ENOMEM`；其他初始化失败保留实际系统错误。

## 电机通知回调

主程序需要接收电机控制通知时，在初始化前调用 `camctl_host_register_motor_control_callback`。函数类型为 `void (*)(int position)`，只有位置参数；位置单位、零点、方向及业务范围由主程序定义。`position` 可以为负数或零，表示范围是 -2147483648～2147483647。终端演示先注册回调再初始化，只输出收到的位置。

首次非空注册返回 `0`；`NULL` 始终返回 `-1` 并设置 `EINVAL`。初始化尚未成功时，重复非空注册返回 `EALREADY`；初始化成功后，非空注册返回 `EBUSY`。失败调用保留原回调，初始化失败也保留注册，允许修正条件后重试。注册只保存函数指针，不创建线程或调用函数。

启用通知时，模块为每次 `run` 准备独立管道，并传入 `--host-notification-fd FD`；`submit` 不带此项。专用线程按消息接收顺序调用回调。回调执行期间不持有进程管理锁，可以调用 `camctl_host_submit`；主程序负责回调访问的状态及所执行设备操作的线程安全。长期阻塞的回调会延迟后续回调，队列满时模块暂停通知读取，继续处理其他输出、提交和进程回收。

CLI 完整写入只表示通知已经发送；回调开始或返回也不向 CLI 提供设备成功证据。尚未读完的旧通知占用原 `run` 位置，排空后新的 `run` 使用新管道，已排队消息继续按原顺序交付。主程序退出或断电后不恢复内存队列。没有注册回调时不创建通知管道、解析缓冲、队列或回调线程。

## 部署准备与共存约定

- 部署人员准备 camctl 可执行程序、交接目录、模块日志父目录和需要的 camctl 配置文件，并显式执行 camctl 的部署初始化。模块不创建业务数据库，不解释 camctl 配置内容。
- `ready` 与 `processing` 是两个不同目录，位于支持原子重命名的同一文件系统。目录中的待传对象应为按协议发布的完整普通文件。模块先完整遍历一次，再用 `renameat` 逐个移动，同名目标被原子替换。单项失败继续，目录故障结束；已移动的文件保留在目标目录。
- 一轮领取的文件名列表最多占 16 MiB；超限或遍历未完成时，本轮不开始移动。明确移动成功或已经尝试但结果未知时，本轮同步两个已打开的目录，后续文件或目录故障不清除已有同步责任。两个同步分别尝试并记录错误；同步成功不证明未知移动成功，失败也不撤回文件。实际断电持久性仍需目标存储验收。
- 模块只观察和回收自己保存的具体 PID。初始化及每次启动前检查 `SIGCHLD`；显式 `SIG_IGN` 或 `SA_NOCLDWAIT` 导致本地接入错误，普通 `SIG_DFL` 和不抢先回收模块子进程的处理器可以使用。主程序持续遵守各自回收的分工，具体约定见仓库的 `docs/host-demo/design.md`。
- 每次调用在执行 camctl 前建立独立进程组。模块保留退出记录，分批检查原组的进程和线程，必要时发送 `SIGKILL`；全部执行工作停止后才最终回收和释放调用位置。扫描未知或退出记录丢失时保留管理责任。部署须提供完整可读的 Linux `/proc`，工具及包装程序保持组归属和查询、终止权限；共享 ADB 服务端按独立生命周期管理。
- 模块不改主程序的信号处理、环境变量、工作目录或标准流；自身描述符使用 close-on-exec（执行新程序时自动关闭）。主程序不需要被 camctl 继承的其他描述符，也应使用 `FD_CLOEXEC`。camctl 子进程的标准输入指向 `/dev/null`，输出通过独立管道读取。
- 公开接口用于主程序普通线程中的顺序调用，不能从信号处理函数调用。初始化成功后随主程序持续运行；断电会丢失尚在模块内存中的输入路径和日志。

## 配置范围与日志

所有路径必须是绝对路径，长度最多为 `CAMCTL_HOST_PATH_MAX` 字节，不含末尾 NUL。`config_path` 和首次计划可以为 `NULL`；其他路径必填。数值默认值由公开头文件的具名宏及 `CAMCTL_HOST_CONFIG_INIT` 提供，缺省路径由 `camctl_host_config_set_home_paths` 在运行时构造；不展开 C 字符串中的 `$HOME` 或 `~`。

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
| `notification_line_capacity` | 1 至 65536 字节，包含结束 LF；默认由 `CAMCTL_HOST_NOTIFICATION_LINE_DEFAULT` 提供 |
| `notification_queue_capacity` | 1 至 4096 项待分发位置，执行中的一项另计；默认由 `CAMCTL_HOST_NOTIFICATION_QUEUE_DEFAULT` 提供 |

自动重试的次数由所有调用共用，成功后不清零。日志和输出容量只影响模块本地处理，不代表业务受理或执行结果。stdout 超限后继续读取并丢弃超出部分，本次结果按异常处理；JSON 解析临时内存由 stdout 上限推导，不随运行时间增长。

通知单行容量包含 LF；超长行丢弃到下一 LF 后恢复，EOF 残片拒绝。已接纳的位置不会因为 CLI 异常退出而丢弃。通知缓冲由行、固定读取块、yyjson 解析池、有界位置队列和一条执行中记录组成，容量不随运行时长增长。默认配置的数据存储上界为 61956 字节，最大合法配置为 938244 字节；此外还有固定管理结构、分配器及线程运行开销。模块在分配前检查乘法和相加溢出。

模块日志由专用线程写入指定文件；归档使用 `.1`、`.2` 等后缀，`.1` 为最近归档。路径和 `.1` 至 `.63` 的归档名称供本模块独占使用，不交给外部轮换程序同时管理。首次文件操作清理超出本次保留数的旧归档；每次写入前检查大小，包含当前文件的总数受配置限制。清理错误按文件通道故障处理，在内存保留步骤、归档编号和 errno，业务继续运行。

日志记录含调用编号、命令、PID、操作、路径和实际错误；过长记录包含 `[truncated]`。队列满时丢弃新诊断，恢复后补记 `dropped` 数量。首次打开、写入或轮换失败会停用本次运行的文件日志，故障状态留在内存中；业务接口和进程管道继续处理。单调钟或进程事件等待设施失效、无法继续处理新输入时，递交返回本地错误，文件领取仍可调用。

## 运行终端演示

camctl 和部署目录准备好后：

```sh
"$HOME/.camctl/host/bin/host-demo"
```

演示程序从 `HOME` 取得主目录；未设置时读取当前用户记录中的主目录。路径无法补齐时明确报错。`--camctl`、`--ready`、`--processing`、`--log`、`--config`、`--initial-plan`、`--retry-limit`、`--retry-delay-ms` 均可选；显式值覆盖对应默认项。指定全部必填路径时不依赖 home。启动参数中的空格路径按 shell 规则加引号。

运行期间输入：

```text
submit <计划文件的绝对路径>
claim
logs
help
```

将 `<计划文件的绝对路径>` 替换为实际路径。`submit` 后面的整段内容是路径，终端命令中不加引号，也不展开 `$HOME` 或 `~`；每条命令以换行结束。`claim` 返回后，由主程序处理 `processing`。`logs` 显示当前日志末尾最多 64 KiB。终端输入结束后模块继续运行，本地演示可通过外部信号终止。

## 分类测试

测试使用部署规格要求的 Python 3.11，用于组织真实进程集成测试。cmocka 已随源码提供，生产构建不会编译它。

```sh
HOST_TEST_BUILD_DIR="$(mktemp -d /tmp/camctl-host-tests.XXXXXX)"
cd "$HOST_TEST_BUILD_DIR"
cmake "$HOST_SOURCE_DIR" -DHOST_BUILD_TESTS=ON -DHOST_TEST_PYTHON=/absolute/path/to/python3.11
cmake --build . -- -j2
ctest -L unit --output-on-failure
ctest -L integration --output-on-failure
```

可通过 `-DHOST_TEST_PYTHON=/absolute/path/to/python3` 指定测试解释器。单元测试使用内存状态与受接口约束的替身；集成测试使用真实线程、进程、管道和文件系统，以可控 CLI 安排结果和故障。可控 CLI 不是业务实现，不能替代真实 camctl、设备和客户端的全流程验收。
