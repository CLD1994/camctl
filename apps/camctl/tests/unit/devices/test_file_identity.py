"""完整路径身份不合并不同绑定，特殊字符按原文本保存。"""
import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity


@pytest.mark.parametrize("path", ["/DCIM/sub/movie.mp4", "/DCIM/a 'b'.mp4", "/DCIM/a\nb.mp4", "/DCIM/照片.DNG"])
def test_full_path_identity_preserves_text_and_binding(path):
    identity = FileIdentity(DeviceBinding("cam-1", "dji-action6"), path)
    assert identity.as_json() == {"device_id": "cam-1", "driver_id": "dji-action6", "path": path}
    assert FileIdentity.from_json(identity.as_json()) == identity
    assert identity.sort_key == ("cam-1", "dji-action6", path)


@pytest.mark.parametrize("path", [None, "", "relative.mp4", "/", "//DCIM/a", "/DCIM/../a",
                                 "/DCIM/./a", "/DCIM//a", "/DCIM/a/", "/DCIM/a\0b"])
def test_noncanonical_or_unusable_identity_is_rejected(path):
    with pytest.raises(ValueError):
        FileIdentity(DeviceBinding("cam-1", "dji-action6"), path)


def test_different_devices_never_share_identity():
    first = FileIdentity(DeviceBinding("cam-1", "dji-action6"), "/DCIM/a")
    other = FileIdentity(DeviceBinding("cam-2", "dji-action6"), "/DCIM/a")
    assert first != other
