"""本地部署与交付安装共用的配置写出及 host 构建。"""
from __future__ import annotations

import datetime
from decimal import Decimal
import json
import re
import tomllib


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
    # 沿用已有的精确值编码，只把嵌套配置展开为便于人工编辑的 TOML 节。
    lines = []

    def table(values, parents=()):
        if parents:
            if lines:
                lines.append("")
            lines.append("[" + ".".join(map(toml_key, parents)) + "]")
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{toml_key(key)} = {toml_value(value)}")
        for key, value in values.items():
            if isinstance(value, dict):
                table(value, (*parents, key))

    table(config)
    contents = "\n".join(lines) + "\n"
    if tomllib.loads(contents, parse_float=Decimal) != config:
        raise ValueError("生成的配置与输入配置不一致")
    path.write_text(contents, encoding="utf-8")


def configure_paths(config, target):
    """为新实例设置独立目录，保留设备和其他本地策略。"""
    config["paths"] = {key: str(target / value) for key, value in {
        "state_db": "state.db", "log_file": "camctl.log", "staging": "staging",
        "ready": "ready", "processing": "processing"}.items()}


def install_host(run, source, target):
    """使用组件支持的 CMake 3.10.2 命令格式构建并安装。"""
    build = target / "host-build"
    build.mkdir()
    run("配置 host", ["cmake", source, "-DCMAKE_BUILD_TYPE=Release", "-DHOST_BUILD_TESTS=OFF",
        f"-DCMAKE_INSTALL_PREFIX={target / 'host'}", "-DCMAKE_INSTALL_LIBDIR=lib"], cwd=build)
    run("构建安装 host", ["cmake", "--build", ".", "--target", "install", "--", "-j2"], cwd=build)
