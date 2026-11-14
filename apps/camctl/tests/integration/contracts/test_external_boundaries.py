"""检查真实源码的外部调用边界。"""

from support.external_boundaries import FILE_ENTRY_PREFIXES, SUBPROCESS_OWNERS, _starts_with


class TestRealSourceBoundaries:
    def test_scan_covers_real_modules(self, source_inspection) -> None:
        """检查对象不是空包：真实源码被完整扫描。"""
        sources = source_inspection
        assert len(sources) >= 100
        for anchor in (
            "camctl.operations.process",
            "camctl.session.locks",
            "camctl.resources",
            "camctl.acceptance.input",
            "camctl.reporting.publication",
            "camctl.host_files.io",
        ):
            assert anchor in sources, f"扫描缺少锚点模块: {anchor}"

    def test_subprocess_creation_only_in_managed_process(self, source_inspection) -> None:
        """子进程创建与 subprocess 导入只出现在受管入口和维护引擎。"""
        violations = [
            entry
            for module, facts in source_inspection.items()
            if not _starts_with(module, SUBPROCESS_OWNERS)
            for entry in facts["subprocess"]
        ]
        assert violations == []

    def test_no_network_client_dependency(self, source_inspection) -> None:
        """生产代码不导入网络客户端，也不建立网络连接。"""
        violations = [
            entry
            for facts in source_inspection.values()
            for entry in facts["network"]
        ]
        assert violations == []

    def test_file_calls_only_in_registered_modules(self, source_inspection) -> None:
        """文件系统直接调用只出现在登记责任的模块。"""
        violations = [
            entry
            for module, facts in source_inspection.items()
            if not _starts_with(module, FILE_ENTRY_PREFIXES)
            for entry in facts["files"]
        ]
        assert violations == []

    def test_no_bare_except(self, source_inspection) -> None:
        """异常分支全部携带显式类型，没有裸 except。"""
        violations = [
            entry
            for facts in source_inspection.values()
            for entry in facts["bare_excepts"]
        ]
        assert violations == []
