"""来源、选择和目标固定后，候选判断遵守各记录的实际生命周期。"""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError, validate_event
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs

from .test_product_competition import competition_database, _proposal
from .test_qualification import qualification_environment, _Seedling, _candidate


def _fixed_source(connection, *, status=1, selection=None, mode=1, requested=("701",)):
    params = {"source": {"action_name": "action-11"}}
    if mode == 2:
        params["filter"] = "preview"
    elif mode == 3:
        params["output_ids"] = list(requested)
    connection.execute("UPDATE actions SET plan_id=2,status=?,execution_started=?,source_resolution_state=2,"
        " resolved_source_plan_id=2,input_fields_json=?,execution_spec_json=? WHERE id=91",
        (status, int(status != 1), json.dumps({"params": params}), json.dumps({"selection_mode": mode})))
    connection.execute("INSERT INTO action_dependencies (id,action_id,depends_on_action_id) VALUES (191,91,11)")
    if selection is not None:
        connection.execute("INSERT INTO obtain_source_selections (id,dependency_id,status) VALUES (191,191,?)", (selection,))
    connection.commit()


@pytest.mark.parametrize("mode,requested,blocked", [(1, (), True), (2, (), False),
                                                  (3, ("701",), True), (3, ("999",), False)])
def test_pending_fixed_source_competes_without_initializing_selection(competition_database, mode, requested, blocked):
    owned = competition_database
    _fixed_source(owned.connection, mode=mode, requested=requested)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
                                                   new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.reason == ("business_order" if blocked else None)
    assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections WHERE dependency_id=191").fetchone() == (0,)
    if blocked:
        assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("status,selection", [(1, 1), (1, 2), (2, None)])
def test_selection_initialization_must_match_action_phase(competition_database, status, selection):
    owned = competition_database
    _fixed_source(owned.connection, status=status, selection=selection)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().grant_file(_candidate(_Seedling(31, 101, 701, 501)),
                                                   new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_formal_grant_requires_reliable_empty_prestart_selection(competition_database):
    _fixed_source(competition_database.connection, mode=2)
    event, context = _proposal(competition_database)
    assert validate_event(event, context).branch_name == "GRANT"
    assert context.complete_rows("obtain_source_selections", "dependency_id", 191) == {}
    ranges = dict(context.read_coverage.ranges)
    ranges["obtain_source_selections", "dependency_id"] -= {191}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, read_coverage=ReadCoverage(ranges)))


def test_formal_grant_sees_prestart_candidate_before_selection_exists(competition_database):
    _fixed_source(competition_database.connection, mode=2)
    event, context = _proposal(competition_database)
    future = deepcopy(context.state_rows)
    candidate = context.state_rows["actions"][91]
    candidate["input_fields_json"]["params"].pop("filter")
    candidate["execution_spec_json"] = {"selection_mode": 1}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))
