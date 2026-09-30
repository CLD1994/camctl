"""camctl 命令行入口。"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """执行 CLI 命令并返回进程退出码。

    命令解析与机器结果编码随后接入；当前入口只保证
    存在可调用的安装物入口，不接受任何命令。
    """
    print("camctl: 未提供可执行的命令", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
