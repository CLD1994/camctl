"""公共业务错误登记的装载入口。

动作最终错误与逐明细错误的整数编号以 protocol 错误登记为唯一
权威来源；程序各处按名称取得编号，不另行维护编号清单。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Mapping

from camctl.bootstrap.resources import resource_bytes


@lru_cache(maxsize=1)
def _registry() -> dict:
    return json.loads(resource_bytes("protocol/workflow-codes.json"))


@lru_cache(maxsize=1)
def action_error_ids() -> Mapping[str, int]:
    """公共登记中动作最终错误的名称到整数编号映射。"""
    return {
        name: int(spec["action_error_id"])
        for name, spec in _registry()["codes"].items()
        if "action_error_id" in spec
    }


@lru_cache(maxsize=1)
def item_error_ids(table: str) -> Mapping[str, int]:
    """一张明细表的公共错误名称到整数编号映射。"""
    return {
        name: int(spec["item_error_ids"][table])
        for name, spec in _registry()["codes"].items()
        if table in spec.get("item_error_ids", {})
    }


def action_error_id(name: str) -> int:
    """按公共名称取得动作错误编号；未知名称是装配错误。"""
    try:
        return action_error_ids()[name]
    except KeyError as error:
        raise ValueError(f"公共错误登记没有该动作错误: {name!r}") from error


def item_error_id(table: str, name: str) -> int:
    """按公共名称取得明细错误编号；未知名称是装配错误。"""
    try:
        return item_error_ids(table)[name]
    except KeyError as error:
        raise ValueError(
            f"公共错误登记没有 {table} 的该项错误: {name!r}"
        ) from error
