"""日常 CLI 在受理和报告工作前核对本次三路径与已保存绑定。"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config
from camctl.bootstrap.application import ConfigAdapter
from camctl.bootstrap import lifecycle
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.cli import main
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.reporting.messages import ErrorKind, ResultFailureMessage
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration, WorkerShutdown, WorkerSupervisor
from camctl.session.clock import ClockCheckInput

from ..acceptance.test_acceptance import Catalog


def _paths(home):
    return {name: str(home / name) for name in ("staging", "ready", "processing")}


def _document(home, directories=None):
    return {"paths": {
        "state_db": str(home / "state.db"), "log_file": str(home / "camctl.log"),
        **(directories if directories is not None else _paths(home)),
    }}


def _config_file(home, directories=None):
    path = home / "config.toml"
    path.write_text("[paths]\n" + "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in _document(home, directories)["paths"].items()
    ) + "\n", encoding="utf-8")
    return path


def _plan_file(home):
    path = home / "plan.json"
    path.write_text(json.dumps({
        "request_id": "1", "created_at": "2026-01-15 08:00:00", "name": "补齐状态",
        "actions": [{"name": "补齐", "type": "report_status", "params": {"scope": "full"}}],
    }), encoding="utf-8")
    return path


def _invoke(home, mode, directories=None, *, plan=True):
    out, err = io.StringIO(), io.StringIO()
    argv = [mode, "--config", str(_config_file(home, directories))]
    if plan:
        argv.append(str(_plan_file(home)))
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _facts(home):
    with sqlite3.connect(home / "state.db") as connection:
        return {
            "counts": {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("plans", "actions", "plan_file_diagnostics", "history_events",
                                     "history_transactions", "state_syncs", "reports")},
            "metadata": connection.execute("SELECT * FROM database_metadata").fetchall(),
            "runtime": connection.execute("SELECT * FROM runtime_state").fetchall(),
        }


def _files(home):
    return {str(path.relative_to(home)): path.read_bytes()
            for root in _paths(home).values() for path in Path(root).rglob("*") if path.is_file()}


@pytest.fixture
def deployed(tmp_path):
    cfg = load_config(_document(tmp_path), ConfigDefaults())
    initialized = initialize_state(cfg, tmp_path / "state.db")
    assert initialized.outcome is InitOutcome.CREATED, initialized.detail
    for name in _paths(tmp_path):
        (tmp_path / name / "existing.txt").write_bytes(f"已保存的{name}文件".encode())
    return tmp_path


@pytest.mark.parametrize("mode", ["run", "submit"])
@pytest.mark.parametrize("changed", [("staging",), ("ready",), ("processing",),
                                     ("staging", "ready", "processing")])
def test_run_and_submit_reject_each_changed_directory_before_acceptance(deployed, mode, changed):
    home = deployed
    original = _paths(home)
    configured = {**original, **{name: str(home / ("changed-" + name)) for name in changed}}
    facts, files = _facts(home), _files(home)

    code, payload, _diagnostic = _invoke(home, mode, configured)

    assert code == 1
    result = json.loads(payload)
    assert result["kind"] == "error"
    assert result["body"]["reason"] == "configuration_error"
    diagnostic = json.dumps(result["body"]["details"], ensure_ascii=False)
    for name in changed:
        assert name in diagnostic
        assert original[name] in diagnostic
        assert configured[name] in diagnostic
    assert _facts(home) == facts
    assert _files(home) == files
    assert all(not Path(configured[name]).exists() for name in changed)


def test_equivalent_normalized_directory_text_keeps_original_binding(deployed):
    home = deployed
    configured = {name: value + "//child/../." for name, value in _paths(home).items()}
    original_metadata = _facts(home)["metadata"]

    code, payload, _diagnostic = _invoke(home, "submit", configured)

    assert code == 0
    assert json.loads(payload) == {"kind": "succeeded", "body": {"needs_run": True}}
    assert _facts(home)["metadata"] == original_metadata


@pytest.mark.parametrize("state", ["missing", "invalid"])
@pytest.mark.parametrize("mode", ["run", "submit"])
def test_directory_gate_keeps_database_error_classification(tmp_path, state, mode):
    if state == "invalid":
        (tmp_path / "state.db").write_bytes(b"invalid state database")

    code, payload, _diagnostic = _invoke(tmp_path, mode)

    assert code == 1
    assert payload, "状态库错误仍须通过最终机器结果通道返回"
    assert json.loads(payload)["body"]["reason"] == "state_db_error"
    if state == "missing":
        assert not (tmp_path / "state.db").exists()


def test_describe_does_not_require_database_or_bound_directories(tmp_path):
    code, payload, _diagnostic = _invoke(tmp_path, "describe", plan=False)

    assert code == 0
    assert json.loads(payload)["devices"] == []
    assert not (tmp_path / "state.db").exists()
    assert all(not Path(path).exists() for path in _paths(tmp_path).values())


@pytest.mark.parametrize("name", ["staging", "ready", "processing"])
@pytest.mark.parametrize("invalid", ["relative/path", "/absolute/path\x00suffix"])
def test_adapter_rejects_nonabsolute_or_nul_directories(deployed, name, invalid):
    paths = {**_paths(deployed), name: invalid}
    config_file = _config_file(deployed, paths)

    with pytest.raises(ConfigError) as failure:
        ConfigAdapter(home=deployed).load(config_file)

    assert name in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["relative/path", "/absolute/path\x00suffix"])
async def test_runtime_rejects_invalid_snapshot_directory(deployed, invalid):
    cfg = load_config(_document(deployed), ConfigDefaults())
    if invalid == "relative/path":
        invalid = os.path.relpath(deployed / "ready")
    cfg = replace(cfg, paths=replace(cfg.paths, ready=invalid))
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "configuration_error"
    assert "ready" in str(outcome.details)
    assert deps.log_runtime is None


def test_adapter_normalizes_expanded_paths_without_directory_access(deployed):
    directories = {name: "$HOME/" + name + "//unused/../."
                   for name in _paths(deployed)}

    cfg = ConfigAdapter(home=deployed).load(_config_file(deployed, directories))

    assert (cfg.paths.staging, cfg.paths.ready, cfg.paths.processing) == tuple(_paths(deployed).values())
    assert all(not (deployed / name / "unused").exists() for name in _paths(deployed))


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_report_configuration_error_stops_normal_and_restricted_session(deployed, mocker, restricted):
    cfg = load_config(_document(deployed), ConfigDefaults())
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    supervisor = mocker.create_autospec(WorkerSupervisor, instance=True, spec_set=True)
    jobs, devices = [], []

    async def reject(job):
        jobs.append(job)
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE, job_id=job.job_id,
            failure=ResultFailureMessage(
                job_id=job.job_id, instance_id=job.instance_id, error_kind=ErrorKind.REPORT,
                error_code="configuration_error", error_message="ready: 原路径 /old，配置新路径 /new"))

    async def device(_context):
        devices.append("called")

    supervisor.generate.side_effect = reject
    supervisor.stop.return_value = WorkerShutdown(exitcode=0, forced=False)
    original_assembly = lifecycle._report_assembly

    def assemble(runtime_deps, failure_log):
        flows, report_supervisor = original_assembly(runtime_deps, failure_log)
        return {
            "report": flows["report"],
            "device": device,
            **{name: flow for name, flow in flows.items() if name != "report"},
        }, report_supervisor

    mocker.patch("camctl.bootstrap.lifecycle._report_assembly", side_effect=assemble)
    mocker.patch("camctl.reporting.supervisor.WorkerSupervisor", return_value=supervisor)
    if restricted:
        mocker.patch("camctl.bootstrap.lifecycle._clock_policy_factory", return_value=lambda _connection:
                     ClockCheckInput(None, 9_000_000_000_000_000, 0, 0))
    source = ParsedInput("plan.json", json.loads(_plan_file(deployed).read_text()))
    try:
        outcome = await execute_command(deps, source)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "configuration_error"
    assert "/old" in str(outcome.details) and "/new" in str(outcome.details)
    assert devices == []
    assert len(jobs) == 1
    assert (jobs[0].staging_root, jobs[0].ready_root, jobs[0].processing_root) == (
        cfg.paths.staging, cfg.paths.ready, cfg.paths.processing)
    with sqlite3.connect(deps.state_db) as connection:
        assert connection.execute("SELECT last_error_json FROM reports").fetchall() == [(None,)]
