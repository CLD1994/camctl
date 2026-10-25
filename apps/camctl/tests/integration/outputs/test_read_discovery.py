"""真实候选查询的全体顺序、容量和错误边界。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.slots import SlotOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_read_associations import read_targets, _command  # noqa: F401
from .test_read_slot import two_copies, _slot  # noqa: F401
from .test_local_read import local_read  # noqa: F401
from .test_qualification import _NOW
from .test_product_competition import competition_database, _seed_many_actions  # noqa: F401
from .test_qualification import qualification_environment, _candidate, _Seedling  # noqa: F401
from .test_obtain_summary import prepared_env, _publish  # noqa: F401
from camctl.persistence.repositories import outputs
from camctl.persistence.transaction import TransactionScope
from camctl.outputs.read_discovery import ReadDiscoveryScope, read_candidates


def _scope(connection, kind="all"):
    database = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
    return ReadDiscoveryScope(database, kind, _NOW, 0)


class _BoundedConnection:
    def __init__(self, connection, limit=1):
        self.real = connection
        self.limit = limit
        self.returned = []
        self.query_plan = None

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, parameters=()):
        cursor = self.real.execute(sql, parameters)
        if not sql.startswith("SELECT fc.id"):
            return cursor
        owner = self
        if " ORDER BY " in sql and self.query_plan is None:
            explain = self.real.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            try:
                self.query_plan = explain.fetchall()
            finally:
                explain.close()

        class Cursor:
            def __getattr__(self, name):
                return getattr(cursor, name)

            def fetchall(self):
                rows = cursor.fetchall()
                owner.returned.append(len(rows))
                assert len(rows) <= owner.limit, "读取查询向 Python 返回了全部候选"
                return rows

            def fetchone(self):
                row = cursor.fetchone()
                owner.returned.append(int(row is not None))
                return row

        return Cursor()


def test_later_registered_earlier_action_wins_without_full_load(two_copies):
    owned, early, late = two_copies
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=32", (_NOW - 1_000_000,))
    owned.connection.commit()
    connection = _BoundedConnection(owned.connection)
    result = OutputsRepository().grant_read_slot(
        _slot(late.copy_id), new_operation_key(), replace(owned, connection=connection))
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.GRANTED
    assert connection.returned and max(connection.returned) == 1
    assert connection.query_plan


@pytest.mark.parametrize("fault", ["scheduled", "run_owner", "run_copy", "holder_binding"])
def test_missing_candidate_basis_is_not_filtered_away(two_copies, fault):
    owned, early, late = two_copies
    owned.connection.execute("PRAGMA ignore_check_constraints=ON")
    if fault == "scheduled":
        owned.connection.execute("UPDATE actions SET scheduled_at=NULL WHERE id=32")
    elif fault == "run_owner":
        owned.connection.execute("UPDATE operation_runs SET action_id=31 WHERE copy_id=?", (late.copy_id,))
    elif fault == "run_copy":
        owned.connection.execute("UPDATE operation_runs SET copy_id=NULL WHERE copy_id=?", (late.copy_id,))
    else:
        owned.connection.execute("UPDATE file_copies SET slot_device_id='wrong-device' WHERE id=?", (late.copy_id,))
    owned.connection.commit()
    result = OutputsRepository().grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError)


def test_product_predecessor_scan_retains_only_current_batch(competition_database):
    connection = competition_database.connection
    _seed_many_actions(connection, "active")
    connection.execute("BEGIN")
    try:
        plan = outputs._GrantFileCommand(_candidate(_Seedling(31, 101, 701, 501)),
            new_operation_key()).plan(TransactionScope(connection, 1, 1))
        assert len(plan.state_rows["actions"]) < 10, "资格前序判断累计了全部候选详情"
    finally:
        connection.rollback()


def test_scope_cannot_read_another_database(two_copies):
    owned, _, _ = two_copies
    with pytest.raises(ValueError):
        read_candidates(owned.connection, replace(_scope(owned.connection), database="other"), limit=1)


def test_host_missing_owner_is_not_filtered_away(local_read):
    owned, command = local_read
    granted = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    owned.connection.execute("PRAGMA foreign_keys=OFF")
    owned.connection.execute("DELETE FROM actions WHERE id=31")
    owned.connection.commit()
    with pytest.raises(ConsistencyError):
        read_candidates(owned.connection, _scope(owned.connection, "local"), limit=1)


def test_internal_and_delivery_reads_share_global_order(read_targets):
    owned = read_targets
    repository = OutputsRepository(read_batch_size=1)
    copies = []
    for internal in (False, True):
        result = repository.grant_file(_command(internal=internal), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        copies.append(result.value.copy_id)
    # 内部责任虽后建档，但录像动作的计划内顺序先于取回动作。
    page = read_candidates(owned.connection, _scope(owned.connection), limit=1)
    assert [candidate.copy_id for candidate in page.items] == [copies[1]]
    assert repository.grant_read_slot(_slot(copies[0]), new_operation_key(), owned).value.reason == "predecessor"
    assert repository.grant_read_slot(_slot(copies[1]), new_operation_key(), owned).value.outcome is SlotOutcome.GRANTED


@pytest.mark.parametrize("parallel", [False, True])
def test_delivery_discovery_keeps_internal_holder_with_capture_owner(read_targets, parallel):
    from camctl.outputs.dispatch import plan_device_work

    owned = read_targets
    repository = OutputsRepository(read_batch_size=1)
    delivery = repository.grant_file(_command(), new_operation_key(), owned)
    internal = repository.grant_file(_command(internal=True), new_operation_key(), owned)
    assert delivery.kind is internal.kind is DbOutcomeKind.COMPLETED
    held = repository.grant_read_slot(_slot(internal.value.copy_id), new_operation_key(), owned)
    assert held.kind is DbOutcomeKind.COMPLETED and held.value.outcome is SlotOutcome.GRANTED
    page = read_candidates(owned.connection, _scope(owned.connection, "delivery"), limit=1)
    before = tuple(owned.connection.iterdump())
    work, = plan_device_work(owned.connection, _NOW, {"cam-1"} if parallel else (), candidates=page.items)
    assert work.grant_reads == work.resume_reads == work.yield_reads == ()
    assert tuple(owned.connection.iterdump()) == before


def test_held_retry_copy_precedes_new_candidates(two_copies):
    owned, early, late = two_copies
    repository = OutputsRepository()
    assert repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
    owned.connection.execute("UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (early.copy_id,))
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=32", (_NOW - 1_000_000,))
    owned.connection.commit()
    assert read_candidates(owned.connection, _scope(owned.connection), limit=1).items[0].copy_id == early.copy_id
    assert repository.grant_read_slot(_slot(late.copy_id), new_operation_key(), owned).value.outcome is SlotOutcome.WAIT


def test_newly_eligible_low_key_is_rediscovered(read_targets):
    from camctl.outputs.read_discovery import ReadDiscoveryOwner
    from .test_read_slot import _later_read_command

    owned = read_targets
    repository = OutputsRepository(read_batch_size=1)
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    late = repository.grant_file(_later_read_command(), new_operation_key(), owned)
    assert late.kind is DbOutcomeKind.COMPLETED, late.error
    owner = ReadDiscoveryOwner(1)
    owner.begin_round(owned.connection, _NOW)
    assert owner.reads(owned.connection, "reads", _NOW).items[0].copy_id == late.value.copy_id
    generation = owner.generation
    early = repository.grant_file(_command(), new_operation_key(), owned)
    assert early.kind is DbOutcomeKind.COMPLETED, early.error
    owner.begin_round(owned.connection, _NOW)
    assert owner.generation > generation
    assert owner.reads(owned.connection, "reads", _NOW).items[0].copy_id == early.value.copy_id


def test_evicted_read_resumes_after_restart(two_copies):
    import weakref
    from pathlib import Path
    from camctl.outputs.read_discovery import ReadDiscoveryOwner
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..bootstrap.test_output_read_recovery import _start_unended_read

    owned, early, _ = two_copies
    repository = OutputsRepository(read_batch_size=1)
    assert repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
    ticket = _start_unended_read(owned, early.copy_id)
    database = _scope(owned.connection).database
    owner = ReadDiscoveryOwner(1)
    owner.begin_round(owned.connection, _NOW)
    page = owner.reads(owned.connection, "reads", _NOW)
    reference = weakref.ref(page.items[0])
    del page, owner
    assert reference() is None
    before = tuple(owned.connection.iterdump())
    owned.connection.close()
    fresh = open_existing(Path(database), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        restarted = ReadDiscoveryOwner(1)
        restarted.begin_round(fresh.connection, _NOW)
        assert restarted.reads(fresh.connection, "reads", _NOW).items[0].copy_id == early.copy_id
        decision = repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), fresh)
        assert decision.kind is DbOutcomeKind.COMPLETED and decision.value.outcome is SlotOutcome.HELD
        assert fresh.connection.execute("SELECT id,attempts_used FROM operation_runs WHERE copy_id=?",
            (early.copy_id,)).fetchone() == (ticket.run_id, 1)
        assert tuple(fresh.connection.iterdump()) == before
    finally:
        fresh.connection.close()


def test_guard_independently_rejects_skipped_predecessor(competition_database, monkeypatch):
    import json
    from camctl.history.validators import EventValidationError

    owned = competition_database
    owned.connection.execute("UPDATE actions SET input_fields_json=?,execution_spec_json=? WHERE id=91",
        (json.dumps({"params": {"source": {"plan_instance_id": "2"}}}), json.dumps({"selection_mode": 1})))
    owned.connection.commit()
    real = outputs.has_product_predecessor
    calls = []

    def skip_once(*args):
        calls.append(args[0])
        return False if len(calls) == 1 else real(*args)

    monkeypatch.setattr(outputs, "has_product_predecessor", skip_once)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository(read_batch_size=1).grant_file(_candidate(_Seedling(31, 101, 701, 501)), new_operation_key(), owned)
    assert len(calls) == 2
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_finishing_obtain_does_not_materialize_plan_siblings(prepared_env, tmp_path):
    from .test_qualification import _seed_action
    from camctl.persistence.repositories.outputs import FinishObtain

    owned, roots, qualification = prepared_env
    _publish(owned, roots, qualification.delivery_id, tmp_path)
    for identity in range(1000, 1131):
        _seed_action(owned.connection, identity, 1, action_type=2, status=3)
    owned.connection.commit()
    owned.connection.execute("BEGIN")
    try:
        plan = outputs._FinishObtainCommand(FinishObtain(31, _NOW + 20), new_operation_key()).plan(
            TransactionScope(owned.connection, 1, 1))
        assert len(plan.state_rows["actions"]) < 10
    finally:
        owned.connection.rollback()


def test_candidate_capacity_tracks_batch_not_total(two_copies):
    import gc
    import json
    import weakref
    from .test_qualification import _seed_action, _seed_selection_and_item
    from camctl.outputs.read_discovery import ReadDiscoveryOwner

    owned, _, _ = two_copies
    seeded = 0
    for total, batch_size in ((12, 1), (120, 1), (120, 3)):
        repository = OutputsRepository(read_batch_size=batch_size)
        for identity in range(1000 + seeded, 1000 + total):
            _seed_action(owned.connection, identity, 1, action_type=4)
            _seed_selection_and_item(owned.connection, dependency_id=identity, selection_id=identity,
                item_id=identity, owner_action_id=identity, source_action_id=11, output_id=701)
            owned.connection.commit()
            result = repository.grant_file(replace(_command(), action_id=identity, item_id=identity), new_operation_key(), owned)
            assert result.kind is DbOutcomeKind.COMPLETED and result.value.copy_id is not None, result
        seeded = total
        before = tuple(owned.connection.iterdump())
        owner = ReadDiscoveryOwner(batch_size)
        connection = _BoundedConnection(owned.connection, batch_size)
        owner.begin_round(connection, _NOW)
        returned = seen = largest = 0
        while True:
            page = owner.reads(connection, "reads", _NOW)
            returned = len(page.items)
            seen += returned
            largest = max(largest, returned)
            assert returned <= batch_size and owner.cursor_count <= 1
            references = tuple(weakref.ref(candidate) for candidate in page.items)
            exhausted = page.exhausted
            del page
            gc.collect()
            assert all(reference() is None for reference in references)
            if exhausted:
                break
        assert seen == total + 2
        assert connection.query_plan and max(connection.returned) == batch_size
        assert tuple(owned.connection.iterdump()) == before
        print(json.dumps({"candidates": seen, "batch_size": batch_size,
                          "max_returned": largest, "retained_after_page": 0,
                          "cursor_count_after_scan": owner.cursor_count,
                          "query_plan": connection.query_plan}))


def test_query_failure_is_not_exhaustion(two_copies):
    from ..operations.test_result_reuse import _FaultConnection
    import sqlite3

    owned, _, _ = two_copies
    with pytest.raises(sqlite3.OperationalError):
        read_candidates(_FaultConnection(owned.connection, "SELECT fc.id"), _scope(owned.connection), limit=1)


def test_terminal_actions_do_not_expand_discovered_details(two_copies):
    from camctl.outputs.read_discovery import ReadDiscoveryOwner

    owned, early, late = two_copies
    _seed_many_actions(owned.connection, "plan")
    connection = _BoundedConnection(owned.connection)
    owner = ReadDiscoveryOwner(1)
    owner.begin_round(connection, _NOW)
    discovered = []
    while True:
        page = owner.reads(connection, "reads", _NOW)
        discovered.extend(candidate.copy_id for candidate in page.items)
        if page.exhausted:
            break
    assert discovered == [early.copy_id, late.copy_id]
    assert max(connection.returned) == 1 and owner.cursor_count == 0


@pytest.mark.parametrize("phase,expected", [("window", 1.0), ("expired", 0.0), ("future", 1.0)])
def test_idle_deadline_returns_only_nearest_pending_metadata(competition_database, phase, expected):
    from types import SimpleNamespace
    from camctl.session.service import _pending_deadline_seconds

    owned = competition_database
    _seed_many_actions(owned.connection, "active")
    if phase == "expired":
        owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=1130", (_NOW - 2_000_000,))
    elif phase == "future":
        owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id>=1000", (_NOW + 2_000_000,))
        owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=1130", (_NOW + 1_000_000,))
    owned.connection.commit()
    returned = []

    class Connection:
        def execute(self, sql, parameters=()):
            cursor = owned.connection.execute(sql, parameters)

            class Cursor:
                def close(self):
                    cursor.close()

                def fetchall(self):
                    rows = cursor.fetchall()
                    returned.append(len(rows))
                    return rows

                def fetchone(self):
                    row = cursor.fetchone()
                    returned.append(int(row is not None))
                    return row

            return Cursor()

    context = SimpleNamespace(clock=SimpleNamespace(utc_micros=lambda: _NOW))
    assert _pending_deadline_seconds(context, replace(owned, connection=Connection())) == expected
    assert returned and max(returned) <= 1, "等待入口向 Python 返回了全部待执行动作"


def test_device_holder_query_detects_multiple_holders():
    import sqlite3
    from camctl.outputs.read_discovery import device_holder

    # 受损结构不能依赖 UNIQUE 来代替读方核实；探测只需要最多两行。
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE file_copies (id INTEGER,slot_device_id TEXT)")
        connection.executemany("INSERT INTO file_copies VALUES (?, 'cam-1')", [(1,), (2,), (3,)])
        with pytest.raises(ConsistencyError):
            device_holder(connection, "cam-1")


@pytest.mark.parametrize("case,phase,prepared,failed", [
    ("waiting", "wait_sources", 0, 0), ("unresolved", "wait_items", 0, 0),
    ("mixed", "ready_to_publish", 1, 131), ("empty", "ready_to_publish", 0, 0),
    ("failed", "ready_to_publish", 0, 132),
])
def test_obtain_summary_folds_many_items_without_full_materialization(prepared_env, case, phase, prepared, failed):
    from camctl.outputs.obtain_summary import decide_obtain_finish
    from camctl.persistence.repositories.outputs import load_obtain_facts

    owned, _, qualified = prepared_env
    for identity in range(1000, 1131):
        owned.connection.execute(
            "INSERT INTO obtain_items (id,selection_id,requested_output_id,basis,status,source_dependency,error_code,error_details_json)"
            " VALUES (?,31,?,5,4,0,1,?)", (identity, identity + 10000,
                '{"requested_output_id":"' + str(identity + 10000) + '"}'))
    if case == "waiting":
        owned.connection.execute("UPDATE obtain_source_selections SET status=1 WHERE id=31")
    elif case == "unresolved":
        owned.connection.execute("UPDATE obtain_items SET status=1,error_code=NULL,error_details_json=NULL WHERE id=1000")
    elif case == "empty":
        owned.connection.execute("DELETE FROM obtain_items WHERE selection_id=31")
    elif case == "failed":
        owned.connection.execute("UPDATE deliveries SET status=6,error_json='{}' WHERE id=?", (qualified.delivery_id,))
    owned.connection.commit()
    facts = load_obtain_facts(owned.connection, 31)
    assert not hasattr(facts, "items") and not hasattr(facts, "sources")
    decision = decide_obtain_finish(facts)
    assert (decision.phase.value, decision.prepared, decision.failed) == (phase, prepared, failed)


def test_plan_guard_independently_rejects_skipped_unfinished_sibling(prepared_env, tmp_path, monkeypatch):
    from camctl.history import validators
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.outputs import FinishObtain

    # 按默认装配注册实际终态守卫，隔离其他用例的受理专用同名注册。
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    owned, roots, qualified = prepared_env
    _publish(owned, roots, qualified.delivery_id, tmp_path)
    monkeypatch.setattr(outputs, "_completion_siblings", lambda connection, action: {action["id"]: action})
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository(read_batch_size=1).finish_obtain(FinishObtain(31, _NOW + 20), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before
