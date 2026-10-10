"""部署装配桥：进程启动阶段登记替身驱动后进入生产 CLI。

部署适配以 CAMCTL_TEST_DRIVER_SPEC 指向的剧本构造受契约约束的设
备替身，把驱动定义与运行端口一起登记（与正式部署装配同一接入
面），随后不加修改地运行 camctl CLI。无剧本时等价于直接进入
CLI。

CAMCTL_TEST_TRACE_DISPATCH 提供时，把会话派发循环记录的单动作异
常写入标准错误，供跨组件用例定位生产行为。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camctl_fixtures import install_stub_driver  # noqa: E402


def _trace_dispatch() -> None:
    import camctl.bootstrap.flows as flows

    original = flows.dispatch_ready

    async def traced(runtime, descriptors):
        results = await original(runtime, descriptors)
        for action_id, outcome in results:
            if isinstance(outcome, BaseException):
                sys.stderr.write(f"dispatch action {action_id} raised:\n")
                sys.stderr.write("".join(traceback.format_exception(
                    type(outcome), outcome, outcome.__traceback__)))
                sys.stderr.flush()
        return results

    flows.dispatch_ready = traced


def _main(argv: list[str]) -> int:
    spec_path = os.environ.get("CAMCTL_TEST_DRIVER_SPEC")
    if spec_path:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        if spec.get("camera_demo"):
            from camera_demo_fixtures import install_camera_demo
            install_camera_demo(spec)
        else:
            install_stub_driver(spec)
        if os.environ.get("CAMCTL_TEST_TRACE_DISPATCH"):
            _trace_dispatch()
    from camctl.cli import main

    return main(argv)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
