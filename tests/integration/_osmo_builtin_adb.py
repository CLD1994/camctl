"""OSMO 的 PATH ADB 子进程替身。

它从安装 wheel 的命令映射读取本场景的唯一设置、START 与 STOP 字面量；
所有文件命令必须与正式 ShellFileTools 生成的脚本完全一致，并由本机
真实 bash/find/stat/sha256sum/dd 执行。替身不导入或登记相机合同。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time


REMOTE_DIRECTORY = "/mnt/media_rw/emulated/DCIM"
OSV_PATH = REMOTE_DIRECTORY + "/OSMO_0001/VID_0001.OSV"
LRF_PATH = REMOTE_DIRECTORY + "/OSMO_0001/VID_0001.LRF"


def _clock_ns() -> int:
    offset = int(Path(os.environ["CAMCTL_OSMO_TEST_CLOCK_FILE"])
                 .read_text(encoding="ascii").strip())
    return time.monotonic_ns() + offset


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _calls(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _scenario():
    from camctl.devices.drivers.adb_cameras.commands import (
        CameraModel, settings_from_effective_params, start_for, stop_for,
    )

    action_type = os.environ["CAMCTL_OSMO_TEST_ACTION"]
    if action_type not in ("camera_record", "camera_timelapse"):
        raise ValueError(f"未登记的 OSMO 测试动作: {action_type!r}")
    params = json.loads(os.environ["CAMCTL_OSMO_TEST_PARAMS"])
    if params.get("type") not in ("osmo360ii_record", "osmo360ii_timelapse"):
        raise ValueError("OSMO 测试参数没有正式参数类型")
    preview_flag = os.environ.get("CAMCTL_OSMO_TEST_INCLUDE_PREVIEW")
    if preview_flag not in ("0", "1"):
        raise ValueError("OSMO 测试场景必须明确声明是否包含 LRF")
    model = CameraModel.OSMO360II
    settings = tuple(tuple(command) for command in
                     settings_from_effective_params(model, action_type, params))
    return (action_type, params, settings, tuple(start_for(model, action_type)),
            tuple(stop_for(model, action_type)), preview_flag == "1")


def _control(script: str, *, root: Path, calls_path: Path):
    started_ns = time.monotonic_ns()
    action_type, _, settings, start, stop, include_preview = _scenario()
    words = tuple(shlex.split(script))
    if words in settings:
        kind = "setting"
        if words[0] == "simulate_device":
            return _run_control(calls_path, {
                "kind": kind, "script": script, "command": list(words),
                "response": None, "cwd": os.getcwd(), "python": sys.executable,
                "argv": sys.argv, "started_ns": started_ns,
                "started_clock_ns": _clock_ns(),
            }, payload=None)
        payload = "ee" if (action_type == "camera_timelapse"
                           and words[-3:] == ("-c", "1e", "0000")) else "00"
    elif words == start:
        kind, payload = "start", "00"
        if any(record.get("kind") == "start" for record in _calls(calls_path)):
            raise ValueError("同一安装版场景重发了原生 START")
        source = Path(os.environ["CAMCTL_OSMO_TEST_SAMPLE"])
        video = root / OSV_PATH.lstrip("/")
        preview = root / LRF_PATH.lstrip("/")
        video.parent.mkdir(parents=True, exist_ok=True)
        if video.exists() or preview.exists():
            raise ValueError("START 目标路径在本场景开始前已经存在")
        shutil.copyfile(source, video)
        if include_preview:
            shutil.copyfile(source.with_suffix(".lrf"), preview)
    elif words == stop:
        kind = "stop"
        payload = "df" if action_type == "camera_timelapse" else "00"
    else:
        raise ValueError(f"控制调用不属于本场景真实命令映射: {words!r}")

    record = {
        "kind": kind,
        "script": script,
        "command": list(words),
        "response": payload,
        "cwd": os.getcwd(),
        "python": sys.executable,
        "argv": sys.argv,
        "started_ns": started_ns,
        "started_clock_ns": _clock_ns(),
    }
    if kind == "start":
        _append(calls_path, record)
        # 让测试在真实 START 响应保存前终止 run 进程，再以同一 SQLite
        # 状态恢复。未设置 gate 时，START 正常立即返回。
        gate = os.environ.get("CAMCTL_OSMO_TEST_START_RESPONSE_GATE")
        if gate:
            gate_path = Path(gate)
            while not gate_path.exists():
                time.sleep(0.02)
        _append(calls_path, {
            "kind": "start_response", "response": payload,
            "cwd": os.getcwd(), "python": sys.executable, "argv": sys.argv,
            "returned_ns": time.monotonic_ns(), "exitcode": 0,
            "returned_clock_ns": _clock_ns(),
        })
    else:
        record.update(returned_ns=time.monotonic_ns(), returned_clock_ns=_clock_ns(), exitcode=0)
        _append(calls_path, record)
    return _emit_response(payload)


def _run_control(calls_path: Path, record: dict, *, payload: str | None):
    record["returned_ns"] = time.monotonic_ns()
    record["returned_clock_ns"] = _clock_ns()
    record["exitcode"] = 0
    _append(calls_path, record)
    return _emit_response(payload)


def _emit_response(payload: str | None) -> int:
    if payload is not None:
        sys.stdout.write(
            "[dji_mb_ctrl] read global seq: 1\n"
            "Resp message, len = 1, data:\n  " + payload + "\n")
    return 0


def _shell_kind(script: str) -> str:
    from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools

    tools = ShellFileTools()
    known_files = (OSV_PATH, LRF_PATH)
    for path in known_files:
        if script == tools.metadata_script(path):
            return "metadata"
        if script == tools.digest_script(path):
            return "digest"
        if script == tools.read_script(path, 0):
            return "read"
        if script.startswith("dd if="):
            match = re.fullmatch(r"dd if=(.+) bs=65536 skip=(0|[1-9][0-9]*)", script)
            if match and match.group(1) == shlex.quote(path):
                return "read"

    lines = script.splitlines()
    after_line = next((line for line in lines if line.startswith("after=")), None)
    batch_match = re.search(r'^    if \[ "\$count" -ge ([1-9][0-9]*) \]; then$', script, re.MULTILINE)
    if after_line is not None and batch_match is not None:
        after_parts = shlex.split(after_line.removeprefix("after="))
        if len(after_parts) == 1:
            expected = tools.directory_script(
                REMOTE_DIRECTORY, after_parts[0], int(batch_match[1]))
            if script == expected:
                return "directory"
    raise ValueError(f"ADB 文件脚本不符合正式 ShellFileTools 结构: {script!r}")


def _file_call(script: str, *, root: Path, calls_path: Path) -> int:
    kind = _shell_kind(script)
    local_script = script.replace(
        "/mnt/media_rw/", str(root / "mnt/media_rw") + "/")
    record = {
        "kind": kind,
        "script": script,
        "cwd": os.getcwd(),
        "python": sys.executable,
        "argv": sys.argv,
        "started_ns": time.monotonic_ns(),
        "started_clock_ns": _clock_ns(),
    }
    if kind == "read":
        _append(calls_path, {**record, "kind": "read_started"})
        gate = os.environ.get("CAMCTL_OSMO_TEST_READ_GATE")
        process = subprocess.Popen(["bash", "-c", local_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        forwarded = 0
        gate_after = int(os.environ.get("CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES", "0"))
        gate_path = None if not gate else Path(gate)
        blocked = False
        if gate_path is not None and gate_after == 0 and not gate_path.exists():
            _append(calls_path, {**record, "kind": "read_blocked",
                                 "forwarded_bytes": 0})
            blocked = True
            while not gate_path.exists():
                time.sleep(0.02)
        while True:
            block = process.stdout.read(65536)
            if not block:
                break
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
            forwarded += len(block)
            if (not blocked and gate_path is not None and gate_after > 0
                    and forwarded >= gate_after and not gate_path.exists()):
                _append(calls_path, {**record, "kind": "read_blocked",
                                     "forwarded_bytes": forwarded})
                blocked = True
                while not gate_path.exists():
                    time.sleep(0.02)
        stderr = process.stderr.read()
        if stderr:
            sys.stderr.buffer.write(stderr)
        exitcode = process.wait()
        record.update(returned_ns=time.monotonic_ns(), returned_clock_ns=_clock_ns(), exitcode=exitcode)
        _append(calls_path, record)
        return exitcode
        exitcode = process.wait()
    else:
        process = subprocess.run(["bash", "-c", local_script],
            capture_output=True)
        exitcode = process.returncode
        output = process.stdout.replace(str(root).encode(), b"")
        sys.stdout.buffer.write(output)
        sys.stderr.buffer.write(process.stderr)
    record.update(returned_ns=time.monotonic_ns(), returned_clock_ns=_clock_ns(), exitcode=exitcode)
    _append(calls_path, record)
    return exitcode


def main() -> int:
    expected_serial = os.environ["CAMCTL_OSMO_TEST_SERIAL"]
    if len(sys.argv) != 6 or sys.argv[1:5] != ["-s", expected_serial, "shell", "-T"]:
        raise ValueError(f"不是本场景明确绑定的 ADB shell 调用: {sys.argv[1:]!r}")
    remote_script = sys.argv[5]
    outer = shlex.split(remote_script)
    if outer[:2] != ["sh", "-c"] or len(outer) != 3:
        raise ValueError("远端 ADB 调用不是单个 sh -c 脚本")
    script = outer[2]
    root = Path(os.environ["CAMCTL_OSMO_TEST_ADB_ROOT"])
    calls_path = Path(os.environ["CAMCTL_OSMO_TEST_ADB_LOG"])
    if script.startswith(("dji_mb_ctrl ", "simulate_device ")):
        return _control(script, root=root, calls_path=calls_path)
    return _file_call(script, root=root, calls_path=calls_path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        sys.stderr.write(f"{error}\n")
        raise SystemExit(64)
