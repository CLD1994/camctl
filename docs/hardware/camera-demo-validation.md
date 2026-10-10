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

以下采集器使用 Python 3.11 标准库。将代码保存为独立的 `collect_call.py`；其运行不依赖 camctl 源码目录。它只执行命令行明确提供的一次调用，并保存原始 stdout、stderr、退出状态及耗时。调用未返回时，设备观察可从另一终端或相机界面记录；不能因为采集器仍在等待而判断设备未开始。

```python
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

directory = Path(sys.argv[1])
argv = sys.argv[2:]
if not argv:
    raise SystemExit("需要提供本次调用的完整 argv")
directory.mkdir(parents=True, exist_ok=False)
started_at = datetime.now(timezone.utc).isoformat()
started_ns = time.monotonic_ns()
with (directory / "stdout.bin").open("wb") as stdout, \
        (directory / "stderr.bin").open("wb") as stderr:
    result = subprocess.run(argv, stdout=stdout, stderr=stderr, check=False)
metadata = {
    "argv": argv,
    "started_at": started_at,
    "returned_at": datetime.now(timezone.utc).isoformat(),
    "elapsed_ns": time.monotonic_ns() - started_ns,
    "returncode": result.returncode,
}
(directory / "call.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(metadata, ensure_ascii=False))
```

在 PowerShell 中执行，例如：

```powershell
py -3.11 collect_call.py action6/001-adb-version adb version
py -3.11 collect_call.py action6/002-devices adb devices -l
py -3.11 collect_call.py action6/003-model adb -s <serial> shell getprop ro.product.model
py -3.11 collect_call.py action6/004-firmware adb -s <serial> shell getprop ro.build.display.id
py -3.11 collect_call.py action6/005-tools adb -s <serial> shell 'command -v dji_mb_ctrl simulate_device find sort awk stat sha256sum dd'
```

将 `<serial>` 替换为实际设备标识；OSMO 的资料使用另一个目录。`getprop` 返回空值时，在观察记录中保存界面上显示的型号和固件，不补写推测值。工具存在只证明能找到该程序，其所需选项仍须实际核实。

### 保存拍摄前目录

相机通过自己的 Linux 工具列举原存储范围。Action6 分别核对 `/mnt/media_rw/emulated/DCIM` 和 `/mnt/media_rw/sd/DCIM`；OSMO 核对 `/mnt/media_rw/emulated/DCIM`。使用实际存在的存储范围，记录不存在或读取失败的原结果。

```powershell
py -3.11 collect_call.py action6/010-before-internal adb -s <serial> shell 'find /mnt/media_rw/emulated/DCIM -type f -print0'
py -3.11 collect_call.py action6/011-before-sd adb -s <serial> shell 'find /mnt/media_rw/sd/DCIM -type f -print0'
```

`stdout.bin` 中的 NUL 分隔保留文件名中的空格、换行等字符，不通过逐行文本代替原始路径。正式驱动的目录适配还需要核实 `sort -z`、`awk` 的 NUL 输入、`stat -c %s`、`sha256sum` 和 `dd` 的实际调用结果；缺少工具或选项时保留错误，供适配实现选择设备已有的工具组合。

## 录像资料

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

## ARM Linux 的完整验收

在独立部署目录安装正式发行物，显式配置两款相机的驱动及本次 ADB 绑定，执行初始化并导出 `describe`。计划参数来自实际导出的能力。C host 提交四项真实时长拍摄；各次拍摄保存正式产物后，再提交独立的 `obtain_action_outputs`，按原动作实例选择产物。

每条链分别核对拍摄与取回成功、产物属于原拍摄动作、完整交付发布到 `ready`、host 领取到 `processing`，以及领取文件的实际长度和 SHA-256 与源文件相符。领取并核验状态报告后提交累计报告 ACK，再核对确认水位。视频收到与报告确认分别记录；两种确认完成后，相机原片仍存在。

验收记录包含日期、ARM Linux 主机与 ADB 环境、固件、实际生效参数、原任务的结束及文件完成依据、动作和产物关联、交付文件长度与摘要及报告结果。四条链分别使用表中的完整真实时长。软件替身的加速运行只用于容器协作验证，真实设备验收保持预设时长。
