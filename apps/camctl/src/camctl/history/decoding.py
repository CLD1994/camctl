"""存储的格式 1 历史事件解码，不执行任何业务决定或外部副作用。"""
from __future__ import annotations

from typing import Any, Sequence

from camctl.contracts.json_values import is_json_integer, parse_exact_json
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import validate_event_structure


def _json_identity(value: Any) -> int:
    if not is_json_integer(value) or not 1 <= value <= MAX_OBJECT_ID:
        raise ValueError("事件正文身份必须是精确正整数")
    return int(value)


def _image(value: Any) -> RowImage:
    if not isinstance(value, dict) or set(value) - {"exists", "values"}:
        raise ValueError("事件行镜像必须只包含存在性和业务列值")
    exists = value["exists"]
    if type(exists) is not bool:
        raise ValueError("事件行存在性必须是布尔值")
    if exists:
        columns = value["values"]
    else:
        columns = value.get("values", {})
    if not isinstance(columns, dict) or (not exists and columns):
        raise ValueError("事件行值与存在性不一致")
    return RowImage(exists, columns)


def decode_event_row(row: Sequence[Any]) -> EventEnvelope:
    """解码 SQL 行并校验可解释结构；坏事实明确归为状态库错误。"""
    try:
        if len(row) != 8:
            raise ValueError("历史事件列结构不完整")
        body = parse_exact_json(row[7])
        if not isinstance(body, dict) or set(body) != {"reason", "evidence", "rows"}:
            raise ValueError("历史事件正文必须包含 reason、evidence 和 rows")
        if not isinstance(body["evidence"], dict) or not isinstance(body["rows"], list):
            raise ValueError("事件 evidence 或 rows 类型非法")
        changes = []
        for item in body["rows"]:
            if not isinstance(item, dict) or set(item) != {"table", "id", "before", "after"}:
                raise ValueError("事件业务行结构不完整")
            if not isinstance(item["table"], str):
                raise ValueError("事件表名必须是字符串")
            changes.append(RowChange(item["table"], _json_identity(item["id"]),
                                     _image(item["before"]), _image(item["after"])))
        event = EventEnvelope(
            event_id=row[0], transaction_id=row[1], event_type=row[2], event_version=row[3],
            occurred_at=row[4], clock_status=row[5], change_seq=row[6],
            reason=_json_identity(body["reason"]), evidence=body["evidence"], rows=tuple(changes),
        )
        validate_event_structure(event)
        return event
    except (ValueError, KeyError, TypeError) as error:
        identity = row[0] if row else "未知"
        raise ConsistencyError(f"历史事件 {identity} 无法解释: {error}") from error
