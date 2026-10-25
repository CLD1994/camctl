"""从实际公开节点及字段依赖登记取得报告增长请求。"""
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import urldefrag, urljoin, urlsplit

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


def _uses_schema(node, identity, document):
    if isinstance(node, Mapping):
        document = urljoin(document, node.get('$id', ''))
        reference = node.get('$ref')
        return (isinstance(reference, str) and urldefrag(urljoin(document, reference)) == identity
                or any(_uses_schema(value, identity, document) for value in node.values()))
    return isinstance(node, list) and any(_uses_schema(value, identity, document) for value in node)


def report_expansion_request(kind, public, *, source_action_id=None):
    """只有实际出现的公开 coverage 或已登记错误头能激活增长阶段。"""
    if public is None:
        return None
    matches = []
    registry = _dependencies()
    error_spec = registered_error(public['code']) if kind == 'error' else None
    if error_spec is not None:
        # 包内 Schema 在统一注册表中按兄弟文件名寻址，不按仓库文件路径寻址。
        document = registry['error_registry']
        if not urlsplit(document).scheme:
            document = PurePosixPath(document).name
    for phase, spec in registry.get('read_expansions', {}).items():
        trigger = spec['trigger']
        if (trigger['kind'] == 'projection' and trigger['projection'] == kind
                or trigger['kind'] == 'registered_error' and error_spec is not None
                and _uses_schema(error_spec['details_schema'],
                    urldefrag(urljoin(document, trigger['details_schema'])), document)):
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
