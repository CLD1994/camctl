"""默认启动入口保留未检查的受理时间及不可变旧历史。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.contracts.history_values import HistoryBoundary
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.history import HistoryRepository
from camctl.session.locks import probe_admission

from ..acceptance.test_acceptance import Catalog

pytestmark = pytest.mark.asyncio

_INPUT_AT = 1_750_000_000_000_000
_CHECK_AT = _INPUT_AT + 1_000_000
_SUBMIT_AT = _INPUT_AT + 2_000_000
_REPLAY_INPUT_AT = _INPUT_AT + 3_000_000
_REPLAY_CHECK_AT = _INPUT_AT + 4_000_000
_RESTRICTED_INPUT_AT = _INPUT_AT + 5_000_000


class ControlledClock:
    """给受理、启动检查和后续调用提供可复现墙钟读数。"""

    def __init__(self, *readings: int) -> None:
        if not readings:
            raise ValueError("至少需要一个时钟读数")
        self.readings = list(readings)
        self.last = readings[-1]

    def utc_micros(self) -> int:
        if self.readings:
            self.last = self.readings.pop(0)
        return self.last

    @staticmethod
    def monotonic_ns() -> int:
        return time.monotonic_ns()


class FileReader:
    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


class FlowCounter:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, context) -> None:
        self.calls += 1


def _config_for(home: Path, *, min_plausible_date: str = "2025-01-01"):
    home.mkdir(parents=True, exist_ok=True)
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "clock": {
                "min_plausible_date": min_plausible_date,
                "recheck_delay_s": "0.01",
                "recheck_count": 1,
            },
        },
        ConfigDefaults(),
    )


def _initialize(cfg) -> Path:
    path = Path(cfg.paths.state_db)
    result = initialize_state(cfg, path)
    assert result.outcome is InitOutcome.CREATED
    return path


def _input_body(request_id: str, *, invalid_photo: bool = False) -> dict:
    if invalid_photo:
        actions = [
            {
                "name": "bad-shots",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2099-01-01 00:00:00",
                "params": {"type": "single_shot", "shots": 99},
                "policy": {"max_delay_ms": 1000},
            }
        ]
    else:
        actions = [
            {
                "name": "later-report",
                "type": "report_status",
                "scheduled_at": "2099-01-01 00:00:00",
                "params": {"scope": "full"},
            }
        ]
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": actions,
    }


async def _parsed(tmp_path: Path, body: dict, label: str):
    target = tmp_path / f"{label}.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    return parse_input(await read_input(str(target), FileReader()))


def _scalar(database: Path, sql: str, *params):
    with sqlite3.connect(database) as connection:
        return connection.execute(sql, params).fetchone()


def _transaction_at(database: Path, occurred_at: int) -> int:
    rows = _rows(
        database,
        "SELECT DISTINCT transaction_id FROM history_events"
        " WHERE occurred_at = ? AND event_type = 1",
        (occurred_at,),
    )
    assert len(rows) == 1
    return rows[0][0]


def _rows(database: Path, sql: str, params=()):
    with sqlite3.connect(database) as connection:
        return connection.execute(sql, params).fetchall()


def _transaction_events(database: Path, transaction_id: int):
    return _rows(
        database,
        "SELECT * FROM history_events WHERE transaction_id = ? ORDER BY id",
        (transaction_id,),
    )


def _assert_unchecked_transaction(
    database: Path, transaction_id: int, occurred_at: int
) -> list[tuple]:
    events = _transaction_events(database, transaction_id)
    assert events
    assert [row[4] for row in events] == [occurred_at] * len(events)
    assert [row[5] for row in events] == [1] * len(events)
    transaction = _scalar(
        database,
        "SELECT first_event_id, last_event_id FROM history_transactions WHERE id = ?",
        transaction_id,
    )
    assert [row[0] for row in events] == list(
        range(transaction[0], transaction[1] + 1)
    )
    return events


def _row_dict(connection, table: str, row_id: int) -> dict:
    cursor = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    values = cursor.fetchone()
    assert values is not None
    return {
        name: json.loads(value) if name.endswith("_json") and value else value
        for name, value in zip(
            (column[0] for column in cursor.description), values
        )
    }


async def _wait_for_admission(
    task: asyncio.Task,
    database: Path,
    admission_lock: Path,
    expected_bound: int,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 8
    while loop.time() < deadline:
        if task.done():
            result = task.result()
            raise AssertionError(f"run 在取得正常接纳资格前结束: {result!r}")
        bound = _scalar(
            database,
            "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
        )[0]
        if bound == expected_bound and not probe_admission(admission_lock).is_free:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run 未保存可信下界并取得接纳锁")


async def _execute_once(
    mode: CommandMode,
    cfg,
    source,
    clock: ControlledClock,
    monkeypatch,
    **kwargs,
):
    monkeypatch.setattr(lifecycle, "SystemClock", lambda: clock)
    deps = build_runtime(mode, cfg, catalog=Catalog())
    try:
        return await execute_command(deps, source, **kwargs)
    finally:
        close_runtime(deps)


async def test_submit_acceptance_never_advances_clock_bound(
    tmp_path: Path, monkeypatch
) -> None:
    no_bound_cfg = _config_for(tmp_path / "no-bound")
    no_bound_db = _initialize(no_bound_cfg)
    no_bound_source = await _parsed(
        tmp_path, _input_body("101", invalid_photo=True), "submit-no-bound"
    )
    no_bound_result = await _execute_once(
        CommandMode.SUBMIT,
        no_bound_cfg,
        no_bound_source,
        ControlledClock(_INPUT_AT),
        monkeypatch,
    )
    assert no_bound_result.succeeded is True
    assert _scalar(
        no_bound_db,
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
    ) == (None,)
    no_bound_txn = _transaction_at(no_bound_db, _INPUT_AT)
    _assert_unchecked_transaction(no_bound_db, no_bound_txn, _INPUT_AT)

    existing_cfg = _config_for(tmp_path / "existing-bound")
    existing_db = _initialize(existing_cfg)
    established = await _execute_once(
        CommandMode.RUN,
        existing_cfg,
        None,
        ControlledClock(_CHECK_AT),
        monkeypatch,
        flows={},
        restricted_flows={},
    )
    assert established.succeeded is True
    assert _scalar(
        existing_db,
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
    ) == (_CHECK_AT,)

    later_source = await _parsed(
        tmp_path, _input_body("102", invalid_photo=True), "submit-existing-bound"
    )
    later_submit = await _execute_once(
        CommandMode.SUBMIT,
        existing_cfg,
        later_source,
        ControlledClock(_SUBMIT_AT),
        monkeypatch,
    )
    assert later_submit.succeeded is True
    assert _scalar(
        existing_db,
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
    ) == (_CHECK_AT,)
    later_txn = _transaction_at(existing_db, _SUBMIT_AT)
    _assert_unchecked_transaction(existing_db, later_txn, _SUBMIT_AT)


async def test_run_accepts_before_successful_clock_check(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _config_for(tmp_path / "normal-run")
    database = _initialize(cfg)
    run_source = await _parsed(tmp_path, _input_body("201"), "run-first-input")
    next_submit_source = await _parsed(
        tmp_path, _input_body("202"), "submit-during-run"
    )

    clock_slot = {"current": ControlledClock(_INPUT_AT, _CHECK_AT)}
    monkeypatch.setattr(lifecycle, "SystemClock", lambda: clock_slot["current"])
    run_deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    run_task = asyncio.create_task(
        execute_command(
            run_deps,
            run_source,
            flows={},
            restricted_flows={},
            poll_interval_s=0.05,
        )
    )
    submit_deps = None
    try:
        await _wait_for_admission(
            run_task, database, run_deps.admission_lock, _CHECK_AT
        )
        run_txn = _transaction_at(database, _INPUT_AT)
        run_events = _assert_unchecked_transaction(database, run_txn, _INPUT_AT)
        assert [row[2] for row in run_events] == [2, 1]
        trusted_time_event_id = _scalar(
            database,
            "SELECT trusted_time_event_id FROM runtime_state WHERE id = 1",
        )[0]
        assert _rows(
            database,
            "SELECT event_type, occurred_at, clock_status FROM history_events"
            " WHERE id = ?",
            (trusted_time_event_id,),
        ) == [(31, _CHECK_AT, 2)]

        # run 已检查并持有正常接纳资格时，submit 仍独立记录未检查读数。
        clock_slot["current"] = ControlledClock(_SUBMIT_AT)
        submit_deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=Catalog())
        submit_result = await execute_command(submit_deps, next_submit_source)
        assert submit_result.succeeded is True
        submit_txn = _transaction_at(database, _SUBMIT_AT)
        submit_events = _assert_unchecked_transaction(
            database, submit_txn, _SUBMIT_AT
        )
        assert [row[2] for row in submit_events] == [2, 1]
        assert _scalar(
            database,
            "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
        ) == (_CHECK_AT,)
    finally:
        if submit_deps is not None:
            close_runtime(submit_deps)
        if not run_task.done():
            run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        close_runtime(run_deps)


async def test_run_acceptance_survives_invalid_clock(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _config_for(
        tmp_path / "invalid-clock", min_plausible_date="2100-01-01"
    )
    database = _initialize(cfg)
    source = await _parsed(tmp_path, _input_body("301"), "run-invalid-clock")
    regular_flow = FlowCounter()

    result = await _execute_once(
        CommandMode.RUN,
        cfg,
        source,
        ControlledClock(_INPUT_AT),
        monkeypatch,
        flows={"ordinary-device": regular_flow},
        restricted_flows={},
    )

    assert result.succeeded is False
    assert result.reason == "clock_invalid"
    assert regular_flow.calls == 0
    assert _scalar(
        database,
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
    ) == (None,)
    txn = _transaction_at(database, _INPUT_AT)
    events = _assert_unchecked_transaction(database, txn, _INPUT_AT)
    assert [row[2] for row in events] == [2, 1]
    assert _scalar(database, "SELECT status FROM actions WHERE id = 1") == (1,)


async def test_later_run_does_not_rewrite_acceptance_history(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _config_for(tmp_path / "later-runs")
    database = _initialize(cfg)
    source_body = _input_body("401", invalid_photo=True)
    submit_source = await _parsed(tmp_path, source_body, "history-submit")
    submitted = await _execute_once(
        CommandMode.SUBMIT,
        cfg,
        submit_source,
        ControlledClock(_INPUT_AT),
        monkeypatch,
    )
    assert submitted.succeeded is True

    transaction_id = _transaction_at(database, _INPUT_AT)
    acceptance_events = _transaction_events(database, transaction_id)
    transaction = _scalar(
        database,
        "SELECT id, last_event_id FROM history_transactions WHERE id = ?",
        transaction_id,
    )
    boundary = HistoryBoundary(transaction[0], transaction[1])
    with sqlite3.connect(database) as connection:
        plan_id = connection.execute(
            "SELECT id FROM plans WHERE request_id = 401"
        ).fetchone()[0]
        action_id = connection.execute(
            "SELECT id FROM actions WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]
        expected_plan = _row_dict(connection, "plans", plan_id)
        expected_action = _row_dict(connection, "actions", action_id)

    reused_source = await _parsed(tmp_path, source_body, "history-normal-run")
    normal = await _execute_once(
        CommandMode.RUN,
        cfg,
        reused_source,
        ControlledClock(_REPLAY_INPUT_AT, _REPLAY_CHECK_AT),
        monkeypatch,
        poll_interval_s=0.01,
    )
    assert normal.succeeded is True
    assert _transaction_events(database, transaction_id) == acceptance_events
    normal_bound = _scalar(
        database,
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
    )
    assert normal_bound == (_REPLAY_CHECK_AT,)

    restricted_source = await _parsed(
        tmp_path, source_body, "history-restricted-run"
    )
    restricted_cfg = _config_for(
        Path(cfg.paths.state_db).parent, min_plausible_date="2100-01-01"
    )
    restricted = await _execute_once(
        CommandMode.RUN,
        restricted_cfg,
        restricted_source,
        ControlledClock(_RESTRICTED_INPUT_AT),
        monkeypatch,
    )
    assert restricted.succeeded is False
    assert restricted.reason == "clock_invalid"
    assert _scalar(
        database,
        "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1",
    ) == normal_bound
    assert _transaction_events(database, transaction_id) == acceptance_events

    history = HistoryRepository(database)
    restored_plan = history.restore_entity("plan", plan_id, boundary)
    restored_action = history.restore_entity("action", action_id, boundary)
    assert restored_plan[("plans", plan_id)] == expected_plan
    assert restored_action[("actions", action_id)] == expected_action
