"""派发前根据窗口和取消事实重新判断资格。"""

import pytest

from camctl.scheduling.resources import recheck_dispatch
from camctl.scheduling.rules import LaunchWindow


@pytest.mark.parametrize("now,canceled,allowed,reason", [
    (2001, False, False, "window_ended"),
    (1500, True, False, "canceled"),
    (1500, False, True, None),
])
def test_recheck_dispatch(now, canceled, allowed, reason):
    result = recheck_dispatch(
        window=LaunchWindow(scheduled_at=1000, window_end=2000),
        trusted_wall_now=now, canceled=canceled,
    )
    assert (result.allowed, result.reason) == (allowed, reason)
