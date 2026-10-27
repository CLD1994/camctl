# 设备工具

## Hasselblad

`hb_cli` 可与 Hasselblad 相机交互。在容器中使用 USB 透传的相机时，先在 `tools` 目录检查工具和设备连接：

```bash
./hb_cli doctor
lsusb -d 2756:000a
```

如果 `doctor` 报告 `libusb_open: LIBUSB_ERROR_ACCESS`，但 `lsusb` 已列出相机，请根据 `lsusb` 输出中的 Bus 和 Device 编号检查对应设备文件。例如，`Bus 001 Device 003` 对应 `/dev/bus/usb/001/003`：

```bash
ls -l /dev/bus/usb/001/003
id -nG
```

当设备文件的属组为 `root`、所属组具有读写权限，且当前用户属于 `plugdev` 时，可以将该设备文件的属组改为 `plugdev`，然后重新检查：

```bash
sudo chgrp plugdev /dev/bus/usb/001/003
./hb_cli doctor
```

命令中的设备路径应替换为当前 `lsusb` 输出对应的路径。相机重新连接或容器重新创建后，设备编号和权限可能变化，需要重新检查。`doctor` 通过后表示工具的连接检查成功，不代表拍摄已经验证。
