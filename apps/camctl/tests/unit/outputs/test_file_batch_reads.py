"""同机批次的原事实读取，以连接替身隔离数据库和包资源。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.persistence.repositories.file_batch_reads import FileBatchReads


class Cursor:
    def __init__(self, pages):
        self.pages = list(pages)

    def fetchone(self):
        rows = self.pages.pop(0) if self.pages else []
        return rows[0] if rows else None

    def fetchall(self):
        return self.pages.pop(0) if self.pages else []

    def fetchmany(self, size):
        return self.pages.pop(0) if self.pages else []

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_resources(monkeypatch):
    from camctl import resources
    from camctl.contracts import enums
    monkeypatch.setattr(resources, "resource_bytes", lambda *_: pytest.fail("禁止读取包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda *_: pytest.fail("禁止读取包资源"))


@pytest.mark.parametrize("owner,requested_action,required,allowed", [
    (None, 1, False, True), (None, 1, True, False),
    (1, 1, True, True), (1, 2, False, False), (1, 2, True, False),
])
def test_same_device_requires_original_batch_owner(owner, requested_action, required, allowed):
    connection = Mock()
    connection.execute.side_effect = [Cursor([[(9,)]]), Cursor([[] if owner is None else [(7, owner)]])]
    assert FileBatchReads(connection).permits(requested_action, "camera", require_owned=required) is allowed


def test_older_format_does_not_query_new_table():
    connection = Mock()
    connection.execute.return_value = Cursor([[(8,)]])
    assert FileBatchReads(connection).holder("camera") is None
    connection.execute.assert_called_once()


def test_missing_database_version_is_not_treated_as_no_holder():
    connection = Mock()
    connection.execute.return_value = Cursor([[]])
    with pytest.raises(ValueError):
        FileBatchReads(connection).holder("camera")


def test_two_holders_are_a_state_error():
    connection = Mock()
    connection.execute.side_effect = [Cursor([[(9,)]]), Cursor([[(7, 1), (8, 2)]])]
    with pytest.raises(ValueError):
        FileBatchReads(connection).holder("camera")


@pytest.mark.parametrize("last_member,finished", [
    ((2, 0, None, None, None, None, 0, None), False),
    ((3, 1, 8, 1, 100, 0, 0, 2), False),
    ((3, 1, 8, 3, 100, 1, 0, 2), False),
    ((3, 1, 8, 3, 100, 0, 1, 2), False),
    ((3, 1, 8, 3, 100, 0, 0, 2), True),
    ((4, 0, None, None, None, None, 0, None), True),
])
def test_release_checks_members_beyond_first_page(monkeypatch, last_member, finished):
    connection = Mock()
    connection.execute.return_value = Cursor([
        [(3, 1, 7, 3, 99, 0, 0, 2)], [last_member], [],
    ])
    reads = FileBatchReads(connection)
    monkeypatch.setattr(reads, "source_ready", lambda *_: True)
    assert reads.obtain_finished(1, "camera") is finished


def test_unfinished_same_camera_source_does_not_compete(monkeypatch):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, "action", lambda _: {
        "type": 4, "status": 2, "cancel_requested": 0,
        "scheduled_at": 10, "source_resolution_state": 2,
    })
    monkeypatch.setattr(reads, "source_ready", lambda *_: False)
    monkeypatch.setattr(reads, "obtain_finished", lambda *_: pytest.fail("来源未就绪不能申请相机"))
    assert not reads.ready(1, "camera", 20)


def test_cleanup_waits_for_all_prior_read_dependencies(monkeypatch):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, "action", lambda _: {
        "type": 5, "status": 2, "cancel_requested": 0,
        "scheduled_at": 10, "target_selection_state": 2,
    })
    monkeypatch.setattr(reads, "cleanup_blocked", lambda *_: True)
    monkeypatch.setattr(reads, "cleanup_device_ready", lambda *_: True)
    monkeypatch.setattr(reads, "cleanup_finished", lambda *_: pytest.fail("旧读取未结束不能占相机"))
    assert not reads.ready(1, "camera", 20)


def test_candidate_discovery_continues_past_waiting_batch(monkeypatch):
    connection = Mock()
    connection.execute.return_value = Cursor([[(1, 10, 4, 1, 0)], [(2, 20, 5, 1, 1)], []])
    reads = FileBatchReads(connection)
    monkeypatch.setattr(reads, "ready", lambda action_id, *_: action_id == 2)
    assert reads.first("camera", 30) == 2


def test_stop_does_not_wait_for_file_batch(monkeypatch):
    from camctl.contracts import enums
    monkeypatch.setattr(enums, "enum_for", lambda _: SimpleNamespace(STOP=2, STOP_RESIDUAL=8, EMERGENCY_STOP=9))
    connection = Mock()
    assert FileBatchReads(connection).operation_permitted({"id": 1, "type": 2}, {"kind": 2})
    connection.execute.assert_not_called()


def test_local_copy_does_not_claim_camera(monkeypatch):
    from camctl.contracts import enums
    monkeypatch.setattr(enums, "enum_for", lambda _: SimpleNamespace(STOP=2, STOP_RESIDUAL=8, EMERGENCY_STOP=9))
    connection = Mock()
    connection.execute.return_value = Cursor([[]])
    assert FileBatchReads(connection).operation_permitted({"id": 1, "type": 4}, {"kind": 3, "copy_id": 7})


@pytest.mark.parametrize("pending,finished,ready", [(True, False, True), (False, False, False), (True, True, False)])
def test_canceled_cleanup_only_claims_for_original_unfinished_settlement(monkeypatch, pending, finished, ready):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, "action", lambda _: {"type": 5, "status": 6, "cancel_requested": 1, "scheduled_at": 10})
    monkeypatch.setattr(reads, "cleanup_residual", lambda *_: pending)
    monkeypatch.setattr(reads, "cleanup_blocked", lambda *_: False)
    monkeypatch.setattr(reads, "cleanup_finished", lambda *_: finished)
    assert reads.ready(1, "camera", 20) is ready


@pytest.mark.parametrize("local_pending,device_work,expected", [
    (True, True, False), (False, False, False), (False, True, True),
])
def test_only_real_device_work_competes_after_local_cache_stage(local_pending, device_work, expected):
    connection = Mock()
    connection.execute.side_effect = [Cursor([[(1,)] if local_pending else []]),
        Cursor([[(1,)] if device_work else []])]
    assert FileBatchReads(connection).obtain_device_ready(1, "camera") is expected


def test_unresolved_cleanup_target_prevents_batch_acquisition():
    connection = Mock()
    connection.execute.return_value = Cursor([[(1,)]])
    assert not FileBatchReads(connection).cleanup_device_ready(1, "camera")


@pytest.mark.parametrize("run", [
    {"kind": 5, "obtain_item_id": 12},
    {"kind": 12, "availability_check_id": 13},
])
def test_query_and_availability_keep_batch_until_original_responsibility_ends(monkeypatch, run):
    import sys
    from types import ModuleType
    transaction = ModuleType("camctl.persistence.repositories.transactions")
    transaction.row_facts = lambda _connection, table, identity: {"id": identity, **run}
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    connection = Mock()
    connection.execute.side_effect = [Cursor([[(7,)], []]), Cursor([[('camera',)]])]
    reads = FileBatchReads(connection)
    monkeypatch.setattr(reads, "action", lambda _: {"id": 1, "type": 4})
    assert reads.calls_pending(1, "camera")


def test_canceled_member_does_not_release_still_running_query(monkeypatch):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, "action", lambda _: {"id": 1, "type": 4})
    monkeypatch.setattr(reads, "calls_pending", lambda *_: True)
    monkeypatch.setattr(reads, "obtain_finished", lambda *_: pytest.fail("查询仍未收场不能释放"))
    assert not reads.finished(1, "camera")


@pytest.mark.parametrize("existing_run,allowed", [(True, True), (False, False)])
def test_legacy_permission_is_for_original_run_and_never_a_new_begin(monkeypatch, existing_run, allowed):
    from camctl.contracts import enums
    monkeypatch.setattr(enums, "enum_for", lambda _: SimpleNamespace(STOP=2, STOP_RESIDUAL=8, EMERGENCY_STOP=9))
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, "operation_device", lambda *_: "camera")
    original = Mock(return_value=True)
    monkeypatch.setattr(reads, "legacy_read_held", original)
    monkeypatch.setattr(reads, "permits", lambda *_args, **_kwargs: False)
    run = {"kind": 3, "copy_id": 7}
    if existing_run:
        run["id"] = 11
    assert reads.operation_permitted({"id": 1, "type": 4}, run) is allowed
    assert original.call_count == int(existing_run)


@pytest.mark.parametrize("holder,original,allowed", [(None, True, True), (None, False, False), ((8, 2), True, False)])
def test_legacy_original_requires_retained_slot_and_no_batch(monkeypatch, holder, original, allowed):
    connection = Mock()
    connection.execute.return_value = Cursor([[(1,)] if original else []])
    reads = FileBatchReads(connection)
    monkeypatch.setattr(reads, "supported", lambda: True)
    monkeypatch.setattr(reads, "holder", lambda _: holder)
    assert reads.legacy_read_held(7, 1, "camera") is allowed
    if holder is None:
        assert connection.execute.call_args.args[1] == (7, 1, "camera")
    else:
        connection.execute.assert_not_called()


@pytest.mark.parametrize('action,sequence', [
    ({'type': 4, 'status': 2, 'cancel_requested': 0, 'scheduled_at': 10,
      'source_resolution_state': 2},
     [('source_ready', True), ('obtain_device_ready', True), ('obtain_finished', False)]),
    ({'type': 5, 'status': 2, 'cancel_requested': 0, 'scheduled_at': 10,
      'target_selection_state': 2},
     [('cleanup_device_ready', True), ('cleanup_blocked', False), ('cleanup_finished', False)]),
    ({'type': 5, 'status': 6, 'cancel_requested': 1, 'scheduled_at': 10},
     [('cleanup_residual', True), ('cleanup_blocked', False), ('cleanup_finished', False)]),
])
@pytest.mark.parametrize('stop_at', [None, 0, 1, 2])
def test_batch_ready_reads_each_required_fact_in_order_and_stops(monkeypatch, action, sequence, stop_at):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, 'action', lambda _: action)
    calls = []
    for index, (name, required) in enumerate(sequence):
        def read(*args, name=name, index=index, required=required):
            calls.append((name, args))
            return not required if index == stop_at else required
        monkeypatch.setattr(reads, name, read)
    assert reads.ready(1, 'camera', 20) is (stop_at is None)
    expected = sequence if stop_at is None else sequence[:stop_at + 1]
    assert calls == [(name, (1, 'camera')) for name, _ in expected]


def test_ineligible_action_does_not_read_batch_conditions(monkeypatch):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, 'action', lambda _: {'scheduled_at': 21})
    monkeypatch.setattr(reads, 'source_ready', lambda *_: pytest.fail('尚未到期不能读取资格'))
    assert reads.ready(1, 'camera', 20) is False


@pytest.mark.parametrize('local_pending,device_pending,expected,calls', [
    (True, True, False, 1),
    (False, False, False, 2),
    (False, True, True, 2),
])
def test_obtain_device_looks_up_local_before_device_and_keeps_limit(local_pending, device_pending, expected, calls):
    connection = Mock()
    connection.execute.side_effect = [Cursor([[(1,)] if local_pending else []]),
                                      Cursor([[(1,)] if device_pending else []])]
    assert FileBatchReads(connection).obtain_device_ready(7, 'camera') is expected
    assert connection.execute.call_count == calls
    statements = connection.execute.call_args_list
    assert 'source_intermediate_file_id' in statements[0].args[0]
    if calls == 2:
        assert 'source_device_file_id' in statements[1].args[0]
    assert all(call.args[0].endswith(' LIMIT 1') and call.args[1] == (7, 'camera') for call in statements)


@pytest.mark.parametrize('stage', [0, 1])
def test_obtain_device_propagates_original_read_error_at_its_step(stage):
    connection = Mock()
    error = RuntimeError('原数据库读取失败')
    connection.execute.side_effect = [error] if stage == 0 else [Cursor([[]]), error]
    with pytest.raises(RuntimeError) as caught:
        FileBatchReads(connection).obtain_device_ready(7, 'camera')
    assert caught.value is error
    assert connection.execute.call_count == stage + 1


@pytest.mark.parametrize('stage', [0, 1, 2])
def test_batch_ready_propagates_first_failing_fact_without_later_reads(monkeypatch, stage):
    reads = FileBatchReads(Mock())
    monkeypatch.setattr(reads, 'action', lambda _: {
        'type': 4, 'status': 2, 'cancel_requested': 0,
        'scheduled_at': 10, 'source_resolution_state': 2})
    error = RuntimeError('原事实读取失败')
    sequence = [('source_ready', True), ('obtain_device_ready', True), ('obtain_finished', False)]
    calls = []
    for index, (name, result) in enumerate(sequence):
        def read(*_, index=index, name=name, result=result):
            calls.append(name)
            if index == stage:
                raise error
            return result
        monkeypatch.setattr(reads, name, read)
    with pytest.raises(RuntimeError) as caught:
        reads.ready(1, 'camera', 20)
    assert caught.value is error
    assert calls == [name for name, _ in sequence[:stage + 1]]
