"""诊断脚本与真实采集器的集成测试；所有设备和媒体工具都是替身。"""

import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "action6_record_probe.py"
SERIAL = "123456789ABCDEF"
INTERNAL = "/mnt/media_rw/emulated/DCIM"
SD = "/mnt/media_rw/sd/DCIM"
VIDEO = INTERNAL + "/DJI_001/late.MP4"
SECOND_VIDEO = INTERNAL + "/DJI_001/second.MP4"
CONTENT = b"CAMCTL_FAKE_VIDEO"
TIMELAPSE_START = "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01"
SHORT_PRESETS = {
    300: "0400000a002c0100000000000000000000",
    1200: "0400000a00b00400000000000000000000",
    30: "04000050001e0000000000000000000000",
    10: "04000050000a0000000000000000000000",
}


# 替身只接受试验声明的调用。目录列举执行实际 shell/find，路径映射到
# 测试独占的临时文件系统；控制响应、源文件与媒体结果按接口提供。
FAKE_TOOL = r'''
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

args = sys.argv[1:]
state_file = Path(os.environ["CAMCTL_PROBE_INTEGRATION_STATE"])
state = json.loads(state_file.read_text())
workspace = state_file.parent
with (workspace / "trace.jsonl").open("a", encoding="utf-8") as trace:
    trace.write(json.dumps(args) + "\n")
internal = "/mnt/media_rw/emulated/DCIM"
sd = "/mnt/media_rw/sd/DCIM"
source = internal + "/DJI_001/late.MP4"
second_source = internal + "/DJI_001/second.MP4"
known_sources = {source, second_source}
content = b"CAMCTL_FAKE_VIDEO"

def local_file(remote):
    assert remote.startswith(internal + "/"), remote
    return workspace / "storage" / "internal" / remote[len(internal) + 1:]

if args == ["-version"]:
    if state.get("version_failure"):
        sys.stderr.write("MEDIA_VERSION_REJECTED\n")
        sys.exit(23)
    print("ffprobe TEST DOUBLE")
elif args[:3] == ["-v", "error", "-show_entries"]:
    assert len(args) == 7 and args[4:6] == ["-of", "json"], args
    assert args[3] == "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration:format=format_name,duration,size", args
    assert Path(args[6]).read_bytes() == content
    copied_source = json.loads(Path(args[6]).with_name(Path(args[6]).stem + "-copy.json").read_text())["source"]
    if state.get("media_failure") or copied_source == state.get("media_failure_source"):
        sys.stderr.write("MEDIA_TOOL_REJECTED\n")
        sys.exit(23)
    print(json.dumps({
        "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840,
                     "height": 2160, "r_frame_rate": "30000/1001",
                     "avg_frame_rate": "30000/1001", "duration": "7.5"}],
        "format": {"duration": "7.6", "size": str(len(content)), "format_name": "mov,mp4"},
    }))
elif args[:3] == ["-s", "123456789ABCDEF", "pull"]:
    assert len(args) == 5 and args[3] in known_sources, args
    pending = json.loads((Path(args[4]).parent / "summary.json").read_text())
    assert pending["video_checks"] == "incomplete", pending
    assert args[3] in pending["observed_mp4_files"], pending
    if state.get("pull_failure"):
        sys.stderr.write("PULL_REJECTED\n")
        sys.exit(31)
    Path(args[4]).write_bytes(b"CORRUPT_COPY" if state.get("copy_mismatch") else local_file(args[3]).read_bytes())
    sys.stderr.write("1 file pulled\n")
else:
    assert args[:4] == ["-s", "123456789ABCDEF", "shell", "-T"] and len(args) == 5, args
    wrapper = shlex.split(args[4])
    assert len(wrapper) == 3 and wrapper[:2] == ["sh", "-c"], wrapper
    command = wrapper[2]
    if command.startswith("if [ -d "):
        root = sd if sd in command else internal
        assert root in command and "find " + root + " -type f -print0" in command, command
        assert "CAMCTL_FIND_EXIT=" in command and "CAMCTL_STORAGE_ABSENT" in command, command
        if state.get("directory_failure") == "exit":
            sys.stderr.write("DIRECTORY_READ_REJECTED\n")
            sys.exit(255)
        if state.get("directory_failure") == "truncated":
            sys.stdout.buffer.write((internal + "/truncated.MP4").encode())
            sys.stderr.write("CAMCTL_FIND_EXIT=0\n")
            sys.exit(0)
        mapped = workspace / "storage" / ("sd" if root == sd else "internal")
        result = subprocess.run(["sh", "-c", command.replace(root, shlex.quote(str(mapped)))], capture_output=True)
        sys.stdout.buffer.write(result.stdout.replace(str(mapped).encode(), root.encode()))
        sys.stderr.buffer.write(result.stderr)
        sys.exit(result.returncode)
    elif command.startswith("find "):
        assert command.startswith("find " + internal + " -type f -print0"), command
        mapped = workspace / "storage" / "internal"
        result = subprocess.run(["sh", "-c", command.replace(internal, shlex.quote(str(mapped)))], capture_output=True)
        sys.stdout.buffer.write(result.stdout.replace(str(mapped).encode(), internal.encode()))
        sys.stderr.buffer.write(result.stderr)
        sys.exit(result.returncode)
    elif command.startswith("test -f "):
        tokens = shlex.split(command)
        assert len(tokens) == 10 and tokens[:2] == ["test", "-f"], tokens
        assert tokens[3:10] == ["&&", "LC_ALL=C", "stat", "-c", "%s", "--", tokens[2]], tokens
        assert tokens[2] in known_sources, tokens
        calls = state.get("size_calls", 0) + 1
        state["size_calls"] = calls
        state_file.write_text(json.dumps(state))
        if state.get("after_source_failure") and calls == 2:
            sys.stderr.write("SOURCE_AFTER_REJECTED\n")
            sys.exit(255)
        print(local_file(tokens[2]).stat().st_size)
    elif command.startswith("LC_ALL=C sha256sum < "):
        tokens = shlex.split(command)
        assert len(tokens) == 4 and tokens[:3] == ["LC_ALL=C", "sha256sum", "<"] and tokens[3] in known_sources, command
        print(hashlib.sha256(local_file(tokens[3]).read_bytes()).hexdigest() + "  -")
    elif command == "simulate_device -s bitrate 2":
        sys.stdout.buffer.write(b"name [DeviceRecordRecSettingBitRate]\nlink to server rlt 0\nregister to server successs\n")
    else:
        allowed = {
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100",
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10",
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42 3d",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00",
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400005000080700000000000000000000",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000050001e0000000000000000000000",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000050000a0000000000000000000000",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
        }
        allowed.update({"dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c " + payload for payload in ("0400000a002c0100000000000000000000", "0400000a00b00400000000000000000000")})
        assert command in allowed, command
        control_calls = state.get("control_calls", 0) + 1
        state["control_calls"] = control_calls
        state_file.write_text(json.dumps(state))
        if control_calls == state.get("control_failure_at"):
            failure = state["control_failure"]
            if failure == "exit":
                sys.stderr.write("CONTROL_REJECTED\n")
                sys.exit(41)
            if failure == "stderr":
                sys.stderr.write("CONTROL_WARNING\n")
            elif failure == "nonzero_ack":
                sys.stdout.buffer.write(b"Resp message, len = 1, data:\n  e3\n")
                sys.exit(0)
            elif failure == "truncated_ack":
                sys.stdout.buffer.write(b"Resp message, len = 2, data:\n  00\n")
                sys.exit(0)
            else:
                raise AssertionError(failure)
        if command.endswith(("-c 01 01", "-c 02 01")) and state.get("video_at_start"):
            target = local_file(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        sys.stdout.buffer.write(b"Resp message, len = 1, data:\n  00\n")
'''


class ProbeIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="camctl-probe-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.state_file = self.workspace / "tool-state.json"
        self.state_file.write_text("{}", encoding="utf-8")
        self.tool = self.workspace / "fake-tool"
        self.tool.write_text("#!" + sys.executable + "\n" + FAKE_TOOL, encoding="utf-8")
        self.tool.chmod(0o755)
        internal = self.workspace / "storage" / "internal"
        internal.mkdir(parents=True)
        (internal / "old.JPG").write_bytes(b"OLD")
        spec = importlib.util.spec_from_file_location("probe_integration", SCRIPT)
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)
        self.clock_seconds = 0.0
        self.sleep_calls = []

    def set_state(self, **changes):
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state.update(changes)
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def invoke(self, *args):
        def clock():
            return self.clock_seconds

        def sleep(seconds):
            self.sleep_calls.append(seconds)
            self.clock_seconds += seconds

        capture = functools.partial(self.probe.capture_once, clock=clock, sleep=sleep)
        argv = [str(SCRIPT), "--adb", str(self.tool), "--ffprobe", str(self.tool), *map(str, args)]
        previous = Path.cwd()
        os.chdir(self.workspace)
        try:
            with patch.dict(os.environ, {"CAMCTL_PROBE_INTEGRATION_STATE": str(self.state_file)}), \
                    patch.object(sys, "argv", argv), \
                    patch.object(self.probe, "capture_once", capture), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.probe.main()
        finally:
            os.chdir(previous)

    def historical_record(self, layout):
        # 独立归档格式：最早四步、旧双预设及后续四步均直接保存原字面量。
        # 不调用现代 seed_original、prepare_timelapse 或候选编码。
        original = self.workspace / "history" / layout
        original.mkdir(parents=True)
        early = layout in ("A0", "A1")
        observation = {"capture": "timelapse", "sampling_started_at": "2026-10-10T12:00:00+00:00",
                       "start_call_elapsed_s": 2, "start_to_observation_s": 1800 if early else 30}
        if layout != "A0":
            observation["observation_only"] = False
        payload = "0400005000080700000000000000000000" if early else "04000050001e0000000000000000000000"
        if not early:
            observation.update(requested_params={"resolution": "4k30", "exposure": "auto", "interval_s": 8,
                                                 "duration_s": 30, "outputs": "video"},
                               preset_payload=payload, preset_basis="experimental_duration")
        (original / "capture-observation.json").write_text(json.dumps(observation))
        common = (
            ("01-mode", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02"),
            ("02-resolution", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000"))
        exposure = "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100"
        timing = "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c " + payload
        settings = (("03-exposure", exposure), ("04-preset", timing)) if early else (("03-timing", timing), ("04-exposure", exposure))
        if layout == "B0":
            settings += (("05-output", timing),)
        for label, command in (*common, *settings, ("12-start", TIMELAPSE_START)):
            self.archive_call(original, label, command, b"Resp message, len = 1, data:\n  00\n", b"")
        for name, remote in (("internal", INTERNAL), ("sd", SD)):
            script = (f"if [ -d {remote} ]; then\n"
                      f"find {remote} -type f -print0\n"
                      "status=$?\nprintf 'CAMCTL_FIND_EXIT=%s\\n' \"$status\" >&2\nexit \"$status\"\n"
                      f"elif [ -e {remote} ] || [ -L {remote} ]; then\n"
                      "printf 'CAMCTL_STORAGE_NOT_DIRECTORY\\n' >&2\nexit 1\n"
                      "else\nprintf 'CAMCTL_STORAGE_ABSENT\\n' >&2\nfi\n")
            self.archive_call(original, "11-before-" + name, script,
                              (INTERNAL + "/old.JPG").encode() + b"\0" if name == "internal" else b"",
                              b"CAMCTL_FIND_EXIT=0\n" if name == "internal" else b"CAMCTL_STORAGE_ABSENT\n")
        return original

    def seed_original(self, duration_s=1800):
        # 旧归档夹具直接保留原请求与原调用，不通过当前新请求入口制造。
        original = self.workspace / "action6" / "timelapse-probe-archived"
        original.mkdir(parents=True)
        payload = {1800: "0400005000080700000000000000000000", **{k: v for k, v in SHORT_PRESETS.items() if k in (30, 10)}}[duration_s]
        basis = "source_example" if duration_s == 1800 else "experimental_duration"
        observation = {"capture": "timelapse", "observation_only": False,
                       "sampling_started_at": "2026-10-10T12:00:00+00:00",
                       "start_call_elapsed_s": 2, "start_to_observation_s": duration_s,
                       "requested_params": {"resolution": "4k30", "exposure": "auto", "interval_s": 8,
                                            "duration_s": duration_s, "outputs": "video"},
                       "preset_payload": payload, "preset_basis": basis}
        (original / "capture-observation.json").write_text(json.dumps(observation))
        (original / "summary.json").write_text("{}")
        controls = self.expected_short_controls(duration_s) if duration_s != 1800 else [
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c " + payload,
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100", TIMELAPSE_START]
        for label, command in zip(("01-mode", "02-resolution", "03-timing", "04-exposure", "12-start"), controls):
            self.archive_call(original, label, command, b"Resp message, len = 1, data:\n  00\n", b"")
        for name, remote in (("internal", INTERNAL), ("sd", SD)):
            storage = self.workspace / "storage" / name
            paths = sorted(remote + "/" + str(p.relative_to(storage)) for p in storage.rglob("*") if p.is_file()) if storage.is_dir() else []
            script = (f"if [ -d {remote} ]; then\n"
                      f"find {remote} -type f -print0\n"
                      "status=$?\nprintf 'CAMCTL_FIND_EXIT=%s\\n' \"$status\" >&2\nexit \"$status\"\n"
                      f"elif [ -e {remote} ] || [ -L {remote} ]; then\n"
                      "printf 'CAMCTL_STORAGE_NOT_DIRECTORY\\n' >&2\nexit 1\n"
                      "else\nprintf 'CAMCTL_STORAGE_ABSENT\\n' >&2\nfi\n")
            self.archive_call(original, "11-before-" + name, script,
                              b"".join(p.encode() + b"\0" for p in paths),
                              b"CAMCTL_FIND_EXIT=0\n" if storage.is_dir() else b"CAMCTL_STORAGE_ABSENT\n")
        return original

    def archive_call(self, original, label, command, stdout, stderr):
        import shlex
        directory = original / label
        directory.mkdir()
        (directory / "call.json").write_text(json.dumps({"argv": [str(self.tool), "-s", SERIAL, "shell", "-T", "sh -c " + shlex.quote(command)], "returncode": 0}))
        (directory / "stdout.bin").write_bytes(stdout)
        (directory / "stderr.bin").write_bytes(stderr)

    def seed_new(self, duration_s=None, interval_s=None):
        args = ["--capture", "timelapse"]
        if duration_s is not None:
            args.extend(["--timelapse-duration-s", duration_s])
        if interval_s is not None:
            args.extend(["--timelapse-interval-s", interval_s])
        with self.assertRaises(RuntimeError):
            self.invoke(*args)
        original, = (self.workspace / "action6").glob("timelapse-probe-*")
        return original

    def seed_five_setting_capture(self, duration_s):
        controls = self.expected_short_controls(duration_s)
        original = self.seed_original(duration_s)
        self.archive_call(original, "05-output", controls[2], b"Resp message, len = 1, data:\n  00\n", b"")
        return original

    def trace(self):
        path = self.workspace / "trace.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def control_commands(self, calls=None):
        return [shlex_command(args) for args in (self.trace() if calls is None else calls)
                if args[:4] == ["-s", SERIAL, "shell", "-T"]
                and shlex_command(args).startswith("dji_mb_ctrl ")]

    @staticmethod
    def expected_short_controls(duration_s):
        preset = "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c " + SHORT_PRESETS[duration_s]
        return [
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
            preset,
            "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100",
            TIMELAPSE_START,
        ]

    @staticmethod
    def snapshot(original):
        return {str(path.relative_to(original)): path.read_bytes()
                for path in original.rglob("*") if path.is_file() and not any(
                    part.startswith("observation-") for part in path.relative_to(original).parts)}

    @staticmethod
    def read_summary(directory):
        return json.loads((directory / "summary.json").read_text(encoding="utf-8"))

    def add_video(self, source=VIDEO):
        path = self.workspace / "storage" / "internal" / source[len(INTERNAL) + 1:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CONTENT)

    def assert_unknown_completion(self, summary):
        for field in ("actual_start", "natural_end", "file_write_complete", "output_set_finalized"):
            self.assertEqual(summary[field], "unknown")

    def assert_original_rejected_without_device_calls(self, original):
        prior = len(self.trace())
        with self.assertRaises((RuntimeError, ValueError, OSError)):
            self.invoke("--observe", original)
        new_calls = self.trace()[prior:]
        self.assertEqual([args for args in new_calls if args[:1] == ["-s"]], [])

    def test_new_capture_without_mp4_keeps_observation_and_summary(self):
        original = self.seed_new()
        summary = self.read_summary(original)
        self.assertEqual(summary["sample_status"], "no_new_mp4")
        self.assertEqual(summary["new_files"], [])
        self.assertEqual(summary["observed_mp4_files"], [])
        self.assertEqual(summary["videos"], [])
        self.assertEqual(summary["video_checks"], "not_attempted")
        self.assert_unknown_completion(summary)
        observation = json.loads((original / "capture-observation.json").read_text())
        self.assertIs(observation["observation_only"], False)
        self.assertEqual(observation["start_to_observation_s"], 300)
        commands = [args[4] for args in self.trace() if args[:4] == ["-s", SERIAL, "shell", "-T"]]
        self.assertEqual(sum("-c 01 01" in command for command in commands), 1)
        self.assertFalse(any("-c 01 00" in command or "-c 02 00" in command for command in commands))

    def assert_short_capture(self, duration_s):
        original = self.seed_new(duration_s)
        expected = self.expected_short_controls(duration_s)
        self.assertEqual(self.control_commands(), expected)
        self.assertEqual(sum(self.sleep_calls), duration_s)
        labels = ("01-mode", "02-resolution", "03-timing", "04-exposure")
        for label, command in zip(labels, expected):
            with self.subTest(label=label):
                metadata = json.loads((original / label / "call.json").read_text())
                self.assertEqual(shlex_command(metadata["argv"][1:]), command)
                self.assertEqual(metadata["returncode"], 0)
                self.assertEqual((original / label / "stdout.bin").read_bytes(),
                                 b"Resp message, len = 1, data:\n  00\n")
        self.assertFalse((original / "05-output").exists())
        expected_params = {"resolution": "4k30", "exposure": "auto", "interval_s": 1,
                           "duration_s": duration_s, "outputs": "video"}
        observation = json.loads((original / "capture-observation.json").read_text())
        self.assertEqual(observation["requested_params"], expected_params)
        self.assertEqual(observation["preset_payload"], SHORT_PRESETS[duration_s])
        self.assertEqual(len(bytes.fromhex(observation["preset_payload"])), 17)
        self.assertEqual(observation["preset_basis"], "sample_derived_candidate")
        self.assertEqual(observation["start_to_observation_s"], duration_s)
        summary = self.read_summary(original)
        self.assertEqual(summary["params"], expected_params)
        self.assertEqual(summary["preset_basis"], "sample_derived_candidate")
        self.assertEqual(summary["sample_status"], "no_new_mp4")
        self.assertEqual(summary["video_checks"], "not_attempted")
        self.assert_unknown_completion(summary)

    def test_300_second_capture_keeps_complete_source_order_and_experiment_metadata(self):
        self.assert_short_capture(300)

    def test_1200_second_capture_keeps_complete_source_order_and_experiment_metadata(self):
        self.assert_short_capture(1200)

    def test_original_duration_keeps_source_example_basis_and_one_full_preset(self):
        original = self.seed_new(1800, 8)
        observation = json.loads((original / "capture-observation.json").read_text())
        self.assertEqual(observation["preset_payload"], "0400005000080700000000000000000000")
        self.assertEqual(observation["preset_basis"], "source_example")

    def test_each_setting_failure_stops_before_start_and_keeps_raw_call(self):
        expected = self.expected_short_controls(300)
        labels = ("01-mode", "02-resolution", "03-timing", "04-exposure")
        for step, label in enumerate(labels, 1):
            for failure in ("exit", "stderr", "nonzero_ack", "truncated_ack"):
                with self.subTest(step=label, failure=failure):
                    self.set_state(control_calls=0, control_failure_at=step,
                                   control_failure=failure)
                    prior = len(self.trace())
                    previous = set((self.workspace / "action6").glob("timelapse-probe-*"))
                    with self.assertRaises(RuntimeError):
                        self.invoke("--capture", "timelapse", "--timelapse-duration-s", 300)
                    calls = self.trace()[prior:]
                    self.assertEqual(self.control_commands(calls), expected[:step])
                    current, = set((self.workspace / "action6").glob("timelapse-probe-*")) - previous
                    metadata = json.loads((current / label / "call.json").read_text())
                    self.assertEqual(shlex_command(metadata["argv"][1:]), expected[step - 1])
                    self.assertEqual(metadata["returncode"], 41 if failure == "exit" else 0)
                    self.assertFalse((current / "12-start").exists())
                    if failure == "exit":
                        self.assertEqual((current / label / "stderr.bin").read_bytes(),
                                         b"CONTROL_REJECTED\n")
                    elif failure == "stderr":
                        self.assertEqual((current / label / "stderr.bin").read_bytes(),
                                         b"CONTROL_WARNING\n")
                    elif failure == "nonzero_ack":
                        self.assertEqual((current / label / "stdout.bin").read_bytes(),
                                         b"Resp message, len = 1, data:\n  e3\n")
                    else:
                        self.assertEqual((current / label / "stdout.bin").read_bytes(),
                                         b"Resp message, len = 2, data:\n  00\n")

    def test_media_tool_preparation_failure_does_not_configure_or_start(self):
        self.set_state(version_failure=True)
        with self.assertRaises(RuntimeError):
            self.invoke("--capture", "timelapse", "--timelapse-duration-s", 300)
        self.assertEqual(self.control_commands(), [])
        self.assertEqual([args for args in self.trace() if args[:1] == ["-s"]], [])

    def test_original_directory_failure_does_not_start_after_settings(self):
        self.set_state(directory_failure="exit")
        with self.assertRaises(RuntimeError):
            self.invoke("--capture", "timelapse", "--timelapse-duration-s", 300)
        self.assertEqual(self.control_commands(), self.expected_short_controls(300)[:-1])
        original, = (self.workspace / "action6").glob("timelapse-probe-*")
        self.assertFalse((original / "12-start").exists())

    def test_short_start_failure_is_attempted_once_without_stop_or_wait(self):
        self.set_state(control_failure_at=5, control_failure="nonzero_ack")
        with self.assertRaises(RuntimeError) as caught:
            self.invoke("--capture", "timelapse", "--timelapse-duration-s", 300)
        self.assertIn("e3", str(caught.exception))
        self.assertEqual(self.control_commands(), self.expected_short_controls(300))
        self.assertEqual(self.sleep_calls, [])
        original, = (self.workspace / "action6").glob("timelapse-probe-*")
        self.assertEqual((original / "12-start" / "stdout.bin").read_bytes(),
                         b"Resp message, len = 1, data:\n  e3\n")

    def test_observe_late_short_capture_video_preserves_selected_duration(self):
        original = self.seed_original(30)
        before = self.snapshot(original)
        self.add_video()
        prior = len(self.trace())
        self.sleep_calls.clear()
        self.invoke("--observe", original)
        current, = original.glob("observation-*")
        summary = self.read_summary(current)
        self.assertEqual(summary["params"], {"resolution": "4k30", "exposure": "auto",
                                          "interval_s": 8, "duration_s": 30, "outputs": "video"})
        self.assertEqual(summary["preset_basis"], "experimental_duration")
        self.assertEqual(summary["videos"][0]["source"], VIDEO)
        self.assertEqual(summary["video_checks"], "complete")
        self.assert_unknown_completion(summary)
        self.assertEqual(self.control_commands(self.trace()[prior:]), [])
        self.assertEqual(self.sleep_calls, [])
        self.assertEqual(self.snapshot(original), before)

    def assert_five_setting_capture_observation(self, duration_s):
        original = self.seed_five_setting_capture(duration_s)
        before = self.snapshot(original)
        self.add_video()
        prior = len(self.trace())
        self.sleep_calls.clear()
        self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["params"], {"resolution": "4k30", "exposure": "auto",
                                          "interval_s": 8, "duration_s": duration_s, "outputs": "video"})
        self.assertEqual(summary["preset_basis"], "experimental_duration")
        self.assertEqual(summary["videos"][0]["source"], VIDEO)
        self.assertEqual(summary["video_checks"], "complete")
        self.assert_unknown_completion(summary)
        observed = json.loads((observation / "capture-observation.json").read_text())
        self.assertEqual(observed["original_capture_observation"],
                         json.loads(before["capture-observation.json"]))
        calls = self.trace()[prior:]
        self.assertEqual(self.control_commands(calls), [])
        commands = [shlex_command(args) for args in calls
                    if args[:4] == ["-s", SERIAL, "shell", "-T"]]
        self.assertFalse(any("simulate_device" in command or "rm " in command for command in commands))
        self.assertEqual(self.sleep_calls, [])
        self.assertEqual(self.snapshot(original), before)

    def test_observe_five_setting_30_second_capture_preserves_original_request(self):
        self.assert_five_setting_capture_observation(30)

    def test_observe_five_setting_10_second_capture_preserves_original_request(self):
        self.assert_five_setting_capture_observation(10)

    def test_observe_without_new_files_does_not_wait_or_mutate_original(self):
        original = self.seed_original()
        before = self.snapshot(original)
        prior = len(self.trace())
        self.sleep_calls.clear()
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["sample_status"], "no_new_mp4")
        self.assertEqual(summary["video_checks"], "not_attempted")
        self.assert_unknown_completion(summary)
        self.assertEqual(self.sleep_calls, [])
        calls = self.trace()[prior:]
        device_calls = [args for args in calls if args[:1] == ["-s"]]
        self.assertEqual(len(device_calls), 2)
        self.assertTrue(all(shlex_command(args).startswith("if [ -d ") for args in device_calls))
        self.assertEqual(self.snapshot(original), before)
        observed = json.loads((observation / "capture-observation.json").read_text())
        self.assertEqual(observed["capture"], "timelapse")
        self.assertIs(observed["observation_only"], True)
        self.assertEqual(Path(observed["source_capture_directory"]), original)
        self.assertEqual(observed["original_capture_observation"], json.loads(before["capture-observation.json"]))
        self.assertNotIn("start_call_elapsed_s", observed)
        self.assertNotIn("start_to_observation_s", observed)
        self.assertIn("sampling_started_at", observed)

    def test_observe_new_non_mp4_keeps_path_before_reporting_missing_video(self):
        original = self.seed_original()
        (self.workspace / "storage" / "internal" / "new.LRF").write_bytes(b"LRF")
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["new_files"], [INTERNAL + "/new.LRF"])
        self.assertEqual(summary["observed_mp4_files"], [])
        self.assertEqual(summary["sample_status"], "no_new_mp4")
        self.assertEqual(summary["video_checks"], "not_attempted")

    def test_observe_late_mp4_downloads_once_and_records_real_copy_checks(self):
        original = self.seed_original()
        before = self.snapshot(original)
        self.add_video()
        prior = len(self.trace())
        self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["new_files"], [VIDEO])
        self.assertEqual(summary["observed_mp4_files"], [VIDEO])
        self.assertEqual(summary["sample_status"], "mp4_observed")
        self.assertEqual(summary["video_checks"], "complete")
        self.assert_unknown_completion(summary)
        video, = summary["videos"]
        self.assertEqual(video["source"], VIDEO)
        self.assertEqual(video["bytes"], len(CONTENT))
        self.assertEqual(video["sha256"], hashlib.sha256(CONTENT).hexdigest())
        self.assertEqual(video["duration_s"], "7.6")
        self.assertIs(video["matches_source_after"], True)
        self.assertEqual((observation / "video-01.mp4").read_bytes(), CONTENT)
        copy = json.loads((observation / "video-01-copy.json").read_text())
        self.assertEqual(copy["source_before"], copy["source_after"])
        self.assertEqual(copy["source_after"], copy["local_facts"])
        calls = self.trace()[prior:]
        self.assertEqual(sum(args[:3] == ["-s", SERIAL, "pull"] for args in calls), 1)
        commands = [shlex_command(args) for args in calls if args[:4] == ["-s", SERIAL, "shell", "-T"]]
        self.assertEqual(sum(command.startswith("test -f ") for command in commands), 2)
        self.assertEqual(sum(command.startswith("LC_ALL=C sha256sum < ") for command in commands), 2)
        self.assertFalse(any("dji_mb_ctrl" in command or "simulate_device" in command or "rm " in command for command in commands))
        self.assertEqual(self.snapshot(original), before)

    def test_two_observations_keep_separate_records_and_original_baseline(self):
        original = self.seed_original()
        before = self.snapshot(original)
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        self.add_video()
        self.invoke("--observe", original)
        observations = sorted(original.glob("observation-*"))
        self.assertEqual(len(observations), 2)
        self.assertEqual(sorted(self.read_summary(path)["sample_status"] for path in observations),
                         ["mp4_observed", "no_new_mp4"])
        self.assertEqual(self.snapshot(original), before)

    def test_raw_baseline_is_authority_for_observe(self):
        original = self.seed_original()
        (original / "11-before-storage.json").write_text(json.dumps({INTERNAL: {"state": "directory", "paths": [VIDEO]}}))
        self.add_video()
        self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        self.assertEqual(self.read_summary(observation)["new_files"], [VIDEO])

    def test_observe_accepts_original_capture_without_new_summary_fields(self):
        original = self.historical_record("A0")
        before = self.snapshot(original)
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        current, = original.glob("observation-*")
        summary = self.read_summary(current)
        self.assertEqual(summary["sample_status"], "no_new_mp4")
        self.assertEqual(summary["params"]["duration_s"], 1800)
        self.assertEqual(summary["preset_basis"], "source_example")
        self.assertEqual(self.snapshot(original), before)

    def test_observe_accepts_reliable_empty_original_internal_directory(self):
        (self.workspace / "storage" / "internal" / "old.JPG").unlink()
        original = self.seed_original()
        self.assertEqual((original / "11-before-internal" / "stdout.bin").read_bytes(), b"")
        self.add_video()
        self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        self.assertEqual(self.read_summary(observation)["new_files"], [VIDEO])

    def test_observe_accepts_present_empty_sd_directory(self):
        (self.workspace / "storage" / "sd").mkdir()
        original = self.seed_original()
        self.assertEqual((original / "11-before-sd" / "stdout.bin").read_bytes(), b"")
        self.assertEqual((original / "11-before-sd" / "stderr.bin").read_bytes(), b"CAMCTL_FIND_EXIT=0\n")
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        storage = json.loads((observation / "14-after-storage.json").read_text())
        self.assertEqual(storage[SD], {"state": "directory", "paths": []})

    def test_capture_and_observe_are_mutually_exclusive(self):
        original = self.seed_original()
        prior = len(self.trace())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.invoke("--capture", "timelapse", "--observe", original)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.trace()[prior:], [])

    def test_duration_option_rejects_default_record_before_tool_calls(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.invoke("--timelapse-duration-s", 30)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.trace(), [])

    def test_duration_option_rejects_explicit_record_before_tool_calls(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.invoke("--capture", "record", "--timelapse-duration-s", 30)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.trace(), [])

    def test_duration_option_rejects_observe_before_tool_calls(self):
        original = self.seed_original(30)
        prior = len(self.trace())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.invoke("--observe", original, "--timelapse-duration-s", 30)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.trace()[prior:], [])

    def test_duration_option_rejects_invalid_seconds_before_tool_calls(self):
        for value in ("0", "-1", "30", "1801", "1.5", "abc", "true", "NaN", "Infinity", "300.000000000000000001"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    self.invoke("--capture", "timelapse", "--timelapse-duration-s", value)
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(self.trace(), [])

    def test_observe_rejects_partial_short_duration_metadata_before_device_call(self):
        original = self.seed_original(30)
        path = original / "capture-observation.json"
        valid = path.read_bytes()
        for field in ("requested_params", "preset_payload", "preset_basis"):
            with self.subTest(missing=field):
                observation = json.loads(valid)
                del observation[field]
                path.write_text(json.dumps(observation))
                self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_inconsistent_short_duration_metadata_before_device_call(self):
        original = self.seed_original(30)
        path = original / "capture-observation.json"
        valid = path.read_bytes()
        mutations = (
            ("preset_payload", SHORT_PRESETS[10]),
            ("preset_payload", "04000050001e00"),
            ("preset_basis", "source_example"),
            ("preset_basis", "unknown"),
            ("requested_params", None),
            ("requested_params", {"resolution": "4k30", "exposure": "auto", "interval_s": 8,
                                  "duration_s": 10, "outputs": "video"}),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                observation = json.loads(valid)
                observation[field] = value
                path.write_text(json.dumps(observation))
                self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_changed_timelapse_parameter_before_device_call(self):
        original = self.seed_original(30)
        path = original / "capture-observation.json"
        valid = path.read_bytes()
        for field, value in (("resolution", "1080p30"), ("exposure", "manual"),
                             ("interval_s", 1), ("duration_s", 0), ("duration_s", 1801),
                             ("duration_s", True), ("outputs", "video_and_photos")):
            with self.subTest(field=field, value=value):
                observation = json.loads(valid)
                observation["requested_params"][field] = value
                path.write_text(json.dumps(observation))
                self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_original_mode_before_device_call(self):
        original = self.seed_original()
        path = original / "capture-observation.json"
        observation = json.loads(path.read_text())
        observation["capture"] = "record"
        path.write_text(json.dumps(observation))
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_missing_original_capture_observation(self):
        original = self.seed_original()
        (original / "capture-observation.json").unlink()
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_invalid_original_sampling_time(self):
        original = self.seed_original()
        path = original / "capture-observation.json"
        valid = path.read_bytes()
        for value in ("invalid", "2026-10-10T12:00:00", None):
            with self.subTest(sampling_started_at=value):
                observation = json.loads(valid)
                observation["sampling_started_at"] = value
                path.write_text(json.dumps(observation))
                self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_invalid_original_elapsed_values(self):
        original = self.seed_original()
        path = original / "capture-observation.json"
        valid = path.read_bytes()
        for field in ("start_call_elapsed_s", "start_to_observation_s"):
            for value in (-1, float("inf"), float("nan"), "1800", True):
                with self.subTest(field=field, value=value):
                    observation = json.loads(valid)
                    observation[field] = value
                    path.write_text(json.dumps(observation))
                    self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_changed_requested_serial(self):
        original = self.seed_original()
        prior = len(self.trace())
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original, "--serial", "DIFFERENT_DEVICE")
        self.assertEqual([args for args in self.trace()[prior:] if args[:1] == ["-s"]], [])

    def test_observe_rejects_original_call_serial_mismatch(self):
        original = self.seed_original()
        for label in ("11-before-internal", "11-before-sd", "12-start"):
            with self.subTest(label=label):
                path = original / label / "call.json"
                valid = path.read_bytes()
                metadata = json.loads(valid)
                metadata["argv"][2] = "DIFFERENT_DEVICE"
                path.write_text(json.dumps(metadata))
                try:
                    self.assert_original_rejected_without_device_calls(original)
                finally:
                    path.write_bytes(valid)

    def test_observe_rejects_failed_original_call(self):
        original = self.seed_original()
        for label in ("11-before-internal", "11-before-sd", "12-start"):
            with self.subTest(label=label):
                path = original / label / "call.json"
                valid = path.read_bytes()
                metadata = json.loads(valid)
                metadata["returncode"] = 255
                path.write_text(json.dumps(metadata))
                try:
                    self.assert_original_rejected_without_device_calls(original)
                finally:
                    path.write_bytes(valid)

    def test_observe_rejects_missing_original_call_record(self):
        original = self.seed_original()
        (original / "12-start" / "call.json").unlink()
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_missing_raw_baseline(self):
        original = self.seed_original()
        (original / "11-before-internal" / "stdout.bin").unlink()
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_truncated_raw_baseline(self):
        original = self.seed_original()
        (original / "11-before-internal" / "stdout.bin").write_bytes((INTERNAL + "/old.JPG").encode())
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_raw_path_outside_recorded_root(self):
        original = self.seed_original()
        (original / "11-before-internal" / "stdout.bin").write_bytes(b"/unrelated/old.JPG\0")
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_rejects_two_absent_original_scopes(self):
        original = self.seed_original()
        (original / "11-before-internal" / "stdout.bin").write_bytes(b"")
        (original / "11-before-internal" / "stderr.bin").write_bytes(b"CAMCTL_STORAGE_ABSENT\n")
        self.assert_original_rejected_without_device_calls(original)

    def test_observe_directory_call_failure_keeps_actual_raw_error(self):
        original = self.seed_original()
        before = self.snapshot(original)
        self.set_state(directory_failure="exit")
        with self.assertRaises(RuntimeError) as caught:
            self.invoke("--observe", original)
        self.assertIn("255", str(caught.exception))
        observation, = original.glob("observation-*")
        metadata = json.loads((observation / "14-after-internal" / "call.json").read_text())
        self.assertEqual(metadata["returncode"], 255)
        self.assertEqual((observation / "14-after-internal" / "stderr.bin").read_bytes(), b"DIRECTORY_READ_REJECTED\n")
        self.assertEqual(self.snapshot(original), before)

    def test_observe_truncated_directory_does_not_create_success_summary(self):
        original = self.seed_original()
        self.set_state(directory_failure="truncated")
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        self.assertFalse((observation / "summary.json").exists())
        self.assertFalse(any(args[:3] == ["-s", SERIAL, "pull"] for args in self.trace()))

    def test_observe_copy_mismatch_keeps_failed_summary_and_copy_record(self):
        original = self.seed_original()
        self.add_video()
        self.set_state(copy_mismatch=True)
        prior = len(self.trace())
        with self.assertRaises(RuntimeError) as caught:
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["video_checks"], "failed")
        self.assertEqual(summary["error"], str(caught.exception))
        self.assertEqual(summary["observed_mp4_files"], [VIDEO])
        copy = json.loads((observation / "video-01-copy.json").read_text())
        self.assertIs(copy["matches_source_after"], False)
        calls = self.trace()[prior:]
        self.assertEqual(sum(args[:3] == ["-s", SERIAL, "pull"] for args in calls), 1)
        self.assertFalse(any("-show_entries" in args for args in calls))

    def test_observe_source_query_failure_keeps_failed_summary(self):
        original = self.seed_original()
        self.add_video()
        self.set_state(after_source_failure=True)
        with self.assertRaises(RuntimeError) as caught:
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["video_checks"], "failed")
        self.assertEqual(summary["error"], str(caught.exception))
        self.assertIn("255", summary["error"])
        self.assertEqual((observation / "video-01.mp4").read_bytes(), CONTENT)

    def test_observe_pull_failure_keeps_failed_summary(self):
        original = self.seed_original()
        self.add_video()
        self.set_state(pull_failure=True)
        with self.assertRaises(RuntimeError) as caught:
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["video_checks"], "failed")
        self.assertEqual(summary["error"], str(caught.exception))
        self.assertIn("31", summary["error"])

    def test_observe_media_failure_keeps_failed_summary_and_actual_response(self):
        original = self.seed_original()
        self.add_video()
        self.set_state(media_failure=True)
        with self.assertRaises(RuntimeError) as caught:
            self.invoke("--observe", original)
        observation, = original.glob("observation-*")
        summary = self.read_summary(observation)
        self.assertEqual(summary["video_checks"], "failed")
        self.assertEqual(summary["error"], str(caught.exception))
        self.assertIn("23", summary["error"])
        self.assertEqual((observation / "video-01-ffprobe" / "stderr.bin").read_bytes(), b"MEDIA_TOOL_REJECTED\n")

    def test_summary_write_failure_preserves_last_saved_video_result(self):
        original = self.seed_original()
        self.add_video()
        original_write_text = Path.write_text
        saved = {}

        def failing_write(path, data, *args, **kwargs):
            if path.name in ("summary.json", "summary.json.tmp"):
                pending = json.loads(data)
                if pending["video_checks"] == "complete":
                    saved["summary"] = path.with_name("summary.json").read_bytes()
                    path.write_bytes(b"PARTIAL_SUMMARY_WRITE")
                    raise OSError("SUMMARY_STORAGE_UNAVAILABLE")
            return original_write_text(path, data, *args, **kwargs)

        with patch.object(Path, "write_text", failing_write), self.assertRaises(OSError) as caught:
            self.invoke("--observe", original)
        self.assertEqual(str(caught.exception), "SUMMARY_STORAGE_UNAVAILABLE")
        observation, = original.glob("observation-*")
        self.assertEqual((observation / "summary.json").read_bytes(), saved["summary"])
        summary = self.read_summary(observation)
        self.assertEqual(summary["video_checks"], "incomplete")
        self.assertEqual(summary["videos"][0]["source"], VIDEO)
        self.assertEqual(summary["videos"][0]["sha256"], hashlib.sha256(CONTENT).hexdigest())
        self.assertEqual((observation / "video-01.mp4").read_bytes(), CONTENT)
        self.assertTrue((observation / "video-01-ffprobe" / "stdout.bin").is_file())

    def test_second_video_error_remains_primary_when_failed_summary_write_fails(self):
        original = self.seed_original()
        self.add_video()
        self.add_video(SECOND_VIDEO)
        self.set_state(media_failure_source=SECOND_VIDEO)
        original_write_text = Path.write_text
        saved = {}

        def failing_write(path, data, *args, **kwargs):
            if path.name in ("summary.json", "summary.json.tmp"):
                pending = json.loads(data)
                if pending["video_checks"] == "failed":
                    saved["summary"] = path.with_name("summary.json").read_bytes()
                    saved["original_error"] = pending["error"]
                    saved["original_exception"] = sys.exception()
                    path.write_bytes(b"PARTIAL_FAILURE_SUMMARY_WRITE")
                    raise OSError("FAILURE_SUMMARY_STORAGE_UNAVAILABLE")
            return original_write_text(path, data, *args, **kwargs)

        with patch.object(Path, "write_text", failing_write), self.assertRaises(RuntimeError) as caught:
            self.invoke("--observe", original)
        self.assertIs(caught.exception, saved["original_exception"])
        self.assertEqual(str(caught.exception), saved["original_error"])
        self.assertIn("23", str(caught.exception))
        self.assertIn("MEDIA_TOOL_REJECTED", str(caught.exception))
        self.assertTrue(any("FAILURE_SUMMARY_STORAGE_UNAVAILABLE" in note
                            for note in getattr(caught.exception, "__notes__", ())))
        observation, = original.glob("observation-*")
        self.assertEqual((observation / "summary.json").read_bytes(), saved["summary"])
        summary = self.read_summary(observation)
        self.assertEqual(summary["observed_mp4_files"], [VIDEO, SECOND_VIDEO])
        self.assertEqual(summary["video_checks"], "incomplete")
        first, = summary["videos"]
        self.assertEqual(first["source"], VIDEO)
        self.assertEqual(first["duration_s"], "7.6")
        self.assertEqual((observation / "video-01.mp4").read_bytes(), CONTENT)
        self.assertEqual((observation / "video-02.mp4").read_bytes(), CONTENT)
        self.assertTrue((observation / "video-01-copy.json").is_file())
        self.assertTrue((observation / "video-01-ffprobe" / "stdout.bin").is_file())
        self.assertEqual((observation / "video-02-ffprobe" / "stderr.bin").read_bytes(), b"MEDIA_TOOL_REJECTED\n")
        metadata = json.loads((observation / "video-02-ffprobe" / "call.json").read_text())
        self.assertEqual(metadata["returncode"], 23)

    def test_summary_replace_failure_preserves_last_saved_video_result(self):
        original = self.seed_original()
        self.add_video()
        original_replace = Path.replace
        saved = {}

        def failing_replace(path, target):
            destination = Path(target)
            if path.name == "summary.json.tmp" and destination.name == "summary.json":
                pending = json.loads(path.read_text(encoding="utf-8"))
                if pending["video_checks"] == "complete":
                    saved["summary"] = destination.read_bytes()
                    raise OSError("SUMMARY_REPLACE_UNAVAILABLE")
            return original_replace(path, target)

        with patch.object(Path, "replace", failing_replace), self.assertRaises(OSError) as caught:
            self.invoke("--observe", original)
        self.assertEqual(str(caught.exception), "SUMMARY_REPLACE_UNAVAILABLE")
        observation, = original.glob("observation-*")
        self.assertEqual((observation / "summary.json").read_bytes(), saved["summary"])
        summary = self.read_summary(observation)
        self.assertEqual(summary["video_checks"], "incomplete")
        first, = summary["videos"]
        self.assertEqual(first["source"], VIDEO)
        self.assertEqual(first["sha256"], hashlib.sha256(CONTENT).hexdigest())
        self.assertEqual(first["duration_s"], "7.6")
        self.assertEqual((observation / "video-01.mp4").read_bytes(), CONTENT)
        self.assertTrue((observation / "video-01-copy.json").is_file())
        self.assertTrue((observation / "video-01-ffprobe" / "stdout.bin").is_file())

    def test_precise_new_record_is_observed_with_original_interval(self):
        original = self.seed_new("3e2", "1.000000000000000000000")
        raw = (original / "capture-observation.json").read_text()
        self.assertIn("1.000000000000000000000", raw)
        self.add_video()
        prior = len(self.trace())
        self.invoke("--observe", original)
        current, = original.glob("observation-*")
        self.assertEqual(self.read_summary(current)["params"]["interval_s"], 1)
        self.assertEqual(self.control_commands(self.trace()[prior:]), [])

    def test_invalid_interval_and_relation_have_zero_external_calls(self):
        for interval, duration in (("true", "300"), ("1.000000000000000000001", "300"),
                                   ("300", "300"), ("3600", "1800"), ("-1", "300")):
            with self.subTest(interval=interval, duration=duration), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke("--capture", "timelapse", "--timelapse-interval-s", interval,
                            "--timelapse-duration-s", duration)
            self.assertEqual(self.trace(), [])

    def test_missing_or_bad_resource_has_zero_external_calls(self):
        for data in (None, "{}", '{"format_version":true}'):
            with self.subTest(data=data):
                resource = self.workspace / "timelapse-time-parameters.json"
                if data is not None:
                    resource.write_text(data)
                with patch.object(self.probe, "TIME_PARAMETERS_PATH", resource), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.invoke("--capture", "timelapse")
                self.assertEqual(self.trace(), [])

    def test_time_resource_selects_the_action6_model_view(self):
        resource = {
            "format_version": 2,
            "duration_s": [300, 18000],
            "relation": "duration_s > interval_s",
            "models": {
                "dji-action6": {
                    "interval_s": [1, 8],
                    "legal_pair_count": 4,
                    "shortest_pair": {"interval_s": 1, "duration_s": 300},
                },
                "dji-osmo360-ii": {
                    "interval_s": [2, 8],
                    "legal_pair_count": 4,
                    "shortest_pair": {"interval_s": 2, "duration_s": 300},
                },
            },
        }
        path = self.workspace / "model-time-parameters.json"
        path.write_text(json.dumps(resource))

        with patch.object(self.probe, "TIME_PARAMETERS_PATH", path):
            domain = self.probe.load_time_parameters()

        self.assertEqual(domain["format_version"], 2)
        self.assertEqual(domain["interval_s"], [1, 8])
        self.assertEqual(domain["duration_s"], [300, 18000])
        self.assertEqual(domain["legal_pair_count"], 4)
        self.assertEqual(domain["shortest_pair"], {"interval_s": 1, "duration_s": 300})

    def test_observe_missing_or_contradictory_original_timing_call_is_rejected(self):
        original = self.seed_original(30)
        call = original / "03-timing" / "call.json"
        raw = call.read_bytes()
        call.unlink()
        self.assert_original_rejected_without_device_calls(original)
        call.write_bytes(raw.replace(b"04000050001e00", b"04000050000a00"))
        self.assert_original_rejected_without_device_calls(original)

    def test_independent_delivery_runs_without_source_or_installed_package(self):
        delivery = self.workspace / "delivery"
        delivery.mkdir()
        for name in ("action6_record_probe.py", "collect_call.py", "timelapse-time-parameters.json", "action6_timelapse_candidate.py"):
            shutil.copyfile(SCRIPT.with_name(name), delivery / name)
        self.set_state(video_at_start=True)
        code = ("import action6_record_probe as p, functools, sys; "
                "state={'now':0}; clock=lambda:state['now']; "
                "sleep=lambda seconds:state.update(now=state['now']+seconds); "
                "p.capture_once=functools.partial(p.capture_once,clock=clock,sleep=sleep); "
                "sys.argv=['probe','--capture','timelapse','--adb'," + repr(str(self.tool)) +
                ",'--ffprobe'," + repr(str(self.tool)) + "]; p.main()")
        prefix = "import sys; sys.path.insert(0, " + repr(str(delivery)) + "); "
        env = {**os.environ, "CAMCTL_PROBE_INTEGRATION_STATE": str(self.state_file)}
        result = subprocess.run([sys.executable, "-I", "-c", prefix + code], cwd=delivery, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        original, = (delivery / "action6").glob("timelapse-probe-*")
        self.assertEqual(self.read_summary(original)["params"]["interval_s"], 1)
        self.assertEqual(self.read_summary(original)["video_checks"], "complete")
        controls = self.control_commands()
        self.assertEqual(sum(command == TIMELAPSE_START for command in controls), 1)
        for name in ("timelapse-time-parameters.json", "action6_timelapse_candidate.py"):
            (delivery / name).unlink()
        code = ("import action6_record_probe as p,sys;sys.argv=['probe','--observe'," + repr(str(original)) +
                ",'--adb'," + repr(str(self.tool)) + ",'--ffprobe'," + repr(str(self.tool)) + "];p.main()")
        result = subprocess.run([sys.executable, "-I", "-c", prefix + code], cwd=delivery, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control_commands(), controls)

    def test_missing_bad_template_and_unrepresentable_window_have_zero_external_calls(self):
        from dataclasses import replace
        encoder = self.probe.load_candidate_encoder()
        model = encoder.CameraModel.ACTION6
        presets = encoder.TIMELAPSE_PRESETS[model]
        identity = encoder.TimelapseCandidateTemplate.ACTION6_AUTO_VIDEO
        variants = (
            (tuple(p for p in presets if p.candidate_template != identity), "CapabilityNotDeclaredError"),
            (tuple(replace(p, command=(*p.command[:-1], "040000")) if p.candidate_template == identity else p for p in presets), "ValueError"),
        )
        for changed, error_name in variants:
            encoder.TIMELAPSE_PRESETS[model] = changed
            with patch.object(self.probe, "load_candidate_encoder", return_value=encoder), contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                self.invoke("--capture", "timelapse")
            self.assertIn(error_name, errors.getvalue())
            self.assertEqual(self.trace(), [])
        encoder.TIMELAPSE_PRESETS[model] = presets
        domain = self.probe.load_time_parameters()
        domain["interval_s"] = [7000]
        domain["duration_s"] = [18000]
        domain["shortest_pair"] = {"interval_s": 7000, "duration_s": 18000}
        with patch.object(self.probe, "load_time_parameters", return_value=domain), contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            self.invoke("--capture", "timelapse")
        self.assertIn("ValueError", errors.getvalue())
        self.assertEqual(self.trace(), [])

    def test_missing_encoder_has_zero_external_calls(self):
        with patch.object(self.probe, "CANDIDATE_PATH", self.workspace / "missing.py"), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke("--capture", "timelapse")
        self.assertEqual(self.trace(), [])

    def test_old_observe_is_independent_of_current_time_and_encoder_resources(self):
        original = self.seed_original(30)
        self.add_video()
        with patch.object(self.probe, "load_time_parameters", side_effect=AssertionError("current time accessed")), patch.object(self.probe, "load_candidate_encoder", side_effect=AssertionError("new encoder accessed")):
            self.invoke("--observe", original)
        current, = original.glob("observation-*")
        self.assertEqual(self.read_summary(current)["params"]["duration_s"], 30)

    def test_new_format_observe_keeps_domain_external_original_values(self):
        original = self.seed_new(300, 1)
        path = original / "capture-observation.json"
        observation = json.loads(path.read_text())
        observation["requested_params"]["interval_s"] = 2
        observation["requested_params"]["duration_s"] = 301
        observation["start_to_observation_s"] = 301
        observation["preset_payload"] = "04000014002d0100000000000000000000"
        path.write_text(json.dumps(observation))
        call = original / "03-timing" / "call.json"
        call.write_bytes(call.read_bytes().replace(b"0400000a002c0100000000000000000000", b"04000014002d0100000000000000000000"))
        self.add_video()
        with patch.object(self.probe, "load_time_parameters", side_effect=AssertionError("current time accessed")), patch.object(self.probe, "load_candidate_encoder", side_effect=AssertionError("new encoder accessed")):
            self.invoke("--observe", original)
        current, = original.glob("observation-*")
        self.assertEqual(self.read_summary(current)["params"]["interval_s"], 2)
        self.assertEqual(self.read_summary(current)["params"]["duration_s"], 301)

    def test_new_format_rejects_partial_or_contradictory_format_facts(self):
        original = self.seed_new(1800, 8)
        path = original / "capture-observation.json"
        valid = path.read_bytes()
        for field in ("encoding_format", "template_identity", "template_payload"):
            observation = json.loads(valid)
            del observation[field]
            path.write_text(json.dumps(observation))
            self.assert_original_rejected_without_device_calls(original)
        observation = json.loads(valid)
        observation["template_payload"] = "04000050001e0000000000000000000000"
        path.write_text(json.dumps(observation))
        self.assert_original_rejected_without_device_calls(original)

    def test_minimal_old_record_needs_original_settings_not_defaults(self):
        original = self.seed_original()
        path = original / "capture-observation.json"
        observation = json.loads(path.read_text())
        for field in ("requested_params", "preset_payload", "preset_basis"):
            del observation[field]
        path.write_text(json.dumps(observation))
        (original / "03-timing" / "call.json").unlink()
        self.assert_original_rejected_without_device_calls(original)

    def test_generated_encoder_matches_production_for_every_current_pair(self):
        from camctl.devices.drivers.adb_cameras.commands import settings_for
        domain = self.probe.load_time_parameters()
        count = 0
        for interval in domain["interval_s"]:
            for duration in domain["duration_s"]:
                if duration <= interval:
                    continue
                metadata, actual = self.probe.prepare_timelapse(duration, interval_s=interval, time_parameters=domain)
                params = {"type": "action6_timelapse", "interval_s": interval, "duration_s": duration,
                          "exposure": {"mode": "auto"}, "outputs": "video"}
                import shlex
                expected = settings_for("dji-action6", "camera_timelapse", params)
                self.assertEqual([shlex.split(command) for _, command in actual], list(map(list, expected)))
                payload = bytes.fromhex(metadata["preset_payload"])
                self.assertEqual(len(payload), 17)
                self.assertEqual(payload[:3], b"\x04\x00\x00")
                self.assertEqual(payload[7:], b"\x00" * 10)
                count += 1
        self.assertEqual(count, domain["legal_pair_count"])

    def test_generator_checks_exact_delivery_and_rejects_unknown_dependency(self):
        generator_path = SCRIPT.parents[2] / "apps/camctl/scripts/render_action6_diagnostic_candidate.py"
        spec = importlib.util.spec_from_file_location("diagnostic_generator", generator_path)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        self.assertEqual(generator.generated_source(), SCRIPT.with_name("action6_timelapse_candidate.py").read_text())
        source = self.workspace / "commands.py"
        original = generator.SOURCE.read_text()
        source.write_text(original.replace('def _encode_settings(model, action_type, params):',
                                           'def _encode_settings(model, action_type, params):\n    unknown_diagnostic_dependency()'))
        with patch.object(generator, "SOURCE", source), self.assertRaises(ValueError) as caught:
            generator.generated_source()
        self.assertIn("unknown_diagnostic_dependency", str(caught.exception))

    def test_original_directory_call_must_match_its_raw_baseline_scope(self):
        original = self.seed_original()
        call = original / "11-before-internal" / "call.json"
        call.write_bytes(call.read_bytes().replace(INTERNAL.encode(), b"/unrelated/DCIM"))
        self.assert_original_rejected_without_device_calls(original)

    def test_non_json_numeric_spellings_are_rejected_before_tools(self):
        for value in ("3_00", "+300", "03e2"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke("--capture", "timelapse", "--timelapse-duration-s", value)
            self.assertEqual(self.trace(), [])

    def test_original_control_call_rejects_non_object_record(self):
        original = self.seed_original()
        (original / "03-timing" / "call.json").write_text("[]")
        self.assert_original_rejected_without_device_calls(original)

    def assert_no_external_rejection(self, original):
        prior = self.trace()
        before = self.snapshot(original)
        with self.assertRaises((RuntimeError, ValueError, OSError)):
            self.invoke("--observe", original)
        self.assertEqual(self.trace(), prior)
        self.assertEqual(self.snapshot(original), before)
        self.assertEqual(list(original.glob("observation-*")), [])

    def test_f1_all_five_record_layouts_are_read_only(self):
        for layout in ("A0", "A1", "B0", "B1", "C"):
            with self.subTest(layout=layout):
                (self.workspace / "storage/internal/DJI_001/late.MP4").unlink(missing_ok=True)
                original = self.seed_new(300, 1) if layout == "C" else self.historical_record(layout)
                before = self.snapshot(original)
                prior = len(self.trace())
                self.sleep_calls.clear()
                self.add_video()
                with patch.object(self.probe, "TIME_PARAMETERS_PATH", self.workspace / "deleted-time.json"), patch.object(self.probe, "CANDIDATE_PATH", self.workspace / "deleted-encoder.py"):
                    self.invoke("--observe", original)
                current, = original.glob("observation-*")
                summary = self.read_summary(current)
                self.assertEqual(summary["params"]["duration_s"], 1800 if layout in ("A0", "A1") else 300 if layout == "C" else 30)
                self.assertEqual(summary["params"]["interval_s"], 1 if layout == "C" else 8)
                self.assertEqual(summary["preset_basis"], "source_example" if layout in ("A0", "A1") else "sample_derived_candidate" if layout == "C" else "experimental_duration")
                self.assert_unknown_completion(summary)
                self.assertEqual(self.control_commands(self.trace()[prior:]), [])
                self.assertEqual(self.sleep_calls, [])
                self.assertEqual(self.snapshot(original), before)

    def test_f1_earliest_layouts_validate_every_required_call_and_raw_file(self):
        for layout in ("A0", "A1"):
            original = self.historical_record(layout)
            before = self.snapshot(original)
            for label in ("01-mode", "02-resolution", "03-exposure", "04-preset", "12-start", "11-before-internal", "11-before-sd"):
                for filename in ("call.json", "stdout.bin", "stderr.bin"):
                    path = original / label / filename
                    raw = path.read_bytes()
                    with self.subTest(layout=layout, label=label, missing=filename):
                        path.unlink()
                        try:
                            self.assert_no_external_rejection(original)
                        finally:
                            path.write_bytes(raw)
                for field, value in (("returncode", 1), ("returncode", True), ("argv", []), ("argv", ["adb", "-s", "OTHER", "shell", "-T", "sh -c invalid"])):
                    path = original / label / "call.json"
                    raw = path.read_bytes()
                    call = json.loads(raw)
                    call[field] = value
                    with self.subTest(layout=layout, label=label, field=field, value=value):
                        path.write_text(json.dumps(call))
                        try:
                            self.assert_no_external_rejection(original)
                        finally:
                            path.write_bytes(raw)
            for label in ("01-mode", "02-resolution", "03-exposure", "04-preset", "12-start"):
                for filename, value in (("stderr.bin", b"ERROR"), ("stdout.bin", b"Resp message, len = 1, data: e3"), ("stdout.bin", b"Resp message, len = 2, data: 00")):
                    path = original / label / filename
                    raw = path.read_bytes()
                    with self.subTest(layout=layout, label=label, filename=filename, invalid=value):
                        path.write_bytes(value)
                        try:
                            self.assert_no_external_rejection(original)
                        finally:
                            path.write_bytes(raw)
            path = original / "04-preset" / "call.json"
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b"0400005000080700000000000000000000", b"04000050001e0000000000000000000000"))
            self.assert_no_external_rejection(original)
            path.write_bytes(raw)
            self.assertEqual(self.snapshot(original), before)

    def test_f1_partial_metadata_and_mixed_layouts_are_rejected(self):
        for layout in ("A0", "A1", "B0", "B1"):
            original = self.historical_record(layout)
            path = original / "capture-observation.json"
            raw = path.read_bytes()
            original_observation = json.loads(raw)
            for field in ("requested_params", "preset_payload", "preset_basis", "encoding_format", "template_identity", "template_payload"):
                observation = json.loads(raw)
                if field in observation:
                    del observation[field]
                else:
                    observation[field] = {} if field == "requested_params" else "partial"
                with self.subTest(layout=layout, partial=field):
                    path.write_text(json.dumps(observation))
                    self.assert_no_external_rejection(original)
            path.write_bytes(raw)
            for label in (("03-timing", "04-exposure", "05-output") if layout in ("A0", "A1") else ("03-exposure", "04-preset")):
                added = original / label
                added.mkdir()
                try:
                    with self.subTest(layout=layout, extra=label):
                        self.assert_no_external_rejection(original)
                finally:
                    added.rmdir()
            observation = dict(original_observation, observation_only=True)
            path.write_text(json.dumps(observation))
            self.assert_no_external_rejection(original)
            path.write_bytes(raw)
        original = self.seed_original()
        path = original / "capture-observation.json"
        observation = json.loads(path.read_text())
        for field in ("requested_params", "preset_payload", "preset_basis"):
            del observation[field]
        path.write_text(json.dumps(observation))
        self.assert_no_external_rejection(original)

    def test_f1_mixed_partial_and_complete_earliest_and_modern_labels_are_rejected(self):
        for remove in ((), ("03-exposure",), ("04-preset",)):
            # 每个失败组合使用独立A0原件，不借现代夹具推断布局。
            original = self.historical_record("A0")
            for label in remove:
                shutil.rmtree(original / label)
            self.archive_call(original, "03-timing", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400005000080700000000000000000000", b"Resp message, len = 1, data: 00", b"")
            self.archive_call(original, "04-exposure", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100", b"Resp message, len = 1, data: 00", b"")
            self.assert_no_external_rejection(original)
            shutil.rmtree(original)

    def test_f1_b0_present_output_is_required_and_must_match(self):
        original = self.historical_record("B0")
        for filename, invalid in (("call.json", b"{}"), ("stdout.bin", b"Resp message, len = 1, data: e3"), ("stderr.bin", b"ERROR")):
            path = original / "05-output" / filename
            raw = path.read_bytes()
            with self.subTest(filename=filename, state="missing"):
                path.unlink()
                self.assert_no_external_rejection(original)
            path.write_bytes(invalid)
            with self.subTest(filename=filename, state="invalid"):
                self.assert_no_external_rejection(original)
            path.write_bytes(raw)
        path = original / "05-output" / "call.json"
        path.write_bytes(path.read_bytes().replace(b"04000050001e0000000000000000000000", b"04000050000a0000000000000000000000"))
        self.assert_no_external_rejection(original)

    def test_f1_v1_rejects_extra_earliest_or_duplicate_output_markers(self):
        original = self.seed_new(300, 1)
        for label in ("03-exposure", "04-preset", "05-output"):
            extra = original / label
            if label == "05-output":
                shutil.copytree(original / "03-timing", extra)
            else:
                extra.mkdir()
            try:
                with self.subTest(extra=label):
                    self.assert_no_external_rejection(original)
            finally:
                shutil.rmtree(extra)

    def test_f1_earliest_records_run_without_deleted_resources_in_isolated_delivery(self):
        delivery = self.workspace / "earliest-delivery"
        delivery.mkdir()
        for name in ("action6_record_probe.py", "collect_call.py", "timelapse-time-parameters.json", "action6_timelapse_candidate.py"):
            shutil.copyfile(SCRIPT.with_name(name), delivery / name)
        for name in ("timelapse-time-parameters.json", "action6_timelapse_candidate.py"):
            (delivery / name).unlink()
        self.add_video()
        env = {**os.environ, "CAMCTL_PROBE_INTEGRATION_STATE": str(self.state_file)}
        for layout in ("A0", "A1"):
            with self.subTest(layout=layout):
                archived = self.historical_record(layout)
                original = delivery / layout
                shutil.copytree(archived, original)
                before = self.snapshot(original)
                result = subprocess.run([sys.executable, "-I", str(delivery / "action6_record_probe.py"), "--observe", str(original), "--adb", str(self.tool), "--ffprobe", str(self.tool)], cwd=delivery, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                current, = original.glob("observation-*")
                summary = self.read_summary(current)
                self.assertEqual(summary["params"]["interval_s"], 8)
                self.assertEqual(summary["params"]["duration_s"], 1800)
                self.assertEqual(summary["preset_basis"], "source_example")
                self.assert_unknown_completion(summary)
                self.assertEqual(self.snapshot(original), before)
        self.assertEqual(self.control_commands(), [])

    def test_f1_earliest_dual_baseline_states_are_distinct(self):
        for layout in ("A0", "A1"):
            original = self.historical_record(layout)
            for internal, sd in (("directory", "absent"), ("absent", "directory"), ("directory", "directory"), ("absent", "absent")):
                for name, state in (("internal", internal), ("sd", sd)):
                    (original / ("11-before-" + name) / "stdout.bin").write_bytes(b"")
                    (original / ("11-before-" + name) / "stderr.bin").write_bytes(b"CAMCTL_FIND_EXIT=0\n" if state == "directory" else b"CAMCTL_STORAGE_ABSENT\n")
                with self.subTest(layout=layout, internal=internal, sd=sd):
                    if internal == sd == "absent":
                        self.assert_no_external_rejection(original)
                    else:
                        _, before, metadata = self.probe.load_observation_source(original, SERIAL)
                        self.assertEqual(before, set())
                        self.assertEqual(metadata["requested_params"]["duration_s"], 1800)
            for name, root in (("internal", INTERNAL), ("sd", SD)):
                path = original / ("11-before-" + name)
                (path / "stderr.bin").write_bytes(b"CAMCTL_FIND_EXIT=0\n")
                for raw in (b"truncated", b"/outside/DCIM/photo.JPG\0", (root + "/a.JPG\0" + root + "/a.JPG\0").encode()):
                    with self.subTest(layout=layout, scope=name, raw=raw):
                        (path / "stdout.bin").write_bytes(raw)
                        self.assert_no_external_rejection(original)
                (path / "stdout.bin").write_bytes(b"")
        self.assertEqual(self.trace(), [])

    def test_f1_original_argv_is_a_nonempty_tool_and_six_strings(self):
        original = self.historical_record("A0")
        for label in ("01-mode", "12-start", "11-before-internal", "11-before-sd"):
            path = original / label / "call.json"
            raw = path.read_bytes()
            for tool in (None, True, ""):
                with self.subTest(label=label, tool=tool):
                    call = json.loads(raw)
                    call["argv"][0] = tool
                    path.write_text(json.dumps(call))
                    self.assert_no_external_rejection(original)
            path.write_bytes(raw)

    def test_recording_keeps_one_start_and_stop_and_complete_video(self):
        self.set_state(video_at_start=True)
        self.invoke("--capture", "record")
        recording, = (self.workspace / "action6").glob("record-probe-*")
        summary = self.read_summary(recording)
        self.assertEqual(summary["videos"][0]["bytes"], len(CONTENT))
        self.assertEqual(summary["aperture"], "unconfirmed")
        self.assertEqual(summary["bitrate_effect"], "unconfirmed")
        commands = [shlex_command(args) for args in self.trace() if args[:4] == ["-s", SERIAL, "shell", "-T"]]
        self.assertEqual(sum(command.endswith("-c 02 01") for command in commands), 1)
        self.assertEqual(sum(command.endswith("-c 02 00") for command in commands), 1)
        self.assertEqual(self.sleep_calls, [10])


class GeneratorBindingChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="camctl-diagnostic-generation-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        path = SCRIPT.parents[2] / "apps/camctl/scripts/render_action6_diagnostic_candidate.py"
        spec = importlib.util.spec_from_file_location("candidate_generator_review", path)
        self.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.generator)
        self.source = self.generator.SOURCE.read_text()
        self.delivery = SCRIPT.with_name("action6_timelapse_candidate.py").read_text()

    def assert_source_rejected(self, source, fragment):
        origin = self.workspace / "commands.py"
        target = self.workspace / "delivery.py"
        origin.write_text(source)
        with patch.object(self.generator, "SOURCE", origin), patch.object(self.generator, "TARGET", target):
            error = None
            try:
                generated = self.generator.generated_source()
            except ValueError as caught:
                error = str(caught)
                generated = self.delivery
            # 即使旧生成器给出错误产物，--check也必须拒绝其源码形状。
            target.write_text(generated)
            before = target.read_bytes()
            with patch.object(sys, "argv", ["generator", "--check"]), contextlib.redirect_stderr(io.StringIO()) as stderr:
                result = self.generator.main()
            self.assertEqual(target.read_bytes(), before)
        self.assertIsNotNone(error, "不支持的源码形状被生成器接受")
        self.assertIn(fragment, error)
        self.assertNotEqual(result, 0, stderr.getvalue())
        self.assertIn(fragment, stderr.getvalue())

    def test_f2_import_definition_bindings_are_unique_in_both_directions(self):
        cases = (
            self.source.replace("from decimal import Decimal", "Decimal = float", 1),
            self.source.replace("class CameraModel", "Decimal = float\n\nclass CameraModel", 1),
            self.source.replace("from decimal import Decimal", "Decimal = float\nfrom decimal import Decimal", 1),
            self.source.replace("class CameraModel", "def Decimal():\n    return None\n\nclass CameraModel", 1),
            self.source.replace("class CameraModel", "class Decimal:\n    pass\n\nclass CameraModel", 1),
            self.source + "\nfrom decimal import Decimal\n",
        )
        for number, source in enumerate(cases):
            with self.subTest(case=number):
                self.assert_source_rejected(source, "Decimal")

    def test_f2_rebound_dependency_builtins_and_external_exception_are_rejected(self):
        for name, binding in (("next", "next = lambda values, default: default"),
                              ("int", "int = float"),
                              ("CapabilityNotDeclaredError", "CapabilityNotDeclaredError = ValueError"),
                              ("next", "from decimal import Decimal as next")):
            with self.subTest(binding=binding):
                self.assert_source_rejected(self.source.replace("class CameraModel", binding + "\n\nclass CameraModel", 1), name)

    def test_f2_import_identity_preserves_level_module_name_and_alias(self):
        for replacement in ("from .decimal import Decimal", "from decimal import Decimal as Decimal",
                            "from math import floor as Decimal"):
            with self.subTest(replacement=replacement):
                self.assert_source_rejected(self.source.replace("from decimal import Decimal", replacement, 1), "Decimal")
        self.assert_source_rejected(self.source.replace("from camctl.devices.drivers.registry import", "from .camctl.devices.drivers.registry import", 1), "CapabilityNotDeclaredError")

    def test_f2_nested_import_and_from_import_in_selected_closure_are_rejected(self):
        for marker, insertion in (("def _encode_settings(model, action_type, params):", "import os"),
                                  ("def _encode_settings(model, action_type, params):", "from os import path"),
                                  ("class CameraModel(StrEnum):", "import os")):
            with self.subTest(marker=marker, insertion=insertion):
                source = self.source.replace(marker, marker + "\n    " + insertion, 1)
                self.assert_source_rejected(source, "Import")

    def test_f2_previous_unknown_shapes_and_missing_root_remain_rejected(self):
        for extra, fragment in (("import os", "Import"), ("a = b = 1", "Assign"),
                                ("a: int", "AnnAssign"), ("async def extra():\n    pass", "AsyncFunctionDef"),
                                ("print('module effect')", "Expr"), ("_DIAG = ()", "_DIAG")):
            with self.subTest(extra=extra):
                self.assert_source_rejected(self.source + "\n" + extra + "\n", fragment)
        self.assert_source_rejected(self.source.replace("def _encode_settings(", "def removed_settings(", 1), "_encode_settings")

    def test_f2_external_exception_shape_changes_are_rejected(self):
        registry = self.workspace / "registry.py"
        for source in ("class CapabilityNotDeclaredError(ValueError):\n    'changed'\n",
                       "@staticmethod\nclass CapabilityNotDeclaredError(Exception):\n    'changed'\n",
                       "class CapabilityNotDeclaredError(Exception):\n    'changed'\n    code = 1\n",
                       "class CapabilityNotDeclaredError(Exception):\n    'first'\nclass CapabilityNotDeclaredError(Exception):\n    'second'\n"):
            with self.subTest(source=source):
                registry.write_text(source)
                with patch.object(self.generator, "REGISTRY", registry):
                    self.assert_source_rejected(self.source, "异常")

    def test_f2_only_annotations_future_directive_is_supported(self):
        self.assert_source_rejected("from __future__ import division\n" + self.source, "division")
        path = self.workspace / "commands.py"
        path.write_text("from __future__ import annotations\n" + self.source)
        with patch.object(self.generator, "SOURCE", path):
            self.assertEqual(self.generator.generated_source(), self.delivery)

    def test_f2_unselected_nested_dependency_does_not_expand_closure(self):
        source = self.source + "\ndef unrelated_diagnostic_function():\n    import os\n    return missing_runtime_dependency()\n"
        path = self.workspace / "commands.py"
        path.write_text(source)
        with patch.object(self.generator, "SOURCE", path):
            self.assertEqual(self.generator.generated_source(), self.delivery)

    def test_f2_missing_and_drifted_distribution_are_rejected_without_writes(self):
        target = self.workspace / "missing.py"
        with patch.object(self.generator, "TARGET", target), patch.object(sys, "argv", ["generator", "--check"]):
            # 使诊断路径仍属于源码工作区，明确检查缺失／漂移而非路径格式错误。
            with patch.object(self.generator, "REPO", self.workspace), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(self.generator.main(), 0)
                self.assertFalse(target.exists())
                target.write_text("DRIFTED")
                self.assertNotEqual(self.generator.main(), 0)
                self.assertEqual(target.read_text(), "DRIFTED")


def shlex_command(args):
    import shlex
    return shlex.split(args[4])[2]


if __name__ == "__main__":
    unittest.main()
