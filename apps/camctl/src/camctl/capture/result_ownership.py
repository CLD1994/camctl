"""对原完整扫描中新增文件的归属责任作逐项判断。"""

from camctl.capture.results import is_optional_preview
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.segment_profiles import SegmentFileKind, profile_for_policy
from camctl.devices.tasks import CaptureResultPolicy


_ROLE = enum_for('device_files.role')
PAIRING_FIELDS = frozenset({'original_device_file_id', 'pairing_evidence_json'})


def is_unassigned_result_file(file):
    """文件来源、用途和配对均未固定时才允许首次归属。"""
    return (file['source_action_id'] is None and file['ownership_evidence_json'] is None
            and file['role'] == int(_ROLE.UNDETERMINED)
            and all(file[name] is None for name in PAIRING_FIELDS))


def result_ownership_already_saved(file, source, role):
    """首次归属返回 False，同一来源已保存返回 True，固定冲突则拒绝。"""
    if is_unassigned_result_file(file):
        return False
    if (file['source_action_id'] == source and file['role'] == role
            and file['ownership_evidence_json'] is not None):
        return True
    raise ConsistencyError('文件来源或用途已经固定，不能改指')


def validate_result_ownership_policy(*, scope_only, policy):
    """结果核实要求使用原动作冻结的结果策略。"""
    if not scope_only and policy not in {item.value for item in CaptureResultPolicy}:
        raise ConsistencyError('原动作没有声明 OSMO 结果策略')


def result_observation_role(*, scope_only, policy, observed_file, action6_format_id):
    """按原页观察与冻结策略判断文件用途；范围页允许用途尚未确定。"""
    suffix = observed_file.identity.rpartition('.')[2].lower()
    if scope_only:
        if policy in {item.value for item in CaptureResultPolicy}:
            kind = profile_for_policy(policy).classify_file(observed_file.identity).kind
            if kind is SegmentFileKind.PREVIEW:
                return int(_ROLE.PREVIEW)
            if kind in (SegmentFileKind.VIDEO, SegmentFileKind.PHOTO):
                return int(_ROLE.ORIGINAL)
        elif observed_file.paired_identity is not None:
            return int(_ROLE.PREVIEW)
        elif observed_file.format_id is not None:
            return int(_ROLE.ORIGINAL)
        return int(_ROLE.UNDETERMINED)
    if policy == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value:
        kind = profile_for_policy(policy).classify_file(observed_file.identity).kind
        expected = ('mp4' if kind is SegmentFileKind.VIDEO else
                    'lrf' if kind is SegmentFileKind.PREVIEW else action6_format_id)
        if (expected is None or not isinstance(observed_file.format_id, str)
                or observed_file.format_id.casefold() != expected):
            raise ConsistencyError('Action6 原观察格式与冻结文件类别不一致')
        return int(_ROLE.PREVIEW if kind is SegmentFileKind.PREVIEW else _ROLE.ORIGINAL)
    if (suffix not in {'osv', 'lrf'} or not isinstance(observed_file.format_id, str)
            or observed_file.format_id.lower() != suffix):
        raise ConsistencyError('原观察格式与 OSV/LRF 文件后缀不一致')
    return int(_ROLE.ORIGINAL if suffix == 'osv' else _ROLE.PREVIEW)


def resolved_result_role(*, scope_only, role, action_id, saved_file):
    """范围页未确定用途时，沿用已经可靠保存的同动作文件用途。"""
    if (scope_only and role == int(_ROLE.UNDETERMINED)
            and saved_file.get('source_action_id') == action_id
            and saved_file.get('ownership_evidence_json') is not None):
        return saved_file['role']
    return role


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
