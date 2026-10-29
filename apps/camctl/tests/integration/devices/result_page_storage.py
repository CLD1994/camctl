"""驱动页集成只装配公开仓储和实际业务数据库，不创建拍摄运行对象。"""
from dataclasses import dataclass, field
from decimal import Decimal
import json

from camctl.operations.attempts import AttemptConfig
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


class SavedDirectoryScan:
    """目录端口替身固定本轮清单；文件元数据仍由设备替身的当前状态提供。"""
    def __init__(self, filesystem):
        self.paths = tuple(filesystem.paths)
        self.calls = filesystem.directory_calls
        self.closed = False

    async def read_directory(self, request, *, stop):
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryCursor, DirectoryRead
        from camctl.devices.file_identity import FileIdentity

        assert not self.closed
        self.calls.append(request)
        after = None if request.cursor is None else request.cursor.after_path
        remaining = [path for path in self.paths if after is None or path > after]
        selected = remaining[:request.batch]
        cursor = (DirectoryCursor(request.binding, request.directories, 0, selected[-1])
                  if len(remaining) > len(selected) else None)
        return DirectoryRead(None, None, Page(tuple(
            FileIdentity(request.binding, path) for path in selected), cursor))

    def close(self):
        self.closed = True
