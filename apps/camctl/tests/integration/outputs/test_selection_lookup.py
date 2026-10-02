"""选择只补查实际请求及此前确认的非本来源产物。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, ValueTypeError, ValueRangeError
from camctl.outputs.sources import SelectionMode, select_outputs
from camctl.persistence.repositories import outputs

from .test_output_family import family_database
from .test_qualification import _seed_device_file, _seed_output
from .test_sources import _fixed_resolution
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture
def lookup_database(family_database):
    connection = family_database.connection
    connection.execute("UPDATE actions SET status=3 WHERE id=11")
    connection.commit()
    return connection


class LookupConnection(_FaultConnection):
    def __init__(self, connection, *, fail_at=None, stage=None):
        super().__init__(connection, "unused")
        self.queries = []
        self.cursors = []
        self.fail_at = fail_at
        self.stage = stage
        self.error = sqlite3.OperationalError("requested output lookup failed")

    def execute(self, sql, parameters=()):
        if sql == "SELECT id FROM outputs":
            raise AssertionError("一次来源选择不能扫描全库产物 ID")
        is_lookup = sql.startswith("SELECT id, source_action_id FROM outputs WHERE id IN")
        if is_lookup:
            self.queries.append(tuple(parameters))
            if len(self.queries) == self.fail_at and self.stage == "execute":
                raise self.error
        cursor = self._connection.execute(sql, parameters)
        if not is_lookup:
            return cursor
        probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        probe.close.side_effect = cursor.close
        probe.fetchall.side_effect = (self.error if len(self.queries) == self.fail_at and self.stage == "fetch"
                                     else cursor.fetchall)
        self.cursors.append(probe)
        return probe


@pytest.mark.parametrize("unrelated_count", [0, 130])
@pytest.mark.parametrize("mode,selected", [(SelectionMode.DEFAULT, (703, 705)), (SelectionMode.PREVIEW, (703,))])
def test_normal_selection_does_not_load_unrelated_ids(lookup_database, unrelated_count, mode, selected):
    connection = lookup_database
    for identity in range(1000, 1000 + unrelated_count):
        _seed_device_file(connection, identity + 1000, 12)
        _seed_output(connection, identity, 12, identity + 1000)
    connection.commit()
    probe = LookupConnection(connection)
    before = tuple(connection.iterdump())
    facts = outputs.load_selection_facts(probe, 11)
    result = select_outputs(_fixed_resolution((11,)), facts, mode)
    assert result.selected_output_ids == selected
    assert probe.queries == []
    assert tuple(connection.iterdump()) == before


def test_explicit_selection_queries_only_nonlocal_targets(lookup_database):
    connection = lookup_database
    # 其他来源的物理文件不参与本来源选择，归属不匹配不依赖读取其文件元数据。
    connection.execute("UPDATE device_files SET role=1 WHERE id=504")
    connection.commit()
    probe = LookupConnection(connection)
    before = tuple(connection.iterdump())
    requested = (999, 704, 703, 705)
    facts = outputs.load_selection_facts(probe, 11, requested_output_ids=requested)
    assert facts.checked_output_sources == {704: 12, 999: None}
    result = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.EXPLICIT_IDS, requested)
    assert [(item.requested_output_id, item.error_code, item.output_id) for item in result.items] == [
        (999, 1, None), (704, 2, None), (703, None, 703), (705, None, 705)]
    assert len(probe.queries) == 1
    assert set(probe.queries[0]) == {704, 999}
    assert tuple(connection.iterdump()) == before
    probe.cursors[0].close.assert_called_once_with()


def test_unqueried_explicit_target_never_becomes_missing_error(lookup_database):
    facts = outputs.load_selection_facts(lookup_database, 11)
    with pytest.raises(ConsistencyError):
        select_outputs(_fixed_resolution((11,)), facts, SelectionMode.EXPLICIT_IDS, (999,))


@pytest.mark.parametrize("previous,expected", [((701,), {}), ((704,), {704: 12}), ((999,), {999: None})])
def test_previous_identity_is_rechecked_in_bounded_scope(lookup_database, previous, expected):
    probe = LookupConnection(lookup_database)
    facts = outputs.load_selection_facts(probe, 11, previously_confirmed_ids=frozenset(previous))
    assert facts.checked_output_sources == expected
    if previous == (999,):
        with pytest.raises(ConsistencyError):
            select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT)
    else:
        assert select_outputs(_fixed_resolution((11,)), facts, SelectionMode.DEFAULT).selected_output_ids == (703, 705)
    assert {identity for query in probe.queries for identity in query} == set(expected)
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()


def test_requested_and_previous_ids_share_one_lookup(lookup_database):
    probe = LookupConnection(lookup_database)
    facts = outputs.load_selection_facts(probe, 11, requested_output_ids=(704, 704, 701, 999),
                                         previously_confirmed_ids=frozenset({704, 705}))
    assert facts.checked_output_sources == {704: 12, 999: None}
    assert sum(len(query) for query in probe.queries) == 2
    assert {identity for query in probe.queries for identity in query} == {704, 999}


def test_explicit_lookup_preserves_complete_results_across_batches(lookup_database):
    probe = LookupConnection(lookup_database)
    requested = tuple(range(1000, 1260))
    facts = outputs.load_selection_facts(probe, 11, requested_output_ids=requested)
    assert facts.checked_output_sources == dict.fromkeys(requested)
    assert len(probe.queries) >= 3
    assert all(0 < len(query) <= 128 for query in probe.queries)
    assert sum(len(query) for query in probe.queries) == 260
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("fail_at", [1, 2])
@pytest.mark.parametrize("stage", ["execute", "fetch"])
def test_lookup_failure_never_returns_partial_absence_facts(lookup_database, fail_at, stage):
    connection = lookup_database
    before = tuple(connection.iterdump())
    probe = LookupConnection(connection, fail_at=fail_at, stage=stage)
    with pytest.raises(sqlite3.OperationalError) as raised:
        outputs.load_selection_facts(probe, 11, requested_output_ids=tuple(range(1000, 1260)))
    assert raised.value is probe.error
    assert len(probe.queries) == fail_at
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("identity,error", [(True, ValueTypeError), (0, ValueRangeError), (9223372036854775808, ValueRangeError)])
@pytest.mark.parametrize("argument", ["requested_output_ids", "previously_confirmed_ids"])
def test_lookup_identity_requires_valid_object_id_before_query(lookup_database, argument, identity, error):
    with pytest.raises(error):
        outputs.load_selection_facts(lookup_database, 11, **{argument: (identity,)})
