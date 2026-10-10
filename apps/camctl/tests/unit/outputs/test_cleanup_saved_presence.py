"""持久化存在性观察必须完整指向原查询成员。"""

from types import SimpleNamespace

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation
from camctl.outputs.cleanup_results import saved_query_presence


def _actual(data, version=1, *, duplicate=False):
    observation = DeviceObservation("file_presence", version, data)
    return SimpleNamespace(observations=(observation,) * (2 if duplicate else 1))


@pytest.mark.parametrize("present", [True, False])
def test_saved_presence_keeps_actual_boolean(present):
    assert saved_query_presence(_actual({"cleanup_item_id": "7", "present": present}), 7) is present


def test_saved_missing_observation_is_unknown():
    assert saved_query_presence(SimpleNamespace(observations=()), 7) is None


@pytest.mark.parametrize("data", [
    {"present": True},
    {"cleanup_item_id": None, "present": True},
    {"cleanup_item_id": "8", "present": True},
    {"cleanup_item_id": 7, "present": True},
    {"cleanup_item_id": "7"},
    {"cleanup_item_id": "7", "present": None},
    {"cleanup_item_id": "7", "present": 1},
    {"cleanup_item_id": "7", "present": True, "extra": 1},
])
def test_saved_presence_rejects_incomplete_or_foreign_data(data):
    with pytest.raises(ConsistencyError):
        saved_query_presence(_actual(data), 7)


@pytest.mark.parametrize("version", [True, 2])
def test_saved_presence_requires_supported_integer_version(version):
    with pytest.raises(ConsistencyError):
        saved_query_presence(_actual({"cleanup_item_id": "7", "present": True}, version), 7)


def test_saved_presence_rejects_duplicate_observations():
    with pytest.raises(ConsistencyError):
        saved_query_presence(_actual({"cleanup_item_id": "7", "present": True}, duplicate=True), 7)
