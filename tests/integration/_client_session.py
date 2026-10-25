"""每个Hasselblad world独占一个真实Node客户端，控制连接只传文件路径。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import time
import uuid


_TIMEOUT = 180.0  # 原一次性Node命令的预算，分别用于startup、操作和正常close。
_TERM_GRACE = 3.0
_KILL_GRACE = 3.0


class SessionError(RuntimeError):
    """会话、业务命令或收场失败；完整stderr另存于world。"""


@dataclass(frozen=True)
class _Process:
    pid: int
    ppid: int
    pgid: int
    starttime: str
    state: str


def _process(pid: int) -> _Process | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return _Process(pid, int(fields[1]), int(fields[2]), fields[19], fields[0])
    except (FileNotFoundError, ProcessLookupError):
        return None


def _same(current: _Process | None, expected: _Process) -> bool:
    return current is not None and (current.pid, current.starttime, current.pgid) == (
        expected.pid, expected.starttime, expected.pgid)


class ClientSession:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str], root: Path):
        self.root, self.cwd = Path(root).resolve(), Path(cwd).resolve()
        self.session_id = str(uuid.uuid4())
        self.node_metrics = self.root / "client-session-node.json"
        self.python_metrics = self.root / "client-session-python.json"
        self.stdout_path = self.root / "client-session.stdout.log"
        self.stderr_path = self.root / "client-session.stderr.log"
        self.process: subprocess.Popen | None = None
        self.connection: socket.socket | None = None
        self.listener: socket.socket | None = None
        self.buffer = bytearray()
        self.owned: dict[int, _Process] = {}
        self.groups: dict[int, list[_Process]] = {}
        self.next_id = 1
        self.state = "starting"
        self.metrics = {
            "schema_version": 1, "session_id": self.session_id,
            "nodeid": os.environ.get("PYTEST_CURRENT_TEST", "").split(" (", 1)[0],
            "command_prefix": command, "phase_timeout_seconds": _TIMEOUT,
            "startup": None, "commands": [], "close": None,
        }
        started = time.monotonic_ns()
        deadline = time.monotonic() + _TIMEOUT
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.listener = socket.socket()
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            host, port = self.listener.getsockname()
            argv = [*command, "session", str(self.root / "client-store"), host,
                    str(port), self.session_id, str(self.node_metrics)]
            with self.stdout_path.open("wb") as stdout, self.stderr_path.open("wb") as stderr:
                self.process = subprocess.Popen(
                    argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True)
            identity = _process(self.process.pid)
            if identity is not None:
                if identity.pgid != self.process.pid:
                    raise SessionError("Node没有取得自己的进程组")
                self._remember(identity)
            self.metrics["node_pid"] = self.process.pid
            self._save()
            self._wait_readable(self.listener, deadline)
            self.connection, _ = self.listener.accept()
            self._close_listener()
            ready = self._read(deadline)
            if (ready.get("kind"), ready.get("session_id"), ready.get("pid")) != (
                    "ready", self.session_id, self.process.pid):
                raise SessionError("客户端startup回执身份不符")
            self.state = "ready"
            self.metrics["startup"] = self._timing(started, "passed")
            self._save()
        except BaseException as error:
            self.metrics["startup"] = self._timing(started, "failed")
            self._fail(error)
            raise

    @staticmethod
    def _timing(started: int, outcome: str) -> dict:
        ended = time.monotonic_ns()
        return {"started_monotonic_ns": started, "finished_monotonic_ns": ended,
                "elapsed_seconds": (ended - started) / 1e9, "outcome": outcome}

    def _save(self):
        target = self.python_metrics.with_suffix(".next")
        target.write_text(json.dumps(self.metrics, ensure_ascii=False, indent=2) + "\n")
        target.replace(self.python_metrics)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"客户端会话超过{_TIMEOUT:g}秒预算；{self.stderr_path}")
        return remaining

    def _wait_readable(self, stream: socket.socket, deadline: float):
        while True:
            if select.select([stream], [], [], min(.1, self._remaining(deadline)))[0]:
                return
            if self.process is not None and self.process.poll() is not None:
                raise SessionError(f"客户端提前退出{self.process.returncode}；{self.stderr_path}")

    def _read(self, deadline: float) -> dict:
        assert self.connection is not None
        while b"\n" not in self.buffer:
            self._wait_readable(self.connection, deadline)
            chunk = self.connection.recv(65536)
            if not chunk:
                raise SessionError(f"客户端回执前连接结束；{self.stderr_path}")
            self.buffer.extend(chunk)
            if len(self.buffer) > 1024 * 1024:
                raise SessionError("客户端控制回执超过1MiB")
        self._remaining(deadline)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        result = json.loads(line)
        if not isinstance(result, dict):
            raise SessionError("客户端控制回执不是对象")
        return result

    def _send(self, command_id: int, operation: str, paths: list[str], deadline: float) -> dict:
        assert self.connection is not None
        self.connection.settimeout(self._remaining(deadline))
        self.connection.sendall((json.dumps({
            "id": command_id, "session_id": self.session_id,
            "operation": operation, "arguments": paths,
        }, ensure_ascii=False) + "\n").encode())
        reply = self._read(deadline)
        if (reply.get("kind") != "result" or reply.get("session_id") != self.session_id
                or type(reply.get("id")) is not int or reply["id"] != command_id
                or type(reply.get("ok")) is not bool):
            raise SessionError("客户端命令回执身份或结果字段不符")
        if not reply["ok"]:
            error = SessionError("客户端命令失败：" + json.dumps(reply.get("error"), ensure_ascii=False))
            # Node在finally中关闭Files/浏览器/HTTP；等待仍使用本命令原截止。
            try:
                self._wait_exit(deadline)
            except BaseException as closing:
                error.add_note(f"Node错误收场：{closing}")
            error.add_note(f"完整stderr：{self.stderr_path}")
            raise error
        return reply

    def _wait_exit(self, deadline: float):
        assert self.process is not None
        self.process.wait(timeout=self._remaining(deadline))

    def call(self, operation: str, *arguments: str, output: Path) -> dict:
        if self.state != "ready" or not operation or operation == "close":
            raise SessionError(f"会话状态{self.state}不接受此业务命令")
        output = Path(output).resolve()
        if output.exists() or output.is_symlink():
            raise SessionError(f"拒绝把旧文件作为新回执：{output}")
        command_id, started = self.next_id, time.monotonic_ns()
        self.next_id += 1
        deadline = time.monotonic() + _TIMEOUT
        record = {"id": command_id, "operation": operation,
                  "arguments": [str(path) for path in arguments], "receipt_path": str(output)}
        self.metrics["commands"].append(record)
        try:
            reply = self._send(command_id, operation, [*record["arguments"], str(output)], deadline)
            if reply.get("receipt_path") != str(output) or not output.is_file():
                raise SessionError("客户端成功回执没有对应的新文件")
            receipt = json.loads(output.read_text(), parse_float=Decimal)
            if not isinstance(receipt, dict):
                raise SessionError("客户端业务回执不是对象")
            self._remaining(deadline)
            record.update(self._timing(started, "passed"))
            self._save()
            return receipt
        except BaseException as error:
            record.update(self._timing(started, "failed"))
            self._fail(error)
            raise

    def close(self):
        if self.state in ("closed", "failed"):
            return
        started, deadline = time.monotonic_ns(), time.monotonic() + _TIMEOUT
        try:
            reply = self._send(self.next_id, "close", [], deadline)
            if "receipt_path" in reply:
                raise SessionError("close回执不应携带业务文件")
            self._wait_exit(deadline)
            assert self.process is not None
            if self.process.returncode != 0:
                raise SessionError(f"客户端close后退出{self.process.returncode}；{self.stderr_path}")
            survivors = self._survivors()
            if survivors:
                raise SessionError(f"客户端正常close后仍有进程：{survivors}")
            self.state = "closed"
            self.metrics["close"] = self._timing(started, "passed")
            self.metrics["owned_processes"] = [asdict(item) for item in self.owned.values()]
            self._save()
        except BaseException as error:
            self.metrics["close"] = self._timing(started, "failed")
            self._fail(error)
            raise
        finally:
            self._close_connection()

    def _remember(self, identity: _Process):
        self.owned[identity.pid] = identity
        anchors = self.groups.setdefault(identity.pgid, [])
        if identity not in anchors:
            anchors.append(identity)

    def _capture(self):
        """只在收场核验当前子树和TS明确记录的Chrome票据。"""
        table = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    value = _process(int(entry.name))
                except (PermissionError, ProcessLookupError):
                    continue
                if value is not None:
                    table[value.pid] = value
        assert self.process is not None
        current = table.get(self.process.pid)
        if self.process.poll() is None and current is not None:
            remembered = self.owned.get(current.pid)
            if remembered is None or _same(current, remembered):
                self._remember(current)  # 持有的未reap Popen子进程PID不会被复用。
                frontier = [current.pid]
                while frontier:
                    parent = frontier.pop()
                    for item in table.values():
                        if item.ppid == parent:
                            self._remember(item)
                            frontier.append(item.pid)
        if self.node_metrics.exists():
            data = json.loads(self.node_metrics.read_text())
            if data.get("session_id") != self.session_id or data.get("node_pid") != self.process.pid:
                raise SessionError("Node进程归属记录与会话不符")
            for record in data.get("browser_processes", []):
                item = table.get(record["pid"])
                if item and (item.pgid, item.starttime) == (record["pgid"], record["starttime"]):
                    self._remember(item)
        # 已有准确身份仍存在时，该组的新成员仍属于同一持有组。
        for pgid, anchors in list(self.groups.items()):
            if any(_same(table.get(item.pid), item) for item in anchors):
                for item in table.values():
                    if item.pgid == pgid:
                        self._remember(item)

    def _survivors(self) -> list[dict]:
        self._capture()
        return [asdict(current) for item in self.owned.values()
                if _same(current := _process(item.pid), item) and current.state not in ("Z", "X")]

    def _signal_owned(self, signum: int):
        for pgid, anchors in self.groups.items():
            if any(_same(_process(item.pid), item) for item in anchors):
                try:
                    os.killpg(pgid, signum)
                except ProcessLookupError:
                    pass
        # /proc读取失败前也可能已经取得Popen；只能直接给持有的子进程发信号。
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signum)

    def _cleanup(self):
        if self.process is None:
            return
        notes = []
        try:
            self._capture()
        except BaseException as error:
            notes.append(str(error))
            current = _process(self.process.pid)
            if self.process.poll() is None and current is not None:
                self._remember(current)
        for signum, grace in ((signal.SIGTERM, _TERM_GRACE), (signal.SIGKILL, _KILL_GRACE)):
            self._signal_owned(signum)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                self.process.poll()
                try:
                    survivors = self._survivors()
                except BaseException as error:
                    notes.append(str(error))
                    survivors = [{"ownership_check": "failed"}]
                if not survivors and self.process.poll() is not None:
                    break
                time.sleep(.05)
            else:
                continue
            break
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=_KILL_GRACE)
        survivors = self._survivors()
        self.metrics["cleanup"] = {"survivors": survivors, "notes": notes,
                                   "owned_processes": [asdict(item) for item in self.owned.values()]}
        if survivors:
            raise SessionError(f"客户端强制收场后仍有非Z/X进程：{survivors}")

    def _fail(self, error: BaseException):
        self.state = "failed"
        self._close_listener()
        self._close_connection()
        self.metrics["failure"] = f"{type(error).__name__}: {error}"
        try:
            self._cleanup()
        except BaseException as closing:
            error.add_note(f"客户端进程收场错误：{closing}")
        try:
            self._save()
        except BaseException as logging:
            error.add_note(f"客户端计时保存错误：{logging}")
        error.add_note(f"stdout={self.stdout_path}; stderr={self.stderr_path}")

    def _close_listener(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None

    def _close_connection(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
