"""复制到仓库外的安装 CLI 入口；只记录来源并装配本测试的确定断点。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

import camctl


def main() -> int:
    root = Path(os.environ["CAMCTL_HB_TEST_ROOT"])
    python = Path(os.environ["CAMCTL_HB_TEST_INSTALLED_PYTHON"])
    assert sys.executable == str(python), (sys.executable, python)
    assert Path(camctl.__file__).is_relative_to(python.parent.parent)
    with (root / "processes.jsonl").open("a") as stream:
        stream.write(json.dumps({
            "kind": "camctl", "argv": sys.argv, "python": sys.executable,
            "camctl": camctl.__file__, "cwd": os.getcwd(), "pid": os.getpid(),
            "ppid": os.getppid(), "pgid": os.getpgrp(),
            "fault_environment": {
                "present": "CAMCTL_HB_TEST_FAULT" in os.environ,
                "value": os.environ.get("CAMCTL_HB_TEST_FAULT"),
            },
            "utc": datetime.now(timezone.utc).isoformat(),
        }) + "\n")
    from camctl.cli import main as cli_main
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
