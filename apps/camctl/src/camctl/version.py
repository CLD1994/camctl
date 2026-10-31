"""查询当前安装的发行版本。"""

from importlib import metadata


def installed_version() -> str:
    return metadata.version("camctl")
