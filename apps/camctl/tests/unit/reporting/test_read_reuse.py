"""报告固定 H 的重复读取与失败汇总，只使用内存协作者。"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError
from camctl.history.bounded import SegmentHistoryHandle
from camctl.history.report_action import ReportHistoryHandle
from camctl.contracts.report_reads import ReportExpansionRequest
from camctl.contracts.segment_errors import SegmentErrorHeader
from camctl.contracts.segment_values import DefaultScopeState
from camctl.history.snapshots import SnapshotRow
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting import bounded_generation as generation
from camctl.reporting.encoding import ReportStream
from camctl.reporting.stream_validation import NodeGate, NodeValidationPlan


H = HistoryBoundary(2, 20)


def _row(table, identity, **values):
    return SnapshotRow(table, identity, {"id": identity, **values})


@pytest.mark.parametrize("target_type", [1, 3])
def test_cancel_result_carries_the_first_target_fact(monkeypatch, target_type):
    repo = create_autospec(HistoryRepository, instance=True)
    target = {"id": 7, "type": target_type}
    repo.restore_primary_at_h.return_value = target
    handle = create_autospec(SegmentHistoryHandle, instance=True)
    row = _row("cancel_items", 4, target_action_id=7)
    handle.related_rows.return_value = iter((row,))
    monkeypatch.setattr(generation, "_project", lambda *args: {"status": "completed"})

    [(identity, result)] = generation._result_rows(repo, handle, H, "cancel_task")

    assert identity == 4
    assert result.target is target
    assert result.row is row
    assert result.fragment == {"status": "completed"}
    repo.restore_primary_at_h.assert_called_once_with("action", 7, H)


def test_cancel_result_rejects_a_target_absent_at_h(monkeypatch):
    repo = create_autospec(HistoryRepository, instance=True)
    repo.restore_primary_at_h.return_value = None
    handle = create_autospec(SegmentHistoryHandle, instance=True)
    handle.related_rows.return_value = iter((_row("cancel_items", 4, target_action_id=7),))
    with pytest.raises(ConsistencyError):
        list(generation._result_rows(repo, handle, H, "cancel_task"))


@pytest.mark.parametrize("batch", [1, 256, 513])
def test_report_reader_forwards_the_event_batch_without_limiting_rows(batch):
    repo = create_autospec(HistoryRepository, instance=True)
    reader = generation._ReportReads(repo, batch)
    reader.restore_primary_at_h("action", 4, H)
    reader.restore_entity("output", 9, H)
    reader.related_primary_rows_at_h("delivery", "action_id", 4, H)
    reader.open_report_action(4, H)
    repo.restore_primary_at_h.assert_called_once_with("action", 4, H, event_batch_size=batch)
    repo.restore_report_entity.assert_called_once_with("output", 9, H, event_batch_size=batch)
    repo.restore_entity.assert_not_called()
    repo.related_primary_rows_at_h.assert_called_once_with(
        "delivery", "action_id", 4, H, event_batch_size=batch)
    repo.open_report_action.assert_called_once_with(4, H, event_batch_size=batch)


class MemoryFailures:
    """报告磁盘索引的有限页契约替身，不打开文件或数据库。"""

    def __init__(self):
        self.refs = {}
        self.seen = set()
        self.values = {}

    def add_reference(self, identity, dependency, output, source):
        if identity in self.refs:
            raise ConsistencyError("重复交付关联")
        self.refs[identity] = (dependency, output, source)

    def bind_delivery(self, identity, output):
        if identity not in self.refs or identity in self.seen:
            raise ConsistencyError("交付关联缺失或重复")
        dependency, expected, source = self.refs[identity]
        if output != expected:
            raise ConsistencyError("交付产物不同")
        self.seen.add(identity)
        return dependency, source

    def add_failure(self, dependency, identity, value):
        self.values[dependency, identity] = value

    def finish(self):
        if self.seen != self.refs.keys():
            raise ConsistencyError("条目引用缺少交付")

    def failures(self, dependency):
        for (owner, identity), value in sorted(self.values.items()):
            if owner == dependency:
                yield identity, value


def _memory_failures():
    fake = MemoryFailures()
    index = create_autospec(generation.FailureDeliveryIndex, instance=True, spec_set=True)
    for name in ("add_reference", "bind_delivery", "add_failure", "finish", "failures"):
        getattr(index, name).side_effect = getattr(fake, name)
    return index


@pytest.fixture
def failures(monkeypatch):
    repo = create_autospec(HistoryRepository, instance=True)
    handle = create_autospec(SegmentHistoryHandle, instance=True)
    handle.action_id = 8
    data = {
        "action_dependencies": [
            _row("action_dependencies", 2, action_id=8, depends_on_action_id=21),
            _row("action_dependencies", 10, action_id=8, depends_on_action_id=22),
        ],
        "obtain_source_selections": [
            _row("obtain_source_selections", 3, dependency_id=2),
            _row("obtain_source_selections", 4, dependency_id=10),
        ],
        "obtain_items": [
            _row("obtain_items", 1, selection_id=3, delivery_id=100, output_id=51),
            _row("obtain_items", 2, selection_id=3, delivery_id=2, output_id=52),
            _row("obtain_items", 3, selection_id=4, delivery_id=10, output_id=53),
        ],
    }
    deliveries = [
        (2, {"id": 2, "action_id": 8, "output_id": 52}),
        (10, {"id": 10, "action_id": 8, "output_id": 53}),
        (100, {"id": 100, "action_id": 8, "output_id": 51}),
    ]

    def rows(table, column=None, value=None):
        return iter(row for row in data[table]
                    if column is None or row.values[column] == value)

    handle.related_rows.side_effect = rows
    handle.row.side_effect = lambda table, identity: next(
        (row.values for row in data[table] if row.row_id == identity), None)
    repo.related_primary_rows_at_h.side_effect = lambda *args, **kwargs: iter(deliveries)
    repo.restore_primary_at_h.side_effect = lambda entity, identity, boundary: {
        "id": identity, "source_action_id": 22 if identity == 53 else 21}

    def project(kind, identity, facts):
        return {"kind": kind, "id": identity}

    monkeypatch.setattr(generation, "_project", project)
    return repo, handle, data, deliveries


def test_failure_union_reads_all_h_deliveries_once_in_registered_order(failures):
    repo, handle, _, _ = failures
    index = _memory_failures()

    values = list(generation._failure_rows(repo, handle, H, index))

    assert [position for position, _ in values] == [
        "2:0:3", "2:1:1", "2:1:2", "2:2:2", "2:2:100",
        "10:0:4", "10:1:3", "10:2:10",
    ]
    assert [value["id"] for _, value in values if value["kind"] == "delivery_failure"] == [2, 100, 10]
    repo.related_primary_rows_at_h.assert_called_once_with("delivery", "action_id", 8, H)
    assert repo.restore_primary_at_h.call_count == 3


def test_nonfailed_delivery_keeps_its_association_checks(failures, monkeypatch):
    repo, handle, _, _ = failures
    original = generation._project
    monkeypatch.setattr(generation, "_project", lambda kind, identity, facts:
        generation.OMIT if kind == "delivery_failure" and identity == 2
        else original(kind, identity, facts))

    values = list(generation._failure_rows(repo, handle, H, _memory_failures()))

    assert [position for position, _ in values if ":2:" in position] == ["2:2:100", "10:2:10"]
    assert repo.restore_primary_at_h.call_count == 3


@pytest.mark.parametrize("damage", [
    "duplicate", "missing_selection", "missing_dependency", "foreign_action",
    "missing_delivery", "orphan_delivery", "duplicate_delivery", "foreign_delivery",
    "missing_output_reference", "wrong_output", "missing_output", "foreign_output",
])
def test_failure_union_rejects_broken_h_associations(failures, damage):
    repo, handle, data, deliveries = failures
    if damage == "duplicate":
        data["obtain_items"].append(_row("obtain_items", 4, selection_id=4, delivery_id=2, output_id=52))
    elif damage == "missing_selection":
        data["obtain_source_selections"].pop()
    elif damage == "missing_dependency":
        data["action_dependencies"].pop()
    elif damage == "foreign_action":
        data["action_dependencies"][0].values["action_id"] = 99
    elif damage == "missing_delivery":
        deliveries.pop()
    elif damage == "orphan_delivery":
        deliveries.append((101, {"id": 101, "action_id": 8, "output_id": 51}))
    elif damage == "duplicate_delivery":
        deliveries.append(deliveries[0])
    elif damage == "foreign_delivery":
        deliveries[0][1]["action_id"] = 99
    elif damage == "missing_output_reference":
        data["obtain_items"][0].values["output_id"] = None
    elif damage == "wrong_output":
        deliveries[0][1]["output_id"] = 999
    elif damage == "missing_output":
        repo.restore_primary_at_h.side_effect = None
        repo.restore_primary_at_h.return_value = None
    else:
        repo.restore_primary_at_h.side_effect = lambda *args: {"source_action_id": 99}

    with pytest.raises(ConsistencyError):
        list(generation._failure_rows(repo, handle, H, _memory_failures()))


def _writer(monkeypatch, *, kind):
    repo = create_autospec(HistoryRepository, instance=True)
    handle = create_autospec(ReportHistoryHandle, instance=True)
    handle.action_id = 8
    handle.__enter__.return_value = handle
    repo.open_report_action.return_value = handle
    plan = create_autospec(NodeValidationPlan, instance=True)
    gate = create_autospec(NodeGate, instance=True)
    plan.begin.return_value = gate
    gate.item_plan.return_value = plan
    gate.state.return_value = SimpleNamespace(value="undecided")
    stream = create_autospec(ReportStream, instance=True)
    monkeypatch.setattr(generation, "_action_facts", lambda *args: {"actions": {8: {"status": 3}}})
    monkeypatch.setattr(generation, "_expansion_arrays", lambda *args: {})
    monkeypatch.setattr(generation, "_coverage_arrays", lambda *args: {})
    monkeypatch.setattr(generation, "projection_structure", lambda *args:
                        SimpleNamespace(entity_fields=()))
    project = create_autospec(generation._project)
    project.side_effect = lambda name, identity, facts, **kwargs: (
        {"type": kind, "result": {}} if name == "action" else {"status": "completed"})
    monkeypatch.setattr(generation, "_project", project)
    return repo, handle, plan, gate, stream, project


@pytest.mark.parametrize("target_type", [1, 3])
def test_cancel_writer_uses_first_target_and_preserves_withdrawals(monkeypatch, target_type):
    repo, handle, plan, gate, stream, project = _writer(monkeypatch, kind="cancel_task")
    repo.restore_primary_at_h.return_value = {"id": 7, "type": target_type}
    row = _row("cancel_items", 4, target_action_id=7)
    withdrawal = _row("cancel_delivery_items", 5, cancel_item_id=4)
    handle.related_rows.side_effect = lambda table, *args: iter(
        (row,) if table == "cancel_items" else (withdrawal,))

    list(generation._write_action(repo, object(), stream, plan, object(), 8, H))

    repo.restore_primary_at_h.assert_called_once_with("action", 7, H)
    withdrawals = [call for call in project.call_args_list if call.args[0] == "withdrawal"]
    assert len(withdrawals) == (target_type == 3)
    handle.__exit__.assert_called_once()


def test_failed_failure_preparation_is_owned_by_action_exit_stack(monkeypatch):
    repo, handle, plan, _, stream, _ = _writer(monkeypatch, kind="obtain_action_outputs")
    index = create_autospec(generation.FailureDeliveryIndex, instance=True)
    index.__enter__.return_value = index
    monkeypatch.setattr(generation, "FailureDeliveryIndex", lambda: index)
    fault = ConsistencyError("broken H relation")
    handle.related_rows.side_effect = fault

    with pytest.raises(ConsistencyError) as caught:
        list(generation._write_action(repo, object(), stream, plan, object(), 8, H))

    assert caught.value is fault
    index.__exit__.assert_called_once()
    handle.__exit__.assert_called_once()


@pytest.mark.parametrize("source_id,existing_boundary,reused", [
    (8, H, True), (21, H, False), (8, HistoryBoundary(2, 19), False),
])
def test_segment_expansion_reuses_only_its_source_at_the_same_h(
    monkeypatch, source_id, existing_boundary, reused
):
    repo = create_autospec(HistoryRepository, instance=True)
    current = create_autospec(ReportHistoryHandle, instance=True)
    current.action_id, current.boundary = 8, existing_boundary
    current.__enter__.return_value = current
    other = create_autospec(ReportHistoryHandle, instance=True)
    other.__enter__.return_value = other
    repo.open_report_action.return_value = other
    growth = create_autospec(ReportHistoryHandle, instance=True)
    growth.__enter__.return_value = growth
    for handle in (current, other):
        handle.primary.return_value = {"type": 2, "execution_started": True, "status": 3}
        handle.active_result_ref.return_value = object()
        handle.open_expansion.return_value = growth
    header = SegmentErrorHeader(source_id, 0, DefaultScopeState.UNKNOWN, None, 0)
    request = ReportExpansionRequest('segment_error', source_id, header)
    validate = create_autospec(generation.report_expansion_request, return_value=request)
    monkeypatch.setattr(generation, "report_expansion_request", validate)
    error = {"code": "default_segment_scope_empty", "details": {}}

    with ExitStack() as stack:
        stack.enter_context(current)
        expansion = generation._SegmentExpansion(
            repo, H, object(), error, stack, source_handle=current)
        assert expansion.handle is growth
        (current if reused else other).open_expansion.assert_called_once_with(request)
        expansion.handle.read_result.assert_called_once_with(expansion.ref)

    validate.assert_called_once_with('error', error)
    current.__exit__.assert_called_once()
    if reused:
        repo.open_report_action.assert_not_called()
        other.__exit__.assert_not_called()
    else:
        repo.open_report_action.assert_called_once_with(source_id, H)
        other.__exit__.assert_called_once()


def test_action_writer_reuses_the_open_source_for_segment_error(monkeypatch):
    repo, handle, plan, _, stream, project = _writer(monkeypatch, kind="record_video")
    handle.boundary = H
    handle.primary.return_value = {"type": 2, "execution_started": True, "status": 3}
    handle.active_result_ref.return_value = object()
    project.side_effect = lambda *args, **kwargs: {
        "type": "record_video", "error": {
            "code": "default_segment_scope_empty", "details": {}}}
    header = SegmentErrorHeader(8, 0, DefaultScopeState.UNKNOWN, None, 0)
    monkeypatch.setattr(generation, "report_expansion_request", lambda kind, value, **kwargs:
                        ReportExpansionRequest('segment_error', 8, header) if value else None)

    list(generation._write_action(repo, object(), stream, plan, object(), 8, H))

    repo.open_report_action.assert_called_once_with(8, H)
    handle.__enter__.assert_called_once()
    handle.__exit__.assert_called_once()


@pytest.mark.parametrize("source_type,has_result", [(1, True), (2, False)])
def test_reused_segment_source_keeps_source_type_and_published_set_checks(
    monkeypatch, source_type, has_result
):
    repo = create_autospec(HistoryRepository, instance=True)
    handle = create_autospec(SegmentHistoryHandle, instance=True)
    handle.action_id, handle.boundary = 8, H
    handle.primary.return_value = {"type": source_type, "execution_started": True, "status": 3}
    handle.active_result_ref.return_value = object() if has_result else None
    header = SegmentErrorHeader(8, 0, DefaultScopeState.UNKNOWN, None, 0)
    monkeypatch.setattr(generation, "report_expansion_request", lambda *args:
                        ReportExpansionRequest('segment_error', 8, header))

    with ExitStack() as stack, pytest.raises(ConsistencyError):
        generation._SegmentExpansion(repo, H, object(), {
            "code": "default_segment_scope_empty", "details": {}}, stack,
            source_handle=handle)

    repo.open_report_action.assert_not_called()
