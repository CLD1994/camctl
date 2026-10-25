"""任务执行将绑定参数交给实际生成仓储，并保留错误分类。"""

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.persistence.runtime import (
    DatabaseMetadata, DatabaseUnsupportedFormatError, DbConfig, DbOpenMode,
    DirectoryBindingError, OwnedConnection,
)
from camctl.reporting import worker
from camctl.reporting.generation import GeneratedFile, GenerationSpec
from camctl.reporting.messages import (
    ErrorKind, JobMessage, ResultFailureMessage, ResultSuccessMessage,
)


@pytest.fixture
def job():
    return JobMessage(
        job_id="generation-12", report_id=12, from_wm=5, to_wm=8,
        frozen_event_id=42, instance_id="a" * 32, db_path="/state.db",
        staging_path="/staging/report.tmp", staging_root="/staging",
        ready_root="/ready", processing_root="/processing",
        entity_batch_size=17, event_batch_size=31, busy_timeout_ms=749,
    )


@pytest.fixture
def collaborators(mocker, job):
    metadata = DatabaseMetadata("camctl", job.instance_id, 5,
                                job.staging_root, job.ready_root, job.processing_root)
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    opener = mocker.patch.object(worker, "open_existing", autospec=True,
                                return_value=OwnedConnection(connection, metadata))
    directory_check = mocker.patch.object(worker, "verify_directory_binding", autospec=True)
    repository_type = mocker.patch.object(worker, "HistoryRepository", autospec=True, spec_set=True)
    generated = GeneratedFile(Path(job.staging_path), 123, "b" * 64)
    generate = mocker.patch.object(worker, "generate_report_file", autospec=True,
                                  return_value=generated)
    rename = mocker.patch.object(worker.os, "replace", autospec=True)
    return SimpleNamespace(metadata=metadata, connection=connection, opener=opener,
                           directory_check=directory_check, repository_type=repository_type,
                           generated=generated, generate=generate, rename=rename)


def test_run_job_passes_binding_and_timeout_to_the_generating_repository(job, collaborators):
    result = worker.run_job(job)

    assert isinstance(result, ResultSuccessMessage)
    assert (result.job_id, result.instance_id, result.size_bytes, result.sha256) == (
        job.job_id, job.instance_id, 123, "b" * 64)
    canonical = Path("/staging/status-report-12-" + "b" * 64 + ".json")
    assert result.path == str(canonical)
    collaborators.opener.assert_called_once_with(
        Path(job.db_path), DbOpenMode.EXISTING_RO, DbConfig(busy_timeout_ms=749))
    collaborators.connection.close.assert_called_once()
    collaborators.directory_check.assert_called_once_with(
        collaborators.metadata, job.staging_root, job.ready_root, job.processing_root)
    collaborators.repository_type.assert_called_once_with(
        Path(job.db_path), config=DbConfig(busy_timeout_ms=749),
        expected_instance_id=job.instance_id,
        expected_directories=(job.staging_root, job.ready_root, job.processing_root))
    collaborators.generate.assert_called_once_with(
        GenerationSpec(Path(job.db_path), 12, 5, 8, 42, Path(job.staging_path), 17, 31),
        repository=collaborators.repository_type.return_value)
    collaborators.rename.assert_called_once_with(Path(job.staging_path), canonical)


@pytest.mark.parametrize("error,kind,code", [
    (ConsistencyError("生成连接的实例身份发生变化"), ErrorKind.STATE, "ConsistencyError"),
    (DirectoryBindingError("生成连接的目录绑定不一致"), ErrorKind.REPORT, "configuration_error"),
    (DatabaseUnsupportedFormatError("生成连接的格式不受支持"),
     ErrorKind.STATE, "DatabaseUnsupportedFormatError"),
])
def test_generation_failure_is_reported_even_after_metadata_precheck_succeeds(
        job, collaborators, error, kind, code):
    collaborators.generate.side_effect = error

    result = worker.run_job(job)

    assert isinstance(result, ResultFailureMessage)
    assert (result.job_id, result.instance_id, result.error_kind, result.error_code) == (
        job.job_id, job.instance_id, kind, code)
    assert str(error) in result.error_message
    collaborators.connection.close.assert_called_once()
    collaborators.directory_check.assert_called_once()
    assert collaborators.generate.call_args.kwargs["repository"] is (
        collaborators.repository_type.return_value)
    collaborators.rename.assert_not_called()


def test_metadata_instance_mismatch_keeps_its_existing_result_and_stops_generation(job, collaborators):
    observed = "c" * 32
    collaborators.opener.return_value = OwnedConnection(
        collaborators.connection, replace(collaborators.metadata, instance_id=observed))

    result = worker.run_job(job)

    assert isinstance(result, ResultFailureMessage)
    assert (result.job_id, result.instance_id, result.error_kind, result.error_code) == (
        job.job_id, observed, ErrorKind.STATE, "instance_mismatch")
    collaborators.connection.close.assert_called_once()
    collaborators.directory_check.assert_not_called()
    collaborators.repository_type.assert_not_called()
    collaborators.generate.assert_not_called()
    collaborators.rename.assert_not_called()


def test_metadata_directory_mismatch_stops_generation_with_configuration_error(job, collaborators):
    collaborators.directory_check.side_effect = DirectoryBindingError("配置目录已改变")

    result = worker.run_job(job)

    assert isinstance(result, ResultFailureMessage)
    assert (result.error_kind, result.error_code) == (ErrorKind.REPORT, "configuration_error")
    collaborators.connection.close.assert_called_once()
    collaborators.repository_type.assert_not_called()
    collaborators.generate.assert_not_called()
    collaborators.rename.assert_not_called()
