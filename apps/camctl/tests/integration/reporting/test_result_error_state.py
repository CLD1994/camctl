"""固定 H 错误不能解释时停止报告；合法历史的文件失败仍归 REPORT。"""

from copy import deepcopy
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.contracts.public_projection import PublicProjectionError
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting import generation
from camctl.reporting.generation import generate_report_file
from camctl.reporting.messages import ErrorKind, JobMessage, ResultFailureMessage, new_job_id
from camctl.reporting.worker import run_job

from .test_result_error_generation import _exhausted_world, _freeze, _spec

pytestmark = pytest.mark.asyncio


def _incomplete_history_reader(spec, report, action_id, activity_id, expected_error):
    """只改变稳定读取返回值；冻结校验、入选和实体回放均委托真实仓储。"""
    actual = HistoryRepository(spec.db_path)
    reader = create_autospec(HistoryRepository, instance=True, spec_set=True)
    reader.frozen_registration.side_effect = actual.frozen_registration
    reader.select_report_scope.side_effect = actual.select_report_scope
    reader.related_entity_ids.side_effect = actual.related_entity_ids
    supplied = []

    def restore(entity, entity_id, boundary, *, event_batch_size=128):
        rows = deepcopy(actual.restore_entity(
            entity, entity_id, boundary, event_batch_size=event_batch_size))
        if entity == "action" and entity_id == action_id:
            assert boundary == report.boundary
            activity = rows["device_activities", activity_id]
            assert activity["last_error_json"] == expected_error
            # 模拟读取端口返回不能解释的 H 字段，不修改任何 SQLite 行。
            activity["last_error_json"] = {"code": expected_error["code"]}
            supplied.append(boundary)
        return rows

    reader.restore_entity.side_effect = restore
    return reader, supplied


def _job(owned, spec):
    metadata = owned.metadata
    return JobMessage(
        job_id=new_job_id(), report_id=spec.report_id,
        from_wm=spec.from_wm, to_wm=spec.to_wm, frozen_event_id=spec.frozen_event_id,
        instance_id=metadata.instance_id, db_path=str(spec.db_path),
        staging_path=str(spec.staging_path), staging_root=metadata.staging_path,
        ready_root=metadata.ready_path, processing_root=metadata.processing_path,
        entity_batch_size=spec.entity_batch_size, event_batch_size=spec.event_batch_size,
        busy_timeout_ms=9000,
    )


def _file_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _snapshot(owned, report, runtime):
    return (
        owned.connection.execute("SELECT * FROM reports WHERE id=?", (report.report_id,)).fetchone(),
        owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall(),
        _file_bytes(Path(owned.metadata.ready_path)),
        _file_bytes(Path(owned.metadata.staging_path)),
        tuple(runtime.results.calls), runtime.driver.control.await_count,
    )


def _assert_preserved(owned, report, runtime, spec, before):
    assert not spec.staging_path.exists(), "失败的半成品不得保留为完整报告文件"
    assert _snapshot(owned, report, runtime) == before


def _assert_error_path(message):
    # 字段路径属于诊断事实，不逐字限定可翻译的人类说明。
    assert "last_error_json" in message or "device_execution" in message
    assert "error" in message


async def test_historical_error_shape_is_state_failure(tmp_path):
    owned, runtime, action_id, error = await _exhausted_world(tmp_path, "timelapse", True)
    try:
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "incomplete-error.json")
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        reader, supplied = _incomplete_history_reader(spec, report, action_id, activity_id, error)
        before = _snapshot(owned, report, runtime)
        try:
            with pytest.raises((ConsistencyError, PublicProjectionError)) as caught:
                generate_report_file(spec, repository=reader)
            _assert_error_path(str(caught.value))
            assert supplied == [report.boundary]
        finally:
            _assert_preserved(owned, report, runtime, spec, before)
    finally:
        owned.connection.close()


async def test_uninterpretable_frozen_error_stops_worker_without_publish(tmp_path, monkeypatch):
    owned, runtime, action_id, error = await _exhausted_world(tmp_path, "timelapse", True)
    try:
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "worker-incomplete-error.json")
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        reader, supplied = _incomplete_history_reader(spec, report, action_id, activity_id, error)
        constructor = create_autospec(HistoryRepository, spec_set=True, return_value=reader)
        monkeypatch.setattr(generation, "HistoryRepository", constructor)
        before = _snapshot(owned, report, runtime)
        job = _job(owned, spec)
        try:
            result = run_job(job)
            assert isinstance(result, ResultFailureMessage)
            assert (result.job_id, result.instance_id) == (job.job_id, job.instance_id)
            assert supplied == [report.boundary]
            assert result.error_kind is ErrorKind.STATE
            _assert_error_path(result.error_message)
        finally:
            _assert_preserved(owned, report, runtime, spec, before)
    finally:
        owned.connection.close()


async def test_output_fsync_error_remains_report_failure(tmp_path, monkeypatch):
    owned, runtime, _action_id, _error = await _exhausted_world(tmp_path, "timelapse", True)
    try:
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "worker-io-error.json")
        before = _snapshot(owned, report, runtime)
        writes = []

        def failed_fsync(fd):
            # 只有真实 Schema 校验和写出完成、flush 后才会到此边界。
            payload = spec.staging_path.read_bytes()
            assert payload
            writes.append((fd, payload))
            raise OSError("report fsync refused")

        monkeypatch.setattr(generation.os, "fsync", failed_fsync)
        job = _job(owned, spec)
        try:
            result = run_job(job)
            assert isinstance(result, ResultFailureMessage)
            assert (result.job_id, result.instance_id) == (job.job_id, job.instance_id)
            assert len(writes) == 1, "合法原历史必须实际到达文件同步边界"
            assert result.error_kind is ErrorKind.REPORT
            assert result.error_code == "OSError"
        finally:
            _assert_preserved(owned, report, runtime, spec, before)
    finally:
        owned.connection.close()
