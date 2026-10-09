"""有限 RESULTS 耗尽的完整收场申请跨真实 UNKNOWN 提交恢复。"""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultSetPhase
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import consumer_world
from .test_record_media_result_settlement import _TrackedCommitFailure
from .test_result_consumer_saves import _result_port

pytestmark = pytest.mark.asyncio


class _SavedBeforeBusiness(Exception):
    """实际仓储可靠保存后截停，独立检查依赖业务尚未执行。"""


@pytest.mark.parametrize("consumer", ["photo", "timelapse"])
@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_exhaustion_unknown_replays_original_request_time_before_business(
    tmp_path, monkeypatch, consumer, after,
):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, consumer, independent_activity=True)
    reopened = None
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert action_id != activity_id
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": [{"identity": "auxiliary",
                    "kind": "other", "complete": True, "size_bytes": 41,
                    "locator": {"path": "/DCIM/auxiliary"}}]}),))
        driver = _result_port(runtime, actual)
        runtime.check_config = AttemptConfig(1, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        run_id, = owned.connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        original_attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == (2, 1, 1)
        original_files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        assert len(original_files) == 1
        original_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        database_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        metadata = owned.metadata
        formed_at = runtime.wall_us() + 1_000_000
        runtime.wall_us = lambda: formed_at
        original_save = CaptureRepository.close_result_check_unconfirmed
        original_finish = CaptureRepository.finish_capture
        inputs, outcomes, proxies, business = [], [], [], []

        def save(repository, request, key, current):
            inputs.append((request, key))
            if len(inputs) == 1:
                proxy = _TrackedCommitFailure(current.connection, after)
                proxies.append(proxy)
                current = replace(current, connection=proxy)
            outcome = original_save(repository, request, key, current)
            outcomes.append(outcome)
            if len(inputs) > 1 and outcome.kind is DbOutcomeKind.COMPLETED:
                raise _SavedBeforeBusiness
            return outcome

        def finish(repository, request, key, current):
            business.append((request, key))
            return original_finish(repository, request, key, current)

        monkeypatch.setattr(CaptureRepository, "close_result_check_unconfirmed", save)
        monkeypatch.setattr(CaptureRepository, "finish_capture", finish)
        with pytest.raises((AssertionError, ConsistencyError)):
            await advance(action_id, runtime)
        assert len(inputs) == len(outcomes) == len(proxies) == 1
        assert proxies[0].commit_calls == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
        original_request, original_key = inputs[0]
        expected_error = {"code": "capture_result_unconfirmed",
            "stage": registered_error("capture_result_unconfirmed")["stage"],
            "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}
        assert original_request.action_id == action_id
        assert original_request.occurred_at == formed_at
        assert original_request.phase is ResultSetPhase.UNCONFIRMED
        assert original_request.contract == "task_scope_files"
        assert original_request.observation == {"reason": "attempts_exhausted"}
        assert original_request.capture == {"status": "unconfirmed", "error": expected_error}
        assert original_request.error == expected_error
        assert business == []

        # 原连接关闭后才判断可靠 F；before 关闭回滚，after 保留完整原组。
        owned.connection.close()
        reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == metadata
        transaction = reopened.connection.execute(
            "SELECT id FROM history_transactions WHERE operation_key=?", (str(original_key),)).fetchone()
        assert (transaction is not None) is after
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert reopened.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == ((6, 1, 0) if after else (2, 1, 1))
        assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == original_attempts
        assert reopened.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == original_files
        assert reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(original_history)] == original_history

        # replace 保留全部原同会话集合；不构造不存在的结果类型或复制 raw 输入。
        # 普通资格可以读取当前钟；完整原申请仍须保持 T1，不能用这个新时刻重形成。
        resumed = replace(runtime, owned=reopened, wall_us=lambda: formed_at + 5_000_000)
        with pytest.raises(_SavedBeforeBusiness):
            await advance(action_id, resumed)

        assert inputs == [(original_request, original_key), (original_request, original_key)]
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
        assert business == []
        assert driver.list_results.await_count == 1
        runtime.driver.control.assert_awaited_once()
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(original_key),)).fetchone() == (1,)
        assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == original_attempts
        assert reopened.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == original_files
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        capture, error = reopened.connection.execute(
            "SELECT capture_json,last_error_json FROM device_activities WHERE id=?", (activity_id,)).fetchone()
        assert json.loads(capture) == {"status": "unconfirmed", "error": expected_error}
        assert json.loads(error) == expected_error
    finally:
        if reopened is not None:
            reopened.connection.close()
        else:
            owned.connection.close()
