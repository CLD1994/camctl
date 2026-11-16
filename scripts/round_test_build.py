"""同一轮冻结源码中的测试产物构建协调。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import time
from typing import Callable


BUILD_NAMES = ("host-demo", "python-wheel", "client-web")


@dataclass(frozen=True)
class BuildContext:
    source: Path
    workspace: Path
    receipt: Path


def load_round_context(name: str) -> BuildContext | None:
    """读取运行器交付的冻结来源和本轮输出；直接 pytest 不配置。"""
    source = os.environ.get("CAMCTL_TEST_BUILD_SOURCE")
    root = os.environ.get("CAMCTL_TEST_BUILD_ROOT")
    if not source and not root:
        return None
    if not source or not root:
        raise RuntimeError("本轮构建的冻结源码和输出目录必须同时提供")
    if name not in BUILD_NAMES:
        raise ValueError(f"未登记的测试构建类型: {name}")
    output = Path(root)
    return BuildContext(Path(source), output / f"{name}-build", output / f"{name}-build.json")


def clone_build_source(context: BuildContext) -> Path:
    """在首次构建中复制源码，隔离生成资源、依赖链接和网页输出。"""
    destination = context.workspace / "source"
    shutil.copytree(context.source, destination, ignore=shutil.ignore_patterns(".git"))
    return destination


def build_once(workspace: Path, receipt: Path, action: Callable[[], None], *, label: str) -> None:
    """串行执行一次构建；未完成和失败状态不作为可复用结果。"""
    import fcntl

    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = workspace / "state"
        if not state.exists():
            state.write_text("building", encoding="utf-8")
            started = time.monotonic()
            started_unix = time.time()

            def record(status: str) -> None:
                temporary = receipt.with_suffix(".tmp")
                temporary.write_text(json.dumps({
                    "status": status, "build_count": 1,
                    "started_unix_seconds": started_unix,
                    "wall_seconds": time.monotonic() - started,
                }), encoding="utf-8")
                os.replace(temporary, receipt)

            record("building")
            try:
                action()
                record("ready")
                state.write_text("ready", encoding="utf-8")
            except BaseException:
                record("failed")
                raise
        if state.read_text(encoding="utf-8") != "ready":
            raise RuntimeError(f"本轮 {label} 构建未完成或失败")


def read_build_summaries(output: Path) -> dict:
    """汇总实际构建；构建过程中的经过时间只用于进度显示。"""
    result = {}
    for name in BUILD_NAMES:
        receipt = output / f"{name}-build.json"
        record = (json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists()
                  else {"status": "not_requested", "build_count": 0})
        if record["status"] == "building":
            record["wall_seconds"] = time.time() - record["started_unix_seconds"]
        result[name.replace("-", "_") + "_build"] = record
    return result
