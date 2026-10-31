"""以有界状态识别 JPEG 外层结束标记，不解码图像。"""

from __future__ import annotations


class JpegBoundary:
    """增量检查标记和段长度；完成后忽略图像后附带的数据。"""

    def __init__(self) -> None:
        self._state = "soi_ff"
        self._remaining = 0
        self._length_high = 0
        self._after_segment = "marker_ff"
        self._in_scan_marker = False
        self._marker_fill = False

    @property
    def complete(self) -> bool:
        return self._state == "done"

    def feed(self, block: bytes) -> None:
        index = 0
        while index < len(block) and self._state not in ("done", "invalid"):
            state = self._state
            if state == "segment":
                count = min(self._remaining, len(block) - index)
                self._remaining -= count
                index += count
                if self._remaining == 0:
                    self._state = self._after_segment
                continue
            if state == "scan":
                marker_index = block.find(b"\xff", index)
                if marker_index < 0:
                    return
                index = marker_index + 1
                self._state = "marker_code"
                self._in_scan_marker = True
                self._marker_fill = False
                continue
            value = block[index]
            index += 1
            if state == "soi_ff":
                self._state = "soi_code" if value == 0xff else "invalid"
            elif state == "soi_code":
                self._state = "marker_ff" if value == 0xd8 else "invalid"
            elif state == "marker_ff":
                self._state = "marker_code" if value == 0xff else "invalid"
                self._in_scan_marker = False
                self._marker_fill = False
            elif state == "marker_code":
                if value == 0xff:
                    self._marker_fill = True
                elif value == 0x00:
                    self._state = ("scan" if self._in_scan_marker and not self._marker_fill
                                   else "invalid")
                elif value == 0xd9:
                    self._state = "done"
                elif value == 0x01 or 0xd0 <= value <= 0xd7:
                    self._state = "scan" if self._in_scan_marker else "marker_ff"
                elif (0xc0 <= value <= 0xcf and value != 0xc8) or 0xda <= value <= 0xef or value == 0xfe:
                    self._after_segment = (
                        "scan" if value == 0xda else "marker_ff"
                    )
                    self._state = "length_high"
                else:
                    # SOI 重复和无法解释的保留标记均不推断段结构。
                    self._state = "invalid"
            elif state == "length_high":
                self._length_high = value
                self._state = "length_low"
            elif state == "length_low":
                length = (self._length_high << 8) | value
                if length < 2:
                    self._state = "invalid"
                else:
                    self._remaining = length - 2
                    self._state = "segment" if self._remaining else self._after_segment
