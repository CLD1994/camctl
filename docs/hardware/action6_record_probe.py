"""Action6 录像诊断；需要同目录的 collect_call.py，使用 Python 3.11。

本脚本只检查已采集的响应样式，不提供正式驱动完成契约。
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


# 此录像组合取自 commands.settings_for 和 action6-record.json。
# 光圈尚待资料方确认，不在诊断步骤中发送。
SETTINGS = (
    ("01-mode", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01"),
    ("02-resolution", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000"),
    ("03-pro", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101"),
    ("04-exposure", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100"),
    ("05-wb", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000"),
    ("06-ev", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10"),
    ("07-colour", "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42 3d"),
    ("08-fov", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101"),
    ("09-stabilization", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100"),
)


def expect_ack(stdout: bytes) -> None:
    if stdout.count(b"Resp message,") != 1:
        raise RuntimeError("未取得唯一完整的设备响应")
    block = stdout.split(b"Resp message,", 1)[1]
    match = re.fullmatch(rb"\s*len = (\d+), data:\s*([0-9a-fA-F \t\r\n]*)", block)
    if match is None:
        raise RuntimeError("设备响应格式与已采集样例不同")
    try:
        payload = bytes.fromhex(match[2].decode("ascii"))
    except ValueError as error:
        raise RuntimeError("设备响应数据不完整") from error
    if len(payload) != int(match[1]):
        raise RuntimeError("设备响应长度与数据不一致")
    if payload != b"\x00":
        raise RuntimeError(f"设备响应为 {payload.hex()}，预期为单字节 00；具体含义尚未确定")


def assess_copy(source_before: tuple[int, str], source_after: tuple[int, str],
                local: tuple[int, str]) -> dict[str, bool]:
    return {
        "size_observation_changed": source_before[0] != source_after[0],
        "digest_observation_changed": source_before[1] != source_after[1],
        "matches_source_after": local == source_after,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="重新配置 Action6，试录十秒并核对视频副本")
    parser.add_argument("--adb", default="./adb.exe")
    parser.add_argument("--serial", default="123456789ABCDEF")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("请使用现有 Python 3.11 环境")

    collector = Path(__file__).resolve().with_name("collect_call.py")
    if not collector.is_file():
        raise RuntimeError("请将 collect_call.py 放在本脚本所在目录")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base = Path("action6") / ("record-probe-" + stamp)
    base.mkdir(parents=True, exist_ok=False)
    print(f"采集目录：{base.resolve()}", flush=True)

    def run(label, argv, *, allow_stderr=False):
        directory = base / label
        subprocess.run([sys.executable, str(collector), str(directory), *argv],
                       check=True, stdout=subprocess.DEVNULL)
        metadata = json.loads((directory / "call.json").read_text(encoding="utf-8"))
        stdout = (directory / "stdout.bin").read_bytes()
        stderr = (directory / "stderr.bin").read_bytes()
        if metadata["returncode"] != 0 or (stderr and not allow_stderr):
            raise RuntimeError(
                f"{label} 返回异常：退出码 {metadata['returncode']}；"
                f"stdout={stdout!r}；stderr={stderr!r}；原始记录：{directory.resolve()}"
            )
        return stdout

    def shell(label, script, *, ack=False, allow_stderr=False):
        stdout = run(label, [args.adb, "-s", args.serial, "shell", "-T",
                             "sh -c " + shlex.quote(script)], allow_stderr=allow_stderr)
        if ack:
            try:
                expect_ack(stdout)
            except RuntimeError as error:
                raise RuntimeError(f"{label}：{error}；stdout={stdout!r}；原始记录：{(base / label).resolve()}") from error
        return stdout

    def inventory(label):
        stdout = shell(label, "find /mnt/media_rw/emulated/DCIM -type f -print0\nstatus=$?\nprintf 'CAMCTL_FIND_EXIT=%s\\n' \"$status\" >&2\nexit \"$status\"\n", allow_stderr=True)
        stderr = (base / label / "stderr.bin").read_bytes()
        if stderr not in (b"CAMCTL_FIND_EXIT=0\r\n", b"CAMCTL_FIND_EXIT=0\n"):
            raise RuntimeError(f"{label} 未取得 find 的预期退出标记：{stderr!r}")
        if stdout and not stdout.endswith(b"\0"):
            raise RuntimeError(f"{label} 的目录列举不完整")
        paths = stdout.split(b"\0")[:-1] if stdout else []
        if any(not path for path in paths) or len(set(paths)) != len(paths):
            raise RuntimeError(f"{label} 的目录记录无效")
        return set(paths)

    def source_facts(label, quoted):
        size_raw = shell(label + "-size", f"test -f {quoted} && LC_ALL=C stat -c %s -- {quoted}")
        if not re.fullmatch(rb"[0-9]+\s*", size_raw) or int(size_raw) <= 0:
            raise RuntimeError(f"{label} 的源文件长度无效：{size_raw!r}")
        digest_raw = shell(label + "-digest", f"LC_ALL=C sha256sum < {quoted}")
        digest_match = re.fullmatch(rb"([0-9a-f]{64})\s+-\s*", digest_raw)
        if digest_match is None:
            raise RuntimeError(f"{label} 的源端摘要格式异常：{digest_raw!r}")
        return {"bytes": int(size_raw), "sha256": digest_match[1].decode("ascii")}

    # 先验证本地主机工具可运行，之后才改变相机设置。
    run("00-ffprobe-version", [args.ffprobe, "-version"])
    for label, script in SETTINGS:
        shell(label, script, ack=True)
        print(f"{label}：收到预期 00 响应", flush=True)
    bitrate = shell("10-bitrate", "simulate_device -s bitrate 2")
    if not all(marker in bitrate for marker in (
        b"name [DeviceRecordRecSettingBitRate]", b"link to server rlt 0", b"register to server successs",
    )):
        raise RuntimeError(f"10-bitrate 未取得已采集的属性及服务注册日志：{bitrate!r}")
    print("光圈待确认；码率命令已返回，其实际生效仍待确认。", flush=True)

    before = inventory("11-before")
    try:
        shell("12-start", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01", ack=True)
        print("启动调用已返回，主机等待 10 秒后发送停止。", flush=True)
        time.sleep(10)
    finally:
        shell("13-stop", "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00", ack=True)
    after = inventory("14-after")
    new_files = sorted(after - before)
    videos = [path for path in new_files if path.lower().endswith(b".mp4")]
    if not videos:
        raise RuntimeError(f"本次列举未取得新增 MP4；新增路径：{new_files!r}；目录：{base.resolve()}")

    summaries = []
    for index, raw_path in enumerate(videos, 1):
        source = raw_path.decode("utf-8")
        quoted = shlex.quote(source)
        prefix = f"video-{index:02d}"
        source_before = source_facts(prefix, quoted)
        local = (base / (prefix + ".mp4")).resolve()
        run(prefix + "-pull", [args.adb, "-s", args.serial, "pull", source, str(local)], allow_stderr=True)
        with local.open("rb") as content:
            local_digest = hashlib.file_digest(content, "sha256").hexdigest()
        local_facts = {"bytes": local.stat().st_size, "sha256": local_digest}
        source_after = source_facts(prefix + "-after", quoted)
        assessment = assess_copy((source_before["bytes"], source_before["sha256"]),
                                 (source_after["bytes"], source_after["sha256"]),
                                 (local_facts["bytes"], local_facts["sha256"]))
        copy_record = {"source": source, "local": str(local), "source_before": source_before,
                       "source_after": source_after, "local_facts": local_facts, **assessment}
        copy_file = base / (prefix + "-copy.json")
        copy_file.write_text(json.dumps(copy_record, ensure_ascii=False, indent=2), encoding="utf-8")
        if not assessment["matches_source_after"]:
            raise RuntimeError(
                f"{local} 与下载后源观测不一致：副本={local_facts}；源={source_after}；"
                f"比对记录：{copy_file.resolve()}"
            )
        probe_raw = run(prefix + "-ffprobe", [args.ffprobe, "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration:format=format_name,duration,size",
            "-of", "json", str(local)])
        media = json.loads(probe_raw)
        streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "video"]
        duration = Decimal(media.get("format", {}).get("duration", "NaN"))
        if not streams or not duration.is_finite() or duration <= 0:
            raise RuntimeError(f"{local} 未取得视频流或有效容器时长")
        summaries.append({**copy_record, **local_facts,
                          "duration_s": str(duration), "video_streams": streams})

    summary = {"new_files": [path.decode("utf-8") for path in new_files], "videos": summaries,
               "aperture": "unconfirmed", "bitrate_effect": "unconfirmed"}
    (base / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("试录及副本一致性检查完成。媒体信息：", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"详细记录：{(base / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
