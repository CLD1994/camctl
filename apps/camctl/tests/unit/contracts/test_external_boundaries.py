"""用合成源码验证外部调用边界检查。"""

from support.external_boundaries import (
    FILE_ENTRY_PREFIXES, SUBPROCESS_OWNERS, _bare_excepts, _file_entries,
    _network_dependencies, _starts_with, _subprocess_creations,
)


class TestCheckerDetectsViolations:
    """用故意违规的最小样本确认每条规则都能够失败。"""

    def test_local_uri_parsing_does_not_grant_network_client_imports(self) -> None:
        for source in (
            "from urllib.parse import unquote, urldefrag, urljoin\n",
            "import urllib.parse as uri_parser\n",
        ):
            assert _network_dependencies("camctl.reporting.fake", source) == []
        for source in (
            "import urllib\n", "from urllib import request\n",
            "import urllib.request as client\n", "from urllib.request import urlopen\n",
            "import http.client\n", "import socket as connection\n",
        ):
            assert _network_dependencies("camctl.reporting.fake", source)

    def test_checker_reports_each_synthetic_violation(self) -> None:
        assert _bare_excepts(
            "camctl.fake", "try:\n    pass\nexcept:\n    pass\n"
        ) == ["camctl.fake:3"]
        assert _subprocess_creations(
            "camctl.capture.fake", "import subprocess\n"
        ) == ["camctl.capture.fake:1 (import subprocess)"]
        assert _subprocess_creations(
            "camctl.reporting.fake",
            "asyncio.create_subprocess_exec('adb')\n",
        ) == ["camctl.reporting.fake:1 (asyncio.create_subprocess_exec)"]
        assert _network_dependencies(
            "camctl.devices.fake", "from urllib.request import urlopen\n"
        ) == ["camctl.devices.fake:1 (from urllib.request import)"]
        assert _network_dependencies(
            "camctl.session.fake", "socket.socket()\n"
        ) == ["camctl.session.fake:1 (socket.socket)"]
        assert _file_entries(
            "camctl.cancellation.fake", "os.unlink(path)\n"
        ) == ["camctl.cancellation.fake:1 (os.unlink)"]
        assert _file_entries(
            "camctl.scheduling.fake", "open(path, 'rb')\n"
        ) == ["camctl.scheduling.fake:1 (open)"]
        assert _file_entries(
            "camctl.history.fake", "target.write_text(data)\n"
        ) == ["camctl.history.fake:1 (.write_text)"]

    def test_checker_allows_registered_entries(self) -> None:
        """登记模块内的合法入口与 str.replace 等同名无关调用不算违规。"""
        assert _file_entries(
            "camctl.session.locks", "os.open(path, os.O_RDWR)\n"
        ) == ["camctl.session.locks:1 (os.open)"]
        assert _starts_with("camctl.session.locks", FILE_ENTRY_PREFIXES)
        assert _starts_with("camctl.update_engine.engine", SUBPROCESS_OWNERS)
        assert not _starts_with("camctl.update_engine_extra", SUBPROCESS_OWNERS)
        assert _starts_with("camctl.deployment", FILE_ENTRY_PREFIXES)
        assert _starts_with("camctl.update_engine.router", FILE_ENTRY_PREFIXES)
        assert _file_entries(
            "camctl.contracts.fake", "text.replace('a', 'b')\n"
        ) == []
        assert _subprocess_creations(
            "camctl.cli.fake", "asyncio.run(main())\n"
        ) == []
        assert _file_entries(
            "camctl.cli.fake", "iter(items)\n"
        ) == []
