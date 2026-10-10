"""Action6 的 ADB 进程替身；控制响应沿真实格式，文件脚本实际执行。

此脚本复制到独立部署目录并从 PATH 调用。它不导入 camctl，也不
替换驱动工厂、响应解析器或时钟。相机绝对路径映射到临时文件树。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time


REMOTE_DIRECTORY = "/mnt/media_rw/emulated/DCIM"
NEW_VIDEO = REMOTE_DIRECTORY + "/DJI_001/DJI_20000101080811_0001_D.MP4"
NEW_PREVIEW = REMOTE_DIRECTORY + "/DJI_001/DJI_20000101080811_0001_D.LRF"

# 该测试只请求这一组普通录像设置；其他设置不被替身静默接受。
SETTINGS = {
    ("e1", "01"), ("18", "1003000000"),
    ("8e", "010100000101"), ("1e", "0100"),
    ("2c", "0634000000"), ("2e", "10"), ("42", "3d"),
    ("8e", "010109000101"), ("8e", "010108000100"),
}


def _append(path, value):
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def main():
    if len(sys.argv) != 6 or sys.argv[1:5] != ["-s", "action6-test-serial", "shell", "-T"]:
        raise ValueError(f"不是明确绑定的 ADB shell 调用: {sys.argv[1:]!r}")
    # Python 可执行包装器不改变 argv；实际 ADB 的远端脚本占一个参数。
    outer = shlex.split(sys.argv[5])
    if outer[:2] != ["sh", "-c"] or len(outer) != 3:
        raise ValueError("远端调用不是单个 sh -c 脚本")
    script = outer[2]
    root = Path(os.environ["CAMCTL_ACTION6_ADB_ROOT"])
    calls = root.parent / "adb-calls.jsonl"
    began_ns = time.monotonic_ns()
    record = {"script": script, "started_ns": began_ns, "serial": sys.argv[2]}
    words = shlex.split(script)
    if words and words[0] == "dji_mb_ctrl":
        index = words.index("-c")
        if index + 3 != len(words):
            raise ValueError("控制调用不是一组完整命令及负载")
        code = format(int(words[index + 1], 16), "x")
        payload = words[index + 2].lower()
        record.update(code=code, payload=payload)
        if (code, payload) == ("2", "01"):
            record["kind"] = "start"
            target = root / NEW_VIDEO.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("START 被重发，会复用本次源文件路径")
            shutil.copyfile(root.parent / "sample.mp4", target)
            (root / NEW_PREVIEW.lstrip("/")).write_bytes(b"Action6 auxiliary preview")
        elif (code, payload) == ("2", "00"):
            record["kind"] = "stop"
        elif (code, payload) in SETTINGS:
            record["kind"] = "setting"
        else:
            raise ValueError(f"未声明的控制调用: {words!r}")
        record["returned_ns"] = time.monotonic_ns()
        _append(calls, record)
        sys.stdout.write("[dji_mb_ctrl] read global seq: 1\nResp message, len = 1, data:\n  00\n")
        return 0

    local_script = script.replace("/mnt/media_rw/", str(root / "mnt/media_rw") + "/")
    result = subprocess.run(["bash", "-c", local_script], capture_output=True)
    record.update(kind="file", returned_ns=time.monotonic_ns(), returncode=result.returncode)
    _append(calls, record)
    output = result.stdout
    # dd 读取的媒体字节保持原样；文本和 NUL 帧中的文件路径还原为设备路径。
    if not script.startswith("dd "):
        output = output.replace(str(root).encode(), b"")
    sys.stdout.buffer.write(output)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError) as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(64)
