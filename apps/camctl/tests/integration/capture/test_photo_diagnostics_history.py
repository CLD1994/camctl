"""真实 START 的诊断随同一 H 恢复；恢复与公共报告只消费结构化事实。"""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture import host_photo
from camctl.capture.handlers import capture_handler
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.drivers.registry import DriverRegistry
from camctl.devices.evidence import EvidenceRegistry
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue
from camctl.operations.result_format import read_result_document
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import ReportingRepository, ReportDecisionKind

from .test_host_photo_flow import _entry, _saved_output_rows
from .test_photo_diagnostics_ownership import _actual_world

pytestmark = pytest.mark.asyncio


def _public_report(cfg, owned, runtime, name):
    selected = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=runtime.wall_us())
    assert selected.kind is DbOutcomeKind.COMPLETED, selected.error
    assert selected.value.kind is ReportDecisionKind.GENERATE
    report = selected.value.report
    generated = generate_report_file(GenerationSpec(
        db_path=Path(cfg.paths.state_db), report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=Path(cfg.paths.staging) / "reports" / name))
    document = json.loads(generated.path.read_bytes())
    validate_document("protocol/status-report.schema.json", document)
    photo, = [action for plan in document["plans"] for action in plan["actions"]
              if action["action_instance_id"] == "1"]
    def check(value):
        if isinstance(value, dict):
            assert not {"diagnostics", "steps", "call_limit", "stdout", "stderr"} & value.keys()
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(document)
    return photo, document


async def test_actual_start_same_history_boundary_and_logless_recovery(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    # 停在真实 START 已保存、归档尚未开始的位置；原文件已由实际端口替身产生。
    archive = host_photo.archive_capture_file
    async def interrupted(*args, **kwargs):
        raise OSError("归档前中断")
    monkeypatch.setattr(host_photo, "archive_capture_file", interrupted)
    try:
        with pytest.raises(OSError, match="归档前中断"):
            await capture_handler("camera_take_photo")(1, runtime)
        saved = host_photo._saved_start(runtime, 1)
        actual = host_photo._load_outcome(saved)
        history = HistoryRepository(Path(cfg.paths.state_db))
        frozen = history.current_boundary()
        expected = history.restore_entity("action", 1, frozen)
        attempt, = [row for (table, _id), row in expected.items() if table == "operation_attempts"]
        historical = read_result_document(attempt["status"], attempt["effect_state"],
            attempt["result_json"], None)
        assert historical == actual
        assert [c.stdout.data for c in historical.diagnostics.calls] == [r.output for r in transport.outcomes]
        assert historical.observations[0].data["original"] == actual.observations[0].data["original"]
        assert historical.observations[0].data["preview"] == actual.observations[0].data["preview"]
        assert historical.observations[0].data["steps"][-1] == actual.observations[0].data["steps"][-1]
        assert host_photo._capture_locator(historical) == host_photo._capture_locator(actual)
        assert not _saved_output_rows(owned)
        malformed = json.loads(saved.result_json)
        malformed["observations"] = []
        with pytest.raises(ConsistencyError):
            host_photo._photo_artifacts(runtime, 1, replace(saved, result_json=json.dumps(malformed)))
        assert not _saved_output_rows(owned)
        log = Path(cfg.paths.log_file)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ordinary diagnostics discarded", encoding="utf-8")
        log.write_bytes(b"")
        class NoDevice:
            async def control(self, request):
                raise AssertionError("恢复不得重放 START")
        recovered = session_capture_assembly(
            devices=cfg.devices, drivers=DriverRegistry((_entry(NoDevice(), EvidenceRegistry(())),)),
            staging=Path(cfg.paths.staging), wait_config=lambda _action: None,
            wall_us=lambda: clock[0], monotonic_ns=lambda: 99_000_000_000)(owned, "cam-1")
        monkeypatch.setattr(host_photo, "archive_capture_file", archive)
        await capture_handler("camera_take_photo")(1, recovered)
        assert len(_saved_output_rows(owned)) == 2
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(transport.calls) == 9
        assert log.read_bytes() == b""
        assert history.restore_entity("action", 1, frozen) == expected
        restored = history.restore_entity("action", 1, frozen)
        attempt_after, = [row for (table, _id), row in restored.items() if table == "operation_attempts"]
        assert attempt_after["result_json"] == attempt["result_json"]
        photo, document = _public_report(cfg, owned, recovered, "diagnostic-success.json")
        assert "device_execution" not in photo
    finally:
        owned.connection.close()


async def test_missing_observation_never_reparses_successful_diagnostics(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    original = driver.control
    async def returned(request):
        response = await original(request)
        # 保留真实成功文本和完整 bytes，但无照片观察的实际结构化失败不得恢复为成功。
        from camctl.devices.ports import DeviceCallResult
        actual = replace(response.outcome, status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
                         error=ErrorValue("device_error", "control", {"reason":"missing_photo_observation"}),
                         observations=())
        return DeviceCallResult.from_outcome(actual)
    monkeypatch.setattr(driver, "control", returned)
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        saved = host_photo._saved_start(runtime, 1)
        decoded = host_photo._load_outcome(saved)
        assert decoded.diagnostics.calls[-1].stdout.data == transport.outcomes[-1].output
        assert b"camera file:" in decoded.diagnostics.calls[-1].stdout.data
        assert not decoded.observations
        assert not _saved_output_rows(owned)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
        await capture_handler("camera_take_photo")(1, runtime)
        assert not _saved_output_rows(owned)
        assert len(transport.calls) == 9
        photo, document = _public_report(cfg, owned, runtime, "diagnostic-failure.json")
        assert "missing_photo_observation" not in json.dumps(document)
        assert "camera file:" not in json.dumps(document)
        assert photo["error"]["code"] == "capture_result_unconfirmed"
        assert "steps" not in photo["error"]
    finally:
        owned.connection.close()
