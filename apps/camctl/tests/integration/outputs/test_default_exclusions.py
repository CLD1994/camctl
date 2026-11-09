"""默认取回按最终产物身份排除，保存与恢复沿用原请求。"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.contracts.schemas import create_validator, validation_errors
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventValidationError
from camctl.outputs.definitions import SelectionMode
from camctl.outputs.sources import SelectionSnapshot, select_outputs
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.file_batches import register_file_batch_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import commit_operation

from .test_catalog_integrity import selection_database
from .test_output_family import family_database
from .test_product_competition import competition_database
from .test_qualification import qualification_environment
from .test_sources import _NOW, _fixed_resolution
from ..support.output_planning import EventSequence, capture_operation
from ..support.output_storage_cases import observe_product_competition, observed_operation
from ..support.outputs import obtain_factory, output_drivers, submit_output_document
from ..support.capture import capture_database
from ..support.capture_media_cases import (
    CONTENT, ScriptTools, media_cases, recording_environment, recording_media,
    recording_runtime,
)
from ..bootstrap.test_output_binding_changes import Catalog
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.devices.read_session import ReadSession
from .test_source_digest_failure_settlement import _ReadStream


def _exclude(owned, identities=(703,), action_id=30):
    params = {"source": {"action_instance_id": "11"}}
    if identities is not None:
        params["exclude_output_ids"] = [str(identity) for identity in identities]
    owned.connection.execute(
        "UPDATE actions SET input_fields_json=?, execution_spec_json=? WHERE id=?",
        (json.dumps({"params": params}), '{"selection_mode":1}', action_id),
    )
    owned.connection.commit()


@pytest.mark.parametrize("extra,valid", [
    ({"exclude_output_ids": ["1"]}, True),
    ({"filter": "default", "exclude_output_ids": ["1"]}, True),
    ({"exclude_output_ids": []}, False),
    ({"exclude_output_ids": ["1", "1"]}, False),
    ({"exclude_output_ids": [1]}, False),
    ({"exclude_output_ids": ["01"]}, False),
    ({"exclude_output_ids": ["9223372036854775808"]}, False),
    ({"filter": "preview", "exclude_output_ids": ["1"]}, False),
    ({"output_ids": ["2"], "exclude_output_ids": ["1"]}, False),
    ({"filter": "preview", "purpose": "auto_preview", "exclude_output_ids": ["1"]}, False),
])
def test_public_default_exclusion_combinations(extra, valid):
    validator = create_validator({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                  "$ref": "plan.schema.json#/$defs/obtain_params"})
    params = {"source": {"action_name": "拍摄"}, **extra}
    assert (not validation_errors(validator, params)) is valid


@pytest.mark.parametrize("excluded,remaining", [
    ((703,), (705,)),
    ((701, 702, 999), (703, 705)),
    ((703, 705), ()),
])
def test_default_selection_filters_final_repaired_identity(selection_database, excluded, remaining):
    owned = selection_database
    _exclude(owned, excluded)
    facts = outputs.load_selection_facts(owned.connection, 11)
    snapshot = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT,
                              excluded_output_ids=frozenset(excluded))
    assert snapshot.selected_output_ids == remaining
    assert snapshot.source_error_code is None
    receipt = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.snapshot.selected_output_ids == remaining


def test_selection_guard_rechecks_original_exclusions(selection_database):
    owned = selection_database
    _exclude(owned)
    facts = outputs.load_selection_facts(owned.connection, 11)
    snapshot = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT,
                              excluded_output_ids=frozenset({703}))
    assert snapshot.selected_output_ids == (705,)
    proposal = capture_operation(owned, lambda actual: outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), actual))

    def legal_control(actual):
        receipt = commit_operation(
            EventSequence(proposal.events, proposal), new_operation_key(), actual)
        assert receipt.kind == "completed", receipt.error
        assert actual.connection.execute(
            "SELECT output_id FROM obtain_items WHERE selection_id=61 ORDER BY id"
        ).fetchall() == [(705,)]
        return receipt

    # 同一原请求和选择在独立完整副本中实际提交，证明对照前提成立。
    control = observed_operation(owned, legal_control)
    assert control.kind == "completed", control.error

    _exclude(owned, (705,))
    changed_facts = outputs.load_selection_facts(owned.connection, 11)
    changed_snapshot = select_outputs(
        _fixed_resolution((11,)), changed_facts, SelectionMode.DEFAULT,
        excluded_output_ids=frozenset({705}))
    assert changed_snapshot.selected_output_ids == (703,)
    changed = capture_operation(owned, lambda actual: outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, changed_snapshot, _NOW), new_operation_key(), actual))
    before = tuple(owned.connection.iterdump())
    rejected = commit_operation(
        EventSequence(proposal.events, changed), new_operation_key(), owned)
    assert rejected.kind == "rolled_back", rejected.error
    assert isinstance(rejected.error, EventValidationError), rejected.error
    assert str(rejected.error) == "选择条目的完整集合、保存顺序或依据与原请求及当前事实不一致"
    assert not owned.connection.in_transaction
    assert tuple(owned.connection.iterdump()) == before


def test_excluded_selection_cannot_restore_excluded_target(selection_database):
    owned = selection_database
    _exclude(owned)
    before = tuple(owned.connection.iterdump())
    receipt = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, select_outputs(_fixed_resolution((11,)), outputs.load_selection_facts(owned.connection, 11), SelectionMode.DEFAULT), _NOW), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


def test_all_excluded_finishes_without_delivery_and_recovers(selection_database):
    owned = selection_database
    _exclude(owned, (703, 705))
    repository = outputs.OutputsRepository()
    request = outputs.FixSelection(61, SelectionSnapshot(True), _NOW)
    key = new_operation_key()
    fixed = repository.fix_selection(request, key, owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    owned.connection.close()
    reopened = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        for operation_key in (key, new_operation_key()):
            again = repository.fix_selection(request, operation_key, reopened)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            assert again.value.snapshot.items == ()
        finish_key = new_operation_key()
        for operation_key in (finish_key, finish_key, new_operation_key()):
            receipt = repository.finish_obtain(outputs.FinishObtain(30, _NOW + 1), operation_key, reopened)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            assert (receipt.value.action_status, receipt.value.prepared, receipt.value.failed) == (3, 0, 0)
            assert receipt.value.plan_status == 3
        assert reopened.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (0,)
    finally:
        reopened.connection.close()


@pytest.mark.parametrize("invalid", ["no_exclusion", "no_sources", "pending_source", "recovered_no_exclusion"])
def test_empty_selection_needs_fixed_exclusion_proof(selection_database, invalid):
    owned = selection_database
    _exclude(owned, None if invalid in ("no_exclusion", "recovered_no_exclusion") else (703, 705))
    owned.connection.execute("UPDATE obtain_source_selections SET status=? WHERE id=61",
                             (1 if invalid == "pending_source" else 2,))
    if invalid == "no_sources":
        owned.connection.execute("DELETE FROM obtain_source_selections WHERE id=61")
        owned.connection.execute("DELETE FROM action_dependencies WHERE id=41")
    if invalid == "recovered_no_exclusion":
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=30")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    receipt = outputs.OutputsRepository().finish_obtain(
        outputs.FinishObtain(30, _NOW + 1), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.asyncio
async def test_obtain_runtime_consumes_saved_default_exclusions(tmp_path, media_cases):
    from camctl.bootstrap.obtain_assembly import obtain_flow
    from camctl.capture.handlers import capture_handler
    from camctl.capture.result_inputs import ObservedFile
    from camctl.capture.results import FileKind
    from camctl.host_files.media import MediaProbe, MediaStream
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor

    register_file_batch_guards()
    owned, staging = await recording_environment(
        tmp_path, check_decision=3,
        check_basis='{"reason": 2, "target_duration_ms": 60000}')
    photo_bytes = b"independent photo bytes"
    entries = (
        ObservedFile(identity="recording.mp4", locator={"path": "/DCIM/recording.mp4"},
                     evidence={"listing": "recording"}, complete=True,
                     size_bytes=len(CONTENT), kind=FileKind.VIDEO,
                     original_name="recording.mp4", media_type="video/mp4", format_id="mp4"),
        ObservedFile(identity="photo.jpg", locator={"path": "/DCIM/photo.jpg"},
                     evidence={"listing": "photo"}, complete=True,
                     size_bytes=len(photo_bytes), kind=FileKind.PHOTO,
                     original_name="photo.jpg", media_type="image/jpeg", format_id="jpeg"),
    )
    tools = ScriptTools(MediaProbe(Decimal("200"), None, (
        MediaStream(0, "video", "h264", 64, 64, Decimal("200"), False),)))

    class Source:
        def __init__(self):
            self.calls = []

        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.calls.append(source)
            return ReadSession(source, offset, _ReadStream(photo_bytes[offset:]), idle_timeout_s)

    driver = Source()
    supervisor = Supervisor()
    try:
        media = await recording_media(owned, tools, staging)
        capture = await recording_runtime(owned, {1: entries}, flow=media)
        await capture_handler("camera_record")(1, capture)
        assert tools.calls == ["probe", "repair"]
        rows = owned.connection.execute(
            "SELECT o.id,o.kind,d.original_name,o.intermediate_file_id"
            " FROM outputs o LEFT JOIN device_files d ON d.id=o.device_file_id"
            " WHERE o.source_action_id=1 ORDER BY o.id").fetchall()
        video_id, = [row[0] for row in rows if row[1:3] == (1, "recording.mp4")]
        photo_id, = [row[0] for row in rows if row[1:3] == (1, "photo.jpg")]
        (repaired_id, repaired_file), = [(row[0], row[3]) for row in rows if row[1] == 2]
        assert owned.connection.execute(
            "SELECT original_output_id FROM output_origins WHERE output_id=?",
            (repaired_id,)).fetchone() == (video_id,)
        repair_path, = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id=?", (repaired_file,)).fetchone()
        assert (staging / repair_path).read_bytes() == tools.content
        selected = select_outputs(_fixed_resolution((1,)), outputs.load_selection_facts(
            owned.connection, 1), SelectionMode.DEFAULT)
        assert set(selected.selected_output_ids) == {repaired_id, photo_id}

        database = await capture_database(owned)
        now = _NOW + 100_000_000
        instant = datetime.fromtimestamp(now / 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        accepted = await submit_output_document(database, catalog=Catalog(), occurred_at=now, body={
            "request_id": "2", "created_at": instant, "name": "排除修复成品",
            "actions": [{"name": "取回", "type": "obtain_action_outputs", "scheduled_at": instant,
                         "params": {"source": {"action_instance_id": "1"},
                                    "exclude_output_ids": [str(repaired_id)]}}]})
        assert accepted.plan_id is not None, accepted
        action_id, fields = owned.connection.execute(
            "SELECT id,input_fields_json FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        assert json.loads(fields)["params"]["exclude_output_ids"] == [str(repaired_id)]
        ready, processing = tmp_path / "ready", tmp_path / "processing"
        for directory in (staging, ready, processing):
            directory.mkdir(parents=True, exist_ok=True)
        factory = obtain_factory(
            devices={"cam-1": {"driver": "camctl-adb"}},
            drivers=output_drivers(driver, EVIDENCE, read=True), staging=staging,
            ready=ready, processing=processing, occurred_at=lambda: now,
            monotonic_ns=lambda: 0, segment_size=len(photo_bytes),
            file_executor=FileTaskExecutor(supervisor))
        context = SimpleNamespace(database=database)
        flow = obtain_flow(factory)
        await flow(context)
        assert owned.connection.execute(
            "SELECT i.output_id FROM obtain_items i"
            " JOIN obtain_source_selections s ON s.id=i.selection_id"
            " JOIN action_dependencies d ON d.id=s.dependency_id"
            " WHERE d.action_id=? ORDER BY i.id", (action_id,)).fetchall() == [(photo_id,)]
        assert owned.connection.execute(
            "SELECT output_id FROM deliveries WHERE action_id=?", (action_id,)).fetchall() == [(photo_id,)]
        assert [json.loads(source.file_id) for source in driver.calls] == [
            ["cam-1", "camctl-adb", "photo.jpg"]]
        relative, = owned.connection.execute(
            "SELECT f.relative_path FROM intermediate_files f"
            " JOIN file_copies c ON c.target_file_id=f.id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=?", (action_id,)).fetchone()
        assert (staging / relative).read_bytes() == photo_bytes
    finally:
        try:
            assert not (await supervisor.drain_required()).failed
        finally:
            owned.connection.close()


def test_locally_qualified_final_target_is_excluded_even_after_host_cleanup(selection_database):
    owned = selection_database
    _exclude(owned, (703, 705))
    owned.connection.execute("UPDATE outputs SET availability=3, cleanup_status=4 WHERE id=703")
    owned.connection.commit()
    receipt = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, SelectionSnapshot(True), _NOW), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.snapshot.items == ()


def test_unfixed_excluded_obtain_does_not_block_product_successor(competition_database):
    owned = competition_database
    _exclude(owned, (701,), action_id=91)
    assert not observe_product_competition(owned, action_id=31, output_id=701, occurred_at=_NOW)
    _exclude(owned, None, action_id=91)
    assert observe_product_competition(owned, action_id=31, output_id=701, occurred_at=_NOW)
