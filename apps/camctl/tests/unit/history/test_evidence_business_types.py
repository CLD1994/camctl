"""保存申请的值类型由业务层与仓储名称共同使用。"""

from decimal import Decimal

from camctl.capture.selection import CaptureSelectionInput
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.operations.availability_wait import WaitingFinishRequest, parse_waiting_finish_request
from camctl.persistence.repositories import availability_wait_finish, capture_selection, scheduling
from camctl.scheduling.preparation_types import FailBindingPreparationRequest


def test_storage_imports_share_the_business_type_and_original_parser():
    assert capture_selection.CaptureSelectionInput is CaptureSelectionInput
    assert availability_wait_finish.WaitingFinishRequest is WaitingFinishRequest
    assert availability_wait_finish._request is parse_waiting_finish_request
    assert scheduling.FailBindingPreparationRequest is FailBindingPreparationRequest

    selection = CaptureSelectionInput("camera", "hasselblad_cfv100c",
        CameraIdentity("serial", 10), Decimal("1.25"), 10)
    assert isinstance(capture_selection.CaptureSelectionInput.from_document(selection.document()), CaptureSelectionInput)
    waiting = parse_waiting_finish_request({"check_id": 3, "trusted_wall_now": 10,
        "occurred_at": 11, "max_attempts": 2})
    assert isinstance(waiting, availability_wait_finish.WaitingFinishRequest)
    binding = FailBindingPreparationRequest(3, 10, 11, {
        "device_id": "camera", "expected_driver_id": "hasselblad_cfv100c", "reason": "missing"})
    assert isinstance(binding, scheduling.FailBindingPreparationRequest)
