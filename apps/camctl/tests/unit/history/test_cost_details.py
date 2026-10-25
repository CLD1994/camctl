"""事务内历史成本只聚合数字，保留原返回、异常及上下文边界。"""

from contextvars import Context
from dataclasses import dataclass
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history import decoding, validators
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.logging_runtime.performance import performance_logging
from camctl.logging_runtime.service import LogChannel
from camctl.logging_runtime import transaction_timing as timing
from camctl.persistence.runtime import OwnedConnection


@dataclass
class _Clock:
    wall: int = 0
    cpu: int = 0

    def spend(self, wall_ms, cpu_ms):
        self.wall += wall_ms * 1_000_000
        self.cpu += cpu_ms * 1_000_000


@pytest.fixture
def history(mocker):
    event = EventEnvelope(1, 1, 1, 1, 0, 2, 1, 1, {}, (
        RowChange("plans", 1, RowImage(False, {}),
            RowImage(True, {"request_id": 42, "name": "p", "created_at": 0, "status": 1})),
    ))
    row = (1, 1, 1, 1, 0, 2, 1, json.dumps({
        "reason": 1, "evidence": {}, "rows": [{
            "table": "plans", "id": 1, "before": {"exists": False, "values": {}},
            "after": {"exists": True, "values": dict(event.rows[0].after.values)},
        }],
    }))
    context = validators.EventContext(TransactionRange(1, 1, 1),
        {("plans", 1): ("plan", 1)}, {"plans": {}})
    mocker.patch.dict(validators.NAMED_GUARDS, {
        "admission": lambda event, context: None,
        "plan_aggregate": lambda event, context: None,
    })
    return event, row, context


@pytest.fixture
def clock(mocker):
    clock = _Clock()
    mocker.patch.object(timing, "monotonic_ns", side_effect=lambda: clock.wall)
    mocker.patch.object(timing, "thread_time_ns", side_effect=lambda: clock.cpu)
    parse = decoding.parse_exact_json
    structure_tail = validators.validate_host_photo_event_structure

    def parse_json(value):
        clock.spend(2, 1)
        return parse(value)

    def check_host_photo(*args, **kwargs):
        clock.spend(3, 2)
        return structure_tail(*args, **kwargs)

    mocker.patch.object(decoding, "parse_exact_json", autospec=True, side_effect=parse_json)
    mocker.patch.object(validators, "validate_host_photo_event_structure", autospec=True,
        side_effect=check_host_photo)
    return clock


def _transaction(work, key="outer"):
    @timing.trace_transaction
    def execute(command, operation_key, owned):
        return SimpleNamespace(kind="completed", value=work())

    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    return execute(SimpleNamespace(), key, OwnedConnection(connection, None))


def _summaries(channel):
    return [json.loads(call.args[0].message.removeprefix("性能计时 "))
        for call in channel.slog.call_args_list]


@pytest.mark.parametrize("entry", ["decode", "structure", "event"])
def test_without_transaction_history_calls_keep_results_without_clocks_or_logs(mocker, history, entry):
    event, row, context = history
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    wall = mocker.patch.object(timing, "monotonic_ns", side_effect=AssertionError("unused clock"))
    cpu = mocker.patch.object(timing, "thread_time_ns", side_effect=AssertionError("unused clock"))
    with performance_logging(channel):
        if entry == "decode":
            factory = mocker.patch.object(decoding, "EventEnvelope", autospec=True, return_value=event)
            assert decoding.decode_event_row(row) is event
            factory.assert_called_once()
        elif entry == "structure":
            result = validators.validate_event_structure(event)
            assert result[:2] == ("PLAN_ACCEPTED", "CREATE")
        else:
            assert validators.validate_event(event, context).envelope is event
    wall.assert_not_called()
    cpu.assert_not_called()
    channel.slog.assert_not_called()


def test_repeated_decoding_aggregates_cost_and_keeps_exact_return_object(mocker, history, clock):
    event, row, _ = history
    mocker.patch.object(decoding, "EventEnvelope", autospec=True, return_value=event)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        actual = _transaction(lambda: [decoding.decode_event_row(row) for _ in range(2)])
    assert actual.value[0] is actual.value[1] is event
    payload, = _summaries(channel)
    assert payload["details_inclusive"] is True
    assert payload["details"] == {
        "history_decode": {"elapsed_ms": 10, "cpu_ms": 6, "calls": 2},
        "history_structure_validation": {"elapsed_ms": 6, "cpu_ms": 4, "calls": 2},
    }


def test_current_event_validation_contains_historical_decode_and_structure_costs(mocker, history, clock):
    event, row, context = history
    mocker.patch.dict(validators.NAMED_GUARDS, {
        "admission": lambda current, facts: decoding.decode_event_row(row),
    })
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        actual = _transaction(lambda: validators.validate_event(event, context))
    assert actual.value.envelope is event
    payload, = _summaries(channel)
    assert payload["details"] == {
        "history_event_validation": {"elapsed_ms": 8, "cpu_ms": 5, "calls": 1},
        "history_decode": {"elapsed_ms": 5, "cpu_ms": 3, "calls": 1},
        "history_structure_validation": {"elapsed_ms": 6, "cpu_ms": 4, "calls": 2},
    }


@pytest.mark.parametrize("entry,name", [
    ("decode", "history_decode"), ("structure", "history_structure_validation"),
    ("event", "history_event_validation"),
])
def test_timed_history_failure_preserves_original_exception(mocker, history, clock, entry, name):
    event, row, context = history
    original = RuntimeError("original history failure")
    if entry == "decode":
        decoding.parse_exact_json.side_effect = original
        work = lambda: decoding.decode_event_row(row)
    elif entry == "structure":
        validators.validate_host_photo_event_structure.side_effect = original
        work = lambda: validators.validate_event_structure(event)
    else:
        def fail(current, facts):
            raise original
        mocker.patch.dict(validators.NAMED_GUARDS, {"admission": fail})
        work = lambda: validators.validate_event(event, context)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        with pytest.raises(RuntimeError) as failure:
            _transaction(work)
    assert failure.value is original
    payload, = _summaries(channel)
    assert payload["details"][name]["calls"] == 1


def test_nested_transactions_keep_separate_history_aggregates(history, clock):
    _, row, _ = history
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    def outer():
        decoding.decode_event_row(row)
        _transaction(lambda: decoding.decode_event_row(row), "inner")
        return decoding.decode_event_row(row)

    with performance_logging(channel):
        _transaction(outer)
    inner, outer = _summaries(channel)
    assert inner["operation_key"] == "inner" and outer["operation_key"] == "outer"
    assert inner["details"]["history_decode"] == {"elapsed_ms": 5, "cpu_ms": 3, "calls": 1}
    assert outer["details"]["history_decode"] == {"elapsed_ms": 10, "cpu_ms": 6, "calls": 2}


def test_history_call_in_another_context_does_not_join_the_transaction(history, clock):
    _, row, _ = history
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    def work():
        decoding.decode_event_row(row)
        return Context().run(decoding.decode_event_row, row)

    with performance_logging(channel):
        _transaction(work)
    payload, = _summaries(channel)
    assert payload["details"]["history_decode"] == {"elapsed_ms": 5, "cpu_ms": 3, "calls": 1}
