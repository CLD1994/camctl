"""设备文件观察登记输入的纯校验测试。

身份键编码、归属与配对证据结构、文件形成状态分区的输入规则；
不访问数据库或文件系统。
"""

import pytest

from camctl.capture.files import (
    FileCompletionSave,
    FileObservationSave,
    OwnershipSave,
    file_identity_key,
)


class TestFileIdentityKey:
    def test_deterministic_encoding_of_binding_and_identity(self):
        first = file_identity_key("cam-1", "camctl-adb", "/DCIM/100_0001.MP4")
        second = file_identity_key("cam-1", "camctl-adb", "/DCIM/100_0001.MP4")
        assert first == second
        assert "/DCIM/100_0001.MP4" in first

    def test_different_bindings_or_identities_never_collide(self):
        base = file_identity_key("cam-1", "camctl-adb", "file-a")
        assert base != file_identity_key("cam-2", "camctl-adb", "file-a")
        assert base != file_identity_key("cam-1", "other-driver", "file-a")
        assert base != file_identity_key("cam-1", "camctl-adb", "file-b")

    @pytest.mark.parametrize("device,driver,identity", [
        ("", "camctl-adb", "file-a"), ("cam-1", "", "file-a"), ("cam-1", "camctl-adb", ""),
    ])
    def test_rejects_empty_components(self, device, driver, identity):
        with pytest.raises(ValueError):
            file_identity_key(device, driver, identity)


class TestFileObservationSave:
    def test_minimal_observation_is_valid(self):
        save = FileObservationSave(
            observer_action_id=11, file_identity="file-a",
            locator={"path": "/DCIM/1.MP4"}, occurred_at=1_750_000_000_000_000,
        )
        assert save.original_name is None and save.media_type is None

    def test_readable_metadata_is_optional_text(self):
        save = FileObservationSave(
            observer_action_id=11, file_identity="file-a", locator={"path": "p"},
            occurred_at=1, original_name="1.MP4", media_type="video/mp4",
        )
        assert save.original_name == "1.MP4"

    @pytest.mark.parametrize("kwargs", [
        {"file_identity": ""}, {"file_identity": 7},
        {"locator": {}}, {"locator": None}, {"locator": "path"},
        {"original_name": ""}, {"media_type": ""}, {"occurred_at": "now"},
    ])
    def test_rejects_invalid_inputs(self, kwargs):
        defaults = dict(observer_action_id=11, file_identity="file-a",
                        locator={"path": "p"}, occurred_at=1)
        defaults.update(kwargs)
        with pytest.raises(ValueError):
            FileObservationSave(**defaults)

    def test_rejects_invalid_observer_identity(self):
        with pytest.raises(ValueError):
            FileObservationSave(observer_action_id=0, file_identity="a",
                                locator={"p": 1}, occurred_at=1)


class TestOwnershipSave:
    def _save(self, **kwargs):
        defaults = dict(file_id=501, source_action_id=11, method=1, role=2,
                        observation={"task": "a"}, occurred_at=1)
        defaults.update(kwargs)
        return OwnershipSave(**defaults)

    def test_task_scope_evidence_document(self):
        save = self._save()
        assert save.ownership_evidence() == {"method": 1, "observation": {"task": "a"}}
        assert save.pairing_evidence() is None

    def test_baseline_difference_requires_activity(self):
        save = self._save(method=2, activity_id=11)
        assert save.ownership_evidence()["activity_id"] == 11
        with pytest.raises(ValueError):
            self._save(method=2)
        with pytest.raises(ValueError):
            self._save(method=1, activity_id=11)

    def test_preview_requires_pairing_original_forbids_it(self):
        save = self._save(role=3, paired_device_file_id=501, pairing_observation={"pair": 1})
        assert save.pairing_evidence() == {"method": 1, "observation": {"pair": 1}}
        with pytest.raises(ValueError):
            self._save(role=3)
        with pytest.raises(ValueError):
            self._save(role=2, paired_device_file_id=501)

    @pytest.mark.parametrize("kwargs", [
        {"method": 4}, {"method": True}, {"role": 1}, {"role": 4},
        {"observation": None}, {"source_action_id": 0},
    ])
    def test_rejects_invalid_inputs(self, kwargs):
        with pytest.raises(ValueError):
            self._save(**kwargs)


class TestFileCompletionSave:
    def _save(self, **kwargs):
        defaults = dict(file_id=501, state=3, occurred_at=1)
        defaults.update(kwargs)
        return FileCompletionSave(**defaults)

    def test_device_guarantee_completion(self):
        save = self._save(basis=1, observation={"stopped": True}, size_bytes=4096)
        assert save.completion_evidence() == {
            "basis": 1, "observation": {"stopped": True}}

    def test_time_and_outputs_requires_saved_wait_reference(self):
        save = self._save(basis=2, observation={"waited": True}, size_bytes=10,
                          activity_id=11, wait_completed_event_id=7)
        document = save.completion_evidence()
        assert document["activity_id"] == 11
        assert document["wait_completed_event_id"] == 7
        with pytest.raises(ValueError):
            self._save(basis=2, observation={"w": 1}, size_bytes=10)
        with pytest.raises(ValueError):
            self._save(basis=1, observation={"w": 1}, size_bytes=10, activity_id=11)

    def test_writing_optionally_carries_guarantee_evidence(self):
        save = self._save(state=2, basis=1, observation={"growing": True})
        assert save.completion_evidence()["basis"] == 1
        assert self._save(state=2).completion_evidence() is None
        with pytest.raises(ValueError):
            self._save(state=2, basis=2, observation={"w": 1})
        with pytest.raises(ValueError):
            self._save(state=2, basis=1, observation={"g": 1}, size_bytes=4)

    def test_unconfirmed_requires_failure_evidence(self):
        save = self._save(state=4, error={"reason": "verify_exhausted"})
        assert save.error is not None
        with pytest.raises(ValueError):
            self._save(state=4)
        with pytest.raises(ValueError):
            self._save(state=4, error={"r": 1}, basis=1, observation={"o": 1})

    @pytest.mark.parametrize("kwargs", [
        {"state": 1}, {"state": 5}, {"state": True},
        {"state": 3},  # 缺完成依据与大小
        {"state": 3, "basis": 1, "observation": {"o": 1}, "size_bytes": -1},
        {"state": 3, "basis": 1, "observation": {"o": 1}, "size_bytes": True},
    ])
    def test_rejects_invalid_states_and_values(self, kwargs):
        with pytest.raises(ValueError):
            self._save(**kwargs)
