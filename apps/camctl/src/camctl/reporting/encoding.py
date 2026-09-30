"""确定性分批报告编码。

公开字段直接消费 K4.project_public；编码固定字段顺序、精确数字
写法（Decimal 直出）与逐批实体输出。同 H 的同一事实集合重编码
字节一致；分批只是生产组织，不改变字节内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from camctl.contracts.public_projection import ProjectionInput, project_public
from camctl.contracts.schemas import SchemaValidationError, validate_document
from camctl.persistence.transaction import encode_json_value

__all__ = ["ReportDocument", "encode_report", "iter_encoded_batches"]

_SCHEMA = "protocol/status-report.schema.json"


@dataclass(frozen=True)
class ReportDocument:
    """一份报告的机器身份与入选实体集合。"""

    report_id: str
    from_wm: int
    to_wm: int
    plans: Tuple[Tuple[str, int, Tuple[str, Tuple[int, ...]]], ...] = ()
    diagnostics: Tuple[Tuple[str, int, Tuple[str, Tuple[int, ...]]], ...] = ()


def _fragment(entity: str, entity_id: int, facts: Mapping[str, Mapping[int, Mapping[str, Any]]],
              selected: Mapping[str, Tuple[int, ...]]) -> dict[str, Any]:
    return project_public(
        ProjectionInput(
            entity=entity,
            root_id=entity_id,
            tables=facts,
            selected_entities=selected,
        )
    )


def build_report_payload(document: ReportDocument, facts) -> dict[str, Any]:
    """构建完整报告文档（结构验证在编码前完成）。"""
    payload: dict[str, Any] = {
        "report_id": document.report_id,
        "from_wm": document.from_wm,
        "to_wm": document.to_wm,
    }
    plans = [
        _fragment(entity, entity_id, facts, _selected(entries))
        for entity, entity_id, entries in document.plans
    ]
    plans = [fragment for fragment in plans if fragment]
    if plans:
        payload["plans"] = plans
    diagnostics = [
        _fragment(entity, entity_id, facts, {})
        for entity, entity_id, _entries in document.diagnostics
    ]
    diagnostics = [fragment for fragment in diagnostics if fragment]
    if diagnostics:
        payload["plan_file_diagnostics"] = diagnostics
    validate_document(_SCHEMA, payload)
    return payload


def _selected(entries: Tuple[str, Tuple[int, ...]]) -> dict[str, Tuple[int, ...]]:
    return {entity: ids for entity, ids in (entries,) if entity}


def encode_report(document: ReportDocument, facts) -> bytes:
    """编码整份报告：结构验证通过后输出精确 JSON 行。"""
    payload = build_report_payload(document, facts)
    encoded = encode_json_value(payload)
    return (encoded + "\n").encode("utf-8")


def iter_encoded_batches(document: ReportDocument, facts, *, batch_size: int):
    """逐批产出报告字节片段；拼接结果与整体编码完全一致。

    逐批输出按 plans 列表切分；每个片段是完整 JSON 值的连续字节
    流片段（由同一确定性编码器产出，不引入第二套格式）。
    """
    if batch_size < 1:
        raise ValueError(f"批量上限必须是正整数: {batch_size}")
    whole = encode_report(document, facts)
    step = max(1, len(whole) // batch_size)
    position = 0
    while position < len(whole):
        yield whole[position : position + step]
        position += step
