"""测试入口传递同一预算，并在真正启动 CLI 前拒绝不可用环境。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def entrypoints(monkeypatch):
    backend = ModuleType("camctl_memory")
    backend.DEFAULT_MEMORY_LIMIT = "7GiB"
    backend.BudgetEnvironmentError = type("BudgetEnvironmentError", (RuntimeError,), {})
    backend.MemoryBudgetExceeded = type("MemoryBudgetExceeded", (RuntimeError,), {})
    backend.config = SimpleNamespace(limit_bytes=7516192768, cgroup_root=None)
    backend.requests = []
    backend.environment_error = None

    def configure(limit, cgroup_root=None):
        if limit == "invalid":
            raise ValueError("invalid capacity")
        backend.config = SimpleNamespace(limit_bytes=123456, cgroup_root=cgroup_root)
        backend.requested_limit = limit
        return backend.config

    def preflight(config):
        if backend.environment_error:
            raise backend.environment_error

    def run(command, **kwargs):
        backend.requests.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7, "business-out", "business-err")

    def start(command, **kwargs):
        backend.requests.append((command, kwargs))
        return SimpleNamespace(pid=731, returncode=None)

    backend.configure_budget = configure
    backend.get_budget_config = lambda: backend.config
    backend.ensure_memory_budget_available = preflight
    backend.run_budgeted = run
    backend.BudgetedPopen = start
    backend.parse_memory_limit = lambda value: 123456
    backend.finalize_budget_receipts = lambda paths: None
    monkeypatch.setitem(sys.modules, "camctl_memory", backend)
    plugin = load_script("memory_entrypoints_under_test", "scripts/camctl_memory_plugin.py")
    monkeypatch.setitem(sys.modules, "camctl_memory_plugin", plugin)
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: path.absolute())
    return plugin, backend


def test_pytest_configuration_does_not_probe_controller(entrypoints):
    plugin, backend = entrypoints
    backend.environment_error = backend.BudgetEnvironmentError("no delegation")
    config = SimpleNamespace(getoption=lambda name: {
        "camctl_memory_limit": "123456B", "camctl_memory_cgroup_root": "/delegated",
    }[name])
    plugin.pytest_configure(config)
    assert backend.requested_limit == "123456B"
    assert backend.config.cgroup_root == Path("/delegated")


def test_pytest_default_comes_from_shared_backend(entrypoints):
    plugin, _ = entrypoints
    parser = argparse.ArgumentParser()
    plugin.pytest_addoption(SimpleNamespace(getgroup=lambda name: SimpleNamespace(
        addoption=parser.add_argument)))
    assert parser.parse_args([]).camctl_memory_limit == "7GiB"


def test_invalid_pytest_budget_is_usage_error(entrypoints):
    plugin, _ = entrypoints
    config = SimpleNamespace(getoption=lambda name: "invalid" if name == "camctl_memory_limit" else None)
    with pytest.raises(pytest.UsageError, match="invalid capacity"):
        plugin.pytest_configure(config)


def test_relative_controller_path_is_fixed_before_subprocess_cwd_changes(entrypoints, monkeypatch):
    plugin, backend = entrypoints
    monkeypatch.setattr(os, "getcwd", lambda: "/virtual/source")
    config = SimpleNamespace(getoption=lambda name: {
        "camctl_memory_limit": "123456B", "camctl_memory_cgroup_root": "delegated",
    }[name])
    plugin.pytest_configure(config)
    assert backend.config.cgroup_root == Path("/virtual/source/delegated")


def test_cli_environment_failure_prevents_unbounded_launch(entrypoints):
    plugin, backend = entrypoints
    backend.environment_error = backend.BudgetEnvironmentError("no delegation")
    with pytest.raises(backend.BudgetEnvironmentError, match="no delegation"):
        plugin.run_camctl(["python", "-m", "camctl"], receipt_root=Path("/receipts"))
    assert backend.requests == []


def test_cli_keeps_business_exit_and_unique_receipts(entrypoints):
    plugin, backend = entrypoints
    command = ["python", "-m", "camctl", "run"]
    environment = {"IDENTITY": "installed"}
    result = plugin.run_camctl(command, receipt_root=Path("/receipts"),
                               timeout=42, capture_output=True, text=True, env=environment)
    process = plugin.start_camctl(command, receipt_root=Path("/receipts"),
                                  stdout=subprocess.PIPE, start_new_session=True)
    assert (result.returncode, result.stdout, result.stderr) == (7, "business-out", "business-err")
    assert process.pid == 731
    first, second = [kwargs for _, kwargs in backend.requests]
    assert first["budget"] is backend.config
    assert first["timeout"] == 42 and first["env"] is environment
    assert first["receipt_path"] != second["receipt_path"]
    assert first["receipt_path"].parent == Path("/receipts/memory-budget")
    assert second["start_new_session"] is True


def test_host_launcher_carries_explicit_config_and_original_command(entrypoints):
    plugin, backend = entrypoints
    backend.config = SimpleNamespace(limit_bytes=3145728, cgroup_root=Path("/delegated group"))
    original = ["/installed/camctl", "run", "--config", "/runtime/config.toml"]
    command = plugin.memory_launcher_command(original, receipt_root=Path("/receipts"))
    separator = command.index("--")
    assert command[separator + 1:] == original
    assert command[command.index("--camctl-memory-limit") + 1] == "3145728B"
    assert command[command.index("--camctl-memory-cgroup-root") + 1] == "/delegated group"


def test_default_home_launcher_keeps_original_command_and_forwards_host_arguments(entrypoints, monkeypatch):
    plugin, _ = entrypoints
    root = Path("/virtual/runtime")
    target = root / "home" / ".camctl" / "venv" / "bin" / "camctl"
    contents, permissions = {}, {}
    def write(path, text, *, encoding):
        assert encoding == "utf-8"
        contents[path] = text
        return len(text)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(Path, "chmod", lambda path, mode: permissions.__setitem__(path, mode))
    original = ["/installed python", "-c", "from camctl.cli import main; main()"]
    assert plugin.write_budgeted_launcher(original, receipt_root=root, target=target) == target
    words = shlex.split(contents[target].splitlines()[1])
    assert words[0] == "exec" and words[-1] == "$@"
    separator = words.index("--")
    assert words[separator + 1:-1] == original
    assert permissions == {target: 0o700}
    assert root / "memory-budget" in plugin._receipt_directories


@pytest.mark.parametrize("use_default_paths", [False, True])
def test_host_process_stays_outside_budget_and_preserves_default_argv(
    entrypoints, monkeypatch, use_default_paths,
):
    host_module = load_script("memory_host_under_test", "tests/integration/_wsl_host_demo.py")
    deployment = SimpleNamespace(root=Path("/world"), ready=Path("/world/ready"),
                                 processing=Path("/world/processing"), config_path=Path("/world/config"))
    wrapped = []
    def launcher(command, **kwargs):
        wrapped.append((command, kwargs))
        return Path("/world/memory-launcher.sh")
    launches = []
    def start(command, **kwargs):
        launches.append((command, kwargs))
        return SimpleNamespace(pid=731, stdout=SimpleNamespace(readline=lambda: "submit <path>, claim, logs, help"))
    monkeypatch.setattr(host_module, "budgeted_host_launcher", launcher)
    monkeypatch.setattr(host_module.subprocess, "Popen", start)
    monkeypatch.setattr(host_module, "_NATIVE_POSIX", True)
    host = host_module.HostDemo(deployment, "/host-demo", launcher="/original/camctl")
    host.start(env={"HOME": "/home"}, use_default_paths=use_default_paths)
    command, options = launches[0]
    assert options["env"] == {"HOME": "/home"}
    if use_default_paths:
        assert command == ["/host-demo"] and wrapped == []
    else:
        assert command[command.index("--camctl") + 1] == "/world/memory-launcher.sh"
        assert wrapped == [("/original/camctl", {"receipt_root": Path("/world")})]
    assert entrypoints[1].requests == []


def test_host_observer_missing_launcher_keeps_original_spawn_boundary(entrypoints, monkeypatch):
    host_module = load_script("memory_observer_host_under_test", "tests/integration/_wsl_host_demo.py")
    deployment = SimpleNamespace(root=Path("/world"), ready=Path("/world/ready"),
                                 processing=Path("/world/processing"), config_path=None)
    def unexpected(*args, **kwargs):
        pytest.fail("纯 host 观察器替身不得创建 camctl 预算代理")
    launches = []
    def start(command, **kwargs):
        launches.append(command)
        return SimpleNamespace(pid=731, stdout=SimpleNamespace(readline=lambda: "submit <path>, claim, logs, help, quit"))
    monkeypatch.setattr(host_module, "budgeted_host_launcher", unexpected)
    monkeypatch.setattr(host_module.subprocess, "Popen", start)
    monkeypatch.setattr(host_module, "_NATIVE_POSIX", True)
    host = host_module.HostDemo(deployment, "/host-demo", launcher="/missing/original-launcher",
                               launcher_is_camctl=False)
    host.start()
    assert launches[0][launches[0].index("--camctl") + 1] == "/missing/original-launcher"
    assert entrypoints[1].requests == []


def test_runner_forwards_budget_without_environment(entrypoints):
    _, _backend = entrypoints
    runner = load_script("memory_runner_under_test", "scripts/run-integration.py")
    args = SimpleNamespace(camctl_memory_limit="384MiB", camctl_memory_cgroup_root=Path("/delegated"))
    assert runner.memory_pytest_arguments(args) == [
        "--camctl-memory-limit", "384MiB", "--camctl-memory-cgroup-root", "/delegated",
    ]


def test_mixed_tool_command_is_not_put_in_camctl_budget(entrypoints, monkeypatch):
    plugin, backend = entrypoints
    backend.environment_error = backend.BudgetEnvironmentError("no delegation")
    calls = []
    def ordinary(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(plugin.subprocess, "run", ordinary)
    command = ["uv", "pip", "install", "wheel.whl"]
    result = plugin.run_test_command(command, receipt_root=Path("/receipts"))
    assert result.returncode == 0 and calls == [command]
    assert backend.requests == []


def test_capability_installed_cli_marks_only_cli_commands(entrypoints, monkeypatch):
    plugin, _ = entrypoints
    installed_module = load_script("memory_capability_under_test",
                                   "tests/integration/test_device_capability_declarations.py")
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "declaration", "")
    monkeypatch.setattr(installed_module, "run_test_command", command)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "glob", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: None)
    installed = installed_module.Installed(Path("/package"), Path("/installed/python"),
                                           Path("/package/wheel.whl"), {"ORIGIN": "installed"})
    assert installed.cli("describe") == "declaration"
    assert installed.run(["uv", "pip", "install", "wheel.whl"]) == "declaration"
    assert calls[0][0] == ["/installed/python", "-m", "camctl", "describe"]
    assert calls[0][1]["camctl"] is True
    assert calls[1][0] == ["uv", "pip", "install", "wheel.whl"]
    assert calls[1][1]["camctl"] is False


def test_deployment_uses_budget_without_changing_cli_input(entrypoints):
    _, backend = entrypoints
    fixtures = load_script("memory_deployment_under_test", "tests/integration/camctl_fixtures.py")
    deployment = fixtures.Deployment.__new__(fixtures.Deployment)
    deployment.root = Path("/world")
    deployment.state_db = Path("/world/state.db")
    deployment._controlled_clock = False
    deployment.clock_env = lambda: {}
    result = deployment.camctl("run", "--config", "/world/config.toml")
    process = deployment.start_camctl("submit", "/world/plan.json")
    assert result.exit_code == 7 and process.pid == 731
    assert backend.requests[0][0][-3:] == ["run", "--config", "/world/config.toml"]
    assert backend.requests[1][0][-2:] == ["submit", "/world/plan.json"]
    assert backend.requests[0][1]["env"]["CAMCTL_TEST_STATE_DB"] == "/world/state.db"


def test_teardown_recovers_registered_receipts_after_fixture_cleanup(entrypoints):
    plugin, backend = entrypoints
    receipt = Path("/registered/camctl-owned.json")
    class Directory:
        def glob(self, pattern):
            return [receipt] if pattern == "camctl-*.json" else []
    plugin._receipt_directories.add(Directory())
    recovered = []
    backend.finalize_budget_receipts = lambda paths: recovered.extend(paths)
    hook = plugin.pytest_runtest_teardown(None, None)
    next(hook)
    assert recovered == []
    with pytest.raises(StopIteration):
        next(hook)
    assert recovered == [receipt]
    assert not plugin._receipt_directories


@pytest.mark.parametrize("active", [[], [731]])
def test_archive_finalizer_checks_stopped_world_and_keeps_original_diagnostic(entrypoints, monkeypatch, active):
    plugin, backend = entrypoints
    receipt = Path("/world/memory-budget/camctl-owned.json")
    monkeypatch.setattr(Path, "glob", lambda directory, pattern:
                        [receipt] if pattern == "camctl-*.json" else [])
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs:
                        json.dumps({"status": "pending", "group": "/groups/owned"})
                        if path == receipt else "\n".join(map(str, active)))
    finalized = []
    backend.finalize_budget_receipts = lambda paths: finalized.extend(paths)

    for _ in range(2):
        if active:
            with pytest.raises(backend.BudgetEnvironmentError, match="仍有活动进程.*731"):
                plugin.finalize_receipts(Path("/world"))
        else:
            plugin.finalize_receipts(Path("/world"))

    assert finalized == [receipt]


def test_archive_finalizer_processes_all_receipts_before_raising(entrypoints, monkeypatch):
    plugin, backend = entrypoints
    receipts = [Path("/world/memory-budget/camctl-oom.json"),
                Path("/world/memory-budget/camctl-clean.json")]
    monkeypatch.setattr(Path, "glob", lambda directory, pattern:
                        receipts if pattern == "camctl-*.json" else [])
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs:
                        json.dumps({"status": "completed", "group_removed": True}))
    finalized = []
    def finalize(paths):
        finalized.extend(paths)
        if paths == [receipts[0]]:
            raise backend.MemoryBudgetExceeded("child OOM")
    backend.finalize_budget_receipts = finalize

    with pytest.raises(backend.BudgetEnvironmentError, match="child OOM"):
        plugin.finalize_receipts(Path("/world"))

    assert finalized == receipts


def test_teardown_does_not_hide_resource_failure_from_host(entrypoints):
    plugin, backend = entrypoints
    class Directory:
        def glob(self, pattern):
            return [Path("/registered/camctl-owned.json")] if pattern == "camctl-*.json" else []
    plugin._receipt_directories.add(Directory())
    def exceeded(paths):
        raise backend.MemoryBudgetExceeded("child OOM despite successful host")
    backend.finalize_budget_receipts = exceeded
    hook = plugin.pytest_runtest_teardown(None, None)
    next(hook)
    with pytest.raises(pytest.fail.Exception, match="child OOM"):
        next(hook)


def test_teardown_surfaces_proxy_failure_before_receipt_creation(entrypoints):
    plugin, _ = entrypoints
    failure = SimpleNamespace(read_text=lambda *, encoding: "BudgetEnvironmentError: unavailable")
    class Directory:
        def glob(self, pattern):
            return [failure] if pattern == "camctl-*.failure" else []
    plugin._receipt_directories.add(Directory())
    hook = plugin.pytest_runtest_teardown(None, None)
    next(hook)
    with pytest.raises(pytest.fail.Exception, match="BudgetEnvironmentError: unavailable"):
        next(hook)


def _session(exitstatus=pytest.ExitCode.OK):
    diagnostics = []
    reporter = SimpleNamespace(write_line=lambda line, **kwargs: diagnostics.append(line))
    return SimpleNamespace(exitstatus=exitstatus, config=SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda _: reporter))), diagnostics


def test_session_finish_recovers_pending_receipts_after_fixture_cleanup(entrypoints):
    plugin, backend = entrypoints
    receipt = Path("/registered/camctl-owned.json")
    class Directory:
        def glob(self, pattern):
            return [receipt] if pattern == "camctl-*.json" else []
    session, diagnostics = _session(pytest.ExitCode.INTERRUPTED)
    recovered = []
    backend.finalize_budget_receipts = lambda paths: recovered.extend(paths)
    hook = plugin.pytest_sessionfinish(session, session.exitstatus)
    next(hook)
    assert recovered == []
    # sessionfinish 中的 fixture 收场也可以登记最后一批待恢复收据。
    plugin._receipt_directories.add(Directory())

    with pytest.raises(StopIteration):
        next(hook)

    assert recovered == [receipt]
    assert session.exitstatus == pytest.ExitCode.INTERRUPTED
    assert diagnostics == []


def test_session_finish_does_not_repeat_completed_per_test_recovery(entrypoints):
    plugin, backend = entrypoints
    receipt = Path("/registered/camctl-owned.json")
    class Directory:
        def glob(self, pattern):
            return [receipt] if pattern == "camctl-*.json" else []
    directory = Directory()
    plugin._receipt_directories.add(directory)
    recovered = []
    backend.finalize_budget_receipts = lambda paths: recovered.extend(paths)
    teardown = plugin.pytest_runtest_teardown(None, None)
    next(teardown)
    with pytest.raises(StopIteration):
        next(teardown)
    # 其他 fixture 再登记同一目录时，也不重复核验已经处理的收据。
    plugin._receipt_directories.add(directory)
    session, _ = _session()
    finish = plugin.pytest_sessionfinish(session, session.exitstatus)
    next(finish)
    with pytest.raises(StopIteration):
        next(finish)

    assert recovered == [receipt]


@pytest.mark.parametrize("exitstatus", [pytest.ExitCode.OK, pytest.ExitCode.INTERRUPTED])
def test_session_finish_processes_every_receipt_and_accumulates_budget_and_proxy_errors(entrypoints, exitstatus):
    plugin, backend = entrypoints
    receipts = [Path("/registered/camctl-oom.json"), Path("/registered/camctl-cleanup.json"),
                Path("/registered/camctl-complete.json")]
    failure = SimpleNamespace(read_text=lambda *, encoding: "proxy launch unavailable")
    class Directory:
        def glob(self, pattern):
            return receipts if pattern == "camctl-*.json" else [failure]
    plugin._receipt_directories.add(Directory())
    recovered = []
    def finalize(paths):
        recovered.extend(paths)
        if receipts[0] in paths:
            raise backend.MemoryBudgetExceeded("child OOM")
        if receipts[1] in paths:
            raise backend.BudgetEnvironmentError("group cleanup unavailable")
    backend.finalize_budget_receipts = finalize
    session, diagnostics = _session(exitstatus)
    hook = plugin.pytest_sessionfinish(session, exitstatus)
    next(hook)

    with pytest.raises(StopIteration):
        next(hook)

    assert recovered == receipts
    assert session.exitstatus == (pytest.ExitCode.TESTS_FAILED if exitstatus == pytest.ExitCode.OK else exitstatus)
    assert any("child OOM" in line for line in diagnostics)
    assert any("group cleanup unavailable" in line for line in diagnostics)
    assert any("proxy launch unavailable" in line for line in diagnostics)


def test_session_finish_budget_failure_does_not_replace_fixture_cleanup_exception(entrypoints):
    plugin, backend = entrypoints
    class Directory:
        def glob(self, pattern):
            return [Path("/registered/camctl-owned.json")] if pattern == "camctl-*.json" else []
    plugin._receipt_directories.add(Directory())
    def exceeded(paths):
        raise backend.MemoryBudgetExceeded("child OOM")
    backend.finalize_budget_receipts = exceeded
    session, diagnostics = _session(pytest.ExitCode.INTERRUPTED)
    hook = plugin.pytest_sessionfinish(session, session.exitstatus)
    next(hook)
    original = RuntimeError("fixture cleanup failure")

    with pytest.raises(RuntimeError) as caught:
        hook.throw(original)

    assert caught.value is original
    assert session.exitstatus == pytest.ExitCode.INTERRUPTED
    assert any("child OOM" in line for line in diagnostics)


def test_host_entry_uses_exec_without_supervising_or_capturing_stdio(entrypoints, monkeypatch):
    plugin, backend = entrypoints
    class EnteredExec(BaseException):
        pass
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        raise EnteredExec
    backend.exec_budgeted = execute
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    with pytest.raises(EnteredExec):
        plugin.launcher_main(["--camctl-memory-limit", "123456B",
                              "--camctl-memory-cgroup-root", "/delegated",
                              "--receipt-directory", "/receipts", "--",
                              "/installed/launcher", "run", "--config", "/runtime/config.toml"])
    assert calls[0][0] == ["/installed/launcher", "run", "--config", "/runtime/config.toml"]
    assert set(calls[0][1]) == {"budget", "receipt_path"}
    assert calls[0][1]["budget"].cgroup_root == Path("/delegated")
    assert backend.requests == []
