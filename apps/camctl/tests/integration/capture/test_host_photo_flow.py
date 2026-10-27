"""Hasselblad 主机单张沿受理、装配、调用与正式产物事务运行。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelApplyMode,
    CancellationEffect,
    FixedCancelSet,
    FixedTarget,
    FixCancelTargets,
    SelectionBasis,
    StartCancelAction,
)
from camctl.capture.handlers import ListedResult, capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import (
    DRIVER_ID,
    driver_definition,
)
from camctl.devices.drivers.hasselblad.driver import EVIDENCE as HASSELBLAD_EVIDENCE
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError,
    DriverEntry,
    DriverRegistry,
    DriverStatus,
)
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration
from camctl.devices.start_entry import ENTERED, entered_observation
from camctl.host_files.models import BoundDirectories, FilePurpose
from camctl.host_files.capture_sources import prepare_capture_workspace, workspace_path
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    ProcessInput,
    register_acceptance_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
    register_cancellation_guards,
)
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest,
    SchedulingRepository,
    StartActionRequest,
    register_window_guard,
)
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.persistence.runtime import (
    DatabaseUnsupportedFormatError,
    DbConfig,
    DbOpenMode,
    open_existing,
)
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import (
    ReportDecisionKind,
    ReportingRepository,
    register_report_guards,
    register_sync_guard,
)


pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000
_ORIGINAL = b"II*\x00hasselblad-raw"
_PREVIEW = b"\xff\xd8preview\xff\xd9"
_JPEG_ORIGINAL = b"\xff\xd8jpeg-original\xff\xd9"
_RETURNED = EvidenceRegistry((
    ENTERED,
    EvidenceContract("operation_returned", 1, "control", frozenset()),
    EvidenceContract("photo_taken", 1, "control",
        frozenset({"activity_id", "original", "preview", "steps"}),
        identity_field="activity_id"),
    EvidenceContract("not_dispatched", 1, "control", frozenset()),
))
_LEGACY_RESULTS = _RETURNED.with_contract(
    EvidenceContract("results_returned", 1, "result", frozenset())
).with_contract(EvidenceContract(
    "result_files_listed", 1, "result", frozenset({"activity_id", "entries"}),
    identity_field="activity_id",
))

register_acceptance_guards()
register_window_guard()
register_operation_guards()
register_capture_guards()
register_outputs_guards()
register_timelapse_guards()
register_cancellation_guards()
register_report_guards()
register_sync_guard()


class _LegacyPhotoCatalog:
    """模拟旧动作固定的空执行规格，不含 Hasselblad 新任务工厂。"""

    def action_types(self):
        return frozenset({"camera_take_photo", "cancel_task", "report_status"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return DRIVER_ID if self.device_exists(device_id) else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_take_photo"

    def parameter_definition(self, device_id, action_type, parameter_type):
        if self.device_supports(device_id, action_type) and parameter_type == "photo":
            return ParameterDefinition(schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"type": {"const": "photo"}},
                "required": ["type"],
                "additionalProperties": False,
            }, defaults={})
        return None


class _PhotoDriver:
    def __init__(self, staging: Path, *, response: str = "success", before_dispatch=None):
        self.roots = BoundDirectories(staging=staging)
        self.response = response
        self.before_dispatch = before_dispatch
        self.requests: list[ControlRequest] = []

    async def control(self, request: ControlRequest) -> DeviceCallResult:
        self.requests.append(request)
        if request.start_entry is not None:
            request.start_entry.enter(request.ticket)
        entry = entered_observation(request.ticket)
        if self.before_dispatch is not None:
            result = self.before_dispatch(request)
            if asyncio.iscoroutine(result):
                await result
        reason = await request.dispatch_check() if request.dispatch_check is not None else None
        if reason is not None:
            return DeviceCallResult.from_outcome(CallOutcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue("dispatch_not_authorized", "execution", {"reason": reason}),
                effect=EffectState.NO_EFFECT,
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                    EvidenceValue("not_dispatched", 1, {})),
                observations=(entry,),
            ))
        if self.response == "unknown":
            await self._write_workspace(request)
            return DeviceCallResult(observations=(entry,), error={
                "code": "transport_timeout", "message": "工具返回未知",
            })
        await self._write_workspace(request)
        steps = [
            _step_fact(name)
            for name in ("list", "info", "drive_mode_single", "capture_format", "capture")
        ]
        steps[-1]["camera_locator"] = {
            "status": "path",
            "camera_path": "/mnt/media/DCIM/B9996039.3FR",
            "camera_name": "B9996039.3FR",
        }
        if self.response == "missing_camera_name":
            steps[-1]["camera_locator"] = {"status": "explicit_missing", "camera_path": None, "camera_name": None}
        jpeg = request.params.get("capture_format") == "jpeg"
        original_name = "capture.jpg" if jpeg else "capture.3fr"
        original_bytes = _JPEG_ORIGINAL if jpeg else _ORIGINAL
        if jpeg and self.response != "missing_camera_name":
            steps[-1]["camera_locator"] = {"status": "path", "camera_path": "/mnt/media/DCIM/B9996039.JPG", "camera_name": "B9996039.JPG"}
        if self.response == "malformed_steps":
            steps = "not-a-step-list"
        return DeviceCallResult(observations=(DeviceObservation(
            "photo_taken", 1, {
                "activity_id": request.ticket.target_id,
                "original": {
                    "original_name": original_name,
                    "size_bytes": len(original_bytes),
                    "sha256": hashlib.sha256(original_bytes).hexdigest(),
                    "media_type": "image/jpeg" if jpeg else "image/x-hasselblad-3fr",
                },
                "preview": {
                    "original_name": original_name + ".thumbnail.jpg",
                    "size_bytes": len(_PREVIEW),
                    "sha256": hashlib.sha256(_PREVIEW).hexdigest(),
                    "media_type": "image/jpeg",
                },
                "steps": steps,
            }), entry), error=None)

    async def _write_workspace(self, request: ControlRequest) -> None:
        identity = request.ticket.target_id
        directory = await prepare_capture_workspace(self.roots, identity)
        jpeg = request.params.get("capture_format") == "jpeg"
        original_name = "capture.jpg" if jpeg else "capture.3fr"
        (directory / original_name).write_bytes(_JPEG_ORIGINAL if jpeg else _ORIGINAL)
        (directory / (original_name + ".thumbnail.jpg")).write_bytes(_PREVIEW)


def _step_fact(name: str) -> dict[str, object]:
    """按驱动保存的实际步骤映射构造 fake-control 返回事实。"""
    return {
        "name": name,
        "local_exit_code": 0,
        "local_signal": None,
        "transport_error": None,
        "output_failure": None,
        "stderr_hex": "",
        "stdout_size_bytes": 0,
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "signal_failures": [],
    }


class _ManagedCaptureTransport:
    """只替换子进程调用；驱动、capture 文件核验和数据库均用生产实现。"""

    def __init__(self, capture_format="raw"):
        self.calls = []
        self.outcomes = []
        self.capture_format = capture_format

    async def run(self, spec, _stop):
        self.calls.append(spec)
        command = spec.argv[3]
        if command == "list":
            output = b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"
        elif command == "info":
            output = b"serial : SERIAL-1\nfirmware : v4.1.0\n"
        elif command == "set":
            output = b""
        elif command == "capture":
            workspace = Path(spec.argv[-1])
            original = workspace / ("capture_1.JPG" if self.capture_format == "jpeg" else "capture_1.3FR")
            preview = workspace / f"{original.name}.thumbnail.jpg"
            original_bytes = _JPEG_ORIGINAL if self.capture_format == "jpeg" else _ORIGINAL
            original.write_bytes(original_bytes)
            preview.write_bytes(_PREVIEW)
            output = (
                f"capture: OK\nsaved {original} ({len(original_bytes)} bytes)\n"
                f"saved {preview} ({len(_PREVIEW)} bytes)\n"
                f"camera file: /mnt/media/DCIM/{'B9996039.JPG' if self.capture_format == 'jpeg' else 'B9996039.3FR'}\n"
            ).encode()
        else:
            raise AssertionError(f"意外 Hasselblad 子命令: {command}")
        raw = RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")
        self.outcomes.append(raw)
        return raw


class _EmptyResults:
    def __init__(self):
        self.tickets = []

    async def list_round(self, ticket, *, timeout_s):
        self.tickets.append((ticket, timeout_s))
        return ListedResult((), CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": ticket.target_id,
                "entries": [],
            }),),
        ))

    async def list_page(self, *_args, **_kwargs):
        raise AssertionError("legacy v1 result fake must use list_round")


def _config(home: Path, *, storage="host"):
    return load_config({
        "paths": {
            "state_db": str(home / "state.db"),
            "log_file": str(home / "camctl.log"),
            "staging": str(home / "staging"),
            "ready": str(home / "ready"),
            "processing": str(home / "processing"),
        },
        "devices": {"cam-1": {
            "kind": "camera", "driver": DRIVER_ID,
            "hasselblad": {"executable": "/usr/bin/hb_cli", "serial": "SERIAL-1", "output_storage": storage},
        }},
    }, ConfigDefaults())


def _input_time() -> str:
    return datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _entry(driver, evidence=None) -> DriverEntry:
    return DriverEntry(
        driver_id=getattr(driver, "driver_id", DRIVER_ID),
        driver=driver,
        declaration=getattr(driver, "declaration", DriverDeclaration(
            control_supported=True, stop_supported=False, query_supported=False,
            result_supported=False, read_supported=False, digest_supported=False,
            delete_supported=False, directory_supported=False)),
        evidence=evidence or getattr(driver, "evidence", _RETURNED),
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    )


def _apply_cancel(owned, cancel_action_id: int, action_id: int) -> None:
    repository = CancellationRepository()
    started = repository.start_cancel_action(
        StartCancelAction(cancel_action_id, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(
        cancel_action_id,
        FixedCancelSet((FixedTarget(action_id, SelectionBasis.DIRECT,
                                   CancellationEffect.NOT_APPLIED),)),
        _NOW), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_id, = fixed.value.item_ids
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP, _NOW),
        new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error


async def _cancel_through_service(owned, runtime, cancel_action_id=2):
    from camctl.bootstrap.flows import _resolve_and_fix
    from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.outputs import OutputsRepository
    repository = CancellationRepository()
    started = repository.start_cancel_action(
        StartCancelAction(cancel_action_id, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    raw, = owned.connection.execute(
        'SELECT input_fields_json FROM actions WHERE id=?', (cancel_action_id,)).fetchone()
    ids = await _resolve_and_fix(runtime.database, repository, cancel_action_id, raw,
                                 runtime.wall_us, start_entries=runtime.start_entries)
    return await apply_cancel(ApplyCancel(cancel_action_id, ids), CancellationRuntime(
        runtime.database, repository, TargetSettlement(runtime.database, OutputsRepository(), repository,
            lambda _: 'unknown', runtime.wall_us), runtime.wall_us,
        start_entries=runtime.start_entries))


async def _world(tmp_path: Path, *, driver: _PhotoDriver | None = None,
                 cancel_action: bool = False, legacy: bool = False,
                 wall: list[int] | None = None, results=None, evidence=None,
                 driver_factory=None, storage="host", capture_format="raw",
                 extra_actions=(), photo_group=None, photo_focus=None, start_first=True, catalog_factory=None):
    cfg = _config(tmp_path, storage=storage)
    state_db = Path(cfg.paths.state_db)
    initialized = initialize_state(cfg, state_db)
    assert initialized.outcome is InitOutcome.CREATED, initialized.detail
    owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
    selected_driver = (driver_factory(cfg) if driver_factory is not None else
                       driver or _PhotoDriver(Path(cfg.paths.staging)))
    actions = [{
        "name": "单张 RAW", "type": "camera_take_photo", "device_id": "cam-1",
        "scheduled_at": _input_time(), "params": (
            {"type": "photo"} if legacy else {
                "type": "hasselblad_photo",
                "capture_format": capture_format,
                "exposure": {"mode": "P", "iso": 100},
                "white_balance": {"mode": "auto"},
                "focus": {"mode": "MF"},
            }
        ),
        "policy": {"max_delay_ms": 5000},
    }]
    if photo_focus is not None:
        actions[0]["params"]["focus"] = photo_focus
    if photo_group is not None:
        actions[0]["group"] = photo_group
    actions.extend(extra_actions)
    if cancel_action:
        actions.append({"name": "取消单张", "type": "cancel_task",
                        "params": {"target": {"action_instance_id": "1"}}})
    catalog = (catalog_factory(cfg) if catalog_factory is not None else
        _LegacyPhotoCatalog() if legacy else build_catalog(
        cfg, DriverDefinitions({DRIVER_ID: driver_definition()})))
    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("hasselblad.json", {
            "request_id": "1", "created_at": _input_time(), "name": "主机 RAW 拍摄",
            "actions": actions,
        }), catalog, CommandMode.RUN, _NOW), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    scheduled = SchedulingRepository()
    for save, request in ((
        (scheduled.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
        (scheduled.start_action, StartActionRequest(1, _NOW, _NOW)),
    ) if start_first else ()):
        receipt = save(request, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    clock = wall if wall is not None else [_NOW]
    registry = DriverRegistry((_entry(selected_driver, evidence),))
    factory = session_capture_assembly(
        devices=cfg.devices, drivers=registry, results=results,
        staging=Path(cfg.paths.staging),
        wait_config=lambda _action: None,
        wall_us=lambda: clock[0], monotonic_ns=lambda: 9_000_000_000,
    )
    database = await BusinessDatabase.open(
        lambda: open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig()))
    try:
        runtime = await factory(database, "cam-1")
    except BaseException:
        await database.close()
        owned.connection.close()
        raise
    return cfg, owned, selected_driver, runtime, clock


def _saved_output_rows(owned):
    return owned.connection.execute(
        "SELECT id,kind,original_name,media_type,intermediate_file_id"
        " FROM outputs ORDER BY id").fetchall()


def _accept_and_start_second_host_photo(cfg, owned):
    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("hasselblad-second.json", {
            "request_id": "2", "created_at": _input_time(), "name": "第二张主机 RAW 拍摄",
            "actions": [{
                "name": "第二张单张 RAW", "type": "camera_take_photo", "device_id": "cam-1",
                "scheduled_at": _input_time(), "params": {
                    "type": "hasselblad_photo",
                    "capture_format": "raw",
                    "exposure": {"mode": "P", "iso": 100},
                    "white_balance": {"mode": "auto"},
                    "focus": {"mode": "MF"},
                },
                "policy": {"max_delay_ms": 5000},
            }],
        }),
        build_catalog(cfg, DriverDefinitions({DRIVER_ID: driver_definition()})),
        CommandMode.RUN, _NOW), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    action_id, = owned.connection.execute(
        "SELECT id FROM actions ORDER BY id DESC LIMIT 1").fetchone()
    scheduled = SchedulingRepository()
    for save, request in (
        (scheduled.observe_window, ObserveWindowRequest(action_id, _NOW, _NOW)),
        (scheduled.start_action, StartActionRequest(action_id, _NOW, _NOW)),
    ):
        receipt = save(request, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return action_id


async def test_host_photo_uses_saved_task_and_real_assembly_to_register_pair(tmp_path):
    cfg, owned, driver, runtime, _clock = await _world(tmp_path)
    try:
        action = await runtime.action(1)
        assert action["execution_spec_json"] == {
            "output_storage": "host", "start_call_timeout_s": Decimal("180")}
        assert runtime.results is None
        assert runtime.roots.staging == Path(cfg.paths.staging)
        assert runtime.host_capture is not None

        await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        request = driver.requests[0]
        assert request.operation == "take_photo"
        assert request.timeout_s == Decimal("180")
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        outputs = _saved_output_rows(owned)
        assert len(outputs) == 2
        original = next(row for row in outputs if row[1] == int(enum_for("outputs.kind").ORIGINAL))
        preview = next(row for row in outputs if row[1] == int(enum_for("outputs.kind").PREVIEW))
        assert (original[2], original[3]) == ("capture.3fr", "image/x-hasselblad-3fr")
        assert (preview[2], preview[3]) == ("capture.3fr.thumbnail.jpg", "image/jpeg")
        assert owned.connection.execute(
            "SELECT output_id,original_output_id FROM output_origins"
        ).fetchall() == [(preview[0], original[0])]
        host_files = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE owner_action_id=1"
            " AND purpose IN (?,?) ORDER BY purpose",
            (int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
             int(enum_for("intermediate_files.purpose").CAPTURE_PREVIEW)),
        ).fetchall()
        assert len(host_files) == 2
        original_path, preview_path = (Path(cfg.paths.staging) / row[0] for row in host_files)
        assert original_path.read_bytes() == _ORIGINAL
        assert preview_path.read_bytes() == _PREVIEW
        assert not (workspace_path(runtime.roots, request.ticket.target_id) / "capture.3fr").exists()

        # 终态恢复只消费本地保存，不再申请第二次相机调用。
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert len(_saved_output_rows(owned)) == 2
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_confirmed_host_photo_closes_activity_before_success_and_report(tmp_path):
    cfg, owned, driver, runtime, _clock = await _world(tmp_path)
    try:
        await capture_handler("camera_take_photo")(1, runtime)

        report_selection = ReportingRepository().freeze_report(
            new_operation_key(), owned, occurred_at=runtime.wall_us())
        assert report_selection.kind is DbOutcomeKind.COMPLETED, report_selection.error
        assert report_selection.value.kind is ReportDecisionKind.GENERATE
        report = report_selection.value.report
        generated = generate_report_file(GenerationSpec(
            db_path=Path(cfg.paths.state_db),
            report_id=report.report_id,
            from_wm=report.from_wm,
            to_wm=report.to_wm,
            frozen_event_id=report.boundary.last_event_id,
            staging_path=Path(cfg.paths.staging) / "reports" / "host-photo-close.json",
        ))
        document = json.loads(generated.path.read_bytes())
        validate_document("protocol/status-report.schema.json", document)
        photo, = [action for plan in document["plans"] for action in plan["actions"]
                  if action["action_instance_id"] == "1"]
        assert "device_execution" not in photo

        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=1"
        ).fetchone() == (3, 2)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(driver.requests) == 1
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_confirmed_host_photo_releases_device_for_second_new_photo(tmp_path):
    cfg, owned, driver, runtime, _clock = await _world(tmp_path)
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=1"
        ).fetchone() == (3, 2)
        second_action_id = _accept_and_start_second_host_photo(cfg, owned)

        await capture_handler("camera_take_photo")(second_action_id, runtime)

        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (second_action_id,)).fetchone() == (3,)
        assert len(driver.requests) == 2
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key IN ('start/1', ?)",
            (f"start/{second_action_id}",),
        ).fetchone() == (2,)
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("commit_before_unknown", [False, True])
async def test_unknown_activity_close_receipt_recovers_without_repeating_capture(
        tmp_path, monkeypatch, commit_before_unknown):
    _cfg, owned, driver, runtime, _clock = await _world(tmp_path)
    conclude = runtime.capture.conclude_activity
    calls = []

    def conclude_with_unknown(command, key, current_owned):
        calls.append((command, key))
        if len(calls) == 1 and not commit_before_unknown:
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("收场提交回执未知"))
        receipt = conclude(command, key, current_owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        if len(calls) == 1:
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("收场提交后回执丢失"))
        return receipt

    monkeypatch.setattr(runtime.capture, "conclude_activity", conclude_with_unknown)
    try:
        with pytest.raises(ConsistencyError, match="活动收场"):
            await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        assert len(calls) == 1
        assert calls[0][0].action_id == 1
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (2,)
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=1"
        ).fetchone() == ((3, 2) if commit_before_unknown else (2, 1))
        assert _saved_output_rows(owned) == []

        await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        assert len(calls) == 2
        assert calls[0][0] == calls[1][0]
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=1"
        ).fetchone() == (3, 2)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(_saved_output_rows(owned)) == 2
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("receipt_kind", [
    DbOutcomeKind.NOT_EXECUTED,
    DbOutcomeKind.ROLLED_BACK,
    DbOutcomeKind.UNKNOWN,
])
async def test_noncompleted_activity_close_receipt_stops_under_python_optimization(receipt_kind):
    script = """
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcome, DbOutcomeKind
sys.path.insert(0, str(Path.cwd() / 'apps/camctl/tests'))
from integration.capture.test_host_photo_flow import _world

async def main():
    with TemporaryDirectory() as directory:
        _, owned, _, runtime, _ = await _world(Path(directory))
        runtime.capture.conclude_activity = lambda *_args: DbOutcome(
            DbOutcomeKind(sys.argv[1]), error=RuntimeError('receipt fault'))
        try:
            try:
                await capture_handler('camera_take_photo')(1, runtime)
            except ConsistencyError as error:
                if 'receipt fault' not in str(error):
                    raise SystemExit(f'wrong failure: {error}')
                print('noncompleted receipt rejected')
            else:
                raise SystemExit('capture continued after a non-COMPLETED receipt')
        finally:
            await runtime.database.close()
            owned.connection.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, receipt_kind.value],
        cwd=Path(__file__).resolve().parents[5],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "noncompleted receipt rejected"


@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
@pytest.mark.parametrize("storage", ["host", "device"])
async def test_actual_hasselblad_driver_steps_and_file_port_finish_through_capture_handler(tmp_path, capture_format, storage):
    transport = _ManagedCaptureTransport(capture_format)

    def make_driver(config):
        return HasselbladDriver(
            config.devices,
            BoundDirectories(Path(config.paths.staging)),
            monotonic_ns=lambda: 1,
            transport=transport,
        )

    cfg, owned, driver, runtime, _clock = await _world(
        tmp_path, driver_factory=make_driver, capture_format=capture_format, storage=storage)
    try:
        await capture_handler("camera_take_photo")(1, runtime)

        assert isinstance(driver, HasselbladDriver)
        assert [call.argv[3] for call in transport.calls] == [
            "list", "info", "set", "set", "set", "set", "set", "set", "capture",
        ]
        assert [call.timeout_s for call in transport.calls] == [Decimal("10")] * 8 + [Decimal("180")]
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        result_json, = owned.connection.execute(
            "SELECT a.result_json FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key='start/1'").fetchone()
        start_result = json.loads(result_json)
        photo = start_result["observations"][0]
        preparation_json, = owned.connection.execute(
            "SELECT a.result_json FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key LIKE 'prepare/%'").fetchone()
        preparation = json.loads(preparation_json)
        steps = next(item for item in preparation["observations"]
                     if item["type"] == "photo_preparation_steps")["data"]["steps"] + photo["data"]["steps"]
        assert [step["name"] for step in steps] == [
            "list", "info", "drive_mode_single", "capture_format", "exposure_mode", "iso", "white_balance_mode", "focus_mode", "capture",
        ]
        calls = preparation["diagnostics"]["calls"] + start_result["diagnostics"]["calls"]
        assert [call["stdout"]["hex"] for call in calls] == [raw.output.hex() for raw in transport.outcomes]
        assert all(call["stderr"] == {"state":"complete", "hex":""} for call in calls)
        assert all("stderr_hex" not in step for step in steps)
        assert all(isinstance(step, dict) for step in steps)
        assert len(_saved_output_rows(owned)) == 2
        original_path, preview_path = (
            Path(cfg.paths.staging) / row[0]
            for row in owned.connection.execute(
                "SELECT relative_path FROM intermediate_files WHERE owner_action_id=1"
                " AND purpose IN (?,?) ORDER BY purpose",
                (int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
                 int(enum_for("intermediate_files.purpose").CAPTURE_PREVIEW)),
            ).fetchall()
        )
        assert original_path.read_bytes() == (_JPEG_ORIGINAL if capture_format == "jpeg" else _ORIGINAL)
        assert preview_path.read_bytes() == _PREVIEW
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_host_photo_resumes_after_original_archive_and_ignores_later_binding_failure(
        tmp_path, monkeypatch):
    cfg, owned, driver, runtime, _clock = await _world(tmp_path)
    from camctl.capture import host_photo

    archive = host_photo.archive_capture_file
    stopped = False

    async def archive_then_interrupt(roots, activity_id, artifact, target, **kwargs):
        nonlocal stopped
        await archive(roots, activity_id, artifact, target, **kwargs)
        if target.purpose is FilePurpose.CAPTURE_ORIGINAL and not stopped:
            stopped = True
            raise RuntimeError("测试在原图归档后中断")

    monkeypatch.setattr(host_photo, "archive_capture_file", archive_then_interrupt)
    try:
        with pytest.raises(RuntimeError, match="原图归档后中断"):
            await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (2,)
        monkeypatch.setattr(host_photo, "archive_capture_file", archive)
        runtime.binding_check = lambda saved: BindingResult(
            BindingStatus.DEVICE_MISSING, saved, None)

        await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=1"
        ).fetchone() == (3, 2)
        assert len(_saved_output_rows(owned)) == 2
        files = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE owner_action_id=1"
            " AND purpose IN (?,?)",
            (int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
             int(enum_for("intermediate_files.purpose").CAPTURE_PREVIEW)),
        ).fetchall()
        assert all((Path(cfg.paths.staging) / row[0]).is_file() for row in files)
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_unknown_capture_keeps_work_files_and_never_retries(tmp_path):
    cfg, owned, driver, runtime, _clock = await _world(
        tmp_path, driver=_PhotoDriver(Path(_config(tmp_path).paths.staging), response="unknown"))
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
        error_code, error_details = owned.connection.execute(
            "SELECT error_code,error_details_json FROM actions WHERE id=1").fetchone()
        assert error_code is not None
        assert json.loads(error_details)["reason"] == "outputs_unknown"
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=1").fetchone()
        work = workspace_path(runtime.roots, str(activity_id))
        assert (work / "capture.3fr").read_bytes() == _ORIGINAL
        assert (work / "capture.3fr.thumbnail.jpg").read_bytes() == _PREVIEW
        assert _saved_output_rows(owned) == []
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_confirmed_start_with_malformed_photo_payload_keeps_consistency_error(tmp_path):
    driver = _PhotoDriver(Path(_config(tmp_path).paths.staging), response="malformed_steps")
    _cfg, owned, driver, runtime, _clock = await _world(tmp_path, driver=driver)
    try:
        for _ in range(2):
            with pytest.raises(ConsistencyError, match="steps|步骤"):
                await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        assert owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key='start/1'").fetchone() == (2,)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (2,)
        assert _saved_output_rows(owned) == []
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_entered_dispatch_preserves_rejected_cancel_and_window_failure(tmp_path):
    # 取消服务在整次 START 已进入后执行；逐项拒绝后原拍摄继续。
    cfg, owned, _driver, _runtime, _clock = await _world(
        tmp_path / "cancel", cancel_action=True)

    async def cancel_callback(request):
        result = await _cancel_through_service(owned, runtime)
        assert result.items[0].status == 4

    canceled_driver = _PhotoDriver(Path(cfg.paths.staging), before_dispatch=cancel_callback)
    registry = DriverRegistry((_entry(canceled_driver),))
    factory = session_capture_assembly(
        devices=cfg.devices, drivers=registry, staging=Path(cfg.paths.staging),
        wait_config=lambda _action: None, wall_us=lambda: _NOW,
        monotonic_ns=lambda: 9_000_000_000)
    runtime = await factory(_runtime.database, "cam-1")
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(canceled_driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(_saved_output_rows(owned)) == 2
    finally:
        await runtime.database.close()
        owned.connection.close()

    window = [_NOW]

    async def expire_during_dispatch(_request):
        window[0] = _NOW + 5_000_001

    cfg, owned, _driver, _runtime, window = await _world(
        tmp_path / "window", wall=window)
    expired_driver = _PhotoDriver(Path(cfg.paths.staging), before_dispatch=expire_during_dispatch)
    factory = session_capture_assembly(
        devices=cfg.devices, drivers=DriverRegistry((_entry(expired_driver),)),
        staging=Path(cfg.paths.staging), wait_config=lambda _action: None,
        wall_us=lambda: window[0], monotonic_ns=lambda: 9_000_000_000)
    runtime = await factory(_runtime.database, "cam-1")
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(expired_driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=1").fetchone()
        assert not workspace_path(runtime.roots, str(activity_id)).exists()
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_legacy_photo_does_not_use_current_hasselblad_binding_as_storage_selector(tmp_path):
    _cfg, owned, driver, runtime, _clock = await _world(tmp_path, legacy=True)
    try:
        assert (await runtime.action(1))["execution_spec_json"] == {}
        assert runtime.results is None
        with pytest.raises(CapabilityNotDeclaredError, match="result"):
            await capture_handler("camera_take_photo")(1, runtime)
        assert driver.requests == []
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (2,)
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_legacy_photo_with_result_port_keeps_device_listing_path(tmp_path):
    results = _EmptyResults()
    _cfg, owned, driver, runtime, _clock = await _world(
        tmp_path, legacy=True, results=results, evidence=_LEGACY_RESULTS)
    try:
        assert (await runtime.action(1))["execution_spec_json"] == {}
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert driver.requests[0].timeout_s == Decimal("30")
        assert len(results.tickets) == 1
        assert results.tickets[0][1] == Decimal("10")
        assert owned.connection.execute(
            "SELECT 1 FROM intermediate_files WHERE owner_action_id=1 AND purpose IN (?,?)",
            (int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
             int(enum_for("intermediate_files.purpose").CAPTURE_PREVIEW)),
        ).fetchone() is None
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
async def test_jpeg_photo_unknown_complete_reuses_original_request_and_pair(tmp_path, capture_format):
    _cfg, owned, driver, runtime, _clock = await _world(tmp_path, capture_format=capture_format)
    from camctl.persistence.repositories.host_capture import HostCaptureRepository

    real = HostCaptureRepository()

    class UnknownCommittedReceipt:
        def __init__(self):
            self.saved = []
            self.verified = []
            self.verifier_connections = []

        def prepare_host_photo(self, *args):
            return real.prepare_host_photo(*args)

        def save_host_photo_complete(self, request, key, current_owned):
            self.saved.append((request, key))
            receipt = real.save_host_photo_complete(request, key, current_owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            current_owned.connection.execute("BEGIN").close()
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("模拟提交后回执丢失"))

        def verify_operation(self, request, key, verifier):
            self.verified.append((request, key))
            assert verifier.connection is not owned.connection
            self.verifier_connections.append(verifier.connection)
            assert owned.connection.in_transaction is False
            assert verifier.connection.in_transaction is False
            return real.verify_operation(request, key, verifier)

    uncertain = UnknownCommittedReceipt()
    runtime.host_capture = uncertain
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM intermediate_files").fetchone() == (2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM output_origins").fetchone() == (1,)
        assert len(uncertain.saved) == len(uncertain.verified) == 1
        assert uncertain.saved[0] == uncertain.verified[0]
        assert await runtime.database.read(
            lambda current: current.connection is uncertain.verifier_connections[0])
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(_saved_output_rows(owned)) == 2
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("verifier_failure", ["unknown", "raises"])
async def test_unknown_host_photo_receipt_with_failed_verification_retains_request(
    tmp_path, verifier_failure,
):
    _cfg, owned, driver, runtime, _clock = await _world(tmp_path)
    from camctl.persistence.repositories.host_capture import HostCaptureRepository

    real = HostCaptureRepository()

    class UnknownCommittedReceipt:
        def __init__(self):
            self.saved = []
            self.verified = []
            self.verifier_connections = []

        def prepare_host_photo(self, *args):
            return real.prepare_host_photo(*args)

        def save_host_photo_complete(self, request, key, current_owned):
            self.saved.append((request, key))
            receipt = real.save_host_photo_complete(request, key, current_owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            if len(self.saved) == 1:
                current_owned.connection.execute("BEGIN").close()
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("提交回执未知"))
            return receipt

        def verify_operation(self, request, key, verifier):
            self.verified.append((request, key))
            self.verifier_connections.append(verifier.connection)
            if verifier_failure == "raises":
                raise OSError("测试仓储核实错误")
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("核实仍未知"))

    uncertain = UnknownCommittedReceipt()
    runtime.host_capture = uncertain
    try:
        expected_error = ConsistencyError if verifier_failure == "unknown" else OSError
        with pytest.raises(expected_error):
            await capture_handler("camera_take_photo")(1, runtime)

        assert len(driver.requests) == 1
        assert len(uncertain.saved) == 1
        assert owned.connection.in_transaction is False
        assert uncertain.verified == uncertain.saved
        pending = runtime.pending_host_photo_completions[1]
        assert (pending.request, pending.key) == uncertain.saved[0]
        assert await runtime.database.read(
            lambda current: current.connection is uncertain.verifier_connections[0])
        assert owned.connection.execute(
            "SELECT size_bytes,sha256 FROM intermediate_files WHERE owner_action_id=1 ORDER BY purpose"
        ).fetchall() == [
            (len(_ORIGINAL), hashlib.sha256(_ORIGINAL).hexdigest()),
            (len(_PREVIEW), hashlib.sha256(_PREVIEW).hexdigest()),
        ]
        assert _saved_output_rows(owned) == []
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert uncertain.saved == [uncertain.saved[0], uncertain.saved[0]]
        assert 1 not in runtime.pending_host_photo_completions
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(_saved_output_rows(owned)) == 2
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_v1_metadata_rejects_host_photo_before_grant_or_workspace(tmp_path):
    cfg, owned, driver, _runtime, _clock = await _world(tmp_path)
    await _runtime.database.close()
    owned.connection.close()
    # Keep a valid saved host action but expose a V1 format marker to the
    # handler. This is a defensive boundary for an inconsistent pending task,
    # before the schema-side admission guard can reject it at acceptance.
    legacy = sqlite3.connect(cfg.paths.state_db)
    try:
        legacy.execute("UPDATE database_metadata SET format_version=1 WHERE id=1")
        legacy.commit()
    finally:
        legacy.close()
    owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    database = await BusinessDatabase.open(lambda: open_existing(
        Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()))
    runtime = await session_capture_assembly(
        devices=cfg.devices,
        drivers=DriverRegistry((_entry(driver),)),
        staging=Path(cfg.paths.staging),
        wait_config=lambda _action: None,
        wall_us=lambda: _NOW,
        monotonic_ns=lambda: 9_000_000_000,
    )(database, "cam-1")
    try:
        activities_before = owned.connection.execute(
            "SELECT COUNT(*) FROM device_activities WHERE action_id=1").fetchone()
        with pytest.raises(DatabaseUnsupportedFormatError):
            await capture_handler("camera_take_photo")(1, runtime)
        assert driver.requests == []
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE action_id=1").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM device_activities WHERE action_id=1").fetchone() == activities_before
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM intermediate_files WHERE owner_action_id=1 AND purpose IN (?,?)",
            (int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
             int(enum_for("intermediate_files.purpose").CAPTURE_PREVIEW)),
        ).fetchone() == (0,)
        assert list((Path(cfg.paths.staging) / "capture-work").iterdir()) == []
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=1").fetchone() == (2,)
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
@pytest.mark.parametrize("storage", ["host", "device"])
async def test_explicit_missing_camera_name_only_allows_host_source(tmp_path, capture_format, storage):
    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage=storage, capture_format=capture_format)
    driver.response = "missing_camera_name"
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        expected_status = 3 if storage == "host" else 4
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (expected_status,)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == ((2 if storage == "host" else 0),)
        result_json, = owned.connection.execute("SELECT result_json FROM operation_attempts").fetchone()
        assert json.loads(result_json)["observations"][0]["data"]["steps"][-1]["camera_locator"]["status"] == "explicit_missing"
        if storage == "device":
            assert json.loads(owned.connection.execute("SELECT error_details_json FROM actions WHERE id=1").fetchone()[0]) == {"activity_id": "1", "reason": "invalid_outputs"}
            original_name = "capture.jpg" if capture_format == "jpeg" else "capture.3fr"
            workspace = workspace_path(runtime.roots, "1")
            assert (workspace / original_name).is_file()
            assert (workspace / (original_name + ".thumbnail.jpg")).read_bytes() == _PREVIEW
            assert owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files").fetchall() == [(1,1),(1,1)]
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("storage", ["host", "device"])
async def test_jpeg_photo_keeps_both_roles_and_distinct_content(tmp_path, storage):
    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage=storage, capture_format="jpeg")
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        source_table = "intermediate_files" if storage == "host" else "device_files"
        source_column = "intermediate_file_id" if storage == "host" else "device_file_id"
        rows = owned.connection.execute(f"SELECT o.kind,o.media_type,f.size_bytes,f.sha256 FROM outputs o JOIN {source_table} f ON f.id=o.{source_column} ORDER BY o.kind").fetchall()
        assert rows == [(1, "image/jpeg", len(_JPEG_ORIGINAL), hashlib.sha256(_JPEG_ORIGINAL).hexdigest()),
                        (3, "image/jpeg", len(_PREVIEW), hashlib.sha256(_PREVIEW).hexdigest())]
        paths = [Path(cfg.paths.staging) / value[0] for value in owned.connection.execute("SELECT relative_path FROM intermediate_files ORDER BY id")]
        assert [path.read_bytes() for path in paths] == [_JPEG_ORIGINAL, _PREVIEW]
        assert [path.parent.name for path in paths] == ["capture-originals", "capture-previews"]
        assert all(path.suffix == ".jpg" for path in paths)
        assert owned.connection.execute("SELECT COUNT(*) FROM output_origins").fetchone() == (1,)
    finally:
        await runtime.database.close()
        owned.connection.close()
