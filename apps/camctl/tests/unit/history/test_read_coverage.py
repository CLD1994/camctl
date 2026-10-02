"""完整查询范围与当前行共同决定可靠的空集合或成员集合。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history import validators


def _context(ranges=None, rows=None, future=None):
    return validators.EventContext(
        transaction=TransactionRange(1, 1, 1), owners={},
        state_rows={} if rows is None else rows, transaction_rows=future,
        read_coverage=validators.ReadCoverage({} if ranges is None else ranges),
    )


@pytest.mark.parametrize("rows", [{}, {"outputs": {}}, {"outputs": {71: {"source_action_id": 11}}}])
def test_uncovered_range_is_not_a_complete_collection(rows):
    with pytest.raises(validators.EventValidationError):
        _context(rows=rows).complete_rows("outputs", "source_action_id", 11)


@pytest.mark.parametrize("ranges", [
    {("outputs", "source_action_id"): {12}},
    {("outputs", "id"): {11}},
    {("obtain_items", "selection_id"): {11}},
])
def test_coverage_of_another_range_does_not_prove_requested_range(ranges):
    with pytest.raises(validators.EventValidationError):
        _context(ranges).complete_rows("outputs", "source_action_id", 11)


@pytest.mark.parametrize("rows", [{}, {"outputs": {}}, {"outputs": {72: {"source_action_id": 12}}}])
def test_covered_empty_range_is_reliably_empty(rows):
    context = _context({("outputs", "source_action_id"): {11}}, rows)
    assert context.complete_rows("outputs", "source_action_id", 11) == {}


def test_complete_range_returns_matching_current_rows_only():
    context = _context({("outputs", "source_action_id"): {11}}, {"outputs": {
        71: {"source_action_id": 11, "availability": 1},
        72: {"source_action_id": 12, "availability": 3},
        73: {"source_action_id": Decimal("11.0"), "availability": 2},
    }})
    assert context.complete_rows("outputs", "source_action_id", 11) == {
        71: {"source_action_id": 11, "availability": 1},
        73: {"source_action_id": 11, "availability": 2},
    }


def test_primary_key_range_uses_current_row_identity():
    context = _context({("outputs", "id"): {71, 72}}, {"outputs": {71: {"source_action_id": 11}}})
    assert context.complete_rows("outputs", "id", 71) == {71: {"source_action_id": 11}}
    assert context.complete_rows("outputs", "id", 72) == {}


def test_primary_key_range_does_not_scan_other_rows():
    class DirectLookupOnly(dict):
        def items(self):
            raise AssertionError("按主键读取不应遍历整份当前行映射")

    context = _context({("outputs", "id"): {71, 72}}, {
        "outputs": DirectLookupOnly({71: {"source_action_id": 11}})})
    assert context.complete_rows("outputs", "id", 71) == {71: {"source_action_id": 11}}
    assert context.complete_rows("outputs", "id", 72) == {}


def test_future_proposal_does_not_fill_current_collection():
    context = _context({("outputs", "source_action_id"): {11}}, future={
        "outputs": {71: {"source_action_id": 11}}})
    assert context.complete_rows("outputs", "source_action_id", 11) == {}


def test_coverage_does_not_cache_business_rows():
    context = _context({("outputs", "source_action_id"): {11}})
    advanced = replace(context, state_rows={"outputs": {71: {"source_action_id": 11}}})
    assert advanced.complete_rows("outputs", "source_action_id", 11) == {71: {"source_action_id": 11}}


@pytest.mark.parametrize("identity", [True, 0, -1, "11", 11.0, None, 9223372036854775808])
def test_coverage_rejects_invalid_identity(identity):
    with pytest.raises(ValueError):
        validators.ReadCoverage({("outputs", "source_action_id"): [identity]})


@pytest.mark.parametrize("identity", [True, 0, "11", 11.0, 9223372036854775808])
def test_query_rejects_invalid_identity(identity):
    context = _context({("outputs", "source_action_id"): {11}})
    with pytest.raises(ValueError):
        context.complete_rows("outputs", "source_action_id", identity)


def test_coverage_is_independent_of_later_input_mutation():
    identities = {11}
    ranges = {("outputs", "source_action_id"): identities}
    context = _context(ranges)
    identities.add(12)
    ranges[("obtain_items", "selection_id")] = {61}
    with pytest.raises(validators.EventValidationError):
        context.complete_rows("outputs", "source_action_id", 12)


@pytest.mark.parametrize("row", [
    {}, {"source_action_id": True}, {"source_action_id": "11"},
    {"source_action_id": 0}, {"source_action_id": 9223372036854775808},
    {"source_action_id": Decimal("11.5")},
])
def test_unknown_membership_cannot_be_skipped_as_unrelated(row):
    context = _context({("outputs", "source_action_id"): {11}}, {"outputs": {71: row}})
    with pytest.raises(validators.EventValidationError):
        context.complete_rows("outputs", "source_action_id", 11)


def test_explicit_null_foreign_identity_does_not_match():
    context = _context({("device_files", "original_device_file_id"): {71}}, {
        "device_files": {71: {"original_device_file_id": None}}})
    assert context.complete_rows("device_files", "original_device_file_id", 71) == {}


@pytest.mark.parametrize("row_id", [True, 0, -1, "71", 71.5, 9223372036854775808])
@pytest.mark.parametrize("source", [11, 12, None])
def test_complete_range_rejects_invalid_row_identity_before_filtering(row_id, source):
    with pytest.raises(validators.EventValidationError):
        context = _context({("outputs", "source_action_id"): {11}}, {
            "outputs": {row_id: {"source_action_id": source}}})
        context.complete_rows("outputs", "source_action_id", 11)


@pytest.mark.parametrize("row_id", [True, 1.0, Decimal("1")])
def test_primary_key_alias_cannot_supply_a_record(row_id):
    with pytest.raises(validators.EventValidationError):
        context = _context({("outputs", "id"): {1}}, {
            "outputs": {row_id: {"source_action_id": 11}}})
        context.complete_rows("outputs", "id", 1)
