"""主机正式文件协作者的纯能力声明。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HostFileDeclaration:
    """主机正式文件协作者明确提供的能力。"""

    delete_supported: bool

    def __post_init__(self) -> None:
        if type(self.delete_supported) is not bool:
            raise ValueError("主机文件删除能力必须是布尔值")


HOST_FILE_DECLARATION = HostFileDeclaration(delete_supported=True)
