"""真实 START 的诊断随同一历史边界恢复，报告只消费结构化事实。"""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.capture import host_photo
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue
from camctl.operations.result_format import read_result_document
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import ReportingRepository, ReportDecisionKind

from .test_host_photo_flow import _saved_output_rows
from ..support.photo_diagnostics_cases import (
    StartReceipts, actual_photo_world, advance_photo, assert_original_streams,
    cold_photo_runtime, start_outcome, start_row,
)

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
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    # 完整原结果在实际事务中提交后中断，归档尚未开始。
    receipts = StartReceipts(monkeypatch)
    archive = host_photo.archive_capture_file

    async def interrupted(*args, **kwargs):
        raise OSError("原 START 完整提交后中断")

    monkeypatch.setattr(host_photo, "archive_capture_file", interrupted)
    try:
        with pytest.raises(OSError, match="完整提交后中断"):
            await advance_photo(runtime)
        saved = start_row(owned)
        actual = receipts.calls[0].finish.outcome.outcome
        assert start_outcome(owned) == actual
        assert_original_streams(actual, transport, owned)
        history = HistoryRepository(Path(cfg.paths.state_db))
        frozen = history.current_boundary()
        expected = history.restore_entity("action", 1, frozen)
        attempt = expected[("operation_attempts", saved["id"])]
        historical = read_result_document(attempt["status"], attempt["effect_state"],
            attempt["result_json"], attempt["error_json"])
        assert historical == actual
        assert_original_streams(historical, transport, owned)
        assert historical.observations[0].data == actual.observations[0].data
        assert not _saved_output_rows(owned)

        # 独立损坏副本只移除结构化观察；即使双流有成功文本也必须拒绝。
        corrupt_path = tmp_path / "missing-observation.db"
        with sqlite3.connect(corrupt_path) as copied:
            owned.connection.backup(copied)
            malformed = parse_exact_json(saved["result_json"])
            malformed["observations"] = []
            copied.execute("UPDATE operation_attempts SET result_json=? WHERE id=?",
                           (json.dumps(malformed), saved["id"]))
        corrupt = await cold_photo_runtime(cfg, corrupt_path, clock)
        with pytest.raises(ConsistencyError):
            await advance_photo(corrupt)
        with sqlite3.connect(corrupt_path) as copied:
            assert copied.execute("SELECT count(*) FROM outputs").fetchone() == (0,)
        assert start_row(owned) == saved
        assert not _saved_output_rows(owned)

        log = Path(cfg.paths.log_file)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ordinary diagnostics discarded", encoding="utf-8")
        log.write_bytes(b"")
        clock[0] += 99
        recovered = await cold_photo_runtime(cfg, owned, clock)
        monkeypatch.setattr(host_photo, "archive_capture_file", archive)
        await advance_photo(recovered)
        assert len(_saved_output_rows(owned)) == 2
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert len(transport.calls) == 9
        assert log.read_bytes() == b""
        assert start_row(owned) == saved
        assert history.restore_entity("action", 1, frozen) == expected
        photo, document = _public_report(cfg, owned, recovered, "diagnostic-success.json")
        assert "device_execution" not in photo
    finally:
        owned.connection.close()


async def test_missing_observation_never_reparses_successful_diagnostics(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await actual_photo_world(tmp_path)
    original = driver.control
    actual = None

    async def returned(request):
        nonlocal actual
        response = await original(request)
        from camctl.devices.ports import DeviceCallResult
        actual = replace(response.outcome, status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
                         error=ErrorValue("device_error", "control", {"reason": "missing_photo_observation"}),
                         observations=())
        return DeviceCallResult.from_outcome(actual, diagnostics_check=response.diagnostics_check)

    monkeypatch.setattr(driver, "control", returned)
    try:
        await advance_photo(runtime)
        saved = start_row(owned)
        decoded = start_outcome(owned)
        assert decoded == actual
        assert_original_streams(decoded, transport, owned)
        assert b"camera file:" in decoded.diagnostics.calls[-1].stdout.data
        assert not decoded.observations
        assert not _saved_output_rows(owned)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
        await advance_photo(runtime)
        assert start_row(owned) == saved
        assert not _saved_output_rows(owned)
        assert len(transport.calls) == 9
        photo, document = _public_report(cfg, owned, runtime, "diagnostic-failure.json")
        assert "missing_photo_observation" not in json.dumps(document)
        assert "camera file:" not in json.dumps(document)
        assert photo["error"]["code"] == "capture_result_unconfirmed"
        assert "steps" not in photo["error"]
    finally:
        owned.connection.close()
