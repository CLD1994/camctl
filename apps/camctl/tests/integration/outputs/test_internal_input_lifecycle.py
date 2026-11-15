"""内部工具已开始时必须保留原输入责任，不能按尚未建档恢复。"""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.validators import EventValidationError
from ..support.output_planning import capture_operation, event_case, validate_static_event as validate_event
from camctl.outputs.qualification import QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.transactions import row_facts

from .test_read_config import internal_read


def _stage(owned, stage):
    connection = owned.connection
    if stage.startswith("check_"):
        connection.execute("UPDATE recording_processing SET check_state=? WHERE id=5",
                           ({"check_running": 2, "check_completed": 3,
                             "check_failed": 4, "check_unconfirmed": 5}[stage],))
    else:
        if stage == "repair_succeeded":
            connection.execute(
                "INSERT INTO intermediate_files (id, owner_action_id, purpose, relative_path,"
                " retention_state, cleanup_state, size_bytes, created_event_id, last_event_id, change_count)"
                " VALUES (801, 11, 4, 'derived/801.mp4', 1, 1, 4096, 1, 1, 1)"
            )
        connection.execute(
            "UPDATE recording_processing SET repair_state=?, repair_basis_json='{}',"
            " repair_output_file_id=?, repair_error_json=? WHERE id=5",
            ({"repair_running": 4, "repair_succeeded": 5, "repair_failed": 6, "repair_canceled": 7}[stage],
             801 if stage == "repair_succeeded" else None, "{}" if stage == "repair_failed" else None),
        )
    connection.commit()


@pytest.mark.parametrize("stage", ["check_running", "check_completed", "repair_running", "repair_succeeded"])
@pytest.mark.parametrize("canceled", [False, True])
def test_started_processing_cannot_recreate_missing_input(internal_read, stage, canceled):
    owned, command = internal_read
    _stage(owned, stage)
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=11")
        owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("stage", ["check_failed", "check_unconfirmed", "repair_failed", "repair_canceled"])
def test_ended_processing_without_input_does_not_prove_missing_records(internal_read, stage):
    owned, command = internal_read
    _stage(owned, stage)
    owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=11")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "action_not_eligible"
    assert tuple(owned.connection.iterdump()) == before


@pytest.fixture
def prepared(internal_read):
    owned, command = internal_read
    key = new_operation_key()
    result = OutputsRepository().grant_file(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED
    return owned, command, key, result.value


@pytest.mark.parametrize("stage", ["check_running", "check_completed", "repair_running", "repair_succeeded"])
@pytest.mark.parametrize("original_key", [False, True])
def test_started_processing_reuses_complete_input(prepared, stage, original_key):
    owned, command, key, first = prepared
    _stage(owned, stage)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, key if original_key else new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    expected = first if original_key else replace(first, reason="already_granted")
    assert result.value == expected
    assert tuple(owned.connection.iterdump()) == before


@pytest.fixture(scope="module")
def _creation_source(tmp_path_factory):
    # 来源由真实授予生成，关闭数据库后才交给静态事实测试。
    resource = internal_read.__wrapped__(tmp_path_factory.mktemp("internal-creation"))
    try:
        owned, command = next(resource)
        plan = capture_operation(owned, lambda target: OutputsRepository().grant_file(
            command, new_operation_key(), target))
        return event_case(plan, 22)
    finally:
        resource.close()


@pytest.fixture
def creation(_creation_source):
    event, proposal = _creation_source
    event, events, owners, state, future = deepcopy((event, proposal.events, proposal.owners,
                                                     proposal.state_rows, proposal.transaction_rows))
    return event, replace(proposal, events=events, owners=owners, state_rows=state,
                          transaction_rows=future)


@pytest.mark.parametrize("field,value", [("check_state", 2), ("check_state", 3),
                                        ("repair_state", 4), ("repair_state", 5)])
def test_creation_guard_rejects_replacement_input_after_tool_started(creation, field, value):
    event, context = creation
    context.state_rows["recording_processing"][5][field] = value
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_creation_guard_accepts_current_input_preparation(creation):
    event, context = creation
    assert validate_event(event, context).branch_name == "CREATE"


@pytest.mark.parametrize("field,value", [("check_state", 2), ("repair_state", 4)])
def test_future_tool_stage_does_not_prevent_current_input_creation(creation, field, value):
    event, context = creation
    context.transaction_rows["recording_processing"][5][field] = value
    assert validate_event(event, context).branch_name == "CREATE"


@pytest.mark.parametrize("field", ["check_state", "repair_state"])
@pytest.mark.parametrize("value", [None, True, 99])
def test_creation_guard_requires_interpretable_processing_stage(creation, field, value):
    event, context = creation
    context.state_rows["recording_processing"][5][field] = value
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_creation_guard_accepts_exact_json_integer_stage(creation):
    event, context = creation
    context.state_rows["recording_processing"][5].update(
        check_state=Decimal("1.0"), repair_state=Decimal("1e0"))
    assert validate_event(event, context).branch_name == "CREATE"
