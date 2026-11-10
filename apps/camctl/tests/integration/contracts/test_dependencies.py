"""K5 实际模块依赖验证的集成测试。

解析真实源码的 AST 导入关系（相对导入、别名与多行括号形式都在
语法层解决），按共享层、规则与业务流程、适配器、仓储与装配入口
的方向检查禁止边；确实需要的依赖以有说明的允许边表达。检查器面
对合成违规样本必须能够报出边；纯规则函数在外部接口替身拒绝调用
的环境下运行，验证不发生外部调用。
"""

from __future__ import annotations

import ast
import builtins
import socket
import sqlite3
import subprocess
from dataclasses import dataclass
from decimal import Decimal

import pytest

#: 业务流程模块（module-contracts.md 依赖图中的 U 层）。
FLOW_PREFIXES = (
    "camctl.acceptance",
    "camctl.session",
    "camctl.scheduling",
    "camctl.capture",
    "camctl.outputs",
    "camctl.cancellation",
    "camctl.reporting",
    "camctl.history",
    "camctl.motor.service",
    "camctl.motor.rules",
)
#: 设备与日志适配器实现（图中的 A 层；文件执行 host_files 归共享
#: 基础设施）。适配器实现之间互不依赖。
ADAPTER_PREFIXES = (
    "camctl.logging_runtime",
    "camctl.devices",
)
#: 共享端口、契约与文件基础设施（图中的 P 层）：操作契约与工具
#: 执行、读取会话端口、任务监督、文件交接与读取、参数规则校验、
#: 受理 schema 规则。适配器与业务流程都可依赖这一层。
PORT_PREFIXES = (
    "camctl.operations",
    "camctl.devices.read_session",
    "camctl.devices.evidence",
    "camctl.session.supervision",
    "camctl.host_files",
    "camctl.devices.parameter_schemas",
    "camctl.acceptance.schema",
    "camctl.motor.notification",
    "camctl.logging_runtime.diagnostics",
    "camctl.logging_runtime.performance",
    "camctl.logging_runtime.events",
    "camctl.logging_runtime.models",
)
#: 装配入口与装配辅助：创建实际资源并传入流程，允许接触 SQLite、
#: 配置与锁实现。报告生成子进程入口 worker 在自己的进程内承担同
#: 一装配角色；设备目录构建从配置与部署定义组装受理目录。
ENTRY_PREFIXES = (
    "camctl.bootstrap",
    "camctl.cli",
    "camctl.persistence.initialization",
    "camctl.reporting.worker",
    "camctl.devices.catalog",
)
#: 允许边：业务流程以连接类型做签名注解、以运行库异常类型做错误
#: 分类，不打开连接。
RUNTIME_TYPE_ONLY = {"OwnedConnection", "RuntimeLibraryError"}
#: 流程提交完整操作和接手取消所使用的公开异步端口；不包含执行器构造。
BUSINESS_PORT_NAMES = {"BusinessDatabase", "SharedSave", "settle_required"}
#: 历史事件应用与公开投影所在的模块。
HISTORY_APPLY_PREFIXES = (
    "camctl.contracts.public_projection",
    "camctl.persistence.row_history",
    "camctl.persistence.repositories.transactions",
)
#: 共享层可以依赖的同层基础设施：包内权威资源读取。
SHARED_ALLOWED = ("camctl.contracts", "camctl.resources")


def _import_key(module: str) -> str:
    """内部依赖保留完整路径；外部依赖归并到顶级名。"""
    if module.startswith("camctl"):
        return module
    return module.split(".")[0]


def _imported_modules(name: str, source: str | ast.AST) -> dict[str, set[str]]:
    """解析一个模块源码的导入：被导入模块 → from-import 名字。

    普通导入语句没有名字级信息，映射到空集合；``from camctl import x``
    展开为 ``camctl.x``。相对导入按源模块的包层级解析成绝对名。
    """
    tree = ast.parse(source) if isinstance(source, str) else source
    imports: dict[str, set[str]] = {}
    package = name.rsplit(".", 1)[0] if "." in name else name

    def record(module: str, names) -> None:
        key = _import_key(module)
        target = imports.setdefault(key, set())
        if key.startswith("camctl"):
            target.update(names)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name, ())
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")
                drop = node.level - 1
                if drop > len(parts):
                    continue
                anchor = ".".join(parts[: len(parts) - drop])
                base = f"{anchor}.{node.module}" if node.module else anchor
            if not base:
                continue
            names = tuple(
                alias.name for alias in node.names if alias.name != "*"
            )
            record(base, names)
            if base.startswith("camctl"):
                for member in names:
                    record(f"{base}.{member}", ())
    return imports


@pytest.fixture(scope="module")
def import_graph(source_inspection) -> dict[str, dict[str, set[str]]]:
    """每条依赖规则读取相同的真实源码导入图。"""
    return {
        name: facts["imports"]
        for name, facts in source_inspection.items()
    }


def _starts_with(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in prefixes
    )


#: 业务流程可依赖的持久化层：类型、事务、历史行应用与仓储实现。
_PERSISTENCE_ALLOWED = (
    "camctl.persistence.models",
    "camctl.persistence.repositories.transactions",
    "camctl.persistence.row_history",
    "camctl.persistence.repositories",
)


def _check_edge(name: str, target: str, names: set[str]) -> list[str]:
    """对一条内部边应用依赖方向规则；返回违规描述。"""
    if _starts_with(name, ENTRY_PREFIXES):
        return []
    # 有限检查、原结果保存和目录绑定各自使用的窄协作端口。
    named_ports = {
        ("camctl.operations.availability", "camctl.persistence.business"): "SharedSave",
        ("camctl.operations.availability_owner", "camctl.persistence.business"): "SharedSave",
        ("camctl.operations.availability", "camctl.scheduling.notifications"): "WakeReason",
        ("camctl.outputs.withdrawal", "camctl.persistence.runtime"): "canonical_directory_binding",
        ("camctl.history.bounded", "camctl.persistence.transaction_data"): "encode_json_value",
        ("camctl.operations.availability_rules", "camctl.devices.drivers.hasselblad.definitions"): "DRIVER_ID",
    }
    for (source, module), symbol in named_ports.items():
        if name == source and (
            (target == module and names and names <= {symbol})
            or (target == f"{module}.{symbol}" and not names)
        ):
            return []
    # 该历史仓储适配器只创建可重建的派生库；不授予整个 history 包
    # SQLite 权限，也不允许它直接打开业务状态库。
    if name in {"camctl.history.bounded", "camctl.reporting.failure_index"} and (
        target == "sqlite3" or _starts_with(target, (
            "camctl.persistence.derived_storage", "camctl.persistence.row_validation",
        ))
    ):
        return []
    if name in {"camctl.logging_runtime.diagnostics", "camctl.logging_runtime.performance"} and _starts_with(
        target, ("camctl.logging_runtime.models", "camctl.logging_runtime.service",
                 "camctl.logging_runtime.admission"),
    ):
        return []
    # 日志入口实现使用本模块的编码与回执，不向其他消费者开放这些实现。
    if (
        name in {"camctl.logging_runtime.diagnostics", "camctl.logging_runtime.events"}
        and _starts_with(target, ("camctl.logging_runtime.formatting",))
    ) or (
        name == "camctl.logging_runtime.diagnostics"
        and _starts_with(target, ("camctl.logging_runtime.receipt",))
    ) or (
        name == "camctl.logging_runtime.events"
        and _starts_with(target, ("camctl.logging_runtime.service",))
    ):
        return []
    if _starts_with(name, PORT_PREFIXES):
        # 端口服务层不反向依赖业务流程、持久化或适配器实现；端口
        # 模块之间可以互相依赖（文件执行使用读取会话端口等）。
        # 端口身份优先于所在包（parameter_schemas 位于 devices 包）。
        forbidden = (
            _starts_with(target, FLOW_PREFIXES)
            and not _starts_with(target, PORT_PREFIXES)
        ) or (
            _starts_with(target, ("camctl.persistence",))
            and not target.startswith("camctl.persistence.models")
        ) or _starts_with(target, ("camctl.bootstrap",)) or (
            _starts_with(target, ADAPTER_PREFIXES)
            and not _starts_with(target, PORT_PREFIXES)
        )
        if forbidden:
            return [f"{name} -> {target}（端口服务不依赖业务流程或装配）"]
        return []
    if name.startswith("camctl.contracts"):
        # R1 共享层只在最底层：不导入业务流程、适配器、端口服务
        # 之外的一切内部模块（资源读取原语与共享层同层）。
        if (
            target.startswith("camctl")
            and target != "camctl"
            and not _starts_with(target, SHARED_ALLOWED)
        ):
            return [f"{name} -> {target}（共享层不导入业务、适配器或持久化）"]
        return []
    if _starts_with(name, ADAPTER_PREFIXES):
        # R4 适配器实现只依赖共享层、端口服务与自己包内的模块。
        own = next(p for p in ADAPTER_PREFIXES if _starts_with(name, (p,)))
        if (
            (_starts_with(target, FLOW_PREFIXES) and not _starts_with(target, PORT_PREFIXES))
            or _starts_with(target, ("camctl.persistence", "camctl.bootstrap"))
            or (
                _starts_with(target, ADAPTER_PREFIXES)
                and not _starts_with(target, PORT_PREFIXES)
                and not _starts_with(target, (own,))
            )
        ):
            return [f"{name} -> {target}（适配器不导入业务流程或其他适配器）"]
        return []
    if _starts_with(name, ("camctl.reporting",)):
        # R3 报告生成不导入会话或设备控制（worker 已按装配入口豁免）。
        if _starts_with(
            target, ("camctl.session", "camctl.capture", "camctl.devices")
        ):
            return [f"{name} -> {target}（报告生成不导入会话或设备控制）"]
    if _starts_with(name, FLOW_PREFIXES):
        if target == "camctl.persistence.business":
            if names and names <= BUSINESS_PORT_NAMES:
                return []
            return [f"{name} -> {target}（业务流程只使用公开业务数据库端口）"]
        if target.startswith("camctl.persistence.business."):
            symbol = target.removeprefix("camctl.persistence.business.")
            if symbol in BUSINESS_PORT_NAMES:
                return []
            return [f"{name} -> {target}（业务流程不依赖业务数据库内部实现）"]
        # R2 业务流程不自行打开 SQLite：连接层只允许类型注解。
        if target == "sqlite3":
            return [f"{name} -> {target}（业务流程不直接使用 sqlite3）"]
        if target.startswith("camctl.persistence.runtime."):
            # 成员拼接边：类型判定由 runtime 基础边的名字集表达。
            if name == "camctl.outputs.withdrawal" and (
                target.removeprefix("camctl.persistence.runtime.") not in RUNTIME_TYPE_ONLY
            ):
                return [f"{name} -> {target}（业务流程不依赖连接层内部实现）"]
            return []
        if target == "camctl.persistence.runtime":
            allowed_names = RUNTIME_TYPE_ONLY
            if name == "camctl.outputs.withdrawal":
                allowed_names = RUNTIME_TYPE_ONLY | {"canonical_directory_binding"}
            if names and names <= allowed_names:
                return []
            return [
                f"{name} -> {target}"
                "（业务流程对连接层只允许 OwnedConnection 类型注解）"
            ]
        if _starts_with(target, ("camctl.persistence",)) and not _starts_with(
            target, _PERSISTENCE_ALLOWED
        ):
            return [f"{name} -> {target}（业务流程不依赖持久化内部实现）"]
        return []
    if _starts_with(name, HISTORY_APPLY_PREFIXES):
        # R5 历史事件应用与公开投影不导入报告协调器。
        if _starts_with(target, ("camctl.reporting",)):
            return [f"{name} -> {target}（历史事件应用不导入报告协调器）"]
    return []


def _forbidden_edges(
    graph: dict[str, dict[str, set[str]]],
) -> list[str]:
    """按依赖方向规则返回违规边描述；空列表表示全部合规。"""
    violations: list[str] = []
    for name, imports in sorted(graph.items()):
        for target, names in sorted(imports.items()):
            violations.extend(_check_edge(name, target, names))
    return violations


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


class TestCheckerDetectsViolations:
    """用故意违规的最小样本确认检查能够失败。"""

    def _synthetic(self) -> dict[str, dict[str, set[str]]]:
        return {
            # 共享层导入业务与适配器；import 别名形式。
            "camctl.contracts.fake": _imported_modules(
                "camctl.contracts.fake",
                "import camctl.reporting.policy as policy\n"
                "from camctl.capture import models\n",
            ),
            # 业务流程打开 SQLite：连接构造名不允许。
            "camctl.capture.fake": _imported_modules(
                "camctl.capture.fake",
                "from camctl.persistence.runtime import open_existing\n",
            ),
            # 业务流程直接使用 sqlite3。
            "camctl.outputs.fake": _imported_modules(
                "camctl.outputs.fake",
                "from sqlite3 import connect\n",
            ),
            # 报告生成导入会话：相对导入形式。
            "camctl.reporting.fake": _imported_modules(
                "camctl.reporting.fake",
                "from ..session import locks\n",
            ),
            # 历史事件应用导入报告协调器：多行括号形式。
            "camctl.persistence.row_history.fake": _imported_modules(
                "camctl.persistence.row_history.fake",
                "from camctl.reporting import (\n    policy,\n)\n",
            ),
            # 适配器导入业务流程。
            "camctl.devices.fake": _imported_modules(
                "camctl.devices.fake",
                "from camctl.session.service import run_session\n",
            ),
        }

    def test_checker_reports_each_synthetic_edge(self) -> None:
        violations = _forbidden_edges(self._synthetic())
        text = "\n".join(violations)
        for expected in (
            "camctl.contracts.fake -> camctl.reporting.policy",
            "camctl.contracts.fake -> camctl.capture.models",
            "camctl.capture.fake -> camctl.persistence.runtime",
            "camctl.outputs.fake -> sqlite3",
            "camctl.reporting.fake -> camctl.session.locks",
            "camctl.persistence.row_history.fake -> camctl.reporting.policy",
            "camctl.devices.fake -> camctl.session.service",
        ):
            assert expected in text, f"检查器漏报: {expected}"

    def test_checker_allows_entry_module_and_type_edges(self) -> None:
        """装配入口与类型注解允许边不算违规。"""
        graph = {
            "camctl.reporting.worker": _imported_modules(
                "camctl.reporting.worker",
                "from camctl.persistence.runtime import open_existing\n"
                "from camctl.session.locks import LockBackend\n",
            ),
            "camctl.session.service": _imported_modules(
                "camctl.session.service",
                "from camctl.persistence.runtime import OwnedConnection\n",
            ),
        }
        assert _forbidden_edges(graph) == []

    def test_named_cooperation_ports_allow_aliases_and_expanded_members(self) -> None:
        for source, module, symbol in (
            ("camctl.operations.availability", "camctl.persistence.business", "SharedSave"),
            ("camctl.operations.availability_owner", "camctl.persistence.business", "SharedSave"),
            ("camctl.operations.availability", "camctl.scheduling.notifications", "WakeReason"),
            ("camctl.outputs.withdrawal", "camctl.persistence.runtime", "canonical_directory_binding"),
            ("camctl.history.bounded", "camctl.persistence.transaction_data", "encode_json_value"),
            ("camctl.operations.availability_rules", "camctl.devices.drivers.hasselblad.definitions", "DRIVER_ID"),
        ):
            for statement in (f"from {module} import {symbol}\n",
                              f"from {module} import {symbol} as port\n"):
                imports = _imported_modules(source, statement)
                assert imports[module] == {symbol}
                assert f"{module}.{symbol}" in imports
                assert _forbidden_edges({source: imports}) == []
        source = "camctl.outputs.withdrawal"
        imports = _imported_modules(source, "from camctl.persistence.runtime import canonical_directory_binding as path, OwnedConnection as connection\n")
        assert _forbidden_edges({source: imports}) == []

    def test_named_cooperation_ports_do_not_allow_other_members(self) -> None:
        for source, module, allowed, forbidden in (
            ("camctl.operations.availability", "camctl.persistence.business", "SharedSave", "BusinessDatabase"),
            ("camctl.operations.availability_owner", "camctl.persistence.business", "SharedSave", "BusinessDatabase"),
            ("camctl.operations.availability", "camctl.scheduling.notifications", "WakeReason", "WorkNotifier"),
            ("camctl.outputs.withdrawal", "camctl.persistence.runtime", "canonical_directory_binding", "open_existing"),
            ("camctl.history.bounded", "camctl.persistence.transaction_data", "encode_json_value", "decode_json_value"),
            ("camctl.operations.availability_rules", "camctl.devices.drivers.hasselblad.definitions", "DRIVER_ID", "other"),
        ):
            imports = _imported_modules(source, f"from {module} import {allowed} as port, {forbidden} as other\n")
            violations = _forbidden_edges({source: imports})
            assert any(f"{source} -> {module}" in edge for edge in violations)

        for source, dependency in (
            ("camctl.operations.availability", "from camctl.persistence.repositories.operations import OperationRepository\n"),
            ("camctl.operations.availability_owner", "from camctl.persistence.repositories.operations import OperationRepository\n"),
            ("camctl.operations.fake", "from camctl.persistence.business import SharedSave\n"),
            ("camctl.outputs.withdrawal", "import camctl.persistence.runtime.open_existing as connect\n"),
            ("camctl.history.bounded", "import camctl.persistence.transaction_data as storage\n"),
            ("camctl.history.fake", "from camctl.persistence.transaction_data import encode_json_value\n"),
            ("camctl.operations.availability_rules", "import camctl.devices.drivers.hasselblad.definitions as definitions\n"),
            ("camctl.operations.fake", "from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID\n"),
            ("camctl.operations.availability_rules", "from camctl.devices.drivers.hasselblad.driver import HasselbladDriver\n"),
            ("camctl.operations.availability_rules", "from camctl.persistence.repositories.transactions import commit_operation\n"),
        ):
            assert _forbidden_edges({source: _imported_modules(source, dependency)})

    @pytest.mark.parametrize("source", ["camctl.devices.fake", "camctl.logging_runtime.fake"])
    def test_adapter_may_use_declared_schema_port_but_not_acceptance_flow(self, source):
        shared = _imported_modules(source, "from camctl.acceptance.schema import validate_precise, RuleError\n")
        flow = _imported_modules(source, "from camctl.acceptance.service import accept_input\n")
        assert _forbidden_edges({source: shared}) == []
        assert _forbidden_edges({source: flow})

    @pytest.mark.parametrize("symbol", sorted(BUSINESS_PORT_NAMES))
    @pytest.mark.parametrize("style", ["direct", "alias"])
    def test_business_flows_use_only_named_database_ports(self, symbol, style):
        source = "camctl.capture.fake"
        statement = (f"from camctl.persistence.business import {symbol}\n"
                     if style == "direct" else f"from camctl.persistence.business import {symbol} as Port\n")
        imports = _imported_modules(source, statement)
        assert _forbidden_edges({source: imports}) == []

    @pytest.mark.parametrize("dependency", [
        "import camctl.persistence.business\n",
        "import camctl.persistence.business as internal\n",
        "from camctl.persistence.business import _settle as internal\n",
        "import camctl.persistence.business._settle as internal\n",
        "from camctl.persistence.executor import DbExecutor\n",
        "from camctl.persistence.runtime import open_existing\n",
    ])
    def test_database_port_does_not_grant_connection_or_executor_access(self, dependency):
        source = "camctl.capture.fake"
        assert _forbidden_edges({source: _imported_modules(source, dependency)})

    @pytest.mark.parametrize("port,symbol", [
        ("diagnostics", "record_error"), ("performance", "record_performance"),
    ])
    def test_device_adapter_uses_logging_ports_without_file_adapter_access(self, port, symbol):
        source = "camctl.devices.fake"
        imports = _imported_modules(source, f"from camctl.logging_runtime.{port} import {symbol}\n")
        assert _forbidden_edges({source: imports}) == []
        private = _imported_modules(source, "from camctl.logging_runtime.clh_adapter import FileChannel\n")
        assert _forbidden_edges({source: private})

    @pytest.mark.parametrize("source,dependency", [
        ("camctl.devices.fake", "from camctl.logging_runtime.events import log_context, measure_stage\n"),
        ("camctl.host_files.fake", "from camctl.logging_runtime.models import LogLevel\n"),
        ("camctl.operations.fake", "from camctl.logging_runtime.events import record_event\n"),
        ("camctl.logging_runtime.diagnostics", "from camctl.logging_runtime.receipt import LogReceipt\n"),
        ("camctl.logging_runtime.diagnostics", "from camctl.logging_runtime.formatting import log_metadata\n"),
        ("camctl.logging_runtime.performance", "from camctl.logging_runtime.events import record_event\n"),
    ])
    def test_logging_interfaces_and_their_own_helpers_are_allowed(self, source, dependency):
        assert _forbidden_edges({source: _imported_modules(source, dependency)}) == []

    @pytest.mark.parametrize("source", [
        "camctl.devices.fake", "camctl.host_files.fake", "camctl.operations.fake",
        "camctl.logging_runtime.events", "camctl.logging_runtime.diagnostics",
    ])
    def test_logging_interfaces_do_not_expose_file_adapter(self, source):
        dependency = "from camctl.logging_runtime.clh_adapter import FileChannel\n"
        assert _forbidden_edges({source: _imported_modules(source, dependency)})

    @pytest.mark.parametrize("source", ["camctl.devices.fake", "camctl.host_files.fake"])
    @pytest.mark.parametrize("helper", ["formatting", "receipt"])
    def test_logging_helpers_are_not_other_components_ports(self, source, helper):
        dependency = f"from camctl.logging_runtime.{helper} import internal\n"
        assert _forbidden_edges({source: _imported_modules(source, dependency)})

    @pytest.mark.parametrize("dependency", [
        "import sqlite3\n",
        "from camctl.persistence.derived_storage import configure_derived_connection\n",
        "from camctl.persistence.row_validation import ProjectionRowValidator\n",
    ])
    @pytest.mark.parametrize("adapter", ["camctl.history.bounded", "camctl.reporting.failure_index"])
    def test_disk_adapter_owns_only_its_derived_storage_edges(self, dependency, adapter):
        assert _forbidden_edges({adapter: _imported_modules(adapter, dependency)}) == []
        for flow in ("camctl.history.fake", adapter + ".child", "camctl.reporting.fake", "camctl.capture.fake"):
            assert _forbidden_edges({flow: _imported_modules(flow, dependency)})
        assert _forbidden_edges({adapter: _imported_modules(
            adapter, "from camctl.persistence.runtime import open_existing\n")})


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
