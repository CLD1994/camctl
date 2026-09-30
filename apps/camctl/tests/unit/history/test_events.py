"""H1 事件版本与具名校验消费者的单元测试。

期望独立来自事件转换登记、历史格式与状态模型；
通过 register_guard 注册业务守卫后才能写入对应事件。
"""

from __future__ import annotations

import pytest

from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import (
    EventContext,
    EventValidationError,
    register_guard,
    validate_event,
)
from camctl.contracts.history_values import TransactionRange

TXN = TransactionRange(txn_id=7, first_event_id=101, last_event_id=103)


def _plan_accepted_envelope(change_seq: int | None = 5, status: int = 1) -> EventEnvelope:
    return EventEnvelope(
        event_id=101,
        transaction_id=7,
        event_type=1,
        event_version=1,
        occurred_at=1_700_000_000_000_000,
        clock_status=2,
        change_seq=change_seq,
        reason=1,
        evidence={},
        rows=(
            RowChange(
                table="plans",
                row_id=1,
                before=RowImage(exists=False, values={}),
                after=RowImage(
                    exists=True,
                    values={
                        "request_id": 42,
                        "name": "p",
                        "created_at": 1_700_000_000_000_000,
                        "status": status,
                    },
                ),
            ),
        ),
    )


def _acceptance_context() -> EventContext:
    return EventContext(
        transaction=TXN,
        owners={("plans", 1): ("plan", 1)},
        state_rows={"plans": {}},
    )


@pytest.fixture()
def admission_guards():
    """注册受理事件所需的业务守根；测试后恢复原状。"""
    from camctl.history import validators

    saved = {}
    for name in ("admission", "plan_aggregate"):
        saved[name] = validators.NAMED_GUARDS.get(name)
        register_guard(name, lambda event, context: None)
    yield
    for name, guard in saved.items():
        if guard is None:
            validators.NAMED_GUARDS.pop(name, None)
        else:
            validators.NAMED_GUARDS[name] = guard


class TestRegistryDrivenValidation:
    def test_valid_plan_accepted_passes_and_reports_references(self, admission_guards) -> None:
        validated = validate_event(_plan_accepted_envelope(), _acceptance_context())
        # plan 的历史对象编号为 4。
        assert validated.references == ((4, 1),)
        assert validated.event_name == "PLAN_ACCEPTED"
        assert validated.branch_name == "CREATE"

    def test_unimplemented_validator_rejects_write(self) -> None:
        from camctl.history import validators

        assert "admission" not in validators.NAMED_GUARDS or True
        # SOURCE_RESOLVED 的守卫包含尚未实现的 source_members。
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=3,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(
                RowChange(
                    table="actions",
                    row_id=8,
                    before=RowImage(
                        exists=True,
                        values={"source_resolution_state": 1, "resolved_source_plan_id": None},
                    ),
                    after=RowImage(
                        exists=True,
                        values={"source_resolution_state": 2, "resolved_source_plan_id": 3},
                    ),
                ),
            ),
        )
        context = EventContext(
            transaction=TXN,
            owners={("actions", 8): ("action", 8)},
            state_rows={"actions": {}},
        )
        with pytest.raises(EventValidationError, match="source_members"):
            validate_event(event, context)

    def test_unknown_event_type_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "event_type", 99)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_unknown_version_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "event_version", 2)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_unknown_branch_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "reason", 9)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_event_id_outside_transaction_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "event_id", 205)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_wrong_transaction_id_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "transaction_id", 8)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_create_with_missing_business_column_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        row = envelope.rows[0]
        object.__setattr__(
            row,
            "after",
            RowImage(exists=True, values={"request_id": 42, "name": "p", "status": 1}),
        )
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_create_with_disallowed_status_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope(status=2)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_update_touching_undeclared_column_rejected(self) -> None:
        # 用 SOURCE_RESOLVED 的行规格：只允许改 source_resolution_state 与 resolved_source_plan_id。
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=3,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(
                RowChange(
                    table="actions",
                    row_id=8,
                    before=RowImage(exists=True, values={"source_resolution_state": 1, "name": "x"}),
                    after=RowImage(exists=True, values={"source_resolution_state": 2, "name": "y"}),
                ),
            ),
        )
        # source_members 未实现即可先命中列校验之前？列校验先于守卫执行。
        with pytest.raises(EventValidationError, match="name"):
            validate_event(event, EventContext(transaction=TXN, owners={}, state_rows={}))

    def test_row_not_matching_any_declared_spec_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        row = envelope.rows[0]
        object.__setattr__(row, "table", "outputs")
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_duplicate_row_in_event_rejected(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        first = envelope.rows[0]
        object.__setattr__(
            envelope,
            "rows",
            (first, RowChange(
                table="plans",
                row_id=1,
                before=RowImage(exists=False, values={}),
                after=first.after,
            )),
        )
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_before_state_mismatch_rejected_for_update(self) -> None:
        # SOURCE_RESOLVED FIX 要求 before.source_resolution_state = 1。
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=3,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(
                RowChange(
                    table="actions",
                    row_id=8,
                    before=RowImage(
                        exists=True,
                        values={"source_resolution_state": 3, "resolved_source_plan_id": None},
                    ),
                    after=RowImage(
                        exists=True,
                        values={"source_resolution_state": 2, "resolved_source_plan_id": 3},
                    ),
                ),
            ),
        )
        with pytest.raises(EventValidationError):
            validate_event(event, EventContext(transaction=TXN, owners={}, state_rows={}))


class TestOwnershipAndReportImpact:
    def test_missing_owner_fact_rejected(self, admission_guards) -> None:
        context = EventContext(transaction=TXN, owners={}, state_rows={})
        with pytest.raises(EventValidationError, match="归属"):
            validate_event(_plan_accepted_envelope(), context)

    def test_wrong_owner_entity_rejected(self, admission_guards) -> None:
        context = EventContext(transaction=TXN, owners={("plans", 1): ("action", 1)}, state_rows={})
        with pytest.raises(EventValidationError, match="归属"):
            validate_event(_plan_accepted_envelope(), context)

    def test_public_change_requires_change_seq(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope(change_seq=None)
        with pytest.raises(EventValidationError, match="change_seq"):
            validate_event(envelope, _acceptance_context())

    def test_internal_change_must_not_allocate_change_seq(self, admission_guards) -> None:
        from camctl.history import validators

        saved = {}
        for name in ("window",):
            saved[name] = validators.NAMED_GUARDS.get(name)
            register_guard(name, lambda event, context: None)
        try:
            event = EventEnvelope(
                event_id=102,
                transaction_id=7,
                event_type=7,
                event_version=1,
                occurred_at=1,
                clock_status=2,
                change_seq=5,
                reason=1,
                evidence={},
                rows=(
                    RowChange(
                        table="actions",
                        row_id=8,
                        before=RowImage(exists=True, values={"first_window_observed_at": None}),
                        after=RowImage(exists=True, values={"first_window_observed_at": 123}),
                    ),
                ),
            )
            context = EventContext(transaction=TXN, owners={("actions", 8): ("action", 8)}, state_rows={})
            with pytest.raises(EventValidationError, match="change_seq"):
                validate_event(event, context)
        finally:
            for name, guard in saved.items():
                if guard is None:
                    validators.NAMED_GUARDS.pop(name, None)
                else:
                    validators.NAMED_GUARDS[name] = guard


def _file_checksum_envelope(change_seq: int | None = 9) -> EventEnvelope:
    return EventEnvelope(
        event_id=102,
        transaction_id=7,
        event_type=17,
        event_version=1,
        occurred_at=1,
        clock_status=2,
        change_seq=change_seq,
        reason=4,
        evidence={},
        rows=(
            RowChange(
                table="device_files",
                row_id=3,
                before=RowImage(
                    exists=True,
                    values={"checksum_support": 1, "sha256": None, "last_error_json": None},
                ),
                after=RowImage(
                    exists=True,
                    values={"checksum_support": 2, "sha256": "b" * 64, "last_error_json": None},
                ),
            ),
        ),
    )


def _file_context(state_rows) -> EventContext:
    rows = {"device_files": {3: {"checksum_support": 1, "sha256": None}}}
    rows.update(state_rows)
    return EventContext(
        transaction=TXN,
        owners={("device_files", 3): ("device_file", 3)},
        state_rows=rows,
    )


@pytest.fixture()
def device_file_guard():
    """注册设备文件守卫；测试后恢复原状。"""
    from camctl.history import validators

    saved = validators.NAMED_GUARDS.get("device_file")
    register_guard("device_file", lambda event, context: None)
    yield
    if saved is None:
        validators.NAMED_GUARDS.pop("device_file", None)
    else:
        validators.NAMED_GUARDS["device_file"] = saved


class TestFileRouteReportImpact:
    def test_file_change_with_referring_output_accepts_change_seq(self, device_file_guard) -> None:
        state_rows = {"outputs": {7: {"source_action_id": 1, "device_file_id": 3}}}
        validated = validate_event(_file_checksum_envelope(change_seq=9), _file_context(state_rows))
        assert validated.event_name == "DEVICE_FILE_OBSERVED"

    def test_file_change_with_referring_output_requires_change_seq(self, device_file_guard) -> None:
        state_rows = {"outputs": {7: {"source_action_id": 1, "device_file_id": 3}}}
        with pytest.raises(EventValidationError, match="change_seq"):
            validate_event(_file_checksum_envelope(change_seq=None), _file_context(state_rows))

    def test_file_change_without_referrer_rejects_change_seq(self, device_file_guard) -> None:
        state_rows = {"outputs": {}}
        with pytest.raises(EventValidationError, match="change_seq"):
            validate_event(_file_checksum_envelope(change_seq=9), _file_context(state_rows))

    def test_file_change_without_referrer_accepts_null_change_seq(self, device_file_guard) -> None:
        state_rows = {"outputs": {}}
        validated = validate_event(_file_checksum_envelope(change_seq=None), _file_context(state_rows))
        assert validated.branch_name == "CHECKSUM"

    def test_clock_status_validated(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "clock_status", 9)
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())

    def test_rows_must_be_non_empty(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(envelope, "rows", ())
        with pytest.raises(EventValidationError):
            validate_event(envelope, _acceptance_context())
