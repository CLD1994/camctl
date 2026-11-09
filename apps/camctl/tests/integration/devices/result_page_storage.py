"""驱动页集成只装配公开仓储和实际业务数据库，不创建拍摄运行对象。"""
from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import PurePosixPath
import re
import shlex

from camctl.operations.attempts import AttemptConfig
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.transaction import row_facts
from ..support.capture import capture_database


@dataclass
class ResultPageStorage:
    owned: object
    database: object
    now: int
    capture: CaptureRepository = field(default_factory=CaptureRepository)
    check_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(3, Decimal("10"), Decimal("0")))

    def wall_us(self):
        return self.now

    def action(self, action_id):
        return row_facts(self.owned.connection, "actions", action_id)


async def result_page_storage(owned, now):
    return ResultPageStorage(owned, await capture_database(owned), now)


def completion_context(owned, action_id, *, kind=None):
    """按已保存的停止结果格式提交驱动契约所需上下文。"""
    row = owned.connection.execute(
        "SELECT r.activity_id,a.status,a.effect_state,a.result_event_id,a.error_json,a.result_json,e.occurred_at"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " JOIN history_events e ON e.id=a.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY a.id DESC LIMIT 1", (f"stop/{action_id}",)).fetchone()
    activity, status, effect, event, error, result, occurred_at = row
    action = row_facts(owned.connection, "actions", action_id)
    context = {"activity_id": str(activity), "stop_result_event_id": event,
        "stop_returned_at_us": occurred_at,
        "file_completion_wait_ms": action["execution_spec_json"]["file_completion_wait_ms"],
        "prior_completion": None, "stop_response": {
            "status": status, "effect_state": effect,
            "error": None if error is None else json.loads(error), "result": json.loads(result)}}
    if kind is not None:
        context["kind"] = kind
    return context


async def allow_dispatch():
    return None


class ResultPageTransport:
    """只实现结果页所需的 ADB 控制、目录和元数据工具回执。"""

    def __init__(self):
        self.paths = []
        self.metadata_errors = set()
        self.directory_calls = []

    @staticmethod
    def _script(spec):
        assert spec.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
        shell = shlex.split(spec.argv[5])
        assert shell[:2] == ["sh", "-c"] and len(shell) == 3
        assert spec.timeout_s > 0
        return shell[2]

    @staticmethod
    def _raw(output, *, code=0, stderr=b""):
        return RawToolOutcome(LocalExit(exit_code=code), output, None, None, stderr=stderr)

    def _directory(self, script):
        line = next(line for line in script.splitlines() if line.startswith("find "))
        root = shlex.split(line)[1]
        return root, sorted((path for path in self.paths if path.startswith(root + "/")),
                            key=lambda path: path.encode("utf-8"))

    async def run_stream(self, spec, stop, receive):
        script = self._script(spec)
        assert "CAMCTL-DIRECTORY/1" in script
        root, paths = self._directory(script)
        self.directory_calls.append((root, None, None))
        frame = b"\0".join((b"CAMCTL-DIRECTORY/1", *(path.encode() for path in paths), b"END", b""))
        for index in range(0, len(frame), 17):
            await receive(frame[index:index + 17])
        return self._raw(b"")

    async def run(self, spec, stop):
        script = self._script(spec)
        if "CAMCTL-DIRECTORY/1" in script:
            root, paths = self._directory(script)
            assignment = next(line for line in script.splitlines() if line.startswith("after="))
            after = shlex.split(assignment)[0].removeprefix("after=")
            batch = int(re.search(r'\[ "\$count" -ge (\d+) \]', script).group(1))
            remaining = [path for path in paths if not after or path.encode() > after.encode()]
            selected = remaining[:batch]
            self.directory_calls.append((root, after or None, batch))
            marker = b"MORE" if len(remaining) > len(selected) else b"END"
            return self._raw(b"\0".join((b"CAMCTL-DIRECTORY/1", *(path.encode() for path in selected), marker, b"")))
        if "CAMCTL-FAMILY/1" in script:
            assignment = next(line for line in script.splitlines() if line.startswith("stem="))
            stem = shlex.split(assignment)[0].removeprefix("stem=")
            paths = sorted((path for path in self.paths if str(PurePosixPath(path).with_suffix("")) == stem),
                           key=lambda path: path.encode("utf-8"))
            return self._raw(b"\0".join((b"CAMCTL-FAMILY/1", *(path.encode() for path in paths), b"END", b"")))
        if "CAMCTL-METADATA/2" in script:
            lexer = shlex.shlex(script, posix=True, punctuation_chars=";")
            lexer.whitespace_split = True
            tokens = list(lexer)
            start = tokens.index("for") + 3
            paths = tokens[start:tokens.index(";", start)]
            records = []
            for path in paths:
                failed = path in self.metadata_errors or path not in self.paths
                records.extend((path.encode(), b"" if failed else b"10\n",
                                b"metadata failed" if failed else b"", b"1" if failed else b"0"))
            return self._raw(b"\0".join((b"CAMCTL-METADATA/2", *records, b"END", b"")))
        if script.startswith("test -f "):
            path = shlex.split(script)[2]
            failed = path in self.metadata_errors or path not in self.paths
            return self._raw(b"" if failed else b"10\n", code=1 if failed else 0,
                             stderr=b"metadata failed" if failed else b"")
        assert shlex.split(script)[0] in ("dji_mb_ctrl", "simulate_device")
        return self._raw(b"Resp message, len = 1, data:\n 00\n")
