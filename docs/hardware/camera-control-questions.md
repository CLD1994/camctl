# 待询问的相机控制资料

本文件记录真机试验中需要向相机资料提供方确认的问题。每项包含现场条件、实际请求和返回，以及需要补充的信息，可直接转发对应小节。

## Action6：光圈设置返回 `e3`

记录日期：2026-10-10。设备由使用者确认是 Action6 调试机，屏幕损坏，无法从相机界面观察设置或拍摄状态。ADB 设备信息中的 product 为 `qcs8625_ac206`；`ro.product.model` 和 `ro.build.display.id` 均未返回内容，固件版本及控制工具版本尚未取得。

试验在 Windows 上通过 `adb shell -T` 执行相机内命令，ADB 为 1.0.41，Platform Tools 为 37.0.1。完整部署的目标主机是 ARM Linux。

### 已观察到的光圈请求与返回

F2.8 的实际命令：

```sh
dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x26 1801
```

F4.0 的实际命令：

```sh
dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x26 9001
```

三次输出均显示发送 `msg_id: 00020026`、长度为 `2`；`Resp message` 的长度均为 `1`。本地 ADB 退出码均为 `0`，stderr 均为空。

| 请求发起时间（UTC） | 请求 | 实际发送数据 | `Resp message` 数据 |
| --- | --- | --- | --- |
| 2026-10-10 10:39:46 | F2.8，首次 | `18 01` | `e3` |
| 2026-10-10 10:43:52 | F2.8，原样复测 | `18 01` | `e3` |
| 2026-10-10 10:46:18 | F4.0，对照试验 | `90 01` | `e3` |

目前没有取得光圈读回结果，不能确认这三次请求是否改变了实际光圈。`e3` 的含义及出现原因尚未明确。

### 设置光圈前的操作顺序

先发送普通视频模式命令 `dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01`，收到 `00`。随后试录时分别发送同一调用前缀下的 `-c 02 01` 和 `-c 02 00`，两者均收到 `00`；期间主机等待十秒，之后新增 MP4 的本地副本可以用播放器打开。没有取得相机空闲状态的查询结果。

之后依次发送以下八条设置命令。每条本地退出码均为 `0`，stderr 均为空，`Resp message` 均为长度 `1`、数据 `00`；这些返回尚未通过设置读回确认实际生效。

```sh
# 4K/30 fps
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000
# Pro 开启
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101
# Auto 曝光
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100
# 手动白平衡 5200 K
dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000
# 曝光补偿 0 EV
dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10
# D-Log M
dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42 3d
# Wide
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101
# 关闭增稳
dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100
```

接着依次进行了 F2.8 首次请求、F2.8 原样复测和 F4.0 对照试验。高码率命令 `simulate_device -s bitrate 2` 在这三次光圈调用之后才执行。

另外执行 `dji_mb_ctrl -h` 后，工具输出通用 `Usage`，本地退出码为 `1`，stderr 为空。帮助没有给出 `e3` 的定义或光圈命令的适用条件。

### 请资料提供方回答

1. `0x26` 请求返回 `e3` 的准确含义是什么？该返回能够确认请求已经发生或尚未发生哪些效果？
2. 对这款调试机，以上 F2.8、F4.0 命令及 `-S test`、`-R diag` 前缀是否适用？请说明硬件、固件、拍摄状态、曝光模式等前置条件；如需其他设置方式，请提供完整命令、合法选项及成功和失败响应样例。
3. 如何读取当前实际光圈，并确认设置已经生效？请提供完整查询命令和原始返回格式；如没有查询接口，请说明可用的确认依据。
4. 如何通过 ADB 获取这台调试机的固件版本和控制工具版本，以便核对上述接口的适用范围？

### 可随问题提供的原始材料

Windows 采集目录为 `action6/042-record-aperture`、`action6/043-record-aperture-repeat`、`action6/044-record-aperture-f4` 和 `action6/046-control-help`。每个目录均包含 `call.json`、`stdout.bin` 和 `stderr.bin`，分别保留实际参数、时间、退出状态及原始输出。

项目内的其他采集事实及适用范围见[普通录像试录记录](../camctl/verification.md#action6-的-windows-普通录像试录2026-10-10)。
