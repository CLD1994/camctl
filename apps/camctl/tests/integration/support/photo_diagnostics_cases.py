"""真实 Hasselblad 诊断用例的正式装配、原结果读取和仓储回执。"""

from dataclasses import dataclass, replace
from pathlib import Path

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.drivers.registry import DriverRegistry
from camctl.devices.evidence import EvidenceRegistry
from camctl.host_files.models import BoundDirectories
from camctl.logging_runtime.diagnostics import flush_diagnostics
from camctl.operations.result_format import read_result_document
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository

from .capture import capture_runtime, restart_capture_database
from ..capture.test_host_photo_flow import _ManagedCaptureTransport, _entry, _world


async def actual_photo_world(tmp_path, *, evidence=None):
    transport = _ManagedCaptureTransport()
    run = transport.run

    async def with_stderr(spec, stop):
        raw = await run(spec, stop)
        raw = replace(raw, stderr=b"hb diagnostic\xff\n")
        transport.outcomes[-1] = raw
        return raw

    transport.run = with_stderr

    def make_driver(config):
        return HasselbladDriver(config.devices, BoundDirectories(Path(config.paths.staging)),
                                monotonic_ns=lambda: 1, transport=transport)

    world = await _world(tmp_path, driver_factory=make_driver, evidence=evidence)
    return (*world, transport)


async def advance_photo(runtime):
    try:
        return await capture_handler("camera_take_photo")(1, runtime)
    finally:
        await flush_diagnostics()


def start_row(owned):
    """按正式存储格式读取原 START，排除准备责任的独立尝试。"""
    cursor = owned.connection.execute(
        "SELECT a.id,a.run_id,a.attempt_no,a.status,a.effect_state,a.result_json,"
        "a.error_json,a.result_event_id,e.occurred_at FROM operation_attempts a "
        "JOIN operation_runs r ON r.id=a.run_id "
        "LEFT JOIN history_events e ON e.id=a.result_event_id "
        "WHERE r.responsibility_key='start/1' ORDER BY a.attempt_no")
    rows = cursor.fetchall()
    assert len(rows) == 1, rows
    return dict(zip((item[0] for item in cursor.description), rows[0]))


def start_outcome(owned):
    row = start_row(owned)
    return read_result_document(row["status"], row["effect_state"],
        parse_exact_json(row["result_json"]),
        None if row["error_json"] is None else parse_exact_json(row["error_json"]))


def assert_original_streams(outcome, transport, owned):
    # C1 把前八步准备与最后一步 START 分别保存，九次实际调用逐项核对。
    row, = owned.connection.execute(
        "SELECT a.status,a.effect_state,a.result_json,a.error_json "
        "FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id "
        "WHERE r.kind=?", (int(enum_for("operation_runs.kind").PREPARE_CAPTURE),)).fetchall()
    preparation = read_result_document(row[0], row[1], parse_exact_json(row[2]),
        None if row[3] is None else parse_exact_json(row[3]))
    calls = (*preparation.diagnostics.calls, *outcome.diagnostics.calls)
    assert [call.stdout.data for call in calls] == [raw.output for raw in transport.outcomes]
    assert [call.stderr.data for call in calls] == [raw.stderr for raw in transport.outcomes]


@dataclass(frozen=True)
class StartSave:
    finish: object
    observation: object
    key: str
    options: dict
    receipt: object


class StartReceipts:
    """仅在公开原结果事务回执边界制造未知提交，真实提交保持生产实现。"""

    def __init__(self, monkeypatch, *, unknown=None):
        self.calls = []
        original = CaptureRepository.finish_start_result

        def save(repository, finish, observation, key, owned, **options):
            hide = unknown is not None and len(self.calls) < 2
            receipt = (DbOutcome(DbOutcomeKind.UNKNOWN) if hide and unknown == "before"
                else original(repository, finish, observation, key, owned, **options))
            if hide and unknown == "after":
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                receipt = DbOutcome(DbOutcomeKind.UNKNOWN)
            self.calls.append(StartSave(finish, observation, key, options, receipt))
            return receipt

        monkeypatch.setattr(CaptureRepository, "finish_start_result", save)

    def assert_same_original(self, actual, *, occurred_at):
        first = self.calls[0]
        assert first.finish.outcome.outcome is actual
        assert first.finish.occurred_at == occurred_at
        for call in self.calls:
            assert call.key == first.key
            assert call.finish is first.finish
            assert call.observation == first.observation
            assert call.options == first.options


async def cold_photo_runtime(cfg, owned_or_path, clock):
    class NoDevice:
        async def control(self, request):
            raise AssertionError("恢复不得重放 START")

    driver = NoDevice()
    database = await restart_capture_database(owned_or_path)
    return await capture_runtime(database, driver=driver, evidence=EvidenceRegistry(()),
        devices=cfg.devices, drivers=DriverRegistry((_entry(driver, EvidenceRegistry(())),)),
        staging=Path(cfg.paths.staging), wait_config=lambda _action: None,
        wall_us=lambda: clock[0], monotonic_ns=lambda: 99_000_000_000)
