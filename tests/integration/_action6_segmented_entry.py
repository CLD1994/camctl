"""Action6 安装版 CLI 入口上的测试专用真实仓储同步点。"""

from __future__ import annotations

import os


def main() -> int:
    if os.environ.get("CAMCTL_ACTION6_TEST_OBSERVER_STAGE"):
        from _action6_segmented_observer import install

        install()
    from _osmo_builtin_clock_entry import main as clocked_cli_main

    return clocked_cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
