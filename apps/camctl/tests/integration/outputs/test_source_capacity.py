"""来源写入及原键核实的进程组预算验收。"""

import pytest

from ..support.source_capacity import run_capacity


@pytest.mark.performance
@pytest.mark.asyncio
async def test_source_write_and_original_key_fit_process_group_budget(tmp_path):
    """2400成员、601事件跨10页；组外完整核对，组内无分配追踪或SQL观察器。"""
    facts = await run_capacity(tmp_path)
    assert facts["member_count"] == 2400
    assert facts["event_count"] == 601
