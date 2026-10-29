"""测试进程及其后代的 Linux 内存与交换空间合计预算。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, NoReturn
from collections.abc import Iterable
import uuid


DEFAULT_MEMORY_LIMIT = "2GiB"
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
          "KB": 1000, "MB": 1000**2, "GB": 1000**3}


class MemoryBudgetExceeded(AssertionError):
    """内核证据表明本次运行违反内存预算。"""


class BudgetEnvironmentError(RuntimeError):
    """环境不能完整实施或核验本次预算。"""


def parse_memory_limit(value: str) -> int:
    """将带明确单位的正整数容量解析成精确字节数。"""
    if not isinstance(value, str):
        raise ValueError("内存预算必须为带单位的正整数字符串")
    match = re.fullmatch(r"([0-9]+)(B|KiB|MiB|GiB|KB|MB|GB)", value)
    if match is None or int(match[1]) <= 0:
        raise ValueError(f"非法内存预算：{value!r}")
    return int(match[1]) * _UNITS[match[2]]


@dataclass(frozen=True)
class BudgetConfig:
    limit_bytes: int
    cgroup_root: Path | None = None
    requested_limit: str | None = None

    def __post_init__(self) -> None:
        if type(self.limit_bytes) is not int or self.limit_bytes <= 0:
            raise ValueError("内存预算字节数必须为正整数")
        if self.cgroup_root is not None:
            object.__setattr__(self, "cgroup_root", Path(self.cgroup_root).absolute())
        if self.requested_limit is not None and parse_memory_limit(self.requested_limit) != self.limit_bytes:
            raise ValueError("请求容量与预算字节数不一致")


_CONFIG = BudgetConfig(parse_memory_limit(DEFAULT_MEMORY_LIMIT))


def configure_budget(limit: str, cgroup_root: Path | None = None) -> BudgetConfig:
    global _CONFIG
    _CONFIG = BudgetConfig(parse_memory_limit(limit), cgroup_root, limit)
    return _CONFIG


def get_budget_config() -> BudgetConfig:
    return _CONFIG


def _read(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def _write(path: Path, value: str) -> None:
    with path.open("w", encoding="ascii") as stream:
        stream.write(value)


def _mkdir(path: Path) -> None:
    path.mkdir()


def _rmdir(path: Path) -> None:
    path.rmdir()


def _exists(path: Path) -> bool:
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


def _write_receipt(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise BudgetEnvironmentError(f"无法保存预算收据 {path}：{error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _optional(path: Path) -> str | None:
    try:
        return _read(path)
    except FileNotFoundError:
        return None


def _numbers(value: str) -> dict[str, int]:
    return {key: int(number) for key, number in (line.split() for line in value.splitlines())}


def _host_swap_bytes() -> int:
    for line in _read(Path("/proc/meminfo")).splitlines():
        if line.startswith("SwapTotal:"):
            fields = line.split()
            if len(fields) == 3 and fields[2] == "kB":
                return int(fields[1]) * 1024
    raise BudgetEnvironmentError("无法读取宿主交换空间容量")


def _mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _discover_root() -> Path:
    memberships = []
    for line in _read(Path("/proc/self/cgroup")).splitlines():
        _, controllers, member = line.split(":", 2)
        memberships.append((controllers.split(","), member))
    memory_on_v1 = any("memory" in controllers for controllers, _ in memberships)
    for line in _read(Path("/proc/self/mountinfo")).splitlines():
        before, after = line.split(" - ", 1)
        fields, mount = before.split(), after.split()
        version = 2 if mount[0] == "cgroup2" else 1 if mount[0] == "cgroup" and "memory" in mount[2].split(",") else 0
        if not version or (memory_on_v1 and version != 1):
            continue
        for controllers, member in memberships:
            if (version == 2 and controllers == [""]) or (version == 1 and "memory" in controllers):
                base = _mount_path(fields[3])
                relative = os.path.relpath(member, base)
                if relative == ".." or relative.startswith("../"):
                    continue
                return Path(_mount_path(fields[4])) / relative
    raise BudgetEnvironmentError("未找到当前进程的内存资源组挂载；请指定已委派根目录")


class _Group:
    def __init__(self, path: Path, version: int, limit: int):
        self.path, self.version, self.limit = path, version, limit

    def pids(self) -> list[int]:
        return [int(value) for value in _read(self.path / "cgroup.procs").split()]

    def snapshot(self) -> dict[str, Any]:
        if self.version == 2:
            current_swap = int(_read(self.path / "memory.swap.current"))
            swap_peak = _optional(self.path / "memory.swap.peak")
            limit = int(_read(self.path / "memory.max"))
            peak = int(_read(self.path / "memory.peak"))
            return {"budget_scope": "memory_and_swap",
                    "limit_bytes": limit,
                    "combined_limit_bytes": limit,
                    "combined_peak_bytes": peak,
                    "combined_peak_method": "memory.peak",
                    "swap_limit_bytes": int(_read(self.path / "memory.swap.max")),
                    "peak_bytes": peak,
                    "swap_peak_bytes": int(swap_peak) if swap_peak is not None else current_swap,
                    "swap_peak_method": "memory.swap.peak" if swap_peak is not None else "memory.swap.max=0 and memory.swap.current",
                    "swap_current_bytes": current_swap,
                    "events": _numbers(_read(self.path / "memory.events"))}
        events = _numbers(_read(self.path / "memory.oom_control"))
        return {"budget_scope": "memory_and_swap",
                "limit_bytes": int(_read(self.path / "memory.limit_in_bytes")),
                "combined_limit_bytes": int(_read(self.path / "memory.memsw.limit_in_bytes")),
                "peak_bytes": int(_read(self.path / "memory.max_usage_in_bytes")),
                "combined_peak_bytes": int(_read(self.path / "memory.memsw.max_usage_in_bytes")),
                "combined_peak_method": "memory.memsw.max_usage_in_bytes",
                "swap_peak_bytes": None,
                "swap_peak_method": "独立交换峰值未计量；合计峰值由 memory.memsw.max_usage_in_bytes 提供",
                "host_swap_bytes": _host_swap_bytes(),
                "events": {**events, "failcnt": int(_read(self.path / "memory.failcnt")),
                           "memsw_failcnt": int(_read(self.path / "memory.memsw.failcnt"))}}

    def terminate(self) -> None:
        # 只向本次拥有的叶组发信号；cgroup.kill 可原子清理 v2 后代。
        if self.version == 2 and _exists(self.path / "cgroup.kill"):
            _write(self.path / "cgroup.kill", "1")
        deadline = time.monotonic() + 5
        while self.pids():
            self.send_signal(signal.SIGKILL)
            if time.monotonic() >= deadline:
                raise BudgetEnvironmentError(f"资源组仍有后代进程，保留供排查：{self.path}")
            time.sleep(0.01)

    def send_signal(self, sig: int) -> None:
        for pid in self.pids():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def remove(self) -> None:
        if self.pids():
            raise BudgetEnvironmentError(f"不能释放仍有进程的资源组：{self.path}")
        _rmdir(self.path)


def _new_group(config: BudgetConfig) -> _Group:
    path: Path | None = None
    created = False
    try:
        root = (config.cgroup_root if config.cgroup_root is not None else _discover_root()).absolute()
        if _optional(root / "cgroup.controllers") is not None or _optional(root / "memory.max") is not None:
            version = 2
        elif _optional(root / "memory.limit_in_bytes") is not None:
            version = 1
        else:
            raise BudgetEnvironmentError(f"目录没有可用内存控制器：{root}")
        path = root / f"camctl-memory-{os.getpid()}-{uuid.uuid4().hex}"
        _mkdir(path)
        created = True
        group = _Group(path, version, config.limit_bytes)
        _read(path / "cgroup.procs")
        if version == 2:
            for name in ("memory.max", "memory.peak", "memory.swap.max", "memory.swap.current", "memory.events"):
                _read(path / name)
            events = _numbers(_read(path / "memory.events"))
            if not {"oom", "oom_kill"} <= events.keys():
                raise BudgetEnvironmentError("memory.events 缺少内存不足事件计数")
            _write(path / "memory.max", str(config.limit_bytes))
            _write(path / "memory.swap.max", "0")
        else:
            for name in ("memory.limit_in_bytes", "memory.memsw.limit_in_bytes",
                         "memory.max_usage_in_bytes", "memory.memsw.max_usage_in_bytes",
                         "memory.failcnt", "memory.memsw.failcnt", "memory.oom_control", "memory.use_hierarchy"):
                _read(path / name)
            if "oom_kill" not in _numbers(_read(path / "memory.oom_control")):
                raise BudgetEnvironmentError("v1 缺少累计 oom_kill 证据")
            if _read(path / "memory.use_hierarchy") != "1":
                raise BudgetEnvironmentError("v1 内存控制器未启用后代层级计量")
            _write(path / "memory.limit_in_bytes", str(config.limit_bytes))
            _write(path / "memory.memsw.limit_in_bytes", str(config.limit_bytes))
        evidence = group.snapshot()
        error = _budget_error(evidence, config)
        if error is not None:
            raise BudgetEnvironmentError(f"新资源组不能落实合计预算：{error}")
        return group
    except (OSError, ValueError, KeyError, BudgetEnvironmentError) as error:
        if created and path is not None:
            try:
                _rmdir(path)
            except OSError:
                pass
        if isinstance(error, BudgetEnvironmentError):
            raise
        raise BudgetEnvironmentError(f"无法创建并配置专属内存资源组：{error}") from error


def ensure_memory_budget_available(config: BudgetConfig) -> None:
    """仅创建并释放本次预检叶组，不启动业务，不修改父组。"""
    group = _new_group(config)
    try:
        group.snapshot()
    except (OSError, ValueError, KeyError) as error:
        raise BudgetEnvironmentError(f"无法读取预检资源组证据：{error}") from error
    finally:
        try:
            group.remove()
        except OSError as error:
            raise BudgetEnvironmentError(f"无法释放预检资源组：{error}") from error


def _start_process(command: Any, group: _Group, kwargs: dict[str, Any]) -> subprocess.Popen:
    if kwargs.get("preexec_fn") is not None or kwargs.get("executable") is not None:
        raise ValueError("内存预算启动器不接受 preexec_fn 或 executable")
    options = dict(kwargs)
    shell = options.pop("shell", False)
    if shell:
        if not isinstance(command, (str, bytes)):
            raise ValueError("shell=True 时预算命令必须为字符串")
        inner = ["/bin/sh", "-c", command]
    else:
        inner = [command] if isinstance(command, (str, bytes, os.PathLike)) else list(command)
    if not inner:
        raise ValueError("预算命令不能为空")
    read_fd, write_fd = os.pipe()
    process = None
    try:
        inherited = tuple(options.pop("pass_fds", ()))
        if options.get("close_fds") is False:
            raise ValueError("内存预算启动器要求 close_fds=True")
        options["pass_fds"] = (*inherited, write_fd)
        # 入组失败时不会执行任何业务代码；握手区分环境错误与业务退出码。
        script = ('printf "%s\\n" "$$" > "$1/cgroup.procs" || exit 125\n'
                  'printf x >&"$2" || exit 125\n'
                  'eval "exec $2>&-"\nshift 2\nexec "$@"')
        # dash 不支持多位重定向 FD；bash -p 同时禁止 BASH_ENV 启动求值。
        try:
            process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-p", "-c", script,
                                        "camctl-memory", str(group.path), str(write_fd), *inner], **options)
        except OSError as error:
            raise BudgetEnvironmentError(f"无法启动必需的 /bin/bash 入组代理：{error}") from error
        os.close(write_fd)
        write_fd = -1
        ready, _, _ = select.select([read_fd], [], [], 10)
        if not ready or os.read(read_fd, 1) != b"x":
            raise BudgetEnvironmentError("启动 shell 未能在执行业务前进入专属资源组")
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
        raise
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def _budget_error(evidence: dict[str, Any], config: BudgetConfig) -> Exception | None:
    if not isinstance(evidence, dict) or evidence.get("budget_scope") != "memory_and_swap":
        return BudgetEnvironmentError("资源证据缺少内存与交换空间合计预算口径")
    for name in ("limit_bytes", "combined_limit_bytes", "peak_bytes", "combined_peak_bytes"):
        if type(evidence.get(name)) is not int or evidence[name] < 0:
            return BudgetEnvironmentError(f"资源证据缺少有效的 {name}")
    if evidence["limit_bytes"] != config.limit_bytes or evidence["combined_limit_bytes"] != config.limit_bytes:
        return BudgetEnvironmentError("实际内存或合计限额与请求预算不符")
    method = evidence.get("combined_peak_method")
    required_events = ("oom_kill", "failcnt", "memsw_failcnt")
    if method == "memory.peak":
        for name in ("swap_limit_bytes", "swap_current_bytes", "swap_peak_bytes"):
            if type(evidence.get(name)) is not int or evidence[name] != 0:
                return BudgetEnvironmentError(f"v2 不能核实零交换约束：{name}")
        if evidence["combined_peak_bytes"] != evidence["peak_bytes"]:
            return BudgetEnvironmentError("v2 合计峰值与零交换下的内存峰值不一致")
        required_events = ("oom", "oom_kill")
    elif method != "memory.memsw.max_usage_in_bytes":
        return BudgetEnvironmentError("资源证据缺少可靠的内核合计峰值来源")
    events = evidence.get("events")
    if not isinstance(events, dict):
        return BudgetEnvironmentError("资源证据缺少内存不足事件计数")
    for name in set(required_events) | ({"oom", "oom_kill", "oom_group_kill"} & events.keys()):
        if type(events.get(name)) is not int or events[name] < 0:
            return BudgetEnvironmentError(f"资源证据缺少有效的事件计数：{name}")
    if evidence["combined_peak_bytes"] > config.limit_bytes:
        return MemoryBudgetExceeded("内存与交换空间合计峰值超过预算")
    if any(events.get(key, 0) for key in ("oom", "oom_kill", "oom_group_kill")):
        return MemoryBudgetExceeded("资源组记录到内存不足事件（包括短命后代）")
    return None


class BudgetedPopen:
    """Popen 代理；主进程和全部后代收场后才完成预算核验。"""

    def __init__(self, command: Any, *, budget: BudgetConfig, receipt_path: Path, **kwargs: Any):
        if _exists(Path(receipt_path)):
            raise BudgetEnvironmentError("每次预算运行必须使用新的独立收据路径")
        if not isinstance(command, (str, bytes, os.PathLike)):
            command = list(command)
        self.args = command
        self._budget = budget
        self._receipt_path = Path(receipt_path)
        self._group: _Group | None = None
        self._process: subprocess.Popen | None = None
        self._finished = False
        self._error: Exception | None = None
        self._explicit_kill = False
        self._started = time.monotonic()
        receipt_command = os.fsdecode(command) if isinstance(command, (str, bytes, os.PathLike)) else [os.fsdecode(value) for value in command]
        self._receipt: dict[str, Any] = {"schema": 1, "budget_scope": "memory_and_swap", "status": "starting", "command": receipt_command,
            "requested_limit": budget.requested_limit or f"{budget.limit_bytes}B", "limit_bytes": budget.limit_bytes,
            "configured_root": str(budget.cgroup_root) if budget.cgroup_root is not None else None}
        try:
            self._group = _new_group(budget)
            self._receipt.update(status="pending", controller=f"cgroup-v{self._group.version}",
                                 group=str(self._group.path), owned_root=str(self._group.path.parent))
            _write_receipt(self._receipt_path, self._receipt)
            self._receipt["baseline"] = self._group.snapshot()
            _write_receipt(self._receipt_path, self._receipt)
            self._process = _start_process(command, self._group, kwargs)
            self._receipt["pid"] = self._process.pid
            _write_receipt(self._receipt_path, self._receipt)
        except BaseException as error:
            self._cleanup("environment_error", error)
            raise

    def __getattr__(self, name: str) -> Any:
        if self._process is None:
            raise AttributeError(name)
        return getattr(self._process, name)

    def _finish(self, status: str = "completed", cause: BaseException | None = None) -> None:
        if self._finished:
            if self._error is not None:
                raise self._error
            return
        try:
            assert self._group is not None
            if self._group.pids():
                raise BudgetEnvironmentError("采集最终证据前资源组仍有进程")
            evidence = self._group.snapshot()
            self._receipt["evidence"] = evidence
            self._error = _budget_error(evidence, self._budget)
            if self._error is not None:
                status = "budget_exceeded" if isinstance(self._error, MemoryBudgetExceeded) else "environment_error"
            self._receipt.update(status="finalizing", final_status=status)
            if self._error is not None or cause is not None:
                self._receipt["error"] = str(self._error or cause)
            # 先落盘最终证据，再释放内核计数；SIGKILL 不能抹去唯一证据。
            _write_receipt(self._receipt_path, self._receipt)
            self._group.remove()
            self._receipt["group_removed"] = True
        except (OSError, ValueError, KeyError, BudgetEnvironmentError) as error:
            self._error = error if isinstance(error, BudgetEnvironmentError) else BudgetEnvironmentError(f"无法完整采集或清理预算证据：{error}")
            status = "environment_error"
        finally:
            self._finished = True
            self._receipt.update(status=status, elapsed_seconds=time.monotonic() - self._started,
                                 returncode=self._process.returncode if self._process is not None else None)
            if self._error is not None or cause is not None:
                self._receipt["error"] = str(self._error or cause)
            _write_receipt(self._receipt_path, self._receipt)
        if self._error is not None:
            raise self._error

    def _cleanup(self, status: str, cause: BaseException | None = None) -> None:
        if self._finished:
            return
        if self._group is None:
            self._receipt.update(status=status, error=str(cause), elapsed_seconds=time.monotonic() - self._started)
            _write_receipt(self._receipt_path, self._receipt)
            self._finished = True
            return
        try:
            cleanup_deadline = time.monotonic() + 5
            self._group.terminate()
            if self._process is not None:
                self._process.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired, BudgetEnvironmentError) as error:
            self._receipt.update(status="environment_error", error=str(error), group_removed=False,
                                 elapsed_seconds=time.monotonic() - self._started)
            _write_receipt(self._receipt_path, self._receipt)
            self._finished = True
            raise BudgetEnvironmentError(f"未能清空本次资源组：{error}") from error
        self._finish(status, cause)

    def poll(self) -> int | None:
        assert self._process is not None and self._group is not None
        code = self._process.poll()
        if self._finished:
            if self._error is not None:
                raise self._error
            return code
        if code is not None and not self._group.pids():
            self._finish()
            return code
        if code is not None and self._explicit_kill:
            self._receipt["returncode"] = code
            _write_receipt(self._receipt_path, self._receipt)
            return code
        return None

    def send_signal(self, sig: int) -> None:
        assert self._process is not None
        if self._finished:
            return
        # 正常信号必须由生产主进程处理；不能替它给所有后代发信号。
        if sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGKILL):
            self._explicit_kill = sig == signal.SIGKILL
            self._receipt["termination_mode"] = "explicit_sigkill" if self._explicit_kill else "graceful_signal"
            _write_receipt(self._receipt_path, self._receipt)
        self._process.send_signal(sig)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def _handle_timeout(self, error: subprocess.TimeoutExpired) -> NoReturn:
        try:
            self._cleanup("timeout", error)
        except (BudgetEnvironmentError, MemoryBudgetExceeded) as cleanup_error:
            # 保留调用方超时，同时让失败收据和异常链保留清理/预算错误。
            raise error from cleanup_error
        raise error

    def wait(self, timeout: float | None = None) -> int:
        assert self._process is not None and self._group is not None
        if self._finished:
            if self._error is not None:
                raise self._error
            return self._process.wait(timeout=timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            effective_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            code = self._process.wait(timeout=effective_timeout)
            if self._explicit_kill and self._group.pids():
                # 故障注入只强杀主进程；业务断言后由 context/外层收场核验后代。
                self._receipt["returncode"] = code
                _write_receipt(self._receipt_path, self._receipt)
                return code
            while self._group.pids():
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                time.sleep(0.01)
            self._finish()
            return code
        except subprocess.TimeoutExpired as error:
            self._handle_timeout(error)
        except BaseException as error:
            self._cleanup("interrupted", error)
            raise

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        assert self._process is not None
        started = time.monotonic()
        try:
            result = self._process.communicate(input=input, timeout=timeout)
            remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
            self.wait(timeout=remaining)
            return result
        except subprocess.TimeoutExpired as error:
            self._handle_timeout(error)
        except BaseException as error:
            self._cleanup("interrupted", error)
            raise

    def __enter__(self) -> BudgetedPopen:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        try:
            if exc is None:
                self.wait()
                if self._explicit_kill and not self._finished:
                    self._cleanup("interrupted")
            else:
                self._cleanup("interrupted", exc)
        finally:
            if self._process is not None:
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    if stream is not None:
                        stream.close()


def run_budgeted(command: Any, *, budget: BudgetConfig, receipt_path: Path,
                 timeout: float | None = None, check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    input_value = kwargs.pop("input", None)
    if input_value is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin 和 input 不能同时使用")
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("capture_output 和 stdout/stderr 不能同时使用")
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with BudgetedPopen(command, budget=budget, receipt_path=receipt_path, **kwargs) as process:
        stdout, stderr = process.communicate(input=input_value, timeout=timeout)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def exec_budgeted(command: Any, *, budget: BudgetConfig, receipt_path: Path,
                  env: dict[str, str] | None = None, cwd: Path | str | None = None) -> NoReturn:
    """当前测试入口入组后直接 exec，保留 C 调用方持有的 PID 和进程组身份。

    成功后由组外测试运行器调用 finalize_budget_receipts 收场；exec 失败也保留
    pending 身份供其恢复，不能从仍在组内的当前进程强杀整个组。
    """
    arguments = [command] if isinstance(command, (str, bytes, os.PathLike)) else list(command)
    if not arguments:
        raise ValueError("预算命令不能为空")
    path = Path(receipt_path).absolute()
    if _exists(path):
        raise BudgetEnvironmentError("每次预算运行必须使用新的独立收据路径")
    data: dict[str, Any] = {"schema": 1, "budget_scope": "memory_and_swap", "status": "starting", "command": [os.fsdecode(value) for value in arguments],
        "requested_limit": budget.requested_limit or f"{budget.limit_bytes}B", "limit_bytes": budget.limit_bytes,
        "configured_root": str(budget.cgroup_root) if budget.cgroup_root is not None else None, "pid": os.getpid()}
    group = None
    try:
        group = _new_group(budget)
        data.update(status="pending", controller=f"cgroup-v{group.version}", group=str(group.path),
                    owned_root=str(group.path.parent))
        _write_receipt(path, data)
        data["baseline"] = group.snapshot()
        _write_receipt(path, data)
        if cwd is not None:
            os.chdir(cwd)
        _write(group.path / "cgroup.procs", str(os.getpid()))
        if os.getpid() not in group.pids():
            raise BudgetEnvironmentError("当前测试入口未进入专属资源组")
        os.execvpe(arguments[0], arguments, os.environ if env is None else env)
    except BaseException as error:
        data.update(status="pending" if group is not None else "environment_error",
                    launch_error=str(error), error=str(error))
        _write_receipt(path, data)
        if isinstance(error, (KeyboardInterrupt, SystemExit, BudgetEnvironmentError)):
            raise
        raise BudgetEnvironmentError(f"受限测试入口无法 exec 业务程序：{error}") from error
    raise AssertionError("os.execvpe 意外返回")


def finalize_budget_receipts(paths: Iterable[Path]) -> None:
    """由组外测试运行器恢复已登记 receipt，覆盖启动代理被强制终止的情况。

    调用方只传本次拥有的 receipt；不会自动扫描目录或修改其他资源组。
    已完成记录同样复核资源证据，全部记录处理后再报告第一个错误。
    """
    errors: list[Exception] = []
    for receipt_path in paths:
        path = Path(receipt_path)
        data: dict[str, Any] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") != 1:
                raise BudgetEnvironmentError(f"未知预算收据格式：{path}")
            config = BudgetConfig(data["limit_bytes"])
            status = data.get("status")
            final_status = "environment_error" if status == "environment_error" else data.get("final_status", "recovered")
            if status in ("pending", "finalizing") or ("group" in data and not data.get("group_removed")):
                group_path, root = Path(data["group"]), Path(data["owned_root"])
                if group_path.parent != root or not re.fullmatch(r"camctl-memory-[0-9]+-[0-9a-f]{32}", group_path.name):
                    raise BudgetEnvironmentError("收据没有有效的本次专属资源组身份")
                version = {"cgroup-v1": 1, "cgroup-v2": 2}.get(data.get("controller"))
                if version is None:
                    raise BudgetEnvironmentError("收据缺少资源组控制器版本")
                group = _Group(group_path, version, config.limit_bytes)
                if _exists(group_path):
                    if data.get("termination_mode") == "graceful_signal" and group.pids():
                        final_status = "environment_error"
                        data["error"] = "取消信号后的实例仍有进程；外层仅为收场而强杀，不能当作正常完成"
                    group.terminate()
                    data["evidence"] = group.snapshot()
                    data.update(status="finalizing", final_status=final_status)
                    _write_receipt(path, data)
                    group.remove()
                elif status != "finalizing" or "evidence" not in data:
                    raise BudgetEnvironmentError("待完成预算资源组已消失且没有持久化最终证据")
                data.update(status=final_status, group_removed=True)
            if "evidence" not in data:
                raise BudgetEnvironmentError(data.get("error", "预算收据缺少完整资源证据"))
            error = _budget_error(data["evidence"], config)
            if error is not None:
                data.update(status="budget_exceeded" if isinstance(error, MemoryBudgetExceeded) else "environment_error", error=str(error))
                errors.append(error)
            elif data.get("status") == "environment_error":
                errors.append(BudgetEnvironmentError(data.get("error", "预算环境错误")))
            elif data.get("launch_error"):
                data.update(status="environment_error", error=data["launch_error"])
                errors.append(BudgetEnvironmentError(data["launch_error"]))
            _write_receipt(path, data)
        except (OSError, ValueError, TypeError, KeyError, BudgetEnvironmentError) as error:
            wrapped = error if isinstance(error, BudgetEnvironmentError) else BudgetEnvironmentError(f"无法恢复预算收据 {path}：{error}")
            errors.append(wrapped)
            if data:
                data.update(status="environment_error", error=str(wrapped))
                _write_receipt(path, data)
    if errors:
        raise errors[0]
