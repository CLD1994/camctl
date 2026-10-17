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
    TIMELAPSE_DURATIONS_S, timelapse_intervals_s, timelapse_model_ids,
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
    duration_labels = "、".join(map(seconds_label, TIMELAPSE_DURATIONS_S))
    model_views = {}
    rows = []
    model_labels = {"dji-action6": "Action6", "dji-osmo360-ii": "OSMO 360 II"}
    for model in timelapse_model_ids():
        intervals = timelapse_intervals_s(model)
        pairs = [(interval, duration) for interval in intervals
                 for duration in TIMELAPSE_DURATIONS_S
                 if Fraction(duration) > Fraction(interval)]
        if not pairs:
            raise ValueError(f"型号 {model!r} 没有合法延时参数组合")
        shortest = min(pairs, key=lambda pair: (pair[1], pair[0]))
        interval_labels = "、".join(map(seconds_label, intervals))
        label = model_labels.get(model, model)
        rows.append(
            f"{label} (`{model}`)|{interval_labels}|{duration_labels}|{len(pairs)}|"
            f"{seconds_label(shortest[0])} 间隔、{seconds_label(shortest[1])} 采集时长"
        )
        model_views[model] = {
            "interval_s": list(intervals),
            "legal_pair_count": len(pairs),
            "shortest_pair": {"interval_s": shortest[0], "duration_s": shortest[1]},
        }
    block = f"""{BEGIN}
型号|间隔允许值|采集时长允许值|合法组合数量|最短组合
-|-|-|-|-
{chr(10).join(rows)}

间隔表示相机两次采集之间的目标间隔；采集时长表示采集过程的目标持续时间，不表示输出视频的播放时长。合法组合要求 `duration_s > interval_s`。
{END}"""
    text = (REPO / DOCUMENT).read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) >= text.index(END):
        raise ValueError(f"{DOCUMENT}: 必须有且只有一个完整时间参数生成区")
    start, end = text.index(BEGIN), text.index(END) + len(END)
    resource = {"format_version": 2, "duration_s": list(TIMELAPSE_DURATIONS_S),
                "relation": "duration_s > interval_s", "models": model_views}
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
