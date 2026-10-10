"""Action6 录像或原生延时诊断；需要同目录的 collect_call.py，使用 Python 3.11。

本脚本只检查已采集的响应样式，不提供正式驱动完成契约。
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


# 此录像组合取自 commands.settings_for 和 action6-record.json。
# 光圈尚待资料方确认，不在诊断步骤中发送。
SETTINGS = (
    ("01-mode", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01"),
    ("02-resolution", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000"),
    ("03-pro", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101"),
    ("04-exposure", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100"),
    ("05-wb", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000"),
    ("06-ev", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10"),
    ("07-colour", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42 3d"),
    ("08-fov", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101"),
    ("09-stabilization", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100"),
)

# 原表 D106、D111 的完整 17 字节预设；秒级值仅供诊断试验。
TIMELAPSE_PAYLOAD = bytes.fromhex("0400005000080700000000000000000000")
TIMELAPSE_DURATION_S = 1800

STORAGE_ROOTS = (("internal", "/mnt/media_rw/emulated/DCIM"), ("sd", "/mnt/media_rw/sd/DCIM"))


class CaptureMode(StrEnum):
    RECORD = "record"
    TIMELAPSE = "timelapse"


class StorageState(StrEnum):
    DIRECTORY = "directory"
    ABSENT = "absent"


class SampleStatus(StrEnum):
    NO_NEW_MP4 = "no_new_mp4"
    MP4_OBSERVED = "mp4_observed"


class VideoCheckState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    FAILED = "failed"


class PresetBasis(StrEnum):
    SOURCE_EXAMPLE = "source_example"
    EXPERIMENTAL_DURATION = "experimental_duration"


def timelapse_payload(duration_s: int) -> str:
    if type(duration_s) is not int or not 1 <= duration_s <= TIMELAPSE_DURATION_S:
        raise ValueError("延时诊断时长必须是 1 至 1800 的整数秒数；硬件支持范围尚未核实")
    payload = bytearray(TIMELAPSE_PAYLOAD)
    # 三个 Action6 样例的这两个低位字节对应小端秒数；完整字段宽度未知。
    payload[5:7] = duration_s.to_bytes(2, "little")
    return payload.hex()


def timelapse_metadata(duration_s: int) -> dict:
    payload = timelapse_payload(duration_s)
    return {
        "requested_params": {"resolution": "4k30", "exposure": "auto", "interval_s": 8,
                             "duration_s": duration_s, "outputs": "video"},
        "preset_payload": payload,
        "preset_basis": (PresetBasis.SOURCE_EXAMPLE if duration_s == TIMELAPSE_DURATION_S
                         else PresetBasis.EXPERIMENTAL_DURATION),
    }


def timelapse_settings(duration_s: int) -> tuple[tuple[str, str], ...]:
    preset = "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c " + timelapse_payload(duration_s)
    # 按原表顺序：间隔与时长、Auto、输出组合；两次设置均发送完整负载。
    return (
        ("01-mode", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02"),
        ("02-resolution", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000"),
        ("03-timing", preset),
        ("04-exposure", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100"),
        ("05-output", preset),
    )


def expect_ack(stdout: bytes) -> None:
    if stdout.count(b"Resp message,") != 1:
        raise RuntimeError("未取得唯一完整的设备响应")
    block = stdout.split(b"Resp message,", 1)[1]
    match = re.fullmatch(rb"\s*len = (\d+), data:\s*([0-9a-fA-F \t\r\n]*)", block)
    if match is None:
        raise RuntimeError("设备响应格式与已采集样例不同")
    try:
        payload = bytes.fromhex(match[2].decode("ascii"))
    except ValueError as error:
        raise RuntimeError("设备响应数据不完整") from error
    if len(payload) != int(match[1]):
        raise RuntimeError("设备响应长度与数据不一致")
    if payload != b"\x00":
        raise RuntimeError(f"设备响应为 {payload.hex()}，预期为单字节 00；具体含义尚未确定")


def assess_copy(source_before: tuple[int, str], source_after: tuple[int, str],
                local: tuple[int, str]) -> dict[str, bool]:
    return {
        "size_observation_changed": source_before[0] != source_after[0],
        "digest_observation_changed": source_before[1] != source_after[1],
        "matches_source_after": local == source_after,
    }


def capture_once(mode, shell, *, duration_s=TIMELAPSE_DURATION_S,
                 clock=time.monotonic, sleep=time.sleep):
    mode = CaptureMode(mode)
    if mode is CaptureMode.TIMELAPSE:
        timelapse_payload(duration_s)
    started = clock()
    if mode is CaptureMode.RECORD:
        try:
            shell("12-start", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01", ack=True)
            returned = clock()
            print("启动调用已返回，主机等待 10 秒后发送停止。", flush=True)
            sleep(10)
        finally:
            shell("13-stop", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00", ack=True)
    else:
        print("正在发送延时 START；该调用的实际返回阶段尚待核实。", flush=True)
        try:
            shell("12-start", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01", ack=True)
        except RuntimeError as error:
            raise RuntimeError(f"延时 START 已尝试，设备是否开始仍未知；未发送停止命令。原错误：{error}") from error
        returned = clock()
        print(f"START 已返回；距本次发起满 {duration_s} 秒后采样，不发送延时 STOP。", flush=True)
        remaining = started + duration_s - clock()
        while remaining > 0:
            sleep(min(60, remaining))
            remaining = started + duration_s - clock()
    return {"start_call_elapsed_s": returned - started,
            "start_to_observation_s": clock() - started}


def parse_directory(stdout: bytes, stderr: bytes):
    if stderr in (b"CAMCTL_STORAGE_ABSENT\n", b"CAMCTL_STORAGE_ABSENT\r\n") and not stdout:
        return StorageState.ABSENT, set()
    if stderr not in (b"CAMCTL_FIND_EXIT=0\n", b"CAMCTL_FIND_EXIT=0\r\n"):
        raise RuntimeError(f"未取得目录的预期结果：stdout={stdout!r}；stderr={stderr!r}")
    if stdout and not stdout.endswith(b"\0"):
        raise RuntimeError("目录列举不完整")
    paths = stdout.split(b"\0")[:-1] if stdout else []
    if any(not path for path in paths) or len(set(paths)) != len(paths):
        raise RuntimeError("目录记录无效")
    return StorageState.DIRECTORY, set(paths)


def assess_sample(mode, before: set[bytes], after: set[bytes], *,
                  duration_s=TIMELAPSE_DURATION_S) -> dict:
    mode = CaptureMode(mode)
    new_files = sorted(after - before)
    videos = [path.decode("utf-8") for path in new_files if path.lower().endswith(b".mp4")]
    summary = {
        "capture": mode,
        "sample_status": SampleStatus.MP4_OBSERVED if videos else SampleStatus.NO_NEW_MP4,
        "new_files": [path.decode("utf-8") for path in new_files],
        "observed_mp4_files": videos,
        "videos": [],
        "video_checks": VideoCheckState.INCOMPLETE if videos else VideoCheckState.NOT_ATTEMPTED,
    }
    if mode is CaptureMode.RECORD:
        summary.update(aperture="unconfirmed", bitrate_effect="unconfirmed")
    else:
        metadata = timelapse_metadata(duration_s)
        summary.update(params=metadata["requested_params"], preset_basis=metadata["preset_basis"],
                       actual_start="unknown", natural_end="unknown",
                       file_write_complete="unknown", output_set_finalized="unknown")
    return summary


def load_observation_source(original: Path, serial: str) -> tuple[dict, set[bytes]]:
    observation = json.loads((original / "capture-observation.json").read_text(encoding="utf-8"))
    if not isinstance(observation, dict) or observation.get("capture") != CaptureMode.TIMELAPSE:
        raise RuntimeError("只读复查只接受原生延时的采集记录")
    metadata_fields = timelapse_metadata(TIMELAPSE_DURATION_S).keys()
    if any(field in observation for field in metadata_fields):
        params = observation.get("requested_params")
        if not isinstance(params, dict) or type(params.get("interval_s")) is not int:
            raise RuntimeError("原延时请求参数缺失或无效")
        try:
            expected = timelapse_metadata(params.get("duration_s"))
        except ValueError as error:
            raise RuntimeError("原延时请求时长缺失或无效") from error
        if any(observation.get(field) != value for field, value in expected.items()):
            raise RuntimeError("原延时参数、完整负载和编码依据缺失或不一致")
    try:
        sampled_at = datetime.fromisoformat(observation["sampling_started_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("原采样时刻缺失或无效") from error
    if sampled_at.utcoffset() is None:
        raise RuntimeError("原采样时刻缺少时区")
    for key in ("start_call_elapsed_s", "start_to_observation_s"):
        value = observation.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise RuntimeError(f"原计时记录 {key} 缺失或无效")

    def check_call(label):
        call = json.loads((original / label / "call.json").read_text(encoding="utf-8"))
        argv = call.get("argv") if isinstance(call, dict) else None
        if not isinstance(argv, list) or argv[1:4] != ["-s", serial, "shell"]:
            raise RuntimeError(f"{label} 的原绑定与本次 serial 不同或记录无效")
        if type(call.get("returncode")) is not int or call["returncode"] != 0:
            raise RuntimeError(f"{label} 的原调用退出状态无效：{call.get('returncode')!r}")

    check_call("12-start")
    before = set()
    states = []
    for name, root in STORAGE_ROOTS:
        label = "11-before-" + name
        check_call(label)
        directory = original / label
        state, paths = parse_directory((directory / "stdout.bin").read_bytes(),
                                       (directory / "stderr.bin").read_bytes())
        if any(not path.startswith(root.encode("utf-8") + b"/") for path in paths):
            raise RuntimeError(f"{label} 的原路径不属于声明范围 {root}")
        states.append(state)
        before.update(paths)
    if all(state is StorageState.ABSENT for state in states):
        raise RuntimeError("原基准的两个存储范围均不存在")
    return observation, before


def main() -> None:
    parser = argparse.ArgumentParser(description="配置 Action6，诊断普通录像或原生延时（支持实验性秒级时长）")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--capture", choices=tuple(CaptureMode), help="新拍摄，默认为 record")
    operation.add_argument("--observe", type=Path, help="沿原延时采集目录的基准进行只读复查")
    parser.add_argument("--timelapse-duration-s", type=int,
                        help="新延时的诊断时长，1 至 1800 秒；默认 1800，其他值为未核实的实验候选")
    parser.add_argument("--adb", default="./adb.exe")
    parser.add_argument("--serial", default="123456789ABCDEF")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    mode = CaptureMode.TIMELAPSE if args.observe is not None else CaptureMode(args.capture or CaptureMode.RECORD)
    if args.timelapse_duration_s is not None and (args.observe is not None or mode is not CaptureMode.TIMELAPSE):
        parser.error("--timelapse-duration-s 只用于新建 --capture timelapse")
    duration_s = args.timelapse_duration_s if args.timelapse_duration_s is not None else TIMELAPSE_DURATION_S
    try:
        requested = timelapse_metadata(duration_s) if mode is CaptureMode.TIMELAPSE else None
    except ValueError as error:
        parser.error(str(error))
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("请使用现有 Python 3.11 环境")

    collector = Path(__file__).resolve().with_name("collect_call.py")
    if not collector.is_file():
        raise RuntimeError("请将 collect_call.py 放在本脚本所在目录")
    original = args.observe.resolve() if args.observe is not None else None
    if original is not None:
        original_observation, before = load_observation_source(original, args.serial)
        # 旧版记录固定使用原 1800 秒预设；新版原要求已经在加载时完整核对。
        duration_s = original_observation.get("requested_params", {}).get("duration_s", TIMELAPSE_DURATION_S)
        requested = timelapse_metadata(duration_s)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base = (original / ("observation-" + stamp) if original is not None
            else Path("action6") / (mode.value + "-probe-" + stamp))
    base.mkdir(parents=True, exist_ok=False)
    print(f"采集目录：{base.resolve()}", flush=True)
    if original is None and requested is not None:
        print(f"延时要求：间隔 8 秒，持续 {duration_s} 秒，仅视频；"
              f"负载依据：{requested['preset_basis']}。", flush=True)

    def run(label, argv, *, allow_stderr=False):
        directory = base / label
        subprocess.run([sys.executable, str(collector), str(directory), *argv],
                       check=True, stdout=subprocess.DEVNULL)
        metadata = json.loads((directory / "call.json").read_text(encoding="utf-8"))
        stdout = (directory / "stdout.bin").read_bytes()
        stderr = (directory / "stderr.bin").read_bytes()
        if metadata["returncode"] != 0 or (stderr and not allow_stderr):
            raise RuntimeError(
                f"{label} 返回异常：退出码 {metadata['returncode']}；"
                f"stdout={stdout!r}；stderr={stderr!r}；原始记录：{directory.resolve()}"
            )
        return stdout

    def shell(label, script, *, ack=False, allow_stderr=False):
        stdout = run(label, [args.adb, "-s", args.serial, "shell", "-T",
                             "sh -c " + shlex.quote(script)], allow_stderr=allow_stderr)
        if ack:
            try:
                expect_ack(stdout)
            except RuntimeError as error:
                raise RuntimeError(f"{label}：{error}；stdout={stdout!r}；原始记录：{(base / label).resolve()}") from error
        return stdout

    def inventory(label):
        stdout = shell(label, "find /mnt/media_rw/emulated/DCIM -type f -print0\nstatus=$?\nprintf 'CAMCTL_FIND_EXIT=%s\\n' \"$status\" >&2\nexit \"$status\"\n", allow_stderr=True)
        return parse_directory(stdout, (base / label / "stderr.bin").read_bytes())[1]

    def timelapse_inventory(label):
        scopes = {}
        all_paths = set()
        for name, root in STORAGE_ROOTS:
            script = (
                f"if [ -d {root} ]; then\n"
                f"find {root} -type f -print0\n"
                "status=$?\nprintf 'CAMCTL_FIND_EXIT=%s\\n' \"$status\" >&2\nexit \"$status\"\n"
                f"elif [ -e {root} ] || [ -L {root} ]; then\n"
                "printf 'CAMCTL_STORAGE_NOT_DIRECTORY\\n' >&2\nexit 1\n"
                "else\nprintf 'CAMCTL_STORAGE_ABSENT\\n' >&2\nfi\n"
            )
            call_label = label + "-" + name
            stdout = shell(call_label, script, allow_stderr=True)
            state, paths = parse_directory(stdout, (base / call_label / "stderr.bin").read_bytes())
            scopes[root] = {"state": state, "paths": [path.decode("utf-8") for path in sorted(paths)]}
            all_paths.update(paths)
        (base / (label + "-storage.json")).write_text(
            json.dumps(scopes, ensure_ascii=False, indent=2), encoding="utf-8")
        if all(scope["state"] == StorageState.ABSENT for scope in scopes.values()):
            raise RuntimeError(f"{label} 的两个存储范围均不存在；范围记录：{base / (label + '-storage.json')}")
        return all_paths

    def source_facts(label, quoted):
        size_raw = shell(label + "-size", f"test -f {quoted} && LC_ALL=C stat -c %s -- {quoted}")
        if not re.fullmatch(rb"[0-9]+\s*", size_raw) or int(size_raw) <= 0:
            raise RuntimeError(f"{label} 的源文件长度无效：{size_raw!r}")
        digest_raw = shell(label + "-digest", f"LC_ALL=C sha256sum < {quoted}")
        digest_match = re.fullmatch(rb"([0-9a-f]{64})\s+-\s*", digest_raw)
        if digest_match is None:
            raise RuntimeError(f"{label} 的源端摘要格式异常：{digest_raw!r}")
        return {"bytes": int(size_raw), "sha256": digest_match[1].decode("ascii")}

    # 先验证本地主机工具可运行，之后才开始采集。
    run("00-ffprobe-version", [args.ffprobe, "-version"])
    if original is None:
        for label, script in (SETTINGS if mode is CaptureMode.RECORD else timelapse_settings(duration_s)):
            shell(label, script, ack=True)
            print(f"{label}：收到预期 00 响应", flush=True)
        if mode is CaptureMode.RECORD:
            bitrate = shell("10-bitrate", "simulate_device -s bitrate 2")
            if not all(marker in bitrate for marker in (
                b"name [DeviceRecordRecSettingBitRate]", b"link to server rlt 0", b"register to server successs",
            )):
                raise RuntimeError(f"10-bitrate 未取得已采集的属性及服务注册日志：{bitrate!r}")
            print("光圈待确认；码率命令已返回，其实际生效仍待确认。", flush=True)

        read_inventory = inventory if mode is CaptureMode.RECORD else timelapse_inventory
        before = read_inventory("11-before")
        timing = capture_once(mode, shell, duration_s=duration_s)
        observation = {"capture": mode, "observation_only": False, **timing}
        if requested is not None:
            observation.update(requested)
    else:
        read_inventory = timelapse_inventory
        observation = {"capture": mode, "observation_only": True,
                       "source_capture_directory": str(original),
                       "original_capture_observation": original_observation}
        print("沿原基准进行只读复查；立即采样，不发送设备控制命令。", flush=True)
    observation["sampling_started_at"] = datetime.now(timezone.utc).isoformat()
    (base / "capture-observation.json").write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    after = read_inventory("14-after")
    summary = assess_sample(mode, before, after, duration_s=duration_s)
    summary["observation"] = observation
    if original is None:
        summary["timing"] = timing

    def save_summary():
        temporary = base / "summary.json.tmp"
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(base / "summary.json")

    save_summary()
    videos = summary["observed_mp4_files"]
    if not videos:
        raise RuntimeError(f"本次采样尚未取得新增 MP4；新增路径：{summary['new_files']!r}；"
                           f"摘要：{(base / 'summary.json').resolve()}")

    def check_video(index, source):
        quoted = shlex.quote(source)
        prefix = f"video-{index:02d}"
        source_before = source_facts(prefix, quoted)
        local = (base / (prefix + ".mp4")).resolve()
        run(prefix + "-pull", [args.adb, "-s", args.serial, "pull", source, str(local)], allow_stderr=True)
        with local.open("rb") as content:
            local_digest = hashlib.file_digest(content, "sha256").hexdigest()
        local_facts = {"bytes": local.stat().st_size, "sha256": local_digest}
        source_after = source_facts(prefix + "-after", quoted)
        assessment = assess_copy((source_before["bytes"], source_before["sha256"]),
                                 (source_after["bytes"], source_after["sha256"]),
                                 (local_facts["bytes"], local_facts["sha256"]))
        copy_record = {"source": source, "local": str(local), "source_before": source_before,
                       "source_after": source_after, "local_facts": local_facts, **assessment}
        copy_file = base / (prefix + "-copy.json")
        copy_file.write_text(json.dumps(copy_record, ensure_ascii=False, indent=2), encoding="utf-8")
        if not assessment["matches_source_after"]:
            raise RuntimeError(
                f"{local} 与下载后源观测不一致：副本={local_facts}；源={source_after}；"
                f"比对记录：{copy_file.resolve()}"
            )
        probe_raw = run(prefix + "-ffprobe", [args.ffprobe, "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration:format=format_name,duration,size",
            "-of", "json", str(local)])
        media = json.loads(probe_raw)
        streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "video"]
        duration = Decimal(media.get("format", {}).get("duration", "NaN"))
        if not streams or not duration.is_finite() or duration <= 0:
            raise RuntimeError(f"{local} 未取得视频流或有效容器时长")
        return {**copy_record, **local_facts,
                "duration_s": str(duration), "video_streams": streams}

    try:
        for index, source in enumerate(videos, 1):
            summary["videos"].append(check_video(index, source))
            save_summary()
    except Exception as error:
        # 保存实际诊断结果后重新抛出原错误；补存失败也保留其原因。
        summary["video_checks"] = VideoCheckState.FAILED
        summary["error"] = str(error)
        try:
            save_summary()
        except Exception as save_error:
            error.add_note(f"失败摘要未能保存：{type(save_error).__name__}: {save_error}；"
                           f"记录目录：{base.resolve()}")
        raise
    summary["video_checks"] = VideoCheckState.COMPLETE
    save_summary()
    print("本次采样及副本一致性检查完成。媒体信息：", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"详细记录：{(base / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
