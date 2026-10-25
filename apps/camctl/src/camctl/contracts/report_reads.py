"""从实际公开节点及字段依赖登记取得报告增长请求。"""
from dataclasses import dataclass
from collections.abc import Mapping

from camctl.contracts.public_projection import _dependencies, PublicProjectionError
from camctl.contracts.segment_errors import SegmentErrorHeader, validate_segment_error_header
from camctl.contracts.values import ObjectId
from camctl.contracts.workflow_errors import registered_error


@dataclass(frozen=True)
class ReportExpansionRequest:
    phase: str
    source_action_id: int
    header: SegmentErrorHeader | None = None

    def __post_init__(self):
        ObjectId(self.source_action_id)


def _uses_schema(node, reference):
    if isinstance(node, Mapping):
        return node.get('$ref') == reference or any(
            _uses_schema(value, reference) for value in node.values())
    return isinstance(node, list) and any(_uses_schema(value, reference) for value in node)


def report_expansion_request(kind, public, *, source_action_id=None):
    """只有实际出现的公开 coverage 或已登记错误头能激活增长阶段。"""
    if public is None:
        return None
    matches = []
    error_spec = registered_error(public['code']) if kind == 'error' else None
    for phase, spec in _dependencies().get('read_expansions', {}).items():
        trigger = spec['trigger']
        if (trigger['kind'] == 'projection' and trigger['projection'] == kind
                or trigger['kind'] == 'registered_error' and error_spec is not None
                and _uses_schema(error_spec['details_schema'], trigger['details_schema'])):
            matches.append(phase)
    if len(matches) > 1:
        raise PublicProjectionError('公开节点对应多个报告增长阶段')
    if not matches:
        if kind == 'error':
            return None
        raise PublicProjectionError('公开节点没有登记报告增长阶段')
    header = (validate_segment_error_header(public['code'], public['details'])
              if error_spec is not None else None)
    return ReportExpansionRequest(matches[0],
        header.source_action_id if header is not None else source_action_id, header)
