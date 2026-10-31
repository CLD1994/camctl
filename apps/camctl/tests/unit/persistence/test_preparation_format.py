"""准备格式启用后，既有 V2/V3 的格式资格仍然有效。"""

from enum import IntEnum
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.host_files.paths import PURPOSE_DIRECTORIES
from camctl.persistence import runtime


@pytest.fixture
def preparation_formats(monkeypatch):
    purposes = IntEnum("Purpose", {
        purpose.name: index for index, purpose in enumerate(PURPOSE_DIRECTORIES, start=1)
    })
    monkeypatch.setattr(runtime, "enum_for", create_autospec(runtime.enum_for, return_value=purposes))
    return purposes


def _connection(*, metadata_versions=None, purposes=None):
    if metadata_versions is None:
        metadata_versions = ",".join(str(version) for version in sorted(runtime.SUPPORTED_FORMAT_VERSIONS))
    if purposes is None:
        purposes = ",".join(str(index) for index in range(1, len(PURPOSE_DIRECTORIES) + 1))
    connection = create_autospec(sqlite3.Connection, instance=True)
    metadata = create_autospec(sqlite3.Cursor, instance=True)
    files = create_autospec(sqlite3.Cursor, instance=True)
    metadata.fetchone.return_value = (
        f"CREATE TABLE database_metadata (format_version INTEGER NOT NULL CHECK (format_version IN ({metadata_versions})))",
    )
    files.fetchone.return_value = (
        f"CREATE TABLE intermediate_files (purpose INTEGER NOT NULL CHECK (purpose IN ({purposes})))",
    )
    connection.execute.side_effect = [metadata, files]
    return connection


@pytest.mark.parametrize("format_version", [2, 3, 4, 5, 6, 7, 8])
def test_supported_modern_format_accepts_complete_purpose_contract(preparation_formats, format_version):
    runtime._verify_format_constraints(_connection(), format_version)


@pytest.mark.parametrize("format_version", [2, 3, 4, 5, 6, 7, 8])
def test_modern_format_rejects_incomplete_purpose_contract(preparation_formats, format_version):
    with pytest.raises(runtime.DatabaseInvalidError):
        runtime._verify_format_constraints(_connection(purposes="1,2,3,4"), format_version)


@pytest.mark.parametrize("format_version,metadata_versions", [(4, "1,2,3"), (5, "1,2,3,4")])
def test_declared_format_must_be_accepted_by_metadata_check(preparation_formats, format_version, metadata_versions):
    with pytest.raises(runtime.DatabaseInvalidError):
        runtime._verify_format_constraints(_connection(metadata_versions=metadata_versions), format_version)


def test_v1_retains_previous_metadata_constraint(preparation_formats):
    runtime._verify_format_constraints(_connection(metadata_versions="1,2,3"), 1)


@pytest.mark.parametrize("format_version", [4, 5, 6, 7, 8])
@pytest.mark.parametrize("action_type", [None, 2, 3])
def test_record_and_timelapse_preparation_keep_v4_minimum(preparation_formats, format_version, action_type):
    runtime.require_capture_preparation_format(format_version, action_type=action_type)


@pytest.mark.parametrize("format_version", [None, True, 1, 2, 3, 9])
def test_preparation_rejects_older_or_unknown_format(preparation_formats, format_version):
    with pytest.raises(runtime.DatabaseUnsupportedFormatError):
        runtime.require_capture_preparation_format(format_version)


@pytest.mark.parametrize("format_version", [5, 6, 7, 8])
def test_photo_preparation_accepts_v5_and_later(preparation_formats, format_version):
    runtime.require_capture_preparation_format(format_version, action_type=1)


@pytest.mark.parametrize("format_version", [None, True, 1, 2, 3, 4, 9])
def test_photo_preparation_rejects_older_or_unknown_formats(preparation_formats, format_version):
    with pytest.raises(runtime.DatabaseUnsupportedFormatError):
        runtime.require_capture_preparation_format(format_version, action_type=1)
