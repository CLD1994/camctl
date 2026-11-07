"""预算委派入口的真实文件与进程测试；受控控制文件不代表内核预算验收。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("run_memory_budget", SCRIPTS / "run-memory-budget.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def controls(path: Path, *, peak: int = 1, oom: int = 0) -> None:
    path.mkdir()
    values = {"cgroup.procs": "", "memory.max": "2147483648", "memory.peak": str(peak),
              "memory.swap.max": "0", "memory.swap.current": "0",
              "memory.events": f"oom 0\noom_kill {oom}", "cgroup.subtree_control": "memory"}
    for name, value in values.items():
        (path / name).write_text(value, encoding="ascii")


def remove_controls(path: Path) -> None:
    for value in path.iterdir():
        value.unlink()
    path.rmdir()


class BudgetLauncherTest(unittest.TestCase):
    def fixture(self, root: Path, *, delegate_error=False, remove_error=False):
        parent = root / "parent"
        parent.mkdir()
        sentinel = parent / "sentinel"
        sentinel.write_text("sibling")
        calls = []

        def privileged(action, path, uid, gid, identity=None):
            calls.append((action, path, uid, gid, identity))
            if action == "--_delegate":
                if delegate_error:
                    return {"ok": False, "error": "controlled sudo failure", "exit_code": 1}
                controls(path)
                (path / "memory.max").write_text("max")
                (path / "memory.swap.max").write_text("max")
                return {"ok": True, "identity": launcher._identity(path), "version": 2,
                        "exit_code": 0}
            self.assertEqual(launcher._identity(path), identity)
            if remove_error:
                return {"ok": False, "error": "controlled rmdir failure", "exit_code": 1}
            remove_controls(path)
            return {"ok": True, "exit_code": 0}

        return parent, sentinel, calls, privileged

    def run_command(self, root, parent, privileged, body, *arguments):
        worker = root / "worker.py"
        worker.write_text(body, encoding="utf-8")
        output = root / "evidence"
        with mock.patch.object(launcher, "_privileged", side_effect=privileged):
            code = launcher.main(["--camctl-memory-cgroup-root", str(parent),
                                  "--evidence-dir", str(output), "--", sys.executable,
                                  str(worker), *arguments])
        receipt = json.loads((output / "launcher.json").read_text())
        return code, receipt

    def test_command_runs_as_user_and_receives_exact_budget_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, sentinel, calls, privileged = self.fixture(root)
            observed = root / "observed.json"
            body = ('import json, os, sys\nfrom pathlib import Path\n'
                    'Path(sys.argv[1]).write_text(json.dumps({"uid":os.geteuid(),'
                    '"gid":os.getegid(),"argv":sys.argv[2:],"pid":os.getpid()}))\n')
            code, receipt = self.run_command(root, parent, privileged, body,
                                              str(observed), "value with spaces")
            actual = json.loads(observed.read_text())
            delegated = calls[0][1]
            self.assertEqual(code, 0)
            self.assertEqual(actual["uid"], os.getuid())
            self.assertEqual(actual["gid"], os.getgid())
            self.assertEqual(actual["argv"], ["value with spaces", "--camctl-memory-limit", "2GiB",
                                               "--camctl-memory-cgroup-root", str(delegated)])
            self.assertEqual(receipt["command_pid"], actual["pid"])
            self.assertEqual(receipt["budget_scope"], "memory_and_swap")
            self.assertEqual(receipt["command_exit_code"], 0)
            self.assertTrue(receipt["removal"]["ok"])
            self.assertEqual(receipt["groups"][-1]["resource"]["memory.max"], "max")
            self.assertFalse(delegated.exists())
            self.assertEqual(sentinel.read_text(), "sibling")
            self.assertEqual([call[0] for call in calls], ["--_delegate", "--_remove"])

    def test_command_failure_remains_failure_after_successful_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _, calls, privileged = self.fixture(root)
            code, receipt = self.run_command(root, parent, privileged, "raise SystemExit(7)\n")
            self.assertEqual(code, 7)
            self.assertEqual(receipt["command_exit_code"], 7)
            self.assertTrue(receipt["removal"]["ok"])
            self.assertFalse(calls[0][1].exists())

    def test_resource_tracker_gets_bounded_time_to_exit(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                parent, _, _, privileged = self.fixture(root)
                marker = root / "tracker.json"
                body = (
                    'import json, multiprocessing, os, signal\nfrom pathlib import Path\n'
                    'semaphore = multiprocessing.get_context("spawn").Semaphore()\n'
                    'children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")\n'
                    'tracker = int(children.read_text().split()[0])\n'
                    'os.kill(tracker, signal.SIGSTOP)\n'
                    f'marker = Path({str(marker)!r})\n'
                    'marker.with_suffix(".tmp").write_text(json.dumps([os.getpid(), tracker]))\n'
                    'marker.with_suffix(".tmp").replace(marker)\n'
                    'os._exit(0)\n'
                )
                def resume_tracker():
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    if not marker.exists():
                        return
                    worker_pid, tracker_pid = json.loads(marker.read_text())
                    while Path(f"/proc/{worker_pid}").exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    time.sleep(1.3 if timeout else .3)
                    try:
                        os.kill(tracker_pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass

                resume = threading.Thread(target=resume_tracker)
                resume.start()
                try:
                    code, receipt = self.run_command(root, parent, privileged, body)
                finally:
                    resume.join(timeout=12)
                    if marker.exists():
                        tracker_pid = json.loads(marker.read_text())[1]
                        try:
                            os.kill(tracker_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                settlement = receipt["command_group_settlement"]
                members = settlement["observations"][0]["members"]
                self.assertTrue(any(member["type"] == "resource_tracker" and
                                    member["state"] == "T" for member in members))
                self.assertEqual(receipt["command_exit_code"], 0)
                if timeout:
                    self.assertNotEqual(code, 0)
                    self.assertEqual(settlement["outcome"], "tracker_timeout")
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(settlement["outcome"], "exited")

    def test_live_command_child_is_rejected_and_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _, _, privileged = self.fixture(root)
            marker = root / "child"
            body = ('import subprocess, sys\nfrom pathlib import Path\n'
                    'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                    f'Path({str(marker)!r}).write_text(str(child.pid))\n')
            try:
                code, receipt = self.run_command(root, parent, privileged, body)
                child_pid = int(marker.read_text())
                self.assertNotEqual(code, 0)
                self.assertEqual(receipt["command_exit_code"], 0)
                settlement = receipt["command_group_settlement"]
                self.assertEqual(settlement["outcome"], "unrecognized_member")
                self.assertTrue(any(member["pid"] == child_pid and member["type"] == "unknown"
                                    for member in settlement["observations"][0]["members"]))
            finally:
                if marker.exists():
                    try:
                        os.kill(int(marker.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_delegation_failure_never_starts_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _, calls, privileged = self.fixture(root, delegate_error=True)
            marker = root / "unexpected"
            code, receipt = self.run_command(root, parent, privileged,
                f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
            self.assertNotEqual(code, 0)
            self.assertNotIn("command_pid", receipt)
            self.assertFalse(marker.exists())
            self.assertEqual(len(calls), 1)

    def test_cleanup_failure_cannot_turn_successful_command_green(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _, calls, privileged = self.fixture(root, remove_error=True)
            code, receipt = self.run_command(root, parent, privileged, "pass\n")
            self.assertNotEqual(code, 0)
            self.assertEqual(receipt["command_exit_code"], 0)
            self.assertFalse(receipt["removal"]["ok"])
            self.assertTrue(calls[0][1].exists())

    def test_leaf_oom_is_saved_before_removal_and_rejects_zero_command_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _, calls, privileged = self.fixture(root)
            actual_privileged = privileged
            def with_leaf(action, path, uid, gid, identity=None):
                result = actual_privileged(action, path, uid, gid, identity)
                if action == "--_delegate":
                    controls(path / "leaf", oom=1)
                return result
            def remove_leaf(group):
                receipt = json.loads((root / "evidence/launcher.json").read_text())
                record = next(item for item in receipt["groups"] if item["path"] == str(group.path))
                self.assertEqual(record["resource"]["events"]["oom_kill"], 1)
                remove_controls(group.path)
            with mock.patch.object(launcher._Group, "remove", remove_leaf):
                code, receipt = self.run_command(root, parent, with_leaf, "pass\n")
            self.assertNotEqual(code, 0)
            self.assertEqual(receipt["command_exit_code"], 0)
            self.assertTrue(receipt["groups"][0]["removed"])
            self.assertIn("budget_error", receipt["groups"][0])
            self.assertFalse(calls[0][1].exists())

    def test_interrupt_allows_original_command_to_save_its_cleanup(self):
        for sent_signal in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sent_signal), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                parent, _, calls, privileged = self.fixture(root)
                stopped = root / "stopped"
                body = ('import os, signal, sys, time\nfrom pathlib import Path\n'
                        f'def stop(sig, frame):\n    Path({str(stopped)!r}).write_text("settled")\n'
                        '    raise SystemExit(0)\n'
                        'signal.signal(signal.SIGINT, stop)\n'
                        f'os.kill(os.getppid(), {sent_signal})\n'
                        'while True: time.sleep(.1)\n')
                code, receipt = self.run_command(root, parent, privileged, body)
                self.assertEqual(code, 128 + sent_signal)
                self.assertEqual(stopped.read_text(), "settled")
                self.assertEqual(receipt["command_exit_code"], 0)
                self.assertEqual(receipt["forwarded_signal"], signal.SIGINT)
                self.assertEqual(receipt["interrupted_signal"], sent_signal)
                self.assertFalse(calls[0][1].exists())

    def test_remaining_owned_process_is_killed_and_unrelated_process_is_untouched(self):
        owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                parent, _, calls, privileged = self.fixture(root)
                def with_process(action, path, uid, gid, identity=None):
                    result = privileged(action, path, uid, gid, identity)
                    if action == "--_delegate":
                        controls(path / "leaf")
                        (path / "leaf/cgroup.procs").write_text(str(owned.pid))
                    return result
                def pids(group):
                    return ([owned.pid] if owned.poll() is None else []) if group.path.name == "leaf" else []
                def remove_leaf(group):
                    receipt = json.loads((root / "evidence/launcher.json").read_text())
                    self.assertEqual(receipt["groups"][0]["remaining_pids"], [owned.pid])
                    self.assertIn("resource", receipt["groups"][0])
                    remove_controls(group.path)
                with mock.patch.object(launcher._Group, "pids", pids), \
                     mock.patch.object(launcher._Group, "remove", remove_leaf):
                    code, receipt = self.run_command(root, parent, with_process, "pass\n")
                self.assertNotEqual(code, 0)
                self.assertIsNotNone(owned.poll())
                self.assertIsNone(unrelated.poll())
                self.assertFalse(calls[0][1].exists())
                self.assertEqual(receipt["command_exit_code"], 0)
        finally:
            for process in (owned, unrelated):
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_interruption_before_delegation_result_skips_command_and_removes_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _, calls, privileged = self.fixture(root)
            def interrupt(action, path, uid, gid, identity=None):
                result = privileged(action, path, uid, gid, identity)
                if action == "--_delegate":
                    os.kill(os.getpid(), signal.SIGINT)
                return result
            marker = root / "unexpected"
            code, receipt = self.run_command(root, parent, interrupt,
                f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
            self.assertEqual(code, 130)
            self.assertNotIn("command_pid", receipt)
            self.assertFalse(marker.exists())
            self.assertTrue(receipt["removal"]["ok"])
            self.assertFalse(calls[0][1].exists())

    def test_v2_shared_root_needs_only_existing_memory_subtree_delegation(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            for name, value in {"cgroup.procs": "", "cgroup.controllers": "cpu memory",
                                "cgroup.subtree_control": "memory"}.items():
                (parent / name).write_text(value)
            self.assertEqual(launcher._controller(parent, parent=True), 2)
            self.assertFalse((parent / "memory.max").exists())
            self.assertEqual((parent / "cgroup.subtree_control").read_text(), "memory")

    def test_privileged_helper_uses_separate_session_and_disables_root_bytecode(self):
        with mock.patch.object(launcher.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, '{"ok":true}', '')) as run:
            result = launcher._privileged("--_delegate", Path("/controlled"), 1002, 1002)
        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["sudo", "-n", sys.executable, "-B"])
        self.assertTrue(run.call_args.kwargs["start_new_session"])

    def test_invalid_limit_is_rejected_before_sudo_or_command(self):
        with mock.patch.object(launcher, "_privileged") as privileged:
            with self.assertRaises(SystemExit) as result:
                launcher.main(["--camctl-memory-limit", "0GiB", "--", "pytest"])
            self.assertEqual(result.exception.code, 2)
            privileged.assert_not_called()


if __name__ == "__main__":
    unittest.main()
