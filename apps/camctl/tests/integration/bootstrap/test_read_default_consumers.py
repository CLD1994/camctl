"""默认三入口在业务筛选前，用新连接核实原 READ 完整申请及读取机会释放。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.history.events import business_columns
from camctl.persistence.transaction import row_facts

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401


pytestmark = pytest.mark.asyncio


class _BeforeBusinessCandidates(Exception):
    """保存门禁已通过；本用例在新业务资格判定前结束该轮。"""


class _CandidateClock:
    def __init__(self, check):
        self.check = check

    def utc_micros(self):
        self.check()
        raise _BeforeBusinessCandidates


def _snapshot(owned, table, row_id):
    facts = row_facts(owned.connection, table, row_id)
    return {name: facts[name] for name in business_columns(table)}


def _read_saved(owned, ticket, ends, finishes, stage):
    from camctl.persistence.repositories.read_end import load_read_end

    inputs = finishes if stage == "finish" else ends
    assert len(inputs) == 2 and inputs[1] == inputs[0]
    assert len(ends) == (2 if stage == "end" else 1)
    assert len(finishes) == (2 if stage == "finish" else 0)
    if stage == "end":
        end = load_read_end(int(ticket.target_id), owned)
        assert end[0] == ticket and end[1] == ends[0][0].end
        assert end[2:] == (ends[0][0].occurred_at, None)
    assert owned.connection.execute(
        "SELECT t.status,t.error_json,r.status,r.attempts_used FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id WHERE r.id=? AND t.attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone() == (
            int(enum_for("operation_attempts.status").SUCCEEDED if stage == "finish" else enum_for("operation_attempts.status").RUNNING), None,
            int(enum_for("operation_runs.status").SUCCEEDED if stage == "finish" else enum_for("operation_runs.status").ACTIVE), ticket.attempt_id)
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
        (int(ticket.target_id),)).fetchone() == (None,)
    for _command, key in (ends[0], *finishes[:1]):
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("stage", ["finish", "end"])
async def test_default_entry_saves_read_before_business_candidates(pipeline, capture_resources, monkeypatch, entrance, after_commit, stage):
    from ..support.bootstrap_consumers import held_default_read

    world, ticket, ends, finishes = await held_default_read(pipeline, capture_resources, monkeypatch, stage, after_commit)
    owned = world.owned
    before_action = _snapshot(owned, "actions", 1)
    before_processing = _snapshot(owned, "recording_processing", 1)
    before_copy = _snapshot(owned, "file_copies", int(ticket.target_id))
    before_calls = tuple(world.driver.calls)
    try:
        await world.replay(entrance)
        _read_saved(owned, ticket, ends, finishes, stage)
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == before_calls
        assert world.tools() == []
        assert _snapshot(owned, "actions", 1) == before_action
        assert _snapshot(owned, "recording_processing", 1) == before_processing
        after_copy = _snapshot(owned, "file_copies", int(ticket.target_id))
        assert {**after_copy, "slot_device_id": before_copy["slot_device_id"],
            "read_end_event_id": before_copy["read_end_event_id"]} == before_copy
    finally:
        await world.close()
        owned.connection.close()


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
async def test_default_entry_confirms_committed_read_child_after_canceled_terminal(pipeline, capture_resources, monkeypatch, entrance):
    from ..support.bootstrap_consumers import held_default_read, cancel_original

    world, ticket, ends, finishes = await held_default_read(pipeline, capture_resources, monkeypatch, "end", True)
    owned = world.owned
    try:
        await cancel_original(world)
        before = {table: _snapshot(owned, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", int(ticket.target_id)),
            ("operation_runs", ticket.run_id))}
        history_count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        before_calls = tuple(world.driver.calls)
        await world.replay(entrance)
        _read_saved(owned, ticket, ends, finishes, "end")
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == before_calls
        assert world.tools() == []
        assert before == {table: _snapshot(owned, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", int(ticket.target_id)),
            ("operation_runs", ticket.run_id))}
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == history_count
    finally:
        await world.close()
        owned.connection.close()
