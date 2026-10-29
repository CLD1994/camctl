"""启动协议与内核专项；各用例明确使用模拟接口还是真实资源组。"""

from contextlib import ExitStack
import os
import json
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace

import pytest

import camctl_memory as memory
from camctl_memory_plugin import finalize_receipts, memory_launcher_command


def test_launcher_handshake_and_close_support_high_file_descriptors(tmp_path, monkeypatch):
    control = tmp_path / "cgroup.procs"
    control.write_text("", encoding="ascii")
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
        monkeypatch.setattr(memory.os, "pipe", pipe)
        code = ("import os; fd=int(os.environ['CAMCTL_TEST_HANDSHAKE_FD']); "
                "print(os.getpid()); "
                "print(os.path.exists('/proc/self/fd/'+str(fd)))")
        process = memory._start_process(
            [sys.executable, "-c", code], SimpleNamespace(path=tmp_path),
            {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
             "env": environment},
        )
        stdout, stderr = process.communicate(timeout=5)

    assert handshake[1] > 9
    assert process.returncode == 0, stderr
    assert stdout.splitlines() == [str(process.pid), "False"]
    assert control.read_text(encoding="ascii").strip() == str(process.pid)


def test_launcher_does_not_evaluate_bash_env_before_entering_group(tmp_path):
    (tmp_path / "cgroup.procs").write_text("", encoding="ascii")
    marker = tmp_path / "unexpected-startup"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch '{marker}'\n", encoding="ascii")
    process = memory._start_process(
        [sys.executable, "-c", "pass"], SimpleNamespace(path=tmp_path),
        {"env": {**os.environ, "BASH_ENV": str(startup)}},
    )

    assert process.wait(timeout=5) == 0
    assert not marker.exists()


def test_exec_launcher_preserves_pid_process_group_stdio_and_cwd(tmp_path):
    control = tmp_path / "cgroup.procs"
    control.write_text("", encoding="ascii")
    receipt = tmp_path / "exec-receipt.json"
    # 仅将 _new_group 替换为普通文件接口；os.execvpe 和 PID/PGID 均真实执行。
    code = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
import camctl_memory as memory
root, receipt = map(Path, sys.argv[1:])
evidence = {"budget_scope": "memory_and_swap", "limit_bytes": 4096,
    "combined_limit_bytes": 4096, "combined_peak_bytes": 0, "combined_peak_method": "memory.peak",
    "peak_bytes": 0, "swap_limit_bytes": 0, "swap_current_bytes": 0, "swap_peak_bytes": 0,
    "events": {"oom": 0, "oom_kill": 0}}
group = SimpleNamespace(path=root, version=2, snapshot=lambda: evidence,
    pids=lambda: [int(value) for value in (root / "cgroup.procs").read_text().split()])
memory._new_group = lambda config: group
business = "import json,os; print(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'cwd':os.getcwd()}))"
memory.exec_budgeted([sys.executable, "-c", business], budget=memory.BudgetConfig(4096),
    receipt_path=receipt, env=dict(os.environ), cwd=root)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(receipt)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(memory.__file__).parent)})
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result == {"pid": process.pid, "pgid": process.pid, "cwd": str(tmp_path)}
    pending = json.loads(receipt.read_text(encoding="utf-8"))
    assert pending["status"] == "pending"
    assert pending["pid"] == process.pid
    assert control.read_text(encoding="ascii").strip() == str(process.pid)


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


def test_failed_group_entry_never_executes_the_business_command(tmp_path):
    marker = tmp_path / "business-started"
    group = SimpleNamespace(path=tmp_path / "missing-group")
    try:
        memory._start_process(
            [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
            group, {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE},
        )
    except memory.BudgetEnvironmentError:
        pass
    else:
        raise AssertionError("failed entry must reject the launch")

    assert not marker.exists()


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
