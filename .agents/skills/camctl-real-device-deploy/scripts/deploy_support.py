"""本地部署与交付安装共用的参数、配置写出及 host 构建。"""

import argparse
import datetime
from decimal import Decimal
import json
from pathlib import Path
import re


# 参数注册、覆盖和新实例默认路径共用此映射；入口须能由 Python 3.6 导入。
PATH_DEFAULTS = {
    "state_db": "state.db", "log_file": "camctl.log", "staging": "staging",
    "ready": "ready", "processing": "processing",
}


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or "\0" in value:
        raise argparse.ArgumentTypeError("路径必须是不含空字符的绝对路径")
    return path


def add_path_arguments(parser):
    for key, default in PATH_DEFAULTS.items():
        parser.add_argument("--" + key.replace("_", "-"), type=absolute_path, metavar="PATH",
            help="设置 paths." + key + "；默认 <target>/" + default)


def path_overrides(args):
    return {key: getattr(args, key) for key in PATH_DEFAULTS if getattr(args, key) is not None}


def require_new_state_db(config):
    path = Path(config["paths"]["state_db"])
    if path.exists() or path.is_symlink():
        raise ValueError("新实例的状态库路径已存在，请指定新的文件路径: " + str(path))


def toml_key(value):
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value, ensure_ascii=False)


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise ValueError(f"无法序列化 TOML 值: {type(value).__name__}")


def write_config(path, config):
    import tomllib

    # 每台设备一个节，嵌套字段用点分键；其余顶层表各自一个节。
    lines = []

    def fields(values, parents=()):
        for key, value in values.items():
            keys = (*parents, key)
            if isinstance(value, dict) and value:
                fields(value, keys)
            else:
                lines.append(f"{'.'.join(map(toml_key, keys))} = {toml_value(value)}")

    def table(values, parents):
        if lines:
            lines.append("")
        lines.append("[" + ".".join(map(toml_key, parents)) + "]")
        fields(values)

    fields({key: value for key, value in config.items() if not isinstance(value, dict)})
    for key, value in config.items():
        if not isinstance(value, dict):
            continue
        if key == "devices" and value and all(isinstance(device, dict) for device in value.values()):
            for device_id, device in value.items():
                table(device, (key, device_id))
        else:
            table(value, (key,))
    contents = "\n".join(lines) + "\n"
    if tomllib.loads(contents, parse_float=Decimal) != config:
        raise ValueError("生成的配置与输入配置不一致")
    path.write_text(contents, encoding="utf-8")


def configure_paths(config, target, *, overrides=None):
    """先生成新实例默认路径，再应用显式覆盖；保留其他本地策略。"""
    config["paths"] = {key: str((overrides or {}).get(key, target / value))
        for key, value in PATH_DEFAULTS.items()}


def install_host(run, source, target):
    """使用组件支持的 CMake 3.10.2 命令格式构建并安装。"""
    build = target / "host-build"
    build.mkdir()
    run("配置 host", ["cmake", source, "-DCMAKE_BUILD_TYPE=Release", "-DHOST_BUILD_TESTS=OFF",
        f"-DCMAKE_INSTALL_PREFIX={target / 'host'}", "-DCMAKE_INSTALL_LIBDIR=lib"], cwd=build)
    run("构建安装 host", ["cmake", "--build", ".", "--target", "install", "--", "-j2"], cwd=build)
