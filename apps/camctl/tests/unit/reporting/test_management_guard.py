"""报告完整状态守卫；登记由受真实接口约束的替身提供。"""

from enum import IntEnum
from importlib.util import find_spec, module_from_spec
import sys
from unittest.mock import create_autospec

import pytest

from camctl.contracts import enums
from camctl.contracts.history_values import TransactionRange
from camctl.history import events
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence import row_history, transaction


@pytest.fixture
def policy(monkeypatch):
    # 协作者先按真实源码加载，避免首次导入把临时登记替身留在生产模块中。
    readers = (transaction.load_enum_registry, transaction.load_event_registry,
               row_history.load_enum_registry, row_history.load_event_registry)
    status = IntEnum("reports.status", {
        "REGISTERED": 1, "PREPARED": 2, "PUBLISHING": 3, "PUBLISHED": 4, "FAILED": 5,
    })
    monkeypatch.setattr(enums, "enum_for", create_autospec(enums.enum_for, return_value=status))
    monkeypatch.setattr(enums, "load_registry", create_autospec(enums.load_registry,
        return_value={"history_objects": {"report": {"id": 6}}}))
    resources = []
    for module in (enums, events):
        reader = create_autospec(module.resource_bytes,
            side_effect=AssertionError("单元测试不能读取真实包资源"))
        resources.append(reader)
        monkeypatch.setattr(module, "resource_bytes", reader)
    # 只提供本函数消费的合法分支约束；真实登记与守卫的组合由集成测试验证。
    branches = {
        "PREPARE": {"reason": 2, "rows": [{"op": "update", "before": {"status": [1, 5]},
            "after": {"status": [2], "last_error_json": [None]}}]},
        "INTENT": {"reason": 3, "rows": [{"op": "update", "before": {"status": [2, 4, 5]},
            "after": {"status": [3]}}]},
        "PUBLISH": {"reason": 4, "rows": [{"op": "update", "before": {"status": [3]},
            "after": {"status": [4], "last_error_json": [None]}}]},
        "FAIL": {"reason": 5, "rows": [{"op": "update", "after": {"status": [5]}}]},
        "RECOVER": {"reason": 6, "rows": [{"op": "update", "before": {"status": [5]},
            "after": {"status": [1, 2, 3]}}]},
    }
    monkeypatch.setattr(events, "load_event_registry", create_autospec(events.load_event_registry,
        return_value={"events": {
            "REPORT_CHANGED": {"id": 28, "branches": branches},
            # policy 模块导入期读取的事件身份；分支约束由集成测试验证。
            "SYNC_CHANGED": {"id": 29, "branches": {}},
            "ACTION_STARTED": {"id": 5, "branches": {}},
            "ACTION_FINISHED": {"id": 8, "branches": {}},
        }}))
    # 每个源码实例及 sys.modules 中原有实例都随 monkeypatch 恢复；不修改登记缓存或守卫表。
    for name in ("camctl.reporting.models", "camctl.persistence.repositories.reporting", "camctl.reporting.policy"):
        spec = find_spec(name)
        module = module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    yield module
    for reader in resources:
        reader.assert_not_called()
    assert readers == (transaction.load_enum_registry, transaction.load_event_registry,
                       row_history.load_enum_registry, row_history.load_event_registry)


def _facts(**overrides):
    return {"status": 1, "size_bytes": None, "sha256": None,
            "publication_count": 0, "last_published_event_id": None,
            "last_error_json": None, **overrides}


@pytest.mark.parametrize("reason,facts,after", [
    (2, _facts(status=4, size_bytes=6, sha256="a" * 64, publication_count=1, last_published_event_id=8),
     {"status": 2}),
    (2, _facts(), {"size_bytes": 6, "sha256": "a" * 64}),
    (3, _facts(status=2, size_bytes=6, sha256="a" * 64), {"last_error_json": None}),
    (5, _facts(status=2, size_bytes=6, sha256="a" * 64), {"last_error_json": {"reason": "failure"}}),
    (6, _facts(), {"status": 2, "size_bytes": 6, "sha256": "a" * 64}),
])
def test_report_guard_validates_full_branch_states_when_changed_fields_are_partial(policy, reason, facts, after):
    event = EventEnvelope(10, 2, 28, 1, 1, 2, None, reason, {}, (
        RowChange("reports", 1, RowImage(True, {k: facts[k] for k in after}), RowImage(True, after)),
    ))
    context = EventContext(TransactionRange(2, 10, 10), {("reports", 1): ("report", 1)},
                           {"reports": {1: facts}})
    with pytest.raises(EventValidationError):
        policy._report_guard(event, context)


def test_report_guard_accepts_status_only_failure_with_existing_error(policy):
    facts = _facts(status=3, size_bytes=6, sha256="a" * 64, last_error_json={"reason": "failure"})
    event = EventEnvelope(10, 2, 28, 1, 1, 2, None, 5, {}, (
        RowChange("reports", 1, RowImage(True, {"status": 3}), RowImage(True, {"status": 5})),
    ))
    context = EventContext(TransactionRange(2, 10, 10), {("reports", 1): ("report", 1)},
                           {"reports": {1: facts}})
    policy._report_guard(event, context)
