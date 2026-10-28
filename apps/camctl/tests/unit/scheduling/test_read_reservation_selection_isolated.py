"""只使用连接替身核对 READ 保留与未派发票据。"""

from importlib.util import find_spec, module_from_spec, spec_from_file_location
from types import SimpleNamespace

import pytest

from camctl import resources
from camctl.contracts import enums


@pytest.fixture
def reads(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda *_: pytest.fail("不能读取包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda *_: pytest.fail("不能读取包资源"))
    values = {"operation_runs.kind": SimpleNamespace(READ_FILE=3),
              "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2),
              "operation_attempts.status": SimpleNamespace(RUNNING=1)}
    monkeypatch.setattr(enums, "enum_for", lambda name: values[name])
    source = find_spec("camctl.persistence.repositories.retry_wait_reads")
    spec = spec_from_file_location("camctl.persistence.repositories._reservation_test", source.origin)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchmany(self, count):
        return self.rows[:count]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class Connection:
    def __init__(self, *, used=1, attempt=(17, 1, 1, None, None, 1, 1), other=False):
        self.used, self.attempt, self.other = used, attempt, other

    def execute(self, sql, parameters):
        if "LEFT JOIN operation_runs" in sql:
            return Cursor([(2, 3, 2 if self.used else 1, self.used, 0)])
        if "SELECT t.id,t.attempt_no" in sql:
            assert parameters == (2, 17, 3)
            return Cursor([] if self.attempt is None else [self.attempt])
        if "id<>?" in sql or "SELECT 1 FROM operation_attempts WHERE run_id=? LIMIT 1" in sql:
            return Cursor([(1,)] if self.other else [])
        raise AssertionError(sql)


def test_unstarted_slot_does_not_prevent_due_capture(reads):
    assert not reads.read_reservation_blocks_control(Connection(used=0), "camera")
    with pytest.raises(reads.ConsistencyError):
        reads.read_reservation_blocks_control(Connection(used=0, other=True), "camera")


@pytest.mark.parametrize("other,blocked", [(False, False), (True, True)])
def test_exclusion_preserves_other_unsettled_attempts(reads, other, blocked):
    assert reads.read_reservation_blocks_control(Connection(other=other), "camera",
                                                 exclude_attempt_id=17) is blocked


@pytest.mark.parametrize("attempt", [None, (17, 1, 1, None, None, 1, 2),
                                     (17, 1, 2, None, None, 1, 1),
                                     (17, 1, 1, 9, {}, 1, 1)])
def test_exclusion_requires_original_running_attempt_and_round(reads, attempt):
    with pytest.raises(reads.ConsistencyError):
        reads.read_reservation_blocks_control(Connection(attempt=attempt), "camera",
                                               exclude_attempt_id=17)
