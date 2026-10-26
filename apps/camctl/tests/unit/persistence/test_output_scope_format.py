"""独立输出范围责任只在明确支持其操作约束的格式中保存。"""

import pytest

from camctl.persistence import runtime


def test_output_scope_accepts_current_format():
    runtime.require_output_scope_format(runtime.CURRENT_FORMAT_VERSION)


@pytest.mark.parametrize("version", [None, True, 1, 2, 3, 4, 5, 7])
def test_output_scope_rejects_old_or_unknown_formats_with_explicit_upgrade(version):
    with pytest.raises(runtime.DatabaseUnsupportedFormatError, match="init --upgrade-output-scope"):
        runtime.require_output_scope_format(version)
