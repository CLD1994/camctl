"""R7 报告专属文件规则与真实发布消费者的组件集成测试。

真实受理、冻结、文件系统与 SQLite 组合：完整发布链（staging 写
入、字节登记、意图、撤下旧 ready 报告、原子移动、发布记录）、
processing 保留与并存、恢复观察、恢复发现补齐发布事实、staging
残留重建与同报告补投，以及发布后同步本地完成与动作成功的共同
保存。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.host_files.handoff import (
    HandoffDirectories,
    PublishResult,
    PublishStage,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import (
    DbConfig,
    DbOpenMode,
    OwnedConnection,
    open_existing,
)
from camctl.persistence.repositories.acceptance import (
    register_acceptance_guards,
)
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.reporting.ack import AckReport
from camctl.reporting.models import FrozenReport, ReportBytes
from camctl.reporting.policy import (
    ReportingRepository,
    publish_report,
    record_recovered_publication,
    record_report_bytes,
    record_report_failure,
    record_report_publish_intent,
    register_report_guards,
    register_sync_guard,
)
from camctl.reporting.publication import (
    DeliveryOutcome,
    DbPublicationSession,
    ReportDirectories,
    decide_republish,
    deliver_report,
    observe_report_locations,
    recover_report_files,
    ReportFileDecision,
    RepublishDecision,
)

from ..persistence.test_runtime import _create_valid_database
from .test_freeze import _submit

register_report_guards()
register_sync_guard()
register_outputs_guards()
register_capture_guards()
register_acceptance_guards()
register_cancellation_guards()

_PAYLOAD = b'{"report_id" : "1", "from_wm" : 0, "to_wm" : 0}\n'
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()
_OTHER_SHA = "cd" * 32


@pytest.fixture
def pipeline(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    directories = ReportDirectories(
        staging=tmp_path / "staging",
        ready=tmp_path / "ready",
        processing=tmp_path / "processing")
    for directory in (directories.staging, directories.ready,
                      directories.processing):
        directory.mkdir()
    yield owned, directories, tmp_path
    owned.connection.close()


def _owned(owned) -> OwnedConnection:
    return OwnedConnection(connection=owned.connection, metadata=None)


def _freeze_report(pipeline) -> FrozenReport:
    """经真实受理与冻结建立一份待发布报告。"""
    owned, _, tmp_path = pipeline

    async def scenario() -> None:
        await _submit(_owned(owned), tmp_path, "1")

    asyncio.run(scenario())
    outcome = ReportingRepository().freeze_report(
        new_operation_key(), _owned(owned), occurred_at=1)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    return outcome.value.report


def _advance_to(pipeline, report: FrozenReport, target: int) -> None:
    """经真实管理入口推进到目标管理状态。"""
    owned = pipeline[0]
    report_id = report.report_id
    steps = {
        2: ("bytes",),
        3: ("bytes", "intent"),
        4: ("bytes", "intent", "publish"),
        5: ("bytes", "failure"),
    }[target]
    for step in steps:
        if step == "bytes":
            outcome = record_report_bytes(
                new_operation_key(), _owned(owned), report_id,
                ReportBytes(len(_PAYLOAD), _SHA))
        elif step == "intent":
            outcome = record_report_publish_intent(
                new_operation_key(), _owned(owned), report_id)
        elif step == "publish":
            outcome = publish_report(
                new_operation_key(), _owned(owned), report_id,
                PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED,
                              True, None))
        else:
            outcome = record_report_failure(
                new_operation_key(), _owned(owned), report_id,
                {"error": "directory_sync_failed"})
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _seed_sync_action(pipeline) -> None:
    """运行中的报告动作 40 与其未结束同步责任。"""
    owned = pipeline[0]
    connection = owned.connection
    boundary = connection.execute(
        "SELECT last_event_id FROM history_transactions ORDER BY id DESC"
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json,"
        " first_window_observed_at, expiration_reason, source_resolution_state,"
        " resolved_source_plan_id, target_selection_state, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (40, 1, 9, 'act-40', 7, NULL, 1750000000000000, NULL, '{}',"
        " NULL, NULL, NULL,"
        " '{}', 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, 1)",
        (boundary, boundary))
    connection.execute(
        "INSERT INTO state_syncs (id, action_id, mode, after_report_id, from_wm,"
        " started_boundary_event_id, status, local_report_id, ack_report_id,"
        " ended_event_id)"
        " VALUES (1, 40, 1, NULL, 0, ?, 1, NULL, NULL, NULL)",
        (boundary,))
    connection.commit()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _last_event_id(owned) -> int:
    return _value(owned,
        "SELECT MAX(id) FROM history_events")[0] or 0


def _new_events(owned, after: int):
    rows = owned.connection.execute(
        "SELECT id, event_type, json_extract(body_json, '$.reason')"
        " FROM history_events WHERE id > ? ORDER BY id", (after,)).fetchall()
    return [(row[1], row[2]) for row in rows]


def _deliver(pipeline, report_id: int, **kwargs):
    owned, directories, _ = pipeline
    session = DbPublicationSession(_owned(owned))
    return asyncio.run(deliver_report(
        report_id, _PAYLOAD,
        HandoffDirectories(directories.staging, directories.ready),
        session, **kwargs))


def _covering(report: FrozenReport) -> AckReport:
    frozen = 0 if report.boundary is None else report.boundary.last_event_id
    return AckReport(report.report_id, report.from_wm, report.to_wm, frozen)


# ---- 完整发布链 ------------------------------------------------------


def test_full_chain_publishes_report_file(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    before = _last_event_id(owned)
    result = _deliver(pipeline, report.report_id)
    assert result.outcome is DeliveryOutcome.PUBLISHED
    ready_file = directories.ready / f"status-report-{report.report_id}-{_SHA}.json"
    assert ready_file.read_bytes() == _PAYLOAD
    assert not list((directories.staging / "reports").iterdir())
    row = _value(owned,
        "SELECT status, size_bytes, sha256, publication_count,"
        " last_published_event_id FROM reports WHERE id = ?",
        report.report_id)
    assert row[0] == 4 and row[1] == len(_PAYLOAD) and row[2] == _SHA
    assert row[3] == 1 and row[4] is not None
    assert _new_events(owned, before) == [(28, 2), (28, 3), (28, 4)]


def test_publish_after_claimed_deletion_keeps_recorded_fact(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    result = _deliver(pipeline, report.report_id)
    assert result.outcome is DeliveryOutcome.PUBLISHED
    # 主程序领取并删除文件后，已确认的发布事实不被撤销。
    (directories.ready
     / f"status-report-{report.report_id}-{_SHA}.json").unlink()
    row = _value(owned,
        "SELECT status, publication_count FROM reports WHERE id = ?",
        report.report_id)
    assert row[0] == 4 and row[1] == 1


# ---- ready 替换与 processing 保留 ------------------------------------


def test_stale_ready_report_is_replaced(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    stale = directories.ready / f"status-report-9-{_OTHER_SHA}.json"
    stale.write_bytes(b"old")
    result = _deliver(pipeline, report.report_id)
    assert result.outcome is DeliveryOutcome.PUBLISHED
    assert not stale.exists()
    assert (directories.ready
            / f"status-report-{report.report_id}-{_SHA}.json").exists()


def test_processing_report_is_kept_alongside_new_ready(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    claimed = directories.processing / f"status-report-9-{_OTHER_SHA}.json"
    claimed.write_bytes(b"claimed")
    result = _deliver(pipeline, report.report_id)
    assert result.outcome is DeliveryOutcome.PUBLISHED
    assert claimed.read_bytes() == b"claimed"
    assert (directories.ready
            / f"status-report-{report.report_id}-{_SHA}.json").exists()


def test_unparseable_ready_files_are_not_touched(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    stranger = directories.ready / "notes.txt"
    stranger.write_bytes(b"keep me")
    result = _deliver(pipeline, report.report_id)
    assert result.outcome is DeliveryOutcome.PUBLISHED
    assert stranger.read_bytes() == b"keep me"


# ---- 恢复观察 --------------------------------------------------------


def test_observe_locations_parses_real_directories(pipeline):
    _, directories, _ = pipeline
    (directories.ready / f"status-report-5-{_SHA}.json").write_bytes(_PAYLOAD)
    (directories.processing / f"status-report-6-{_OTHER_SHA}.json").write_bytes(b"x")
    (directories.ready / "unrelated.json").write_bytes(b"{}")
    (directories.staging / "reports").mkdir()
    (directories.staging / "reports"
     / f"status-report-7-{_SHA}.json").write_bytes(b"y")
    locations = observe_report_locations(directories)
    assert locations.ready.error is None
    assert set(locations.ready.files) == {(5, _SHA)}
    assert set(locations.processing.files) == {(6, _OTHER_SHA)}
    assert set(locations.staging.files) == {(7, _SHA)}


def test_observe_missing_directory_is_reported_as_error(pipeline):
    _, directories, _ = pipeline
    directories.processing.rmdir()
    locations = observe_report_locations(directories)
    assert locations.processing.error is not None
    report = FrozenReport(5, None, 0, 30, 1)
    assert recover_report_files(
        report, locations) is ReportFileDecision.UNRELIABLE


def test_recover_decision_from_real_observation(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    (directories.ready
     / f"status-report-{report.report_id}-{_SHA}.json").write_bytes(_PAYLOAD)
    assert recover_report_files(
        report, observe_report_locations(directories)
    ) is ReportFileDecision.PRESENT_ELSEWHERE


# ---- 恢复发现补齐发布事实 ---------------------------------------------


def test_recovery_saves_publication_for_prepared_report(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    _advance_to(pipeline, report, 2)
    before = _last_event_id(owned)
    (directories.ready
     / f"status-report-{report.report_id}-{_SHA}.json").write_bytes(_PAYLOAD)
    outcome = record_recovered_publication(
        new_operation_key(), _owned(owned), report.report_id,
        observed_sha256=_SHA)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    row = _value(owned,
        "SELECT status, publication_count, last_published_event_id"
        " FROM reports WHERE id = ?", report.report_id)
    assert row[0] == 4 and row[1] == 1 and row[2] is not None
    assert _new_events(owned, before) == [(28, 3), (28, 4)]
    # 已发布过的报告不追加事实。
    again = record_recovered_publication(
        new_operation_key(), _owned(owned), report.report_id,
        observed_sha256=_SHA)
    assert again.kind is DbOutcomeKind.COMPLETED
    assert _value(owned,
        "SELECT publication_count FROM reports WHERE id = ?",
        report.report_id)[0] == 1
    assert _new_events(owned, before) == [(28, 3), (28, 4)]


def test_recovery_saves_publication_for_publishing_report(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    _advance_to(pipeline, report, 3)
    before = _last_event_id(owned)
    (directories.ready
     / f"status-report-{report.report_id}-{_SHA}.json").write_bytes(_PAYLOAD)
    outcome = record_recovered_publication(
        new_operation_key(), _owned(owned), report.report_id,
        observed_sha256=_SHA)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert _value(owned, "SELECT status FROM reports WHERE id = ?",
                  report.report_id)[0] == 4
    assert _new_events(owned, before) == [(28, 4)]


def test_recovery_restores_failed_report_then_publishes(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    _advance_to(pipeline, report, 5)
    before = _last_event_id(owned)
    (directories.ready
     / f"status-report-{report.report_id}-{_SHA}.json").write_bytes(_PAYLOAD)
    outcome = record_recovered_publication(
        new_operation_key(), _owned(owned), report.report_id,
        observed_sha256=_SHA)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    row = _value(owned,
        "SELECT status, publication_count, last_error_json"
        " FROM reports WHERE id = ?", report.report_id)
    assert row[0] == 4 and row[1] == 1 and row[2] is None
    assert _new_events(owned, before) == [(28, 6), (28, 4)]


def test_recovery_rejects_digest_mismatch(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    _advance_to(pipeline, report, 2)
    before = _last_event_id(owned)
    (directories.ready
     / f"status-report-{report.report_id}-{_SHA}.json").write_bytes(_PAYLOAD)
    outcome = record_recovered_publication(
        new_operation_key(), _owned(owned), report.report_id,
        observed_sha256=_OTHER_SHA)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert _new_events(owned, before) == []


def test_recovery_rejects_report_without_determined_bytes(pipeline):
    owned, _, _ = pipeline
    report = _freeze_report(pipeline)
    outcome = record_recovered_publication(
        new_operation_key(), _owned(owned), report.report_id,
        observed_sha256=_SHA)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


# ---- staging 残留重建与同报告补投 -------------------------------------


def test_staging_residue_is_regenerated_and_published(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    residue_dir = directories.staging / "reports"
    residue_dir.mkdir()
    (residue_dir / f"status-report-{report.report_id}-{_OTHER_SHA}.json"
     ).write_bytes(b"partial")
    assert recover_report_files(
        report, observe_report_locations(directories)
    ) is ReportFileDecision.STAGING_RESIDUE
    result = _deliver(pipeline, report.report_id)
    assert result.outcome is DeliveryOutcome.PUBLISHED
    ready_file = directories.ready / f"status-report-{report.report_id}-{_SHA}.json"
    assert ready_file.read_bytes() == _PAYLOAD
    assert not list(residue_dir.iterdir())


def test_republish_reuses_identity_and_settles_sync(pipeline):
    owned, directories, _ = pipeline
    report = _freeze_report(pipeline)
    _advance_to(pipeline, report, 4)
    _seed_sync_action(pipeline)
    before = _last_event_id(owned)
    decision = decide_republish(
        True, _covering(report), observe_report_locations(directories))
    assert decision is RepublishDecision.REPUBLISH_EXISTING
    result = _deliver(pipeline, report.report_id, local_actions=(40,))
    assert result.outcome is DeliveryOutcome.PUBLISHED
    ready_file = directories.ready / f"status-report-{report.report_id}-{_SHA}.json"
    assert ready_file.read_bytes() == _PAYLOAD
    row = _value(owned,
        "SELECT status, publication_count FROM reports WHERE id = ?",
        report.report_id)
    assert row[0] == 4 and row[1] == 2
    assert _new_events(owned, before) == [
        (28, 3), (28, 4), (29, 2), (8, 1)]
    sync = _value(owned,
        "SELECT local_report_id FROM state_syncs WHERE id = 1")
    assert sync[0] == report.report_id
    assert _value(owned, "SELECT status FROM actions WHERE id = 40")[0] == 3


def test_republish_not_needed_without_responsibility(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    decision = decide_republish(
        False, _covering(report), observe_report_locations(directories))
    assert decision is RepublishDecision.NOT_NEEDED


def test_existing_delivered_report_is_kept(pipeline):
    _, directories, _ = pipeline
    report = _freeze_report(pipeline)
    (directories.processing
     / f"status-report-{report.report_id}-{_SHA}.json").write_bytes(_PAYLOAD)
    decision = decide_republish(
        True, _covering(report), observe_report_locations(directories))
    assert decision is RepublishDecision.KEEP_EXISTING


# ---- 发布后同步本地完成共同保存 ---------------------------------------


def test_publication_settles_local_report_and_action_success(pipeline):
    owned, _, _ = pipeline
    report = _freeze_report(pipeline)
    _seed_sync_action(pipeline)
    before = _last_event_id(owned)
    result = _deliver(pipeline, report.report_id, local_actions=(40,))
    assert result.outcome is DeliveryOutcome.PUBLISHED
    assert result.error is None
    sync = _value(owned,
        "SELECT local_report_id, status FROM state_syncs WHERE id = 1")
    assert sync == (report.report_id, 1)
    assert _value(owned, "SELECT status FROM actions WHERE id = 40")[0] == 3
    assert _new_events(owned, before) == [
        (28, 2), (28, 3), (28, 4), (29, 2), (8, 1)]
