"""可信时间下界只能引用本次事件并严格推进。"""

import pytest

from camctl.session.clock_rules import ClockBoundError, validate_trusted_bound


@pytest.mark.parametrize("old,new,reference,event", [(None, 10, 2, 2), (10, 11, 2, 2)])
def test_trusted_bound_accepts_initial_and_increasing_values(old, new, reference, event):
    validate_trusted_bound(old, new, reference, event)


@pytest.mark.parametrize("old,new,reference,event,message", [
    (None, None, 2, 2, "首次建立必须写入可信时间下界"),
    (10, None, 2, 2, "可信时间下界只严格提高"),
    (10, 10, 2, 2, "可信时间下界只严格提高"),
    (10, 9, 2, 2, "可信时间下界只严格提高"),
    (10, 11, 3, 2, "下界依据必须引用本事件"),
])
def test_trusted_bound_rejects_invalid_changes(old, new, reference, event, message):
    with pytest.raises(ClockBoundError, match=message):
        validate_trusted_bound(old, new, reference, event)
