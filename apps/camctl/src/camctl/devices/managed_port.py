"""厂商命令映射与既有受管调用之间的设备端口适配。

部署提供命令与响应解释，适配器沿原尝试调用 invoke 一次。本次
业务配置的期限覆盖命令模板值，绑定和操作身份须与原尝试一致。
传输负责等待真实收场；适配器不另设取消等待或隐藏业务重试。
"""

from dataclasses import replace
from typing import Callable

from camctl.devices.adb_transport import DeviceCommand, ManagedTransport, invoke
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration

__all__ = ["ManagedDeviceDriver"]


class ManagedDeviceDriver:
    """用正式命令映射实现控制、停止、查询、列举及删除端口。

    command_for 接收原请求和适用列举批量，提供具体命令及响应
    解释。文件内容读取及整片摘要继续由对应独立端口负责。
    """

    def __init__(self, *, declaration: DriverDeclaration,
                 command_for: Callable[[ControlRequest, int | None], DeviceCommand],
                 transport: ManagedTransport) -> None:
        self.declaration = declaration
        self._command_for = command_for
        self._transport = transport

    async def _call(self, request: ControlRequest, operation: str,
                    batch: int | None = None) -> DeviceCallResult:
        ticket = request.ticket
        if ticket is None:
            raise ValueError("受管设备调用必须携带已提交的原尝试身份")
        if ticket.operation != operation:
            raise ValueError("受管设备端口与原尝试的操作类别不符")
        if request.timeout_s is None or not request.timeout_s.is_finite() or request.timeout_s <= 0:
            raise ValueError("受管设备控制调用必须携带本次有限正秒数期限")
        command = self._command_for(request, batch)
        if command.binding != request.binding or command.operation != ticket.operation:
            raise ValueError("命令映射与原设备绑定或尝试操作不符")
        command = replace(command, timeout_s=request.timeout_s)
        outcome = await invoke(command, ticket, self._transport)
        return DeviceCallResult.from_outcome(outcome)

    async def control(self, request: ControlRequest) -> DeviceCallResult:
        return await self._call(request, "control")

    async def stop(self, request: ControlRequest) -> DeviceCallResult:
        return await self._call(request, "stop")

    async def query_state(self, request: ControlRequest) -> DeviceCallResult:
        return await self._call(request, "query")

    async def list_results(self, request: ControlRequest, batch: int) -> DeviceCallResult:
        return await self._call(request, "result", batch)

    async def delete(self, request: ControlRequest) -> DeviceCallResult:
        return await self._call(request, "delete")
