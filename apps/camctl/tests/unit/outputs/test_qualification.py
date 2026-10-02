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
        source_device_file_id=501, target_extension="part",
        delivery_extension="mp4", delivery_display_name="录像",
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
    candidate = replace(_candidate(), item_id=None, processing_id=5, output_id=None,
                        delivery_extension=None, delivery_display_name=None)
    assert candidate.processing_id == 5
    assert candidate.output_id is None


def test_internal_candidate_rejects_formal_output_identity():
    internal = replace(_candidate(), item_id=None, processing_id=5, output_id=None,
                       delivery_extension=None, delivery_display_name=None)
    with pytest.raises(ValueError):
        replace(internal, output_id=701)


def test_delivery_candidate_requires_formal_output_identity():
    with pytest.raises(ValueError):
        replace(_candidate(), output_id=None)


@pytest.mark.parametrize("config", [None, {}, 3])
def test_read_candidate_requires_validated_configuration(config):
    with pytest.raises(ValueError):
        replace(_candidate(), config=config)


@pytest.mark.parametrize("field", ["target_extension", "delivery_extension"])
@pytest.mark.parametrize("invalid", ["", "../mp4", "a.b", "mp4\n", True, 1])
def test_read_candidate_rejects_invalid_extension(field, invalid):
    with pytest.raises(ValueError):
        replace(_candidate(), **{field: invalid})


def test_delivery_candidate_requires_extension():
    with pytest.raises(ValueError):
        replace(_candidate(), delivery_extension=None)


def test_target_candidate_can_omit_extension():
    assert replace(_candidate(), target_extension=None).target_extension is None


@pytest.mark.parametrize("invalid", [None, "", 1, True])
def test_delivery_candidate_requires_display_name(invalid):
    with pytest.raises(ValueError):
        replace(_candidate(), delivery_display_name=invalid)


def test_internal_candidate_does_not_accept_delivery_metadata():
    with pytest.raises(ValueError):
        replace(_candidate(), item_id=None, processing_id=5, output_id=None)


def test_local_candidate_uses_host_source_without_device_configuration():
    candidate = replace(_candidate(), source_device_file_id=None,
                        source_intermediate_file_id=801, config=None)
    assert candidate.source_intermediate_file_id == 801
    assert candidate.config is None


@pytest.mark.parametrize("changes", [
    {"source_device_file_id": None},
    {"source_intermediate_file_id": 801},
    {"source_device_file_id": None, "source_intermediate_file_id": 801},
    {"source_device_file_id": None, "source_intermediate_file_id": True, "config": None},
])
def test_candidate_rejects_mixed_or_incomplete_source_configuration(changes):
    with pytest.raises(ValueError):
        replace(_candidate(), **changes)


def test_internal_candidate_requires_device_source():
    with pytest.raises(ValueError):
        replace(_candidate(), item_id=None, processing_id=5, output_id=None,
                source_device_file_id=None, source_intermediate_file_id=801,
                config=None, delivery_extension=None, delivery_display_name=None)
