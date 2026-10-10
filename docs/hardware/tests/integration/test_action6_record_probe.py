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
            "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
        }
        assert command in allowed, command
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

    def seed_original(self):
        with self.assertRaises(RuntimeError):
            self.invoke("--capture", "timelapse")
        original, = (self.workspace / "action6").glob("timelapse-probe-*")
        self.assertTrue((original / "capture-observation.json").is_file())
        return original

    def trace(self):
        path = self.workspace / "trace.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

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
        original = self.seed_original()
        summary = self.read_summary(original)
        self.assertEqual(summary["sample_status"], "no_new_mp4")
        self.assertEqual(summary["new_files"], [])
        self.assertEqual(summary["observed_mp4_files"], [])
        self.assertEqual(summary["videos"], [])
        self.assertEqual(summary["video_checks"], "not_attempted")
        self.assert_unknown_completion(summary)
        observation = json.loads((original / "capture-observation.json").read_text())
        self.assertIs(observation["observation_only"], False)
        self.assertEqual(observation["start_to_observation_s"], 1800)
        commands = [args[4] for args in self.trace() if args[:4] == ["-s", SERIAL, "shell", "-T"]]
        self.assertEqual(sum("-c 01 01" in command for command in commands), 1)
        self.assertFalse(any("-c 01 00" in command or "-c 02 00" in command for command in commands))

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
        original = self.seed_original()
        (original / "summary.json").unlink()
        path = original / "capture-observation.json"
        observation = json.loads(path.read_text())
        del observation["observation_only"]
        path.write_text(json.dumps(observation))
        before = self.snapshot(original)
        with self.assertRaises(RuntimeError):
            self.invoke("--observe", original)
        current, = original.glob("observation-*")
        self.assertEqual(self.read_summary(current)["sample_status"], "no_new_mp4")
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


def shlex_command(args):
    import shlex
    return shlex.split(args[4])[2]


if __name__ == "__main__":
    unittest.main()
