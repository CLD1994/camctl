"""票据、归属、保留成员与配对的小结果只在完整核验成功后复用。"""

from copy import deepcopy
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import OwnershipFromResultPageSave, PairResultFilesSave, file_identity_key
from camctl.capture.result_inputs import page_from_outcome
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.capture.segment_models import FileCompletionState, OwnershipState, SegmentMemberRole
from camctl.capture.segment_storage import SegmentMemberInput
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation
from camctl.devices.directory import DirectoryCursor
from camctl.devices.tasks import CaptureResultPolicy
from camctl.operations.models import AttemptTicket, CallOutcome
from camctl.persistence.read_reuse import clear_read_reuse, read_reuse_scope
from camctl.persistence.repositories import (
    result_file_ownership as ownership, result_file_pairing as pairing,
    result_file_proofs as proofs, result_pages as pages, segment_result_sets as sets,
)


class OtherTicket(AttemptTicket):
    pass


class OtherPage(ResultPageRef):
    pass


class OtherBoundary(HistoryBoundary):
    pass


class OtherOwnership(OwnershipFromResultPageSave):
    pass


@pytest.fixture(params=["ticket", "ownership", "retained", "pairing"])
def entry(request, monkeypatch):
    db = create_autospec(sqlite3.Connection, instance=True)
    db.in_transaction, db.total_changes = True, 0
    ticket = AttemptTicket(1, "result", "3", "results/3", 28)
    ref = ResultPageRef(ticket, 1, 10, None)
    boundary = HistoryBoundary(20, 100)
    state = SimpleNamespace(db=db, kind=request.param, loads=0, failure=None,
                            boundary=boundary, ref=ref, missing=False, fact=None)

    def observed():
        state.loads += 1
        if state.failure is not None:
            raise state.failure

    def patch(module, name, **options):
        port = create_autospec(getattr(module, name), **options)
        monkeypatch.setattr(module, name, port)
        return port

    run = dict(id=28, kind=6, activity_id=3, action_id=7, responsibility_key="results/3")
    attempt = dict(id=100, result_first_page_event_id=10, result_last_page_event_id=10)
    action = dict(id=7, device_id="camera", driver_id="osmo", execution_spec_json={
        "result_policy": CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value})
    activity = dict(id=3, action_id=7)
    identity = "/DCIM/a.osv"
    file = dict(id=30, identity_key=file_identity_key("camera", "osmo", identity), details=[1])
    if state.kind == "ticket":
        rows = dict(operation_runs=run, operation_attempts=attempt, actions=action,
                    device_activities=activity)

        def facts(connection, table, identity):
            if table == "operation_runs":
                observed()
            return rows[table]

        patch(pages, "row_facts", side_effect=facts)
        patch(pages, "enum_for", return_value=SimpleNamespace(CHECK_CAPTURE_RESULTS=6))
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        db.execute.return_value = cursor
        cursor.fetchone.side_effect = lambda: None if state.missing else (100,)
        state.call = lambda **values: pages._load(db, values.get("ticket", ticket))
        state.expected = (run, attempt, action, activity)
        state.mutable = lambda value: value[2]["execution_spec_json"]
        state.variants = [dict(ticket=replace(ticket, **{field: value})) for field, value in (
            ("attempt_id", 2), ("run_id", 29), ("operation", "other"),
            ("target_id", "4"), ("responsibility_key", "other"), ("attempt_id", True))]
        state.variants.append(dict(ticket=OtherTicket(**vars(ticket))))
    elif state.kind == "ownership":
        page = page_from_outcome(ticket, CallOutcome(observations=(DeviceObservation(
            "result_files_listed", 2, dict(activity_id="3", entries=[dict(identity=identity,
                locator={"path": identity}, complete=True, size_bytes=10, kind="video",
                format_id="osv")], cursor=None, next_cursor=None, set_finalized=False,
                completion_evidence=None)),)))
        saved = SavedResultPage(ref, page, 10, ((identity, 30),))

        def at_record(connection, event_id):
            observed()
            return saved, {}, None, TransactionRange(2, 10, 10)

        patch(pages, "_at_record", side_effect=at_record)
        patch(pages, "_load", return_value=(run, attempt, action, activity))
        patch(pages, "committed_page_proof", return_value=(action, activity, attempt, run))
        state.final = patch(ownership, "row_facts", return_value=file)
        original = OwnershipFromResultPageSave(ref, 30, 11)
        state.call = lambda **values: ownership._proof(db, values.get("request", original),
            current_boundary=values.get("current_boundary"))
        state.expected = (7, int(ownership._ROLE.ORIGINAL), file)
        state.mutable = lambda value: value[2]
        state.variants = [dict(request=replace(original, **change)) for change in (
            dict(file_id=31), dict(occurred_at=12), dict(page=replace(ref, page_no=2)),
            dict(page=replace(ref, event_id=11)),
            dict(page=replace(ref, ticket=replace(ticket, run_id=29))))]
        state.variants.extend([
            dict(request=OtherOwnership(ref, 30, 11)),
            dict(request=replace(original, page=OtherPage(**vars(ref)))),
            dict(request=replace(original, page=replace(ref, ticket=OtherTicket(**vars(ticket))))),
            dict(request=replace(original, page=replace(ref, next_cursor=DirectoryCursor(
                DeviceBinding("camera", "osmo"), ("/DCIM",), 0, identity)))),
        ])
        state.variants.append(dict(current_boundary=boundary))
    elif state.kind == "retained":
        def at(connection, event_id):
            observed()
            return SimpleNamespace(ref=ref), {}

        patch(sets, "_at", side_effect=at)
        state.file = dict(original_device_file_id=None)
        patch(proofs, "_file_at", side_effect=lambda *args: state.file)
        state.lookup = patch(sets, "_one", side_effect=lambda *args: state.fact)
        result = SegmentMemberInput(30, 10, 10, SegmentMemberRole.ORIGINAL,
            OwnershipState.OWNED, FileCompletionState.COMPLETE)

        def strict(connection, page, file_id, fixed, *, pairing_fact_id=None, current_boundary=None):
            if pairing_fact_id is not None:
                raise ConsistencyError("原片不能引用预览配对事实")
            return result

        state.final = patch(proofs, "load_strict_result_file_input", side_effect=strict)
        state.call = lambda **values: sets._retained_input(db, values.get("file_id", 30),
            values.get("page_event_id", 10), values.get("boundary", boundary))
        state.expected = result
        state.mutable = None
        state.variants = [dict(file_id=31), dict(file_id=True), dict(page_event_id=11),
            dict(boundary=HistoryBoundary(21, 100)), dict(boundary=HistoryBoundary(20, 101)),
            dict(boundary=OtherBoundary(20, 100))]
    else:
        fact = dict(id=17, preview_file_id=30, created_event_id=50, source_event_id=49, details=[1])

        def facts(connection, table, identity):
            observed()
            return None if state.missing else fact

        patch(pairing, "row_facts", side_effect=facts)
        patch(pairing, "_one", side_effect=lambda connection, sql, parameters:
              ("a" * 32, 50) if "JOIN history_transactions" in sql else ("event",))
        patch(pages, "decode_event_row", return_value=SimpleNamespace())
        original = PairResultFilesSave(30, ref, HistoryBoundary(2, 10), 51)
        patch(pairing, "_request", return_value=original)
        state.final = patch(pairing, "_verify", return_value=17)
        state.call = lambda **values: pairing.verify_pairing_fact(db,
            values.get("fact_id", 17), values.get("preview_file_id", 30),
            values.get("boundary", boundary), current_boundary=values.get("current_boundary"))
        state.expected = fact
        state.mutable = lambda value: value
        state.variants = [dict(fact_id=18), dict(fact_id=True), dict(preview_file_id=31),
            dict(boundary=HistoryBoundary(21, 100)), dict(boundary=HistoryBoundary(20, 101)),
            dict(boundary=OtherBoundary(20, 100)), dict(current_boundary=boundary)]
    return state


def test_same_scoped_input_completes_once(entry):
    with read_reuse_scope(entry.db):
        assert entry.call() == entry.expected
        assert entry.call() == entry.expected
    assert entry.loads == 1


def test_without_scope_repeats_complete_read(entry):
    assert entry.call() == entry.expected
    assert entry.call() == entry.expected
    assert entry.loads == 2


def test_bounded_page_scope_reuses_the_small_result(entry):
    from camctl.persistence.page_read_cache import page_read_scope

    with read_reuse_scope(entry.db), page_read_scope(entry.db, 100):
        assert entry.call() == entry.expected
        assert entry.call() == entry.expected
    assert entry.loads == 1


def test_every_read_condition_keeps_its_own_identity(entry):
    error = ConsistencyError("实际读取拒绝新输入")
    with read_reuse_scope(entry.db):
        entry.call()
        entry.failure = error
        for variant in entry.variants:
            with pytest.raises(ValueError):
                entry.call(**variant)


@pytest.mark.parametrize("reset", ["write", "clear", "scope"])
def test_write_and_new_verification_phase_repeat_complete_read(entry, reset):
    with read_reuse_scope(entry.db):
        entry.call()
        if reset == "write":
            entry.db.total_changes += 1
        elif reset == "clear":
            clear_read_reuse(entry.db)
        if reset != "scope":
            assert entry.call() == entry.expected
    if reset == "scope":
        with read_reuse_scope(entry.db):
            assert entry.call() == entry.expected
    assert entry.loads == 2


def test_original_error_is_not_cached(entry):
    error = sqlite3.OperationalError("读取失败")
    entry.failure = error
    with read_reuse_scope(entry.db):
        with pytest.raises(sqlite3.OperationalError) as caught:
            entry.call()
        assert caught.value is error
        entry.failure = None
        assert entry.call() == entry.expected
        assert entry.call() == entry.expected
    assert entry.loads == 2


@pytest.mark.parametrize("entry", ["ticket", "ownership", "pairing"], indirect=True)
def test_mutable_return_values_are_isolated(entry):
    expected = deepcopy(entry.expected)
    with read_reuse_scope(entry.db):
        first = entry.call()
        entry.mutable(first)["mutation"] = [1]
        second = entry.call()
        assert second == expected
        entry.mutable(second)["mutation"] = [2]
        assert entry.call() == expected
    assert entry.loads == 1


@pytest.mark.parametrize("entry", ["ticket", "pairing"], indirect=True)
def test_missing_required_source_is_not_cached(entry):
    entry.missing = True
    with read_reuse_scope(entry.db):
        with pytest.raises(ConsistencyError):
            entry.call()
        entry.missing = False
        assert entry.call() == entry.expected
    assert entry.loads == 2


@pytest.mark.parametrize("entry", ["ownership", "retained", "pairing"], indirect=True)
def test_failure_after_earlier_proofs_is_not_cached(entry):
    original = entry.final.side_effect
    error = ConsistencyError("完整证明最后阶段失败")
    entry.final.side_effect = error
    with read_reuse_scope(entry.db):
        with pytest.raises(ConsistencyError) as caught:
            entry.call()
        assert caught.value is error
        entry.final.side_effect = original
        assert entry.call() == entry.expected
        assert entry.call() == entry.expected
    assert entry.loads == 2


@pytest.mark.parametrize("entry", ["retained"], indirect=True)
def test_original_file_still_checks_and_rejects_pairing_fact(entry):
    entry.fact = (17,)
    with read_reuse_scope(entry.db):
        for _ in range(2):
            with pytest.raises(ConsistencyError, match="原片"):
                entry.call()
    assert entry.lookup.call_count == 2
    assert entry.final.call_args.kwargs["pairing_fact_id"] == 17


@pytest.mark.parametrize("entry", ["retained"], indirect=True)
def test_original_file_reuses_successful_absent_pairing_lookup(entry):
    with read_reuse_scope(entry.db):
        for _ in range(10):
            assert entry.call() == entry.expected
    entry.lookup.assert_called_once()
    assert entry.final.call_args.kwargs["pairing_fact_id"] is None
