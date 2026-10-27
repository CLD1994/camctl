"""文件详细结果与通用完成事实保持同一来源。"""

import pytest

from camctl.logging_runtime.clh_adapter import WriteResult
from camctl.logging_runtime.models import LogLevel, LogWriteResult


@pytest.mark.parametrize("appended,durable", [(False, False), (True, False), (True, True)])
def test_file_completion_preserves_independent_facts(appended, durable):
    result = WriteResult(appended, False, durable=durable)
    assert result.completion == LogWriteResult(appended, durable)
    assert result.completion.delivered_for(LogLevel.INFO) is appended
    assert result.completion.delivered_for(LogLevel.ERROR) is (appended and durable)
