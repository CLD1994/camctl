"""K2 精确 JSON 解析、数值判断与存在性的单元测试。

期望值独立构造（Fraction 独立预期、常量字符串对照），
不调用被测实现产生期望。
"""

from __future__ import annotations

from decimal import Decimal, getcontext
from fractions import Fraction

import pytest

from camctl.contracts.json_values import (
    MISSING,
    JsonParseError,
    is_json_integer,
    is_multiple,
    json_field,
    parse_exact_json,
)

_LONE_SURROGATE = chr(0xD800)
_TRAILING_SURROGATE = chr(0xDC00)


class TestParseExactJson:
    def test_decimal_context_does_not_change_value(self) -> None:
        original = getcontext().prec
        try:
            getcontext().prec = 5
            value = parse_exact_json('{"v": 1.0000000000000001}')
            assert value["v"] == Decimal("1.0000000000000001")
            assert is_json_integer(Decimal("1e0")) is True
            assert is_json_integer(Decimal("1.0000000000000001")) is False
            assert is_json_integer(Decimal("-2.5")) is False
        finally:
            getcontext().prec = original

    def test_integers_stay_int_and_exponent_becomes_decimal(self) -> None:
        value = parse_exact_json('{"a": 1, "b": 1.0, "c": 1e2, "d": -0}')
        assert value["a"] == 1 and isinstance(value["a"], int) and not isinstance(value["a"], bool)
        assert value["b"] == Decimal("1.0")
        assert value["c"] == Decimal("1e2") == 100
        assert value["d"] == 0

    @pytest.mark.parametrize(
        "text",
        [
            '{"a": 1, "a": 2}',
            '{"x": {"a": 1, "a": 2}}',
            'NaN',
            'Infinity',
            '-Infinity',
            '{"v": NaN}',
            '{"v": Infinity}',
            '{"v": \\ud800}',
            '"\\ud800abc"',
            '"lag\\ud83d"',
            '{"a": [1,]}',
            "{'a': 1}",
            '{"a": 1',
            '',
            '01',
        ],
    )
    def test_illegal_json_is_rejected(self, text: str) -> None:
        with pytest.raises(JsonParseError):
            parse_exact_json(text)

    def test_non_string_input_is_type_error(self) -> None:
        with pytest.raises(ValueError):
            parse_exact_json(b"{}")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            parse_exact_json(None)  # type: ignore[arg-type]

    def test_raw_unpaired_surrogate_in_text_rejected(self) -> None:
        with pytest.raises(JsonParseError):
            parse_exact_json('{"k": "' + "ab" + _LONE_SURROGATE + '"')
        with pytest.raises(JsonParseError):
            parse_exact_json('"tail' + _TRAILING_SURROGATE + '"')

    def test_valid_unicode_passes(self) -> None:
        value = parse_exact_json('{"k": "café 中文 é", "emoji": "😀"}')
        assert value["k"] == "café 中文 é"
        assert value["emoji"] == "😀"

    def test_structure_round_trip_preserves_facts(self) -> None:
        text = '{"b": [1, "2", null, true], "a": {"nested": 1.5}}'
        value = parse_exact_json(text)
        assert isinstance(value, dict)
        assert list(value.keys()) == ["b", "a"]
        assert value["b"][3] is True
        assert value["a"]["nested"] == Decimal("1.5")


class TestMissing:
    def test_missing_is_distinct_from_null(self) -> None:
        value = parse_exact_json('{"present": null}')
        assert value["present"] is None
        assert value["present"] is not MISSING
        assert json_field(value, "present") is None
        assert json_field(value, "absent") is MISSING
        assert json_field(value, "absent") is not None

    def test_missing_is_singleton(self) -> None:
        assert repr(MISSING) == "MISSING"

    def test_missing_is_not_boolean(self) -> None:
        with pytest.raises(TypeError):
            bool(MISSING)  # noqa: B023 - 验证哨兵不允许布尔使用

    def test_json_field_requires_object(self) -> None:
        with pytest.raises(ValueError):
            json_field([1, 2], "a")  # type: ignore[arg-type]


class TestNumberJudgements:
    def test_is_json_integer_excludes_bool_and_other_types(self) -> None:
        assert is_json_integer(7) is True
        assert is_json_integer(True) is False
        assert is_json_integer(False) is False
        assert is_json_integer("7") is False
        assert is_json_integer(None) is False
        assert is_json_integer(7.5) is False
        assert is_json_integer([]) is False
        assert is_json_integer(Decimal("-3.00")) is True
        assert is_json_integer(Decimal("1E+40")) is True
        assert is_json_integer(Decimal("1E-40")) is False

    def test_is_multiple_uses_independent_rationals(self) -> None:
        # 独立预期：商写成 Fraction，分母为 1 即整倍数。
        original = getcontext().prec
        try:
            getcontext().prec = 4
            cases = [
                (Decimal("0.3"), Decimal("0.1"),
                 (Fraction(Decimal("0.3")) / Fraction(Decimal("0.1"))).denominator == 1),
                (Decimal("0.07"), Decimal("0.01"), True),
                (10, 3, False),
                (12, 4, True),
                (Decimal("1.0005"), Decimal("0.0001"), True),
                (Decimal("1e-2"), Decimal("1e-3"), True),
                (0, 5, True),
                (7, 1, True),
            ]
            for value, divisor, expected in cases:
                assert is_multiple(value, divisor) is expected, (value, divisor)
        finally:
            getcontext().prec = original

    def test_is_multiple_rejects_bad_inputs(self) -> None:
        with pytest.raises(ValueError):
            is_multiple(True, 1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            is_multiple(1, True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            is_multiple(1, 0)
        with pytest.raises(ValueError):
            is_multiple(Decimal("NaN"), 1)
