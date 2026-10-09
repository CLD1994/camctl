"""媒体原申请验证使用真实受理、启动停止及文件归属历史。"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest_asyncio

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.lifecycle import build_runtime, close_runtime
from camctl.capture.files import FileCompletionSave, FileObservationSave, FilePresenceSave, OwnershipSave
from camctl.capture.handlers import _stop_call, capture_handler
from camctl.capture.models import ActivityConcludeSave
from camctl.capture.processing import (
    CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckReason, SourceFileSave,
)
from camctl.capture.result_inputs import RESULT_FILES_CONTRACT
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DriverDeclaration
from camctl.host_files.models import BoundDirectories
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..bootstrap.test_media_assembly import _SessionDriver, _config
from ..bootstrap.test_recording_stop import _RecordCatalog
from .test_capture_contract import ResultsDouble
from .test_input_copy import _CONTENT, _NOW


class _Catalog(_RecordCatalog):
    duration_s = Decimal("60")


_EVIDENCE = EvidenceRegistry((
    EvidenceContract("operation_returned", 1, "control", frozenset()),
    EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract("stop_returned", 1, "stop", frozenset()),
    EvidenceContract("stop_confirmed", 1, "stop", frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract("results_returned", 1, "result", frozenset()),
    RESULT_FILES_CONTRACT,
    EvidenceContract("read_returned", 1, "read", frozenset()),
    EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
))


@pytest_asyncio.fixture
async def media_pipeline(tmp_path):
    cfg = _config(tmp_path)
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    repository, scheduling = CaptureRepository(), SchedulingRepository()
    driver = _SessionDriver(_CONTENT)
    registry = DriverRegistry((DriverEntry("camctl-adb", driver,
        DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
            result_supported=False, read_supported=True, digest_supported=True, delete_supported=False),
        _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("media.json", {
            "request_id": "1", "created_at": instant, "name": "媒体原结果",
            "actions": [{"name": "录像", "type": "camera_record", "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000}}]}), _Catalog(), CommandMode.RUN, _NOW),
            new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        observed = scheduling.observe_window(ObserveWindowRequest(1, _NOW, _NOW), new_operation_key(), owned)
        assert observed.kind is DbOutcomeKind.COMPLETED, observed.error
        started = scheduling.start_action(StartActionRequest(1, _NOW, _NOW), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        factory = session_capture_assembly(devices=cfg.devices, drivers=registry,
            results=ResultsDouble({}), staging=Path(cfg.paths.staging), wait_config=lambda _action: None,
            wall_us=lambda: _NOW, monotonic_ns=lambda: 7_000_000_000)
        runtime = factory(owned, "cam-1")
        await capture_handler("camera_record")(1, runtime)
        stopped = await _stop_call(runtime, runtime.action(1))
        assert stopped.phase == "confirmed", stopped
        assert [name for name, _operation in driver.calls] == ["control", "stop"]
        attempts = owned.connection.execute(
            "SELECT r.kind,t.status,t.result_json FROM operation_attempts t"
            " JOIN operation_runs r ON r.id=t.run_id ORDER BY t.id").fetchall()
        assert len(attempts) == 2 and all(status == int(enum_for("operation_attempts.status").SUCCEEDED)
            and result is not None for _kind, status, result in attempts)
        concluded = repository.conclude_activity(ActivityConcludeSave(1, _NOW), new_operation_key(), owned)
        assert concluded.kind is DbOutcomeKind.COMPLETED, concluded.error
        assert owned.connection.execute("SELECT activity_state FROM device_activities WHERE action_id=1").fetchone() == (3,)
        source = repository.save_file_observation(FileObservationSave(1, "original-recording", {
            "path": "/DCIM/original.mp4"}, _NOW, "original.mp4", "video/mp4"), new_operation_key(), owned)
        assert source.kind is DbOutcomeKind.COMPLETED, source.error
        source_id = source.value.file_id
        for save, command in (
            (repository.save_file_presence, FilePresenceSave(source_id, 2, _NOW,
                locator={"path": "/DCIM/original.mp4"})),
            (repository.save_file_ownership, OwnershipSave(source_id, 1, 1, 2,
                {"task": "original-recording"}, _NOW)),
            (repository.save_file_completion, FileCompletionSave(source_id, 3, _NOW,
                basis=1, observation={"stopped": True}, size_bytes=len(_CONTENT))),
            (repository.save_source_file, SourceFileSave(1, source_id, _NOW)),
            (repository.save_check_decision, CheckDecisionSave(1, CheckDecisionChoice.REQUIRED,
                CheckBasis(CheckReason.INSUFFICIENT_TIMING, 60000), _NOW)),
        ):
            result = save(command, new_operation_key(), owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (2,)
        assert owned.connection.execute(
            "SELECT presence_state,completion_state FROM device_files WHERE id=?", (source_id,)
        ).fetchone() == (2, 3)
        assert owned.connection.execute(
            "SELECT check_decision,check_state,repair_state,source_device_file_id"
            " FROM recording_processing WHERE id=1").fetchone() == (3, 1, 1, source_id)
        yield owned, BoundDirectories(staging=Path(cfg.paths.staging)), source_id
    finally:
        owned.connection.close()
        close_runtime(deps)
