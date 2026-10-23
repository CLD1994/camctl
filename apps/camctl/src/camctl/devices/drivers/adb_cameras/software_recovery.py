"""ADB 相机的软件重启端口；只有固件合同明确配置后由恢复责任调用。"""
from contextvars import ContextVar
from decimal import Decimal
import shlex
from types import SimpleNamespace

from camctl.devices.ports import ControlRequest
from camctl.operations.models import EffectState
from camctl.operations.process import ToolSpec, ToolStartError
from .contracts import CameraCall
from .filesystem import _returned
from .transport import shell_argv

_RECOVERY_ACCESS = ContextVar("adb_camera_recovery_access", default=None)


def raw_document(raw):
    if raw is None:
        return {"not_started": True}
    return {"error": raw.error, "output_failure": raw.output_failure,
        "exit_code": None if raw.exit is None else raw.exit.exit_code,
        "signal": None if raw.exit is None else raw.exit.signal,
        "stdout_hex": None if raw.output is None else raw.output.hex(),
        "stderr_hex": None if raw.stderr is None else raw.stderr.hex()}


class SoftwareRecoveryDriver:
    """由 AdbCameraDriver 继承，所有普通端口共用排他检查。"""

    def hold_recovery(self, device_id):
        self._recovery_devices.add(device_id)

    def release_recovery(self, device_id):
        self._recovery_devices.discard(device_id)

    def recovery_serial(self, binding):
        token = _RECOVERY_ACCESS.set((id(self), binding.device_id))
        try:
            return self._serial(binding)
        finally:
            _RECOVERY_ACCESS.reset(token)

    def _check_recovery_access(self, binding):
        if (binding.device_id in self._recovery_devices
                and _RECOVERY_ACCESS.get() != (id(self), binding.device_id)):
            raise RuntimeError("相机由软件恢复责任独占，普通设备端口暂不可用")

    async def _recovery_raw(self, binding, argv, timeout_s):
        if (not isinstance(timeout_s, Decimal) or not timeout_s.is_finite() or timeout_s <= 0):
            raise ValueError("恢复调用必须具有有限正期限")
        try:
            return await self.transport.run(
                ToolSpec(tuple(argv), timeout_s, self.terminate_grace_s), _RecoveryStop())
        except ToolStartError:
            return None

    async def recovery_identity(self, binding, *, timeout_s):
        from .firmware_identity import identity_script, decode_identity
        serial = self.recovery_serial(binding)
        raw = await self._recovery_raw(binding, shell_argv(serial, identity_script(binding.driver_id)), timeout_s)
        result = {"identity": None, "call": raw_document(raw)}
        if raw is None or not _returned(raw) or raw.stderr != b"":
            result["reason"] = "identity_call_failed"
            return result
        try:
            result["identity"] = decode_identity(binding.driver_id, raw.output)
        except (ValueError, UnicodeError) as error:
            result["reason"] = str(error)
        return result

    async def recovery_reboot(self, binding, *, timeout_s):
        serial = self.recovery_serial(binding)
        raw = await self._recovery_raw(binding, ("adb", "-s", serial, "reboot"), timeout_s)
        return raw_document(raw)

    async def recovery_prepare(self, binding, params, *, activity_id, directories, timeout_s):
        serial = self.recovery_serial(binding)
        began = self.monotonic_ns()
        records = []
        # 新启动代次不得使用旧会话设置缓存；这里只执行准备，绝不 START。
        self._configured_settings = {key for key in self._configured_settings if key[0] != binding}
        request = ControlRequest("start_recording", binding, params,
            ticket=SimpleNamespace(operation="control", target_id=str(activity_id)), timeout_s=timeout_s)
        for command in self.contract.session_setup_commands:
            remaining = timeout_s - Decimal(self.monotonic_ns() - began) / 1000000000
            if remaining <= 0:
                return {"ready": False, "calls": records}
            mapped = self.contract.commands[CameraCall.SETTING](request, serial,
                shell_argv(serial, shlex.join(command)), None)
            raw = await self._recovery_raw(binding, mapped.argv, remaining)
            records.append({"argv": list(mapped.argv), "result": raw_document(raw)})
            if raw is None:
                return {"ready": False, "calls": records}
            facts = mapped.interpreter.interpret(raw)
            if facts.error is not None or facts.effect is not EffectState.CONFIRMED:
                return {"ready": False, "calls": records}
        remaining = timeout_s - Decimal(self.monotonic_ns() - began) / 1000000000
        if remaining <= 0 or not directories:
            return {"ready": False, "calls": records}
        script = " && ".join("test -d " + shlex.quote(root) + " && test -w " + shlex.quote(root)
                             + " && df -k " + shlex.quote(root) for root in directories)
        raw = await self._recovery_raw(binding, shell_argv(serial, script), remaining)
        records.append({"storage": True, "result": raw_document(raw)})
        ready = raw is not None and _returned(raw) and raw.stderr == b""
        if ready:
            rows = [line.split() for line in raw.output.decode("utf-8", "replace").splitlines()]
            capacities = [int(row[3]) for row in rows if len(row) >= 6
                          and all(value.isdecimal() for value in row[1:4])]
            ready = len(capacities) == len(directories) and all(value > 0 for value in capacities)
        if ready:
            self._configured_settings.update((binding, serial, command) for command in self.contract.session_setup_commands)
        return {"ready": ready, "calls": records}

    async def recovery_directory(self, request):
        token = _RECOVERY_ACCESS.set((id(self), request.binding.device_id))
        try:
            return await self.read_directory(request, stop=_RecoveryStop())
        finally:
            _RECOVERY_ACCESS.reset(token)


class _RecoveryStop:
    async def requested(self):
        import asyncio
        await asyncio.Future()
