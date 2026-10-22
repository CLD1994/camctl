# Action6 普通录像演示与相机资料采集

设备的当前安排见[真机验证安排](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)。2026-10-17 的 Action6 普通录像、六个延时诊断分支及 USB 断连复现见[真机验证记录](../camctl/verification.md#action6-tx2-真机验证与暂停2026-10-17)。用户已确认[Action6 固件存在长间隔延时后 ADB 不自行重连的问题](../camctl/verification.md#action6-长间隔延时固件缺陷与验证暂停2026-10-17)，该设备的真机验证等待固件修复后再恢复，不执行下文拍摄或设备复查步骤。

ARM 上的 Action6 普通录像演示已由用户于 2026-10-11 确认正常，该次十秒演示范围已完成，不需要重复拍摄或补交报告。该次原始计划、正式报告、媒体文件和确认水位未归档；其范围与 2026-10-17 的扩大验证分别记录，详见[普通录像现场验收记录](../camctl/verification.md#action6-普通录像演示的现场验收2026-10-11)。

Action6 原生延时的正式软件执行及产物契约已定义，涵盖 STOP 响应确认、自然结束、有限取消和产物核实；这些规则是软件实现与验证范围，不代表设备已经接受 STOP、自然结束或完成写入。该设备的真机验证仍等待固件修复，尚无本契约的实机结果。OSMO 360 II 的[1 秒／300 秒历史手工演示已由用户现场确认通过](../camctl/verification.md#osmo-360-ii-最短延时手工演示的现场验收2026-10-16)；该确认针对历史请求，不代表当前最短 2 秒新请求的实机验收。OSMO 正式普通录像、原生延时、低功耗中断恢复及有限取消的 TX2 业务链现已按[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)完成；本轮启动余量软件、ARM 安装和 2 秒／300 秒新请求实机结果见[启动余量 TX2 定点验收](../camctl/verification.md#启动余量-tx2-定点验收2026-10-18)。Auto 1 秒／300 秒旧样片的实际采样间隔未独立读回；不能由其帧数确认 1 秒效果。其他范围与历史设备证据见[实施计划 T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)。Hasselblad 单张 RAW 的操作步骤见[安装演示说明](hasselblad/validation.md)，ARM 验收结果由[组件验证记录](../camctl/verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)。

真实目标主机性能测量、物理断电后的持久性验证和真实第三方传输方联调采用独立验收范围；边界见[第一版完善与验收范围](../architecture/README.md#第一版完善与验收范围)和[文件交接规格](../architecture/file-handoff.md)。本文保留相机资料采集、诊断工具说明以及 ARM 安装版 host 演示的操作步骤，设备当前结果以组件验证记录为准。

Windows 采集器保存逐次调用及设备目录观察；ARM Linux 步骤说明如何从已安装的 camctl 与 C host 开展演示。任务范围和软件验证由[相机接入设计](../superpowers/specs/2026-10-10-camctl-real-camera-demo-design.md)、[实施计划 T8](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t8-容器的四条跨组件演示链与设备采集交付)和[T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)跟踪。

| 已验收的普通录像演示 | 拍摄要求 | 后续交接 |
| --- | --- | --- |
| Action6 普通录像 | 目标 10 秒，主机发送停止 | 独立取回正式产物，经 C host 领取文件并确认报告 |
| OSMO 360 II 正式普通录像首轮 | Auto EV 0、目标 10 秒，STOP 确认后实际等待 30 秒 | 独立取回、媒体导入与 ACK 已完成；完整证据及剩余场景见[TX2 验证记录](../camctl/verification.md#tx2-正式业务验收2026-10-18-起) |
| OSMO 360 II 正式普通录像第二轮 | Manual ISO 800、目标 10 秒，STOP 确认后实际等待 30 秒 | 原片和可选预览已配对并独立交付；媒体导入、ACK 及摘要见[TX2 验证记录](../camctl/verification.md#tx2-正式业务验收2026-10-18-起) |

新增设备接入前，应先核实设置、启动、停止及文件访问的设备事实。生产任务只使用 `camctl describe` 导出的完整能力；配置和计划模板不能使候选任务变为可执行能力。命令候选和资料缺口见[相机控制交接资料](camera-control-handoff.md)，行为契约由相应[架构规格](../architecture/README.md)维护。

## 开发容器访问宿主机 ADB 服务

开发容器通过宿主机的 ADB 服务访问真实相机，统一使用 `host.docker.internal` 和端口 `5037`，避免宿主机 IP 变化影响连接：

```bash
adb -H host.docker.internal -P 5037 devices -l
adb -H host.docker.internal -P 5037 -s <serial> shell getprop ro.product.name
```

先从实际设备列表选择 serial，再核对型号和当前设备状态；后续调用均保留相同服务地址、端口和明确 serial。此方式适用于开发容器中的真实设备诊断和驱动验证；ARM 安装、目标主机性能和断电验收仍按各自范围记录。

## Windows 上采集设备资料

每款相机使用独立资料目录，记录机身型号、固件版本、使用内置存储还是 SD 卡，以及本次选择的完整参数。相机按键、手机应用和其他控制程序不另行拍摄。保留试验前已有文件，便于核对新文件的关联。

先执行 `adb version` 和 `adb devices -l`，记录完整结果。从设备列表中明确选取目标 serial，后续每次设备调用都带 `-s <serial>`。不要根据列表顺序选择设备。

### 按原始字节保存每次调用

采集器使用 Python 3.11 标准库，其运行不依赖 camctl 源码目录。它只执行命令行明确提供的一次调用，并保存原始 stdout、stderr、退出状态及耗时。调用未返回时，设备观察可从另一终端或相机界面记录；不能因为采集器仍在等待而判断设备未开始。

独立脚本为 [collect_call.py](collect_call.py)。将它复制到 Windows 的 `platform-tools` 目录。使用已创建的 Python 3.11 环境时，将下面命令中的 `py -3.11` 替换为 `.\.venv\Scripts\python.exe`，并将 `adb` 替换为 `.\adb.exe`。

每次调用的终端输出和 `call.json` 是元数据，其中 `returncode` 是被调用程序的退出码。该程序非零退出时，采集器仍能正常保存结果；Python 自身的退出码不代替被调用程序的退出码。原始响应分别保存在 `stdout.bin` 和 `stderr.bin`；查看文本时继续保留这两个原文件。
在 PowerShell 中执行，例如：

```powershell
py -3.11 collect_call.py action6/001-adb-version adb version
py -3.11 collect_call.py action6/002-devices adb devices -l
py -3.11 collect_call.py action6/003-model adb -s <serial> shell getprop ro.product.model
py -3.11 collect_call.py action6/004-firmware adb -s <serial> shell getprop ro.build.display.id
py -3.11 collect_call.py action6/005-tools adb -s <serial> shell 'for t in dji_mb_ctrl simulate_device sh mktemp find sort stat sha256sum dd rm test printf trap read; do echo TOOL=$t; command -v $t || echo NOT_FOUND; done'
```

将 `<serial>` 替换为 Action6 的实际设备标识；后续采集其他相机时使用独立目录和对应绑定。`getprop` 返回空值时，在观察记录中保存界面上显示的型号和固件，不补写推测值。工具存在只证明能找到该程序，其所需选项仍须实际核实。

### 核对 ADB 返回和字节

每款相机分别执行受控退出和字节探针：设备脚本向 stdout、stderr 各输出一个不同标记后以 `7` 退出，核对本地 ADB 的退出码和两个输出文件；另输出包含 NUL、LF、CR 及高位字节的已知序列，用 Python 的 `read_bytes()` 精确比较。`exec-out` 与 `shell -T` 分别采集，不从一种通道的结果推定另一种通道。

采集器保存的是本地 ADB 程序实际输出的字节。ADB 可能合并设备输出、丢失远端退出码或转换行尾，因此本地退出 `0` 和采集器使用二进制文件都不能单独证明设备命令成功或设备字节原样保留。保存每次实际结果及其适用环境，原文件不作行尾替换。Action6 的 Windows 实测范围见[ADB 通道核验记录](../camctl/verification.md#action6-的-windows-adb-通道核验2026-10-10)。

Windows 上取得完整媒体副本使用后文的 `adb pull` 和源端摘要核对。ARM Linux 部署仍须核实本机执行通道的退出、分流和原始字节，普通前台命令的适用条件见[ADB 调用返回契约](../architecture/adb-execution.md#第一版普通前台命令的运行假设)。

### 保存拍摄前目录

相机通过自己的 Linux 工具列举原存储范围。Action6 分别核对 `/mnt/media_rw/emulated/DCIM` 和 `/mnt/media_rw/sd/DCIM`；OSMO 核对 `/mnt/media_rw/emulated/DCIM`。使用实际存在的存储范围，记录不存在或读取失败的原结果。

```powershell
py -3.11 collect_call.py action6/010-before-internal adb -s <serial> shell 'find /mnt/media_rw/emulated/DCIM -type f -print0'
py -3.11 collect_call.py action6/011-before-sd adb -s <serial> shell 'find /mnt/media_rw/sd/DCIM -type f -print0'
```

远端 `find -print0` 使用 NUL 分隔，可以表达文件名中的空格、换行等字符；本地收到的路径字节是否原样保留，仍须按上节核实实际通道，不通过逐行文本代替原始路径。正式驱动的目录适配使用 `sort -z` 和相机 shell 的 `IFS= read -r -d ''`；还需要核实 `stat -c %s`、`sha256sum` 和 `dd` 的实际调用结果。缺少工具或选项时保留错误，供适配实现选择设备已有的工具组合。

分页试验直接使用具体文件适配生成的脚本，通过 `shell -T` 执行。核对版本头 `CAMCTL-DIRECTORY/1`、NUL 分隔路径、`MORE` 或 `END` 及末尾 NUL；后续页使用上页最后一个文件路径作为游标，直到收到 `END`。完整分页结果须与同一静止目录的原始列举一致；只收到首页不能证明全部文件已列出。

## 录像资料

### Action6 的统一诊断脚本

[action6_record_probe.py](action6_record_probe.py) 使用 Python 3.11，在一次执行中重新进入普通录像模式，依次发送 4K/30 fps、Pro 开启、Auto 曝光、5200 K 白平衡、0 EV、D-Log M、Wide、关闭增稳及高码率命令，然后试录、停止、列出新增文件、拉取新增 MP4、核对源端与副本的长度和 SHA-256，并保存 `ffprobe` 的实际媒体信息。光圈的命令与返回仍需资料方说明，诊断脚本将光圈标为未确认；它不替代包含光圈要求的完整预设验收。

当前 ARM 普通录像演示已完成，不要为重复取证启动此 Windows 诊断流程。脚本的默认模式会设置并启动一段新的普通录像；只有另行安排新的诊断时，才按本节操作。光圈与码率能力边界见[参数规格](../architecture/camera-parameters.md)。

将脚本与 [collect_call.py](collect_call.py) 放在 Windows 的 `platform-tools` 目录，使用现有 Python 3.11 环境执行。明确指定相机 serial 和已安装的 `ffprobe`；例如：

```powershell
python .\action6_record_probe.py --serial 123456789ABCDEF --ffprobe "C:\path\to\ffprobe.exe"
```

默认录像模式使用新的 `action6/record-probe-<UTC 时间>` 目录，每项调用分别保留 `call.json`、`stdout.bin` 和 `stderr.bin`。脚本检查真正的工具退出码；`dji_mb_ctrl` 必须返回唯一完整的单字节 `00`，非 `00`、缺失、多段、不完整响应或非预期 stderr 都会使脚本报错并停止。已经发送的设置保留实际效果，不自动撤销或重试。普通录像的 START 已经尝试时，脚本在收场阶段发送一次 STOP，再结束本次试验。原生延时模式的时序和停止边界见[统一采集](#action6-原生延时的统一采集)。

每次采集或复查中，每份 MP4 只下载一次，下载前后分别读取一次源长度及摘要。`video-<序号>-copy.json` 保留两组源观测、副本值、长度和摘要各自是否变化，以及副本是否与下载后的源观测一致；比对不一致时，脚本先保存该记录，再报告实际数值并结束。后续源查询失败或结果无效时直接报错，不使用早期值代替。长度和摘要是分开的查询，不能视为同一时刻的文件快照；这些检查不证明源文件以后保持不变，也不证明本次全部文件已写完。

`simulate_device` 的属性匹配和服务连接、注册日志不证明实际码率。普通录像驱动在本地工具、通道、退出状态和输出完整性检查通过时记录 `setting_command_accepted`，只表示码率命令已接受，不表示相机实际码率独立读回。十秒从启动调用返回后计算；没有实际开始、结束观察时，不把这段主机等待认作十秒有效视频。成功执行只证明本次诊断步骤及副本一致性检查完成，`summary.json` 保留实际媒体属性；设置生效、全部产物写完、驱动启用和 ARM Linux 完整验收继续按相应设备契约核实。

脚本响应与副本比对的单元测试可在容器中使用 `apps/camctl/.venv/bin/python -m unittest discover -s docs/hardware/tests/unit` 执行；测试使用采集样式和故障输入，不连接相机。

集成测试在容器中另行运行 `apps/camctl/.venv/bin/python -m unittest discover -s docs/hardware/tests/integration`。它通过真实采集器、隔离目录和受命令接口约束的 ADB、ffprobe 替身核对完整流程，目录调用使用容器的 `sh` 和 `find`；可控时钟只缩短软件测试，不提供设备时长证据。

### 原始调用与设备观察

从交接资料中选择该相机的一套完整录像设置，逐条执行并分别保存调用结果。互斥选项只选择一项，不依次执行整张候选命令表。记录自动或手动曝光、分辨率及对应的固定设置；Action6 的画面、增稳、光圈和码率分别记录实际选择。

每条设备侧设置命令通过 `collect_call.py <独立目录> adb -s <serial> shell <命令及参数>` 执行。设置明确失败时先保存该结果，结束这次试验，不继续启动。

Action6 普通录像的启动、停止候选为：

```powershell
py -3.11 collect_call.py action6/020-record-start adb -s <serial> shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01
py -3.11 collect_call.py action6/021-record-stop adb -s <serial> shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00
```

在设备实际开始录像后等待 10 秒，再执行停止；记录实际开始和实际停止的观察来源与时刻。若只能确定启动调用已返回，记录这个事实及停止调用间隔，真实录像时长保持未知。Action6 的启动候选需要核实其在实际固件中的效力。

停止后重复保存目录，识别新增路径。记录文件开始出现、最后写完、相机退出录像状态的观察及先后关系；继续保存必要的后续目录和文件信息，直到能够说明本次全部文件已齐备。文件出现或短时间内长度不变，各自只能证明当时的文件状态。

## 延时摄影资料

OSMO 最短延时手工演示的历史范围见[现场验收记录](../camctl/verification.md#osmo-360-ii-最短延时手工演示的现场验收2026-10-16)，较长间隔下的连接行为见[低功耗现场确认](../camctl/verification.md#osmo-360-ii-延时低功耗与-adb-恢复的现场确认2026-10-17)。OSMO TX2 正式业务验收已经完成；当前进度只由[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)维护，拍摄与交付实测见[验证记录](../camctl/verification.md#tx2-正式业务验收2026-10-18-起)。Auto 1 秒／300 秒样片没有实际采样间隔独立读回。Action6 的手工专项已取得固件缺陷结论，后续真机诊断等待修复；剩余设备资料及候选命令由[相机控制交接资料](camera-control-handoff.md)维护，推进计划见[T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t9-设备事实补齐与-arm-linux-完整验收)。

两款使用相同的[采集时长域和严格组合关系](camera-control-handoff.md#间隔与采集时长限制)，间隔按型号定义。当前最短实机演示目标分别为 Action6 的 1 秒／300 秒和 OSMO 的 2 秒／300 秒；OSMO 2 秒／300 秒新请求的 TX2 实测已完成，结果与边界见[本轮定点验收](../camctl/verification.md#启动余量-tx2-定点验收2026-10-18)。下文 1 秒／300 秒 OSMO 请求是历史演示记录，不属于当前 OSMO 新请求允许域。模式、曝光、输出与编码分别沿自身契约。Action6 新诊断的输入、完整映射和旧记录读取由[驱动计划 D1A](../superpowers/plans/2026-10-16-osmo360ii-timelapse-driver.md#d1a-action6-新诊断与旧记录只读复查)同步。新诊断省略时间选项时从同源资源派生最短组合；下文秒级结果保留为既有记录和只读复查背景。

### 延时采集期间的低功耗与连接观察

OSMO 360 II 的[用户现场确认](../camctl/verification.md#osmo-360-ii-延时低功耗与-adb-恢复的现场确认2026-10-17)说明，较长间隔采集可能伴随低功耗及 ADB 断开，并在采集完成后恢复连接。观察时分别记录相机采集状态、功率变化和 USB／ADB 可用性；通信不可用本身不证明采集失败或结束。

采集期间通过相机屏幕、功率测量和主机 USB 日志观察，不因断连补发 START、发送未核实 STOP 或立即重启。以相机实际完成为依据继续观察连接恢复；主机等待到目标时长、短暂重枚举和 ADB 恢复均不替代媒体与集合完整性检查。OSMO 的约 5 秒恢复观察值不直接成为固定超时或文件处理余量；Action6 当前固件不自行重连的问题按[专项结论](../camctl/verification.md#action6-长间隔延时固件缺陷与验证暂停2026-10-17)单独处理。

### OSMO 360 II 当前 START 命令

新验证请求按[OSMO START 调用](../camctl/timelapse-command-encoding.md#osmo-start-调用)省略可选响应等待参数。在 TX2 上执行：

```bash
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01'
```

下文两套手工顺序保留当时的完整命令，用于核对既有现场记录；新验证使用本节的 START 命令，其余设置按实际请求选择。

### OSMO 360 II 的最短实机演示

本节保留此前 OSMO 手工演示的历史请求：采集间隔为 1 s、采集时长为 5 min（300 s），仅输出视频。当前[参数限制](camera-control-handoff.md#间隔与采集时长限制)规定 OSMO 新请求的最短间隔为 2 s；下文的 1 s 请求和命令仅用于理解历史记录，不是当前合法的新请求。当前最短目标为 2 s／300 s，其实机结果见[本轮定点验收](../camctl/verification.md#启动余量-tx2-定点验收2026-10-18)。其余模式、分辨率和曝光设置沿用当时执行记录，实际参数仍需核实。

本节记录此前通过用户整体确认的历史 OSMO 1 秒／300 秒手工演示；完整命令和当时未归档的参数读回、计时范围仍见[历史验收记录](../camctl/verification.md#osmo-360-ii-最短延时手工演示的现场验收2026-10-16)。该历史确认不覆盖当前 2 秒新请求；当前请求的实机范围与边界见[启动余量定点验收记录](../camctl/verification.md#启动余量-tx2-定点验收2026-10-18)。新代码的 TX2 正式业务验收已按[2026-10-18 完成设计](../superpowers/specs/2026-10-18-osmo360ii-capture-completion-design.md)和[完成契约计划任务 6](../superpowers/plans/2026-10-18-osmo360ii-capture-completion.md#任务-6tx2-正式链与定点异常验收)完成；实际场景和结果见[验证记录](../camctl/verification.md#tx2-正式业务验收2026-10-18-起)。Auto 1 秒／300 秒这一轮的实际采样间隔未独立读回，不据帧数确认 1 秒效果。300 秒表示采集时长，命令响应、自然余量、文件等待与结果读取按完成设计分别处理。

### OSMO 360 II 的最短手工演示顺序

以下保留用户现场整体确认通过的历史 1 秒／300 秒演示请求顺序；确认范围由[现场验收记录](../camctl/verification.md#osmo-360-ii-最短延时手工演示的现场验收2026-10-16)定义。这些命令与参数不属于当前 OSMO 新请求域，也不能证明当前 2 秒请求已通过实机复测。该顺序与下文既有 30 秒／600 秒试验分别保留，不使用旧试验的 610 秒观察安排判定其他请求。

在 TX2 终端先用 `adb devices -l` 确认目标 serial，下面沿用原绑定 `123456789ABCDEF`，实际不同时统一替换。逐条执行：仅在本地退出码为 `0`、没有未解释报错，并收到唯一完整的 `Resp message, len = 1, data: 00` 后继续；出现错误或响应不明确时停止后续派发，不自动重试。

```bash
# 1. 模式切换准备，与下一条配合进入全景静止延时模式。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101'
# 2. 选择全景静止延时模式。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b'
# 3. 设置输出为 8K / 30 fps，输出帧率与采集间隔分别设置。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000'
# 4. 打开 Pro，准备设置手动曝光。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101'
# 5. 切换至 M 档曝光。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 1e 0400'
# 6. 设置 ISO 800。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 2A 06'
# 7. 设置快门为 1/60 秒。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 28 013c8000'
# 8. 设置白平衡为 5200 K。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2c 0634000000'
# 9. 请求设置 1 秒采集间隔、300 秒采集时长及仅视频输出。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407000a002c01000000000000000001'
# 10. 发送一次延时摄影启动请求，保留原响应等待选项。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 -o 20000000 01'
```

新验证的 START 使用[当前命令](#osmo-360-ii-当前-start-命令)。启动后的观察覆盖至少 300 秒采集目标及后续视频处理；等待到期或 `00` 响应不能单独替代完成判定。结果未知或 USB 断连时保留原请求与现场材料，不补发 START。当前已通过的演示无需重复采集。

### OSMO 360 II 的 TX2 手工复验顺序

以下保留 2026-10-15 TX2 复验采用的执行顺序，作用依据[全景静止延时控制资料](camera-control-handoff.md#osmo-360--360-ii-全景静止延时摄影)说明，实际参数尚未逐项回读确认。间隔与采集时长的完整允许值由[参数限制](camera-control-handoff.md#间隔与采集时长限制)维护；这里保留当时使用的 30 s / 600 s / 仅视频完整预设，作为既有试验记录。

这套记录对应的操作约定为：命令在 TX2 终端逐条执行，确认 serial 为目标相机。每条执行后检查本地 `adb` 的退出码和标准错误；仅在退出码为 `0`、没有未解释的报错，并收到唯一完整的 `Resp message, len = 1, data: 00` 后执行下一条。错误、响应缺失或含义不明时停止后续派发，不自动重试。

```bash
# 1. 模式切换准备：与下一条配合进入全景静止延时；资料未单独解释本条控制字段。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101'
# 2. 选择全景静止延时模式，为后续设置和启动准备拍摄模式。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b'
# 3. 设置输出 8K / 30 fps；输出帧率与采集间隔是不同参数。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000'
# 4. 打开 Pro，准备按资料中的手动曝光流程设置参数。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101'
# 5. 使用 M 档，为手动 ISO 和快门建立曝光模式。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 1e 0400'
# 6. 固定 ISO 800，保持本次复验的感光度条件。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 2A 06'
# 7. 固定快门 1/60 秒，保持每次采集的曝光时间设置。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 28 013c8000'
# 8. 固定白平衡 5200 K，减少自动白平衡造成的帧间色彩变化。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2c 0634000000'
# 9. 一次设置 30 秒间隔、600 秒采集时长和仅视频输出。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c015802000000000000000001'
# 10. 发送一次启动请求；-o 20000000 是响应等待参数，不是采集时长。
adb -s 123456789ABCDEF shell -T 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 -o 20000000 01'
```

这套 30 s / 600 s 复验的观察等待安排为：第 10 条执行后等待至少 610 秒再进行结果观察。该安排不是实测的启动至文件完成耗时，也不构成已核实的驱动处理余量或自然结束保证。上次完整观察期间没有发送停止命令，也没有在 USB 断连后再次发送启动命令；复查原试验时沿用已有记录，不补发 START。

`00` 只作为这套手工顺序继续执行的依据，不能单独证明参数生效、相机实际开始、拍摄完成或文件写完。USB 断连也不能单独证明上述任一结果；较长间隔低功耗场景的后续说明见[现场确认](../camctl/verification.md#osmo-360-ii-延时低功耗与-adb-恢复的现场确认2026-10-17)。结果判定仍需区分实际参数、采集状态、本次文件归属、文件写完和产物集合齐备。

### Action6 固件缺陷与复测前提

用户已完成长间隔延时的手工专项，确认当前固件在该场景下不会自行恢复 ADB 连接，详见[用户现场记录](../camctl/verification.md#action6-长间隔延时固件缺陷与验证暂停2026-10-17)。本次提供的用例为 30 秒间隔、300 秒（5 分钟）采集、仅视频、手动 ISO 800；原命令和历史失败记录保留，不作为继续运行当前固件的操作入口。

Action6 的后续真机验证按[T9 暂停及恢复前提](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)等待固件修复。恢复安排确定后，先核对实际修复固件、型号和现场状态，再复核长间隔采集完成后的自动重连及媒体完整性；不采用 OSMO 的约 5 秒作为 Action6 的恢复保证。软件 STOP 命令、严格响应和有限执行契约见[Action6 原生有限延时任务](../architecture/camera-capture.md#action6-原生有限延时任务)；设备是否接受并执行 STOP、是否自然结束及产物是否写完仍须另行实机核验。

### Action6 原生延时的统一采集

`action6_record_probe.py --capture timelapse` 使用 Action6 自身的 4K／30 fps、Auto、仅视频设置。新诊断的 `--timelapse-interval-s` 和 `--timelapse-duration-s` 接受精确秒数，包括数学值等价的 `1.0`、`1e0`；允许值和严格关系统一见[时间参数](camera-control-handoff.md#间隔与采集时长限制)。省略的时间字段从生成资源中的最短组合派生，当前为间隔 1 秒、采集时长 300 秒。30 秒实验时长、等值、反向关系和邻近但不相等的数值均在任何工具调用前拒绝。

诊断按[候选编码契约](../camctl/timelapse-command-encoding.md)生成完整设置，模板或资源缺失、结构损坏及表示超界也在运行 ffprobe 或 ADB 前拒绝。记录保存精确 JSON 数字、完整负载、`encoding_format`、`template_identity`、原完整模板和样例推导依据。默认值只服务于该诊断入口，不为生产参数补默认值。

脚本只发送一次 START，没有 Action6 延时 STOP，不会自动重发 START；调用异常后仍无法据此判断设备是否开始。本机时钟、调用耗时、文件出现或副本检查都不能证明设备开始、自然结束、写完或产物集合已固定。当前设备访问按[T9 暂停安排](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)处理；本节保留工具行为说明。既有 Windows 设备观察见[原生延时记录](../camctl/verification.md#action6-的-windows-原生延时采样2026-10-10)，本次 TX2 结果见[2026-10-17 验证记录](../camctl/verification.md#action6-tx2-真机验证与暂停2026-10-17)。

### Action6 独立诊断交付

Windows 诊断机使用 Python 3.11，并将下列四个文件放在同一目录：

- [action6_record_probe.py](action6_record_probe.py)：参数校验、原记录读取、采样与副本检查。
- [collect_call.py](collect_call.py)：分别保存每次调用的参数、退出状态与原始输出。
- [timelapse-time-parameters.json](timelapse-time-parameters.json)：从生产权威时间定义生成的有限时间域和最短组合。
- [action6_timelapse_candidate.py](action6_timelapse_candidate.py)：从生产完整模板与纯编码函数生成的标准库模块。

交付文件不要求 Windows 诊断机存在源码工作区、安装 camctl 或运行生成器。ADB 与 ffprobe 沿原选项指定。运行 `python .\action6_record_probe.py --help` 可查看时间选项和只读入口。

维护者在源码工作区运行以下检查，验证实际交付文件与生产权威来源一致：

```bash
apps/camctl/.venv311/bin/python apps/camctl/scripts/render_timelapse_time_parameters.py --check
apps/camctl/.venv311/bin/python apps/camctl/scripts/render_action6_diagnostic_candidate.py --check
```

第二个生成器只提取当前生产模块的已核对纯设置依赖及原异常声明；按原导入的模块、相对层级、名称和别名核对依赖；同名绑定、闭包内建名称重绑定、闭包内嵌套导入及其他未知依赖或源码形状均明确拒绝，维护者需要重新核对独立交付边界。软件隔离目录运行及工具替身验证不表示 Windows 本机、ARM 或真实设备已验收。

### Action6 秒级延时试验

已有 1800 秒原预设及 30 秒、10 秒实验记录按原请求、原负载和原设置顺序保留，只用于只读复查。具体依据见[相机控制交接资料](camera-control-handoff.md)及[秒级候选试验记录](../camctl/verification.md#30-秒候选与完整设置顺序的试验)。这些记录不扩大当前新请求的时间域，也不提供相机已经自然结束或文件写完的保证。

### Action6 延时采样后的只读复查

本节是另行恢复设备验证后的工具说明。暂停期间只核对已有本地材料，不运行 `--observe`；该入口会访问相机，不属于离线材料检查。恢复条件见[T9 当前安排](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)。

恢复后，对已经存在并保存了 `capture-observation.json` 的原采样目录，如需补充目录观察，可使用同一个脚本的 `--observe <原采集目录>`。该入口从原始记录读取启动前目录，新增观察保存在原目录下，不覆盖原记录，不发送设置、START、STOP 或删除命令。它可能列举文件、下载文件并运行媒体检查，因此只用于原试验资料复查，不能当成新拍摄或恢复采集。

```powershell
python .\action6_record_probe.py --observe ".\action6\timelapse-probe-<原 UTC 时间>" --adb .\adb.exe --serial 123456789ABCDEF --ffprobe "C:\path\to\ffprobe.exe"
```

`--observe` 与 `--capture` 互斥，只接受原生延时记录，不能另带间隔或时长选项。脚本校验原模式、计时记录、START、启动前目录调用的 serial、退出状态及目录结果；原始记录缺失或无效时，不会继续设备观察。原 START 异常而尚未保存 `capture-observation.json` 的记录应只检查原始调用材料，不得补发 START。

新格式记录按保存的精确参数、完整负载、格式身份和原模板核对，不读取当前时间资源或按新请求 Schema 重判。旧记录沿[原记录格式与设置布局](#action6-原记录格式与设置布局)识别；原始事实不完整时不补默认值。复查结果分别记录本次目录差集、下载与媒体检查，不能更新原试验的开始、结束、文件写完或产物集合结论。既有复查语义和观察值见[验证记录](../camctl/verification.md#action6-的-windows-原生延时采样2026-10-10)。

### Action6 原记录格式与设置布局

只读入口将 observation 字段和原设置目录一起核对，再读取对应调用。下表各分区均要求 `01-mode`、`02-resolution`，以及同一相机的 `12-start`、`11-before-internal` 和 `11-before-sd` 原调用。

分区|observation 字段|其余设置目录
-|-|-
A0，最早记录|采集模式、采样时刻和两项计时，没有 `observation_only` 或参数 metadata。|`03-exposure`、`04-preset`。
A1，带采集标志的最早记录|与 A0 相同，增加 `observation_only=false`。|与 A0 相同。
B0，两次完整预设的旧实验记录|`observation_only=false`，`requested_params`、`preset_payload`、`preset_basis` 全部存在，没有 v1 字段。|`03-timing`、`04-exposure`、`05-output`；两次完整负载一致。
B1，一次完整预设的旧实验记录|与 B0 相同。|`03-timing`、`04-exposure`。
C，第一版候选记录|`observation_only=false`，三项参数 metadata 和 `encoding_format`、`template_identity`、`template_payload` 全部存在。|与 B1 相同。

A0／A1 的完整原调用证明 4K／30 fps、Auto、仅视频和 8 秒／1800 秒；B0／B1 保存原 8 秒间隔及 1—1800 整数秒实验时长。C 沿原精确参数和固定格式解释，不因当前时间允许值变化而改写原记录。原文件保持原字节，当前时间资源和编码模块缺失不影响这些只读路径。

各设置和 START 调用的原 argv 必须是六个字符串组成的 ADB shell 包装，工具名非空、serial 一致、退出状态为整数 `0`、标准错误为空，并保存唯一完整 `00` 响应。两个原基准分别核对目录脚本、原始输出和路径范围，可靠空目录与不存在分别处理，不能同时不存在。`summary.json`、`11-before-storage.json`、后采样及媒体资料不能补齐缺失的原调用或覆盖原基准。

部分 metadata、部分或未知 v1 身份、旧新设置目录混合，以及必要原调用缺失或矛盾，都会在任何工具调用前拒绝。B0 的 `05-output` 存在时必须完整且与原时间负载一致；读取器不会因该目录内容损坏而按 B1 继续。旧格式没有独立版本字段，因此无法推断一个已经整目录删除的 `05-output` 原本是否存在。无关媒体和后续 `observation-*` 目录不参与设置布局判定。

### 分别保存原始调用

以下启动与停止候选仅供未来单独安排的设备观察使用，执行前须满足[T9 当前安排](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#当前真机验证安排)；暂停期间不派发，对既有试验不重发 START。

Action6 原生延时的 START 候选及原表组合见[相机控制交接资料](camera-control-handoff.md#action--action6-静止延时摄影)。

OSMO 使用独立资料目录和自己的 serial。记录启动调用是在发送后、实际开始后，还是整项任务完成后返回。等待预设的真实持续时间，观察相机是否自行结束、是否进入视频处理阶段，以及最终全部文件何时写完。

OSMO 的停止候选和适用前提见[相机控制交接资料](camera-control-handoff.md#osmo-360--360-ii-全景静止延时摄影)。现有 OSMO 候选不能证明设备已经接受或停止采集；Action6 延时 STOP 的软件命令与响应规则见[正式任务契约](../architecture/camera-capture.md#action6-原生有限延时任务)，设备实际接受并执行该命令仍待真机核验。

设备接受有限目标并自行结束时，保存实际结束或明确等待假设所需的证据。由主机负责结束时，保存实际开始、计时和停止证据。启动调用一直等待整项任务结束的情况，记录完整耗时及处理阶段，用于确定驱动的有限完整调用期限。

## 文件访问与资料交回

对每次任务识别出的新增文件，分别保存精确路径、实际格式、长度及源端 SHA-256。路径中的特殊字符必须作为同一个远端参数传递。检查照片、视频及可能的配对文件，依据实际结果记录数量，不用持续时间除以间隔代替设备计数。

使用 `adb -s <serial> pull <精确路径> <独立本地路径>` 取得完整副本，再用 `Get-FileHash -Algorithm SHA256` 核对本地文件。保存源文件的长度和摘要响应、pull 的完整结果、本地长度和摘要。原片继续保留；显式删除试验仅操作用户指定的试验文件，并另存删除及存在性核实结果。

将每款相机的独立目录和观察记录一并交回。观察记录说明每次设置、开始、结束及文件写完的实际现象，关联对应调用目录。尚未观察到的事实写为未知。

| 资料 | 用于确定的契约 |
| --- | --- |
| 型号、固件、serial、存储范围及工具结果 | 命令适用范围、明确绑定及文件适配 |
| 每条设置、启动和停止的原始调用结果 | 设置与启动分别判断、实际启动或停止、错误及未知分区 |
| 延时自然结束或主动停止、视频处理及全部文件写完的时序 | 结束方式、完成保证、必要等待余量和完整调用期限 |
| 前后目录、新文件的实际格式、长度及源摘要 | 原目录基准、文件归属、必要产物与完整读取 |
| 完成文件读取期间执行必要停止的实际结果 | 文件读取与控制的设备兼容性 |

## 从安装包准备计划和配置

先按[独立安装与主程序联调](../../apps/host-demo/docs/camctl-integration.md#一准备运行账户和交付文件)在目标主机安装 Python 3.11、配套依赖、camctl wheel 和 host-demo，并准备可用的 ADB。在个人电脑按[客户端运行指南](../client/running.md)启动客户端，供后续导入报告和视频。以下主机步骤使用已安装的程序，不要求目标机存在源码仓库。

在终端 B 从 camctl 包提取演示资源。提取目录为 `$HOME/.camctl/camera-demo`，已有同名文件会使操作失败，以便保留人工填写的 serial 和已经生成的计划。

```bash
"$HOME/.camctl/venv/bin/python" - <<'PY'
from pathlib import Path
from camctl.resources import available_resources, resource_bytes

root = Path.home() / ".camctl" / "camera-demo"
root.mkdir(parents=True, exist_ok=True)
prefix = "examples/camera-demo/"
for name in sorted(available_resources()):
    if name.startswith(prefix):
        target = root / name.removeprefix(prefix)
        with target.open("xb") as file:
            file.write(resource_bytes(name))
print(root)
PY
```

提取出的 [osmo360ii-config.toml](../camctl/examples/camera-demo/osmo360ii-config.toml) 用于后续 [OSMO 最短实机演示](#osmo-360-ii-的最短实机演示)，设备标识为 `osmo360ii`；将其中 `REPLACE_OSMO360II_SERIAL` 替换为 `adb devices -l` 列出的实际 serial。该样例使用当前运行账户的默认路径。已有部署将 OSMO 设备项合并到原配置，保留原设备、状态库和交接目录。[E3 的设备与正式软件前置](../superpowers/plans/2026-10-16-osmo360ii-timelapse-delivery.md#e3-arm-最短实机演示)满足后，初始化、实际 `describe` 和 host 统一使用这份现场配置；已有状态库不重新初始化。

提取目录保留 Action6 延时与 OSMO 的计划模板。Action6 延时现有正式软件任务；这些模板用于提供示例请求，不代表真机验收。当前演示使用 `action6-record`、`obtain` 和 `report-ack`。在提取的 `config.toml` 中，将 Action6 的 serial 替换为 `adb devices -l` 列出的实际标识，`device_id` 为 `action6`。配置只包含这款相机，不要求填写 OSMO 绑定；样例省略 `[paths]`，使用运行账户的默认部署目录。已有部署保留原配置，将 Action6 设备项合并到实际使用的配置中。初始化、能力导出和 host 始终使用同一份配置：

```bash
"$HOME/.camctl/venv/bin/camctl" init --config "$HOME/.camctl/camera-demo/config.toml"
"$HOME/.camctl/venv/bin/camctl" describe --config "$HOME/.camctl/camera-demo/config.toml" \
  > "$HOME/.camctl/camera-demo/capabilities.json"
```

`prepare-plan.py` 从安装包读取计划模板，生成新的正整数请求身份和 UTC 时间，并按刚导出的能力 Schema 校验拍摄参数。默认安排在生成后 10 秒开始，允许迟到 30 秒。每份新计划选择尚不存在的输出文件名并在提交前生成；原键重送时继续使用原文件。内置 Action6 驱动导出普通录像和原生有限延时能力；其余未启用的拍摄候选仍等待各自设备接入。实际能力不支持计划时，生成器在写入前拒绝生成，不能用候选 Schema 代替实际 `describe`。

Action6 录像模板按[参数规则](../architecture/camera-parameters.md)显式选择 `aperture=f2.8` 和 `bitrate=high`。新计划必须包含两项；`prepare-plan.py` 依据刚导出的实际 Schema 校验缺失、空值和非法选项。光圈响应未知或调用失败会保留在设置调用历史中，收场后继续后续设置和 START；码率调用通过本地完整性检查时只记录命令已接受，不代表实际码率独立读回。

正式驱动的停止及文件完成规则见[Action6 录像规格](../architecture/camera-recording.md#action6-普通录像的运行假设)，本文不重复定义其确认语义。当前 ARM 普通录像演示已获用户整体确认；原始报告、媒体及确认水位未归档，因此不据此填写具体响应、计时或媒体测量值。

上述命令展示首次部署。已有部署合并设备项后，将本节所有 `--config` 路径替换为实际沿用的配置；配置覆盖 `[paths]` 时，host 同时传入对应的 `--ready <绝对路径>` 和 `--processing <绝对路径>`，初始化和 CLI 使用同一状态库与交接目录。

打开终端 A，运行 `"$HOME/.camctl/host/bin/host-demo" --config "$HOME/.camctl/camera-demo/config.toml"`。保持它运行，在终端 B 生成一份拍摄计划：

```bash
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" action6-record \
  --capabilities "$HOME/.camctl/camera-demo/capabilities.json" \
  --output "$HOME/.camctl/camera-demo/capture-plan.json"
```

将生成器打印的 `submit <绝对路径>` 整行复制到终端 A。host 交互命令不展开 `$HOME` 或 `~`，路径外不加引号。此计划要求 Action6 普通录像 10 秒；拍摄参数经本次实际能力说明校验。后续取回和报告确认各自生成并提交独立计划。

拍摄结束后在终端 A 输入 `claim`，host 将待传文件移到 `processing`。将其中的状态报告保持原文件名和原始字节复制到个人电脑，在客户端“文件导入”中选择报告，等待导入成功。主机领取与客户端导入是两个独立步骤。

沿用已有主机状态库时，新客户端可能尚无主机已经确认的历史。页面提示历史未补齐或“累计确认依据”暂无时，先通过客户端“准备状态同步”生成独立计划；需要全量补齐时选择“重新获取完整状态”。将导出的计划复制到主机，在终端 A 提交，再领取并导入补齐报告。客户端接受补齐报告后，才使用页面提供的累计确认依据继续取回和确认；缺口报告的身份不能用作 ACK。具体操作见[状态同步](../client/files-and-status.md#准备状态同步)。

从本次 `request_id` 对应计划中取得拍摄动作的 `action_instance_id`；拍摄动作应为 `succeeded`，正式产物应属于这个原动作。将此身份填入独立取回计划，并从客户端页面的“累计确认依据”取得可靠接受的报告 ID。下例中的 `123` 和 `456` 分别替换为原拍摄动作身份和该报告 ID：

```bash
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" obtain \
  --source-action-id 123 --last-report-id 456 \
  --output "$HOME/.camctl/camera-demo/obtain-plan.json"
```

按打印的路径在终端 A 提交，等待取回结束并再次 `claim`。核对取回报告的 `deliveries` 与领取文件的文件名、长度及 SHA-256，再将报告和视频保持原名原字节递交到个人电脑，在客户端“文件导入”中选择它们并核对导入结果。领取视频本身不确认报告；`last_report_id` 只采用客户端可靠保存的累计位置。用更新后的“累计确认依据”生成最后的确认计划，再提交给 host：

```bash
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" report-ack \
  --last-report-id 789 --output "$HOME/.camctl/camera-demo/report-ack-plan.json"
```

将 `789` 替换为客户端此时显示的累计确认依据。最后核对 camctl 已吸收该确认。下面的只读检查使用默认状态库；已有部署将第一个路径替换为配置的实际 `state_db`，第二个路径替换为这份已成功导入报告的主机文件路径：

```bash
"$HOME/.camctl/venv/bin/python" - "$HOME/.camctl/state.db" \
  "$HOME/.camctl/processing/status-report-实际文件名.json" <<'PY'
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

database = Path(sys.argv[1]).resolve(strict=True)
report = json.loads(Path(sys.argv[2]).read_bytes())
with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
    watermark, = connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()
print("主机确认水位：", watermark, "；已导入报告水位：", report["to_wm"])
print("累计确认已吸收" if watermark >= report["to_wm"] else "尚未吸收，请等待后复查")
PY
```

保留本次计划、客户端导入凭据、最终报告及源文件信息。host 的 `logs` 命令用于查看 CLI 调用和收场；受理或执行失败以正式结果及报告中的错误为依据。

## OSMO 正式计划与分段验收

OSMO 的现场安排和分层验收项见[分段计划 P9](../superpowers/plans/2026-10-21-osmo360ii-record-segmentation.md#p9按独立证据层次完成-arm-与实机验收)。使用上节从安装包提取的资源，将 OSMO 绑定合并到实际部署配置，初始化、能力导出和 host 使用同一配置及状态库。以下示例采用提取出的 `osmo360ii-config.toml`；已有部署使用实际配置路径。

在终端 B 导出能力，再生成普通录像计划：

```bash
"$HOME/.camctl/venv/bin/camctl" describe \
  --config "$HOME/.camctl/camera-demo/osmo360ii-config.toml" \
  > "$HOME/.camctl/camera-demo/osmo-capabilities.json"
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" osmo360ii-record \
  --capabilities "$HOME/.camctl/camera-demo/osmo-capabilities.json" \
  --device-id osmo360ii \
  --output "$HOME/.camctl/camera-demo/osmo-record-plan.json"
```

`prepare-plan.py` 使用本次安装包中的正式模板和刚导出的参数 Schema 校验，然后生成独立请求身份与 UTC 时间。原生延时使用样例名 `osmo360ii-timelapse` 和独立输出文件名；自定义参数可由客户端按同一能力说明编辑、校验并导出。生成后将打印的完整 `submit` 命令交给使用同一配置的 host；重送沿用原计划文件。

拍摄报告保留每份合格原片及其独立预览身份。默认取回和预览取回消费来源已保存的范围；需要范围外文件时，由客户端以明确 `output_ids` 导出独立取回计划。来源缺段失败与前缀文件成功交付分别核对。每份文件领取后保留实际名称、长度、SHA-256、output／delivery 关联、客户端导入凭据和报告 ACK。

本地副本的实际时长、双路视频、解码和容限采用[分段媒体验收工具](osmo360ii-segment-media-validation.md#运行工具)。验收环境单独部署 `validate_osmo_segments.py` 与其权威依赖清单，准备 Python 3.11、ffprobe／ffmpeg 后，按该专题建立本地清单并运行；工具及其依赖不加入 camctl 运行环境。软件安装链、ARM 部署和真实媒体分别记录证据。

## ARM Linux 的完整验收

本节对应的 ARM Action6 普通录像演示已由用户现场确认正常，当前 T9 演示范围完成；用户不需要重新拍摄或补交报告。现场结果和未归档材料边界见[组件验证记录](../camctl/verification.md#action6-普通录像演示的现场验收2026-10-11)。本节以下内容是操作流程和验收项的参考，供另行安排的演示使用，不是当前待办。

独立部署时安装正式发行物，显式配置 Action6 驱动及 ADB 绑定，初始化后导出 `describe`，并只按实际导出的能力生成计划。通过 C host 提交录像、独立取回和报告确认计划；从原拍摄动作选择正式产物，并分别记录文件领取与报告确认。有关状态流转和文件交接的契约以[正式产物与取回规格](../architecture/outputs.md)及[状态同步规格](../architecture/README.md)为准。

核对拍摄与取回成功、产物属于原拍摄动作、完整交付发布到 `ready`、host 领取到 `processing`，以及领取文件的实际长度和 SHA-256 与源文件相符。领取并核验状态报告后提交累计报告 ACK，再核对确认水位。视频收到与报告确认分别记录；两种确认完成后，相机原片仍存在。

在装有 `ffprobe` 的主机或个人电脑检查领取的 MP4，将下例路径替换为实际副本：

```bash
ffprobe -v error -show_entries \
  'format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,duration' \
  -of json /absolute/path/to/received.MP4
```

如另行开展包含媒体属性测量的演示，保存工具实际输出并按[录像规格](../architecture/camera-recording.md#action6-普通录像的运行假设)核对；不能从请求参数补写媒体属性。当前用户确认的演示没有归档这些原始测量材料。

另行安排的新验收应保留环境、调用、动作与产物关联、交付文件及报告的实际证据。未实际观察到的参数、写完条件和媒体值应明确保持未知。软件替身与目标设备证据分别见[组件验证记录](../camctl/verification.md)，不能互相替代。
