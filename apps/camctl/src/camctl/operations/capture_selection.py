"""读取意图消费的拍摄选择申请端口。"""

from abc import ABC, abstractmethod


class CaptureSelectionRequest(ABC):
    """具有固定事实时刻、可保存原申请的拍摄选择。"""

    trusted_wall_now: int

    @abstractmethod
    def document(self):
        """返回固定申请内容。"""
