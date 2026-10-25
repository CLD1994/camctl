"""动作句柄的行批次与事件批次相互独立，不打开真实数据库。"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.history.bounded import SegmentHistoryHandle
from camctl.persistence.repositories import history


@pytest.mark.parametrize("batch", [1, 256, 513])
@pytest.mark.parametrize("forward", [False, True])
def test_handle_event_pages_use_the_event_capacity_and_keep_event_stats(batch, forward):
    handle = object.__new__(SegmentHistoryHandle)
    handle.repository = create_autospec(history.HistoryRepository, instance=True)
    handle.seed = SimpleNamespace(ref=(1, 7))
    handle.batch_size = 2
    handle.event_batch_size = batch
    handle.stats = {"events": 0}
    ids = list(range(1, 260))
    if not forward:
        ids.reverse()

    def page(ref, lower, upper, previous, count, *, forward, batch_size, remaining):
        selected = [identity for identity in ids
                    if (identity > previous if forward else identity < previous)]
        return tuple(SimpleNamespace(event_id=identity) for identity in selected[:min(batch_size, remaining)])

    handle.repository._object_event_page.side_effect = page
    first, end = (1, 259) if forward else (259, 1)
    restored = list(handle._events(HistoryBoundary(0, 0), HistoryBoundary(259, 259), first, end, forward))

    assert [event.event_id for event in restored] == ids
    assert handle.stats["events"] == 259
    assert len(handle.repository._object_event_page.call_args_list) == (259 + batch - 1) // batch
    assert all(call.kwargs["batch_size"] == batch for call in handle.repository._object_event_page.call_args_list)


@pytest.mark.parametrize("batch", [1, 256, 513])
def test_open_segment_history_accepts_separate_event_capacity(monkeypatch, batch):
    repo = create_autospec(history.HistoryRepository, instance=True)
    repo._read_connection.return_value = nullcontext(object())
    seed = SimpleNamespace(snapshot=None, floor=(1, 1), head=(1, 1))
    repo._object_seed.return_value = seed
    factory = create_autospec(SegmentHistoryHandle)
    monkeypatch.setattr("camctl.history.bounded.SegmentHistoryHandle", factory)

    result = history.HistoryRepository.open_segment_history(
        repo, 7, HistoryBoundary(1, 1), batch_size=2, event_batch_size=batch)

    assert result is factory.return_value.restore.return_value
    assert repo._object_seed.call_args.kwargs["event_batch_size"] == batch
    factory.assert_called_once_with(repo, seed, 2, event_batch_size=batch)


@pytest.mark.parametrize("batch", [0, -1, True, 1.5, "256"])
def test_invalid_event_capacity_is_rejected_before_any_read(batch):
    repo = create_autospec(history.HistoryRepository, instance=True)
    with pytest.raises(ValueError):
        history.HistoryRepository.open_segment_history(
            repo, 7, HistoryBoundary(1, 1), event_batch_size=batch)
    repo._read_connection.assert_not_called()


@pytest.mark.parametrize("batch", [1, 256, 513])
def test_related_primary_pages_forward_event_capacity_to_each_row(monkeypatch, batch):
    import sqlite3

    repo = create_autospec(history.HistoryRepository, instance=True)
    connection = create_autospec(sqlite3.Connection, instance=True)
    first = create_autospec(sqlite3.Cursor, instance=True)
    first.fetchmany.return_value = [(7,), (9,)]
    last = create_autospec(sqlite3.Cursor, instance=True)
    last.fetchmany.return_value = []
    connection.execute.side_effect = [first, last]
    repo._read_connection.side_effect = lambda: nullcontext(connection)
    repo.restore_primary_at_h.side_effect = lambda entity, identity, boundary, **kwargs: {"id": identity}
    monkeypatch.setattr(history, "load_enum_registry", lambda: {
        "history_objects": {"delivery": {"table": "deliveries", "id": 2}}})
    monkeypatch.setattr(history, "load_event_registry", lambda: {
        "tables": {"deliveries": {"immutable": ["action_id"]}}})

    values = list(history.HistoryRepository.related_primary_rows_at_h(
        repo, "delivery", "action_id", 4, HistoryBoundary(1, 1),
        batch_size=2, event_batch_size=batch))

    assert values == [(7, {"id": 7}), (9, {"id": 9})]
    assert [call.kwargs for call in repo.restore_primary_at_h.call_args_list] == [
        {"event_batch_size": batch}, {"event_batch_size": batch}]
    first.close.assert_called_once()
    last.close.assert_called_once()
