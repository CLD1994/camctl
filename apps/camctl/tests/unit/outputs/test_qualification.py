"""读取配置只接受可保存的次数和精确秒数。"""

from decimal import Decimal, localcontext

import pytest

from camctl.outputs.qualification import OperationConfig


@pytest.mark.parametrize("attempts", [True, False, 0, -1, 1.0, "1", None, 9223372036854775808])
def test_read_config_rejects_invalid_attempt_limit(attempts):
    with pytest.raises(ValueError):
        OperationConfig(attempts, Decimal("1"), Decimal("0"))


@pytest.mark.parametrize("timeout", [Decimal("0"), Decimal("-0"), Decimal("-1"),
    Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"),
    True, False, 1, 1.0, "1", None])
def test_read_config_requires_finite_positive_decimal_timeout(timeout):
    with pytest.raises(ValueError):
        OperationConfig(1, timeout, Decimal("0"))


@pytest.mark.parametrize("interval", [Decimal("-0.001"), Decimal("NaN"), Decimal("sNaN"),
    Decimal("Infinity"), Decimal("-Infinity"), True, False, 0, 0.0, "0", None])
def test_read_config_requires_finite_nonnegative_decimal_interval(interval):
    with pytest.raises(ValueError):
        OperationConfig(1, Decimal("1"), interval)


@pytest.mark.parametrize("attempts", [1, 9223372036854775807])
@pytest.mark.parametrize("interval", [Decimal("0"), Decimal("0.0000000000000000001")])
def test_read_config_preserves_exact_values(attempts, interval):
    with localcontext() as context:
        context.prec = 2
        config = OperationConfig(attempts, Decimal("1.234567890123456789"), interval)
    assert config.max_attempts == attempts
    assert config.timeout_s.as_tuple() == Decimal("1.234567890123456789").as_tuple()
    assert config.retry_interval_s.as_tuple() == interval.as_tuple()
