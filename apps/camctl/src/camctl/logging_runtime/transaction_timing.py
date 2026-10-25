"""完整事务的辅助计量；只聚合数字，不访问数据库或保存 SQL。"""

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from time import monotonic_ns, thread_time_ns

from camctl.logging_runtime.performance import performance_enabled, record_performance


class TransactionPhase(StrEnum):
    BEGIN = "begin_write"
    READ_STATE = "read_state"
    PLAN = "plan"
    EVENT_GENERATION = "event_generation"
    CONTEXT = "validation_context"
    REPORT_TARGETS = "report_targets"
    VALIDATE = "event_validation"
    DERIVE = "change_derivation"
    WRITE = "sql_writes"
    FINAL_CHECKS = "final_checks"
    COMMIT = "commit"
    ROLLBACK = "rollback"


@dataclass
class _Cost:
    elapsed_ns: int = 0
    cpu_ns: int = 0
    calls: int = 0

    def as_dict(self):
        return {"elapsed_ms": round(self.elapsed_ns / 1_000_000, 3),
                "cpu_ms": round(self.cpu_ns / 1_000_000, 3), "calls": self.calls}


def _clock():
    return monotonic_ns(), thread_time_ns()


@dataclass
class _Measurement:
    began: tuple[int, int] = field(default_factory=_clock)
    phase: str = "unclassified"
    phases: dict = field(default_factory=lambda: defaultdict(_Cost))
    details: dict = field(default_factory=lambda: defaultdict(_Cost))
    sql: dict = field(default_factory=lambda: defaultdict(int))
    phase_sql: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    counts: dict = field(default_factory=lambda: defaultdict(int))

    def __post_init__(self):
        self.last = self.began

    def account(self, now):
        cost = self.phases[self.phase]
        cost.elapsed_ns += now[0] - self.last[0]
        cost.cpu_ns += now[1] - self.last[1]
        self.last = now

    def sql_call(self, kind, began, *, rows_read=0, rows_written=0):
        now = _clock()
        for totals in (self.sql, self.phase_sql[self.phase]):
            totals[kind + "_calls"] += 1
            totals["elapsed_ns"] += now[0] - began[0]
            totals["cpu_ns"] += now[1] - began[1]
            totals["rows_read"] += rows_read
            totals["rows_written"] += rows_written

    def emit(self, **fields):
        now = _clock()
        self.account(now)
        record_performance(
            "db_transaction", now[0] - self.began[0],
            cpu_ms=round((now[1] - self.began[1]) / 1_000_000, 3),
            phases={name: {**cost.as_dict(), "sql": _sql_values(self.phase_sql[name])}
                    for name, cost in self.phases.items()},
            details={name: cost.as_dict() for name, cost in self.details.items()},
            details_inclusive=True, sql=_sql_values(self.sql), counts=dict(self.counts), **fields)


def _sql_values(values):
    return {key: value for key, value in values.items() if not key.endswith("_ns")} | {
        "elapsed_ms": round(values.get("elapsed_ns", 0) / 1_000_000, 3),
        "cpu_ms": round(values.get("cpu_ns", 0) / 1_000_000, 3),
    }


_current = ContextVar("camctl_transaction_measurement", default=None)
_fields = ContextVar("camctl_transaction_measurement_fields", default={})


@contextmanager
def db_operation_context(**fields):
    token = _fields.set({**_fields.get(), **fields})
    try:
        yield
    finally:
        _fields.reset(token)


def trace_transaction(function):
    @wraps(function)
    def traced(command, operation_key, owned):
        if not performance_enabled():
            return function(command, operation_key, owned)
        measurement = _Measurement()
        token = _current.set(measurement)
        fields = {**_fields.get(), "command": type(command).__name__,
                  "operation_key": str(operation_key), "outcome": "raised"}
        try:
            result = function(command, operation_key, owned)
            fields["outcome"] = result.kind
            return result
        finally:
            _current.reset(token)
            measurement.emit(**fields)
    return traced


@contextmanager
def db_phase(phase: TransactionPhase):
    measurement = _current.get()
    if measurement is None:
        yield
        return
    previous = measurement.phase
    measurement.account(_clock())
    measurement.phase = phase.value
    measurement.phases[phase.value].calls += 1
    try:
        yield
    finally:
        measurement.account(_clock())
        measurement.phase = previous


def db_count(name, amount=1):
    measurement = _current.get()
    if measurement is not None:
        measurement.counts[name] += amount


@contextmanager
def transaction_detail(name):
    measurement = _current.get()
    if measurement is None:
        yield
        return
    began = _clock()
    measurement.details[name].calls += 1
    try:
        yield
    finally:
        now = _clock()
        cost = measurement.details[name]
        cost.elapsed_ns += now[0] - began[0]
        cost.cpu_ns += now[1] - began[1]


def trace_detail(name):
    def decorate(function):
        @wraps(function)
        def traced(*args, **kwargs):
            if _current.get() is None:
                return function(*args, **kwargs)
            with transaction_detail(name):
                return function(*args, **kwargs)
        return traced
    return decorate


def timed_connection(connection):
    measurement = _current.get()
    return connection if measurement is None else _Connection(connection, measurement)


class _Connection:
    def __init__(self, raw, measurement):
        self._raw, self._measurement = raw, measurement

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._raw, name, value)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *args):
        return self._raw.__exit__(*args)

    def _execute(self, method, *args, **kwargs):
        began = _clock()
        cursor = None
        try:
            cursor = getattr(self._raw, method)(*args, **kwargs)
            return _Cursor(cursor, self._measurement)
        finally:
            count = None if cursor is None else cursor.rowcount
            self._measurement.sql_call(method, began,
                rows_written=count if type(count) is int and count > 0 else 0)

    def execute(self, *args, **kwargs):
        return self._execute("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._execute("executemany", *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _Cursor(self._raw.cursor(*args, **kwargs), self._measurement)


class _Cursor:
    def __init__(self, raw, measurement):
        self._raw, self._measurement = raw, measurement
        self._iterator = None

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._raw, name, value)

    def _execute(self, method, *args, **kwargs):
        began = _clock()
        completed = False
        try:
            getattr(self._raw, method)(*args, **kwargs)
            completed = True
            return self
        finally:
            count = self._raw.rowcount if completed else 0
            self._measurement.sql_call(method, began,
                rows_written=count if type(count) is int and count > 0 else 0)

    def execute(self, *args, **kwargs):
        return self._execute("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._execute("executemany", *args, **kwargs)

    def _fetch(self, method, *args, **kwargs):
        began = _clock()
        count = 0
        try:
            result = getattr(self._raw, method)(*args, **kwargs)
            count = int(result is not None) if method == "fetchone" else len(result)
            return result
        finally:
            self._measurement.sql_call("fetch", began, rows_read=count)

    def fetchone(self):
        return self._fetch("fetchone")

    def fetchmany(self, *args, **kwargs):
        return self._fetch("fetchmany", *args, **kwargs)

    def fetchall(self):
        return self._fetch("fetchall")

    def __iter__(self):
        self._iterator = iter(self._raw)
        return self

    def __next__(self):
        began = _clock()
        count = 0
        try:
            if self._iterator is None:
                self._iterator = iter(self._raw)
            row = next(self._iterator)
            count = 1
            return row
        finally:
            self._measurement.sql_call("fetch", began, rows_read=count)
