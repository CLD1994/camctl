"""双相机软件契约与目录世界；只由测试 launcher 显式装配。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import shlex
import time

from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.drivers.adb_cameras.commands import CameraModel, TIMELAPSE_PRESETS
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities, output_scope_for
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
from camctl.devices.drivers.adb_cameras.transport import AdbTransport, shell_argv
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn
from camctl.operations.models import EffectState, ErrorValue
from camctl.operations.process import LocalExit, RawToolOutcome


class DemoClock:
    """仅推进业务 UTC 和采集计时；工具期限与 asyncio 保持真实时间。"""

    def __init__(self, path):
        self.path = Path(path)

    def _delta(self):
        value = int(self.path.read_text())
        if value < 0:
            raise ValueError("演示时钟增量不能为负")
        return value

    def utc_micros(self):
        return (time.time_ns() + self._delta()) // 1000

    def monotonic_ns(self):
        return time.monotonic_ns() + self._delta()


def advance_clock(path, seconds):
    path = Path(path)
    temporary = path.with_suffix(".next")
    temporary.write_text(str(int(path.read_text()) + seconds * 1_000_000_000))
    temporary.replace(path)


def schedule(clock_path, seconds=1):
    now = DemoClock(clock_path).utc_micros() + seconds * 1_000_000
    return datetime.fromtimestamp(now // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def recording_params(model):
    prefix = "action6" if model is CameraModel.ACTION6 else "osmo360ii"
    params = {"type": prefix + "_record", "duration_s": 10,
              "exposure": {"mode": "auto", "compensation_ev": 0}}
    if model is CameraModel.ACTION6:
        params.update(resolution="4k30", fov="wide", stabilization="off", aperture="f2.8", bitrate="high")
    return params


def timelapse_params(model, outputs="video"):
    duration = 1800 if model is CameraModel.ACTION6 else 600
    preset = next(p for p in TIMELAPSE_PRESETS[model]
                  if p.duration_s == duration and p.outputs == outputs)
    prefix = "action6" if model is CameraModel.ACTION6 else "osmo360ii"
    return {"type": prefix + "_timelapse", "interval_s": preset.interval_s,
            "duration_s": preset.duration_s, "outputs": str(preset.outputs),
            "exposure": {"mode": preset.exposure_mode, **({"iso": 800} if preset.exposure_mode == "manual" else {})}}


def camera_spec(deployment, model, params):
    """旧文件先于基准存在；每次拍摄使用新的子目录和路径。"""
    clock_path = deployment.root / "business-clock.txt"
    clock_path.write_text("0")
    remote_root = deployment.root / "camera-files"
    for directory in output_scope_for(model)["directories"]:
        (remote_root / directory.lstrip("/")).mkdir(parents=True)
    (remote_root / "mnt/media_rw/emulated/DCIM/old.mp4").write_bytes(b"old camera source")
    declaration = {"kind": "camera", "driver": str(model), "adb": {"serial": "demo-camera"}}
    with deployment.config_path.open("a") as config:
        config.write(f'\n[devices.camera]\nkind = "camera"\ndriver = "{model}"\n'
                     '[devices.camera.adb]\nserial = "demo-camera"\n'
                     '[devices.camera.copy]\nread_idle_timeout_s = "60"\n')
    return {"camera_demo": True, "clock_path": str(clock_path), "remote_root": str(remote_root),
            "devices": {"demo-camera": {"device_id": "camera", "driver_id": str(model)}},
            "declaration": declaration}


class CameraWorld:
    """确定的设备事实来自测试剧本；真实文件工具与读取端仍执行。"""

    def __init__(self, spec):
        self.spec = spec
        self.root = Path(spec["remote_root"])
        self.clock = DemoClock(spec["clock_path"])
        self.gates = Path(spec["gate_dir"])
        self.requests = {}

    def _state(self, serial):
        path = self.gates / (serial + "-state.json")
        return path, json.loads(path.read_text()) if path.exists() else {"captures": []}

    def _write_state(self, path, state):
        temporary = path.with_suffix(".next")
        temporary.write_text(json.dumps(state))
        temporary.replace(path)

    def source_path(self, remote):
        return self.root / remote.lstrip("/")

    def call(self, serial, request, kind, batch):
        path, state = self._state(serial)
        with (self.gates / "camera-calls.jsonl").open("a") as log:
            log.write(json.dumps({"kind": str(kind), "serial": serial, "target": request.ticket.target_id}) + "\n")
        if kind is CameraCall.START:
            number = len(state["captures"]) + 1
            capture = {"params": dict(request.params), "started_at": self.clock.utc_micros(), "stopped": False, "files": []}
            formats = ["mp4"]
            if request.params.get("outputs") in ("video_raw", "video_jpeg"):
                photo_format = "dng" if request.params["outputs"] == "video_raw" else "jpeg"
                product_case = self.spec.get("product_case", "complete")
                if product_case == "wrong_format":
                    photo_format = "jpeg" if photo_format == "dng" else "dng"
                if product_case != "missing_photo":
                    formats += [photo_format]
            for format_id in formats:
                remote = f"/mnt/media_rw/emulated/DCIM/new-{number}/clip {number}.{format_id}"
                source = self.source_path(remote)
                source.parent.mkdir(exist_ok=True)
                source.write_bytes((f"camera-source-{number}-{format_id}:".encode() * 100000)[:1_200_000])
                capture["files"].append(remote)
            state["captures"].append(capture)
            self._write_state(path, state)
        elif kind is CameraCall.STOP:
            state["captures"][-1]["stopped"] = True
            self._write_state(path, state)
        if kind is CameraCall.RESULT:
            return b"SOFTWARE-RESULT/1\n" + json.dumps(self._page(serial, request, batch, state)).encode()
        return ("SOFTWARE-CAMERA/1 " + kind.value).encode()

    def _page(self, serial, request, batch, state):
        directories = tuple(request.params["output_scope"]["directories"])
        cursor = request.params.get("cursor")
        index = 0 if cursor is None else cursor["directory_index"]
        after = None if cursor is None else cursor["after_path"]
        root = self.source_path(directories[index])
        paths = sorted("/" + str(p.relative_to(self.root)) for p in root.rglob("*") if p.is_file())
        paths = [p for p in paths if after is None or p > after]
        selected = paths[:batch]
        next_cursor = None
        if len(paths) > batch or index + 1 < len(directories):
            next_cursor = {**self.spec["devices"][serial], "directories": list(directories),
                           "directory_index": index if len(paths) > batch else index + 1,
                           "after_path": selected[-1] if len(paths) > batch else None}
        ready = {}
        for capture in state["captures"]:
            params = capture["params"]
            complete = (capture["stopped"] if params["type"].endswith("_record") else
                        self.clock.utc_micros() >= capture["started_at"] + params["duration_s"] * 1_000_000)
            ready.update((path, complete) for path in capture["files"])
        entries = []
        for remote in selected:
            source = self.source_path(remote)
            suffix = source.suffix[1:]
            entries.append({"identity": remote, "locator": {"path": remote},
                "complete": ready.get(remote, True), "size_bytes": source.stat().st_size,
                "kind": "video" if suffix == "mp4" else "photo", "format_id": suffix,
                "original_name": source.name, "media_type": "video/mp4" if suffix == "mp4" else "image/" + suffix,
                "paired_identity": None})
        return {"activity_id": request.ticket.target_id, "entries": entries, "cursor": cursor,
                "next_cursor": next_cursor, "set_finalized": next_cursor is None and all(ready.values()),
                "completion_evidence": None}


class WorldTransport:
    """保留 ADB serial、远端命令和实际文件工具，只替换设备通信。"""

    def __init__(self, world):
        self.world = world

    def _script(self, spec):
        assert spec.argv[:2] == ("adb", "-s") and spec.argv[3] == "exec-out"
        assert spec.argv[2] in self.world.spec["devices"]
        words = shlex.split(spec.argv[4])
        assert words[:2] == ["sh", "-c"] and len(words) == 3
        return words[2]

    async def run(self, spec, stop):
        script = self._script(spec)
        request = self.world.requests.pop((spec.argv[2], script), None)
        if request is not None:
            output = self.world.call(spec.argv[2], *request)
            return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")
        local = replace(spec, argv=("sh", "-c", self._local_script(script)))
        raw = await AdbTransport().run(local, stop)
        if raw.output is not None and raw.output.startswith(b"CAMCTL-DIRECTORY/1\0"):
            raw = replace(raw, output=raw.output.replace(str(self.world.root).encode(), b""))
        return raw

    def _local_script(self, script):
        return script.replace("/mnt/media_rw/", str(self.world.root / "mnt/media_rw") + "/")

    async def run_stream(self, spec, stop, stdout_sink):
        script = self._script(spec)
        first = True
        async def gated_sink(data):
            nonlocal first
            await stdout_sink(data)
            if first and (self.world.gates / "hold-read").exists():
                first = False
                (self.world.gates / "read-started").touch()
                while not (self.world.gates / "release-read").exists():
                    await asyncio.sleep(0.01)
        return await AdbTransport().run_stream(replace(spec, argv=("sh", "-c", self._local_script(script))), stop, gated_sink)


def _contract(model, world):
    returned = {op: EvidenceContract(op + "_returned", 1, op, frozenset()) for op in ("control", "stop", "result")}
    assumptions = {op: EvidenceContract(op + "_foreground", 1, op, frozenset({"terminate_grace_s"})) for op in returned}
    observations = [EvidenceContract("dispatch_prevented", 1, "control", frozenset()), RESULT_PAGE_CONTRACT,
        EvidenceContract("read_returned", 1, "read", frozenset()),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id")]
    observations += [EvidenceContract(name, 1, op, frozenset({"activity_id"}), identity_field="activity_id")
                     for name, op in (("setting_applied", "control"), ("start_confirmed", "control"),
                                      ("timelapse_sent", "control"), ("stop_confirmed", "stop"))]
    evidence = EvidenceRegistry((*returned.values(), *assumptions.values(), *observations))
    class Parser:
        def __init__(self, request, kind):
            self.request, self.kind = request, kind
        def interpret(self, raw):
            if raw.error or raw.output_failure or raw.exit is None or raw.exit.exit_code != 0:
                return InterpretedFacts((), ErrorValue("software_camera_call_failed", "device"), EffectState.UNKNOWN)
            if self.kind is CameraCall.RESULT:
                prefix = b"SOFTWARE-RESULT/1\n"
                assert raw.output.startswith(prefix)
                observation = DeviceObservation("result_files_listed", 2, json.loads(raw.output[len(prefix):]))
            else:
                assert raw.output == ("SOFTWARE-CAMERA/1 " + self.kind.value).encode()
                name = ("setting_applied" if self.kind is CameraCall.SETTING else "stop_confirmed"
                        if self.kind is CameraCall.STOP else "timelapse_sent"
                        if self.request.operation == "start_timelapse" else "start_confirmed")
                observation = DeviceObservation(name, 1, {"activity_id": self.request.ticket.target_id})
            return InterpretedFacts((observation,), None, EffectState.CONFIRMED)
    def factory(kind):
        def build(request, serial, argv, batch):
            argv = argv or shell_argv(serial, "software-results")
            script = shlex.split(argv[4])[2]
            world.requests[(serial, script)] = (request, kind, batch)
            op = request.ticket.operation
            return DeviceCommand(op, argv, request.binding, request.timeout_s, Decimal("1"), evidence,
                                 returned[op], assumptions[op], Parser(request, kind), kind.value)
        return build
    def task(params):
        action_type = "camera_record" if params["type"].endswith("_record") else "camera_timelapse"
        rules = [{"kind": "video", "format_id": "mp4", "min_count": 1, "exact_count": None, "require_pairing": False}]
        if params.get("outputs") in ("video_raw", "video_jpeg"):
            rules += [{"kind": "photo", "format_id": "dng" if params["outputs"] == "video_raw" else "jpeg",
                       "min_count": 1, "exact_count": None, "require_pairing": False}]
        fixed = CaptureTask(action_type, target_duration_s=params["duration_s"], stop_supported=True,
                            ownership_mode=2, output_scope=output_scope_for(model), product_rules=tuple(rules))
        if action_type == "camera_timelapse":
            return replace(fixed, duration_based=True, wait_after_send=True, end_control=EndControl.DEVICE,
                stop_supported=model is CameraModel.OSMO360II, start_return_meaning=StartReturn.SENT,
                completion_mode=CompletionMode.TIME_AND_OUTPUTS, result_wait_margin_s=0)
        return fixed
    return CameraContract(model, task_factories={cap.parameter_type: task for cap in candidate_capabilities(model)},
        commands={kind: factory(kind) for kind in (CameraCall.SETTING, CameraCall.START, CameraCall.STOP, CameraCall.RESULT)},
        file_tools=ShellFileTools(), digest_timeout_s=lambda size: Decimal("60"), evidence=evidence,
        capture_read_parallel_supported=True)


def install_camera_demo(spec):
    """测试进程在真实 CLI 登记前装配；生产入口没有该模式。"""
    from camctl.devices.drivers.adb_cameras import registration
    from camctl.bootstrap import lifecycle, capture_assembly, obtain_assembly, cleanup_assembly
    world = CameraWorld(spec)
    contracts = tuple(_contract(model, world) for model in CameraModel)
    registration.builtin_camera_contracts = lambda: contracts
    registration.AdbTransport = lambda: WorldTransport(world)
    lifecycle.SystemClock = lambda: world.clock
    for module, name, fields in (
        (capture_assembly, "session_capture_assembly", ("wall_us", "monotonic_ns")),
        (obtain_assembly, "session_obtain_assembly", ("occurred_at", "monotonic_ns")),
        (cleanup_assembly, "session_cleanup_assembly", ("occurred_at", "monotonic_ns")),
    ):
        original = getattr(module, name)
        def wrapped(*, _original=original, _fields=fields, **kwargs):
            for field in _fields:
                kwargs[field] = world.clock.monotonic_ns if field == "monotonic_ns" else world.clock.utc_micros
            return _original(**kwargs)
        setattr(module, name, wrapped)
