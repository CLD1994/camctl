"""从生产时间定义生成共同资料与可独立交付的诊断 JSON。"""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "apps/camctl/src"))

from camctl.devices.drivers.adb_cameras.timelapse_parameters import (  # noqa: E402
    TIMELAPSE_DURATIONS_S, TIMELAPSE_INTERVALS_S,
)

BEGIN = "<!-- BEGIN GENERATED TIMELAPSE TIME PARAMETERS -->"
END = "<!-- END GENERATED TIMELAPSE TIME PARAMETERS -->"
DOCUMENT = Path("docs/hardware/camera-control-handoff.md")
RESOURCE = Path("docs/hardware/timelapse-time-parameters.json")


def seconds_label(value):
    if value % 3600 == 0:
        return f"{value // 3600} h"
    if value % 60 == 0:
        return f"{value // 60} min"
    return f"{value} s"


def generated_views():
    pairs = [(interval, duration) for interval in TIMELAPSE_INTERVALS_S
             for duration in TIMELAPSE_DURATIONS_S if Fraction(duration) > Fraction(interval)]
    shortest = min(pairs, key=lambda pair: (pair[1], pair[0]))
    interval_labels = "、".join(map(seconds_label, TIMELAPSE_INTERVALS_S))
    duration_labels = "、".join(map(seconds_label, TIMELAPSE_DURATIONS_S))
    block = f"""{BEGIN}
参数|允许值|含义
-|-|-
采集间隔|{interval_labels}|相机两次采集之间的目标间隔，共 {len(TIMELAPSE_INTERVALS_S)} 个允许值。
采集时长|{duration_labels}|采集过程的目标持续时间，共 {len(TIMELAPSE_DURATIONS_S)} 个允许值；不表示输出视频的播放时长。

合法组合要求 `duration_s > interval_s`，共有 {len(pairs)} 种。最短组合为 {seconds_label(shortest[0])} 间隔、{seconds_label(shortest[1])} 采集时长。
{END}"""
    text = (REPO / DOCUMENT).read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) >= text.index(END):
        raise ValueError(f"{DOCUMENT}: 必须有且只有一个完整时间参数生成区")
    start, end = text.index(BEGIN), text.index(END) + len(END)
    resource = {"format_version": 1, "interval_s": list(TIMELAPSE_INTERVALS_S),
                "duration_s": list(TIMELAPSE_DURATIONS_S), "relation": "duration_s > interval_s",
                "legal_pair_count": len(pairs),
                "shortest_pair": {"interval_s": shortest[0], "duration_s": shortest[1]}}
    return {DOCUMENT: text[:start] + block + text[end:],
            RESOURCE: json.dumps(resource, ensure_ascii=False, indent=2) + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write", action="store_true", help="更新派生资料和资源")
    modes.add_argument("--check", action="store_true", help="检查派生资料和资源漂移")
    args = parser.parse_args()
    try:
        views = generated_views()
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    stale = []
    for relative, expected in views.items():
        target = REPO / relative
        if target.exists() and target.read_text(encoding="utf-8") == expected:
            continue
        if args.write:
            target.write_text(expected, encoding="utf-8")
            print(f"已更新 {relative}")
        else:
            stale.append(relative)
            print(f"{relative}: 派生内容缺失或与生产时间定义不一致；请运行本脚本 --write", file=sys.stderr)
    return int(bool(stale))


if __name__ == "__main__":
    raise SystemExit(main())
