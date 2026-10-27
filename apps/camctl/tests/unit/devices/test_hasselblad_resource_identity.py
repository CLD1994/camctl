"""固定机身身份及资源键的纯契约。"""

import pytest

from camctl.devices.drivers.hasselblad.identity import CameraIdentity


def test_resource_key_uses_driver_and_exact_serial_not_product():
    first = CameraIdentity(serial='A/"B', product_id=10)
    second = CameraIdentity(serial='A/"B', product_id=11)
    assert first.resource_key == second.resource_key
    assert first.resource_key == '["hasselblad_cfv100c","A/\\"B"]'
    assert first != second
    assert first.resource_key != CameraIdentity(serial='A/"B ', product_id=10).resource_key


@pytest.mark.parametrize("serial", [None, "", "x\ny", "x\x00y", 1])
def test_identity_rejects_invalid_serial(serial):
    with pytest.raises(ValueError):
        CameraIdentity(serial=serial, product_id=10)


@pytest.mark.parametrize("product_id", [None, True, 0, 65536, "10"])
def test_identity_rejects_invalid_product_id(product_id):
    with pytest.raises(ValueError):
        CameraIdentity(serial="serial", product_id=product_id)
