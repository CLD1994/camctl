"""精确 JSON 文本编码，不经过 float 或上下文舍入。"""
import json
from typing import Any


def encode_exact_json(value: Any, *, error_type: type[Exception] = ValueError) -> str:
    """按精确数值编码 JSON 文本；不经过 float，不使用上下文舍入。"""

    def dump(node: Any) -> str:
        if node is None:
            return "null"
        if node is True:
            return "true"
        if node is False:
            return "false"
        if isinstance(node, int):
            return str(node)
        if isinstance(node, str):
            return json.dumps(node, ensure_ascii=False)
        from decimal import Decimal

        if isinstance(node, Decimal):
            if not node.is_finite():
                raise error_type("事件正文不能包含非有限数值")
            return str(node)
        if isinstance(node, list):
            return "[" + ",".join(dump(item) for item in node) + "]"
        if isinstance(node, dict):
            parts = []
            for key, item in node.items():
                if not isinstance(key, str):
                    raise error_type("事件正文对象键必须是字符串")
                parts.append(json.dumps(key, ensure_ascii=False) + ":" + dump(item))
            return "{" + ",".join(parts) + "}"
        raise error_type(f"事件正文包含无法精确编码的值: {type(node).__name__}")

    return dump(value)
