"""没有新调用结果的 START 完整收场在保存失败后仍由会话持有。"""

from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .test_capture_contract import _NOW
from .test_record_media_result_settlement import _TrackedCommitFailure
from .test_recording_start_runtime import (
    StartDriver, StateQuery, _configured, _result, _world,
)


pytestmark = pytest.mark.asyncio


async def start_close_world(tmp_path, mode):
    """真实调用已经保存；下一次推进只形成原 START 的复合收场。

    query_exhausted 保存一次失败查询后降低本次查询上限。自然用完
    原查询上限的最后结果由 finish_start_result 收场，不经过 close_start。
    """
    if mode not in ("unknown", "lowered", "query_exhausted"):
        raise ValueError(f"未知启动收场场景: {mode}")
    owned = _world(tmp_path)
    try:
        wall, mono = [_NOW], [7_000_000_000]
        driver = StartDriver(_result(
            no_effect=mode == "lowered",
            error=ErrorValue("device_busy", "device") if mode == "lowered"
            else ErrorValue("transport_timeout", "transport")))
        runtime = _configured(owned, driver, wall=wall, mono=mono, maximum=3)
        query = StateQuery() if mode == "query_exhausted" else None
        runtime.state_query = query
        handler = capture_handler("camera_record")
        await handler(12, runtime)
        if query is not None:
            await handler(12, runtime)
            assert len(query.calls) == 1
            assert owned.connection.execute(
                "SELECT status,attempts_used FROM operation_runs"
                " WHERE responsibility_key='query/start/12/12'"
            ).fetchone() == (2, 1)
            runtime.query_config = replace(runtime.query_config, max_attempts=1)
        if mode == "lowered":
            runtime.start_config = replace(runtime.start_config, max_attempts=1)
        assert len(driver.calls) == 1
        assert runtime.action(12)["status"] == 2
        assert runtime.pending_start_results == runtime.pending_capture_completions == {}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE status=1 OR result_event_id IS NULL"
        ).fetchone() == (0,)
        wall[0] = _NOW + 5_000_000
        mono[0] += 5_000_000_000
        return SimpleNamespace(
            owned=owned, runtime=runtime, driver=driver, query=query, wall=wall, mono=mono,
            path=Path(tmp_path) / "state.db", metadata=owned.metadata,
            formed_at=wall[0], action_id=12)
    except BaseException:
        owned.connection.close()
        raise


class _ProjectionFailure:
    """完整收场的真实动作投影写入后报错，事务必须撤销全部变化。"""

    def __init__(self, connection):
        self.connection = connection
        self.failed = False
        self.rollback_calls = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        result = self.connection.execute(sql, parameters)
        if sql == "ROLLBACK" and self.failed:
            self.rollback_calls += 1
        if not self.failed and sql.startswith("UPDATE actions SET"):
            self.failed = True
            raise sqlite3.OperationalError("启动收场动作投影保存失败")
        return result


def _attempts(owned):
    return owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()


def _history(owned):
    return owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()


def _assert_complete_group(world, owned, finish, key):
    events = saved_transaction_events(owned.connection, key)
    assert events is not None
    assert {event["occurred_at"] for event in events} == {world.formed_at}
    assert len({event["transaction"].txn_id for event in events}) == 1
    changes = {(row["table"], row["id"]): row["after"]["values"]
               for event in events for row in event["body"]["rows"]}
    expected_start = 4 if world.mode == "lowered" else 6
    for responsibility in finish.responsibility_keys:
        run = owned.connection.execute(
            "SELECT id,status,retry_wait_required FROM operation_runs WHERE responsibility_key=?",
            (responsibility,)).fetchone()
        if run is None:
            # 无查询端口的请求包含固定查询身份，但不补造不存在的查询。
            assert world.mode == "unknown" and responsibility == "query/start/12/12"
            continue
        assert run[1:] == (expected_start, 0)
        assert changes[("operation_runs", run[0])]["status"] == expected_start
    assert changes[("actions", 12)]["status"] == 4
    assert changes[("plans", 1)]["status"] == 3
    assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (4,)
    assert owned.connection.execute("SELECT status FROM plans WHERE id=1").fetchone() == (3,)
    occupancy = 2 if world.mode == "lowered" else 1
    assert owned.connection.execute(
        "SELECT occupancy_state FROM device_activities WHERE action_id=12").fetchone() == (occupancy,)
    if world.mode == "lowered":
        assert changes[("device_activities", 12)]["occupancy_state"] == 2
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)
    ).fetchone() == (1,)
    return events


async def _assert_save_recovery(tmp_path, monkeypatch, mode, phase):
    world = await start_close_world(tmp_path, mode)
    world.mode = mode
    owned, runtime = world.owned, world.runtime
    actual_save = CaptureRepository.close_start
    inputs, receipts, faults = [], [], []
    before_attempts, before_history = _attempts(owned), _history(owned)
    before_dump = tuple(owned.connection.iterdump())
    before_query_calls = 0 if world.query is None else len(world.query.calls)

    def save(repository, finish, action_finish, key, current):
        inputs.append((finish, action_finish, key))
        if len(inputs) == 1:
            fault = (_ProjectionFailure(current.connection) if phase == "projection"
                     else _TrackedCommitFailure(current.connection, phase == "commit_after"))
            faults.append(fault)
            current = replace(current, connection=fault)
        receipt = actual_save(repository, finish, action_finish, key, current)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(CaptureRepository, "close_start", save)
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        finish, action_finish, key = inputs[0]
        assert finish.occurred_at == action_finish.occurred_at == world.formed_at
        expected_keys = ("start/12",) if mode == "lowered" else (
            "start/12", "query/start/12/12")
        assert finish.responsibility_keys == expected_keys
        if phase == "projection":
            assert faults[0].failed and faults[0].rollback_calls == 1
            assert receipts[0].kind is DbOutcomeKind.ROLLED_BACK
            assert tuple(owned.connection.iterdump()) == before_dump
        else:
            assert faults[0].commit_calls == 1
            assert receipts[0].kind is DbOutcomeKind.UNKNOWN
        held = runtime.pending_capture_completions
        anchors = dict(runtime.retry_gate.anchors)

        # 关闭原连接排除迟到提交，只从新可靠连接确认原键与实际状态。
        owned.connection.close()
        owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == world.metadata
        first_events = saved_transaction_events(owned.connection, key)
        after = phase == "commit_after"
        assert (first_events is not None) is after
        assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (
            4 if after else 2,)
        if not after:
            assert tuple(owned.connection.iterdump()) == before_dump
        pending = held[12]
        assert pending.request.finish is finish
        assert pending.request.action_finish is action_finish
        assert pending.key == key
        runtime.owned = owned
        runtime.start_config = replace(runtime.start_config, max_attempts=9)
        runtime.query_config = replace(runtime.query_config, max_attempts=9)
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        await capture_handler("camera_record")(12, runtime)

        assert inputs == [(finish, action_finish, key), (finish, action_finish, key)]
        assert inputs[1][0] is finish and inputs[1][1] is action_finish
        assert receipts[1].kind is DbOutcomeKind.COMPLETED and receipts[1].value is None
        assert runtime.pending_capture_completions is held and held == {}
        assert runtime.retry_gate.anchors == {
            responsibility: anchor for responsibility, anchor in anchors.items()
            if responsibility not in finish.responsibility_keys}
        events = _assert_complete_group(world, owned, finish, key)
        if first_events is not None:
            assert events == first_events
        assert _attempts(owned) == before_attempts
        assert _history(owned)[:len(before_history)] == before_history
        assert len(world.driver.calls) == 1
        assert (0 if world.query is None else len(world.query.calls)) == before_query_calls
        final_dump = tuple(owned.connection.iterdump())
        await capture_handler("camera_record")(12, runtime)
        assert tuple(owned.connection.iterdump()) == final_dump and len(inputs) == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["unknown", "lowered", "query_exhausted"])
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_unknown_start_close_keeps_full_request_until_original_key_is_confirmed(
        tmp_path, monkeypatch, mode, after_commit):
    await _assert_save_recovery(
        tmp_path, monkeypatch, mode, "commit_after" if after_commit else "commit_before")


@pytest.mark.parametrize("mode", ["unknown", "lowered"])
async def test_rolled_back_start_close_preserves_whole_request_and_original_time(
        tmp_path, monkeypatch, mode):
    await _assert_save_recovery(tmp_path, monkeypatch, mode, "projection")
