"""启动协议与内核专项；各用例明确使用模拟接口还是真实资源组。"""

from contextlib import ExitStack
import os
import json
from pathlib import Path
import stat
import subprocess
import shutil
import sys

import pytest

import camctl_memory as memory
from camctl_memory_plugin import finalize_receipts, memory_launcher_command


def test_launcher_handshake_and_close_support_high_file_descriptors(tmp_path, monkeypatch):
    budget = _real_probe_budget()
    receipt = tmp_path / "handshake-receipt.json"
    environment = dict(os.environ)
    original_pipe = os.pipe
    handshake = []

    def pipe():
        pair = original_pipe()
        if not handshake:
            handshake.extend(pair)
            environment["CAMCTL_TEST_HANDSHAKE_FD"] = str(pair[1])
        return pair

    # pytest 本身也经常持有多位 FD；明确占住低位以稳定覆盖这一边界。
    with ExitStack() as stack:
        for _ in range(16):
            descriptor = os.open(os.devnull, os.O_RDONLY)
            stack.callback(os.close, descriptor)
        monkeypatch.setattr(os, "pipe", pipe)
        code = ("import json,os,sys; from pathlib import Path; "
                "r=json.loads(Path(sys.argv[1]).read_text()); "
                "assert str(os.getpid()) in (Path(r['group'])/'cgroup.procs').read_text().split(); "
                "fd=int(os.environ['CAMCTL_TEST_HANDSHAKE_FD']); "
                "print(os.getpid()); "
                "print(os.path.exists('/proc/self/fd/'+str(fd)))")
        with memory.BudgetedPopen(
                [sys.executable, "-c", code, str(receipt)], budget=budget, receipt_path=receipt,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment) as process:
            stdout, stderr = process.communicate(timeout=5)

    assert handshake[1] > 9
    assert process.returncode == 0, stderr
    assert stdout.splitlines() == [str(process.pid), "False"]
    final = json.loads(receipt.read_text(encoding="utf-8"))
    assert final["pid"] == process.pid and final["status"] == "completed"
    assert final["group_removed"] is True and not Path(final["group"]).exists()


def test_launcher_does_not_evaluate_bash_env_before_entering_group(tmp_path):
    budget = _real_probe_budget()
    receipt = tmp_path / "bash-env-receipt.json"
    marker = tmp_path / "unexpected-startup"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch '{marker}'\n", encoding="ascii")
    code = ("import json,os,sys; from pathlib import Path; "
            "r=json.loads(Path(sys.argv[1]).read_text()); "
            "assert str(os.getpid()) in (Path(r['group'])/'cgroup.procs').read_text().split(); "
            "print(os.getpid())")
    result = memory.run_budgeted(
        [sys.executable, "-c", code, str(receipt)], budget=budget, receipt_path=receipt,
        env={**os.environ, "BASH_ENV": str(startup)}, capture_output=True, text=True,
        timeout=5, check=True)

    assert not marker.exists()
    final = json.loads(receipt.read_text(encoding="utf-8"))
    assert result.stdout.strip() == str(final["pid"])
    assert final["status"] == "completed" and final["group_removed"] is True
    assert not Path(final["group"]).exists()


def test_exec_launcher_preserves_pid_process_group_stdio_and_cwd(tmp_path):
    receipt = tmp_path / "exec-receipt.json"
    budget = _real_probe_budget()
    memory.ensure_memory_budget_available(budget)
    code = r'''
import os, sys
from pathlib import Path
import camctl_memory as memory
cwd, receipt = map(Path, sys.argv[1:3])
delegated = Path(sys.argv[3]) if sys.argv[3] else None
business = """
import json, os, sys
from pathlib import Path
receipt = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
members = (Path(receipt['group']) / 'cgroup.procs').read_text(encoding='ascii').split()
assert str(os.getpid()) in members
print(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(), 'cwd': os.getcwd()}))
"""
budget = memory.BudgetConfig(memory.parse_memory_limit("64MiB"), delegated, "64MiB")
memory.exec_budgeted([sys.executable, "-c", business, str(receipt)], budget=budget,
    receipt_path=receipt, env=dict(os.environ), cwd=cwd)
'''
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(receipt),
             "" if budget.cgroup_root is None else str(budget.cgroup_root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(Path(memory.__file__).parent)})
        try:
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result == {"pid": process.pid, "pgid": process.pid, "cwd": str(tmp_path)}
        pending = json.loads(receipt.read_text(encoding="utf-8"))
        assert pending["status"] == "pending"
        assert pending["pid"] == process.pid
        if budget.cgroup_root is None:
            assert pending["configured_root"] is None
        else:
            assert Path(pending["group"]).parent == budget.cgroup_root
    finally:
        if receipt.exists():
            memory.finalize_budget_receipts((receipt,))
            final = json.loads(receipt.read_text(encoding="utf-8"))
            assert final["status"] == "recovered" and final["group_removed"] is True
            assert not Path(final["group"]).exists()


def test_final_receipt_is_ready_before_world_archive(tmp_path):
    command = memory_launcher_command(
        [sys.executable, "-c", "print('completed')"], receipt_root=tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "completed\n"
    receipt, = (tmp_path / "memory-budget").glob("camctl-*.json")
    assert json.loads(receipt.read_text())["status"] == "pending"

    finalize_receipts(tmp_path)
    archived = tmp_path / "archive"
    shutil.copytree(receipt.parent, archived)
    final = json.loads((archived / receipt.name).read_text())
    assert final["group_removed"] is True
    assert final["status"] == "recovered"
    assert final["evidence"]["combined_peak_bytes"] <= final["limit_bytes"]
    assert final["evidence"]["events"]["oom_kill"] == 0
    finalize_receipts(tmp_path)
    assert json.loads(receipt.read_text()) == final


def test_failed_group_entry_never_executes_the_business_command(tmp_path, monkeypatch):
    budget = _real_probe_budget()
    receipt = tmp_path / "failed-entry-receipt.json"
    marker = tmp_path / "business-started"
    real_popen = subprocess.Popen
    children, controls = [], []

    def deny_group_entry(*args, **kwargs):
        pending = json.loads(receipt.read_text(encoding="utf-8"))
        assert pending["status"] == "pending" and "baseline" in pending
        control = Path(pending["group"]) / "cgroup.procs"
        mode = stat.S_IMODE(control.stat().st_mode)
        controls.append((control, mode))
        control.chmod(mode & ~0o222)
        assert not stat.S_IMODE(control.stat().st_mode) & 0o222
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", deny_group_entry)
    try:
        with pytest.raises(memory.BudgetEnvironmentError):
            memory.run_budgeted(
                [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
                budget=budget, receipt_path=receipt, capture_output=True, text=True, timeout=5)
        assert len(children) == 1 and children[0].poll() is not None
        assert not marker.exists()
        final = json.loads(receipt.read_text(encoding="utf-8"))
        assert final["status"] == "environment_error" and final["group_removed"] is True
        assert not Path(final["group"]).exists()
    finally:
        try:
            for child in children:
                if child.poll() is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                child.communicate(timeout=5)
        finally:
            for control, mode in controls:
                if control.exists():
                    control.chmod(mode)


def _real_probe_budget():
    # 执行器专项的小预算；委派目录仍从统一配置取得。
    return memory.BudgetConfig(memory.parse_memory_limit("64MiB"),
        memory.get_budget_config().cgroup_root, "64MiB")


def test_real_cgroup_finishes_the_whole_process_tree(tmp_path):
    """需要真实委派控制器；缺少权限/接口时明确失败，不跳过。"""
    budget = _real_probe_budget()
    memory.ensure_memory_budget_available(budget)
    receipt = tmp_path / "normal-tree-receipt.json"
    grandchild = "import os; hold=bytearray(4*1024**2); print(os.getpid(), flush=True)"
    child = ("import os,subprocess,sys; hold=bytearray(4*1024**2); "
             "print(os.getpid(), flush=True); "
             "subprocess.run([sys.executable,'-c',sys.argv[1]],check=True)")
    parent = ("import os,subprocess,sys; hold=bytearray(4*1024**2); "
              "print(os.getpid(), flush=True); "
              "subprocess.run([sys.executable,'-c',sys.argv[1],sys.argv[2]],check=True); "
              "print('complete',flush=True)")

    result = memory.run_budgeted([sys.executable, "-c", parent, child, grandchild],
        budget=budget, receipt_path=receipt, capture_output=True, text=True, timeout=20, check=True)

    lines = result.stdout.splitlines()
    assert lines[-1] == "complete"
    assert len(lines) == 4
    assert len({int(value) for value in lines[:3]}) == 3
    evidence = json.loads(receipt.read_text(encoding="utf-8"))
    assert evidence["status"] == "completed"
    assert evidence["group_removed"] is True
    assert evidence["budget_scope"] == "memory_and_swap"
    assert evidence["evidence"]["combined_limit_bytes"] == budget.limit_bytes
    assert evidence["evidence"]["combined_peak_bytes"] <= budget.limit_bytes
    assert evidence["evidence"]["events"]["oom_kill"] == 0


def test_real_cgroup_detects_aggregate_child_oom_when_parent_returns_zero(tmp_path):
    """父24MiB、子48MiB各自低于64MiB，但合计须触发同组内存不足。"""
    budget = _real_probe_budget()
    memory.ensure_memory_budget_available(budget)
    receipt, business = tmp_path / "aggregate-oom-receipt.json", tmp_path / "parent-result.json"
    standalone = r'''
import sys
hold = bytearray(int(sys.argv[1]))
for offset in range(0, len(hold), 4096):
    hold[offset] = 1
print('complete', flush=True)
'''
    # 先核对两档分配各自能够在相同64MiB限额完成，排除单体本身超限。
    for retained_mib in (24, 48):
        result = memory.run_budgeted([sys.executable, "-c", standalone, str(retained_mib * 1024**2)],
            budget=budget, receipt_path=tmp_path / f"standalone-{retained_mib}-receipt.json",
            capture_output=True, text=True, timeout=20, check=True)
        assert result.stdout.strip() == "complete"
        standalone_evidence = json.loads((tmp_path / f"standalone-{retained_mib}-receipt.json").read_text(encoding="utf-8"))
        assert standalone_evidence["evidence"]["combined_peak_bytes"] <= budget.limit_bytes
    child = r'''
from pathlib import Path
Path('/proc/self/oom_score_adj').write_text('1000', encoding='ascii')
hold = bytearray(48 * 1024**2)
for offset in range(0, len(hold), 4096):
    hold[offset] = 1
'''
    parent = r'''
import json, os, subprocess, sys
from pathlib import Path
hold = bytearray(24 * 1024**2)
for offset in range(0, len(hold), 4096):
    hold[offset] = 1
child = subprocess.run([sys.executable, '-c', sys.argv[1]], capture_output=True, text=True, timeout=15)
Path(sys.argv[2]).write_text(json.dumps({'pid': os.getpid(), 'child_returncode': child.returncode,
    'child_stderr': child.stderr, 'parent_retained_bytes': len(hold)}), encoding='utf-8')
# 故意吞掉子进程退出码；预算执行器仍须从累计事件发现失败。
'''

    with pytest.raises(memory.MemoryBudgetExceeded):
        memory.run_budgeted([sys.executable, "-c", parent, child, str(business)],
            budget=budget, receipt_path=receipt, capture_output=True, text=True, timeout=20, check=True)

    result = json.loads(business.read_text(encoding="utf-8"))
    assert result["parent_retained_bytes"] == 24 * 1024**2
    assert result["child_returncode"] == -memory.signal.SIGKILL, result["child_stderr"]
    evidence = json.loads(receipt.read_text(encoding="utf-8"))
    assert evidence["returncode"] == 0
    assert evidence["status"] == "budget_exceeded"
    assert evidence["group_removed"] is True
    assert evidence["budget_scope"] == "memory_and_swap"
    assert evidence["evidence"]["combined_limit_bytes"] == budget.limit_bytes
    assert 0 < evidence["evidence"]["combined_peak_bytes"] <= budget.limit_bytes
    assert evidence["evidence"]["events"]["oom_kill"] > 0
