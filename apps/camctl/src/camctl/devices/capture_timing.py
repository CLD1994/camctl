"""冻结拍摄时长的精确运算；不把计时下界解释为实际媒体时长。"""
from dataclasses import dataclass
from camctl.contracts.values import MAX_OBJECT_ID


def _bounded(value: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_OBJECT_ID:
        raise ValueError("拍摄时间超出可保存的精确整数范围")
    return value


@dataclass(frozen=True)
class CaptureTiming:
    duration_ms: int
    startup_margin_ms: int
    result_margin_ms: int = 0
    extra_wait_ms: int = 0

    def __post_init__(self):
        _bounded(self.duration_ms, 1)
        for value in (self.startup_margin_ms, self.result_margin_ms, self.extra_wait_ms):
            _bounded(value)

    @property
    def total_ms(self) -> int:
        return _bounded(self.duration_ms + self.startup_margin_ms + self.result_margin_ms + self.extra_wait_ms)

    def deadline_us(self, anchor_us: int) -> int:
        return _bounded(_bounded(anchor_us) + self.total_ms * 1000)

    def deadline_ns(self, anchor_ns: int) -> int:
        return _bounded(_bounded(anchor_ns) + self.total_ms * 1000000)

    def recording_lower_bound_ns(self, control_elapsed_ns: int) -> int:
        return max(0, _bounded(control_elapsed_ns) - self.startup_margin_ms * 1000000)
