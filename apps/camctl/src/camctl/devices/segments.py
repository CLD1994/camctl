"""设备分段顺序使用的精确键。"""

from dataclasses import dataclass

from camctl.contracts.values import MAX_OBJECT_ID


@dataclass(frozen=True, order=True, slots=True)
class SegmentKey:
    """CAM 目录编号和目录内 index 的数值顺序，不表达文件归属。"""

    directory_number: int
    index: int

    def __post_init__(self) -> None:
        if (type(self.directory_number) is not int
                or not 1 <= self.directory_number <= MAX_OBJECT_ID):
            raise ValueError("CAM 目录编号必须是正安全整数")
        if type(self.index) is not int or not 1 <= self.index <= 999:
            raise ValueError("分段 index 必须是 1～999 的整数")

    def successor(self) -> "SegmentKey":
        """返回设备命名顺序中的下一个位置。"""
        if self.index < 999:
            return SegmentKey(self.directory_number, self.index + 1)
        return SegmentKey(self.directory_number + 1, 1)

    def predecessor(self) -> "SegmentKey | None":
        """返回设备命名顺序中的前一个位置；首个位置没有前项。"""
        if self.index > 1:
            return SegmentKey(self.directory_number, self.index - 1)
        if self.directory_number > 1:
            return SegmentKey(self.directory_number - 1, 999)
        return None
