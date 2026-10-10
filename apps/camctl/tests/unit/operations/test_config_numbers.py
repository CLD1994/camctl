"""流程及尝试配置的数值守卫拒绝非有限秒数。"""

from decimal import Decimal

import pytest

from camctl.history.validators import EventValidationError
from camctl.persistence.repositories.operations import _check_config_numbers


@pytest.mark.parametrize("field", ["timeout_s_json", "retry_interval_s_json"])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_config_guard_rejects_nonfinite_seconds(field, value):
    with pytest.raises(EventValidationError):
        _check_config_numbers({field: value}, "operation_runs#1")


@pytest.mark.parametrize("field", ["timeout_s_json", "retry_interval_s_json"])
def test_config_guard_classifies_invalid_number_as_event_error(field):
    with pytest.raises(EventValidationError) as raised:
        _check_config_numbers({field: "not-a-number"}, "operation_attempts#2")
    assert isinstance(raised.value.__cause__, ValueError)
