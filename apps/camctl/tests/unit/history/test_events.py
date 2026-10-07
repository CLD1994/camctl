"""H1 事件版本与具名校验消费者的单元测试。

期望独立来自事件转换登记、历史格式与状态模型；
通过 register_guard 注册业务守卫后才能写入对应事件。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import (
    EventContext,
    EventValidationError,
    register_guard,
    validate_event,
    validate_event_structure,
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

        # 窗口守卫可能已被同进程更早的装配测试注册；临时摘除并在
        # 结束后恢复，使本用例不依赖测试执行顺序。
        saved_guard = validators.NAMED_GUARDS.pop("window", None)
        try:
            event = EventEnvelope(
                event_id=101,
                transaction_id=7,
                event_type=7,
                event_version=1,
                occurred_at=1,
                clock_status=1,
                change_seq=None,
                reason=1,
                evidence={},
                rows=(
                    RowChange(
                        table="actions",
                        row_id=5,
                        before=RowImage(
                            exists=True,
                            values={"first_window_observed_at": None},
                        ),
                        after=RowImage(
                            exists=True,
                            values={"first_window_observed_at": 1_750_000_000_000_000},
                        ),
                    ),
                ),
            )
            context = EventContext(
                transaction=TXN,
                owners={("actions", 5): ("action", 5)},
                state_rows={"actions": {5: {"id": 5, "type": 1, "status": 1,
                                            "cancel_requested": 0}},
                            "outputs": {}},
            )
            with pytest.raises(EventValidationError, match="具名校验未接入"):
                validate_event(event, context)
        finally:
            if saved_guard is not None:
                validators.NAMED_GUARDS["window"] = saved_guard

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
            context = EventContext(transaction=TXN, owners={("actions", 8): ("action", 8)},
                                   state_rows={"actions": {8: {"type": 1, "status": 1}}})
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
                    values={"checksum_support": 1, "sha256": None},
                ),
                after=RowImage(
                    exists=True,
                    values={"checksum_support": 2, "sha256": "b" * 64},
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


class TestStrictChangeBody:
    """E2：更新正文只保存真实变化；必要列必须实际变化。

    SOURCE_RESOLVED.FIX 声明 source_resolution_state 与
    resolved_source_plan_id 为必要更新列，用它的行规格驱动内核
    层反例。
    """

    @staticmethod
    def _source_resolved(before: dict, after: dict) -> EventEnvelope:
        return EventEnvelope(
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
                    before=RowImage(exists=True, values=before),
                    after=RowImage(exists=True, values=after),
                ),
            ),
        )

    def test_unchanged_column_in_body_is_rejected(self) -> None:
        event = self._source_resolved(
            {"source_resolution_state": 1, "resolved_source_plan_id": None},
            {"source_resolution_state": 2, "resolved_source_plan_id": None},
        )
        with pytest.raises(EventValidationError, match="没有实际变化"):
            validate_event_structure(event)

    @pytest.mark.parametrize(
        "before_value,after_value",
        [
            (None, None),
            (3, Decimal("3.0")),
            (Decimal("2"), 2),
            ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
        ],
    )
    def test_exact_json_equal_values_are_rejected_as_unchanged(
        self, before_value, after_value
    ) -> None:
        """数字同值按数学值比较；null、等值 Decimal 及成员顺序不同的
        同值对象同样折叠，不构成真实变化。"""
        event = self._source_resolved(
            {"source_resolution_state": 1, "resolved_source_plan_id": before_value},
            {"source_resolution_state": 2, "resolved_source_plan_id": after_value},
        )
        with pytest.raises(EventValidationError, match="没有实际变化"):
            validate_event_structure(event)

    def test_missing_required_column_is_rejected(self) -> None:
        """必要列未变化被构造器省略后，正文缺少该列即拒绝。"""
        event = self._source_resolved(
            {"source_resolution_state": 1}, {"source_resolution_state": 2}
        )
        with pytest.raises(EventValidationError, match="缺少必要更新列"):
            validate_event_structure(event)

    def test_empty_update_column_set_is_rejected(self) -> None:
        event = self._source_resolved({}, {})
        with pytest.raises(EventValidationError):
            validate_event_structure(event)

    def test_write_once_column_reassignment_is_rejected(self) -> None:
        """一次写列只允许从空值赋值；已赋值后再改不构成合法更新。"""
        event = self._source_resolved(
            {"source_resolution_state": 1, "resolved_source_plan_id": 5},
            {"source_resolution_state": 2, "resolved_source_plan_id": 9},
        )
        with pytest.raises(EventValidationError, match="一次写列"):
            validate_event_structure(event)

    def test_write_once_first_assignment_passes_structure(self) -> None:
        event = self._source_resolved(
            {"source_resolution_state": 1, "resolved_source_plan_id": None},
            {"source_resolution_state": 2, "resolved_source_plan_id": 9},
        )
        validate_event_structure(event)

    def test_boolean_is_not_the_flag_number_in_transitions(self) -> None:
        """未枚举登记的标志列上，布尔不得冒充数字 1 命中分支或转换。

        RETRY_WAIT_CHANGED.REQUIRE 声明 operation_runs.retry_wait_required
        从 0 到 1；True 与 1 精确不等，不能通过结构校验。
        """
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=6,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(
                RowChange(
                    table="operation_runs",
                    row_id=40,
                    before=RowImage(exists=True, values={"retry_wait_required": 0}),
                    after=RowImage(exists=True, values={"retry_wait_required": True}),
                ),
            ),
        )
        with pytest.raises(EventValidationError, match="不在允许范围"):
            validate_event_structure(event)


class TestEvidenceMembers:
    """E4：事件依据成员按分支登记使用，公共成员遵守格式类型。"""

    @staticmethod
    def _observe(evidence: dict) -> EventEnvelope:
        return EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=13,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=2,
            evidence=evidence,
            rows=(
                RowChange(
                    table="device_activities",
                    row_id=11,
                    before=RowImage(exists=True, values={"sent_at": None}),
                    after=RowImage(exists=True,
                                   values={"sent_at": 1_700_000_000_000_000}),
                ),
            ),
        )

    def test_undeclared_member_is_rejected(self) -> None:
        with pytest.raises(EventValidationError, match="登记成员"):
            validate_event_structure(self._observe({"input_key": "a" * 32}))

    def test_member_of_other_branch_is_undeclared_here(self, admission_guards) -> None:
        envelope = _plan_accepted_envelope()
        object.__setattr__(
            envelope, "evidence", {"observation": {"value": Decimal("0.1")}})
        with pytest.raises(EventValidationError, match="登记成员"):
            validate_event_structure(envelope)

    def test_observation_must_be_structured(self) -> None:
        with pytest.raises(EventValidationError, match="observation 必须是结构化对象"):
            validate_event_structure(self._observe({"observation": 5}))

    def test_declared_structured_observation_passes_structure(self) -> None:
        name, branch, _ = validate_event_structure(self._observe(
            {"observation": {"type": "stop_confirmed", "version": 1,
                             "data": {"activity_id": "11"}}}))
        assert (name, branch) == ("DEVICE_OBSERVED", "OBSERVE")

    @pytest.mark.parametrize("bad", ["9", 0, -1, True])
    def test_attempt_id_must_be_positive_integer(self, bad) -> None:
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=12,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=4,
            evidence={"attempt_id": bad},
            rows=(
                RowChange(
                    table="operation_attempts",
                    row_id=9,
                    before=RowImage(exists=True, values={"max_attempts_used": 2}),
                    after=RowImage(exists=True, values={"max_attempts_used": 3}),
                ),
            ),
        )
        with pytest.raises(EventValidationError, match="attempt_id"):
            validate_event_structure(event)

    @pytest.mark.parametrize("bad", ["abc", "a" * 31, 12])
    def test_input_key_must_be_32_hex_identity(self, bad) -> None:
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=27,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=1,
            evidence={"input_key": bad},
            rows=(
                RowChange(
                    table="plan_file_diagnostics",
                    row_id=5,
                    before=RowImage(exists=False, values={}),
                    after=RowImage(exists=True, values={
                        "input_path": "plan.json", "request_id": "42",
                        "plan_id": 1, "errors_json": {"code": "invalid_plan"},
                    }),
                ),
            ),
        )
        with pytest.raises(EventValidationError, match="input_key"):
            validate_event_structure(event)

    def test_baseline_member_must_be_positive_integer(self) -> None:
        event = EventEnvelope(
            event_id=101,
            transaction_id=7,
            event_type=14,
            event_version=1,
            occurred_at=1,
            clock_status=1,
            change_seq=None,
            reason=2,
            evidence={"activity_id": 11, "chunk_count": "3", "entry_count": 3},
            rows=(
                RowChange(
                    table="device_activities",
                    row_id=11,
                    before=RowImage(exists=True, values={"baseline_state": 2}),
                    after=RowImage(exists=True, values={"baseline_state": 3}),
                ),
            ),
        )
        with pytest.raises(EventValidationError, match="chunk_count"):
            validate_event_structure(event)
