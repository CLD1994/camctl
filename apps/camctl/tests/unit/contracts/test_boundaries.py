"""K3 完整历史边界与分页结果的单元测试。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from camctl.contracts.history_values import (
    INITIAL_BOUNDARY,
    BoundaryError,
    HistoryBoundary,
    ReadOrder,
    ReadScope,
    TransactionRange,
    validate_boundary,
    validate_page,
)
from camctl.contracts.pages import Page


@pytest.fixture()
def transaction() -> TransactionRange:
    return TransactionRange(txn_id=1, first_event_id=201, last_event_id=203)


class TestHistoryBoundary:
    def test_initial_boundary_is_zero_zero(self) -> None:
        assert INITIAL_BOUNDARY.txn_id == 0
        assert INITIAL_BOUNDARY.last_event_id == 0

    @pytest.mark.parametrize("txn_id,last_event_id", [(0, 1), (1, 0), (-1, 0), (0, -1)])
    def test_partial_zero_boundary_is_invalid(self, txn_id: int, last_event_id: int) -> None:
        with pytest.raises(BoundaryError):
            HistoryBoundary(txn_id=txn_id, last_event_id=last_event_id)

    def test_matching_transaction_end_is_accepted(self, transaction: TransactionRange) -> None:
        validate_boundary(HistoryBoundary(1, 203), transaction)

    def test_mid_transaction_position_is_rejected(self, transaction: TransactionRange) -> None:
        with pytest.raises(BoundaryError):
            validate_boundary(HistoryBoundary(1, 202), transaction)

    def test_wrong_transaction_id_is_rejected(self, transaction: TransactionRange) -> None:
        with pytest.raises(BoundaryError):
            validate_boundary(HistoryBoundary(2, 203), transaction)

    def test_first_event_of_multi_event_transaction_is_not_complete(self) -> None:
        with pytest.raises(BoundaryError):
            validate_boundary(HistoryBoundary(1, 201), TransactionRange(1, 201, 203))

    def test_single_event_transaction_end_matches(self) -> None:
        validate_boundary(HistoryBoundary(5, 30), TransactionRange(5, 30, 30))

    def test_initial_boundary_has_no_transaction(self, transaction: TransactionRange) -> None:
        with pytest.raises(BoundaryError):
            validate_boundary(INITIAL_BOUNDARY, transaction)

    def test_transaction_range_validation(self) -> None:
        with pytest.raises(BoundaryError):
            TransactionRange(txn_id=0, first_event_id=1, last_event_id=1)
        with pytest.raises(BoundaryError):
            TransactionRange(txn_id=1, first_event_id=5, last_event_id=4)

    def test_boundary_equality_and_immutability(self, transaction: TransactionRange) -> None:
        boundary = HistoryBoundary(1, 203)
        assert boundary == HistoryBoundary(1, 203)
        assert boundary != HistoryBoundary(1, 202)
        with pytest.raises(dataclasses.FrozenInstanceError):
            boundary.txn_id = 2  # type: ignore[misc]


class TestPage:
    def test_empty_page_can_continue(self) -> None:
        page: Page[int] = Page(items=(), next_cursor=41)
        assert page.exhausted is False

    def test_nonempty_page_can_continue(self) -> None:
        page: Page[int] = Page(items=(1, 2), next_cursor=41)
        assert page.exhausted is False
        assert tuple(page.items) == (1, 2)

    def test_last_page_keeps_items(self) -> None:
        page: Page[int] = Page(items=(7,), next_cursor=None)
        assert page.exhausted is True
        assert tuple(page.items) == (7,)

    def test_empty_page_is_exhausted(self) -> None:
        page: Page[int] = Page(items=(), next_cursor=None)
        assert page.exhausted is True

    def test_exhausted_is_not_a_constructor_argument(self) -> None:
        with pytest.raises(TypeError):
            Page(items=(), next_cursor=None, exhausted=True)  # type: ignore[call-arg]

    def test_exhausted_is_not_assignable(self) -> None:
        page: Page[int] = Page(items=(), next_cursor=None)
        with pytest.raises(AttributeError):
            page.exhausted = False  # type: ignore[misc]

    def test_items_must_be_a_sequence(self) -> None:
        with pytest.raises(ValueError):
            Page(items=None, next_cursor=None)  # type: ignore[arg-type]


class TestValidatePage:
    @staticmethod
    def _scope(
        order: ReadOrder = ReadOrder.ASCENDING,
        previous: int | None = 10,
        upper: int | None = 100,
        limit: int = 5,
    ) -> ReadScope[int]:
        return ReadScope(
            order=order,
            previous_position=previous,
            upper_position=upper,
            lower_position=1,
            batch_limit=limit,
            cursor_position=lambda cursor: cursor,
        )

    def test_advancing_cursor_is_accepted(self) -> None:
        page: Page[int] = Page(items=(1,), next_cursor=11)
        validate_page(page, self._scope())

    def test_ascending_cursor_must_strictly_advance(self) -> None:
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor=10), self._scope())
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor=9), self._scope())

    def test_descending_cursor_must_strictly_decrease(self) -> None:
        scope = self._scope(order=ReadOrder.DESCENDING)
        validate_page(Page(items=(), next_cursor=9), scope)
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor=10), scope)
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor=11), scope)

    def test_cursor_beyond_fixed_upper_bound_is_rejected(self) -> None:
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor=101), self._scope())

    def test_cursor_below_lower_bound_is_rejected(self) -> None:
        scope = self._scope(order=ReadOrder.DESCENDING, previous=50)
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor=0), scope)

    def test_first_read_has_no_previous_position(self) -> None:
        scope = self._scope(previous=None)
        validate_page(Page(items=(), next_cursor=1), scope)

    def test_batch_over_limit_is_rejected(self) -> None:
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(1, 2, 3, 4, 5, 6), next_cursor=None), self._scope())

    def test_exhausted_page_skips_cursor_checks(self) -> None:
        validate_page(Page(items=(1,), next_cursor=None), self._scope())

    def test_scope_requires_positive_limit(self) -> None:
        with pytest.raises(BoundaryError):
            self._scope(limit=0)

    def test_cursor_position_extractor_is_used(self) -> None:
        scope = ReadScope(
            order=ReadOrder.ASCENDING,
            previous_position=2,
            upper_position=100,
            lower_position=1,
            batch_limit=5,
            cursor_position=lambda cursor: cursor["position"],
        )
        validate_page(Page(items=(), next_cursor={"position": 3}), scope)
        with pytest.raises(BoundaryError):
            validate_page(Page(items=(), next_cursor={"position": 2}), scope)
