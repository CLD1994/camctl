# camctl 测试部署包

本包包含嵌入式 Linux 主机上的 camctl、C 接入模块 camctl_host、终端演示 host-demo，以及个人电脑上的客户端。版本、源码摘要、能力说明来源和实际验证结果见 `RELEASE.json`；文件摘要见 `SHA256SUMS`。

## 组件怎样协作

| 组件 | 运行位置与职责 | 配置入口 |
| --- | --- | --- |
| 客户端 | 在个人电脑上提供网页；编辑和导出计划，导入报告、图片和视频 | `CAMCTL_DATA_DIR` 指定客户端数据目录；设备说明从该目录加载 |
| camctl | 在 Linux 主机上作为独立进程运行；受理计划、调度设备、保存状态、生成报告和待传文件 | `config.toml`；通过 `--config` 指定 |
| camctl_host | 静态链接进第三方 C 主程序；启动和管理 camctl，递交计划、领取文件、分发电机通知 | 初始化前填写 `camctl_host_config` 结构体 |
| host-demo | 使用 camctl_host 的终端演示程序；用于检查接入流程 | 命令行参数；安装器生成的启动脚本已填写本实例路径 |

日常文件流为：客户端导出计划 → 人工或传输系统交给主程序 → 主程序递交给 camctl → camctl 在 `staging` 准备结果并发布到 `ready` → 主程序调用领取接口移入 `processing` → 传输系统送回客户端。客户端与 Linux 主机分别保存自己的数据库，通过文件交换业务资料。

## Linux 主机首次安装

### 前提与操作

在目标 Linux 主机解压整个包，准备 uv、CMake 3.10.2 或以上、C11 编译器和 pthread。相机工具和媒体工具另外准备：ADB 相机需要 `adb`，Hasselblad 需要 `hb_cli`，相应视频核验和处理需要 `ffprobe`、`ffmpeg`。本包不附带这些工具、Python 或 Node.js。

`install-host.py` 是**首次安装入口**。它接受 Python 3.6 或以上；当前解释器不是 3.11 时，通过 uv 查找或下载 Python 3.11，再继续安装。camctl 始终使用 Python 3.11。首次获取解释器或锁定依赖时需要联网；不会修改系统 Python。

在解压后的 `camctl-deploy` 目录执行。默认安装和后面的自定义安装示例按需选择一种；每个目标目录只安装一次。以下示例在同一个终端使用变量 `deploy_root` 表示新实例目录：

```sh
deploy_root="$HOME/.camctl/device-test"
python3 install-host.py --target "$deploy_root"
```

安装器依次创建虚拟环境、安装锁定依赖和本包 wheel、检查依赖、查询相机身份、生成配置和能力说明、在目标机编译安装 C 模块与演示、显式初始化状态库，最后生成启动脚本。安装完成后保持停止状态，不启动主程序或执行拍摄。主机安装不需要 Node.js，也不会在这台机器上安装客户端依赖。

### 安装参数

所有路径参数使用绝对路径；含空格时按 shell 规则引用。`--target` 必填且目录必须不存在。

| 参数 | 作用 | 省略时 |
| --- | --- | --- |
| `--target PATH` | 新实例根目录；保存配置、程序和安装诊断 | 必须提供 |
| `--hb-cli PATH` | 现场 `hb_cli` 可执行文件 | 从 `PATH` 查找 |
| `--state-db PATH` | 写入 `paths.state_db`；文件必须不存在 | `<target>/state.db` |
| `--log-file PATH` | 写入 `paths.log_file`，配置 Python camctl 日志 | `<target>/camctl.log` |
| `--staging PATH` | 写入 `paths.staging`，保存准备文件及主机正式源 | `<target>/staging` |
| `--ready PATH` | 写入 `paths.ready`，发布完整待传文件 | `<target>/ready` |
| `--processing PATH` | 写入 `paths.processing`，主程序领取后的目录 | `<target>/processing` |
| `--client-data PATH` | 将导出的能力说明复制到指定客户端数据目录 | `<target>/client-data` |
| `--help` | 显示本包安装器支持的参数 | 不执行安装 |

例如，首次安装就使用自定义日志和交接目录：

```sh
python3 install-host.py \
  --target "$deploy_root" \
  --hb-cli /opt/hb_cli/hb_cli \
  --log-file "$deploy_root/logs/camctl.log" \
  --ready /var/opt/SatService/dt_outbox \
  --processing /var/opt/SatService/dt_sending
```

运行账户须具有所用目录的读写权限；`staging`、`ready`、`processing` 须互不相同、互不包含，并满足同一文件系统上的原子移动条件。安装器在初始化前确定最终路径，`config.toml` 和生成的 host-demo 启动命令使用同一组交接目录。

`init` 会检查该 Python 进程实际链接的 SQLite 运行库；安装了新版系统 `sqlite3` 命令并不能代替这项检查。运行库不满足本包要求时，按初始化诊断准备合格的解释器和运行库，再使用新目录安装。

`--client-data` 只复制能力说明，必要时创建目录，并覆盖其中同名的 `device-capabilities.json`；不安装客户端、不创建客户端数据库，也不改动已有数据库或媒体。个人电脑与主机分开部署时，通常保持默认值，安装后再传递该文件。

### 相机自动发现与占位配置

安装器通过 `adb devices -l` 的产品标识识别 ADB 相机，通过 `hb_cli` 的身份查询识别 Hasselblad。已识别设备写入真实 serial，同型号多台 ADB 相机使用稳定排序后的编号。Hasselblad 工具不能唯一选择多台匹配机身时停止安装。

工具未找到或型号未识别时，缺失型号生成 `REPLACE_*` 占位配置，终端和 `camera-discovery.json` 会列出待修改字段。软件安装完成不表示这些相机已绑定；使用前填写实际 serial、工具路径，并删除确实不需要的占位设备。身份查询失败、超时或响应无效时停止安装并保留诊断。

需要指定 ADB 服务时，在运行安装器前设置实际的 `ADB_SERVER_SOCKET`。安装器将该值保存在发现报告和 host-demo 启动脚本中；接入自有主程序时，也应在启动主程序的环境中设置同一个值。

### 安装产物与失败处理

| `<target>` 下的路径 | 用途 |
| --- | --- |
| `venv/bin/camctl` | 本实例的 camctl 可执行入口，无需激活虚拟环境 |
| `config.toml` | 自动生成的主机配置；后续按部署要求修改 |
| `state.db`、`staging/`、`ready/`、`processing/` | 默认运行路径；显式参数覆盖后的实际位置见配置和收据 |
| `host/include/camctl_host.h`、`host/lib/libcamctl_host.a` | 第三方主程序编译接入所需的头文件和静态库 |
| `host/bin/host-demo`、`start-host-demo.sh` | 演示程序和本实例的启动入口 |
| `device-capabilities.json`、`client-data/device-capabilities.json` | 按本实例配置导出的声明及默认客户端同步副本 |
| `camera-discovery.json` | 身份查询的参数、原始响应、退出或超时事实及占位项 |
| `install.log`、`install-receipt.json` | 安装各步骤的输出、退出码和最终状态 |

检查安装器退出码为 0，且收据的 `status` 为 `installed_stopped`。失败时先查看终端指出的步骤，再查 `install.log`、收据和发现报告；保留失败目录，修正原因后使用新目标目录重试。该入口不用于更新或覆盖已有部署。

## camctl 的配置与日志

### 选择配置文件

安装器生成 `<target>/config.toml`。生成的启动脚本显式传入此文件；手工执行 `describe`、`init`、`run` 或 `submit` 时也应传入同一 `--config`：

```sh
"$deploy_root/venv/bin/camctl" describe --config "$deploy_root/config.toml"
```

省略 `--config` 时，camctl 读取运行账户的 `$HOME/.camctl/config.toml`。所选文件不存在时允许使用内置默认值，因此拼错文件名可能表现为使用默认目录。选择配置文件或安装目录不会自动改变其他数据路径。`describe` 校验配置并导出设备能力，不展示全部生效配置，也不核验状态库绑定。

### 常用配置

配置采用 TOML。**在原配置中合入或修改对应字段，保留其他路径和设备；已有节不要重复声明。** 以下示例中的绝对路径须替换为实际部署位置。

```toml
[paths]
log_file = "/home/user/.camctl/device-test/logs/camctl.log"

[log]
level = "INFO"
max_size = "20 MiB"
file_count = 5
```

`log.level` 支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`，内置默认值为 `WARNING`。`max_size` 是单文件轮换阈值，`file_count` 包含当前日志文件。需要调整日志队列时，还要保持 `0 < queue_low_watermark < queue_high_watermark < queue_capacity`；单独缩小队列不会自动缩放水位。

设备配置沿用安装器生成的设备 ID，例如：

```toml
[devices.action6-01]
kind = "camera"
driver = "dji-action6"
adb.serial = "REPLACE_WITH_REAL_SERIAL"
connection.prepare_lead_s = 30
recording.start_timeout_s = 15
recording.start_retry_interval_s = 3
video.result_mode = "single_file"
```

`connection.prepare_lead_s` 控制提前准备时间；`recording` 控制设备启动调用的时限和尝试策略。`video.result_mode` 根据实际固件和部署选择 `single_file` 或 `segmented`。Hasselblad 的工具路径、serial 和正式源选择放在该设备的 `hasselblad` 子表。分辨率、曝光和采集时长等拍摄要求由计划动作携带，按导出的能力说明填写。

常用配置及所有字段的默认值、合法范围和实际生效边界见包内[完整 camctl 配置参考](docs/camctl-configuration.md)。它还包含会话轮询、时钟、文件拷贝与清理等配置，并明确标出仅完成加载、尚未接入执行的字段。

### 三类日志分别配置

| 对象 | 位置由谁决定 | 记录什么 |
| --- | --- | --- |
| Python camctl 运行日志 | `config.toml` 的 `paths.log_file` 与 `[log]`；安装时可用 `--log-file` 定路径 | camctl 的调用、运行和错误诊断 |
| C 模块运行日志 | `camctl_host_config.log_path` 和 `log_*` 容量、轮换成员；host-demo 用 `--log` 定路径 | 进程启动、提交、领取及模块本地错误 |
| 安装日志 | 固定为 `<target>/install.log` | 构建和安装各步骤的 stdout、stderr |

安装器生成的 host-demo 启动脚本使用 `<target>/host.log`。`install-host.py --log-file` 只配置 Python 日志；要改 C 模块日志位置，应修改 host-demo 的 `--log` 或自有主程序的 `log_path`。host-demo 的 `logs` 命令显示 C 模块日志。

C 模块还会把失败 camctl 调用的有界输出及进程上下文保存为 `processing` 内的独立诊断文件；它不经过 `ready`，无需再次领取。模块日志、诊断文件和状态报告分别说明本地接入、调用失败和业务结果，不能用其中一种替代其他结果。

### 修改配置的生效步骤

配置在每次 CLI 启动时读取，本次进程运行期间固定。修改前停止主程序管理的相关调用并确认收场；修改后再启动。设备 ID 被计划和历史引用，应保持稳定。改变设备绑定或能力后重新导出声明并交给客户端。

首次安装已执行 `init`。普通日志级别或轮换参数调整不需要重建状态库。已有状态库的交接目录具有绑定关系；更换 `staging`、`ready` 或 `processing` 时，须先完成依赖原目录的文件与责任，暂停 CLI、领取和传输，再按项目的目录切换流程操作，不能只修改路径后启动。保留已有状态库与历史。

## 先使用 host-demo 检查接入

填好现场设备配置并同步能力说明后，在确认其他环境没有操作同一设备的进程时启动：

```sh
"$deploy_root/start-host-demo.sh"
```

终端内输入 `help` 查看命令；常用操作如下。`submit` 后填写实际绝对路径，终端命令中不加引号，也不展开 `$HOME` 或 `~`：

```text
submit /absolute/path/to/plan.json
claim
logs
quit
```

`submit` 阻塞等待本次输入处理；返回成功后原计划文件可删除，但计划是否受理、设备动作是否完成仍看状态报告。`claim` 同步把本轮文件从 `ready` 移到 `processing`，实际传输由第三方处理。`quit`、Ctrl+C 或 SIGTERM 会等待模块停止在管进程后退出；仅关闭标准输入时模块继续运行。

host-demo 是自有 C 主程序的接入示例。部署时由其中一个程序管理本实例，避免同时启动演示和自有主程序来操作同一设备。

## 将 camctl_host 集成到自有 C 主程序

### 编译与链接

主机安装器已在目标平台编译静态库。将头文件目录和库加入现有工程，并链接 pthread。例如，在放有 `main.c` 的目录执行：

```sh
cc -std=c11 main.c \
  -I"$deploy_root/host/include" \
  "$deploy_root/host/lib/libcamctl_host.a" \
  -pthread -o my-main
```

也可解压包内 `host/*-Source.tar.gz`，将源码作为 CMake 子目录加入工程，再用 `target_link_libraries(my-main PRIVATE camctl_host)` 链接。静态库必须与目标架构和系统工具链匹配。

### 配置对应关系

先用 `CAMCTL_HOST_CONFIG_INIT` 初始化结构体，再设置路径和需要覆盖的数值。C 配置独立于 TOML，模块通过 `config_path` 为各次 camctl 调用添加 `--config`，不会读取 TOML 来替主程序填交接目录。

| C 成员 | 应填写的值 | 与 camctl 的关系 |
| --- | --- | --- |
| `camctl_path` | `<target>/venv/bin/camctl` 的绝对路径 | 模块启动的独立程序 |
| `config_path` | `<target>/config.toml` 的绝对路径 | Python 配置来源；为 `NULL` 时按 CLI 默认规则选文件 |
| `ready_path` | 生效配置中的 `paths.ready` | 模块领取文件的来源，必须与 camctl 一致 |
| `processing_path` | 生效配置中的 `paths.processing` | 模块领取后的目标，必须与 camctl 一致 |
| `log_path` | C 模块自己的日志绝对路径 | 独立于 `paths.log_file` |

C 路径最多为 `CAMCTL_HOST_PATH_MAX` 字节，不展开 `$HOME` 或 `~`。`camctl_host_config_set_home_paths` 可按用户 home 补齐尚为 `NULL` 的四个必填路径；自定义安装根目录时，应如上显式填写路径。模块复制初始化配置；初始化成功后修改调用方结构体不会改变运行实例。

数值参数分别控制模块的自动重试、计划队列、输出缓冲、日志队列及轮换、电机通知的行缓冲和队列容量。默认值和字段类型见[本包公开头文件](docs/camctl_host.h)，合法范围及日志行为见[camctl_host 配置参考](docs/host-configuration.md)。host-demo 只开放它的 `--help` 所列参数，其他成员由自有主程序在初始化前填写。

其中 `retry_limit` 是整个模块生命周期共享的自动重试次数，设为 0 关闭自动重试，调用成功不会清零已使用次数；它与 TOML 中设备控制的 `recording.max_start_attempts` 等次数分别管理。`plan_capacity` 包含排队、正在提交和等待重试的计划；`stdout_capacity` 限制调用结果缓冲，不决定业务计划是否受理。

### 调用时机与返回含义

下面的函数放入自有主程序的对应生命周期中。示例假定运行账户为 `/home/user`、目标目录为 `/home/user/.camctl/device-test`，且采用安装器默认交接目录；实际接入时填写本实例的路径。`motor` 由主程序实现，回调只有位置参数，位置含义由主程序定义。

```c
#include "camctl_host.h"
#include <stddef.h>

void motor(int position);

int start_camera_control(void) {
    camctl_host_config config = CAMCTL_HOST_CONFIG_INIT;
    config.camctl_path = "/home/user/.camctl/device-test/venv/bin/camctl";
    config.config_path = "/home/user/.camctl/device-test/config.toml";
    config.ready_path = "/home/user/.camctl/device-test/ready";
    config.processing_path = "/home/user/.camctl/device-test/processing";
    config.log_path = "/home/user/.camctl/device-test/host.log";
    if (camctl_host_register_motor_control_callback(motor) != 0)
        return -1;
    return camctl_host_init(&config, NULL);
}

int receive_plan(const char *absolute_plan_path) {
    return camctl_host_submit(absolute_plan_path);
}

void collect_ready_files(void) {
    camctl_host_claim();
}

int stop_camera_control(void) {
    return camctl_host_stop();
}
```

- 启动时先完成 `camctl init`，再在普通线程中注册所需回调并初始化模块。本包安装器已执行首次 `init`。`camctl_host_init` 返回 0 只说明模块本地资源和首次启动安排成功。第二个参数为 `NULL` 表示不携带首次计划；提供计划路径时首次处理仍是异步的，应保留原文件直到报告确认。无需电机通知时可省略注册，直接初始化；初始化失败后修正重试时保留已有注册，不重复注册。
- 收到计划后先完整写入并关闭文件，再递交绝对路径；等待期间保持文件路径及内容不变。`submit` 返回 0 表示本次输入处理结果已保存，含已保存的拒绝或诊断；返回 -1 时检查 `errno` 和日志，保留原文件以核实或沿用原 `request_id` 重送。需要继续处理其他事务的主程序可用独立普通线程递交。
- 主程序决定何时调用 `claim`，返回后处理 `processing` 的文件。该接口返回 `void`，逐项失败通过模块诊断记录；同名目标可以被原子覆盖，实际传输和文件处理由主程序负责。
- 主程序准备退出时，在普通线程调用 `stop` 等待在管进程、通知、回调和诊断收场。停止不会取消已经保存的计划；同一进程停止后不能重新初始化模块。信号处理函数只通知普通线程退出，不能直接调用这些接口；回调线程也不能调用 `stop`。

一个主程序进程内只有一个模块实例。主程序保留模块子进程的退出记录，不显式忽略 `SIGCHLD`、不设置 `SA_NOCLDWAIT`，也不通过通配 `waitpid` 抢先回收模块子进程。运行环境须提供可读的 Linux `/proc`。电机回调在专用线程中执行，主程序负责共享状态的线程安全；回调应及时返回。回调或工具长期不返回时，停止可能持续等待。

安装后的完整接入说明与演示源码在 `<target>/host/share/doc/camctl_host/` 下；本包 C 源码压缩包也包含它们。

## 个人电脑客户端

### 安装与默认启动

将整个 `client/` 复制到个人电脑，保留内部目录结构。安装符合 `client/apps/client/package.json` 中 `engines.node` 范围的 Node.js 正式版本。网页已构建，只安装生产依赖；node_modules 在目标电脑生成。

Windows PowerShell，从包根目录执行：

```powershell
Set-Location client/apps/client
npm ci --omit=dev --engine-strict --no-audit --no-fund
Set-Location ../..
./start-client.ps1
```

Linux，从包根目录执行：

```sh
cd client/apps/client
npm ci --omit=dev --engine-strict --no-audit --no-fund
cd ../..
sh start-client.sh
```

启动脚本未取得 `CAMCTL_DATA_DIR` 时，使用脚本所在 `client/data/`。打开 `http://localhost:4310`，核对页面的数据目录后点击“创建客户端数据”。已有数据直接复用；正常启动不会自动创建数据库。浏览器须支持原生 `JSON.rawJSON` 和 `JSON.isRawJSON`。

### 自定义数据目录与端口

启动脚本优先保留环境变量 `CAMCTL_DATA_DIR`，它须指向已经存在、可读写的绝对目录。该目录同时保存能力说明、数据库及媒体。首次使用新目录时，先创建它并复制设备说明；不要往已有业务目录复制初始数据库。

Windows PowerShell，以下命令在 `client/` 目录执行：

```powershell
$env:CAMCTL_DATA_DIR = 'D:\camctl-data'
$env:PORT = '4311'
New-Item -ItemType Directory -Force $env:CAMCTL_DATA_DIR | Out-Null
Copy-Item ./data/device-capabilities.json $env:CAMCTL_DATA_DIR
./start-client.ps1
```

Linux，以下命令在 `client/` 目录执行：

```sh
export CAMCTL_DATA_DIR="$HOME/camctl-data"
export PORT=4311
mkdir -p "$CAMCTL_DATA_DIR"
cp ./data/device-capabilities.json "$CAMCTL_DATA_DIR/"
sh start-client.sh
```

此时访问 `http://localhost:4311`，检查页面显示的实际目录。环境变量只对当前终端及其子进程生效；新终端再次启动时须设置相同值，或由自己的启动环境保存。客户端默认仅监听 `127.0.0.1`，通过 localhost 访问。

从包内默认目录切换到已有业务数据时，先停止客户端并确认后端结束，再整体复制或恢复数据目录，包括 SQLite 伴随文件及 `uploads/`；只改环境变量会选择另一个目录，不迁移原数据。停止服务使用终端的 Ctrl+C，关闭浏览器不会停止后端；备份应在停机后复制整个实际数据目录。

## 将现场设备声明同步给客户端

包内 `client/data/device-capabilities.json` 由本包 wheel 自动生成，`RELEASE.json` 标记它来自指定配置还是占位配置。占位声明用于查看参数结构；实际部署以主机安装后按现场配置导出的声明为准。打包时的 `--config` 只决定初始客户端声明，不作为主机安装配置。

主机安装器将 `<target>/device-capabilities.json` 复制到 `--client-data` 指定的目录，默认在 `<target>/client-data`。两台机器分开部署时，传递该文件并覆盖个人电脑**实际 `CAMCTL_DATA_DIR`** 内的 `device-capabilities.json`；客户端已运行时，在网页点击“重新加载能力说明”，确认加载成功。

主机修改设备配置后，重新导出到临时文件，命令成功后再替换交付文件；例如在同一个终端执行：

```sh
"$deploy_root/venv/bin/camctl" describe --config "$deploy_root/config.toml" \
  > "$deploy_root/device-capabilities.new.json" && \
  mv "$deploy_root/device-capabilities.new.json" "$deploy_root/device-capabilities.json"
```

之后重新传递到客户端实际数据目录。该命令不会自动更新之前的同步副本。客户端运行时，仅替换磁盘文件不会立即启用新说明；加载失败时查看页面提示，并保留现有数据。

## 验证与交付边界

打包入口在仓库外解压，核对文件清单，使用设备身份查询替身安装主机，并启动客户端检查网页、数据目录与能力说明加载。实际环境与检查结果见 `RELEASE.json`。这些检查不代替完整组件测试、最低 CMake 版本、Windows、ARM64、第三方主程序集成或真实相机验收。

首次安装和已有环境更新的边界不同。已有主机状态库、客户端数据库和媒体须保留；部署包及安装器不承担数据迁移。交付后的现场配置、传输与硬件联调应按对应部署流程另行核验。
