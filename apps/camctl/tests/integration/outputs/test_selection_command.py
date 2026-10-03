"""真实协议登记装配下，选择固定命令建立规范身份和事实时刻。"""

from decimal import Decimal

import pytest

from camctl.outputs.sources import SelectionSnapshot
from camctl.persistence.repositories.outputs import FixSelection


@pytest.mark.parametrize("value", [True, 61.0, Decimal(61), "61", None, 0, -1, 2**63])
def test_selection_identity_requires_a_bounded_integer(value):
    with pytest.raises(ValueError):
        FixSelection(value, SelectionSnapshot(True), 0)


@pytest.mark.parametrize("value", [True, 1000.0, Decimal(1000), "1000", None, -(2**63)-1, 2**63])
def test_selection_time_requires_signed_integer_microseconds(value):
    with pytest.raises(ValueError):
        FixSelection(61, SelectionSnapshot(True), value)


@pytest.mark.parametrize("value", [-(2**63), -1, 0, 2**63-1])
def test_selection_time_preserves_zero_negative_and_boundary_values(value):
    command = FixSelection(61, SelectionSnapshot(True), value)
    assert command.occurred_at == value
