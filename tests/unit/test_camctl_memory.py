"""预算控制器契约测试；控制器文件和子进程均由内存替身提供。"""

from copy import deepcopy
from pathlib import Path
import json
import subprocess

import pytest

import camctl_memory as memory


@pytest.fixture(autouse=True)
def restore_budget_config(monkeypatch):
    monkeypatch.setattr(memory, "_exists", lambda _: False)
    monkeypatch.setattr(
        memory,
        "_CONFIG",
        memory.BudgetConfig(memory.parse_memory_limit(memory.DEFAULT_MEMORY_LIMIT)),
    )


@pytest.fixture
def budget():
    return memory.BudgetConfig(64, Path("/fake/cgroup"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1B", 1),
        ("1KiB", 1024),
        ("1MiB", 1024**2),
        ("1GiB", 1024**3),
        ("1KB", 1000),
        ("1MB", 1000**2),
        ("1GB", 1000**3),
        ("2GiB", 2 * 1024**3),
        ("2147483648B", 2 * 1024**3),
    ],
)
def test_parse_memory_limit_uses_exact_declared_units(value, expected):
    assert memory.parse_memory_limit(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "0B",
        "-1MiB",
        "1",
        "1Ki",
        "1mb",
        " 1GiB",
        "1GiB ",
        "1.5GiB",
        "1TiB",
        "",
        None,
    ],
)
def test_parse_memory_limit_rejects_unrecognized_or_nonpositive_values(value):
    with pytest.raises(ValueError):
        memory.parse_memory_limit(value)


def test_budget_configuration_preserves_the_exact_request_and_default():
    default = memory.get_budget_config()
    assert memory.DEFAULT_MEMORY_LIMIT == "2GiB"
    assert default.limit_bytes == 2 * 1024**3

    configured = memory.configure_budget("3MiB", Path("/fake/cgroup"))

    assert configured.requested_limit == "3MiB"
    assert configured.limit_bytes == 3 * 1024**2
    assert configured.cgroup_root == Path("/fake/cgroup")
    assert memory.get_budget_config() == configured


class FakeControllerFs:
    """用 Path 键保存文件内容，不访问真实文件系统。"""

    def __init__(self, version, *, swap_bytes=0, denied_write=None):
        self.root = Path("/fake/cgroup")
        self.version = version
        self.files = {}
        self.directories = {self.root}
        self.mkdir_calls = []
        self.rmdir_calls = []
        self.writes = []
        self.denied_write = denied_write

        if version == 2:
            self.files[self.root / "cgroup.controllers"] = "memory"
        elif version == 1:
            self.files[self.root / "memory.limit_in_bytes"] = "max"
        self.files[Path("/proc/meminfo")] = (
            f"MemTotal: 1048576 kB\nSwapTotal: {swap_bytes // 1024} kB\n"
        )

    def read(self, path):
        path = Path(path)
        if path not in self.files:
            raise FileNotFoundError(str(path))
        return self.files[path]

    def write(self, path, value):
        path = Path(path)
        if path.name == self.denied_write:
            raise PermissionError(f"fake permission denial for {path.name}")
        self.writes.append((path, value))
        self.files[path] = value

    def mkdir(self, path):
        path = Path(path)
        self.mkdir_calls.append(path)
        self.directories.add(path)
        self.files[path / "cgroup.procs"] = ""
        if self.version == 2:
            self.files.update(
                {
                    path / "memory.max": "max",
                    path / "memory.peak": "19",
                    path / "memory.swap.max": "max",
                    path / "memory.swap.current": "0",
                    path / "memory.swap.peak": "0",
                    path / "memory.events": (
                        "low 0\nhigh 0\nmax 0\noom 0\n"
                        "oom_kill 0\noom_group_kill 0"
                    ),
                }
            )
        elif self.version == 1:
            self.files.update(
                {
                    path / "memory.limit_in_bytes": str(2**63 - 1),
                    path / "memory.memsw.limit_in_bytes": str(2**63 - 1),
                    path / "memory.max_usage_in_bytes": "19",
                    path / "memory.memsw.max_usage_in_bytes": "23",
                    path / "memory.failcnt": "0",
                    path / "memory.memsw.failcnt": "0",
                    path / "memory.oom_control": "oom_kill 0\nunder_oom 0",
                    path / "memory.use_hierarchy": "1",
                }
            )

    def rmdir(self, path):
        path = Path(path)
        self.rmdir_calls.append(path)
        self.directories.remove(path)

    def install(self, monkeypatch):
        monkeypatch.setattr(memory, "_read", self.read)
        monkeypatch.setattr(memory, "_write", self.write)
        monkeypatch.setattr(memory, "_mkdir", self.mkdir)
        monkeypatch.setattr(memory, "_rmdir", self.rmdir)


def test_v2_group_applies_memory_and_swap_limits_and_reads_evidence(monkeypatch, budget):
    fs = FakeControllerFs(version=2)
    fs.install(monkeypatch)

    group = memory._new_group(budget)
    snapshot = group.snapshot()

    assert group.version == 2
    assert fs.files[group.path / "memory.max"] == "64"
    assert fs.files[group.path / "memory.swap.max"] == "0"
    assert snapshot["limit_bytes"] == 64
    assert snapshot["peak_bytes"] == 19
    assert snapshot["budget_scope"] == "memory_and_swap"
    assert snapshot["combined_limit_bytes"] == 64
    assert snapshot["combined_peak_bytes"] == 19
    assert snapshot["combined_peak_method"] == "memory.peak"
    assert snapshot["swap_peak_bytes"] == 0
    assert snapshot["events"]["oom"] == 0
    assert snapshot["events"]["oom_kill"] == 0

    group.remove()
    assert group.path in fs.rmdir_calls
    assert group.path not in fs.directories


def test_v1_group_applies_combined_limit_with_host_swap_enabled(monkeypatch, budget):
    fs = FakeControllerFs(version=1, swap_bytes=2 * 1024**2)
    fs.install(monkeypatch)

    group = memory._new_group(budget)

    assert group.version == 1
    assert fs.files[group.path / "memory.limit_in_bytes"] == "64"
    assert fs.files[group.path / "memory.memsw.limit_in_bytes"] == "64"
    snapshot = group.snapshot()
    assert snapshot["budget_scope"] == "memory_and_swap"
    assert snapshot["host_swap_bytes"] == 2 * 1024**2
    assert snapshot["combined_limit_bytes"] == 64
    assert snapshot["peak_bytes"] == 19
    assert snapshot["combined_peak_bytes"] == 23
    assert snapshot["combined_peak_method"] == "memory.memsw.max_usage_in_bytes"
    assert snapshot["swap_peak_bytes"] is None
    assert snapshot["events"]["oom_kill"] == 0
    assert snapshot["events"]["failcnt"] == 0
    assert snapshot["events"]["memsw_failcnt"] == 0
    group.remove()


@pytest.mark.parametrize("control", ["memory.memsw.limit_in_bytes", "memory.memsw.max_usage_in_bytes", "memory.memsw.failcnt"])
def test_v1_group_rejects_missing_combined_budget_interface(monkeypatch, budget, control):
    fs = FakeControllerFs(version=1)
    fs.install(monkeypatch)
    original_mkdir = fs.mkdir

    def mkdir(path):
        original_mkdir(path)
        del fs.files[path / control]

    monkeypatch.setattr(memory, "_mkdir", mkdir)

    with pytest.raises(memory.BudgetEnvironmentError):
        memory._new_group(budget)

    assert fs.rmdir_calls == fs.mkdir_calls


@pytest.mark.parametrize("control", ["memory.limit_in_bytes", "memory.memsw.limit_in_bytes"])
def test_v1_group_rejects_a_limit_that_does_not_accept_the_exact_budget(monkeypatch, budget, control):
    fs = FakeControllerFs(version=1)
    fs.install(monkeypatch)
    original_write = fs.write

    def write(path, value):
        original_write(path, "63" if path.name == control else value)

    monkeypatch.setattr(memory, "_write", write)

    with pytest.raises(memory.BudgetEnvironmentError):
        memory._new_group(budget)

    assert fs.rmdir_calls == fs.mkdir_calls


def test_group_creation_reports_permission_failure_and_removes_partial_leaf(monkeypatch, budget):
    fs = FakeControllerFs(version=2, denied_write="memory.swap.max")
    fs.install(monkeypatch)

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.ensure_memory_budget_available(budget)

    assert len(fs.mkdir_calls) == 1
    assert fs.rmdir_calls == fs.mkdir_calls
    assert fs.mkdir_calls[0] not in fs.directories


def test_preflight_releases_its_dedicated_group(monkeypatch, budget):
    fs = FakeControllerFs(version=2)
    fs.install(monkeypatch)

    memory.ensure_memory_budget_available(budget)

    assert len(fs.mkdir_calls) == 1
    assert fs.rmdir_calls == fs.mkdir_calls
    assert fs.mkdir_calls[0] not in fs.directories


def test_missing_controller_is_an_environment_error_without_fallback(monkeypatch, budget):
    fs = FakeControllerFs(version=None)
    fs.install(monkeypatch)

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.ensure_memory_budget_available(budget)

    assert fs.mkdir_calls == []


def _evidence(*, peak=0, combined_peak=None, oom=0, oom_kill=0, version=2, swap_peak=0):
    return {
        "budget_scope": "memory_and_swap",
        "limit_bytes": 64,
        "swap_limit_bytes": 0,
        "swap_current_bytes": 0,
        "peak_bytes": peak,
        "combined_limit_bytes": 64,
        "combined_peak_bytes": peak if combined_peak is None else combined_peak,
        "combined_peak_method": "memory.peak" if version == 2 else "memory.memsw.max_usage_in_bytes",
        "swap_peak_bytes": swap_peak,
        "events": {"oom": oom, "oom_kill": oom_kill, "oom_group_kill": 0, "failcnt": 0, "memsw_failcnt": 0},
    }


class FakeGroup:
    def __init__(self, *, final_evidence=None, live_pids=(), pid_sequence=(), version=2):
        self.path = Path("/fake/cgroup/camctl-memory-10-" + "a" * 32)
        self.version = version
        self._baseline = _evidence(version=version)
        self._final = deepcopy(final_evidence if final_evidence is not None else self._baseline)
        self.live_pids = set(live_pids)
        self.pid_sequence = list(pid_sequence)
        self.terminated_pids = []
        self.snapshot_calls = 0
        self.removed = False

    def snapshot(self):
        self.snapshot_calls += 1
        return deepcopy(self._baseline if self.snapshot_calls == 1 else self._final)

    def pids(self):
        if self.pid_sequence:
            return list(self.pid_sequence.pop(0))
        return sorted(self.live_pids)

    def terminate(self):
        self.terminated_pids = sorted(self.live_pids)
        self.live_pids.clear()

    def remove(self):
        if self.live_pids:
            raise memory.BudgetEnvironmentError("fake group still has processes")
        self.removed = True


class FakePopen:
    def __init__(self, *, exit_code=0, timeout_first_wait=False, stdout=b"ok", stderr=b""):
        self.pid = 4321
        self.returncode = None
        self.exit_code = exit_code
        self.timeout_first_wait = timeout_first_wait
        self.stdout_value = stdout
        self.stderr_value = stderr
        self.wait_timeouts = []
        self.communicate_calls = []
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.signals = []

    def send_signal(self, sig):
        self.signals.append((self.pid, sig))

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.timeout_first_wait:
            self.timeout_first_wait = False
            raise subprocess.TimeoutExpired(["fake-child"], timeout)
        self.returncode = self.exit_code
        return self.returncode

    def poll(self):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.communicate_calls.append((input, timeout))
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.stdout_value, self.stderr_value


@pytest.fixture
def receipt_store(monkeypatch):
    receipts = {}

    def store(path, data):
        receipts[Path(path)] = deepcopy(data)

    monkeypatch.setattr(memory, "_write_receipt", store)
    return receipts


@pytest.fixture
def receipt_files(monkeypatch):
    """只在内存映射中保留 JSON，不创建目录或实际文件。"""
    files = {}

    def read(path, *args, **kwargs):
        try:
            return files[Path(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    def write(path, data):
        files[Path(path)] = json.dumps(data)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(memory, "_write_receipt", write)
    monkeypatch.setattr(memory, "_exists", lambda path: Path(path) in files)
    return files


def install_fake_runner(monkeypatch, group, process):
    started = []

    def start(command, assigned_group, kwargs):
        assert assigned_group is group
        started.append((command, dict(kwargs)))
        return process

    monkeypatch.setattr(memory, "_new_group", lambda config: group)
    monkeypatch.setattr(memory, "_start_process", start)
    return started


def test_budgeted_popen_refuses_to_start_without_a_configured_group(
    monkeypatch, budget, receipt_store
):
    started = []

    def unavailable(config):
        raise memory.BudgetEnvironmentError("fake controller unavailable")

    monkeypatch.setattr(memory, "_new_group", unavailable)
    monkeypatch.setattr(
        memory,
        "_start_process",
        lambda *args: started.append(args),
    )

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.BudgetedPopen(
            ["fake-child"], budget=budget, receipt_path=Path("/receipt.json")
        )

    assert started == []
    assert receipt_store[Path("/receipt.json")]["status"] == "environment_error"


def test_budgeted_popen_rejects_peak_over_limit_and_preserves_receipt(
    monkeypatch, budget, receipt_store
):
    group = FakeGroup(final_evidence=_evidence(peak=65))
    process = FakePopen(exit_code=0)
    install_fake_runner(monkeypatch, group, process)
    receipt_path = Path("/receipt.json")
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=receipt_path)

    with pytest.raises(memory.MemoryBudgetExceeded):
        wrapped.wait()

    receipt = receipt_store[receipt_path]
    assert receipt["status"] == "budget_exceeded"
    assert receipt["evidence"]["peak_bytes"] == 65
    assert receipt["evidence"]["limit_bytes"] == 64
    assert receipt["group_removed"] is True
    assert group.removed


def test_v1_run_accepts_swap_and_reclaim_events_with_combined_peak_within_budget(
    monkeypatch, budget, receipt_store
):
    final = _evidence(peak=48, combined_peak=64, version=1, swap_peak=32)
    final["host_swap_bytes"] = 2 * 1024**3
    final["events"].update(failcnt=5, memsw_failcnt=3)
    group, process = FakeGroup(final_evidence=final, version=1), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    path = Path("/receipt.json")

    assert memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=path).wait() == 0

    receipt = receipt_store[path]
    assert receipt["budget_scope"] == "memory_and_swap"
    assert receipt["status"] == "completed"
    assert receipt["evidence"]["combined_peak_bytes"] == 64
    assert receipt["evidence"]["swap_peak_bytes"] == 32


def test_v1_run_rejects_combined_peak_over_budget_even_when_memory_peak_is_lower(
    monkeypatch, budget, receipt_store
):
    group = FakeGroup(final_evidence=_evidence(peak=32, combined_peak=65, version=1), version=1)
    install_fake_runner(monkeypatch, group, FakePopen())
    path = Path("/receipt.json")

    with pytest.raises(memory.MemoryBudgetExceeded):
        memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=path).wait()

    assert receipt_store[path]["status"] == "budget_exceeded"
    assert receipt_store[path]["evidence"]["peak_bytes"] == 32
    assert receipt_store[path]["evidence"]["combined_peak_bytes"] == 65


@pytest.mark.parametrize("field", ["limit_bytes", "combined_limit_bytes"])
def test_run_rejects_actual_memory_or_combined_limit_drift(monkeypatch, budget, receipt_store, field):
    final = _evidence(version=1)
    final[field] = 65
    group = FakeGroup(final_evidence=final)
    install_fake_runner(monkeypatch, group, FakePopen())
    path = Path("/receipt.json")

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=path).wait()

    assert receipt_store[path]["status"] == "environment_error"


@pytest.mark.parametrize("field", ["budget_scope", "combined_limit_bytes", "combined_peak_bytes", "combined_peak_method"])
def test_run_rejects_missing_combined_budget_evidence(monkeypatch, budget, receipt_store, field):
    final = _evidence()
    del final[field]
    group = FakeGroup(final_evidence=final)
    install_fake_runner(monkeypatch, group, FakePopen())

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json")).wait()


@pytest.mark.parametrize("field", ["swap_limit_bytes", "swap_current_bytes", "swap_peak_bytes"])
def test_v2_run_rejects_swap_constraint_drift(monkeypatch, budget, receipt_store, field):
    final = _evidence()
    final[field] = 1
    group = FakeGroup(final_evidence=final)
    install_fake_runner(monkeypatch, group, FakePopen())

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json")).wait()


@pytest.mark.parametrize("version", [1, 2])
def test_short_lived_child_oom_fails_even_when_parent_returns_zero(
    monkeypatch, budget, receipt_store, version
):
    group = FakeGroup(final_evidence=_evidence(oom=1, oom_kill=1, version=version), version=version)
    process = FakePopen(exit_code=0)
    install_fake_runner(monkeypatch, group, process)
    receipt_path = Path("/receipt.json")
    wrapped = memory.BudgetedPopen(["fake-parent"], budget=budget, receipt_path=receipt_path)

    with pytest.raises(memory.MemoryBudgetExceeded):
        wrapped.wait()

    receipt = receipt_store[receipt_path]
    assert process.returncode == 0
    assert receipt["status"] == "budget_exceeded"
    assert receipt["evidence"]["events"]["oom_kill"] == 1
    assert group.removed


def test_wait_tracks_child_after_parent_exit_until_the_group_is_empty(
    monkeypatch, budget, receipt_store
):
    group = FakeGroup(pid_sequence=([7654], []))
    process = FakePopen(exit_code=0)
    install_fake_runner(monkeypatch, group, process)
    monkeypatch.setattr(memory.time, "sleep", lambda _: None)
    wrapped = memory.BudgetedPopen(
        ["fake-parent"], budget=budget, receipt_path=Path("/receipt.json")
    )

    assert wrapped.wait() == 0

    assert group.removed
    assert receipt_store[Path("/receipt.json")]["status"] == "completed"


def test_timeout_kills_the_whole_group_and_keeps_final_evidence(
    monkeypatch, budget, receipt_store
):
    group = FakeGroup(live_pids=(7654, 7655))
    process = FakePopen(exit_code=-9, timeout_first_wait=True)
    install_fake_runner(monkeypatch, group, process)
    receipt_path = Path("/receipt.json")
    wrapped = memory.BudgetedPopen(["fake-parent"], budget=budget, receipt_path=receipt_path)

    with pytest.raises(subprocess.TimeoutExpired):
        wrapped.wait(timeout=0)

    receipt = receipt_store[receipt_path]
    assert group.terminated_pids == [7654, 7655]
    assert group.removed
    assert receipt["status"] == "timeout"
    assert receipt["group_removed"] is True
    assert receipt["evidence"]["limit_bytes"] == 64
    assert process.wait_timeouts[0] == 0
    assert 0 < process.wait_timeouts[1] <= 5


def test_run_budgeted_returns_completed_process_without_real_child(
    monkeypatch, budget, receipt_store
):
    group = FakeGroup()
    process = FakePopen(stdout=b"output", stderr=b"diagnostic")
    started = install_fake_runner(monkeypatch, group, process)

    result = memory.run_budgeted(
        ["fake-child"],
        budget=budget,
        receipt_path=Path("/receipt.json"),
        capture_output=True,
        check=True,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        b"output",
        b"diagnostic",
    )
    assert started[0][1]["stdout"] == subprocess.PIPE
    assert started[0][1]["stderr"] == subprocess.PIPE
    assert group.removed
    assert receipt_store[Path("/receipt.json")]["status"] == "completed"


def test_pending_receipt_is_saved_before_the_business_start(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(), FakePopen()
    monkeypatch.setattr(memory, "_new_group", lambda _: group)
    path = Path("/receipt.json")

    def start(*args):
        pending = receipt_store[path]
        assert pending["status"] == "pending"
        assert pending["group"] == str(group.path)
        assert pending["baseline"]["events"]["oom_kill"] == 0
        return process

    monkeypatch.setattr(memory, "_start_process", start)
    memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=path).wait()


def test_context_base_exception_cleans_descendants_and_keeps_receipt(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(live_pids=(7654, 7655)), FakePopen()
    install_fake_runner(monkeypatch, group, process)

    with pytest.raises(KeyboardInterrupt):
        with memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json")):
            raise KeyboardInterrupt()

    assert group.terminated_pids == [7654, 7655]
    assert receipt_store[Path("/receipt.json")]["status"] == "interrupted"
    assert group.removed


def test_v2_termination_writes_write_only_cgroup_kill_without_reading_it(monkeypatch):
    path = Path("/fake/cgroup/owned")
    live = [111, 222]
    writes = []

    def read(control):
        assert control.name != "cgroup.kill", "write-only control must not be read"
        return "\n".join(map(str, live))

    def write(control, value):
        writes.append((control, value))
        live.clear()

    monkeypatch.setattr(memory, "_read", read)
    monkeypatch.setattr(memory, "_write", write)
    monkeypatch.setattr(memory, "_exists", lambda control: control == path / "cgroup.kill")
    memory._Group(path, 2, 64).terminate()

    assert writes == [(path / "cgroup.kill", "1")]


@pytest.mark.parametrize("oom", [0, 1])
def test_outer_runner_recovers_pending_receipt_after_launcher_disappears(receipt_files, monkeypatch, oom):
    group = FakeGroup(live_pids=(7654,), final_evidence=_evidence(oom_kill=oom))
    # 恢复对象没有初始化时的 snapshot；下一次读取就是最终证据。
    group.snapshot_calls = 1
    path = Path("/fake/receipts/pending.json")
    receipt_files[path] = json.dumps({"schema": 1, "status": "pending", "limit_bytes": 64,
        "group": str(group.path), "owned_root": str(group.path.parent),
        "controller": "cgroup-v2", "baseline": _evidence()})
    monkeypatch.setattr(memory, "_Group", lambda *args: group)
    monkeypatch.setattr(memory, "_exists", lambda _: True)

    if oom:
        with pytest.raises(memory.MemoryBudgetExceeded):
            memory.finalize_budget_receipts([path])
    else:
        memory.finalize_budget_receipts([path])

    receipt = json.loads(receipt_files[path])
    assert group.terminated_pids == [7654]
    assert receipt["group_removed"] is True
    assert receipt["evidence"]["events"]["oom_kill"] == oom
    assert receipt["status"] == ("budget_exceeded" if oom else "recovered")


def test_outer_runner_rechecks_completed_child_oom_and_processes_other_receipts(receipt_files):
    paths = [Path("/fake/receipts/bad.json"), Path("/fake/receipts/good.json")]
    for path, oom in zip(paths, (1, 0)):
        receipt_files[path] = json.dumps({"schema": 1, "status": "completed", "limit_bytes": 64,
            "evidence": _evidence(oom_kill=oom)})

    with pytest.raises(memory.MemoryBudgetExceeded):
        memory.finalize_budget_receipts(paths)

    assert json.loads(receipt_files[paths[0]])["status"] == "budget_exceeded"
    assert json.loads(receipt_files[paths[1]])["status"] == "completed"


@pytest.mark.parametrize("status", ["completed", "finalizing"])
def test_outer_runner_rechecks_combined_peak_after_the_group_is_removed(receipt_files, status):
    path = Path("/fake/receipts/combined-peak.json")
    receipt_files[path] = json.dumps({"schema": 1, "budget_scope": "memory_and_swap",
        "status": status, "final_status": "completed", "limit_bytes": 64,
        "group": "/fake/cgroup/camctl-memory-1-" + "a" * 32,
        "owned_root": "/fake/cgroup", "controller": "cgroup-v1", "group_removed": True,
        "evidence": _evidence(peak=32, combined_peak=65, version=1)})

    with pytest.raises(memory.MemoryBudgetExceeded):
        memory.finalize_budget_receipts([path])

    receipt = json.loads(receipt_files[path])
    assert receipt["status"] == "budget_exceeded"
    assert receipt["evidence"]["combined_peak_bytes"] == 65


@pytest.mark.parametrize("field", ["budget_scope", "combined_limit_bytes", "combined_peak_bytes", "combined_peak_method"])
def test_outer_runner_rejects_a_completed_receipt_without_combined_evidence(receipt_files, field):
    path = Path("/fake/receipts/incomplete.json")
    evidence = _evidence()
    del evidence[field]
    receipt_files[path] = json.dumps({"schema": 1, "status": "completed", "limit_bytes": 64,
        "group_removed": True, "evidence": evidence})

    with pytest.raises(memory.BudgetEnvironmentError):
        memory.finalize_budget_receipts([path])

    assert json.loads(receipt_files[path])["status"] == "environment_error"


def test_outer_runner_accepts_v1_swap_and_reclaim_counts_with_combined_peak_at_budget(receipt_files):
    path = Path("/fake/receipts/swap.json")
    evidence = _evidence(peak=48, combined_peak=64, version=1, swap_peak=32)
    evidence["host_swap_bytes"] = 2 * 1024**3
    evidence["events"].update(failcnt=5, memsw_failcnt=3)
    receipt_files[path] = json.dumps({"schema": 1, "status": "completed", "limit_bytes": 64,
        "group_removed": True, "evidence": evidence})

    memory.finalize_budget_receipts([path])

    assert json.loads(receipt_files[path])["status"] == "completed"


def test_run_budgeted_keeps_inherited_stdio_and_business_exit_semantics(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(), FakePopen(exit_code=7, stdout=None, stderr=None)
    started = install_fake_runner(monkeypatch, group, process)
    result = memory.run_budgeted(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))

    assert result.returncode == 7
    assert "stdin" not in started[0][1]
    assert "stdout" not in started[0][1]
    assert "stderr" not in started[0][1]


def test_final_evidence_is_persisted_before_the_group_is_removed(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    path = Path("/receipt.json")
    original_remove = group.remove

    def remove():
        assert receipt_store[path]["status"] == "finalizing"
        assert receipt_store[path]["evidence"]["limit_bytes"] == 64
        original_remove()

    monkeypatch.setattr(group, "remove", remove)
    memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=path).wait()


def test_outer_runner_recovers_evidence_when_group_was_removed_before_final_receipt(receipt_files):
    path = Path("/fake/receipts/finalizing.json")
    receipt_files[path] = json.dumps({"schema": 1, "status": "finalizing", "final_status": "completed",
        "limit_bytes": 64, "group": "/fake/cgroup/camctl-memory-1-" + "a" * 32,
        "owned_root": "/fake/cgroup", "controller": "cgroup-v2", "evidence": _evidence()})

    memory.finalize_budget_receipts([path])

    data = json.loads(receipt_files[path])
    assert data["status"] == "completed"
    assert data["group_removed"] is True


def test_terminate_only_signals_main_and_leftover_descendants_fail_at_caller_timeout(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(live_pids=(7654, 7655)), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1))
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    wrapped.terminate()

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        wrapped.wait(timeout=30)

    assert caught.value.timeout == 30
    assert clock[0] == 30
    assert process.signals == [(process.pid, memory.signal.SIGTERM)]
    assert group.terminated_pids == [7654, 7655]
    assert receipt_store[Path("/receipt.json")]["status"] == "timeout"


def test_exec_failure_keeps_owned_pending_group_for_outer_cleanup(monkeypatch, budget, receipt_store):
    group = FakeGroup(live_pids=(123,))
    monkeypatch.setattr(memory, "_new_group", lambda _: group)
    monkeypatch.setattr(memory.os, "getpid", lambda: 123)
    writes = []
    monkeypatch.setattr(memory, "_write", lambda path, value: writes.append((path, value)))

    def unavailable(*args):
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr(memory.os, "execvpe", unavailable)
    with pytest.raises(memory.BudgetEnvironmentError):
        memory.exec_budgeted(["missing"], budget=budget, receipt_path=Path("/receipt.json"))

    assert writes == [(group.path / "cgroup.procs", "123")]
    assert receipt_store[Path("/receipt.json")]["status"] == "pending"
    assert "missing executable" in receipt_store[Path("/receipt.json")]["launch_error"]
    assert not group.removed


def test_group_name_collision_never_removes_an_existing_group(monkeypatch, budget):
    fs = FakeControllerFs(version=2)
    fs.install(monkeypatch)

    def collision(path):
        raise FileExistsError(str(path))

    monkeypatch.setattr(memory, "_mkdir", collision)
    with pytest.raises(memory.BudgetEnvironmentError):
        memory._new_group(budget)

    assert fs.rmdir_calls == []


def test_communicate_after_terminate_propagates_caller_timeout(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(live_pids=(7654,)), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    wrapped.terminate()
    calls = []

    def communicate(input=None, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(["fake-child"], timeout)
        return b"output", b""

    monkeypatch.setattr(process, "communicate", communicate)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        wrapped.communicate(timeout=30)
    assert caught.value.timeout == 30
    assert calls == [30]
    assert group.removed
    assert receipt_store[Path("/receipt.json")]["status"] == "timeout"


@pytest.mark.parametrize("operation", ["wait", "communicate"])
@pytest.mark.parametrize("timeout", [None, 30])
def test_terminate_preserves_caller_thirty_second_window_for_six_second_exit(
    monkeypatch, budget, receipt_store, operation, timeout
):
    group, process = FakeGroup(live_pids=(7654,)), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    wrapped.terminate()

    def complete(timeout=None):
        assert timeout is None or timeout >= 6
        clock[0] = 6.0
        group.live_pids.clear()
        process.returncode = 0
        return 0

    def communicate(input=None, timeout=None):
        complete(timeout)
        return b"output", b""

    monkeypatch.setattr(process, "wait", complete)
    monkeypatch.setattr(process, "communicate", communicate)
    if operation == "wait":
        assert wrapped.wait(timeout=timeout) == 0
    else:
        assert wrapped.communicate(timeout=timeout) == (b"output", b"")

    assert process.signals == [(process.pid, memory.signal.SIGTERM)]
    assert group.terminated_pids == []
    assert receipt_store[Path("/receipt.json")]["status"] == "completed"


@pytest.mark.parametrize("main_exited", [False, True])
def test_poll_after_terminate_does_not_clean_a_live_instance_at_six_seconds(monkeypatch, budget, receipt_store, main_exited):
    group, process = FakeGroup(live_pids=(7654,)), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    wrapped.terminate()
    clock[0] = 6.0
    if main_exited:
        process.returncode = 0

    assert wrapped.poll() is None
    assert group.terminated_pids == []
    assert group.live_pids == {7654}
    assert receipt_store[Path("/receipt.json")]["status"] == "pending"


def test_caller_timeout_reports_group_cleanup_failure_and_preserves_receipt(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(live_pids=(7654,)), FakePopen(timeout_first_wait=True)
    install_fake_runner(monkeypatch, group, process)
    monkeypatch.setattr(memory.time, "monotonic", lambda: 0)
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    wrapped.terminate()

    def failed_cleanup():
        raise memory.BudgetEnvironmentError("fake group did not empty")

    monkeypatch.setattr(group, "terminate", failed_cleanup)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        wrapped.wait(timeout=30)

    assert isinstance(caught.value.__cause__, memory.BudgetEnvironmentError)
    assert "fake group did not empty" in str(caught.value.__cause__)
    assert process.wait_timeouts == [30]
    assert receipt_store[Path("/receipt.json")]["status"] == "environment_error"
    assert receipt_store[Path("/receipt.json")]["group_removed"] is False


@pytest.mark.parametrize("operation", ["wait", "communicate"])
def test_original_timeout_is_preserved_when_final_evidence_exceeds_budget(
    monkeypatch, budget, receipt_store, operation
):
    group, process = FakeGroup(final_evidence=_evidence(peak=65)), FakePopen()
    install_fake_runner(monkeypatch, group, process)
    monkeypatch.setattr(memory.time, "monotonic", lambda: 0)
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    original = subprocess.TimeoutExpired(["fake-child"], 30)
    calls = []
    original_wait = process.wait

    def timed_out(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise original
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(process, operation, timed_out)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        getattr(wrapped, operation)(timeout=30)

    assert caught.value is original
    assert isinstance(caught.value.__cause__, memory.MemoryBudgetExceeded)
    assert receipt_store[Path("/receipt.json")]["status"] == "budget_exceeded"
    assert group.removed


def test_group_cleanup_and_main_reaping_share_the_five_second_cleanup_window(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(live_pids=(7654,)), FakePopen(timeout_first_wait=True)
    install_fake_runner(monkeypatch, group, process)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    original_terminate = group.terminate

    def terminate():
        clock[0] += 4
        original_terminate()

    monkeypatch.setattr(group, "terminate", terminate)
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))
    with pytest.raises(subprocess.TimeoutExpired):
        wrapped.wait(timeout=30)

    assert process.wait_timeouts == [30, 1]
    assert group.removed


def test_explicit_kill_keeps_main_signal_semantics_and_defers_descendant_cleanup(monkeypatch, budget, receipt_store):
    group, process = FakeGroup(live_pids=(7654,)), FakePopen(exit_code=-9)
    install_fake_runner(monkeypatch, group, process)
    wrapped = memory.BudgetedPopen(["fake-child"], budget=budget, receipt_path=Path("/receipt.json"))

    wrapped.kill()
    assert process.signals == [(process.pid, memory.signal.SIGKILL)]
    assert group.live_pids == {7654}
    assert wrapped.wait() == -9
    assert not group.removed
    assert receipt_store[Path("/receipt.json")]["termination_mode"] == "explicit_sigkill"
    assert receipt_store[Path("/receipt.json")]["status"] == "pending"

    wrapped.__exit__(None, None, None)
    assert group.terminated_pids == [7654]
    assert group.removed


def test_outer_recovery_retains_a_previously_recorded_environment_error(receipt_files, monkeypatch):
    group = FakeGroup()
    group.snapshot_calls = 1
    path = Path("/fake/receipts/failed-cleanup.json")
    receipt_files[path] = json.dumps({"schema": 1, "status": "environment_error", "limit_bytes": 64,
        "group": str(group.path), "owned_root": str(group.path.parent), "controller": "cgroup-v2",
        "error": "original cleanup failed", "group_removed": False})
    monkeypatch.setattr(memory, "_Group", lambda *args: group)
    monkeypatch.setattr(memory, "_exists", lambda _: True)

    with pytest.raises(memory.BudgetEnvironmentError, match="original cleanup failed"):
        memory.finalize_budget_receipts([path])

    assert group.removed
    assert json.loads(receipt_files[path])["status"] == "environment_error"


@pytest.mark.parametrize("runner", [memory.BudgetedPopen, memory.exec_budgeted])
def test_new_instance_does_not_overwrite_a_previous_receipt(receipt_files, monkeypatch, budget, runner):
    path = Path("/fake/receipts/previous.json")
    receipt_files[path] = "previous evidence"
    groups = []
    monkeypatch.setattr(memory, "_new_group", lambda config: groups.append(config))

    with pytest.raises(memory.BudgetEnvironmentError):
        runner(["fake-child"], budget=budget, receipt_path=path)

    assert receipt_files[path] == "previous evidence"
    assert groups == []


def test_exec_baseline_read_failure_keeps_group_identity_for_outer_recovery(receipt_files, monkeypatch, budget):
    group = FakeGroup()
    monkeypatch.setattr(memory, "_new_group", lambda _: group)
    calls = []

    def snapshot():
        calls.append(None)
        if len(calls) == 1:
            raise OSError("transient baseline read failure")
        return _evidence()

    monkeypatch.setattr(group, "snapshot", snapshot)
    path = Path("/fake/receipts/exec-baseline-error.json")
    with pytest.raises(memory.BudgetEnvironmentError):
        memory.exec_budgeted(["fake-child"], budget=budget, receipt_path=path)

    pending = json.loads(receipt_files[path])
    assert pending["group"] == str(group.path)
    assert pending["controller"] == "cgroup-v2"
    assert pending["owned_root"] == str(group.path.parent)
    monkeypatch.setattr(memory, "_Group", lambda *args: group)
    monkeypatch.setattr(memory, "_exists", lambda _: True)
    with pytest.raises(memory.BudgetEnvironmentError, match="transient baseline read failure"):
        memory.finalize_budget_receipts([path])

    assert group.removed


@pytest.mark.parametrize("mode", ["graceful_signal", "explicit_sigkill"])
def test_outer_recovery_distinguishes_graceful_leaks_from_explicit_main_kill(receipt_files, monkeypatch, mode):
    group = FakeGroup(live_pids=(7654,))
    group.snapshot_calls = 1
    path = Path("/fake/receipts/interrupted.json")
    receipt_files[path] = json.dumps({"schema": 1, "status": "pending", "limit_bytes": 64,
        "group": str(group.path), "owned_root": str(group.path.parent), "controller": "cgroup-v2",
        "termination_mode": mode})
    monkeypatch.setattr(memory, "_Group", lambda *args: group)
    monkeypatch.setattr(memory, "_exists", lambda _: True)

    if mode == "graceful_signal":
        with pytest.raises(memory.BudgetEnvironmentError):
            memory.finalize_budget_receipts([path])
    else:
        memory.finalize_budget_receipts([path])

    assert group.removed
    assert json.loads(receipt_files[path])["status"] == ("environment_error" if mode == "graceful_signal" else "recovered")
