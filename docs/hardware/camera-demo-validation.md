# 双相机资料采集与完整演示验收

本流程用于 Action6 和 OSMO 360 II。Windows 负责试验 ADB 命令并采集原始设备资料；完整部署验收在 ARM Linux 上完成。任务范围及完成依据见[双相机接入设计](../superpowers/specs/2026-10-10-camctl-real-camera-demo-design.md)，软件和设备门禁分别由[实施计划 T8、T9](../superpowers/plans/2026-10-10-camctl-real-camera-demo.md#t8-容器的四条跨组件演示链与设备采集交付)跟踪。

| 相机 | 录像 | 原生延时摄影 |
| --- | --- | --- |
| Action6 | 目标 10 秒，主机发送停止 | 间隔 8 秒，持续 30 分钟，仅视频 |
| OSMO 360 II | 目标 10 秒，主机发送停止 | 间隔 30 秒，持续 10 分钟，仅视频 |

设备试验先取得设置、启动、停止及文件访问的实际响应，确定每种返回能够证明什么。正式任务只使用 `camctl describe` 导出的完整能力；尚未具有响应或结束契约的任务保持候选状态。命令候选和相互约束见[相机控制交接资料](camera-control-handoff.md)。

## Windows 上先采集一款相机

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

将 `<serial>` 替换为实际设备标识；OSMO 的资料使用另一个目录。`getprop` 返回空值时，在观察记录中保存界面上显示的型号和固件，不补写推测值。工具存在只证明能找到该程序，其所需选项仍须实际核实。

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

将脚本与 [collect_call.py](collect_call.py) 放在 Windows 的 `platform-tools` 目录，使用现有 Python 3.11 环境执行。明确指定相机 serial 和已安装的 `ffprobe`；例如：

```powershell
python .\action6_record_probe.py --serial 123456789ABCDEF --ffprobe "C:\path\to\ffprobe.exe"
```

每次执行使用新的 `action6/record-probe-<UTC 时间>` 目录，每项调用分别保留 `call.json`、`stdout.bin` 和 `stderr.bin`。脚本检查真正的工具退出码；`dji_mb_ctrl` 必须返回唯一完整的单字节 `00`，非 `00`、缺失、多段、不完整响应或非预期 stderr 都会使脚本报错并停止。已经发送的设置保留实际效果，不自动撤销或重试。START 已经尝试时，脚本在收场阶段发送一次 STOP，再结束本次试验。

每份 MP4 只下载一次，下载前后分别读取一次源长度及摘要。`video-<序号>-copy.json` 保留两组源观测、副本值、长度和摘要各自是否变化，以及副本是否与下载后的源观测一致；比对不一致时，脚本先保存该记录，再报告实际数值并结束。后续源查询失败或结果无效时直接报错，不使用早期值代替。长度和摘要是分开的查询，不能视为同一时刻的文件快照；这些检查不证明源文件以后保持不变，也不证明本次全部文件已写完。

`simulate_device` 只核对已有样例中的属性匹配和服务连接、注册日志，码率生效仍标为未确认。十秒从启动调用返回后计算；没有实际开始、结束观察时，不把这段主机等待认作十秒有效视频。成功执行只证明本次诊断步骤及副本一致性检查完成，`summary.json` 保留实际媒体属性；设置生效、全部产物写完、驱动启用和 ARM Linux 完整验收继续按相应设备契约核实。

脚本响应与副本比对的单元测试可在容器中使用 `apps/camctl/.venv/bin/python -m unittest discover -s docs/hardware/tests/unit` 执行；测试使用采集样式和故障输入，不连接相机。

### 原始调用与设备观察

从交接资料中选择该相机的一套完整录像设置，逐条执行并分别保存调用结果。互斥选项只选择一项，不依次执行整张候选命令表。记录自动或手动曝光、分辨率及对应的固定设置；Action6 的画面、增稳、光圈和码率分别记录实际选择。

每条设备侧设置命令通过 `collect_call.py <独立目录> adb -s <serial> shell <命令及参数>` 执行。设置明确失败时先保存该结果，结束这次试验，不继续启动。

两款相机的录像启动、停止候选分别为：

```powershell
py -3.11 collect_call.py action6/020-record-start adb -s <serial> shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01
py -3.11 collect_call.py action6/021-record-stop adb -s <serial> shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00
```

在设备实际开始录像后等待 10 秒，再执行停止；记录实际开始和实际停止的观察来源与时刻。若只能确定启动调用已返回，记录这个事实及停止调用间隔，真实录像时长保持未知。Action6 的启动候选需要核实其在实际固件中的效力。

停止后重复保存目录，识别新增路径。记录文件开始出现、最后写完、相机退出录像状态的观察及先后关系；继续保存必要的后续目录和文件信息，直到能够说明本次全部文件已齐备。文件出现或短时间内长度不变，各自只能证明当时的文件状态。

## 延时摄影资料

两款相机分别选择表中的完整预设。Action6 使用 4K/30 fps、自动曝光、8 秒间隔、30 分钟和仅视频的组合；OSMO 使用全景延时、8K/30 fps、手动曝光、30 秒间隔、10 分钟和仅视频的组合。设置步骤和完整负载从[交接资料](camera-control-handoff.md#action--action6-静止延时摄影)及[OSMO 延时资料](camera-control-handoff.md#osmo-360--360-ii-全景静止延时摄影)取得，不自行修改负载中的时间字节。

逐条保存设置调用后，采集启动候选：

```powershell
py -3.11 collect_call.py action6/030-timelapse-start adb -s <serial> shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01
```

OSMO 使用独立资料目录和自己的 serial。记录启动调用是在发送后、实际开始后，还是整项任务完成后返回。等待预设的真实持续时间，观察相机是否自行结束、是否进入视频处理阶段，以及最终全部文件何时写完。

OSMO 已有停止候选 `dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 00`。在需要主机停止的情况下执行并独立保存结果；记录实际停止是否结束采集，以及之后的视频处理。Action6 的延时停止接口尚缺具体资料；若预设结束后仍需主动停止，提供实际可用接口及响应后再补齐该任务契约。

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

先按[独立安装与主程序联调](../../apps/host-demo/docs/camctl-integration.md#一准备运行账户和交付文件)安装 Python 3.11、配套依赖、camctl wheel 和 host-demo。以下步骤使用已安装的程序，不要求目标机存在源码仓库。

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

在提取的 `config.toml` 中，将两款相机的 serial 分别替换为 `adb devices -l` 列出的实际标识。`device_id` 分别为 `action6` 和 `osmo360ii`；样例省略 `[paths]`，使用运行账户的默认部署目录。已有部署保留原配置，将设备项合并到实际使用的配置中。初始化、能力导出和 host 始终使用同一份配置：

```bash
"$HOME/.camctl/venv/bin/camctl" init --config "$HOME/.camctl/camera-demo/config.toml"
"$HOME/.camctl/venv/bin/camctl" describe --config "$HOME/.camctl/camera-demo/config.toml" \
  > "$HOME/.camctl/camera-demo/capabilities.json"
```

`prepare-plan.py` 从安装包读取计划模板，生成新的正整数请求身份和 UTC 时间，并按刚导出的能力 Schema 校验拍摄参数。默认安排在生成后 10 秒开始，允许迟到 30 秒。每份生成计划提交前都重新生成；原键重送时继续使用原文件。若实际能力尚未包含该任务，生成器会拒绝生成。候选命令和样例存在不表示设备响应、结束方式和文件工具已核实；完成 T9 契约后再执行真机演示。

打开终端 A，运行 `"$HOME/.camctl/host/bin/host-demo" --config "$HOME/.camctl/camera-demo/config.toml"`。保持它运行，在终端 B 生成一份拍摄计划：

```bash
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" action6-record \
  --capabilities "$HOME/.camctl/camera-demo/capabilities.json" \
  --output "$HOME/.camctl/camera-demo/capture.json"
```

将生成器打印的 `submit <绝对路径>` 整行复制到终端 A。host 交互命令不展开 `$HOME` 或 `~`，路径外不加引号。其他三条链分别使用 `action6-timelapse`、`osmo360ii-record` 和 `osmo360ii-timelapse`，每次指定不同的输出文件。四份模板的时长与本文开头的表一致，延时预设保持其完整参数组合。

拍摄结束后在终端 A 输入 `claim`，由客户端校验并可靠导入 `processing` 中的状态报告。从本次 `request_id` 对应计划中取得拍摄动作的 `action_instance_id`；拍摄动作应为 `succeeded`，正式产物应属于这个原动作。将此身份填入独立取回计划。下例中的 `123` 和 `456` 分别替换为原拍摄动作身份和客户端已经可靠导入的累计报告身份：

```bash
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" obtain \
  --source-action-id 123 --last-report-id 456 \
  --output "$HOME/.camctl/camera-demo/obtain.json"
```

按打印的路径在终端 A 提交，等待取回结束并再次 `claim`。核对取回报告的 `deliveries` 与领取文件的文件名、长度及 SHA-256，并由客户端可靠导入报告。领取视频本身不确认报告；`last_report_id` 只采用客户端可靠保存的累计位置。用该位置生成最后的确认计划，再提交给 host：

```bash
"$HOME/.camctl/venv/bin/python" "$HOME/.camctl/camera-demo/prepare-plan.py" report-ack \
  --last-report-id 789 --output "$HOME/.camctl/camera-demo/report-ack.json"
```

将 `789` 替换为取回结束报告的累计身份。最后确认提交与 camctl 吸收确认的结果，保留本次计划、客户端导入凭据、最终报告及源文件信息。host 的 `logs` 命令用于查看 CLI 调用和收场；受理或执行失败以正式结果及报告中的错误为依据。

## ARM Linux 的完整验收

在独立部署目录安装正式发行物，显式配置两款相机的驱动及本次 ADB 绑定，执行初始化并导出 `describe`。计划参数来自实际导出的能力。C host 提交四项真实时长拍摄；各次拍摄保存正式产物后，再提交独立的 `obtain_action_outputs`，按原动作实例选择产物。

每条链分别核对拍摄与取回成功、产物属于原拍摄动作、完整交付发布到 `ready`、host 领取到 `processing`，以及领取文件的实际长度和 SHA-256 与源文件相符。领取并核验状态报告后提交累计报告 ACK，再核对确认水位。视频收到与报告确认分别记录；两种确认完成后，相机原片仍存在。

验收记录包含日期、ARM Linux 主机与 ADB 环境、固件、实际生效参数、原任务的结束及文件完成依据、动作和产物关联、交付文件长度与摘要及报告结果。四条链分别使用表中的完整真实时长。软件替身的加速运行只用于容器协作验证，真实设备验收保持预设时长。
