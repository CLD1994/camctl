"""可信时间下界的纯业务规则。"""


class ClockBoundError(ValueError):
    """可信时间下界变化与其事件依据不一致。"""


def validate_trusted_bound(old_bound, new_bound, reference_event_id, event_id):
    if old_bound is None:
        if new_bound is None:
            raise ClockBoundError("首次建立必须写入可信时间下界")
    elif new_bound is None or new_bound <= old_bound:
        raise ClockBoundError("可信时间下界只严格提高")
    if reference_event_id != event_id:
        raise ClockBoundError("下界依据必须引用本事件")
