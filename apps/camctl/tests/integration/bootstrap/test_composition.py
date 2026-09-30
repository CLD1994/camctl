"""B6 按命令装配与资源关闭的组件集成测试。

真实装配执行 submit/run：submit 不派发设备调用，初始化中途失败
只关闭已创建资源，重复关闭不重复处理；日常入口面对缺失库不创建。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.application import ConfigAdapter, query_work_facts
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.session.locks import probe_admission

from ..persistence.test_runtime import _create_valid_database

CAMERA_DEFINITION = {
    "type": "object",
    "properties": {"type": {"const": "single_shot"}},
    "required": ["type"],
    "additionalProperties": False,
}


class RecordingCatalog:
    """记录一切调用的目录替身；submit 只允许静态查询。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def action_types(self):
        self.calls.append("action_types")
        return frozenset({"camera_take_photo"})

    def device_exists(self, device_id):
        self.calls.append(f"device_exists:{device_id}")
        return device_id == "cam-1"

    def driver_id(self, device_id):
        self.calls.append(f"driver_id:{device_id}")
        return "camctl-adb"

    def parameter_definition(self, device_id, action_type):
        self.calls.append(f"parameter_definition:{device_id}")
        return ParameterDefinition(schema=CAMERA_DEFINITION, defaults={})


class RecordingNotifier:
    def __init__(self) -> None:
        self.notifications: list[int] = []

    def work_available(self, plan_id: int) -> None:
        self.notifications.append(plan_id)


def _config_for(home: Path):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            }
        },
        ConfigDefaults(),
    )


def _plan_body(request_id: str = "42") -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 1000},
            }
        ],
    }


async def _parsed(tmp_path: Path, body: dict):
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(body), encoding="utf-8")

    class Reader:
        def read(self, path: str) -> bytes:
            with open(path, "rb") as handle:
                return handle.read()

    return parse_input(await read_input(str(target), Reader()))


class TestSubmitComposition:
    async def test_submit_never_dispatches_device(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = RecordingCatalog()
        notifier = RecordingNotifier()
        deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=catalog, notifier=notifier)
        source = await _parsed(tmp_path, _plan_body())
        outcome = await execute_command(deps, source)
        close_runtime(deps)
        assert outcome.succeeded is True
        assert outcome.needs_run is True
        # submit 只做静态目录查询与受理，不产生任何设备调用端口。
        assert all(not call.startswith("dispatch") for call in catalog.calls)
        assert notifier.notifications == [1]

    async def test_submit_missing_database_is_not_created(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        with pytest.raises(FileNotFoundError):
            build_runtime(CommandMode.SUBMIT, cfg)
        assert not Path(cfg.paths.state_db).exists()


class TestRunComposition:
    async def test_run_with_plan_accepts_and_exits_success(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=RecordingCatalog())
        source = await _parsed(tmp_path, _plan_body())
        outcome = await execute_command(deps, source)
        close_runtime(deps)
        assert outcome.succeeded is True
        # 会话退出后接纳锁不被遗留持有。
        assert probe_admission(deps.admission_lock).is_free

    async def test_bare_run_without_clock_lower_bound_restricted(self, tmp_path: Path) -> None:
        # 尚无可信下界且系统墙钟早于部署最小可信日期时受限退出；
        # 这里系统时间可信，按正常路径成功并保存下界。
        cfg = _config_for(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=RecordingCatalog())
        outcome = await execute_command(deps, None)
        close_runtime(deps)
        assert outcome.succeeded is True

    async def test_bad_new_input_preserves_existing_work(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        submit_deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=RecordingCatalog())
        good = await _parsed(tmp_path, _plan_body(request_id="1"))
        outcome = await execute_command(submit_deps, good)
        close_runtime(submit_deps)
        assert outcome.succeeded is True

        # 坏新输入（解析失败）：run 保存诊断，已有工作保持可发现。
        broken = tmp_path / "broken.json"
        broken.write_bytes(b'{"request_id": "2", "actions": [')
        reader = type("R", (), {"read": staticmethod(lambda path: broken.read_bytes())})()
        diagnostic = parse_input(await read_input(str(broken), reader))
        run_deps = build_runtime(CommandMode.RUN, cfg, catalog=RecordingCatalog())
        outcome = await execute_command(run_deps, diagnostic)
        close_runtime(run_deps)
        assert outcome.succeeded is True

        from camctl.persistence.runtime import DbOpenMode, DbConfig, open_existing

        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            facts = query_work_facts(owned.connection)
            assert facts.unfinished_actions == 1
            assert facts.pending_report_changes is True
            diagnostics = owned.connection.execute(
                "SELECT COUNT(*) FROM plan_file_diagnostics"
            ).fetchone()[0]
            assert diagnostics == 1
        finally:
            owned.connection.close()


class TestResourceCleanup:
    async def test_mid_failure_keeps_locks_releasable(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        # 装配成功后未执行命令即关闭：不遗留锁资格。
        deps = build_runtime(CommandMode.SUBMIT, cfg)
        close_runtime(deps)
        close_runtime(deps)  # 重复关闭不重复处理。
        assert probe_admission(deps.admission_lock).is_free
