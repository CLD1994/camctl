"""共用 ADB 单文件命令与私有 NUL 帧解释；具体设备合同决定启用。"""

from dataclasses import dataclass
from decimal import Decimal
import re

from camctl.contracts.values import parse_object_id
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.devices.file_identity import FileIdentity
from camctl.operations.models import EffectState, ErrorValue
from .contracts import CameraCall
from .transport import shell_argv


FILE_COMMAND_EVIDENCE = (
    *(
        EvidenceContract(operation + "_returned", 1, operation, frozenset())
        for operation in ("query", "delete")
    ),
    *(
        EvidenceContract(
            operation + "_foreground_recovery",
            1,
            operation,
            frozenset({"terminate_grace_s"}),
        )
        for operation in ("query", "delete")
    ),
    EvidenceContract(
        "file_presence",
        1,
        "query",
        frozenset({"cleanup_item_id", "present"}),
        identity_field="cleanup_item_id",
    ),
    EvidenceContract(
        "file_presence",
        2,
        "query",
        frozenset({"obtain_item_id", "present"}),
        identity_field="obtain_item_id",
    ),
    EvidenceContract(
        "file_delete_presence",
        1,
        "delete",
        frozenset({"cleanup_item_id", "present"}),
        identity_field="cleanup_item_id",
    ),
)
_EXIT_CODE = re.compile(rb"(?:0|[1-9][0-9]{0,2})\Z")


def _exit_code(value):
    if _EXIT_CODE.fullmatch(value) is None or int(value) > 255:
        raise ValueError("文件响应退出码无效")
    return int(value)


@dataclass(frozen=True)
class FileResponseInterpreter:
    operation: str
    target_id: str
    path: str
    obtain: bool = False

    def interpret(self, raw):
        observations = ()
        details = {}
        try:
            if raw.output_failure is not None or not isinstance(raw.output, bytes):
                raise ValueError("文件响应输出不完整")
            parts = raw.output.split(b"\0")
            if (len(parts) != 8 or parts[0] != b"CAMCTL-FILE/1"
                    or parts[1] != self.operation.encode("ascii")
                    or parts[2] != self.path.encode("utf-8") or parts[-2:] != [b"END", b""]):
                raise ValueError("文件响应缺少唯一完整目标帧")
            state, rm_status, scan_status = parts[3:6]
            scan_code = _exit_code(scan_status)
            details["scan_exit_code"] = scan_code
            if self.operation == "delete":
                details["rm_exit_code"] = _exit_code(rm_status)
            elif rm_status != b"-":
                raise ValueError("查询响应不得包含删除退出码")
            if state not in (b"0", b"1", b"unknown", b"type", b"duplicate", b"invalid"):
                raise ValueError("文件响应在场状态无效")
            if state == b"0" and scan_code != 0:
                raise ValueError("不完整目录不能证明缺席")
            if state == b"unknown" and scan_code == 0:
                raise ValueError("未知扫描状态缺少失败依据")
            if state in (b"0", b"1"):
                observation_type = "file_delete_presence" if self.operation == "delete" else "file_presence"
                observations = (
                    DeviceObservation(
                        observation_type,
                        2 if self.obtain else 1,
                        {
                            "obtain_item_id"
                            if self.obtain
                            else "cleanup_item_id": self.target_id,
                            "present": state == b"1",
                        },
                    ),
                )
            else:
                details["reason"] = state.decode("ascii")
        except ValueError as error:
            # 损坏帧中的前缀不能承载观察或退出事实。
            details = {"reason": str(error)}
            observations = ()
        failed = (not observations or raw.error is not None or raw.output_failure is not None
                  or raw.exit is None or raw.exit.exit_code != 0
                  or details.get("scan_exit_code", 0) != 0 or details.get("rm_exit_code", 0) != 0)
        if failed:
            details.update(transport_error=raw.error, output_failure=raw.output_failure)
            if raw.exit is not None:
                details.update(local_exit_code=raw.exit.exit_code, local_signal=raw.exit.signal)
            if raw.stderr:
                details["stderr"] = raw.stderr.decode("utf-8", "replace")
        effect = (EffectState.CONFIRMED if observations and (
            self.operation == "query" or not observations[0].data["present"]) else EffectState.UNKNOWN)
        error = ErrorValue("adb_file_" + self.operation + "_failed", "device_file_access", details) if failed else None
        return InterpretedFacts(observations, error, effect)


def file_command_factory(kind, *, tools, evidence):
    """构造正式端口工厂，不授予型号能力或增加执行通道。"""
    if not isinstance(kind, CameraCall) or kind not in (CameraCall.FILE_QUERY, CameraCall.DELETE):
        raise ValueError("文件工厂只接受独立查询或删除")
    operation = "delete" if kind is CameraCall.DELETE else "query"

    def build(request, serial, argv, batch):
        if request.ticket is None or request.ticket.operation != operation:
            raise ValueError("文件调用必须携带原匹配操作票据")
        identity = FileIdentity.from_source(request.params["identity_key"], request.params["locator"])
        if identity.binding != request.binding:
            raise ValueError("文件调用改变原设备绑定")
        obtain = request.ticket.responsibility_key.startswith("obtain-exists/")
        owner = "obtain_item_id" if obtain else "cleanup_item_id"
        parse_object_id(request.params[owner])
        if request.params[owner] != request.ticket.target_id or (
            obtain
            and (
                operation != "query"
                or request.ticket.responsibility_key
                != f"obtain-exists/{request.ticket.target_id}"
            )
        ):
            raise ValueError("文件调用改变原成员或操作责任")
        if batch is not None:
            raise ValueError("单文件调用不接受批次")
        expected = shell_argv(serial, tools.file_presence_script(identity.path, delete=kind is CameraCall.DELETE))
        if argv is not None and argv != expected:
            raise ValueError("文件调用改变原精确命令")
        return DeviceCommand(
            operation,
            expected,
            request.binding,
            request.timeout_s,
            Decimal("1"),
            evidence,
            evidence.contract(operation + "_returned", 1),
            evidence.contract(operation + "_foreground_recovery", 1),
            FileResponseInterpreter(
                operation, request.ticket.target_id, identity.path, obtain
            ),
            kind.value,
        )

    return build
