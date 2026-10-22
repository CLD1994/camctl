"""从拍摄判定建立共享的有限分段错误头。"""

from camctl.capture.segment_models import SegmentAssessment
from camctl.contracts.segment_errors import (
    SegmentErrorHeader as SegmentErrorHeader,
    public_gap as public_gap,
    validate_segment_error_header as validate_segment_error_header,
)


def segment_error_header(assessment: SegmentAssessment, retained_original_count: int):
    scope = assessment.default_scope
    return SegmentErrorHeader(assessment.set_ref.action_id,assessment.gap_count,scope.state,
        scope.member_count,retained_original_count,assessment.first_gap)
