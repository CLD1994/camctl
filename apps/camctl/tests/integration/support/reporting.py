"""报告测试的正式会话、生成和文件发布入口。"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import asyncio
import sqlite3

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state


@dataclass(frozen=True)
class ReportWorld:
    """各场景独占部署路径；会话继续使用默认生产流程。"""

    home: Path
    config: object

    @property
    def database_path(self):
        return Path(self.config.paths.state_db)

    @property
    def ready(self):
        return Path(self.config.paths.ready)

    @property
    def staging(self):
        return Path(self.config.paths.staging)

    @property
    def processing(self):
        return Path(self.config.paths.processing)


def report_config(home: Path):
    """按独占路径创建报告默认会话配置，已有世界重开复用同一规则。"""
    return load_config({"paths": {
        "state_db": str(home / "state.db"), "log_file": str(home / "camctl.log"),
        "staging": str(home / "staging"), "ready": str(home / "ready"),
        "processing": str(home / "processing"),
    }, "devices": {}}, ConfigDefaults())


def report_world(home: Path) -> ReportWorld:
    """初始化真实数据库、目录和锁，测试不自制报告流程。"""
    home.mkdir(parents=True, exist_ok=True)
    config = report_config(home)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    return ReportWorld(home, config)


async def run_report_session(world: ReportWorld, source=None, *, config=None):
    """经完整默认装配执行会话，并回收该会话拥有的资源。"""
    deps = build_runtime(CommandMode.RUN, config or world.config)
    try:
        return await asyncio.wait_for(execute_command(deps, source), timeout=60)
    finally:
        close_runtime(deps)


@contextmanager
def deny_storage_access(connection, operation, table, column=None):
    """在 SQLite 实际访问边界拒绝指定表或字段，不依赖 SQL 文本。"""
    observed = []

    def authorize(action, first, second, database, trigger):
        if (action == operation and first == table
                and (column is None or second == column)):
            observed.append((action, first, second))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        yield observed
    finally:
        connection.set_authorizer(None)


async def complete_photo(owned, *, action_id=1, results=None):
    """正式调度登记后通过工厂装配和 C1 处理器完成单张拍摄。"""
    import time
    from camctl.capture.handlers import capture_handler
    from camctl.capture.results import FileKind
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
    from .capture import capture_runtime
    from ..capture.test_capture_contract import ResultsDouble, _entry
    from ..history.test_report_scope import _ActivityDriver, _PAGE_EVIDENCE

    now = time.time_ns() // 1000
    started = SchedulingRepository().start_action(
        StartActionRequest(action_id, now, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    port = results if results is not None else ResultsDouble({
        activity_id: (_entry("shot-1", kind=FileKind.PHOTO),)})
    runtime = await capture_runtime(owned, driver=_ActivityDriver(str(activity_id)),
        evidence=_PAGE_EVIDENCE, results=port,
        wall_us=lambda: time.time_ns() // 1000, monotonic_ns=time.monotonic_ns)
    await capture_handler("camera_take_photo")(action_id, runtime)
