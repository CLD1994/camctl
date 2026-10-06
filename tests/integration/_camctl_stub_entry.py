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
    spec_path = os.environ.get("CAMCTL_TEST_DRIVER_SPEC")
    if spec_path:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        install_stub_driver(spec)
    from camctl.cli import main

    return main(argv)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
