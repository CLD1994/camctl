"""设备文件观察登记的组件集成测试。

真实 SQLite 与事务内核组合：驱动结果列举的稳定文件身份按
[设备, 驱动, 文件身份] 确定编码落为 device_files 行；重复发现复用
原行并核对原绑定与定位；归属只能从未知一次确认，预览配对两端属
于同一来源任务；完成事实按登记的状态转换保存，等待与产物契约依
据引用已保存的等待完成。守卫对五个分支按事件前事实核对。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.capture.files import (
    FileCompletionSave,
    FileObservationSave,
    OwnershipSave,
    file_identity_key,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.contracts.history_values import TransactionRange
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import (
    RowChange,
    RowImage,
    event_envelope,
    row_facts,
    update_change,
)

from ..persistence.test_runtime import _create_valid_database

register_operation_guards()
register_capture_guards()

_NOW = 1_750_000_000_000_000


def _environment(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)",
        (_NOW,),
    )
    _seed_action(connection, 11, action_type=2)
    _seed_action(connection, 12, action_type=1)
    connection.commit()
    return owned


def _seed_action(connection, action_id: int, *, action_type: int) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, ?, 'cam-1', ?, NULL, '{}', '{}', 'camctl-adb',"
        " 1000, '{}', 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, action_id - 11, f"act-{action_id}", action_type, _NOW),
    )


def _observation(action_id: int = 11, identity: str = "task-a/original.mp4",
                 locator=None) -> FileObservationSave:
    return FileObservationSave(
        observer_action_id=action_id,
        file_identity=identity,
        locator={"path": "/DCIM/original.mp4"} if locator is None else locator,
        occurred_at=_NOW,
        original_name="original.mp4",
        media_type="video/mp4",
    )


def _row(owned, file_id: int):
    row = owned.connection.execute(
        "SELECT * FROM device_files WHERE id = ?", (file_id,)
    ).fetchone()
    assert row is not None
    columns = [description[0] for description in owned.connection.execute(
        "SELECT * FROM device_files WHERE id = ?", (file_id,)).description]
    return dict(zip(columns, row))


def _created_file(owned, repository=None, observation=None) -> int:
    repository = repository if repository is not None else CaptureRepository()
    outcome = repository.save_file_observation(
        observation if observation is not None else _observation(),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value.file_id


class TestCreateObservation:
    def test_registers_new_file_with_initial_unknown_facts(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            outcome = CaptureRepository().save_file_observation(
                _observation(), new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            file_id = outcome.value.file_id
            assert outcome.value.created is True
            row = _row(owned, file_id)
            assert row["observer_action_id"] == 11
            assert row["identity_key"] == file_identity_key(
                "cam-1", "camctl-adb", "task-a/original.mp4")
            assert json.loads(row["locator_json"]) == {"path": "/DCIM/original.mp4"}
            assert row["original_name"] == "original.mp4"
            assert row["media_type"] == "video/mp4"
            assert (row["source_action_id"], row["ownership_evidence_json"],
                    row["role"], row["original_device_file_id"],
                    row["pairing_evidence_json"], row["presence_state"],
                    row["completion_state"], row["completion_evidence_json"],
                    row["size_bytes"], row["checksum_support"], row["sha256"],
                    row["last_error_json"]) == (None, None, 1, None, None, 1, 1,
                                                None, None, 1, None, None)
            assert row["change_count"] == 1
            event = owned.connection.execute(
                "SELECT event_type, body_json FROM history_events WHERE id = 2"
            ).fetchone()
            assert event[0] == 17
            assert json.loads(event[1])["reason"] == 1
        finally:
            owned.connection.close()

    def test_repeated_discovery_reuses_row_and_keeps_first_observer(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            again = repository.save_file_observation(
                _observation(), new_operation_key(), owned)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            assert (again.value.file_id, again.value.created) == (file_id, False)
            other_observer = repository.save_file_observation(
                _observation(action_id=12), new_operation_key(), owned)
            assert (other_observer.value.file_id, other_observer.value.created) == (
                file_id, False)
            assert _row(owned, file_id)["observer_action_id"] == 11
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE event_type = 17"
            ).fetchone()[0] == 1
        finally:
            owned.connection.close()

    def test_conflicting_locator_on_rediscovery_preserves_original(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            before = tuple(owned.connection.iterdump())
            outcome = repository.save_file_observation(
                _observation(locator={"path": "/DCIM/other.mp4"}),
                new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome.error
            assert isinstance(outcome.error, ConsistencyError)
            assert tuple(owned.connection.iterdump()) == before
            assert _row(owned, file_id)["observer_action_id"] == 11
        finally:
            owned.connection.close()

    def test_original_key_resend_restores_first_response(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            key = new_operation_key()
            first = repository.save_file_observation(_observation(), key, owned)
            assert first.value.created is True
            resend = repository.save_file_observation(_observation(), key, owned)
            assert resend.kind is DbOutcomeKind.COMPLETED, resend.error
            assert (resend.value.file_id, resend.value.created) == (
                first.value.file_id, True)
            conflict = repository.save_file_observation(
                _observation(identity="task-a/other.mp4"), key, owned)
            assert conflict.kind is DbOutcomeKind.ROLLED_BACK, conflict.error
            late = repository.save_file_observation(
                FileObservationSave(
                    observer_action_id=11, file_identity="task-a/original.mp4",
                    locator={"path": "/DCIM/original.mp4"},
                    occurred_at=_NOW + 1_000_000), key, owned)
            assert late.kind is DbOutcomeKind.ROLLED_BACK, late.error
        finally:
            owned.connection.close()

    def test_rejects_observer_without_capture_binding(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            # 拍摄类型的设备与驱动绑定由表约束物理保证；这里构造
            # 非拍摄类型与观察动作缺失两个可达分区。
            owned.connection.execute(
                "UPDATE actions SET type = 4, execution_spec_json = NULL,"
                " effective_params_json = NULL, driver_id = NULL, device_id = NULL,"
                " max_delay_ms = NULL, status = 4, execution_started = 0,"
                " error_code = 1, error_details_json = '{}' WHERE id = 11")
            owned.connection.commit()
            repository = CaptureRepository()
            outcome = repository.save_file_observation(
                _observation(action_id=11), new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome.error
            assert isinstance(outcome.error, ConsistencyError)
            missing = repository.save_file_observation(
                _observation(action_id=99), new_operation_key(), owned)
            assert missing.kind is DbOutcomeKind.ROLLED_BACK, missing.error
            assert isinstance(missing.error, ConsistencyError)
        finally:
            owned.connection.close()

    def test_write_failure_rolls_back_and_retry_succeeds(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            from dataclasses import replace
            from ..operations.test_result_reuse import _FaultConnection
            repository = CaptureRepository()
            key = new_operation_key()
            failing = replace(
                owned, connection=_FaultConnection(owned.connection, "INSERT INTO device_files"))
            outcome = repository.save_file_observation(_observation(), key, failing)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome.error
            retry = repository.save_file_observation(_observation(), key, owned)
            assert retry.kind is DbOutcomeKind.COMPLETED, retry.error
        finally:
            owned.connection.close()


class TestOwnershipConfirmation:
    def test_confirms_source_once_with_task_scope_evidence(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            outcome = repository.save_file_ownership(
                OwnershipSave(
                    file_id=file_id, source_action_id=11, method=1, role=2,
                    observation={"task": "a"}, occurred_at=_NOW,
                ), new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            row = _row(owned, file_id)
            assert row["source_action_id"] == 11
            assert json.loads(row["ownership_evidence_json"]) == {
                "method": 1, "observation": {"task": "a"}}
            assert row["role"] == 2
            bodies = owned.connection.execute(
                "SELECT body_json FROM history_events WHERE event_type = 17"
            ).fetchall()
            assert any(json.loads(body[0])["reason"] == 2 for body in bodies)
        finally:
            owned.connection.close()

    def test_confirmed_source_never_changes_or_reconfirms(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            assert repository.save_file_ownership(
                OwnershipSave(file_id=file_id, source_action_id=11, method=1,
                              role=2, observation={"t": 1}, occurred_at=_NOW),
                new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
            for command in (
                OwnershipSave(file_id=file_id, source_action_id=12, method=1,
                              role=2, observation={"t": 1}, occurred_at=_NOW),
                OwnershipSave(file_id=file_id, source_action_id=11, method=3,
                              role=3, observation={"t": 1}, occurred_at=_NOW,
                              paired_device_file_id=file_id,
                              pairing_observation={"p": 1}),
            ):
                outcome = repository.save_file_ownership(
                    command, new_operation_key(), owned)
                assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome.error
            assert _row(owned, file_id)["source_action_id"] == 11
        finally:
            owned.connection.close()

    def test_preview_pairing_requires_confirmed_original_of_same_task(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            original_id = _created_file(owned, repository, _observation())
            preview_id = _created_file(owned, repository, _observation(
                identity="task-a/preview.mp4", locator={"path": "/DCIM/preview.mp4"}))
            before = tuple(owned.connection.iterdump())
            unconfirmed = repository.save_file_ownership(
                OwnershipSave(file_id=preview_id, source_action_id=11, method=1,
                              role=3, observation={"t": 1}, occurred_at=_NOW,
                              paired_device_file_id=original_id,
                              pairing_observation={"p": 1}),
                new_operation_key(), owned)
            assert unconfirmed.kind is DbOutcomeKind.ROLLED_BACK, unconfirmed.error
            assert isinstance(unconfirmed.error, ConsistencyError)
            assert tuple(owned.connection.iterdump()) == before
            assert repository.save_file_ownership(
                OwnershipSave(file_id=original_id, source_action_id=11, method=1,
                              role=2, observation={"t": 1}, occurred_at=_NOW),
                new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
            paired = repository.save_file_ownership(
                OwnershipSave(file_id=preview_id, source_action_id=11, method=1,
                              role=3, observation={"t": 1}, occurred_at=_NOW,
                              paired_device_file_id=original_id,
                              pairing_observation={"p": 1}),
                new_operation_key(), owned)
            assert paired.kind is DbOutcomeKind.COMPLETED, paired.error
            row = _row(owned, preview_id)
            assert row["original_device_file_id"] == original_id
            assert json.loads(row["pairing_evidence_json"]) == {
                "method": 1, "observation": {"p": 1}}
        finally:
            owned.connection.close()

    def test_original_key_resend_restores_ownership_response(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            key = new_operation_key()
            first = repository.save_file_ownership(
                OwnershipSave(file_id=file_id, source_action_id=11, method=1,
                              role=2, observation={"t": 1}, occurred_at=_NOW),
                key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED, first.error
            resend = repository.save_file_ownership(
                OwnershipSave(file_id=file_id, source_action_id=11, method=1,
                              role=2, observation={"t": 1}, occurred_at=_NOW),
                key, owned)
            assert resend.kind is DbOutcomeKind.COMPLETED, resend.error
            other = repository.save_file_ownership(
                OwnershipSave(file_id=file_id, source_action_id=11, method=2,
                              role=2, observation={"t": 1}, occurred_at=_NOW,
                              activity_id=11),
                key, owned)
            assert other.kind is DbOutcomeKind.ROLLED_BACK, other.error
        finally:
            owned.connection.close()


class TestCompletionFacts:
    def test_writing_to_complete_with_unchanged_evidence_keeps_original_key_facts(self, tmp_path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            writing = FileCompletionSave(file_id, 2, _NOW, basis=1, observation={"device_contract": "files"})
            writing_key = new_operation_key()
            first = repository.save_file_completion(writing, writing_key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED, first.error
            complete = replace(writing, state=3, size_bytes=8)
            complete_key = new_operation_key()
            saved = repository.save_file_completion(complete, complete_key, owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            body = json.loads(owned.connection.execute(
                "SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1").fetchone()[0])
            assert "completion_evidence_json" not in body["rows"][0]["after"]["values"]
            count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
            for command, key in ((complete, complete_key), (writing, writing_key)):
                reused = repository.save_file_completion(command, key, owned)
                assert reused.kind is DbOutcomeKind.COMPLETED, reused.error
                changed = repository.save_file_completion(
                    replace(command, observation={"device_contract": "changed"}), key, owned)
                assert changed.kind is DbOutcomeKind.ROLLED_BACK, changed.error
            assert _row(owned, file_id)["completion_state"] == 3
            assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == count
        finally:
            owned.connection.close()

    @pytest.mark.parametrize("state,values", [
        (2, {}), (3, {"basis": 1, "observation": {"stopped": True}, "size_bytes": 8}),
        (4, {"error": {"reason": "not_confirmed"}}),
    ])
    @pytest.mark.parametrize("provide_metadata", [False, True])
    @pytest.mark.parametrize("field", ["locator", "original_name", "media_type"])
    def test_original_key_preserves_complete_optional_input_in_every_state(
            self, tmp_path, state, values, provide_metadata, field):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            metadata = {"locator": {"path": "/DCIM/original.mp4"},
                        "original_name": "original.mp4", "media_type": "video/mp4"}
            original_input = metadata if provide_metadata else dict.fromkeys(metadata)
            command = FileCompletionSave(file_id, state, _NOW, **values, **original_input)
            key = new_operation_key()
            first = repository.save_file_completion(command, key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED, first.error
            body = json.loads(owned.connection.execute(
                "SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1").fetchone()[0])
            assert body["evidence"] == {"completion_request": original_input}
            assert not {"locator_json", "original_name", "media_type"} & body["rows"][0]["after"]["values"].keys()
            again = repository.save_file_completion(command, key, owned)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            history_before = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
            changed = replace(command, **{field: None if provide_metadata else metadata[field]})
            receipt = repository.save_file_completion(changed, key, owned)
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
            assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == history_before
        finally:
            owned.connection.close()

    def test_device_guarantee_completion_saves_size_and_evidence(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            outcome = repository.save_file_completion(
                FileCompletionSave(
                    file_id=file_id, state=3, occurred_at=_NOW, basis=1,
                    observation={"stopped": True}, size_bytes=4096,
                ), new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            row = _row(owned, file_id)
            assert row["completion_state"] == 3
            assert row["size_bytes"] == 4096
            assert json.loads(row["completion_evidence_json"]) == {
                "basis": 1, "observation": {"stopped": True}}
        finally:
            owned.connection.close()

    def test_time_and_outputs_references_saved_wait_completion(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            owned.connection.execute(
                "INSERT INTO device_activities (id, action_id, task_key,"
                " state_query_supported, stop_supported, safe_repeat_stop,"
                " start_return_meaning, completion_mode, ownership_mode,"
                " output_scope_json, baseline_state, dispatch_state,"
                " activity_state, occupancy_state, result_set_state,"
                " wait_completed_event_id)"
                " VALUES (11, 11, ?, 1, 1, 1, 1, 2, 1, '{}', 1, 2, 2, 1, 1, 1)",
                ("a" * 32,),
            )
            owned.connection.commit()
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            good = repository.save_file_completion(
                FileCompletionSave(
                    file_id=file_id, state=3, occurred_at=_NOW, basis=2,
                    observation={"waited": True}, size_bytes=10,
                    activity_id=11, wait_completed_event_id=1,
                ), new_operation_key(), owned)
            assert good.kind is DbOutcomeKind.COMPLETED, good.error
            other_id = _created_file(owned, repository, _observation(
                identity="task-a/second.mp4", locator={"path": "/DCIM/second.mp4"}))
            wrong_reference = repository.save_file_completion(
                FileCompletionSave(
                    file_id=other_id, state=3, occurred_at=_NOW, basis=2,
                    observation={"waited": True}, size_bytes=11,
                    activity_id=11, wait_completed_event_id=2,
                ), new_operation_key(), owned)
            assert wrong_reference.kind is DbOutcomeKind.ROLLED_BACK, wrong_reference.error
            assert isinstance(wrong_reference.error, ConsistencyError)
            no_activity = repository.save_file_completion(
                FileCompletionSave(
                    file_id=other_id, state=3, occurred_at=_NOW, basis=2,
                    observation={"waited": True}, size_bytes=11,
                    activity_id=99, wait_completed_event_id=1,
                ), new_operation_key(), owned)
            assert no_activity.kind is DbOutcomeKind.ROLLED_BACK, no_activity.error
        finally:
            owned.connection.close()

    @pytest.mark.parametrize("first_state,command_kwargs,accepted", [
        (1, {"state": 2, "basis": 1, "observation": {"g": 1}}, True),
        (1, {"state": 4, "error": {"r": 1}}, True),
        (2, {"state": 3, "basis": 1, "observation": {"o": 1}, "size_bytes": 8}, True),
        (4, {"state": 3, "basis": 1, "observation": {"o": 1}, "size_bytes": 8}, True),
        (3, {"state": 2}, False),
        (3, {"state": 4, "error": {"r": 1}}, False),
        (2, {"state": 4, "error": {"r": 1}}, True),
    ])
    def test_transition_rules(self, tmp_path: Path, first_state, command_kwargs, accepted):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            if first_state != 1:
                first_kwargs = {
                    2: {"state": 2, "basis": 1, "observation": {"g": 1}},
                    3: {"state": 3, "basis": 1, "observation": {"o": 1}, "size_bytes": 8},
                    4: {"state": 4, "error": {"r": 1}},
                }[first_state]
                assert repository.save_file_completion(
                    FileCompletionSave(file_id=file_id, occurred_at=_NOW, **first_kwargs),
                    new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
            outcome = repository.save_file_completion(
                FileCompletionSave(file_id=file_id, occurred_at=_NOW, **command_kwargs),
                new_operation_key(), owned)
            assert (outcome.kind is DbOutcomeKind.COMPLETED) is accepted, outcome.error
        finally:
            owned.connection.close()

    def test_original_key_resend_restores_completion_response(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            key = new_operation_key()
            command = FileCompletionSave(
                file_id=file_id, state=3, occurred_at=_NOW, basis=1,
                observation={"o": 1}, size_bytes=8)
            assert repository.save_file_completion(
                command, key, owned).kind is DbOutcomeKind.COMPLETED
            assert repository.save_file_completion(
                command, key, owned).kind is DbOutcomeKind.COMPLETED
            changed = repository.save_file_completion(
                FileCompletionSave(
                    file_id=file_id, state=3, occurred_at=_NOW, basis=1,
                    observation={"o": 1}, size_bytes=9),
                key, owned)
            assert changed.kind is DbOutcomeKind.ROLLED_BACK, changed.error
        finally:
            owned.connection.close()


_DEVICE_FILE_EVENT = 17


def _guard_context(owned, rows, reason: int):
    from camctl.persistence.transaction import row_facts
    event = event_envelope(2, 2, _DEVICE_FILE_EVENT, reason, rows, _NOW)
    state = {"actions": {11: row_facts(owned.connection, "actions", 11)}}
    return event, state


class TestDeviceFileGuardBranches:
    """无生产命令的 CHECKSUM 与 PRESENCE 分支由直接事件核对。"""

    def _context(self, owned, file_id, facts, *, event_id: int = 2):
        return EventContext(
            TransactionRange(event_id, event_id, event_id),
            {("device_files", file_id): ("device_file", file_id)},
            {"actions": {11: row_facts(owned.connection, "actions", 11)},
             "device_files": {file_id: facts}},
        )

    def test_checksum_digest_requires_completed_file_and_support_is_final(
            self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            facts = row_facts(owned.connection, "device_files", file_id)
            context = self._context(owned, file_id, facts)
            # 文件尚未完成：摘要与支持声明共同保存被拒绝。
            with pytest.raises(EventValidationError):
                validate_event(event_envelope(2, 2, _DEVICE_FILE_EVENT, 4, (
                    update_change("device_files", file_id,
                        {"checksum_support": 1, "sha256": None},
                        {"checksum_support": 2, "sha256": "a" * 64}),), _NOW), context)
            # 只声明支持（无摘要）在未完成文件上合法。
            validate_event(event_envelope(3, 3, _DEVICE_FILE_EVENT, 4, (
                update_change("device_files", file_id,
                    {"checksum_support": 1}, {"checksum_support": 2}),), _NOW),
                self._context(owned, file_id, facts, event_id=3))
            facts["checksum_support"] = 2
            # 摘要查询失败不能改成不支持。
            with pytest.raises(EventValidationError):
                validate_event(event_envelope(4, 4, _DEVICE_FILE_EVENT, 4, (
                    update_change("device_files", file_id,
                        {"checksum_support": 2}, {"checksum_support": 3}),), _NOW),
                    self._context(owned, file_id, facts, event_id=4))
        finally:
            owned.connection.close()

    def test_presence_change_kept_separate_from_cleanup_success(self, tmp_path: Path):
        owned = _environment(tmp_path)
        try:
            repository = CaptureRepository()
            file_id = _created_file(owned, repository)
            facts = row_facts(owned.connection, "device_files", file_id)
            context = self._context(owned, file_id, facts)
            validate_event(event_envelope(2, 2, _DEVICE_FILE_EVENT, 5, (
                update_change("device_files", file_id,
                    {"presence_state": 1}, {"presence_state": 3}),), _NOW), context)
            facts["presence_state"] = 3
            validate_event(event_envelope(3, 3, _DEVICE_FILE_EVENT, 5, (
                update_change("device_files", file_id,
                    {"presence_state": 3}, {"presence_state": 2}),), _NOW),
                self._context(owned, file_id, facts, event_id=3))
            # 无实际变化的存在性更新不是合法观察。
            same = RowChange(
                table="device_files", row_id=file_id,
                before=RowImage(exists=True, values={"presence_state": 2}),
                after=RowImage(exists=True, values={"presence_state": 2}),
            )
            with pytest.raises(EventValidationError):
                validate_event(event_envelope(
                    4, 4, _DEVICE_FILE_EVENT, 5, (same,), _NOW),
                    self._context(owned, file_id, facts, event_id=4))
        finally:
            owned.connection.close()
