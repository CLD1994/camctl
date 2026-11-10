"""固定跨轮保留结果的缺口数量必须与可靠结论一致。"""

from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from ..capture.conftest import capture_resources
from ..capture.result_consumer_fixtures import result_driver
from ..capture.test_record_segment_scope_recovery import (
    _completed_result, _entry, _stopped_world, _stop_context,
)
from ..support.result_cases import result_case, result_case_for


@pytest.mark.asyncio
async def test_retained_gap_count_mismatch_rejects_completion(tmp_path, monkeypatch):
    owned, runtime, action_id, handler, _ = await _stopped_world(
        tmp_path, check_config=AttemptConfig(2, Decimal("1"), Decimal(0)))
    context = _stop_context(owned, action_id)
    calls = 0

    async def read(request, batch):
        nonlocal calls
        calls += 1
        index = 7 if calls == 1 else 9
        return _completed_result(request.ticket, context,
            entries=[_entry(f"CAM_001/CAM_20261022100000_{index:04d}_D.OSV")],
            finalized=calls > 1)

    result_driver(result_case_for(), side_effect=read)
    original = CaptureRepository.finish_segment_recording
    observed = []

    def corrupt_gap_count(repository, request, key, actual_owned, **options):
        connection = actual_owned.connection
        failure, = connection.execute(
            "SELECT current_failure_json FROM segment_check_conclusions WHERE set_id=?",
            (request.ref.set_id,)).fetchone()
        assert parse_exact_json(failure)["details"]["gap_count"] == 1
        assert connection.execute("SELECT COUNT(*) FROM segment_gaps WHERE set_id=?",
                                  (request.ref.set_id,)).fetchone() == (1,)
        # 存储守卫反例：模拟固定结论之后的矛盾投影。
        connection.execute("DELETE FROM segment_gaps WHERE set_id=?", (request.ref.set_id,))
        before = connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
        receipt = original(repository, request, key, actual_owned, **options)
        observed.append((receipt, before, connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]))
        return receipt

    monkeypatch.setattr(CaptureRepository, "finish_segment_recording", corrupt_gap_count)
    try:
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        with pytest.raises(ConsistencyError, match="固定集合缺口数量与已保存决定不同"):
            await advance(action_id, runtime)
        assert calls == 2 and len(observed) == 1
        receipt, before, after = observed[0]
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK and after == before
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        owned.connection.close()
