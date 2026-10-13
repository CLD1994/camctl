"""纯 Hasselblad 配置解析不访问工具、文件或相机。"""

import pytest

def _declaration(hasselblad):
    return {"kind": "camera", "driver": "hasselblad_cfv100c", "hasselblad": hasselblad}


def test_camera_config_preserves_serial_and_defaults_product_id():
    from camctl.devices.drivers.hasselblad.configuration import (
        CameraConfig,
        read_camera_config,
    )

    result = read_camera_config(
        _declaration({"executable": "/home/cld/hb_cli/hb_cli", "serial": " 64010904 "})
    )
    assert result == CameraConfig("/home/cld/hb_cli/hb_cli", " 64010904 ", 10)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"serial": "64010904"},
        {"executable": "/home/cld/hb_cli/hb_cli"},
        {"executable": "", "serial": "64010904"},
        {"executable": "hb_cli", "serial": "64010904"},
        {"executable": "/home/cld/hb_cli/\x00hb_cli", "serial": "64010904"},
        {"executable": "/home/cld/hb_cli/hb_cli", "serial": ""},
        {"executable": "/home/cld/hb_cli/hb_cli", "serial": "bad\nserial"},
        {"executable": "/home/cld/hb_cli/hb_cli", "serial": "64010904", "product_id": True},
        {"executable": "/home/cld/hb_cli/hb_cli", "serial": "64010904", "product_id": 0},
        {"executable": "/home/cld/hb_cli/hb_cli", "serial": "64010904", "product_id": 65536},
        {"executable": "/home/cld/hb_cli/hb_cli", "serial": "64010904", "unknown": 1},
    ],
)
def test_camera_config_rejects_invalid_or_unknown_fields(value):
    from camctl.devices.drivers.hasselblad.configuration import read_camera_config

    with pytest.raises(ValueError):
        read_camera_config(_declaration(value))


@pytest.mark.parametrize(
    "declaration",
    [
        {"kind": "camera", "driver": "another_driver", "hasselblad": {"executable": "/x", "serial": "s"}},
        {"kind": "other", "driver": "hasselblad_cfv100c", "hasselblad": {"executable": "/x", "serial": "s"}},
        {"kind": "camera", "driver": "hasselblad_cfv100c", "hasselblad": None},
    ],
)
def test_camera_config_requires_the_hasselblad_camera_declaration(declaration):
    from camctl.devices.drivers.hasselblad.configuration import read_camera_config

    with pytest.raises(ValueError):
        read_camera_config(declaration)
