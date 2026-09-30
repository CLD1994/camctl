"""R1 冻结报告模型完整性验证的单元测试。"""

from __future__ import annotations

import pytest

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.reporting.models import FrozenReport, validate_frozen_report


def _report(**overrides) -> FrozenReport:
    base = dict(
        report_id=1,
        boundary=HistoryBoundary(txn_id=2, last_event_id=2),
        from_wm=0,
        to_wm=3,
        format_version=1,
        scope=(("plan", (1,)),),
    )
    base.update(overrides)
    return FrozenReport(**base)


class TestValidateFrozenReport:
    def test_valid_report_passes(self) -> None:
        validate_frozen_report(_report())

    def test_initial_boundary_with_empty_scope_passes(self) -> None:
        validate_frozen_report(
            _report(boundary=INITIAL_BOUNDARY, scope=(), from_wm=0, to_wm=0)
        )

    def test_missing_complete_boundary_rejected(self) -> None:
        with pytest.raises(ValueError, match="边界"):
            validate_frozen_report(_report(boundary=None))

    def test_partial_boundary_cannot_exist(self) -> None:
        # HistoryBoundary 构造即拒绝不完整边界：部分边界无法进入冻结模型。
        from camctl.contracts.history_values import BoundaryError

        with pytest.raises(BoundaryError):
            HistoryBoundary(txn_id=0, last_event_id=5)
        with pytest.raises(BoundaryError):
            HistoryBoundary(txn_id=3, last_event_id=0)

    def test_watermark_range_inconsistent_rejected(self) -> None:
        with pytest.raises(ValueError, match="水位"):
            validate_frozen_report(_report(from_wm=5, to_wm=3))

    def test_negative_watermark_rejected(self) -> None:
        with pytest.raises(ValueError, match="水位"):
            validate_frozen_report(_report(from_wm=-1, to_wm=0))

    def test_unsupported_format_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="格式"):
            validate_frozen_report(_report(format_version=2))

    def test_scope_with_unknown_entity_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="对象类型"):
            validate_frozen_report(_report(scope=(("nosuch", (1,)),)))

    def test_scope_ids_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="对象"):
            validate_frozen_report(_report(scope=(("plan", (0,)),)))
