"""Action6 的 ADB 进程替身；控制响应沿真实格式，文件脚本实际执行。

此脚本复制到独立部署目录并从 PATH 调用。设置命令从安装包声明读取，
不替换驱动工厂、响应解析器或时钟。相机绝对路径映射到临时文件树。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time


REMOTE_DIRECTORY = "/mnt/media_rw/emulated/DCIM"
NEW_VIDEO = REMOTE_DIRECTORY + "/DJI_001/DJI_20000101080811_0001_D.MP4"
NEW_PREVIEW = REMOTE_DIRECTORY + "/DJI_001/DJI_20000101080811_0001_D.LRF"
NEW_PHOTO = REMOTE_DIRECTORY + "/DJI_001/FRAME_0001.JPG"

# 该测试只请求这一组普通录像设置；其他设置不被替身静默接受。
SETTINGS = {
    ("e1", "01"), ("18", "1003000000"),
    ("8e", "010100000101"), ("1e", "0100"),
    ("2c", "0634000000"), ("2e", "10"), ("42", "3d"),
    ("8e", "010109000101"), ("8e", "010108000100"),
    ("26", "1801"),
}


def _scenario_files():
    """Read only the explicit source files declared by an integration case."""
    manifest = os.environ.get("CAMCTL_ACTION6_TEST_FILES")
    if manifest is None:
        return ()
    entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Action6 文件场景必须显式声明非空路径集合")
    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "source"}:
            raise ValueError("Action6 文件场景条目必须只含 path 和 source")
        path, source = entry["path"], Path(entry["source"])
        if (
            not isinstance(path, str)
            or not path.startswith(REMOTE_DIRECTORY + "/")
            or "\0" in path
            or os.path.normpath(path) != path
            or ".." in Path(path).parts
            or not source.is_file()
            or path in seen
        ):
            raise ValueError("Action6 文件场景路径越界、重复或缺少本地样例")
        seen.add(path)
        result.append((path, source))
    return tuple(result)


def _clock_fields(prefix):
    state = os.environ.get("CAMCTL_OSMO_TEST_CLOCK_FILE")
    if not state:
        return {}
    from _osmo_builtin_clock import read_clock
    reading = read_clock(Path(state))
    return {f"{prefix}_clock_ns": reading["monotonic_ns"],
            f"{prefix}_clock_utc_us": reading["utc_us"], f"{prefix}_epoch": reading["epoch"]}


def _file_kind(script):
    from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
    tools = ShellFileTools()
    paths = tuple(path for path, _ in _scenario_files()) + (
        NEW_VIDEO, NEW_PREVIEW, NEW_PHOTO,
        REMOTE_DIRECTORY + "/DJI_001/DJI_OLD.MP4")
    batch_line = next((line.strip() for line in script.splitlines()
        if line.strip().startswith("for path in ") and line.strip().endswith("; do")), None)
    if batch_line is not None:
        selected = tuple(shlex.split(batch_line[len("for path in "):-len("; do")]))
        if (selected and all(path in paths for path in selected)
                and script == tools.metadata_many_script(selected)):
            return "metadata"
    for path in paths:
        for kind, candidate in (("metadata", tools.metadata_script(path)),
                                ("digest", tools.digest_script(path)),
                                ("query", tools.file_presence_script(path)),
                                ("delete", tools.file_presence_script(path, delete=True))):
            if script == candidate:
                return kind
        match = re.fullmatch(r"dd if=(.+) bs=65536 skip=(0|[1-9][0-9]*)", script)
        if match and match.group(1) == shlex.quote(path):
            return "read"
    after = next((line[6:] for line in script.splitlines() if line.startswith("after=")), None)
    batch = re.search(r'^    if \[ "\$count" -ge ([1-9][0-9]*) \]; then$', script, re.MULTILINE)
    if after is not None and batch is not None:
        parts = shlex.split(after)
        if len(parts) == 1 and script == tools.directory_script(REMOTE_DIRECTORY, parts[0], int(batch[1])):
            return "directory"
    raise ValueError(f"文件脚本不属于正式文件工具: {script!r}")


def _append(path, value):
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError(f"ADB 调用日志记录写入不完整: {written}/{len(encoded)} bytes")
    finally:
        os.close(descriptor)


def _wait_after_copy_segment_bytes(file_kind, script, bytes_forwarded):
    """Pause after streaming one durable copy-progress segment's bytes."""
    gate_value = os.environ.get("CAMCTL_ACTION6_TEST_READ_GATE_DIR")
    if file_kind != "read" or gate_value is None:
        return
    from camctl.host_files.segments import DEFAULT_CHUNK_SIZE_BYTES

    if bytes_forwarded < DEFAULT_CHUNK_SIZE_BYTES:
        return
    match = re.fullmatch(r"dd if=(.+) bs=65536 skip=(0|[1-9][0-9]*)", script)
    if match is None:
        return
    gate = Path(gate_value)
    entered = gate / "read-entered.json"
    release = gate / "release-read"
    if entered.exists() or release.exists():
        return
    try:
        claim = os.open(gate / "read.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    else:
        os.close(claim)
    record = {
        "stage": "after_first_copy_segment_bytes_streamed",
        "path_argument": match[1],
        "request_skip": int(match[2]),
        "request_offset_bytes": int(match[2]) * 65_536,
        "bytes_forwarded": bytes_forwarded,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "cli_pid": os.getppid(),
        "started_ns": time.monotonic_ns(),
    }
    temporary = entered.with_suffix(".new")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(entered)
    while not release.exists():
        time.sleep(0.01)


def _forward_read_stream(script, local_script):
    """Forward dd output incrementally so a recovery boundary can interrupt it."""
    process = subprocess.Popen(
        ["bash", "-c", local_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    bytes_forwarded = 0
    while True:
        chunk = process.stdout.read(65_536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        bytes_forwarded += len(chunk)
        _wait_after_copy_segment_bytes("read", script, bytes_forwarded)
    stderr = process.stderr.read()
    returncode = process.wait()
    sys.stderr.buffer.write(stderr)
    return returncode


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
    record = {"script": script, "started_ns": began_ns, "serial": sys.argv[2],
              "pid": os.getpid(), "cli_pid": os.getppid(), **_clock_fields("started")}
    words = shlex.split(script)
    if words and words[0] == "simulate_device":
        from camctl.devices.drivers.adb_cameras.commands import ACTION6_SESSION_SETUP, ACTION_BITRATE
        if tuple(words) not in (*ACTION6_SESSION_SETUP, *ACTION_BITRATE.values()):
            raise ValueError(f"未声明的模拟设置调用: {words!r}")
        record.update(kind="setting", command=words)
        record["returned_ns"] = time.monotonic_ns()
        record.update(**_clock_fields("returned"), exitcode=0)
        _append(calls, record)
        sys.stdout.write("link to server rlt 0\nregister to server successs\n")
        return 0
    if words and words[0] == "dji_mb_ctrl":
        if words[:words.index("-c") + 1] not in (
                ["dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c"],
                ["dji_mb_ctrl", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c"]):
            raise ValueError(f"不是 Action6 完整控制命令: {words!r}")
        index = words.index("-c")
        if index + 3 != len(words):
            raise ValueError("控制调用不是一组完整命令及负载")
        code = format(int(words[index + 1], 16), "x")
        payload = words[index + 2].lower()
        record.update(code=code, payload=payload)
        timelapse = os.environ.get("CAMCTL_ACTION6_TEST_CAPTURE") == "timelapse"
        timelapse_settings = ()
        if timelapse:
            from camctl.devices.drivers.adb_cameras.commands import settings_from_effective_params, start_for, stop_for
            params = json.loads(os.environ["CAMCTL_ACTION6_TEST_PARAMS"])
            timelapse_settings = settings_from_effective_params(
                "dji-action6", "camera_timelapse", params
            )
            allowed = (*timelapse_settings,
                       start_for("dji-action6", "camera_timelapse"), stop_for("dji-action6", "camera_timelapse"))
            if tuple(words) not in allowed:
                raise ValueError(f"不是本次 Action6 的完整命令: {words!r}")
        setting_call = (
            tuple(words) in timelapse_settings
            if timelapse
            else (code, payload) in SETTINGS
        )
        start_code = "1" if timelapse else "2"
        if (code, payload) == (start_code, "01"):
            record["kind"] = "start"
            files = _scenario_files()
            if files:
                for path, source in files:
                    target = root / path.lstrip("/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise ValueError("START 被重发，会复用本次源文件路径")
                    shutil.copyfile(source, target)
            else:
                target = root / NEW_VIDEO.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError("START 被重发，会复用本次源文件路径")
                shutil.copyfile(root.parent / "sample.mp4", target)
                (root / NEW_PREVIEW.lstrip("/")).write_bytes(b"Action6 auxiliary preview")
                if timelapse:
                    (root / NEW_PHOTO.lstrip("/")).write_bytes(b"Action6 independent JPEG original")
        elif (code, payload) == (start_code, "00"):
            record["kind"] = "stop"
        elif setting_call:
            record["kind"] = "setting"
        else:
            raise ValueError(f"未声明的控制调用: {words!r}")
        record["returned_ns"] = time.monotonic_ns()
        record.update(**_clock_fields("returned"), exitcode=0)
        _append(calls, record)
        response = "e3" if (code, payload) == ("26", "1801") else "00"
        sys.stdout.write(f"[dji_mb_ctrl] read global seq: 1\nResp message, len = 1, data:\n  {response}\n")
        return 0

    file_kind = _file_kind(script)
    local_script = script.replace("/mnt/media_rw/", str(root / "mnt/media_rw") + "/")
    if file_kind == "read":
        returncode = _forward_read_stream(script, local_script)
        output = b""
    else:
        result = subprocess.run(["bash", "-c", local_script], capture_output=True)
        returncode = result.returncode
        output = result.stdout
        output = output.replace(str(root).encode(), b"")
        sys.stdout.buffer.write(output)
        sys.stderr.buffer.write(result.stderr)
    record.update(kind="file", file_kind=file_kind, returned_ns=time.monotonic_ns(),
                  returncode=returncode, **_clock_fields("returned"))
    _append(calls, record)
    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError) as error:
        sys.stderr.write(str(error) + "\n")
        raise SystemExit(64)
