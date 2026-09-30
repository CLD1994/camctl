"""K1 身份、时间和整数枚举的单元测试。

用例从对象 ID 契约、时间字面量契约与整数登记独立推导预期，
不调用被测实现生成期望值。解析精确 JSON 的用例由 K2 承接。
"""

from __future__ import annotations


from datetime import datetime, timezone
from decimal import Decimal

import pytest

from camctl.contracts.enums import (
    assert_public_contracts,
    decode_member,
    encode_member,
    enum_for,
    public_text,
)
from camctl.contracts.values import (
    DurationMillis,
    ObjectId,
    OperationKey,
    UtcMicros,
    ValueFormatError,
    ValueRangeError,
    ValueTypeError,
    format_object_id,
    format_utc_micros,
    make_object_id,
    new_operation_key,
    parse_object_id,
    seconds_to_duration_ms,
    to_utc_micros,
)

MAX_ID = 9223372036854775807


class TestObjectId:
    def test_bool_is_not_object_id(self) -> None:
        with pytest.raises(ValueError):
            parse_object_id(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            make_object_id(True)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "raw",
        [
            12,
            1.0,
            None,
            ["12"],
            {"id": "12"},
            "",
            "0",
            "012",
            "+12",
            "-12",
            " 12",
            "12 ",
            "1.0",
            "1e1",
            "１２",
            "9223372036854775808",
        ],
    )
    def test_non_canonical_id_is_rejected(self, raw: object) -> None:
        with pytest.raises(ValueError):
            parse_object_id(raw)  # type: ignore[arg-type]

    def test_max_legal_value_is_exact(self) -> None:
        parsed = parse_object_id("9223372036854775807")
        assert int(parsed) == MAX_ID
        assert parsed == make_object_id(MAX_ID)

    def test_min_legal_value(self) -> None:
        assert int(parse_object_id("1")) == 1

    @pytest.mark.parametrize("value", [0, -1, MAX_ID + 1])
    def test_make_object_id_range(self, value: int) -> None:
        with pytest.raises(ValueRangeError):
            make_object_id(value)

    @pytest.mark.parametrize("value", [1.5, "12", None, Decimal("12")])
    def test_make_object_id_type(self, value: object) -> None:
        with pytest.raises(ValueTypeError):
            make_object_id(value)  # type: ignore[arg-type]

    def test_round_trip_keeps_canonical_string(self) -> None:
        for text in ["1", "42", "999999999", str(MAX_ID)]:
            assert format_object_id(parse_object_id(text)) == text

    def test_object_id_supports_exact_ordering(self) -> None:
        ordered = sorted([make_object_id(100), make_object_id(2), make_object_id(10)])
        assert [int(value) for value in ordered] == [2, 10, 100]


class TestUtcMicros:
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _expected(self, moment: datetime) -> int:
        delta = moment - self.EPOCH
        assert delta.microseconds == 0
        return (delta.days * 86400 + delta.seconds) * 1_000_000

    def test_epoch_both_sides(self) -> None:
        assert int(to_utc_micros("1970-01-01 00:00:00")) == 0
        assert int(to_utc_micros("1969-12-31 23:59:59")) == -1_000_000

    def test_earliest_and_latest_legal_dates(self) -> None:
        earliest = datetime(1, 1, 1, tzinfo=timezone.utc)
        latest = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert int(to_utc_micros("0001-01-01 00:00:00")) == self._expected(earliest)
        assert int(to_utc_micros("9999-12-31 23:59:59")) == self._expected(latest)

    def test_leap_day(self) -> None:
        leap = datetime(2024, 2, 29, 12, 0, 0, tzinfo=timezone.utc)
        assert int(to_utc_micros("2024-02-29 12:00:00")) == self._expected(leap)
        with pytest.raises(ValueError):
            to_utc_micros("2023-02-29 00:00:00")

    @pytest.mark.parametrize(
        "raw",
        [
            "2026-01-01 00:00:00.5",
            "2026-01-01T00:00:00",
            "2026-1-1 00:00:00",
            "2026/01/01 00:00:00",
            "2026-01-01 00:00",
            "2026-01-01  00:00:00",
            " 2026-01-01 00:00:00",
            "2026-01-01 00:00:00 ",
            "2026-01-01 00:00:00Z",
            "2026-01-01 24:00:00",
            "2026-01-01 00:60:00",
            "2026-01-01 00:00:60",
            "2026-13-01 00:00:00",
            "2026-01-32 00:00:00",
            "1767225600",
            "",
        ],
    )
    def test_illegal_time_literals(self, raw: str) -> None:
        with pytest.raises(ValueError):
            to_utc_micros(raw)

    def test_type_rejected(self) -> None:
        with pytest.raises(ValueTypeError):
            to_utc_micros(1767225600)  # type: ignore[arg-type]

    def test_round_trip_unique_format(self) -> None:
        raw = "2026-09-13 09:00:00"
        value = to_utc_micros(raw)
        assert format_utc_micros(value) == raw

    def test_utc_micros_type_rejects_non_integral(self) -> None:
        with pytest.raises(ValueError):
            UtcMicros(1_000_000_000_000_000.5)  # type: ignore[arg-type]


class TestDurationMillis:
    def test_exact_duration_conversion(self) -> None:
        assert seconds_to_duration_ms(Decimal("1.5")) == 1500
        assert seconds_to_duration_ms(Decimal("1e0")) == 1000
        assert seconds_to_duration_ms(Decimal("1.000")) == 1000
        assert seconds_to_duration_ms(2) == 2000
        assert seconds_to_duration_ms(0) == 0

    @pytest.mark.parametrize(
        "seconds",
        [
            Decimal("1.0005"),
            Decimal("0.0001"),
            Decimal("-1.5"),
            Decimal("NaN"),
            Decimal("Infinity"),
            1.5,
            "1.5",
            True,
            None,
        ],
    )
    def test_inexact_or_illegal_seconds(self, seconds: object) -> None:
        with pytest.raises(ValueError):
            seconds_to_duration_ms(seconds)  # type: ignore[arg-type]

    def test_duration_millis_type_rejects_bool_and_negative(self) -> None:
        with pytest.raises(ValueError):
            DurationMillis(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            DurationMillis(-1)
        assert DurationMillis(1500) == 1500


class TestRegistryEnums:
    def test_member_values_follow_registry(self) -> None:
        # 独立预期：来源为编号一览文档中的固定成员与编号。
        action_status = enum_for("actions.status")
        assert [(member.name, int(member)) for member in action_status] == [
            ("PENDING", 1),
            ("RUNNING", 2),
            ("SUCCEEDED", 3),
            ("FAILED", 4),
            ("EXPIRED", 5),
            ("CANCELED", 6),
        ]
        plan_status = enum_for("plans.status")
        assert [(member.name, int(member)) for member in plan_status] == [
            ("PENDING", 1),
            ("RUNNING", 2),
            ("COMPLETED", 3),
        ]
        action_type = enum_for("actions.type")
        assert int(action_type["CAMERA_TAKE_PHOTO"]) == 1
        assert int(action_type["REPORT_STATUS"]) == 7

    def test_unknown_code_is_rejected(self) -> None:
        action_status = enum_for("actions.status")
        with pytest.raises(ValueError):
            action_status(99)

    def test_cross_enum_mixing_is_rejected(self) -> None:
        action_status = enum_for("actions.status")
        with pytest.raises(ValueError):
            encode_member("plans.status", action_status.RUNNING)
        with pytest.raises(ValueError):
            decode_member("plans.status", action_status.RUNNING)

    def test_decode_and_round_trip(self) -> None:
        member = decode_member("actions.status", 2)
        assert member.name == "RUNNING"
        assert encode_member("actions.status", member) == 2

    def test_unknown_column_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            enum_for("plans.nonexistent")

    def test_public_text_encoding(self) -> None:
        assert public_text(enum_for("plans.status").COMPLETED) == "completed"
        assert public_text(enum_for("actions.type").CAMERA_RECORD) == "camera_record"
        with pytest.raises(ValueError):
            public_text("not-an-enum-member")  # type: ignore[arg-type]

    def test_public_contracts_agree_with_schema(self) -> None:
        # 生成的映射与权威资源一致：内部成员的小写名称集合
        # 必须与公共 Schema 登记的文本枚举完全相同。
        assert_public_contracts()

    def test_registry_itself_is_well_formed(self) -> None:
        # 独立核对登记的成员编号互不冲突且为正整数。
        from camctl.contracts.enums import load_registry

        registry = load_registry()
        combined = {**registry["enums"], **registry["json_enums"]}
        assert "actions.status" in combined
        for column, definition in combined.items():
            codes = list(definition["members"].values())
            assert all(isinstance(code, int) and not isinstance(code, bool) and code > 0 for code in codes)
            assert len(codes) == len(set(codes)), column


class TestOperationKey:
    def test_valid_key_round_trips(self) -> None:
        raw = "0123456789abcdef0123456789abcdef"
        key = OperationKey(raw)
        assert key == raw
        assert isinstance(key, str)

    def test_uppercase_hex_rejected(self) -> None:
        with pytest.raises(ValueFormatError):
            OperationKey("0123456789ABCDEF0123456789ABCDEF")

    def test_wrong_length_rejected(self) -> None:
        with pytest.raises(ValueFormatError):
            OperationKey("0123")
        with pytest.raises(ValueFormatError):
            OperationKey("0" * 33)

    def test_non_hex_characters_rejected(self) -> None:
        with pytest.raises(ValueFormatError):
            OperationKey("g" * 32)

    def test_non_string_input_rejected(self) -> None:
        for bad in (16, None, True, b"0" * 32, 1.5):
            with pytest.raises(ValueFormatError):
                OperationKey(bad)  # type: ignore[arg-type]

    def test_new_operation_key_format_and_distinct(self) -> None:
        first = new_operation_key()
        second = new_operation_key()
        assert OperationKey(first) == first
        assert first != second
