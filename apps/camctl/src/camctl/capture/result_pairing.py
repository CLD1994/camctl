"""根据固定扫描成员事实决定预览与原片的配对结论。"""

from dataclasses import dataclass

from camctl.capture.segment_models import FileCompletionState, OwnershipState
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError


_REASON = enum_for("device_file_pairing_facts.reason")


@dataclass
class ScanFamilyAssessment:
    """逐项消费固定事实，保留族结论所需的有限状态。"""

    _reliable: bool = True
    _valid: bool = True

    def observe(self, *, ownership, completion, observed_complete, observed_size,
                observed_locator, saved_file):
        """累计一个族成员；可靠观察与固定文件事实矛盾时拒绝。"""
        if (ownership is not OwnershipState.OWNED
                or completion is not FileCompletionState.COMPLETE
                or not observed_complete or observed_size is None):
            self._reliable = False
        elif (observed_size != saved_file['size_bytes']
              or not json_equal(saved_file['locator_json'], observed_locator)):
            raise ConsistencyError('当前族原观察与固定文件大小或定位矛盾')
        elif observed_size == 0:
            self._valid = False

    def conclusion(self, *, original_count, preview_count, original_id, preview_file):
        """未知成员优先保持未知；其他结果按真实数量、大小和已有关系分类。"""
        if not self._reliable:
            return None
        reason = (_REASON.MISSING if original_count == 0 else
                  _REASON.AMBIGUOUS if original_count > 1 or preview_count > 1 else
                  _REASON.INVALID if not self._valid else _REASON.UNIQUE)
        if reason == _REASON.UNIQUE and preview_file['original_device_file_id'] not in (None, original_id):
            return _REASON.CONFLICT
        return reason
