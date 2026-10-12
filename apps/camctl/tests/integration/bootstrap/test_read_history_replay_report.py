"""历史回放与固定 H 报告不加载新配置、无设备与文件副作用。"""

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from camctl.bootstrap import capture_assembly
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.contracts.values import new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.validators import ValidatedEvent
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.persistence.models import DbOutcomeKind
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.transaction import row_facts
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import ReportingRepository

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401

def _boundary_of(owned):
    from camctl.persistence.repositories.history import HistoryRepository
    return HistoryRepository(Path(
        owned.connection.execute("PRAGMA database_list").fetchone()[2])).current_boundary()


pytestmark = pytest.mark.asyncio
register_report_guards()
register_sync_guard()
register_outputs_guards()

_META = {"id", "created_event_id", "last_event_id", "change_count"}
_TABLES = ("file_copies", "operation_runs", "operation_attempts")


def _events_of(connection, entity_type: int, entity_id: int):
    rows = connection.execute(
        "SELECT h.id, h.transaction_id, h.event_type, h.event_version,"
        " h.occurred_at, h.clock_status, h.change_seq, h.body_json"
        " FROM history_events h"
        " JOIN entity_event_links l ON l.event_id=h.id"
        " WHERE l.entity_type=? AND l.entity_id=? ORDER BY h.id",
        (entity_type, entity_id)).fetchall()
    reference = (entity_type, entity_id)
    events = []
    for raw in rows:
        envelope = decode_event_row(raw)
        events.append(ValidatedEvent(
            envelope=envelope,
            event_name=event_type_name(envelope.event_type),
            branch_name=branch_of(envelope.event_type, envelope.reason)[0],
            references=(reference,),
            row_owners={(row.table, row.row_id): reference
                        for row in envelope.rows},
        ))
    return events


async def test_replay_and_fixed_report_ignore_changed_read_configuration(
        pipeline, monkeypatch):
    """读取责任的回放与报告重建与配置变化无关，且无设备或文件副作用。"""
    owned, roots, source_id = pipeline
    # 前提：经真实媒体链完成一次完整读取，建立读取责任的持久化事实。
    from ..capture.test_media_flow import ProbeTools, ReadDriverDouble, _flow
    from camctl.host_files.media import MediaProbe

    from ..capture.test_input_copy import _CONTENT
    driver = ReadDriverDouble(_CONTENT)
    tools = ProbeTools(MediaProbe(duration_s=Decimal("61"), error=None))
    flow = _flow(pipeline, driver, tools)
    from camctl.capture.media_flow import run_recording_media
    await run_recording_media(flow, 1, 1, source_id)
    await run_recording_media(
        _flow(pipeline, ReadDriverDouble(_CONTENT), tools), 1, 1, source_id)
    read_run, = owned.connection.execute(
        "SELECT id FROM operation_runs WHERE kind=3").fetchone()
    copy_id, = owned.connection.execute(
        "SELECT id FROM file_copies WHERE processing_id=1").fetchone()
    used_before = owned.connection.execute(
        "SELECT attempts_used, max_attempts_used FROM operation_runs WHERE id=?",
        (read_run,)).fetchone()
    assert used_before[0] == 1

    # 固定 H 报告：冻结、生成并保存字节；staging 文件集为副作用基线。
    frozen = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=1)
    assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
    report = frozen.value.report
    db_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    spec = GenerationSpec(
        db_path=db_path, report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=roots.staging / "reports" / "before.json")
    generated = generate_report_file(spec)
    before_bytes = generated.path.read_bytes()
    validate_document("protocol/status-report.schema.json", json.loads(before_bytes))
    staging_before = sorted(str(path.relative_to(roots.staging))
                            for path in roots.staging.rglob("*") if path.is_file())

    # 配置变化：设备驱动改绑、读取上限与间隔改值。
    from types import SimpleNamespace
    from camctl.devices import bindings
    actual_check = bindings.check_binding

    def check(saved, current):
        if saved.device_id != "cam-1":
            return actual_check(saved, current)
        return actual_check(saved, SimpleNamespace(devices={
            "cam-1": {"kind": "camera", "driver": "alternate-camera"}}))

    monkeypatch.setattr(bindings, "check_binding", check)
    monkeypatch.setattr(capture_assembly, "check_binding", check)
    changed = SimpleNamespace(devices={"cam-1": {
        "kind": "camera", "driver": "alternate-camera",
        "recording": {"max_read_attempts": 9, "read_retry_interval_s": "0.01"}}})

    # 固定 H 报告重建：字节一致，不产生新的 staging 文件。
    rebuilt = generate_report_file(GenerationSpec(
        db_path=db_path, report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=roots.staging / "reports" / "after.json",
        entity_batch_size=1, event_batch_size=1))
    assert rebuilt.path.read_bytes() == before_bytes
    assert hashlib.sha256(before_bytes).hexdigest() == generated.sha256
    staging_after = sorted(str(path.relative_to(roots.staging))
                           for path in roots.staging.rglob("*") if path.is_file())
    assert {name for name in staging_after if not name.startswith("reports/")} == {
        name for name in staging_before if not name.startswith("reports/")}

    # 历史回放：录像动作对象的读取责任成员按初始回放恢复，已用次
    # 数、上限、尝试编号与错误事实与当前投影一致；配置变化不参与。
    events = _events_of(owned.connection, 1, 1)
    image = restore(RestoreSeed(
        EntityImage(1, 1, False, {}, 0, 0), INITIAL_BOUNDARY),
        events, _boundary_of(owned))
    projected = {(table, row_id): {name: value for name, value in values.items()
                                   if name not in _META}
                 for (table, row_id), values in image.rows.items()
                 if table in _TABLES}
    for (table, row_id), values in projected.items():
        current = row_facts(owned.connection, table, row_id)
        assert values == {name: value for name, value in dict(current).items()
                          if name not in _META}, (table, row_id)
    read_row = projected[("operation_runs", read_run)]
    assert read_row["attempts_used"] == used_before[0]
    assert read_row["max_attempts_used"] == used_before[1]
    assert read_row["responsibility_key"].startswith("read/")
    attempt_row, = [values for (_table, row_id), values in projected.items()
                    if _table == "operation_attempts"
                    and values["run_id"] == read_run]
    assert (attempt_row["attempt_no"], attempt_row["status"]) == (1, 2)
    assert attempt_row["error_json"] is None

    # 读取配置的变更不被回放或报告消费：回放与报告阶段零工具调用。
    assert tools.calls == ["probe"]
