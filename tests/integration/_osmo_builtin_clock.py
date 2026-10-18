"""OSMO installation-chain absolute clock, isolated from Python's global time."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _read_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or type(state.get("epoch")) is not int:
        raise ValueError("OSMO 测试时钟状态缺少整数 epoch")
    if state["mode"] == "frozen":
        fields = ("utc_us", "monotonic_ns")
    elif state["mode"] == "running":
        fields = ("utc_anchor_us", "monotonic_anchor_ns", "real_monotonic_anchor_ns")
    else:
        raise ValueError(f"OSMO 测试时钟模式无效: {state.get('mode')!r}")
    if any(type(state.get(name)) is not int for name in fields):
        raise ValueError(f"OSMO 测试时钟状态缺少整数读数: {fields!r}")
    return state


def read_clock(path: Path, *, real_monotonic_ns: int | None = None) -> dict:
    """按绝对冻结值或真实经过时间锚点返回共享的 UTC／单调读数。"""
    state = _read_state(path)
    real_now = time.monotonic_ns() if real_monotonic_ns is None else real_monotonic_ns
    if state["mode"] == "frozen":
        utc_us = state["utc_us"]
        monotonic_ns = state["monotonic_ns"]
    else:
        delta_ns = real_now - state["real_monotonic_anchor_ns"]
        if delta_ns < 0:
            raise ValueError("OSMO 测试时钟真实单调锚点位于未来")
        utc_us = state["utc_anchor_us"] + delta_ns // 1_000
        monotonic_ns = state["monotonic_anchor_ns"] + delta_ns
    return {
        "mode": state["mode"], "epoch": state["epoch"],
        "utc_us": utc_us, "monotonic_ns": monotonic_ns,
        "real_monotonic_ns": real_now,
    }


def _write_state(path: Path, state: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.new")
    temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return state


def _next_epoch(path: Path) -> int:
    return _read_state(path)["epoch"] + 1 if path.exists() else 1


def set_frozen(path: Path, *, utc_us: int, monotonic_ns: int) -> dict:
    if type(utc_us) is not int or type(monotonic_ns) is not int:
        raise TypeError("OSMO 冻结 UTC 与单调读数必须为整数")
    return _write_state(path, {
        "mode": "frozen", "epoch": _next_epoch(path),
        "utc_us": utc_us, "monotonic_ns": monotonic_ns,
    })


def set_running(path: Path, *, utc_us: int, monotonic_ns: int) -> dict:
    if type(utc_us) is not int or type(monotonic_ns) is not int:
        raise TypeError("OSMO 推进 UTC 与单调读数必须为整数")
    return _write_state(path, {
        "mode": "running", "epoch": _next_epoch(path),
        "utc_anchor_us": utc_us, "monotonic_anchor_ns": monotonic_ns,
        "real_monotonic_anchor_ns": time.monotonic_ns(),
    })


def whole_second_utc_us(now_ns: int | None = None) -> int:
    current = time.time_ns() if now_ns is None else now_ns
    return current // 1_000_000_000 * 1_000_000


def format_utc(utc_us: int) -> str:
    value = datetime.fromtimestamp(utc_us / 1_000_000, timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _camctl_stack() -> list[dict]:
    frames = []
    frame = sys._getframe(2)
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if module.startswith("camctl"):
            frames.append({
                "module": module,
                "function": frame.f_code.co_name,
                "line": frame.f_lineno,
                "file": frame.f_code.co_filename,
            })
        frame = frame.f_back
    return frames


class ControlledClock:
    """为已显式接线的业务时钟模块记录来源，不修改全局 time。"""

    def __init__(self, state_path: Path, log_path: Path) -> None:
        self._state_path = state_path
        self._log_path = log_path

    def _read(self, kind: str) -> dict:
        real_now = time.monotonic_ns()
        reading = read_clock(self._state_path, real_monotonic_ns=real_now)
        _append_jsonl(self._log_path, {
            "pid": os.getpid(), "epoch": reading["epoch"], "kind": kind,
            "utc_us": reading["utc_us"], "monotonic_ns": reading["monotonic_ns"],
            "real_monotonic_ns": real_now, "mode": reading["mode"],
            "stack": _camctl_stack(),
        })
        return reading

    def time_ns(self) -> int:
        return self._read("time_ns")["utc_us"] * 1_000

    def monotonic_ns(self) -> int:
        return self._read("monotonic_ns")["monotonic_ns"]

    def time(self) -> float:
        return self._read("time")["utc_us"] / 1_000_000
