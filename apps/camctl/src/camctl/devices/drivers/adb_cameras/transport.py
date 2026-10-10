"""指定 ADB 绑定的受管传输；共享服务端由 ADB 自身管理。"""
import shlex

from camctl.operations.process import execute_tool


class AdbTransport:
    async def run(self, spec, stop):
        return await execute_tool(spec, stop=stop)

    async def run_stream(self, spec, stop, stdout_sink):
        return await execute_tool(spec, stop=stop, stdout_sink=stdout_sink)


def shell_argv(serial, script):
    if (not isinstance(serial, str) or not serial or serial.startswith("-")
            or "\0" in serial):
        raise ValueError("ADB 调用需要本次配置的明确 serial")
    if not isinstance(script, str) or not script or "\0" in script:
        raise ValueError("远端脚本必须是非空且不含 NUL 的文本")
    return ("adb", "-s", serial, "shell", "-T", "sh -c " + shlex.quote(script))
