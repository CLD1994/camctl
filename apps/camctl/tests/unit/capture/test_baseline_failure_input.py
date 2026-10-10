"""基准失败的完整保存申请必须携带实际读取错误。"""
import pytest

from camctl.capture.media import RecordingFailure
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.repositories.capture import FinishCapture


def test_baseline_failure_requires_actual_read_error():
    with pytest.raises(ValueError):
        FinishCapture(1, (), OutputCatalogFacts(1, True), 1,
                      RecordingFailure("capture_failed", {"activity_id": "1", "reason": "baseline_read_failed"}))
