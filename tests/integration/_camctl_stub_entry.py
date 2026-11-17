"""部署装配桥：进程启动阶段登记替身驱动后进入生产 CLI。

部署适配以 CAMCTL_TEST_DRIVER_SPEC 指向的剧本构造受契约约束的设
备替身，把驱动定义与运行端口一起登记（与正式部署装配同一接入
面），随后不加修改地运行 camctl CLI。无剧本时等价于直接进入
CLI。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camctl_fixtures import install_stub_driver  # noqa: E402


def _main(argv: list[str]) -> int:
    session_assemblies = not argv or argv[0] not in ("init", "describe", "submit")
    clock_path = os.environ.get("CAMCTL_TEST_CLOCK_FILE")
    if clock_path:
        from _osmo_builtin_clock import ControlledClock
        from _osmo_builtin_clock_entry import _install_clock_ports

        _install_clock_ports(ControlledClock(
            Path(clock_path), Path(os.environ["CAMCTL_TEST_CLOCK_LOG"])),
            session_assemblies=session_assemblies)
    spec_path = os.environ.get("CAMCTL_TEST_DRIVER_SPEC")
    if spec_path:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        if spec.get("camera_demo"):
            from camera_demo_fixtures import install_camera_demo
            install_camera_demo(spec, session_assemblies=session_assemblies)
        else:
            install_stub_driver(spec)
    from camctl.cli import main

    return main(argv)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
