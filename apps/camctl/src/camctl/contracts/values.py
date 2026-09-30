"""共享值类型：身份、带单位时间及错误分类。

全部转换使用整数或 Decimal 精确运算，不经过 float；
类型错误、格式错误和范围错误分别表达，不以默认值代替。
"""

from __future__ import annotations

import re
import secrets
from datetime import date
from decimal import Decimal
from typing import Any

MAX_OBJECT_ID = 9223372036854775807
MIN_OBJECT_ID = 1

#: 对象 ID 的规范十进制字符串：首位 1～9 的 ASCII 数字，无符号、前导零或空白。
_CANONICAL_ID = re.compile(r"\A[1-9][0-9]*\Z")

#: 时间字面量固定格式 YYYY-MM-DD HH:mm:ss，只接受 ASCII 数字。
_TIME_LITERAL = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\Z")

_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_MICROS_PER_DAY = 86_400_000_000


class ValueTypeError(ValueError):
    """输入不是所属字段要求的 JSON 类型。"""


class ValueFormatError(ValueError):
    """字符串写法不符合字段的固定格式。"""


class ValueRangeError(ValueError):
    """数学值超出字段允许的范围，或不能无损表示为目标单位。"""


class ObjectId(int):
    """经范围校验的数据库对象身份，取值 1～2^63-1。"""

    __slots__ = ()

    def __new__(cls, value: int) -> "ObjectId":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueTypeError(f"对象身份必须是整数: {value!r}")
        if not MIN_OBJECT_ID <= value <= MAX_OBJECT_ID:
            raise ValueRangeError(f"对象身份超出范围 1～{MAX_OBJECT_ID}: {value}")
        return super().__new__(cls, value)


class UtcMicros(int):
    """UTC 起点计的整数微秒；合法日期范围由字段契约约束。"""

    __slots__ = ()

    def __new__(cls, value: int) -> "UtcMicros":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueTypeError(f"UTC 微秒必须是整数: {value!r}")
        return super().__new__(cls, value)


class DurationMillis(int):
    """非负整数毫秒时长；是否要求为正由所属字段契约决定。"""

    __slots__ = ()

    def __new__(cls, value: int) -> "DurationMillis":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueTypeError(f"毫秒时长必须是整数: {value!r}")
        if value < 0:
            raise ValueRangeError(f"毫秒时长不能为负: {value}")
        return super().__new__(cls, value)


#: 操作身份固定为 32 位小写十六进制，与历史事务表的登记约束一致。
_OPERATION_KEY = re.compile(r"\A[0-9a-f]{32}\Z")


class OperationKey(str):
    """一次完整数据库业务操作的稳定身份。"""

    __slots__ = ()

    def __new__(cls, raw: str) -> "OperationKey":
        if not isinstance(raw, str) or _OPERATION_KEY.match(raw) is None:
            raise ValueFormatError(f"操作身份必须是 32 位小写十六进制: {raw!r}")
        return super().__new__(cls, raw)


def new_operation_key() -> OperationKey:
    """生成新的操作身份；其输入、阶段和目标含义由发起方赋予。"""
    return OperationKey(secrets.token_hex(16))


def parse_object_id(raw: Any) -> ObjectId:
    """从公共 JSON 值解析对象身份。

    只接受规范十进制字符串；JSON 数字、布尔值、null、容器、
    非规范写法及越界值分别按类型、格式或范围错误拒绝。
    """
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueTypeError(f"对象身份必须是规范十进制字符串: {raw!r}")
    if not _CANONICAL_ID.match(raw):
        raise ValueFormatError(f"对象身份不是规范十进制写法: {raw!r}")
    value = int(raw)
    if value > MAX_OBJECT_ID:
        raise ValueRangeError(f"对象身份超出范围 1～{MAX_OBJECT_ID}: {raw}")
    return ObjectId(value)


def make_object_id(value: int) -> ObjectId:
    """从内部整数构造对象身份，拒绝布尔值与范围外整数。"""
    return ObjectId(value)


def format_object_id(value: ObjectId) -> str:
    """生成公共 JSON 使用的规范十进制字符串。"""
    if not isinstance(value, ObjectId):
        raise ValueTypeError(f"预期对象身份值: {value!r}")
    return str(int(value))


def to_utc_micros(raw: Any) -> UtcMicros:
    """把 ``YYYY-MM-DD HH:mm:ss`` 的 UTC 时间字面量转换为整数微秒。

    使用整数的日与秒运算，本地时区不参与转换；
    小数秒、时区后缀、首尾空白及不真实的日期都按格式错误拒绝。
    """
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueTypeError(f"时间字面量必须是字符串: {raw!r}")
    if not _TIME_LITERAL.match(raw):
        raise ValueFormatError(f"时间字面量不符合固定秒级格式: {raw!r}")
    year, month, day = int(raw[0:4]), int(raw[5:7]), int(raw[8:10])
    hour, minute, second = int(raw[11:13]), int(raw[14:16]), int(raw[17:19])
    if hour > 23 or minute > 59 or second > 59:
        raise ValueFormatError(f"时间分量超出范围: {raw!r}")
    try:
        ordinal = date(year, month, day).toordinal()
    except ValueError as error:
        raise ValueFormatError(f"日期不真实: {raw!r}") from error
    micros = (ordinal - _EPOCH_ORDINAL) * _MICROS_PER_DAY
    micros += (hour * 3600 + minute * 60 + second) * 1_000_000
    return UtcMicros(micros)


def format_utc_micros(value: UtcMicros) -> str:
    """从整数微秒还原唯一的时间字面量。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueTypeError(f"UTC 微秒必须是整数: {value!r}")
    total_seconds, rem = divmod(int(value), 1_000_000)
    if rem != 0:
        raise ValueFormatError(f"微秒值不是整秒: {value}")
    ordinal, day_seconds = divmod(total_seconds, 86400)
    try:
        moment = date.fromordinal(ordinal + _EPOCH_ORDINAL)
    except ValueError as error:
        raise ValueRangeError(f"微秒值超出可表达日期范围: {value}") from error
    hour, rem = divmod(day_seconds, 3600)
    minute, second = divmod(rem, 60)
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} {hour:02d}:{minute:02d}:{second:02d}"


def seconds_to_duration_ms(seconds: Any) -> DurationMillis:
    """把秒转换为整数毫秒，只接受可精确表示的值。

    数学值为整数毫秒的 int 或 Decimal 原样转换；
    非有限值、不可整除的秒及字符串、布尔值等其他类型分别拒绝。
    """
    if isinstance(seconds, bool):
        raise ValueTypeError(f"秒值类型非法: {seconds!r}")
    if isinstance(seconds, int):
        scaled = Decimal(seconds * 1000)
    elif isinstance(seconds, Decimal):
        if not seconds.is_finite():
            raise ValueRangeError(f"秒值不是有限数: {seconds}")
        scaled = seconds * 1000
    else:
        raise ValueTypeError(f"秒值类型非法: {seconds!r}")
    if scaled != scaled.to_integral_value():
        raise ValueRangeError(f"秒值不能精确表示为整数毫秒: {seconds}")
    result = int(scaled)
    if result < 0:
        raise ValueRangeError(f"毫秒时长不能为负: {seconds}")
    return DurationMillis(result)
