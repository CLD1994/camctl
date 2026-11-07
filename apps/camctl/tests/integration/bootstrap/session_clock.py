"""会话与拍摄工厂共用的测试时间输入。"""

import time
from datetime import datetime, timezone


class SessionClock:
    def __init__(self):
        self.wall_ns = time.time_ns()
        self.mono_ns = time.monotonic_ns()

    def utc_micros(self):
        return self.wall_ns // 1_000

    def monotonic_ns(self):
        return self.mono_ns

    def advance(self, seconds):
        self.advance_ns(seconds * 1_000_000_000)

    def advance_ns(self, delta):
        assert delta >= 0
        self.wall_ns += delta
        self.mono_ns += delta

    def reach(self, scheduled_at):
        target = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
        target_ns = int(target.timestamp()) * 1_000_000_000
        self.advance_ns(max(0, target_ns - self.wall_ns))

    def __getitem__(self, key):
        assert key == "ns"
        return self.mono_ns

    def __setitem__(self, key, value):
        assert key == "ns"
        self.advance_ns(value - self.mono_ns)
