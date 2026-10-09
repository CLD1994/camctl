"""原媒体实际结果必须沿原申请和原键在新连接核实，不能重做工具。"""

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, run_recording_media
from camctl.capture.processing import CheckPhase
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.host_files.io import HashResult
from camctl.host_files.media import MediaArtifact, MediaProbe
from camctl.host_files.models import FileObservation, FileObservationKind
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_input_copy import _CONTENT
from .media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from .test_media_flow import ReadDriverDouble, _flow
from ..bootstrap.test_binding_transactions import _CommitFailure
from ..operations.test_result_reuse import _FaultConnection


pytestmark = pytest.mark.asyncio


class _ActualTools:
    """媒体端口替身携带实际原结果；后继工具返回故意不同。"""

    def __init__(self, branch):
        self.branch = branch
        self.probes = 0
        self.repairs = 0
        self.original_bytes = b"original complete repaired bytes"

    async def probe(self, input):
        self.probes += 1
        return FileTaskResult(FileTaskId(f"probe-{self.probes}"), ran=True,
            value=MediaProbe(Decimal("75.125") if self.probes == 1 else Decimal("9"), None))

    async def repair(self, input, output, *, trim_s):
        self.repairs += 1
        identity = FileTaskId(f"repair-{self.repairs}")
        if self.branch == "check":
            return FileTaskResult(identity, ran=False)
        path = Path(output.root) / output.relative_path
        if self.branch in ("decision", "repair_failed"):
            return FileTaskResult(identity, ran=True, value=MediaArtifact(
                f"original repair diagnostic {self.repairs}",
                FileObservation(FileObservationKind.MISSING, path),
                postprocessing_stopped=True))
        content = self.original_bytes if self.repairs == 1 else b"different later artifact"
        path.write_bytes(content)
        return FileTaskResult(identity, ran=True, value=MediaArtifact(None,
            FileObservation(FileObservationKind.VALID_OBJECT, path, True, size_bytes=len(content)),
            HashResult(hashlib.sha256(content).hexdigest(), len(content), None),
            postprocessing_stopped=True))


@pytest.mark.parametrize("branch", ["check", "decision", "repair_failed", "repair_complete"])
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_media_actual_result_keeps_original_request_and_key_on_reconstructed_flow(
        pipeline, monkeypatch, branch, phase):
    register_capture_guards()
    register_operation_guards()
    register_outputs_guards()
    owned, roots, source_id = pipeline
    driver, tools = ReadDriverDouble(_CONTENT), _ActualTools(branch)
    flow = _flow(pipeline, driver, tools)
    method = {"check": "save_check_result", "decision": "save_repair_decision",
              "repair_failed": "save_repair_result", "repair_complete": "complete_repair_output"}[branch]
    original_save = getattr(CaptureRepository, method)
    inputs, outcomes = [], []
    fault_pending = True

    def save(repository, command, key, operation_owned):
        nonlocal fault_pending
        if branch == "check" and command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, operation_owned)
        inputs.append((command, key))
        if fault_pending:
            fault_pending = False
            connection = (_FaultConnection(operation_owned.connection, "UPDATE recording_processing")
                          if phase == "projection" else
                          _CommitFailure(operation_owned.connection, phase == "commit_after"))
            result = original_save(repository, command, key, replace(operation_owned, connection=connection))
        else:
            result = original_save(repository, command, key, operation_owned)
        outcomes.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, method, save)
    first_step, first_error = None, None
    try:
        first_step = await run_recording_media(flow, 1, 1, source_id)
    except ConsistencyError as error:
        first_error = error
    assert len(inputs) == 1, (
        f"媒体链尚未进入原结果保存边界: step={first_step!r}, error={first_error!r}, "
        f"probe={tools.probes}, repair={tools.repairs}, read={driver.opens!r}")
    assert isinstance(first_error, ConsistencyError), (first_step, first_error)
    assert outcomes[0].kind is (DbOutcomeKind.ROLLED_BACK if phase == "projection" else DbOutcomeKind.UNKNOWN)
    assert tools.probes == 1
    assert tools.repairs == (1 if branch.startswith("repair_") else 0)
    original_request, original_key = inputs[0]

    # 原实际结果由同一会话保留；新连接与新运行时必须先核原申请。
    # 后续时刻及余量改变只可用于尚未计算的决定，不能替换已持有决定。
    owned.connection.close()
    reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        current = replace(flow, owned=reopened,
            sessions=DriverReadSessions(reopened, driver, ticket=None),
            occurred_at=lambda: 1_750_000_200_000_000,
            policy=MediaPolicy(repair_margin_s=Decimal("100")))
        await run_recording_media(current, 1, 1, source_id)
        assert len(inputs) == 2, "当前处理终态也不能遗漏原申请及原键核实"
        assert inputs[1] == (original_request, original_key)
        assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
        assert tools.probes == 1, "原观察保存失败后不能再 probe"
        assert tools.repairs == (0 if branch == "check" else 1), "原工具结果保存失败后不能再 repair"
        assert len(driver.opens) == 1
        state = reopened.connection.execute(
            "SELECT check_state,media_json,repair_state,repair_basis_json,repair_error_json,repair_output_file_id"
            " FROM recording_processing WHERE id=1").fetchone()
        assert state[0] == 3
        assert parse_exact_json(state[1])["duration"]["seconds"] == Decimal("75.125")
        if branch == "decision":
            # 原决定仍需修复，后续实际工具失败不改变其原门槛。
            assert state[2] == 6
            assert parse_exact_json(state[3]) == original_request.basis.as_json()
            assert parse_exact_json(state[3])["threshold_s"] == Decimal("62")
        elif branch == "repair_failed":
            assert state[2] == 6 and parse_exact_json(state[4]) == original_request.error.as_json()
        elif branch == "repair_complete":
            assert state[2] == 5 and state[5] == original_request.output_file_id
            assert reopened.connection.execute(
                "SELECT size_bytes,sha256 FROM intermediate_files WHERE id=?", (state[5],)).fetchone() == (
                len(tools.original_bytes), hashlib.sha256(tools.original_bytes).hexdigest())
        before = tuple(reopened.connection.iterdump())
        await run_recording_media(current, 1, 1, source_id)
        assert tuple(reopened.connection.iterdump()) == before
        assert len(inputs) == 2 and tools.probes == 1
        assert tools.repairs == (0 if branch == "check" else 1)
    finally:
        reopened.connection.close()
