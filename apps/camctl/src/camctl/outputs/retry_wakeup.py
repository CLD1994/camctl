"""读取重试的会话内最早截止，实际资格仍由读取流程判断。"""


class ReadRetryWakeup:
    def __init__(self):
        self._target_ns = None
        self._at = None

    def schedule(self, target_ns, now_ns, now_s):
        if self._target_ns is not None and self._target_ns <= target_ns:
            return False
        self._target_ns = target_ns
        self._at = now_s + max(0, target_ns - now_ns) / 1_000_000_000
        return True

    def consume_due(self, now):
        if self._at is not None and now >= self._at:
            self._target_ns, self._at = None, None

    def next_at(self, now, margin_s):
        return self._at
