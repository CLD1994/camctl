"""用内存中的导入图验证依赖方向检查。"""

import pytest

from support.dependency_layers import BUSINESS_PORT_NAMES, _forbidden_edges, _imported_modules


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
