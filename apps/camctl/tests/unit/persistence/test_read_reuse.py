"""事务内已核验读取的作用域、写入失效及容量边界。"""

from contextvars import Context
from dataclasses import dataclass
import sqlite3
import sys
from unittest.mock import MagicMock, Mock, PropertyMock

import pytest

from camctl.capture.result_inputs import page_from_outcome
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.devices.evidence import DeviceObservation
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.operations.models import AttemptTicket, CallOutcome
from camctl.persistence import read_reuse
from camctl.persistence.read_reuse import read_reuse_scope, reuse_read


@pytest.fixture
def connection():
    value = Mock(spec_set=sqlite3.Connection)
    value.in_transaction = True
    value.total_changes = 0
    yield value
    value.execute.assert_not_called()
    value.commit.assert_not_called()
    value.rollback.assert_not_called()


def test_same_key_loads_once_within_scope(connection):
    load = Mock(side_effect=["first", "second"])
    with read_reuse_scope(connection):
        assert reuse_read(connection, ("proof", 3), load) == "first"
        assert reuse_read(connection, ("proof", 3), load) == "first"
    load.assert_called_once_with()


def test_different_keys_load_independently(connection):
    load = Mock(side_effect=["first", "second"])
    with read_reuse_scope(connection):
        assert reuse_read(connection, ("proof", 3), load) == "first"
        assert reuse_read(connection, ("proof", 4), load) == "second"


def test_no_scope_always_loads(connection):
    load = Mock(side_effect=[1, 2])
    assert reuse_read(connection, "proof", load) == 1
    assert reuse_read(connection, "proof", load) == 2


def test_other_connection_bypasses_current_scope(connection):
    other = Mock(spec_set=sqlite3.Connection)
    load = Mock(side_effect=[1, 2])
    with read_reuse_scope(connection):
        assert reuse_read(other, "proof", load) == 1
        assert reuse_read(other, "proof", load) == 2
    other.execute.assert_not_called()


def test_connection_binding_uses_identity_instead_of_equality(connection):
    other = MagicMock(spec_set=sqlite3.Connection)
    other.__eq__.return_value = True
    load = Mock(side_effect=[1, 2])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 1
        assert reuse_read(other, "proof", load) == 2


@pytest.mark.parametrize("has_scope", [False, True])
def test_bypass_does_not_inspect_connection_state(connection, has_scope):
    other = Mock(spec_set=sqlite3.Connection)
    type(other).in_transaction = PropertyMock(side_effect=AssertionError("不应读取事务状态"))
    type(other).total_changes = PropertyMock(side_effect=AssertionError("不应读取写入代"))
    if has_scope:
        with read_reuse_scope(connection):
            assert reuse_read(other, "proof", lambda: "direct") == "direct"
    else:
        assert reuse_read(other, "proof", lambda: "direct") == "direct"


def test_same_connection_nested_scope_shares_values(connection):
    load = Mock(side_effect=[1, 2])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 1
        with read_reuse_scope(connection):
            assert reuse_read(connection, "proof", load) == 1
        assert reuse_read(connection, "proof", load) == 1


def test_other_connection_nested_scope_restores_parent(connection):
    other = Mock(spec_set=sqlite3.Connection)
    other.in_transaction = True
    other.total_changes = 0
    load = Mock(side_effect=["outer", "other", "inner"])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == "outer"
        with read_reuse_scope(other):
            assert reuse_read(other, "proof", load) == "other"
            with read_reuse_scope(connection):
                assert reuse_read(connection, "proof", load) == "inner"
            assert reuse_read(other, "proof", load) == "other"
        assert reuse_read(connection, "proof", load) == "outer"


def test_scope_exit_discards_values(connection):
    load = Mock(side_effect=[1, 2])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 1
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 2


def test_fresh_context_has_no_parent_scope(connection):
    load = Mock(side_effect=[1, 2])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 1
        assert Context().run(reuse_read, connection, "proof", load) == 2
        assert reuse_read(connection, "proof", load) == 1


def test_total_changes_discards_every_old_value(connection):
    load = Mock(side_effect=["old-a", "old-b", "new-a", "new-b"])
    with read_reuse_scope(connection):
        reuse_read(connection, "a", load)
        reuse_read(connection, "b", load)
        connection.total_changes += 1
        assert reuse_read(connection, "a", load) == "new-a"
        assert reuse_read(connection, "b", load) == "new-b"


def test_without_transaction_always_loads(connection):
    connection.in_transaction = False
    load = Mock(side_effect=[1, 2])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 1
        assert reuse_read(connection, "proof", load) == 2


def test_observed_transaction_end_discards_values(connection):
    load = Mock(side_effect=[1, 2, 3])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == 1
        connection.in_transaction = False
        assert reuse_read(connection, "proof", load) == 2
        connection.in_transaction = True
        assert reuse_read(connection, "proof", load) == 3


@pytest.mark.parametrize("failure", [ValueError("读取失败"), KeyboardInterrupt(), SystemExit()])
def test_loader_exception_is_preserved_and_retried(connection, failure):
    load = Mock(side_effect=[failure, "success"])
    with read_reuse_scope(connection):
        with pytest.raises(type(failure)) as caught:
            reuse_read(connection, "proof", load)
        assert caught.value is failure
        assert reuse_read(connection, "proof", load) == "success"
        assert reuse_read(connection, "proof", load) == "success"


def test_loader_write_returns_value_without_caching_it(connection):
    def write_while_loading():
        connection.total_changes += 1
        return "before-write"

    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", write_while_loading) == "before-write"
        assert reuse_read(connection, "proof", lambda: "after-write") == "after-write"


def test_loader_write_then_error_discards_prior_values(connection):
    failure = ValueError("写后读取失败")

    def write_then_fail():
        connection.total_changes += 1
        raise failure

    with read_reuse_scope(connection):
        reuse_read(connection, "prior", lambda: "old")
        with pytest.raises(ValueError) as caught:
            reuse_read(connection, "writing", write_then_fail)
        assert caught.value is failure
        assert reuse_read(connection, "prior", lambda: "new") == "new"


def test_recursive_load_cannot_store_old_value_after_write(connection):
    def load_before_write():
        connection.total_changes += 1
        assert reuse_read(connection, "inner", lambda: "new-inner") == "new-inner"
        return "old-outer"

    with read_reuse_scope(connection):
        assert reuse_read(connection, "outer", load_before_write) == "old-outer"
        assert reuse_read(connection, "outer", lambda: "new-outer") == "new-outer"


def test_loader_ending_transaction_does_not_cache(connection):
    def end_transaction():
        connection.in_transaction = False
        return "ended"

    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", end_transaction) == "ended"
        connection.in_transaction = True
        assert reuse_read(connection, "proof", lambda: "next") == "next"


@pytest.mark.parametrize("failure", [ValueError("退出失败"), KeyboardInterrupt(), SystemExit()])
def test_exceptional_nested_scope_restores_parent(connection, failure):
    other = Mock(spec_set=sqlite3.Connection)
    other.in_transaction = True
    other.total_changes = 0
    with read_reuse_scope(connection):
        reuse_read(connection, "proof", lambda: "parent")
        with pytest.raises(type(failure)) as caught:
            with read_reuse_scope(other):
                reuse_read(other, "proof", lambda: "child")
                raise failure
        assert caught.value is failure
        assert reuse_read(connection, "proof", lambda: "wrong") == "parent"
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", lambda: "next") == "next"


def test_unhashable_key_runs_original_loader_without_reuse(connection):
    load = Mock(side_effect=["first", "second"])
    with read_reuse_scope(connection):
        assert reuse_read(connection, [], load) == "first"
        assert reuse_read(connection, [], load) == "second"


@pytest.mark.parametrize("key", [[], "proof"])
def test_loader_type_error_is_preserved(connection, key):
    failure = TypeError("原读取拒绝输入类型")

    def load():
        raise failure

    with read_reuse_scope(connection), pytest.raises(TypeError) as caught:
        reuse_read(connection, key, load)
    assert caught.value is failure


def test_entry_limit_evicts_least_recently_used(connection, monkeypatch):
    monkeypatch.setattr(read_reuse, "MAX_READ_REUSE_ENTRIES", 2)
    with read_reuse_scope(connection):
        reuse_read(connection, "a", lambda: "a")
        reuse_read(connection, "b", lambda: "b")
        assert reuse_read(connection, "a", lambda: "wrong") == "a"
        reuse_read(connection, "c", lambda: "c")
        assert reuse_read(connection, "a", lambda: "wrong") == "a"
        assert reuse_read(connection, "b", lambda: "reloaded") == "reloaded"


def test_byte_limit_evicts_old_values(connection, monkeypatch):
    value = b"x" * 200
    entry_bytes = sys.getsizeof("a") + sys.getsizeof(value)
    monkeypatch.setattr(read_reuse, "MAX_READ_REUSE_BYTES", entry_bytes * 2)
    with read_reuse_scope(connection):
        reuse_read(connection, "a", lambda: value)
        reuse_read(connection, "b", lambda: value)
        reuse_read(connection, "c", lambda: value)
        assert reuse_read(connection, "b", lambda: "wrong") == value
        assert reuse_read(connection, "a", lambda: "reloaded") == "reloaded"


def test_recursive_same_key_does_not_count_replaced_value_twice(connection, monkeypatch):
    value = b"x" * 200
    entry_bytes = sys.getsizeof("a") + sys.getsizeof(value)
    monkeypatch.setattr(read_reuse, "MAX_READ_REUSE_BYTES", entry_bytes * 2)

    def load_outer():
        assert reuse_read(connection, "a", lambda: value) == value
        return value

    with read_reuse_scope(connection):
        assert reuse_read(connection, "a", load_outer) == value
        reuse_read(connection, "b", lambda: value)
        assert reuse_read(connection, "a", lambda: "wrong") == value


def test_oversized_value_returns_without_evicting_cached_values(connection, monkeypatch):
    monkeypatch.setattr(read_reuse, "MAX_READ_REUSE_BYTES", 500)
    load = Mock(side_effect=[b"x" * 1000, b"y" * 1000])
    with read_reuse_scope(connection):
        reuse_read(connection, "small", lambda: "kept")
        assert reuse_read(connection, "large", load) == b"x" * 1000
        assert reuse_read(connection, "large", load) == b"y" * 1000
        assert reuse_read(connection, "small", lambda: "wrong") == "kept"


@dataclass(slots=True)
class MutableProof:
    members: list


def test_dataclass_nested_payload_counts_toward_byte_limit(connection, monkeypatch):
    monkeypatch.setattr(read_reuse, "MAX_READ_REUSE_BYTES", 500)
    load = Mock(side_effect=[MutableProof([b"x" * 1000]), MutableProof([b"y" * 1000])])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load).members == [b"x" * 1000]
        assert reuse_read(connection, "proof", load).members == [b"y" * 1000]


def test_repeated_references_count_once(connection, monkeypatch):
    shared = b"x" * 1000
    value = [shared] * 10
    # 预算容纳列表及一份载荷；无法容纳重复计算的十份载荷。
    monkeypatch.setattr(read_reuse, "MAX_READ_REUSE_BYTES", 1500)
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", lambda: value) == value
        assert reuse_read(connection, "proof", lambda: "wrong") == value


def test_recursive_container_can_be_cached(connection):
    value = []
    value.append(value)
    with read_reuse_scope(connection):
        reuse_read(connection, "proof", lambda: value)
        hit = reuse_read(connection, "proof", lambda: "wrong")
    assert hit is hit[0]
    assert hit is not value


def test_mutable_results_are_isolated_from_stored_and_hit_values(connection):
    loaded = MutableProof([{"event": [1]}])
    with read_reuse_scope(connection):
        first = reuse_read(connection, "proof", lambda: loaded)
        first.members[0]["event"].append(2)
        second = reuse_read(connection, "proof", lambda: "wrong")
        assert second.members == [{"event": [1]}]
        second.members[0]["event"].append(3)
        third = reuse_read(connection, "proof", lambda: "wrong")
        assert third.members == [{"event": [1]}]
        assert third is not second


@pytest.mark.parametrize("kind", ["saved-page", "page", "event", "row-image"])
def test_actual_read_value_types_support_isolated_reuse(connection, kind):
    ticket = AttemptTicket(1, "result", "71", "results/71", 9)
    outcome = CallOutcome(observations=(DeviceObservation("result_files_listed", 2, {
        "activity_id": "71", "entries": [{
            "identity": "/DCIM/a.mp4", "locator": {"path": "/DCIM/a.mp4"},
            "complete": True, "size_bytes": 50, "kind": "video", "format_id": "mp4",
        }], "cursor": None, "next_cursor": None, "set_finalized": False,
        "completion_evidence": None,
    }),))
    page = page_from_outcome(ticket, outcome)
    row_image = RowImage(True, {"status": 2, "details": {"members": [1]}})
    values = {
        "saved-page": SavedResultPage(ResultPageRef(ticket, 1, 3, None), page, 10),
        "page": page,
        "event": EventEnvelope(3, 2, 1, 1, 10, 1, None, 1, {"members": [1]}, (
            RowChange("actions", 71, RowImage(False, {}), row_image),
        )),
        "row-image": row_image,
    }
    value = values[kind]
    with read_reuse_scope(connection):
        assert reuse_read(connection, kind, lambda: value) is value
        copied = reuse_read(connection, kind, lambda: "wrong")
    assert type(copied) is type(value)
    assert copied == value
    assert copied is not value


@pytest.mark.parametrize("key", [[], {}, ("proof", [])])
def test_unhashable_key_keeps_original_loader_behavior(connection, key):
    load = Mock(side_effect=["first", "second"])
    with read_reuse_scope(connection):
        assert reuse_read(connection, key, load) == "first"
        assert reuse_read(connection, key, load) == "second"
    assert load.call_count == 2


def test_unhashable_key_does_not_swallow_loader_type_error(connection):
    error = TypeError("原读取拒绝该输入")
    load = Mock(side_effect=error)
    with read_reuse_scope(connection):
        with pytest.raises(TypeError) as caught:
            reuse_read(connection, [], load)
    assert caught.value is error
    load.assert_called_once_with()


def test_independent_check_discards_prior_phase_without_leaving_transaction(connection):
    from camctl.persistence.read_reuse import clear_read_reuse

    load = Mock(side_effect=["proposal", "verified"])
    with read_reuse_scope(connection):
        assert reuse_read(connection, "proof", load) == "proposal"
        clear_read_reuse(connection)
        assert reuse_read(connection, "proof", load) == "verified"
        assert reuse_read(connection, "proof", load) == "verified"
    assert load.call_count == 2
