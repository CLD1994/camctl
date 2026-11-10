"""固定分段文件的正式产物候选资格。"""

from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentMember, SegmentMemberRole,
)


def eligible_original(member: SegmentMember) -> bool:
    """原片必须可靠归属、完成且实际长度非零。"""
    return (member.role is SegmentMemberRole.ORIGINAL
            and member.ownership is OwnershipState.OWNED
            and member.completion is FileCompletionState.COMPLETE
            and member.size_bytes is not None and member.size_bytes > 0)
