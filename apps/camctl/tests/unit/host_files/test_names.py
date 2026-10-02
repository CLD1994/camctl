"""登记名称由文件身份和扩展名确定，与用户可读名称分离。"""

import pytest

from camctl.host_files import paths
from camctl.host_files.models import FilePurpose


@pytest.mark.parametrize("identity,extension,expected", [
    (28, "mp4", "28.mp4"), (28, None, "28"),
    (9223372036854775807, "MP4", "9223372036854775807.MP4"),
    (1, "x" * 253, "1." + "x" * 253),
])
def test_object_file_name_preserves_identity_and_valid_extension(identity, extension, expected):
    assert paths.object_file_name(identity, extension) == expected


@pytest.mark.parametrize("extension", ["", "a.b", "../mp4", "mp4/", "mp4\\", "视频", " mp4", "mp4\n", "x\0", True, 1])
def test_object_file_name_rejects_invalid_extension(extension):
    with pytest.raises(ValueError):
        paths.object_file_name(28, extension)


@pytest.mark.parametrize("identity", [True, 0, -1, 1.0, "1", 9223372036854775808])
def test_object_file_name_rejects_invalid_identity(identity):
    with pytest.raises(ValueError):
        paths.object_file_name(identity, "mp4")


def test_object_file_name_checks_length_after_identity_allocation():
    with pytest.raises(ValueError):
        paths.object_file_name(28, "x" * 253)


@pytest.mark.parametrize("purpose,expected", [
    (FilePurpose.DELIVERY_COPY, "deliveries/28.part"),
    (FilePurpose.RECORDING_INPUT, "recording-inputs/28.part"),
    (FilePurpose.PROCESSING_TEMP, "processing-temp/28.part"),
    (FilePurpose.REPAIR_OUTPUT, "derived/28.part"),
])
def test_relative_file_path_uses_formal_purpose_directory(purpose, expected):
    assert paths.relative_file_path(purpose, 28, "part") == expected


@pytest.mark.parametrize("purpose", [None, "delivery_copy", 1])
def test_relative_file_path_rejects_untyped_purpose(purpose):
    with pytest.raises(ValueError):
        paths.relative_file_path(purpose, 28, None)
