"""固定分段文件的正式产物候选资格。"""

from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, PreviewPairingState, SegmentMember, SegmentMemberRole,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.segment_profiles import SegmentFileKind


_FAMILY_ROLE = enum_for('result_page_files.family_role')


def eligible_original(member: SegmentMember) -> bool:
    """原片必须可靠归属、完成且实际长度非零。"""
    return (member.role is SegmentMemberRole.ORIGINAL
            and member.ownership is OwnershipState.OWNED
            and member.completion is FileCompletionState.COMPLETE
            and member.size_bytes is not None and member.size_bytes > 0)


def eligible_photo_original(member: SegmentMember, file_kind: SegmentFileKind) -> bool:
    """固定照片成员按自身事实决定是否成为原片候选。"""
    return (member.role is SegmentMemberRole.OTHER and file_kind is SegmentFileKind.PHOTO
            and member.ownership is OwnershipState.OWNED
            and member.completion is FileCompletionState.COMPLETE
            and member.size_bytes is not None and member.size_bytes > 0)


def is_paired_preview(member: SegmentMember) -> bool:
    """只有确认配对的预览需要进一步读取和核对原片端点。"""
    return (member.role is SegmentMemberRole.PREVIEW
            and member.pairing_state is PreviewPairingState.PAIRED)


def eligible_paired_preview(*, member, original, preview_file, original_file_id,
                            pairing_fact_id, pairing_fact, preview_family, original_family):
    """复核固定关联及原名族后，按两端实际成员事实决定预览资格。"""
    if pairing_fact_id is not None:
        same_family = (preview_family[0] is not None and preview_family[:2] == original_family[:2]
                       and preview_family[2] == int(_FAMILY_ROLE.PREVIEW)
                       and original_family[2] == int(_FAMILY_ROLE.ORIGINAL))
        same_family = (same_family and pairing_fact is not None
                       and pairing_fact['original_file_id'] == original_file_id)
    else:
        same_family = (preview_family.key is not None and original_family.key is not None
                       and preview_family.pairing_identity == original_family.pairing_identity)
    if preview_file['original_device_file_id'] != original_file_id:
        raise ConsistencyError('预览固定文件关联与同集合实际原片不同')
    if original.role is not SegmentMemberRole.ORIGINAL or not same_family:
        raise ConsistencyError('预览没有同集合、同完整名称主干的真实原片')
    return (member.ownership is OwnershipState.OWNED
            and member.completion is FileCompletionState.COMPLETE
            and member.size_bytes is not None and member.size_bytes > 0
            and eligible_original(original))
