"""正式内置录像契约的静态输入与能力边界。"""

from decimal import Decimal

from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.registration import builtin_camera_contracts


def _contract(model):
    return next(contract for contract in builtin_camera_contracts() if contract.driver_id == model)


def _params():
    return {"type": "action6_record", "duration_s": 10, "resolution": "4k30",
            "fov": "wide", "stabilization": "off",
            "exposure": {"mode": "auto", "compensation_ev": 0}}


def test_action6_builtin_exposes_only_ordinary_recording():
    capabilities = _contract(CameraModel.ACTION6).capabilities()
    assert {capability.action_type for capability in capabilities if callable(capability.task_factory)} == {"camera_record"}


def test_action6_task_freezes_internal_scope_and_five_second_completion_rule():
    factory = _contract(CameraModel.ACTION6).task_factories.get("action6_record")
    assert callable(factory)
    task = factory(_params())
    assert task.target_duration_s == 10
    assert task.stop_supported is True
    assert task.file_completion_wait_s == Decimal("5")
    assert task.ownership_mode == 2
    assert task.output_scope == {"directories": ["/mnt/media_rw/emulated/DCIM"]}
    assert task.product_rules == ({"kind": "video", "format_id": "mp4", "min_count": 1,
                                  "exact_count": 1, "require_pairing": False},)


def test_action6_declaration_has_real_results_and_files_without_query_or_delete():
    declaration = _contract(CameraModel.ACTION6).declaration
    assert declaration.control_supported and declaration.stop_supported and declaration.result_supported
    assert declaration.directory_supported and declaration.read_supported and declaration.digest_supported
    assert not declaration.query_supported and not declaration.delete_supported
    assert not declaration.capture_read_parallel_supported


def test_action6_result_reader_is_declared_as_a_composite_operation():
    assert callable(getattr(_contract(CameraModel.ACTION6), "result_reader", None))


def test_action6_digest_deadline_is_finite_and_positive_for_source_sizes():
    timeout = _contract(CameraModel.ACTION6).digest_timeout_s
    assert callable(timeout)
    for size in (0, 1, 65536, 64637726):
        value = timeout(size)
        assert isinstance(value, Decimal) and value.is_finite() and value > 0


def test_osmo_builtin_remains_pending_for_both_capture_actions():
    assert all(capability.task_factory is None for capability in _contract(CameraModel.OSMO360II).capabilities())
