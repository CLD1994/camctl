# hb_cli 使用说明

`hb_cli` 用于在 Linux 下通过 USB 控制 Hasselblad CFV 100C。

本工具单独提供的子命令不等同于 camctl 已接入的拍摄能力。当前 camctl 只通过 `camera_take_photo` 执行单张 RAW 拍摄；部署与现场结果见[ARM 演示记录](validation.md)，行为契约见[驱动设计](../../camctl/hasselblad-driver.md)。

## 目录结构

交付包用于 Jetson TX2 Ubuntu 18.04 ARM64：

```text
hb_cli/
├── hb_cli
└── README.md
```

可执行文件支持 ARM64（`aarch64`）Ubuntu 18.04 及以上版本。

## 首次运行

进入对应目录并添加执行权限：

```bash
chmod +x hb_cli
./hb_cli doctor
```

`doctor: PASS` 表示 USB、相机联机握手和基础参数通信正常。

如果系统能看到相机但提示没有权限，执行：

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2756", MODE="0666"' | sudo tee /etc/udev/rules.d/50-hasselblad.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

然后重新拔插相机。VMware 中需要将 Hasselblad USB 设备连接给 Ubuntu。连接前请退出 Phocus，避免设备被占用。

## 基础指令

```bash
./hb_cli list                     # 查看已连接的 Hasselblad 相机
./hb_cli doctor                   # 检查运行环境和相机通信
./hb_cli connect                  # 建立联机状态后退出
./hb_cli info                     # 查看常用相机信息
./hb_cli params                   # 查看支持的参数名称
./hb_cli get firmware             # 读取固件版本
./hb_cli get battery              # 读取电池状态
```

排查通信问题时添加 `--verbose`：

```bash
./hb_cli --verbose get firmware
```

## 拍摄参数

```bash
./hb_cli set exposure-mode M      # 曝光模式：M / A / S / P
./hb_cli set drive-mode single    # single / continuous / self-timer / interval / exposure-bracketing / focus-bracketing
./hb_cli set iso 100              # ISO
./hb_cli set aperture 8           # 光圈 f/8
./hb_cli set shutter 0.008        # 快门秒数，小数格式；0.008 约为 1/125 秒
./hb_cli set capture-format raw   # RAW（3FR）
./hb_cli set capture-format jpeg  # JPEG（固件允许时）
```

读取参数：

```bash
./hb_cli get iso
./hb_cli get aperture
./hb_cli get shutter
./hb_cli get exposure-mode
./hb_cli get capture-format
```

## 白平衡

```bash
./hb_cli set wb-mode manual       # auto / daylight / cloudy / shade / tungsten / fluorescent / flash / manual
./hb_cli set wb-temp 5500         # 色温 5500 K；必须先将 wb-mode 设置为 manual
./hb_cli set wb-tint 10           # 色调 10；必须先将 wb-mode 设置为 manual
./hb_cli set wb 5500,10           # 同时设置色温和色调；必须先将 wb-mode 设置为 manual
./hb_cli get wb                    # 读取色温和色调
```

## 对焦

```bash
./hb_cli set focus-mode MF        # 手动对焦
./hb_cli set focus-mode AF-S      # 单次自动对焦
./hb_cli autofocus                # 执行一次自动对焦
./hb_cli focus 5                  # 向无限远移动；绝对值越大，单次相对移动量越大
./hb_cli focus -5                 # 向近处移动；绝对值越大，单次相对移动量越大
```

`focus` 的数值是相对移动量，不代表距离或百分比。每 1 个 CLI 单位对应协议移动量 25。

## 拍摄和保存图片

```bash
./hb_cli capture                   # 拍摄，同时将原图和缩略图保存到当前目录的 capture 目录
./hb_cli capture --output ./photos # 拍摄，同时将原图和缩略图保存到指定目录
```

1. `capture` 会先启用相机与本地双落盘：原图同时保存在相机存储和本地目录，并在本地为 JPEG 或 3FR 生成同名的 `.thumbnail.jpg` 缩略图。
2. `capture` 返回的名称是本地存盘的名称，可以理解成收到这名称就意味着拍摄成功结束且相机里已经存在该照片了。不需要再通过列出相机的所有图片进行新增核对。
3. 本地原图按 `capture_<图片编号>.<扩展名>` 命名，缩略图追加 `.thumbnail.jpg`。先输出本地保存信息，最后输出机身路径，例如：

   ```text
   capture: OK
   saved capture/capture_22675465.JPG (23310336 bytes)
   saved capture/capture_22675465.JPG.thumbnail.jpg (806728 bytes)
   camera file: /mnt/media_rw/ssd/DCIM/106HASBL/B9996039.JPG
   ```

   `--verbose capture` 会显示相机报告的原始路径。
   如果相机未报告照片名，会提示 `capture: camera filename was not reported`。

## 浏览和下载图片

```bash
./hb_cli images                                             # 列出相机存储中的图片

./hb_cli download thumbnail --output ./thumbs               # 下载全部缩略图
./hb_cli download original --output ./originals             # 下载全部原图

./hb_cli download thumbnail A0001234.JPG --output ./thumbs   # 下载指定图片的缩略图
./hb_cli download original A0001234.JPG --output ./originals # 下载指定原图
```

不指定图片名称时下载全部；指定名称时只下载对应图片。

指定图片时，`download original` 和 `download thumbnail` 只接受机内文件名，不接受完整机内路径。拍摄响应中的本地文件名与机内文件名分别表示主机副本和相机原图；下载使用 `camera file:` 返回路径中的文件名。

机身只保存原图，不单独保存缩略图文件。`download thumbnail` 按需从指定原图提取或生成缩略图，并将结果保存到主机：

| 原图格式 | 缩略图的取得方式 |
| --- | --- |
| JPEG | 优先提取内嵌图；没有内嵌图时，生成宽、高分别为原图 1/10 的图片 |
| 3FR | 提取原图中内嵌的 JPEG 预览图 |

`capture` 在主机保存的 `.thumbnail.jpg` 是工具生成的主机文件，不表示机身中另存了一份缩略图。

## 删除机内照片

```bash
./hb_cli-linux-aarch64 delete /mnt/media_rw/ssd/DCIM/106HASBL/B9996039.JPG # 按完整机内路径删除一张照片
./hb_cli-linux-aarch64 delete B9996025.JPG # 从 images 列表中唯一匹配文件名后删除
./hb_cli-linux-aarch64 delete             # 删除 images 能列出的全部照片；直接执行，无确认提示
```

成功输出 `deleted <路径>`；相机拒绝时输出错误并返回非零退出码。
部分新拍照片暂未出现在 `images` 中，此时使用经过确认的完整机内路径删除。机内路径不是本地 Linux 的文件路径；删除不影响本地已下载的照片。

删除机内原图后，工具无法再从该原图提取或生成缩略图；此前已经保存到主机的原图和缩略图仍保留。

## 时间和维护

```bash
./hb_cli sync-time                         # 使用 Linux 当前本地时间同步相机
./hb_cli set-time "2026-09-12 18:30:00"   # 将相机设置为指定日期和时间
./hb_cli stop                              # 关闭相机电源
./hb_cli bad-pixel-calibrate               # 启动机内坏点校准，可能需要约 10 分钟
```

`stop` 会直接关闭 CFV 100C。CFV 100C 不支持通过普通联机会话远程重启。
