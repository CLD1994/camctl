"""对原完整扫描中新增文件的归属责任作逐项判断。"""

from camctl.capture.results import is_optional_preview
from camctl.contracts.values import ConsistencyError


def require_result_file_ownership(*, action_id, source_action_id, ownership_evidence,
                                  responsibility_key, target_id, observed_file, product_rules):
    """必要文件须可靠归属；非范围收尾可保留原规则中的可选预览。"""
    if source_action_id == action_id and ownership_evidence is not None:
        return
    if (responsibility_key != f"scope/{target_id}" and source_action_id is None
            and is_optional_preview(observed_file.kind, observed_file.format_id,
                                    observed_file.paired_identity, product_rules)):
        return
    raise ConsistencyError('原完整扫描仍有未确认归属的新增必要文件')
