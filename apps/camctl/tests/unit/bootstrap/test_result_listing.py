"""结果列举驱动适配与生产等待配置的单元测试。

DriverResultListing 把驱动的 list_results 调用结果转成业务侧的候选
产物文件：观察按登记契约校验，条目结构逐字段解释，调用错误与观察
并存时可靠事实优先。execution_wait_config 从已保存的动作执行定义
取得延时等待配置。两类纯适配不访问数据库。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from camctl.bootstrap.capture_assembly import (
    DriverResultListing,
    execution_wait_config,
)
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import ControlRequest, DeviceCallResult

_BINDING = DeviceBinding(device_id="cam-1", driver_id="listing-double")

#: 结果列举观察的契约形态（与 D5 契约测试的 result_files_listed 一致）。
_EVIDENCE = EvidenceRegistry((
    EvidenceContract(
        type="result_files_listed", version=1, operation="result",
        fields=frozenset({"activity_id", "entries"}),
        identity_field="activity_id"),
    EvidenceContract(
        type="results_returned", version=1, operation="result",
        fields=frozenset()),
))


class _ListingDriver:
    """受 result 端口约束的替身：返回编排的调用结果。"""

    def __init__(self, result: DeviceCallResult) -> None:
        self._result = result
        self.requests: list[ControlRequest] = []

    async def list_results(
        self, request: ControlRequest, batch: int,
    ) -> DeviceCallResult:
        self.requests.append(request)
        self.batch = batch
        return self._result


def _listed(
    entries: list[dict[str, Any]], activity: str = "7",
) -> DeviceObservation:
    return DeviceObservation(
        type="result_files_listed", version=1,
        data={"activity_id": activity, "entries": entries})


def _entry(identity: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "identity": identity,
        "locator": {"path": f"/DCIM/{identity}"},
        "size_bytes": 4096,
        "complete": True,
        "kind": "video",
        "original_name": f"{identity}.mp4",
        "media_type": "video/mp4",
    }
    entry.update(overrides)
    return entry


def _adapter(driver: _ListingDriver) -> DriverResultListing:
    return DriverResultListing(driver, _BINDING, _EVIDENCE)


class TestDriverResultListing:
    def test_entries_are_converted_to_observed_files(self) -> None:
        driver = _ListingDriver(DeviceCallResult(
            observations=(_listed([
                _entry("11"),
                _entry("12", kind="photo", size_bytes=None, complete=False,
                       original_name=None, media_type=None),
                _entry("13", kind="unknown-kind"),
            ]),), error=None))
        files = asyncio.run(_adapter(driver).list_files(7))
        assert driver.requests == [ControlRequest(
            operation="result", binding=_BINDING,
            params={"activity_id": "7"})]
        assert driver.batch == 100
        assert [f.identity for f in files] == ["11", "12", "13"]
        assert files[0].kind.value == "video"
        assert files[0].complete is True and files[0].size_bytes == 4096
        assert files[0].locator == {"path": "/DCIM/11"}
        assert files[0].original_name == "11.mp4"
        assert files[0].media_type == "video/mp4"
        assert files[0].paired_identity is None
        # 未完成条目允许无大小；未知类别不冒充已知类别。
        assert files[1].complete is False and files[1].size_bytes is None
        assert files[2].kind.value == "other"
        # 条目自身结构作为归属与完成的结构化依据。
        assert files[0].evidence["identity"] == "11"

    def test_preview_entry_carries_driver_pairing(self) -> None:
        driver = _ListingDriver(DeviceCallResult(
            observations=(_listed([
                _entry("11"),
                _entry("11-preview", kind="photo", paired_identity="11"),
            ]),), error=None))
        files = asyncio.run(_adapter(driver).list_files(7))
        assert files[1].paired_identity == "11"
        assert files[0].paired_identity is None

    def test_malformed_pairing_field_is_rejected(self) -> None:
        for value in ("", 3, {}):
            with pytest.raises(ValueError, match="配对身份"):
                asyncio.run(_adapter(_ListingDriver(DeviceCallResult(
                    observations=(_listed([
                        _entry("11-preview", kind="photo",
                               paired_identity=value)]),),
                    error=None))).list_files(7))

    def test_call_error_without_observation_raises(self) -> None:
        driver = _ListingDriver(DeviceCallResult(
            observations=(), error={"code": "transport_timeout"}))
        with pytest.raises(RuntimeError, match="transport_timeout"):
            asyncio.run(_adapter(driver).list_files(7))

    def test_observation_survives_late_call_error(self) -> None:
        driver = _ListingDriver(DeviceCallResult(
            observations=(_listed([_entry("11")]),),
            error={"code": "connection_reset"}))
        files = asyncio.run(_adapter(driver).list_files(7))
        assert [f.identity for f in files] == ["11"]

    def test_identity_mismatch_is_rejected(self) -> None:
        driver = _ListingDriver(DeviceCallResult(
            observations=(_listed([_entry("11")], activity="8"),),
            error=None))
        with pytest.raises(ValueError, match="身份"):
            asyncio.run(_adapter(driver).list_files(7))

    def test_foreign_observation_type_is_not_consumed(self) -> None:
        driver = _ListingDriver(DeviceCallResult(
            observations=(DeviceObservation(
                type="results_returned", version=1, data={}),),
            error=None))
        with pytest.raises(RuntimeError, match="列举观察"):
            asyncio.run(_adapter(driver).list_files(7))

    def test_missing_contract_is_assembly_error(self) -> None:
        empty = EvidenceRegistry(())
        adapter = DriverResultListing(
            _ListingDriver(DeviceCallResult(
                observations=(_listed([_entry("11")]),), error=None)),
            _BINDING, empty)
        with pytest.raises(Exception, match="契约"):
            asyncio.run(adapter.list_files(7))

    def test_malformed_entry_is_rejected(self) -> None:
        cases = [
            {"locator": {}, "size_bytes": 1, "complete": True},
            {**_entry("11"), "size_bytes": "4096"},
            {**_entry("11"), "complete": "yes"},
            {**_entry("11"), "locator": "/DCIM/11"},
        ]
        for entry in cases:
            with pytest.raises(ValueError, match="条目"):
                asyncio.run(_adapter(_ListingDriver(DeviceCallResult(
                    observations=(_listed([entry]),), error=None)
                )).list_files(7))


class TestExecutionWaitConfig:
    def test_timelapse_spec_provides_target_and_margin(self) -> None:
        for raw in (
            {"execution_spec_json": '{"duration_based": true, "wait_after_send": true,'
             ' "end_control": 1, "stop_supported": false, "start_return_meaning": 1,'
             ' "completion_mode": 2, "target_duration_ms": 60000,'
             ' "result_wait_margin_ms": 2000}', "effective_params_json": {}},
            {"execution_spec_json": {
                "target_duration_ms": 60000, "result_wait_margin_ms": 2000}},
        ):
            config = execution_wait_config(raw)
            assert config.target_duration_ms == 60000
            assert config.driver_margin_ms == 2000
            assert config.extra_wait_ms == 0

    def test_margin_defaults_to_zero_when_absent(self) -> None:
        config = execution_wait_config(
            {"execution_spec_json": {"target_duration_ms": 30000}})
        assert config.driver_margin_ms == 0

    def test_spec_without_target_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_duration_ms"):
            execution_wait_config({"execution_spec_json": {}})
