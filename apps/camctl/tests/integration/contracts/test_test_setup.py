"""验证真实 pytest 装配对单元与集成入口的资源准备分工。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_unit_contracts_do_not_generate_resources() -> None:
    tests = Path(__file__).resolve().parents[2]
    script = """
import sys
import types
import pytest

def forbidden(*args):
    raise AssertionError("unit_resource_sync")

sys.modules["sync_resources"] = types.SimpleNamespace(sync_package_resources=forbidden)
raise SystemExit(pytest.main(sys.argv[1:]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script,
         str(tests / "unit/contracts/test_enums.py"),
         str(tests / "unit/contracts/test_schemas.py"), "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
