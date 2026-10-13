"""对 hb_cli 已核验输出样式进行纯解析测试。"""

import pytest


_NON_LF_SEPARATORS = [
    b"\v",
    b"\f",
    b"\r",
    b"\r\n",
    b"\x1c",
    b"\x1d",
    b"\x1e",
    "\u0085".encode("utf-8"),
    "\u2028".encode("utf-8"),
    "\u2029".encode("utf-8"),
]


def _api():
    # 放在用例执行时导入，使首轮缺少实现时失败位置清楚。
    from camctl.devices.drivers.hasselblad.responses import (
        CapturedFile,
        CaptureResponse,
        CliParseError,
        UsbCamera,
        parse_capture,
        parse_devices,
        parse_serial,
    )

    return (CapturedFile, CaptureResponse, CliParseError, UsbCamera,
            parse_capture, parse_devices, parse_serial)


def test_parse_one_usb_device_from_verified_list_line():
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    assert parse_devices(b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n") == (
        UsbCamera(vendor_id=0x2756, product_id=0x000A, bus=1, address=23),
    )


def test_parse_multiple_usb_devices_and_empty_device_list():
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    assert parse_devices(
        b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"
        b"vid=0x2756 pid=0x000b  bus=  2 addr=  4\n"
    ) == (
        UsbCamera(vendor_id=0x2756, product_id=0x000A, bus=1, address=23),
        UsbCamera(vendor_id=0x2756, product_id=0x000B, bus=2, address=4),
    )
    assert parse_devices(b"(no Hasselblad USB devices found)\n") == ()


@pytest.mark.parametrize("output", [
    b"vid=0x2756 pid=0x000a bus=1 addr=23\nextra\n",
    b"vid=0x2756 pid=0x000a bus=x addr=23\n",
    b"vid=0x2756 pid=0x000a bus=1 addr=23\n(no Hasselblad USB devices found)\n",
    b"\xff\n",
])
def test_device_list_rejects_unrecognized_or_invalid_output(output):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    with pytest.raises(CliParseError):
        parse_devices(output)


def test_parse_serial_preserves_text_after_info_separator():
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    assert parse_serial(
        b"firmware        : v4.1.0\nserial          : 64010904\n"
    ) == "64010904"


@pytest.mark.parametrize("output", [
    b"firmware : v4.1.0\n",
    b"serial : 64010904\nserial: 64010904\n",
    b"serial :    \n",
    b"serial : \xff\n",
])
def test_serial_rejects_missing_repeated_empty_or_non_utf8_value(output):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    with pytest.raises(CliParseError):
        parse_serial(output)


def test_parse_capture_requires_raw_and_returns_paired_files_in_output_directory():
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    result = parse_capture(
        b"capture: OK\n"
        b"saved /workspace/capture_22544386.3FR (211222528 bytes)\n"
        b"saved /workspace/capture_22544386.3FR.thumbnail.jpg (689251 bytes)\n",
        output_directory="/workspace",
    )

    assert result == CaptureResponse(
        original=CapturedFile(
            path="/workspace/capture_22544386.3FR",
            original_name="capture_22544386.3FR",
            size=211222528,
        ),
        preview=CapturedFile(
            path="/workspace/capture_22544386.3FR.thumbnail.jpg",
            original_name="capture_22544386.3FR.thumbnail.jpg",
            size=689251,
        ),
    )


@pytest.mark.parametrize("output, output_directory", [
    (b"capture: OK\nsaved /workspace/photo.jpg (10 bytes)\nsaved /workspace/photo.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/other.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/photo.3FR.thumbnail.jpg (0 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (-1 bytes)\nsaved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\nsurprise\n", "/workspace"),
    (b"capture: OK\nsaved /elsewhere/photo.3FR (10 bytes)\nsaved /elsewhere/photo.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/sub/photo.3FR (10 bytes)\nsaved /workspace/sub/photo.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/../photo.3FR (10 bytes)\nsaved /workspace/../photo.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/a\nb.3FR (10 bytes)\nsaved /workspace/a\nb.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/a\x00b.3FR (10 bytes)\nsaved /workspace/a\x00b.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/./photo.3FR (10 bytes)\nsaved /workspace/./photo.3FR.thumbnail.jpg (2 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/photo.3FR (10 bytes)\n", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\n", "workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\xff", "/workspace"),
    (b"capture: OK\nsaved /workspace/photo.3FR (10 bytes)\nsaved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\n", "/workspace/../workspace"),
])
def test_capture_rejects_untrusted_or_non_raw_file_results(output, output_directory):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    with pytest.raises(CliParseError):
        parse_capture(output, output_directory=output_directory)


def test_capture_accepts_spaces_in_direct_child_filenames():
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    result = parse_capture(
        b"capture: OK\n"
        b"saved /workspace/my photo.3FR (10 bytes)\n"
        b"saved /workspace/my photo.3FR.thumbnail.jpg (2 bytes)\n",
        output_directory="/workspace",
    )

    assert result.original.original_name == "my photo.3FR"
    assert result.preview.original_name == "my photo.3FR.thumbnail.jpg"


@pytest.mark.parametrize("output", [
    b"firmware        : v4.1.0\nserial          : 64010904\nerror: query failed\n",
    b"firmware        : v4.1.0\nserial          : 64010904\nunrecognized output\n",
    b"firmware        : v4.1.0\nserial          : 64010904\n\n",
])
def test_serial_rejects_non_parameter_info_rows(output):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    with pytest.raises(CliParseError):
        parse_serial(output)


@pytest.mark.parametrize("command", ["list", "info", "capture"])
@pytest.mark.parametrize("separator", _NON_LF_SEPARATORS)
def test_parsers_reject_unverified_non_lf_line_separators(command, separator):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()
    outputs = {
        "list": (
            b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n",
            parse_devices,
        ),
        "info": (
            b"firmware        : v4.1.0\nserial          : 64010904\n",
            parse_serial,
        ),
        "capture": (
            b"capture: OK\n"
            b"saved /workspace/photo.3FR (10 bytes)\n"
            b"saved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\n",
            parse_capture,
        ),
    }
    output, parser = outputs[command]
    output = output.replace(b"\n", separator)

    with pytest.raises(CliParseError):
        if command == "capture":
            parser(output, output_directory="/workspace")
        else:
            parser(output)


@pytest.mark.parametrize("command", ["list", "info", "capture"])
@pytest.mark.parametrize("placement", ["in_row", "trailing"])
@pytest.mark.parametrize("separator", _NON_LF_SEPARATORS)
def test_parsers_reject_non_lf_separators_inside_rows_and_at_end(
    command, placement, separator
):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    if command == "list":
        output = b"vid=0x2756 pid=0x000a bus=1 addr=23"
        if placement == "in_row":
            output = output.replace(b"addr=23", b"addr=2" + separator + b"3")
        else:
            output += separator
        parser = parse_devices
    elif command == "info":
        output = b"serial : 64010904"
        if placement == "in_row":
            output = output.replace(b"64010904", b"6401" + separator + b"0904")
        else:
            output += separator
        parser = parse_serial
    else:
        if placement == "in_row":
            original_name = b"photo" + separator + b".3FR"
        else:
            original_name = b"photo.3FR"
        preview_name = original_name + b".thumbnail.jpg"
        output = (
            b"capture: OK\n"
            b"saved /workspace/"
            + original_name
            + b" (10 bytes)\n"
            b"saved /workspace/"
            + preview_name
            + b" (2 bytes)"
        )
        if placement == "trailing":
            output += separator
        parser = parse_capture

    with pytest.raises(CliParseError):
        if command == "capture":
            parser(output, output_directory="/workspace")
        else:
            parser(output)


def test_serial_rejects_the_verified_single_line_vertical_tab_counterexample():
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    with pytest.raises(CliParseError):
        parse_serial(b"serial : 64010904\v")


@pytest.mark.parametrize("control", [b"\x00", b"\x01", b"\t", b"\x7f", "\u0080".encode("utf-8")])
def test_serial_rejects_control_characters_inside_its_value(control):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    with pytest.raises(CliParseError):
        parse_serial(b"serial : 64010904" + control + b"\n")


@pytest.mark.parametrize("field", ["bus", "address", "size"])
def test_decimal_conversion_limit_is_wrapped_as_cli_parse_error(field):
    CapturedFile, CaptureResponse, CliParseError, UsbCamera, parse_capture, parse_devices, parse_serial = _api()

    # Python 3.11's default decimal conversion limit is 4300 digits; keep it unchanged.
    oversized_decimal = b"1" * 4301
    if field == "bus":
        output = (
            b"vid=0x2756 pid=0x000a bus="
            + oversized_decimal
            + b" addr=23\n"
        )
        parser = parse_devices
    elif field == "address":
        output = (
            b"vid=0x2756 pid=0x000a bus=1 addr="
            + oversized_decimal
            + b"\n"
        )
        parser = parse_devices
    else:
        output = (
            b"capture: OK\n"
            b"saved /workspace/photo.3FR ("
            + oversized_decimal
            + b" bytes)\n"
            b"saved /workspace/photo.3FR.thumbnail.jpg (2 bytes)\n"
        )
        parser = parse_capture

    with pytest.raises(CliParseError):
        if field == "size":
            parser(output, output_directory="/workspace")
        else:
            parser(output)
