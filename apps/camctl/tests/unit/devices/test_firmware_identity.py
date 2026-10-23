"""固件资料完整性与有来源的稳定身份；不进行文件或设备 IO。"""
import hashlib
import json

import pytest

from camctl.devices.drivers.adb_cameras.firmware_identity import (
    firmware_fingerprint, firmware_profile, decode_identity, validate_firmware_fingerprint,
)


_DOCUMENT = {"Version": "v1.2.3.4", "Metabuild_Info": {
    "Meta_Build_ID": "test-build", "Product_Flavor": "asic", "Time_Stamp": "2026-10-20 19:32:30"}}
_CANONICAL = b'{"Metabuild_Info":{"Meta_Build_ID":"test-build","Product_Flavor":"asic","Time_Stamp":"2026-10-20 19:32:30"},"Version":"v1.2.3.4"}'


def test_action6_fingerprint_uses_complete_exact_canonical_json():
    expected = "action6-verinfo-json-sha256/v1:" + hashlib.sha256(_CANONICAL).hexdigest()
    assert firmware_fingerprint("dji-action6", json.dumps(_DOCUMENT, indent=2).encode() + b"\n") == expected
    assert firmware_fingerprint("dji-action6", _CANONICAL) == expected
    changed = {**_DOCUMENT, "extra": {"设备": [1, True, None]}}
    assert firmware_fingerprint("dji-action6", json.dumps(changed).encode()) != expected
    validate_firmware_fingerprint("dji-action6", expected)
    assert firmware_profile("dji-action6").command == "cat /firmware/verinfo/ver_info.txt"


@pytest.mark.parametrize("raw", [b"", b"{}", b'{"Version":"v1"}',
    b'{"Version":"v1.2.3.4","Version":"v2.3.4.5"}',
    b'{"Version":"v1.2.3.4","Metabuild_Info":null}', b"{\xff}",
    b'{"Version":"v1.2.3.4","Metabuild_Info":{},"n":NaN}',
    _CANONICAL + b" trailing", _CANONICAL.replace(b"19:32:30", b"99:32:30")])
def test_action6_incomplete_or_ambiguous_firmware_documents_are_rejected(raw):
    with pytest.raises((ValueError, UnicodeError)):
        firmware_fingerprint("dji-action6", raw)


def test_osmo_fingerprint_has_distinct_source_prefix_and_optional_line_ending():
    assert firmware_fingerprint("dji-osmo360-ii", b"device-build\n") == "android-build-fingerprint/v1:device-build"
    assert firmware_fingerprint("dji-osmo360-ii", b"device-build\r\n") == "android-build-fingerprint/v1:device-build"
    with pytest.raises(ValueError): firmware_fingerprint("dji-osmo360-ii", b"\n")
    with pytest.raises(ValueError): validate_firmware_fingerprint("dji-osmo360-ii", "device-build")
    with pytest.raises(ValueError): validate_firmware_fingerprint("dji-action6", "android-build-fingerprint/v1:device-build")


def test_identity_frame_preserves_complete_json_and_optional_serial():
    output = (b"CAMCTL-RECOVERY-IDENTITY/1\n00000000-0000-4000-8000-000000000001\n"
        b"CAMCTL-FIRMWARE\n" + _CANONICAL + b"\nCAMCTL-SERIAL\n\nCAMCTL-END\n")
    identity = decode_identity("dji-action6", output)
    assert identity[0] == "00000000-0000-4000-8000-000000000001" and identity[2] is None
    assert identity[1] == firmware_fingerprint("dji-action6", _CANONICAL)
    with pytest.raises(ValueError): decode_identity("dji-action6", output[:-2])
