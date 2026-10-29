"""通过 ADB 和 hb_cli 的身份查询生成新实例的相机配置。"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


# 对应已核验的 adb devices -l product 字段；不按序列号或列表顺序推断。
ADB_PRODUCTS = {
    "qcs8625_ac206": ("action6", "dji-action6"),
    "eagle4_oq102": ("osmo360ii", "dji-osmo360-ii"),
}
HB_VENDOR = 0x2756
HB_PRODUCT = 0x000a
QUERY_TIMEOUT_S = 30


def resolve_hb_cli(explicit=None):
    if explicit is not None:
        path = Path(explicit).expanduser().absolute()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"--hb-cli 不是可执行文件：{path}")
        return str(path)
    found = shutil.which("hb_cli")
    return str(Path(found).absolute()) if found else None


def parse_adb_devices(output):
    lines = output.decode("utf-8", errors="strict").splitlines()
    if not lines or lines[0] != "List of devices attached":
        raise ValueError("adb devices -l 缺少设备列表表头")
    devices = []
    serials = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"ADB 设备行格式无效：{line}")
        serial, state = parts[:2]
        if serial in serials or serial.startswith("-") or "\x00" in serial:
            raise ValueError(f"ADB 设备标识无效或重复：{serial!r}")
        serials.add(serial)
        products = [part.removeprefix("product:") for part in parts[2:] if part.startswith("product:")]
        if len(products) > 1:
            raise ValueError(f"ADB product 字段重复：{serial}")
        devices.append({"serial": serial, "state": state, "product": products[0] if products else None,
            "raw": line})
    return devices


def discover_cameras(hb_cli=None, *, query=None):
    """返回身份查询事实及配置；失败通过 report 保留原调用结果。"""
    report = {"status": "running", "tools": {}, "queries": [], "observations": [], "devices": {},
        "environment": {key: os.environ[key] for key in ("ADB_SERVER_SOCKET",) if key in os.environ}}
    execute = query or subprocess.run

    def streams(stdout, stderr):
        return {"stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "stdout_base64": base64.b64encode(stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(stderr).decode("ascii")}

    def run(argv):
        record = {"argv": argv, "timeout_s": QUERY_TIMEOUT_S}
        report["queries"].append(record)
        try:
            result = execute(argv, capture_output=True, timeout=QUERY_TIMEOUT_S)
        except subprocess.TimeoutExpired as error:
            record.update(returncode=None, timed_out=True,
                **streams(error.stdout or b"", error.stderr or b""))
            raise ValueError(f"设备身份查询超过 {QUERY_TIMEOUT_S} 秒：{argv}") from error
        except OSError as error:
            record.update(returncode=None, error=str(error), errno=error.errno)
            raise
        record.update(returncode=result.returncode, **streams(result.stdout, result.stderr))
        if result.returncode:
            raise ValueError(f"设备身份查询失败，退出码 {result.returncode}：{argv}；{record['stderr'] or record['stdout']}")
        return result.stdout

    try:
        adb = shutil.which("adb")
        report["tools"]["adb"] = {"path": adb, "status": "available" if adb else "not_found"}
        hb = resolve_hb_cli(hb_cli)
        report["tools"]["hb_cli"] = {"path": hb, "source": "--hb-cli" if hb_cli else "PATH",
            "status": "available" if hb else "not_found"}
        matched = []
        if adb:
            for device in parse_adb_devices(run([adb, "devices", "-l"])):
                observation = {"transport": "adb", **device}
                report["observations"].append(observation)
                if device["state"] != "device":
                    observation["result"] = "unavailable"
                elif device["product"] not in ADB_PRODUCTS:
                    observation["result"] = "unknown_product"
                else:
                    prefix, driver = ADB_PRODUCTS[device["product"]]
                    observation.update(result="identified", driver=driver)
                    matched.append((prefix, driver, device["serial"]))
        counts = Counter()
        for prefix, driver, serial in sorted(matched):
            counts[prefix] += 1
            device_id = f"{prefix}-{counts[prefix]:02d}"
            report["devices"][device_id] = {"kind": "camera", "driver": driver, "adb": {"serial": serial}}

        if hb:
            # 使用交付 wheel 中已经核验的响应解析器，避免另行维护 hb_cli 格式。
            from camctl.devices.drivers.hasselblad.responses import parse_devices, parse_serial
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID

            cameras = parse_devices(run([hb, "--pid", f"0x{HB_PRODUCT:04x}", "list"]))
            matching = [camera for camera in cameras
                if camera.vendor_id == HB_VENDOR and camera.product_id == HB_PRODUCT]
            report["observations"].extend({"transport": "usb", "vendor_id": camera.vendor_id,
                "product_id": camera.product_id, "bus": camera.bus, "address": camera.address,
                "result": "matching_product" if camera in matching else "unknown_product"} for camera in cameras)
            if len(matching) > 1:
                locations = ", ".join(f"bus={camera.bus} addr={camera.address}" for camera in matching)
                raise ValueError(f"发现 {len(matching)} 台 CFV 100C（{locations}），hb_cli 只能按 PID 选择，无法唯一绑定；请只连接本实例使用的相机")
            if matching:
                serial = parse_serial(run([hb, "--pid", f"0x{HB_PRODUCT:04x}", "info"]))
                report["devices"]["hasselbla-01"] = {"kind": "camera", "driver": DRIVER_ID,
                    "hasselblad": {"executable": hb, "serial": serial, "product_id": HB_PRODUCT,
                        "output_storage": "host"},
                    "recording": {"start_timeout_s": 30}, "connection": {"prepare_lead_s": 30},
                    "result_check": {"call_timeout_s": 180}}
        if not report["devices"]:
            raise ValueError("未识别到可配置的相机；请核对工具路径、USB/ADB 连接与权限，详见检测报告")
        report["status"] = "complete"
    except (OSError, ValueError, ImportError) as error:
        report.update(status="failed", error=str(error))
    return report


def format_report(report):
    lines = ["相机自动配置："]
    for name, tool in report["tools"].items():
        lines.append(f"  {name}: {tool['path'] or '未找到，未检测该通道'}")
    for name, value in report["environment"].items():
        lines.append(f"  {name} = {value}")
    for device_id, device in report["devices"].items():
        lines.append(f"  设备 {device_id}：")
        for section, value in device.items():
            if isinstance(value, dict):
                for key, item in value.items():
                    lines.append(f"    {section}.{key} = {json.dumps(item, ensure_ascii=False)}")
            else:
                lines.append(f"    {section} = {json.dumps(value, ensure_ascii=False)}")
    for observation in report["observations"]:
        if observation["result"] in ("unavailable", "unknown_product"):
            lines.append("  未配置设备：" + json.dumps(observation, ensure_ascii=False))
    if report["status"] == "failed":
        lines.append("  检测失败：" + report["error"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hb-cli", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = discover_cameras(args.hb_cli)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(format_report(report))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
