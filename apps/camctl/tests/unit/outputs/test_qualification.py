"""读取配置只接受可保存的次数和精确秒数。"""

from decimal import Decimal, localcontext

import pytest

from dataclasses import replace

from camctl.outputs.qualification import FileCandidate, OperationConfig


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


def _candidate():
    return FileCandidate(
        action_id=31, item_id=101, processing_id=None, output_id=701,
        source_device_file_id=501, target_relative_path="deliveries/1.part",
        delivery_file_name="1.mp4", delivery_display_name="录像",
        config=OperationConfig(3, Decimal("10"), Decimal("0")), occurred_at=1,
    )


@pytest.mark.parametrize("field", ["action_id", "item_id", "output_id", "source_device_file_id"])
@pytest.mark.parametrize("invalid", [True, 0, -1, 1.0, "1", 9223372036854775808])
def test_read_candidate_rejects_invalid_identity(field, invalid):
    with pytest.raises(ValueError):
        replace(_candidate(), **{field: invalid})


@pytest.mark.parametrize("invalid", [True, 1.0, "1", None, -9223372036854775809, 9223372036854775808])
def test_read_candidate_rejects_invalid_event_time(invalid):
    with pytest.raises(ValueError):
        replace(_candidate(), occurred_at=invalid)


@pytest.mark.parametrize("time", [-9223372036854775808, -1, 0, 9223372036854775807])
def test_read_candidate_accepts_sqlite_utc_microseconds(time):
    assert replace(_candidate(), occurred_at=time).occurred_at == time


def test_internal_candidate_has_processing_identity_without_formal_output():
    candidate = replace(_candidate(), item_id=None, processing_id=5, output_id=None)
    assert candidate.processing_id == 5
    assert candidate.output_id is None


def test_internal_candidate_rejects_formal_output_identity():
    with pytest.raises(ValueError):
        replace(_candidate(), item_id=None, processing_id=5)


def test_delivery_candidate_requires_formal_output_identity():
    with pytest.raises(ValueError):
        replace(_candidate(), output_id=None)


@pytest.mark.parametrize("config", [None, {}, 3])
def test_read_candidate_requires_validated_configuration(config):
    with pytest.raises(ValueError):
        replace(_candidate(), config=config)
