"""D2 可选能力、绑定与证据类型的单元测试。

能力缺失是显式声明而非缺省；绑定以已保存事实为准；观察按契约
登记校验，未知版本、操作不匹配与错误身份分别拒绝。
"""

from __future__ import annotations

import pytest

from camctl.devices.bindings import (
    BindingResult,
    BindingStatus,
    DeviceBinding,
    check_binding,
)
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceError,
    EvidenceRegistry,
    validate_observation,
)
from camctl.devices.ports import DriverDeclaration


def _declaration(**overrides: bool) -> DriverDeclaration:
    values = {
        "control_supported": True,
        "stop_supported": True,
        "query_supported": True,
        "result_supported": True,
        "read_supported": True,
        "digest_supported": True,
        "delete_supported": True,
    }
    values.update(overrides)
    return DriverDeclaration(**values)


class TestDeclaration:
    def test_absent_query_is_declared_capability(self) -> None:
        declaration = _declaration(query_supported=False)
        assert declaration.query_supported is False
        # 不支持状态查询不成为失败：其余能力仍有效。
        assert declaration.control_supported is True
        assert declaration.result_supported is True


class TestBindings:
    def _config(self, devices: dict[str, dict[str, str]]) -> object:
        class Snapshot:
            pass

        snapshot = Snapshot()
        snapshot.devices = devices
        return snapshot

    def test_matched_binding_uses_saved_facts(self) -> None:
        saved = DeviceBinding(device_id="cam0", driver_id="demo_camera")
        result = check_binding(
            saved, self._config({"cam0": {"driver": "demo_camera"}})
        )
        assert result.status is BindingStatus.MATCHED
        assert result.binding == saved
        assert result.current_driver_id == "demo_camera"

    def test_device_missing_preserves_original_binding(self) -> None:
        saved = DeviceBinding(device_id="cam0", driver_id="demo_camera")
        result = check_binding(saved, self._config({}))
        assert result.status is BindingStatus.DEVICE_MISSING
        assert result.binding == saved
        assert result.current_driver_id is None

    def test_driver_mismatch_reports_both_drivers(self) -> None:
        saved = DeviceBinding(device_id="cam0", driver_id="demo_camera")
        result = check_binding(saved, self._config({"cam0": {"driver": "other"}}))
        assert result.status is BindingStatus.DRIVER_MISMATCH
        assert result.binding.driver_id == "demo_camera"
        assert result.current_driver_id == "other"

    def test_unreadable_device_declaration_is_not_missing(self) -> None:
        saved = DeviceBinding(device_id="cam0", driver_id="demo_camera")
        result = check_binding(saved, self._config({"cam0": {}}))
        assert result.status is BindingStatus.UNAVAILABLE

    def test_terminal_source_keeps_binding(self) -> None:
        """取回、删除及独立收场从原文件和动作读原绑定，不读当前默认。"""
        saved = DeviceBinding(device_id="cam0", driver_id="demo_camera")
        changed = check_binding(
            saved, self._config({"cam0": {"driver": "rewired"}})
        )
        removed = check_binding(saved, self._config({}))
        for result in (changed, removed):
            assert isinstance(result, BindingResult)
            # 原绑定事实保持可读，供解释历史证据与旧报告。
            assert result.binding is saved
            assert result.binding.driver_id == "demo_camera"


CONTRACT = EvidenceContract(
    type="stop_confirmed",
    version=1,
    operation="stop",
    fields=frozenset({"activity_id"}),
    identity_field="activity_id",
    max_observations=2,
)


class TestEvidence:
    def test_valid_observation_passes(self) -> None:
        observation = DeviceObservation(
            type="stop_confirmed", version=1, data={"activity_id": "7"}
        )
        validate_observation(observation, CONTRACT, expected_identity="7")

    def test_unknown_type_and_version_rejected(self) -> None:
        wrong_type = DeviceObservation(
            type="other_evidence", version=1, data={"activity_id": "7"}
        )
        with pytest.raises(EvidenceError):
            validate_observation(wrong_type, CONTRACT)
        unknown_version = DeviceObservation(
            type="stop_confirmed", version=2, data={"activity_id": "7"}
        )
        with pytest.raises(EvidenceError):
            validate_observation(unknown_version, CONTRACT)

    def test_operation_mismatch_rejected(self) -> None:
        query_contract = EvidenceContract(
            type="state_snapshot",
            version=1,
            operation="query",
            fields=frozenset({"running"}),
        )
        observation = DeviceObservation(
            type="state_snapshot", version=1, data={"running": True}
        )
        stop_contract = EvidenceContract(
            type="stop_confirmed",
            version=1,
            operation="stop",
            fields=frozenset({"activity_id"}),
            identity_field="activity_id",
            max_observations=1,
        )
        with pytest.raises(EvidenceError):
            validate_observation(observation, stop_contract)
        assert query_contract.operation == "query"

    def test_wrong_identity_rejected(self) -> None:
        observation = DeviceObservation(
            type="stop_confirmed", version=1, data={"activity_id": "9"}
        )
        with pytest.raises(EvidenceError):
            validate_observation(observation, CONTRACT, expected_identity="7")

    def test_undefined_members_and_missing_identity_rejected(self) -> None:
        extra = DeviceObservation(
            type="stop_confirmed",
            version=1,
            data={"activity_id": "7", "raw_text": "stopped"},
        )
        with pytest.raises(EvidenceError):
            validate_observation(extra, CONTRACT)
        missing = DeviceObservation(
            type="stop_confirmed", version=1, data={}
        )
        with pytest.raises(EvidenceError):
            validate_observation(missing, CONTRACT)

    def test_non_canonical_identity_rejected(self) -> None:
        observation = DeviceObservation(
            type="stop_confirmed", version=1, data={"activity_id": "07"}
        )
        with pytest.raises(EvidenceError):
            validate_observation(observation, CONTRACT)

    def test_registry_is_single_source_for_production_and_doubles(self) -> None:
        registry = EvidenceRegistry([CONTRACT])
        assert registry.contract("stop_confirmed", 1) is CONTRACT
        with pytest.raises(EvidenceError):
            registry.contract("stop_confirmed", 3)
        with pytest.raises(EvidenceError):
            EvidenceRegistry([CONTRACT, CONTRACT])

    def test_observation_count_limited_by_contract(self) -> None:
        assert CONTRACT.max_observations == 2
        with pytest.raises(EvidenceError):
            EvidenceContract(
                type="bulk",
                version=1,
                operation="result",
                fields=frozenset(),
                max_observations=0,
            )
