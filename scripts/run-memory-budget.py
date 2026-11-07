#!/usr/bin/env python3
"""为普通用户测试命令创建并回收本次独占的内存资源组委派目录。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

from camctl_memory import (
    DEFAULT_MEMORY_LIMIT, BudgetConfig, BudgetEnvironmentError, _Group,
    _budget_error, _discover_root, _write_receipt, parse_memory_limit,
)


_GRACE_SECONDS = 20
_KILL_WAIT_SECONDS = 5
_TRACKER_GRACE_SECONDS = 1
_NAME = re.compile(r"camctl-budget-[0-9]+-[0-9a-f]{32}")
_TRACKER_COMMAND = re.compile(rb"from multiprocessing\.resource_tracker import main;main\([0-9]+\)")


def _controller(path: Path, *, parent: bool = False) -> int:
    if (path / "memory.max").is_file() or (path / "cgroup.controllers").is_file():
        names = (("cgroup.controllers", "cgroup.subtree_control") if parent else
                 ("memory.max", "memory.peak", "memory.swap.max",
                  "memory.swap.current", "memory.events", "cgroup.subtree_control"))
        version = 2
    elif (path / "memory.limit_in_bytes").is_file():
        names = ("memory.limit_in_bytes", "memory.max_usage_in_bytes",
                 "memory.memsw.limit_in_bytes", "memory.memsw.max_usage_in_bytes",
                 "memory.memsw.failcnt", "memory.failcnt", "memory.oom_control",
                 "memory.use_hierarchy")
        version = 1
    else:
        raise BudgetEnvironmentError(f"目录没有可用内存控制器：{path}")
    for name in ("cgroup.procs", *names):
        (path / name).read_text(encoding="ascii")
    if version == 2 and parent and "memory" not in (
            path / "cgroup.subtree_control").read_text().split():
        raise BudgetEnvironmentError("v2 父目录未向子组提供内存控制器")
    if version == 1 and (path / "memory.use_hierarchy").read_text().strip() != "1":
        raise BudgetEnvironmentError("v1 内存控制器未启用后代层级计量")
    return version


def _identity(path: Path) -> dict[str, int]:
    value = path.stat()
    return {"device": value.st_dev, "inode": value.st_ino}


def _helper(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("--_delegate", "--_remove"))
    parser.add_argument("path", type=Path)
    parser.add_argument("uid", type=int)
    parser.add_argument("gid", type=int)
    parser.add_argument("device", nargs="?", type=int)
    parser.add_argument("inode", nargs="?", type=int)
    # 内部动作是位置参数；它们以 -- 开头，故由调用者先加分隔符。
    args = parser.parse_args(["--", *arguments])
    result: dict[str, Any] = {"path": str(args.path), "action": args.action}
    created = False
    try:
        if os.geteuid() != 0 or (args.uid, args.gid) != (
                int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])):
            raise BudgetEnvironmentError("内部委派操作必须通过调用用户的 sudo 执行")
        if not args.path.is_absolute() or _NAME.fullmatch(args.path.name) is None:
            raise BudgetEnvironmentError("内部委派路径必须是本次唯一的绝对路径")
        if args.action == "--_delegate":
            _controller(args.path.parent, parent=True)
            args.path.mkdir()
            created = True
            result["identity"] = _identity(args.path)
            result["version"] = _controller(args.path)
            if result["version"] == 2:
                (args.path / "cgroup.subtree_control").write_text("+memory", encoding="ascii")
            for control in args.path.iterdir():
                os.chown(control, args.uid, args.gid)
            os.chown(args.path, args.uid, args.gid)
        else:
            if _identity(args.path) != {"device": args.device, "inode": args.inode}:
                raise BudgetEnvironmentError("本次委派目录的身份已经改变，保留目录供排查")
            args.path.rmdir()
        result["ok"] = True
    except (OSError, ValueError, KeyError, BudgetEnvironmentError) as error:
        result.update(ok=False, error=str(error))
        if created:
            try:
                args.path.rmdir()
                result["removed_after_error"] = True
            except OSError as cleanup_error:
                result["cleanup_error"] = str(cleanup_error)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def _privileged(action: str, path: Path, uid: int, gid: int,
                identity: dict[str, int] | None = None) -> dict[str, Any]:
    command = ["sudo", "-n", sys.executable, "-B", str(Path(__file__).resolve()),
               action, str(path), str(uid), str(gid)]
    if identity is not None:
        command.extend(str(identity[key]) for key in ("device", "inode"))
    # 外层记录中断，短暂的特权操作完成收据后再清理；终端信号不截断委派握手。
    result = subprocess.run(command, capture_output=True, text=True, start_new_session=True)
    try:
        evidence = json.loads(result.stdout)
    except ValueError:
        evidence = {"ok": False, "error": "sudo 操作没有返回有效收据", "stdout": result.stdout}
    evidence.update(exit_code=result.returncode, stderr=result.stderr)
    if result.returncode:
        evidence["ok"] = False
    return evidence


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _wait(process: subprocess.Popen, interrupted: list[int], evidence: dict[str, Any]) -> int:
    deadline = None
    while True:
        if interrupted and deadline is None:
            # pytest 与隔离运行器通过 KeyboardInterrupt 执行已有的任务收场。
            _signal_group(process, signal.SIGINT)
            evidence["forwarded_signal"] = signal.SIGINT
            deadline = time.monotonic() + _GRACE_SECONDS
        try:
            return process.wait(timeout=.1)
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                evidence["grace_expired"] = True
                _signal_group(process, signal.SIGKILL)
                return process.wait(timeout=_KILL_WAIT_SECONDS)


def _settle_command_group(process: subprocess.Popen, evidence: dict[str, Any]) -> None:
    def exists() -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    if not exists():
        return
    record: dict[str, Any] = {"observations": [], "outcome": "observing"}
    evidence["command_group_settlement"] = record
    started = time.monotonic()
    while True:
        members: list[dict[str, Any]] = []
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                member = None
                try:
                    # comm 可以包含空格或括号；从最后的右括号之后读取固定字段。
                    stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(stat[2]) != process.pid:
                        continue
                    member: dict[str, Any] = {"pid": int(entry.name), "state": stat[0],
                                              "start_time": stat[19], "type": "unknown"}
                    members.append(member)
                    if stat[0] != "Z":
                        argv = (entry / "cmdline").read_bytes().split(b"\0")[:-1]
                        member["argv"] = [os.fsdecode(value) for value in argv]
                        current = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                        member["state"] = current[0]
                        if (len(argv) == 3 and os.path.realpath(os.fsdecode(argv[0])) ==
                                os.path.realpath(sys.executable) and argv[1] == b"-c" and
                                _TRACKER_COMMAND.fullmatch(argv[2]) and current[0] != "Z" and
                                current[2] == stat[2] and current[19] == stat[19]):
                            member["type"] = "resource_tracker"
                except FileNotFoundError:
                    # 扫描期间进程可能退出；下面重新检查进程组。
                    if member is not None:
                        members.remove(member)
                    continue
                except (OSError, ValueError, IndexError) as error:
                    if member is None:
                        members.append({"pid": int(entry.name), "type": "unknown", "error": str(error)})
                    else:
                        member["error"] = str(error)
        except OSError as error:
            members.append({"type": "unknown", "error": str(error)})
        record["observations"].append({"elapsed_seconds": time.monotonic() - started,
                                       "members": members})
        if any(member["type"] != "resource_tracker" for member in members):
            record["outcome"] = "unrecognized_member"
            break
        if not exists():
            record["outcome"] = "exited"
            return
        if not members:
            record["outcome"] = "members_unavailable"
            break
        if time.monotonic() - started >= _TRACKER_GRACE_SECONDS:
            record["outcome"] = "tracker_timeout"
            break
        time.sleep(.02)
    evidence["errors"].append("原测试命令退出后，其持有的进程组仍有后代进程")
    evidence["command_group_remaining"] = True
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + _KILL_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    _signal_group(process, signal.SIGKILL)


def _cleanup(path: Path, version: int, config: BudgetConfig, evidence: dict[str, Any],
             save: Any) -> list[str]:
    errors: list[str] = []
    groups = evidence.setdefault("groups", [])
    def persist() -> bool:
        try:
            save()
            return True
        except (OSError, BudgetEnvironmentError) as error:
            errors.append(f"无法保存清理证据，保留资源组：{error}")
            return False
    # 只遍历本次独占目录，不寻找其他运行留下的资源组或临时文件。
    paths = sorted((value for value in path.rglob("*") if value.is_dir()),
                   key=lambda value: len(value.parts), reverse=True)
    for child in [*paths, path]:
        record: dict[str, Any] = {"path": str(child)}
        groups.append(record)
        group = _Group(child, version, config.limit_bytes)
        try:
            record["remaining_pids"] = group.pids()
            if record["remaining_pids"]:
                errors.append(f"命令退出后资源组仍有运行进程：{child}")
                group.terminate()
        except (OSError, ValueError, BudgetEnvironmentError) as error:
            record["termination_error"] = str(error)
            errors.append(f"无法收场本次资源组 {child}：{error}")
        try:
            if child == path:
                # 委派根服务多个独立预算实例，其默认无界值不能解释为实例限额。
                record["role"] = "delegation"
                names = (("memory.max", "memory.swap.max", "memory.peak", "memory.events")
                         if version == 2 else
                         ("memory.limit_in_bytes", "memory.memsw.limit_in_bytes",
                          "memory.max_usage_in_bytes", "memory.memsw.max_usage_in_bytes",
                          "memory.oom_control"))
                record["resource"] = {name: (child / name).read_text(encoding="ascii").strip()
                                      for name in names}
            else:
                record["resource"] = group.snapshot()
                error = _budget_error(record["resource"], config)
                if error is not None:
                    record["budget_error"] = str(error)
                    errors.append(str(error))
        except (OSError, ValueError, KeyError, BudgetEnvironmentError) as error:
            record["resource_error"] = str(error)
            errors.append(f"无法收集资源组最终证据 {child}：{error}")
        # 删除之前持久保存最终资源证据；写入失败时保留资源组。
        persisted = persist()
        if child != path and "resource" in record and persisted:
            try:
                group.remove()
                record["removed"] = True
            except (OSError, BudgetEnvironmentError) as error:
                record["remove_error"] = str(error)
                errors.append(f"无法删除本次叶组 {child}：{error}")
        persist()
    return errors


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments and arguments[0] in ("--_delegate", "--_remove"):
        return _helper(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camctl-memory-limit", default=DEFAULT_MEMORY_LIMIT)
    parser.add_argument("--camctl-memory-cgroup-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    if not args.command or args.command[0] != "--" or len(args.command) == 1:
        parser.error("请在 -- 后提供原测试命令")
    try:
        limit = parse_memory_limit(args.camctl_memory_limit)
    except ValueError as error:
        parser.error(str(error))
    if os.geteuid() == 0:
        parser.error("测试启动器须由普通用户执行；仅内部委派操作使用 sudo")
    output = args.evidence_dir.absolute() if args.evidence_dir else Path(
        tempfile.mkdtemp(prefix="camctl-memory-budget-"))
    if args.evidence_dir:
        output.mkdir(parents=True)
    uid, gid = os.getuid(), os.getgid()
    evidence: dict[str, Any] = {
        "budget_scope": "memory_and_swap", "uid": uid, "gid": gid,
        "requested_limit": args.camctl_memory_limit, "limit_bytes": limit,
        "cwd": str(Path.cwd()), "command": args.command[1:], "status": "preparing",
        "started_at": time.time(), "errors": [],
    }
    receipt = output / "launcher.json"
    save = lambda: _write_receipt(receipt, evidence)
    print(f"内存预算启动收据：{receipt}", flush=True)
    interrupted: list[int] = []
    def handle_signal(signum: int, _frame: Any) -> None:
        if not interrupted:
            interrupted.append(signum)
    handlers = {sig: signal.signal(sig, handle_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    process = None
    delegated = None
    path = None
    code = 1
    try:
        save()
        parent = (args.camctl_memory_cgroup_root or _discover_root()).resolve()
        path = parent / f"camctl-budget-{os.getpid()}-{uuid.uuid4().hex}"
        evidence["delegation_path"] = str(path)
        save()
        delegated = _privileged("--_delegate", path, uid, gid)
        evidence["delegation"] = delegated
        save()
        if not delegated["ok"]:
            raise BudgetEnvironmentError(f"无法委派本次资源组：{delegated.get('error', delegated)}")
        command = [*args.command[1:], "--camctl-memory-limit", args.camctl_memory_limit,
                   "--camctl-memory-cgroup-root", str(path)]
        evidence["executed_command"] = command
        if not interrupted:
            process = subprocess.Popen(command, start_new_session=True)
            evidence.update(command_pid=process.pid, command_pgid=process.pid, status="running")
            save()
            code = _wait(process, interrupted, evidence)
            evidence["command_exit_code"] = code
            _settle_command_group(process, evidence)
        evidence["status"] = "cleaning"
        save()
    except (OSError, ValueError, KeyError, BudgetEnvironmentError, subprocess.TimeoutExpired) as error:
        evidence["errors"].append(str(error))
        if process is not None and process.poll() is None:
            _signal_group(process, signal.SIGKILL)
            evidence["command_exit_code"] = process.wait(timeout=_KILL_WAIT_SECONDS)
            _settle_command_group(process, evidence)
    finally:
        try:
            if delegated and delegated.get("identity") and not delegated.get("removed_after_error"):
                if delegated.get("ok"):
                    evidence["errors"].extend(_cleanup(
                        path, delegated["version"], BudgetConfig(limit, path), evidence, save))
                evidence["removal"] = _privileged("--_remove", path, uid, gid, delegated["identity"])
                if not evidence["removal"]["ok"]:
                    evidence["errors"].append(f"无法删除本次委派目录：{evidence['removal']}")
        except (OSError, ValueError, KeyError, BudgetEnvironmentError) as error:
            evidence["errors"].append(f"无法完成本次资源组清理：{error}")
        if interrupted:
            evidence["interrupted_signal"] = interrupted[0]
            code = 128 + interrupted[0]
        elif code < 0:
            code = 128 - code
        if evidence["errors"] and code == 0:
            code = 1
        evidence.update(status="finished", exit_code=code, finished_at=time.time())
        try:
            save()
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
    for error in evidence["errors"]:
        print(error, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
