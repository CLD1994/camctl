"""恢复观察必须拒绝重新建档、从零读取或已经放行的数据。"""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "osmo_copy_observation",
    Path(__file__).resolve().parents[1] / "integration/_osmo_copy_observation.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
assert_copy_resumed_before_data = _MODULE.assert_copy_resumed_before_data


def _observations(progress=65536):
    command = "dd if='/mnt/media_rw/emulated/DCIM/CAM_001/segment 7.OSV' bs=65536"
    return {
        "interrupted_read": {
            "kind": "read_blocked", "started_ns": 100,
            "script": command + " skip=0", "forwarded_bytes": progress,
        },
        "resumed_read": {
            "kind": "read_started", "started_ns": 200,
            "script": command + f" skip={progress // 65536}",
        },
        "resumed_gate": {
            "kind": "read_blocked", "started_ns": 200, "forwarded_bytes": 0,
        },
        "saved_copy": (61, 71, progress),
        "resumed_copies": [(61, 71, progress)],
        "saved_deliveries": [(71, 81), (72, 82)],
        "resumed_deliveries": [(71, 81), (72, 82)],
    }


@pytest.mark.parametrize("progress", [65536, 131072])
def test_saved_copy_resumes_from_its_exact_progress_before_data(progress):
    assert_copy_resumed_before_data(**_observations(progress))


@pytest.mark.parametrize(
    "change",
    [
        {"resumed_read": {"script": "dd if='/mnt/media_rw/emulated/DCIM/CAM_001/segment 7.OSV' bs=65536 skip=0"}},
        {"resumed_read": {"script": "dd if='/mnt/media_rw/emulated/DCIM/CAM_001/segment 7.OSV' bs=65536 skip=10"}},
        {"resumed_read": {"script": "dd if=/mnt/media_rw/emulated/DCIM/CAM_001/other.OSV bs=65536 skip=1"}},
        {"resumed_read": {"started_ns": 100}},
        {"resumed_gate": {"started_ns": 100}},
        {"resumed_gate": {"forwarded_bytes": 65536}},
        {"resumed_copies": [(62, 71, 65536)]},
        {"resumed_copies": [(61, 71, 0)]},
        {"resumed_copies": [(61, 71, 65536), (62, 71, 0)]},
        {"resumed_copies": [(61, 73, 65536)]},
        {"resumed_deliveries": [(73, 81), (72, 82)]},
        {"resumed_deliveries": [(71, 82), (72, 81)]},
    ],
    ids=[
        "restart-from-zero", "different-offset", "different-source", "old-read",
        "old-gate", "data-already-forwarded", "new-copy", "progress-reset",
        "extra-copy", "copy-changes-delivery", "new-delivery", "delivery-changes-output",
    ],
)
def test_invalid_resume_observation_is_rejected(change):
    observed = _observations()
    for key, value in deepcopy(change).items():
        if isinstance(value, dict):
            observed[key].update(value)
        else:
            observed[key] = value
    with pytest.raises(AssertionError):
        assert_copy_resumed_before_data(**observed)
