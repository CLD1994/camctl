"""Action6 录像或原生延时诊断；需要同目录的 collect_call.py，使用 Python 3.11。

本脚本只检查已采集的响应样式，不提供正式驱动完成契约。
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import importlib.util
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

# 旧记录只读解释保留原格式；新请求默认值只从生成资源派生。
TIMELAPSE_DURATION_S = 1800
TIME_PARAMETERS_PATH = Path(__file__).resolve().with_name("timelapse-time-parameters.json")
CANDIDATE_PATH = Path(__file__).resolve().with_name("action6_timelapse_candidate.py")
ENCODING_FORMAT = "action6-timelapse-candidate-v1"

STORAGE_ROOTS = (("internal", "/mnt/media_rw/emulated/DCIM"), ("sd", "/mnt/media_rw/sd/DCIM"))


class CaptureMode(StrEnum):
    RECORD = "record"
    TIMELAPSE = "timelapse"


class ObservationLayout(StrEnum):
    EARLIEST = "A0"
    EARLIEST_WITH_FLAG = "A1"
    TWO_PRESETS = "B0"
    SINGLE_PRESET = "B1"
    CANDIDATE_V1 = "C"


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
    SAMPLE_DERIVED_CANDIDATE = "sample_derived_candidate"


def exact_json(text):
    def reject(value):
        raise ValueError(f"JSON 数字无效：{value}")
    return json.loads(text, parse_float=Decimal, parse_constant=reject)


def json_text(value):
    """用标准 JSON 数字保存 Decimal 原精度，不投影到 float。"""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("不能保存非有限数字")
        return str(value)
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(key, ensure_ascii=False) + ": " + json_text(item)
                                for key, item in value.items()) + "}"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(map(json_text, value)) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def exact_positive(value):
    return (not isinstance(value, bool) and isinstance(value, (int, Decimal))
            and (not isinstance(value, Decimal) or value.is_finite()) and value > 0)


def load_time_parameters():
    resource = exact_json(TIME_PARAMETERS_PATH.read_text(encoding="utf-8"))
    required = {"format_version", "interval_s", "duration_s", "relation", "legal_pair_count", "shortest_pair"}
    if (not isinstance(resource, dict) or set(resource) != required
            or type(resource["format_version"]) is not int or resource["format_version"] != 1
            or resource["relation"] != "duration_s > interval_s"):
        raise ValueError("共同时间资源的格式或版本无效")
    for field in ("interval_s", "duration_s"):
        values = resource[field]
        if (not isinstance(values, list) or not values or not all(exact_positive(v) for v in values)
                or sorted(set(values)) != values
                or (field == "duration_s" and any(v != int(v) for v in values))):
            raise ValueError(f"共同时间资源 {field} 无效")
    pairs = [(i, d) for i in resource["interval_s"] for d in resource["duration_s"] if d > i]
    if not pairs:
        raise ValueError("共同时间资源没有合法组合")
    interval, duration = min(pairs, key=lambda pair: (pair[1], pair[0]))
    if (type(resource["legal_pair_count"]) is not int or resource["legal_pair_count"] != len(pairs)
            or resource["shortest_pair"] != {"interval_s": interval, "duration_s": duration}
            or not all(exact_positive(v) for v in resource["shortest_pair"].values())):
        raise ValueError("共同时间资源的组合数量或最短组合不一致")
    return resource


def load_candidate_encoder():
    name = "action6_timelapse_candidate"
    spec = importlib.util.spec_from_file_location(name, CANDIDATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prepare_timelapse(duration_s=None, *, interval_s=None, time_parameters=None):
    """新诊断唯一入口：精确校验当前时间域，再完整编码，尚无外部调用。"""
    domain = load_time_parameters() if time_parameters is None else time_parameters
    shortest = domain["shortest_pair"]
    interval = shortest["interval_s"] if interval_s is None else interval_s
    duration = shortest["duration_s"] if duration_s is None else duration_s
    if (not exact_positive(interval) or not exact_positive(duration)
            or interval not in domain["interval_s"] or duration not in domain["duration_s"]
            or duration <= interval):
        raise ValueError("延时间隔、采集时长必须属于共同时间域，且时长严格大于间隔")
    encoder = load_candidate_encoder()
    params = {"type": "action6_timelapse", "interval_s": interval, "duration_s": duration,
              "exposure": {"mode": "auto"}, "outputs": "video"}
    commands = encoder._encode_settings(encoder.CameraModel.ACTION6, "camera_timelapse", params)
    template = encoder._candidate_template_for(encoder.CameraModel.ACTION6, "video", "auto")
    payload = commands[2][-1]
    metadata = {
        "requested_params": {"resolution": "4k30", "exposure": "auto", "interval_s": interval,
                             "duration_s": duration, "outputs": "video"},
        "preset_payload": payload,
        "preset_basis": (PresetBasis.SOURCE_EXAMPLE
                         if (interval, duration) == (template.interval_s, template.duration_s)
                         else PresetBasis.SAMPLE_DERIVED_CANDIDATE),
        "encoding_format": ENCODING_FORMAT, "template_identity": template.candidate_template.value,
        "template_payload": template.command[-1],
    }
    labels = ("01-mode", "02-resolution", "03-timing", "04-exposure")
    return metadata, tuple((label, shlex.join(command)) for label, command in zip(labels, commands))


def timelapse_payload(duration_s=None, *, interval_s=None, time_parameters=None):
    return prepare_timelapse(duration_s, interval_s=interval_s, time_parameters=time_parameters)[0]["preset_payload"]


def timelapse_metadata(duration_s=None, *, interval_s=None, time_parameters=None):
    return prepare_timelapse(duration_s, interval_s=interval_s, time_parameters=time_parameters)[0]


def timelapse_settings(duration_s=None, *, interval_s=None, time_parameters=None):
    return prepare_timelapse(duration_s, interval_s=interval_s, time_parameters=time_parameters)[1]


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
        if not exact_positive(duration_s):
            raise ValueError("采样等待时长必须为精确正数")
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
        # 单调钟与 sleep 使用标准库浮点秒；原精确参数及编码保持原值。
        remaining = started + float(duration_s) - clock()
        while remaining > 0:
            sleep(min(60, remaining))
            remaining = started + float(duration_s) - clock()
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
                  metadata=None) -> dict:
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
        if metadata is not None:
            summary.update(params=metadata["requested_params"], preset_basis=metadata["preset_basis"])
        summary.update(actual_start="unknown", natural_end="unknown",
                       file_write_complete="unknown", output_set_finalized="unknown")
    return summary


def storage_inventory_script(root):
    return (
        f"if [ -d {root} ]; then\n"
        f"find {root} -type f -print0\n"
        "status=$?\nprintf 'CAMCTL_FIND_EXIT=%s\\n' \"$status\" >&2\nexit \"$status\"\n"
        f"elif [ -e {root} ] || [ -L {root} ]; then\n"
        "printf 'CAMCTL_STORAGE_NOT_DIRECTORY\\n' >&2\nexit 1\n"
        "else\nprintf 'CAMCTL_STORAGE_ABSENT\\n' >&2\nfi\n"
    )


def load_observation_source(original: Path, serial: str) -> tuple[dict, set[bytes], dict]:
    observation = exact_json((original / "capture-observation.json").read_text(encoding="utf-8"))
    if not isinstance(observation, dict) or observation.get("capture") != CaptureMode.TIMELAPSE:
        raise RuntimeError("只读复查只接受原生延时的采集记录")
    try:
        sampled_at = datetime.fromisoformat(observation["sampling_started_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("原采样时刻缺失或无效") from error
    if sampled_at.utcoffset() is None:
        raise RuntimeError("原采样时刻缺少时区")
    for key in ("start_call_elapsed_s", "start_to_observation_s"):
        value = observation.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))
                or not math.isfinite(value) or value < 0):
            raise RuntimeError(f"原计时记录 {key} 缺失或无效")

    def check_call(label, root=None):
        call = json.loads((original / label / "call.json").read_text(encoding="utf-8"))
        argv = call.get("argv") if isinstance(call, dict) else None
        if (not isinstance(argv, list) or len(argv) != 6 or not all(isinstance(arg, str) for arg in argv)
                or not argv[0] or argv[1:4] != ["-s", serial, "shell"]):
            raise RuntimeError(f"{label} 的原绑定与本次 serial 不同或记录无效")
        if type(call.get("returncode")) is not int or call["returncode"] != 0:
            raise RuntimeError(f"{label} 的原调用退出状态无效：{call.get('returncode')!r}")
        if root is not None:
            try:
                wrapper = shlex.split(argv[5]) if len(argv) == 6 and argv[4] == "-T" else []
            except (TypeError, ValueError) as error:
                raise RuntimeError(f"{label} 的原目录调用格式无效") from error
            if wrapper != ["sh", "-c", storage_inventory_script(root)]:
                raise RuntimeError(f"{label} 的原目录调用与声明范围 {root} 不一致")

    metadata = original_timelapse_metadata(original, observation, serial)
    check_call("12-start")
    before = set()
    states = []
    for name, root in STORAGE_ROOTS:
        label = "11-before-" + name
        check_call(label, root)
        directory = original / label
        state, paths = parse_directory((directory / "stdout.bin").read_bytes(),
                                       (directory / "stderr.bin").read_bytes())
        if any(not path.startswith(root.encode("utf-8") + b"/") for path in paths):
            raise RuntimeError(f"{label} 的原路径不属于声明范围 {root}")
        states.append(state)
        before.update(paths)
    if all(state is StorageState.ABSENT for state in states):
        raise RuntimeError("原基准的两个存储范围均不存在")
    return observation, before, metadata


def original_observation_layout(original, observation):
    """先按原字段与完整布局分区，缺失／混杂不能逐标签回退。"""
    legacy_fields = {"requested_params", "preset_payload", "preset_basis"}
    version_fields = {"encoding_format", "template_identity", "template_payload"}
    legacy = legacy_fields & observation.keys()
    version = version_fields & observation.keys()
    if legacy and legacy != legacy_fields:
        raise RuntimeError(f"原请求 metadata 缺失：{sorted(legacy_fields - legacy)}")
    if version and version != version_fields:
        raise RuntimeError(f"原 v1 格式事实缺失：{sorted(version_fields - version)}")
    if version and not legacy:
        raise RuntimeError("原 v1 请求 metadata 缺失")
    if "observation_only" in observation and observation["observation_only"] is not False:
        raise RuntimeError("原记录必须是 observation_only=false 的采集记录")
    if legacy and "observation_only" not in observation:
        raise RuntimeError("原请求 metadata 缺少 observation_only=false")
    if version:
        layout = ObservationLayout.CANDIDATE_V1
    elif legacy:
        output = original / "05-output"
        layout = (ObservationLayout.TWO_PRESETS if output.exists() or output.is_symlink()
                  else ObservationLayout.SINGLE_PRESET)
    else:
        layout = (ObservationLayout.EARLIEST_WITH_FLAG if "observation_only" in observation
                  else ObservationLayout.EARLIEST)
    early = layout in (ObservationLayout.EARLIEST, ObservationLayout.EARLIEST_WITH_FLAG)
    expected = {"01-mode", "02-resolution"}
    expected.update(("03-exposure", "04-preset") if early else ("03-timing", "04-exposure"))
    if layout is ObservationLayout.TWO_PRESETS:
        expected.add("05-output")
    known = {label for label, _ in SETTINGS} | {"03-exposure", "04-preset", "03-timing", "05-output"}
    present = {label for label in known if (original / label).exists() or (original / label).is_symlink()}
    if present != expected:
        raise RuntimeError(f"原 {layout} 设置布局缺失或混合：缺少 {sorted(expected - present)}；"
                           f"额外 {sorted(present - expected)}")
    return layout


def original_timelapse_metadata(original, observation, serial):
    """解释原格式与调用事实；不消费当前时间资源或新请求编码。"""
    layout = original_observation_layout(original, observation)
    early = layout in (ObservationLayout.EARLIEST, ObservationLayout.EARLIEST_WITH_FLAG)
    timing_label = "04-preset" if early else "03-timing"
    exposure_label = "03-exposure" if early else "04-exposure"

    def command(label):
        directory = original / label
        call = exact_json((directory / "call.json").read_text(encoding="utf-8"))
        if not isinstance(call, dict):
            raise RuntimeError(f"{label} 的原调用记录必须是对象")
        argv = call.get("argv")
        if (not isinstance(argv, list) or len(argv) != 6 or not all(isinstance(arg, str) for arg in argv)
                or not argv[0] or argv[1:5] != ["-s", serial, "shell", "-T"]
                or type(call.get("returncode")) is not int or call["returncode"] != 0):
            raise RuntimeError(f"{label} 的原调用、绑定或退出状态无效")
        try:
            wrapper = shlex.split(argv[5])
            if len(wrapper) != 3 or wrapper[:2] != ["sh", "-c"]:
                raise ValueError("原 shell 包装无效")
            result = shlex.split(wrapper[2])
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"{label} 的原命令格式无效") from error
        if (directory / "stderr.bin").read_bytes():
            raise RuntimeError(f"{label} 的原调用存在错误输出")
        expect_ack((directory / "stdout.bin").read_bytes())
        return result

    expected_controls = {
        "01-mode": "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
        "02-resolution": "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
        exposure_label: "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100",
        "12-start": "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
    }
    for label, expected in expected_controls.items():
        if command(label) != shlex.split(expected):
            raise RuntimeError(f"{label} 的原延时设置或启动命令不一致")
    timing = command(timing_label)
    prefix = shlex.split("dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c")
    if timing[:-1] != prefix or len(timing) != len(prefix) + 1:
        raise RuntimeError("原延时完整负载调用无效")
    payload = timing[-1]
    try:
        raw = bytes.fromhex(payload)
    except ValueError as error:
        raise RuntimeError("原延时完整负载无法解码") from error
    if len(payload) != 34 or len(raw) != 17:
        raise RuntimeError("原延时完整负载长度不符")
    if layout is ObservationLayout.TWO_PRESETS and command("05-output") != timing:
        raise RuntimeError("原输出与时间负载调用不一致")
    fields = {"requested_params", "preset_payload", "preset_basis"}
    has_metadata = not early
    new_format = layout is ObservationLayout.CANDIDATE_V1
    if new_format:
        required = fields | {"encoding_format", "template_identity", "template_payload"}
        if (not required <= observation.keys() or observation["encoding_format"] != ENCODING_FORMAT
                or observation["template_identity"] != "action6-auto-video"):
            raise RuntimeError("原延时格式身份缺失或无效")
        try:
            template = bytes.fromhex(observation["template_payload"])
        except (TypeError, ValueError) as error:
            raise RuntimeError("原完整模板无效") from error
        if (len(template) != 17 or len(observation["template_payload"]) != 34
                or template[:3] != b"\x04\x00\x00" or template[7:] != b"\x00" * 10
                or int.from_bytes(template[3:5], "little") != 80
                or int.from_bytes(template[5:7], "little") != 1800
                or raw[:3] != template[:3] or raw[7:] != template[7:]):
            raise RuntimeError("原完整负载与原模板非时间字节不一致")
    else:
        if raw[:3] != b"\x04\x00\x00" or raw[7:] != b"\x00" * 10:
            raise RuntimeError("原延时负载不属于可解释的旧格式")
    interval_units = int.from_bytes(raw[3:5], "little")
    interval = Decimal(f"{interval_units // 10}.{interval_units % 10}")
    duration = int.from_bytes(raw[5:7], "little")
    if interval <= 0 or duration <= 0:
        raise RuntimeError("原时间窗口不是正数")
    if not new_format and (interval != 8 or not 1 <= duration <= TIMELAPSE_DURATION_S):
        raise RuntimeError("原延时负载不属于旧诊断时间格式")
    params = {"resolution": "4k30", "exposure": "auto", "interval_s": interval,
              "duration_s": duration, "outputs": "video"}
    basis = (PresetBasis.SOURCE_EXAMPLE if (interval, duration) == (8, 1800) else
             PresetBasis.SAMPLE_DERIVED_CANDIDATE if new_format else PresetBasis.EXPERIMENTAL_DURATION)
    if has_metadata:
        if (not fields <= observation.keys() or observation["requested_params"] != params
                or observation["preset_payload"] != payload or observation["preset_basis"] != basis
                or not isinstance(observation["requested_params"], dict)
                or not all(exact_positive(observation["requested_params"].get(key)) for key in ("interval_s", "duration_s"))):
            raise RuntimeError("原延时参数、完整负载和编码依据缺失或不一致")
        return {field: observation[field] for field in observation if field in fields or field in {"encoding_format", "template_identity", "template_payload"}}
    if new_format or (interval, duration) != (8, 1800):
        raise RuntimeError("原请求参数缺失，不能补齐事实")
    return {"requested_params": params, "preset_payload": payload, "preset_basis": basis}


def seconds_argument(text):
    try:
        value = exact_json(text)
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError("不是数值")
        return value
    except ValueError as error:
        raise argparse.ArgumentTypeError("时间必须是精确 JSON 数值") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="配置 Action6，诊断普通录像或共同时间域内的原生延时；旧记录只读复查")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--capture", choices=tuple(CaptureMode), help="新拍摄，默认为 record")
    operation.add_argument("--observe", type=Path, help="沿原延时采集目录的基准进行只读复查")
    parser.add_argument("--timelapse-duration-s", type=seconds_argument,
                        help="新延时采集时长（精确秒数）；省略时从共同资源派生最短时长")
    parser.add_argument("--timelapse-interval-s", type=seconds_argument,
                        help="新延时采集间隔（精确秒数）；省略时从共同资源派生最短间隔")
    parser.add_argument("--adb", default="./adb.exe")
    parser.add_argument("--serial", default="123456789ABCDEF")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    mode = CaptureMode.TIMELAPSE if args.observe is not None else CaptureMode(args.capture or CaptureMode.RECORD)
    if (args.timelapse_duration_s is not None or args.timelapse_interval_s is not None) and (args.observe is not None or mode is not CaptureMode.TIMELAPSE):
        parser.error("时间选项只用于新建 --capture timelapse")
    requested = settings = None
    duration_s = TIMELAPSE_DURATION_S
    if args.observe is None and mode is CaptureMode.TIMELAPSE:
        try:
            requested, settings = prepare_timelapse(args.timelapse_duration_s, interval_s=args.timelapse_interval_s)
            duration_s = requested["requested_params"]["duration_s"]
        except Exception as error:
            parser.error(f"新延时请求无法准备：{type(error).__name__}: {error}")
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("请使用现有 Python 3.11 环境")

    collector = Path(__file__).resolve().with_name("collect_call.py")
    if not collector.is_file():
        raise RuntimeError("请将 collect_call.py 放在本脚本所在目录")
    original = args.observe.resolve() if args.observe is not None else None
    if original is not None:
        original_observation, before, requested = load_observation_source(original, args.serial)
        duration_s = requested["requested_params"]["duration_s"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base = (original / ("observation-" + stamp) if original is not None
            else Path("action6") / (mode.value + "-probe-" + stamp))
    base.mkdir(parents=True, exist_ok=False)
    print(f"采集目录：{base.resolve()}", flush=True)
    if original is None and requested is not None:
        print(f"延时要求：间隔 {requested['requested_params']['interval_s']} 秒，持续 {duration_s} 秒，仅视频；"
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
            script = storage_inventory_script(root)
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
        for label, script in (SETTINGS if mode is CaptureMode.RECORD else settings):
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
        json_text(observation), encoding="utf-8")
    after = read_inventory("14-after")
    summary = assess_sample(mode, before, after, metadata=requested)
    summary["observation"] = observation
    if original is None:
        summary["timing"] = timing

    def save_summary():
        temporary = base / "summary.json.tmp"
        temporary.write_text(json_text(summary), encoding="utf-8")
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
    print(json_text(summary))
    print(f"详细记录：{(base / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
