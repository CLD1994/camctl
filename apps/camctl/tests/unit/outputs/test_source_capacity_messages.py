"""容量进程的完整结果协议和预算预检，不执行真实容量工作。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from integration.support import source_capacity


def _message():
    manifest = {"version": 1, "database": "/state.db", "key": "original-key",
                "request": {"action_id": 21, "spec": {"plan_instance_id": 1}},
                "batch_size": 4, "count": 20}
    message = {key: manifest[key] for key in
               ("version", "database", "key", "request", "batch_size")}
    message.update(phase="result", stage="reuse", kind="completed", seconds=0.1,
                   value={"fixed": True, "member_count": 20, "selection_count": 20,
                          "source_plan_id": 1, "error_code": None, "error_details": None})
    return manifest, message


def test_complete_original_result_is_accepted():
    manifest, message = _message()
    source_capacity.validate_stage_message(message, manifest, "reuse")


@pytest.mark.parametrize("damage", ["missing", "key", "request", "kind", "value", "stage"])
def test_exit_success_cannot_replace_complete_result(damage):
    manifest, message = _message()
    message = deepcopy(message)
    if damage == "missing":
        del message["value"]
    elif damage == "request":
        message["request"]["action_id"] += 1
    elif damage == "value":
        message["value"]["selection_count"] -= 1
    else:
        message[damage] = "different"
    with pytest.raises(AssertionError):
        source_capacity.validate_stage_message(message, manifest, "reuse")


@pytest.mark.asyncio
async def test_unavailable_budget_prevents_world_preparation(monkeypatch):
    class UnavailableBudget(RuntimeError):
        pass

    def unavailable(_budget):
        raise UnavailableBudget("controlled unavailable controller")

    async def forbidden(_directory):
        raise AssertionError("预算不可用时不能准备来源世界")

    monkeypatch.setattr(source_capacity, "memory_budget_backend", lambda: SimpleNamespace(
        get_budget_config=lambda: object(), ensure_memory_budget_available=unavailable))
    monkeypatch.setattr(source_capacity, "prepare_case", forbidden)
    with pytest.raises(UnavailableBudget):
        await source_capacity.run_capacity(Path("unused"))
