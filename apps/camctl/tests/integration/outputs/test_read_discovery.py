"""真实候选查询的全体顺序、容量和错误边界。"""

import asyncio
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.slots import SlotOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_read_associations import read_targets, _command  # noqa: F401
from .test_read_slot import mixed_purposes, same_batch_copies, two_copies, _acquire_early_batch, _slot  # noqa: F401
from .test_local_read import local_read  # noqa: F401
from .test_qualification import _NOW
from .test_product_competition import competition_database, _seed_many_actions  # noqa: F401
from .test_qualification import qualification_environment, _candidate, _Seedling  # noqa: F401
from .test_obtain_summary import _roots
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.transactions import commit_operation
from ..support.output_planning import EventSequence, capture_operation
from ..support.public_delivery_cases import prepare_public_delivery
from ..support.read_batch_cases import acquire_read_batch
from ..support.output_storage_cases import QueryProbe, capture_scoped_operation
from ..support.outputs import observed_query
from ..persistence.transaction_fixtures import decorate_transaction_command
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
        cursor, columns, reads = observed_query(self.real, sql, parameters)
        if not (
            ("file_copies", "id") in reads and ("actions", "scheduled_at") in reads
        ):
            return cursor
        owner = self
        if self.query_plan is None:
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


@asynccontextmanager
async def _discovery_database(path, *, batch_size=None):
    bounded = []

    def open_connection():
        owned = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
        if batch_size is None:
            return owned
        connection = _BoundedConnection(owned.connection, batch_size)
        bounded.append(connection)
        return replace(owned, connection=connection)

    database = await BusinessDatabase.open(open_connection)
    try:
        yield database, bounded
    finally:
        await database.close()


@pytest.fixture
def prepared_env(read_targets, tmp_path):
    import hashlib
    from decimal import Decimal
    from camctl.devices.read_session import ReadSession, SourceFile
    from camctl.outputs.copy import (
        CompletionContext, CompletionPhase, CopyContext, SegmentContext, SegmentOutcome,
        complete_copy, copy_next_segment, prepare_copy,
    )
    from .test_copy_resume import _command as copy_command, _qualified
    from .test_copy_segments import _ByteStream, _CONTENT

    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute("UPDATE device_files SET size_bytes=10,checksum_support=2,sha256=? WHERE id=501",
        (hashlib.sha256(_CONTENT).hexdigest(),))
    owned.connection.commit()
    qualification = _qualified(owned, copy_command())
    roots = _roots(tmp_path)

    async def prepare():
        async with _discovery_database(_scope(owned.connection).database) as (database, _):
            repository = OutputsRepository()
            await prepare_copy(qualification.copy_id, CopyContext(
                repository=repository, database=database, roots=roots, occurred_at=_NOW + 1))
            session = ReadSession(SourceFile("501", {}, len(_CONTENT)), 0,
                _ByteStream(_CONTENT), Decimal("10"))
            try:
                for index in range(99):
                    step = await copy_next_segment(qualification.copy_id, SegmentContext(
                        repository=repository, database=database, roots=roots,
                        occurred_at=_NOW + 1 + index, segment_size=10, session=session))
                    if step.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                        break
            finally:
                session.request_stop()
                await session.wait_stopped()
            completed = await complete_copy(qualification.copy_id, CompletionContext(
                repository=repository, database=database, roots=roots, occurred_at=_NOW + 5))
            assert completed.phase is CompletionPhase.PREPARED

    asyncio.run(prepare())
    return owned, roots, qualification


@pytest.fixture
def published_public_delivery(tmp_path):
    from .test_copy_segments import _CONTENT
    from .test_delivery import _publish as publish

    owned, roots, directories, qualification = asyncio.run(
        prepare_public_delivery(tmp_path, content=_CONTENT, now=_NOW))
    try:
        result = publish(owned, directories, qualification.delivery_id)
        from camctl.outputs.handoff import DeliveryPhase
        assert result.phase is DeliveryPhase.PUBLISHED
        yield owned, qualification
    finally:
        owned.connection.close()


def test_later_registered_earlier_action_wins_without_full_load(two_copies):
    owned, early, late = two_copies
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=32", (_NOW - 1_000_000,))
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    connection = _BoundedConnection(owned.connection)
    page = read_candidates(connection, _scope(connection), limit=1)
    assert [candidate.copy_id for candidate in page.items] == [late.copy_id]
    assert connection.returned and max(connection.returned) == 1
    assert connection.query_plan
    acquire_read_batch(owned, 32, "cam-1", _NOW)
    result = OutputsRepository().grant_read_slot(
        _slot(late.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.GRANTED


@pytest.mark.parametrize("fault", ["scheduled", "run_owner", "run_copy", "holder_binding"])
def test_missing_candidate_basis_is_not_filtered_away(request, fault):
    if fault == "scheduled":
        owned, early, late = request.getfixturevalue("two_copies")
        _acquire_early_batch(owned)
    else:
        owned, early, late = request.getfixturevalue("same_batch_copies")
    owned.connection.execute("PRAGMA ignore_check_constraints=ON")
    if fault == "scheduled":
        owned.connection.execute("UPDATE actions SET scheduled_at=NULL WHERE id=32")
    elif fault == "run_owner":
        owned.connection.execute("UPDATE operation_runs SET action_id=32 WHERE copy_id=?", (late.copy_id,))
    elif fault == "run_copy":
        owned.connection.execute("UPDATE operation_runs SET copy_id=NULL WHERE copy_id=?", (late.copy_id,))
    else:
        owned.connection.execute("UPDATE file_copies SET slot_device_id='wrong-device' WHERE id=?", (late.copy_id,))
    owned.connection.commit()
    result = OutputsRepository().grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError)
    assert "读取候选缺少可靠关联、流程或排序依据" in str(result.error)


def test_product_predecessor_scan_retains_only_current_batch(
    competition_database, monkeypatch
):
    owned = competition_database
    _seed_many_actions(owned.connection, "active")
    sizes = []

    def observe(command, scope):
        plan = command.plan(scope)
        sizes.append(len(plan.state_rows["actions"]))
        return plan

    decorate_transaction_command(monkeypatch, outputs, observe)
    result = OutputsRepository().grant_file(
        _candidate(_Seedling(31, 101, 701, 501)), new_operation_key(), owned
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert sizes and max(sizes) < 10, "资格前序判断累计了全部候选详情"


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


def test_internal_and_delivery_reads_share_device_order(mixed_purposes):
    from camctl.outputs.batches import BatchOutcome, BatchRequest
    from camctl.persistence.repositories.file_batches import FileBatchRepository

    owned, internal, external = mixed_purposes
    repository = OutputsRepository(read_batch_size=1)
    # 内部责任虽后建档，但录像动作的计划内顺序先于取回动作。
    page = read_candidates(owned.connection, _scope(owned.connection), limit=1)
    assert [candidate.copy_id for candidate in page.items] == [internal.copy_id]
    batch = FileBatchRepository().acquire(
        BatchRequest(32, "cam-1", _NOW), new_operation_key(), owned)
    assert batch.kind is DbOutcomeKind.COMPLETED and batch.value is BatchOutcome.WAIT, batch
    granted = repository.grant_read_slot(_slot(internal.copy_id), new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED and granted.value.outcome is SlotOutcome.GRANTED, granted


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
    _acquire_early_batch(owned)
    held = repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert held.kind is DbOutcomeKind.COMPLETED and held.value.outcome is SlotOutcome.GRANTED, held
    owned.connection.execute("UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (early.copy_id,))
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=32", (_NOW - 1_000_000,))
    owned.connection.commit()
    assert read_candidates(owned.connection, _scope(owned.connection), limit=1).items[0].copy_id == early.copy_id
    assert repository.grant_read_slot(_slot(late.copy_id), new_operation_key(), owned).value.outcome is SlotOutcome.WAIT


@pytest.mark.asyncio
async def test_newly_eligible_low_key_is_rediscovered(read_targets):
    from camctl.outputs.read_discovery import ReadDiscoveryOwner
    from .test_read_slot import _later_read_command

    owned = read_targets
    repository = OutputsRepository(read_batch_size=1)
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    late = repository.grant_file(_later_read_command(), new_operation_key(), owned)
    assert late.kind is DbOutcomeKind.COMPLETED, late.error
    owner = ReadDiscoveryOwner(1)
    async with _discovery_database(_scope(owned.connection).database) as (database, _):
        await owner.begin_round(database, _NOW)
        assert (await owner.reads(database, "reads", _NOW)).items[0].copy_id == late.value.copy_id
        generation = owner.generation
        key = new_operation_key()
        early = await database.write(lambda current: repository.grant_file(_command(), key, current), key=key)
        assert early.kind is DbOutcomeKind.COMPLETED, early.error
        await owner.begin_round(database, _NOW)
        assert owner.generation > generation
        assert (await owner.reads(database, "reads", _NOW)).items[0].copy_id == early.value.copy_id


@pytest.mark.asyncio
async def test_pending_read_resumes_after_discovery_restart(two_copies):
    from camctl.outputs.read_discovery import ReadDiscoveryOwner
    from ..bootstrap.test_output_read_recovery import _start_unended_read

    owned, early, _ = two_copies
    repository = OutputsRepository(read_batch_size=1)
    _acquire_early_batch(owned)
    held = repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert held.kind is DbOutcomeKind.COMPLETED and held.value.outcome is SlotOutcome.GRANTED, held
    ticket = _start_unended_read(owned, early.copy_id)
    path = _scope(owned.connection).database
    async with _discovery_database(path) as (database, _):
        owner = ReadDiscoveryOwner(1)
        await owner.begin_round(database, _NOW)
        page = await owner.reads(database, "reads", _NOW)
        assert [candidate.copy_id for candidate in page.items] == [early.copy_id]
    before = tuple(owned.connection.iterdump())
    owned.connection.close()
    async with _discovery_database(path) as (database, _):
        restarted = ReadDiscoveryOwner(1)
        await restarted.begin_round(database, _NOW)
        assert (await restarted.reads(database, "reads", _NOW)).items[0].copy_id == early.copy_id
        key = new_operation_key()
        decision = await database.write(lambda current: repository.grant_read_slot(_slot(early.copy_id), key, current), key=key)
        assert decision.kind is DbOutcomeKind.COMPLETED and decision.value.outcome is SlotOutcome.HELD
        assert await database.read(lambda current: current.connection.execute(
            "SELECT id,attempts_used FROM operation_runs WHERE copy_id=?",
            (early.copy_id,)).fetchone()) == (ticket.run_id, 1)
        assert await database.read(lambda current: current.connection.execute(
            "SELECT status,result_json,max_attempts_used FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()) == (1, None, 3)
        assert await database.read(lambda current: tuple(current.connection.iterdump())) == before


def test_guard_independently_rejects_skipped_predecessor(competition_database):
    import json
    from camctl.history.validators import EventValidationError

    owned = competition_database
    proposal = capture_scoped_operation(
        owned,
        outputs,
        lambda current: OutputsRepository(read_batch_size=1).grant_file(
            _candidate(_Seedling(31, 101, 701, 501)), new_operation_key(), current
        ),
    )
    # 已形成的合法提案不能越过提交时当前出现的同产物前序。
    owned.connection.execute(
        "UPDATE actions SET input_fields_json=?,execution_spec_json=? WHERE id=91",
        (
            json.dumps({"params": {"source": {"plan_instance_id": "2"}}}),
            json.dumps({"selection_mode": 1}),
        ),
    )
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = commit_operation(
        EventSequence(proposal.events, proposal), new_operation_key(), owned
    )
    assert result.kind == "rolled_back"
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_finishing_obtain_does_not_materialize_plan_siblings(
    published_public_delivery, monkeypatch
):
    from .test_qualification import _seed_action
    from camctl.persistence.repositories.outputs import FinishObtain

    owned, qualification = published_public_delivery
    action_id = qualification.action_id
    plan_id, = owned.connection.execute(
        "SELECT plan_id FROM actions WHERE id=?", (action_id,)).fetchone()
    for identity in range(1000, 1131):
        _seed_action(owned.connection, identity, plan_id, action_type=2, status=3)
    owned.connection.commit()
    sizes = []

    def observe(command, scope):
        plan = command.plan(scope)
        sizes.append(len(plan.state_rows["actions"]))
        return plan

    decorate_transaction_command(monkeypatch, outputs, observe)
    result = OutputsRepository().finish_obtain(
        FinishObtain(action_id, _NOW + 20), new_operation_key(), owned
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert sizes and max(sizes) < 10


@pytest.mark.asyncio
async def test_candidate_capacity_tracks_batch_not_total(two_copies):
    import gc
    import json
    import weakref
    from .test_qualification import _seed_action, _seed_selection_and_item
    from camctl.outputs.read_discovery import ReadDiscoveryOwner

    owned, early, late = two_copies
    expected_ids = [early.copy_id, late.copy_id]
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
            expected_ids.append(result.value.copy_id)
        seeded = total
        before = tuple(owned.connection.iterdump())
        async with _discovery_database(_scope(owned.connection).database, batch_size=batch_size) as (database, bounded):
            owner = ReadDiscoveryOwner(batch_size)
            await owner.begin_round(database, _NOW)
            returned = seen = largest = 0
            actual_ids = []
            while True:
                page = await owner.reads(database, "reads", _NOW)
                returned = len(page.items)
                seen += returned
                actual_ids.extend(candidate.copy_id for candidate in page.items)
                assert actual_ids == expected_ids[:len(actual_ids)]
                assert len(actual_ids) == len(set(actual_ids))
                largest = max(largest, returned)
                assert returned <= batch_size and owner.cursor_count <= 1
                references = tuple(weakref.ref(candidate) for candidate in page.items)
                exhausted = page.exhausted
                del page
                # 等当前asyncio交付Handle退出后核对释放，不触发下一次数据库查询。
                await asyncio.sleep(0)
                # 已释放的候选无需扫描整个堆；仍存活时保留循环回收后的释放检查。
                if any(reference() is not None for reference in references):
                    gc.collect()
                assert all(reference() is None for reference in references), "发现器保留了已消费页的候选详情"
                if exhausted:
                    break
            connection, = bounded
            assert actual_ids == expected_ids
            assert seen == total + 2
            assert connection.query_plan and max(connection.returned) == batch_size
        assert tuple(owned.connection.iterdump()) == before
        print(json.dumps({"candidates": seen, "batch_size": batch_size,
                          "max_returned": largest, "retained_after_page": 0,
                          "cursor_count_after_scan": owner.cursor_count,
                          "query_plan": connection.query_plan}))


@pytest.mark.asyncio
async def test_release_observation_detects_page_retained_by_discovery_owner(two_copies):
    import gc
    import weakref
    from camctl.outputs.read_discovery import ReadDiscoveryOwner

    owned, _, _ = two_copies
    async with _discovery_database(_scope(owned.connection).database) as (database, _):
        owner = ReadDiscoveryOwner(1)
        await owner.begin_round(database, _NOW)
        page = await owner.reads(database, "reads", _NOW)
        reference = weakref.ref(page.items[0])
        # 主动保留一页作为反例；交付回调退出不能掩盖发现器持有候选详情。
        owner.retained_page_for_control = page
        del page
        await asyncio.sleep(0)
        if reference() is not None:
            gc.collect()
        with pytest.raises(AssertionError, match="发现器保留了已消费页的候选详情"):
            assert reference() is None, "发现器保留了已消费页的候选详情"


def test_query_failure_is_not_exhaustion(two_copies):
    import sqlite3

    owned, _, _ = two_copies
    with pytest.raises(sqlite3.OperationalError):
        read_candidates(
            QueryProbe(
                owned.connection,
                lambda columns, reads, parameters: ("file_copies", "id") in reads
                and ("actions", "scheduled_at") in reads,
                failure="execute",
            ),
            _scope(owned.connection),
            limit=1,
        )


@pytest.mark.asyncio
async def test_terminal_actions_do_not_expand_discovered_details(two_copies):
    from camctl.outputs.read_discovery import ReadDiscoveryOwner

    owned, early, late = two_copies
    _seed_many_actions(owned.connection, "plan")
    async with _discovery_database(_scope(owned.connection).database, batch_size=1) as (database, bounded):
        owner = ReadDiscoveryOwner(1)
        await owner.begin_round(database, _NOW)
        discovered = []
        while True:
            page = await owner.reads(database, "reads", _NOW)
            discovered.extend(candidate.copy_id for candidate in page.items)
            if page.exhausted:
                break
        connection, = bounded
        assert discovered == [early.copy_id, late.copy_id]
        assert max(connection.returned) == 1 and owner.cursor_count == 0


@pytest.mark.parametrize(
    "phase,expected", [("window", 1.0), ("expired", 0.0), ("future", 1.0)]
)
@pytest.mark.asyncio
async def test_idle_deadline_returns_only_nearest_pending_metadata(
    competition_database, phase, expected
):
    from camctl.session.service import run_session
    from camctl.session.work import WorkFacts
    from camctl.scheduling.notifications import WorkNotifier
    from ..support.session import session_context, open_session_database, cancel_session
    from ..session.test_session import FixedClock

    owned = competition_database
    _seed_many_actions(owned.connection, "active")
    if phase == "expired":
        owned.connection.execute(
            "UPDATE actions SET scheduled_at=? WHERE id=1130", (_NOW - 2_000_000,)
        )
    elif phase == "future":
        owned.connection.execute(
            "UPDATE actions SET scheduled_at=? WHERE id>=1000", (_NOW + 2_000_000,)
        )
        owned.connection.execute(
            "UPDATE actions SET scheduled_at=? WHERE id=1130", (_NOW + 1_000_000,)
        )
    owned.connection.commit()
    probes = []

    class Notifier(WorkNotifier):
        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()
            self.delays = []

        async def wait_changed(self, observed, deadline):
            self.delays.append(deadline - asyncio.get_running_loop().time())
            self.waiting.set()
            return await super().wait_changed(observed, deadline)

    notifier = Notifier()
    path = Path(_scope(owned.connection).database)

    def factory():
        current = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        probe = QueryProbe(
            current.connection,
            lambda columns, reads, parameters: ("actions", "scheduled_at") in reads
            and len(columns) == 1,
        )
        probes.append(probe)
        return replace(current, connection=probe)

    context = session_context(
        path,
        clock=FixedClock(_NOW),
        flows={},
        wake=notifier,
        poll_interval_s=3.0,
        facts_query=lambda connection: WorkFacts(
            1, 0, False, False, False, False, False, False
        ),
        open_database=lambda: open_session_database(path, factory=factory),
    )
    task = asyncio.create_task(run_session(context, None))
    try:
        signal = asyncio.create_task(notifier.waiting.wait())
        try:
            done, _ = await asyncio.wait({signal, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
                pytest.fail("会话在进入等待前结束")
            assert signal in done, "会话未在十秒内进入等待"
        finally:
            signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)
        assert notifier.delays[0] == pytest.approx(expected, abs=0.05)
        assert (
            probes and probes[0].returned and max(probes[0].returned) <= 1
        ), "等待入口向 Python 返回了全部待执行动作"
    finally:
        await cancel_session(task)


def test_device_holder_query_detects_multiple_holders():
    import sqlite3
    from camctl.outputs.read_discovery import device_holder

    # 受损结构不能依赖 UNIQUE 来代替读方核实；探测只需要最多两行。
    with closing(sqlite3.connect(":memory:")) as connection:
        with connection:
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
        owned.connection.execute("INSERT INTO action_dependencies (id,action_id,depends_on_action_id) VALUES (9999,31,12)")
        owned.connection.execute("INSERT INTO obtain_source_selections (id,dependency_id,status,error_code,error_details_json) VALUES (9999,9999,1,NULL,NULL)")
    elif case == "unresolved":
        owned.connection.execute("UPDATE obtain_items SET status=1,error_code=NULL,error_details_json=NULL WHERE id=1000")
    elif case == "empty":
        owned.connection.execute("DELETE FROM obtain_items WHERE selection_id=31")
    elif case == "failed":
        owned.connection.execute("UPDATE deliveries SET status=6,error_json='{}' WHERE id=?", (qualified.delivery_id,))
    owned.connection.commit()
    (item_count,) = owned.connection.execute(
        "SELECT COUNT(*) FROM obtain_items WHERE selection_id=31").fetchone()
    assert item_count == (0 if case == "empty" else 132)

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.delivered = []
            self.rows = []

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            counts = self.delivered
            observed_rows = self.rows
            counts.append(0)
            index = len(counts) - 1

            class Cursor:
                def __getattr__(self, name):
                    return getattr(cursor, name)

                def fetchone(self):
                    row = cursor.fetchone()
                    counts[index] += row is not None
                    if row is not None:
                        observed_rows.append(row)
                    return row

                def fetchmany(self, *args):
                    rows = cursor.fetchmany(*args)
                    counts[index] += len(rows)
                    observed_rows.extend(rows)
                    return rows

                def fetchall(self):
                    rows = cursor.fetchall()
                    counts[index] += len(rows)
                    observed_rows.extend(rows)
                    return rows

                def __iter__(self):
                    return self

                def __next__(self):
                    row = next(cursor)
                    counts[index] += 1
                    observed_rows.append(row)
                    return row

            return Cursor()

    observed = ObservedConnection(owned.connection)
    facts = load_obtain_facts(observed, 31)
    # 此样本最多有两种条目与交付状态组合，加上一行来源计数。
    assert observed.delivered and sum(observed.delivered) <= 3
    assert all(len(row) <= 3 and all(value is None or isinstance(value, int)
                                      for value in row) for row in observed.rows)
    decision = decide_obtain_finish(facts)
    assert (decision.phase.value, decision.prepared, decision.failed) == (phase, prepared, failed)
