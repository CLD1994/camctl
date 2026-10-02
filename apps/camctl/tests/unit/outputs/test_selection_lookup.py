"""有限查询事实区分未核实、记录缺失与其他来源。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.catalog import OutputKind
from camctl.outputs.definitions import SelectionMode
from .test_original_selection import sources, no_resource_reads


def _facts(checked, *, completed=True, previous=(), availability=1):
    return sources.SelectionFacts(
        source_action_id=11, source_completed=completed,
        outputs=(sources.CatalogEntry(101, OutputKind.ORIGINAL, availability),),
        checked_output_sources=checked, previously_confirmed_ids=frozenset(previous))


def _select(facts, requested=(), mode=SelectionMode.EXPLICIT_IDS):
    return sources.select_outputs(sources.SourceResolution(
        state=sources.ResolutionState.FIXED, member_action_ids=(11,), source_plan_id=1),
        facts, mode, requested)


@pytest.mark.parametrize("availability,status,code", [(1, 2, None), (2, 4, 4), (3, 4, 3), (4, 4, 3), (5, 1, None)])
def test_local_catalog_proves_existence_without_supplemental_query(availability, status, code):
    item, = _select(_facts({}, availability=availability), (101,)).items
    assert (item.status, item.error_code, item.requested_output_id) == (status, code, 101)
    assert item.output_id == (None if availability == 5 else 101)


@pytest.mark.parametrize("source,code", [(12, 2), (None, 1)])
def test_explicit_lookup_distinguishes_missing_and_other_source(source, code):
    item, = _select(_facts({202: source}), (202,)).items
    assert (item.status, item.error_code, item.requested_output_id, item.output_id) == (4, code, 202, None)
    assert item.error_details == {"requested_output_id": "202"}


def test_explicit_target_not_queried_cannot_be_classified_as_missing():
    with pytest.raises(ConsistencyError):
        _select(_facts({}), (202,))


@pytest.mark.parametrize("source", [11, True, 0, -1, "12", 12.0, 9223372036854775808])
@pytest.mark.parametrize("mode", [SelectionMode.EXPLICIT_IDS, SelectionMode.DEFAULT])
def test_supplemental_lookup_requires_explainable_external_source(source, mode):
    with pytest.raises(ConsistencyError):
        _select(_facts({202: source}), (202,), mode)


@pytest.mark.parametrize("identity", [True, 0, "202", 202.0, 9223372036854775808])
def test_supplemental_lookup_requires_valid_record_identity(identity):
    with pytest.raises(ConsistencyError):
        _select(_facts({identity: 12}), mode=SelectionMode.DEFAULT)


def test_complete_catalog_cannot_repeat_record_identity():
    facts = _facts({})
    with pytest.raises(ConsistencyError):
        _select(replace(facts, outputs=facts.outputs * 2), mode=SelectionMode.DEFAULT)


@pytest.mark.parametrize("source", [11, 12, None])
def test_supplemental_lookup_does_not_duplicate_local_authority(source):
    with pytest.raises(ConsistencyError):
        _select(_facts({101: source}), mode=SelectionMode.DEFAULT)


@pytest.mark.parametrize("completed", [True, False])
@pytest.mark.parametrize("checked", [{}, {202: None}, {202: 11}])
def test_previously_confirmed_target_requires_positive_current_evidence(completed, checked):
    with pytest.raises(ConsistencyError):
        _select(_facts(checked, completed=completed, previous=(202,)), mode=SelectionMode.DEFAULT)


@pytest.mark.parametrize("previous,checked", [((101,), {}), ((202,), {202: 12})])
@pytest.mark.parametrize("completed", [True, False])
def test_previously_confirmed_local_or_external_record_is_retained(previous, checked, completed):
    selection = _select(_facts(checked, completed=completed, previous=previous), mode=SelectionMode.DEFAULT)
    assert selection.is_fixed is completed
    assert selection.selected_output_ids == ((101,) if completed else ())


def test_pending_source_does_not_classify_unqueried_explicit_target():
    selection = _select(_facts({}, completed=False), (202,))
    assert not selection.is_fixed
    assert selection.items == ()
