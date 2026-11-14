"""预算不可用时，容量准备保持未启动。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from integration.persistence import segment_capacity_case
from integration.persistence import test_segment_transaction_batches as batches


@pytest.mark.asyncio
async def test_capacity_budget_is_checked_before_world_preparation(monkeypatch):
    class UnavailableBudget(RuntimeError):
        pass

    def unavailable(budget):
        raise UnavailableBudget("controlled unavailable controller")

    async def forbidden_preparation(*args, **kwargs):
        raise AssertionError("预算不可用时禁止昂贵准备")

    backend = SimpleNamespace(get_budget_config=lambda: object(),
                              ensure_memory_budget_available=unavailable)
    monkeypatch.setattr(segment_capacity_case, "memory_budget_backend", lambda: backend)
    monkeypatch.setattr(batches, "_ready_world", forbidden_preparation)
    with pytest.raises(UnavailableBudget):
        await batches._run_fixed_batch_capacity(Path("/unused"))
