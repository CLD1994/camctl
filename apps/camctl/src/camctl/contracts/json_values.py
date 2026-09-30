"""精确 JSON 解析、数值判断与字段存在性。

解析把整数字面量读作 ``int``、小数和指数按原文构造 ``Decimal``，
不经过 float；重复成员名、非 JSON 数值常量和未配对代理码点整份拒绝。
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any, TypeAlias

from camctl.contracts.values import ValueTypeError

JsonValue: TypeAlias = "int | Decimal | str | bool | None | list[JsonValue] | dict[str, JsonValue]"


class JsonParseError(ValueError):
    """JSON 文本无法无歧义解析为精确值。"""


class _Missing:
    """字段省略的单一哨兵；与显式 ``null`` 分开。"""

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        raise TypeError("MISSING 表示字段省略，不能作为布尔值使用")


MISSING = _Missing()


def _reject_constant(name: str) -> Any:
    raise JsonParseError(f"非 JSON 数值常量: {name}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonParseError(f"重复成员名: {key!r}")
        result[key] = value
    return result


def _check_surrogates(value: Any) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise JsonParseError("字符串包含未配对代理码点") from error
    elif isinstance(value, list):
        for item in value:
            _check_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_surrogates(key)
            _check_surrogates(item)


def parse_exact_json(text: Any) -> JsonValue:
    """无歧义解析 JSON 文本，保持数字精确。

    输入必须是字符串；解析失败、重复成员名、非 JSON 数值常量
    或未配对代理码点都使整份解析失败，不从片段提取任何值。
    """
    if isinstance(text, bool) or not isinstance(text, str):
        raise ValueTypeError(f"JSON 输入必须是字符串: {text!r}")
    try:
        value = json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs,
        )
    except JsonParseError:
        raise
    except ValueError as error:
        raise JsonParseError(f"JSON 语法非法: {error}") from error
    _check_surrogates(value)
    return value


def json_field(obj: Any, key: str) -> Any:
    """读取对象字段；省略返回 ``MISSING``，与显式 ``null`` 区分。"""
    if not isinstance(obj, dict):
        raise ValueTypeError(f"字段读取要求 JSON 对象: {obj!r}")
    if key not in obj:
        return MISSING
    return obj[key]


def is_json_integer(value: Any) -> bool:
    """精确判断 JSON 数值的数学值是否为整数。

    ``int``（排除布尔值）和数学值为整数的有限 ``Decimal`` 满足；
    字符串、布尔值、null、容器、浮点数及非整数 Decimal 不满足。
    判断直接检查数字元组，不受 Decimal 上下文影响。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        if not value.is_finite():
            return False
        digit_tuple = value.as_tuple()
        exponent = digit_tuple.exponent
        if exponent >= 0:
            return True
        fractional = -exponent
        digits = digit_tuple.digits
        significant = digits if fractional <= len(digits) else digits + (0,) * (fractional - len(digits))
        return all(digit == 0 for digit in significant[len(significant) - fractional:])
    return False


def _as_exact_rational(value: Any) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueTypeError(f"数值判断要求 int 或 Decimal: {value!r}")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueTypeError(f"数值判断要求有限数: {value}")
        return Fraction(value)
    return Fraction(value)


def is_multiple(value: Any, divisor: Any) -> bool:
    """用有理数精确判断 ``value`` 是否为 ``divisor`` 的整倍数。

    商的分母为 1 即整倍数；计算不经过 float，不受 Decimal
    上下文精度影响。除数为零拒绝。
    """
    numerator = _as_exact_rational(value)
    denominator = _as_exact_rational(divisor)
    if denominator == 0:
        raise ValueTypeError("除数不能为零")
    return (numerator / denominator).denominator == 1
