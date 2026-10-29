"""独立输出范围责任只在明确支持其操作约束的格式中保存。"""

import pytest

from camctl.persistence import runtime


@pytest.mark.parametrize("version", [6, 7, 8])
def test_output_scope_accepts_supported_formats(version):
    runtime.require_output_scope_format(version)


@pytest.mark.parametrize("version", [None, True, 1, 2, 3, 4, 5, 9])
def test_output_scope_rejects_old_or_unknown_formats_with_explicit_upgrade(version):
    with pytest.raises(runtime.DatabaseUnsupportedFormatError, match="init --upgrade-output-scope"):
        runtime.require_output_scope_format(version)
