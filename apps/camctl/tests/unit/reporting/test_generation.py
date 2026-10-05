"""生成输入的独立值校验：报告身份、冻结依据与执行参数。"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.reporting.generation import GenerationSpec


def _spec(**overrides):
    values = dict(
        db_path=Path("state.db"),
        report_id=1,
        from_wm=0,
        to_wm=4,
        frozen_event_id=9,
        staging_path=Path("staging/report-1.json"),
    )
    values.update(overrides)
    return GenerationSpec(**values)


def test_valid_spec_is_accepted() -> None:
    spec = _spec()
    assert (spec.report_id, spec.from_wm, spec.to_wm) == (1, 0, 4)


@pytest.mark.parametrize("field", ["from_wm", "to_wm", "frozen_event_id"])
def test_negative_inputs_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _spec(**{field: -1})


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_non_integer_report_id_is_rejected(value) -> None:
    with pytest.raises(ValueError):
        _spec(report_id=value)


@pytest.mark.parametrize("field", ["entity_batch_size", "event_batch_size"])
def test_zero_batches_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _spec(**{field: 0})
