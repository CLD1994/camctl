"""验证真实模块依赖及纯规则的外部调用边界。"""

from __future__ import annotations

import builtins
import socket
import sqlite3
import subprocess
from decimal import Decimal

import pytest

from support.dependency_layers import _forbidden_edges


@pytest.fixture(scope="module")
def import_graph(source_inspection) -> dict[str, dict[str, set[str]]]:
    """每条依赖规则读取相同的真实源码导入图。"""
    return {
        name: facts["imports"]
        for name, facts in source_inspection.items()
    }


class TestImportGraph:
    def test_graph_is_not_empty(self, import_graph) -> None:
        """检查对象不是空包：真实源码被完整扫描。"""
        graph = import_graph
        assert len(graph) >= 100
        for anchor in (
            "camctl.contracts.public_projection",
            "camctl.persistence.runtime",
            "camctl.persistence.repositories.history",
            "camctl.reporting.worker",
            "camctl.logging_runtime.service",
            "camctl.bootstrap.lifecycle",
        ):
            assert anchor in graph, f"导入图缺少锚点模块: {anchor}"

    def test_rules_do_not_import_adapters(self, import_graph) -> None:
        """真实导入图不含任何禁止边。"""
        assert _forbidden_edges(import_graph) == []


class TestPureRulesMakeNoExternalCalls:
    def test_external_interfaces_not_called_by_rules(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """运行规则函数时外部接口替身拒绝调用。"""
        external_calls: list[str] = []

        def reject(name: str):
            def guard(*args, **kwargs):
                external_calls.append(name)
                raise AssertionError(f"规则函数不允许外部调用: {name}")

            return guard

        monkeypatch.setattr(builtins, "open", reject("open"), raising=False)
        monkeypatch.setattr(
            sqlite3, "connect", reject("sqlite3.connect"), raising=False
        )
        monkeypatch.setattr(
            subprocess, "Popen", reject("subprocess.Popen"), raising=False
        )
        monkeypatch.setattr(
            socket, "socket", reject("socket.socket"), raising=False
        )

        from camctl.contracts.history_values import (
            HistoryBoundary, TransactionRange, validate_boundary,
        )
        from camctl.contracts.json_values import (
            is_json_integer, json_equal, parse_exact_json,
        )
        from camctl.contracts.pages import Page

        value = parse_exact_json('{"n": 1.0000000000000001}')
        assert json_equal(value, {"n": Decimal("1.0000000000000001")})
        assert is_json_integer(Decimal("1e0")) is True
        transaction = TransactionRange(
            txn_id=1, first_event_id=201, last_event_id=203
        )
        validate_boundary(HistoryBoundary(1, 203), transaction)
        page = Page(items=(1, 2), next_cursor=5)
        assert page.exhausted is False
        assert external_calls == []
