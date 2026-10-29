"""身份发现与安装版 hb_cli 响应解析器组合，外部命令使用受格式约束的替身。"""
from __future__ import annotations

import importlib.util
import base64
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("camera_discovery", SCRIPTS / "camera_discovery.py")
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)

ADB_LIST = b"List of devices attached\nA device product:qcs8625_ac206 model:unknown\nO device product:eagle4_oq102\n\n"
HB_LIST = b"vid=0x2756 pid=0x000a bus=1 addr=2\n"


class CameraDiscovery(unittest.TestCase):
    def run_discovery(self, *, adb=ADB_LIST, hb=HB_LIST, info=b"serial : H123\n", extra=None):
        def query(argv, *, capture_output, timeout):
            self.assertTrue(capture_output)
            self.assertGreater(timeout, 0)
            if extra:
                result = extra(argv)
                if result is not None:
                    return result
            output = adb if argv[-1] == "-l" else hb if argv[-1] == "list" else info
            return subprocess.CompletedProcess(argv, 0, output, b"")

        with patch.object(discovery.shutil, "which", side_effect=lambda name: "/tools/" + name):
            return discovery.discover_cameras(query=query)

    def test_all_three_cameras_auto_configured_and_reported(self):
        report = self.run_discovery()
        self.assertEqual(report["status"], "complete", report)
        devices = report["devices"]
        self.assertEqual(set(devices), {"action6-01", "osmo360ii-01", "hasselbla-01"})
        self.assertEqual(devices["action6-01"]["adb"]["serial"], "A")
        self.assertEqual(devices["osmo360ii-01"]["adb"]["serial"], "O")
        self.assertEqual(devices["hasselbla-01"]["hasselblad"], {
            "serial": "H123", "executable": "/tools/hb_cli", "product_id": 10, "output_storage": "host"})
        self.assertEqual(len(report["queries"]), 3)
        self.assertFalse(report["placeholder_devices"])
        self.assertFalse(report["warnings"])
        text = discovery.format_report(report)
        for value in ("H123", "/tools/hb_cli", "adb.serial", "start_timeout_s", "output_storage"):
            self.assertIn(value, text)

    def test_multiple_adb_cameras_have_distinct_deterministic_ids(self):
        rows = [b"Z device product:qcs8625_ac206\n", b"A device product:qcs8625_ac206\n",
            b"O device product:eagle4_oq102\n"]
        report = self.run_discovery(adb=b"List of devices attached\n" + b"".join(rows))
        self.assertEqual(report["status"], "complete", report)
        self.assertEqual(report["devices"]["action6-01"]["adb"]["serial"], "A")
        self.assertEqual(report["devices"]["action6-02"]["adb"]["serial"], "Z")
        self.assertEqual(report["devices"]["osmo360ii-01"]["adb"]["serial"], "O")
        reversed_report = self.run_discovery(adb=b"List of devices attached\n" + b"".join(reversed(rows)))
        self.assertEqual(report["devices"], reversed_report["devices"])

    def test_unknown_unavailable_and_missing_product_are_reported_without_guessing(self):
        adb = b"List of devices attached\nX device product:phone\nA offline product:qcs8625_ac206\nU unauthorized\nN device\n"
        report = self.run_discovery(adb=adb)
        self.assertEqual(report["status"], "complete", report)
        self.assertEqual(set(report["devices"]), {"hasselbla-01", "action6-01", "osmo360ii-01"})
        self.assertEqual(set(report["placeholder_devices"]), {"action6-01", "osmo360ii-01"})
        self.assertEqual([v["result"] for v in report["observations"][:4]],
            ["unknown_product", "unavailable", "unavailable", "unknown_product"])

    def test_absent_cameras_or_tools_generate_placeholders_and_report_required_edits(self):
        report = self.run_discovery(adb=b"List of devices attached\n", hb=b"(no Hasselblad USB devices found)\n")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(set(report["devices"]), {"hasselbla-01", "action6-01", "osmo360ii-01"})
        self.assertEqual(set(report["placeholder_devices"]), set(report["devices"]))
        self.assertEqual(report["devices"]["action6-01"]["adb"]["serial"], "REPLACE_ACTION6_SERIAL")
        self.assertEqual(report["devices"]["osmo360ii-01"]["adb"]["serial"], "REPLACE_OSMO360II_SERIAL")
        self.assertEqual(report["devices"]["hasselbla-01"]["hasselblad"]["serial"], "REPLACE_HASSELBLAD_SERIAL")
        self.assertEqual(report["devices"]["hasselbla-01"]["hasselblad"]["executable"], "/tools/hb_cli")
        self.assertEqual(report["placeholder_devices"]["hasselbla-01"], ["hasselblad.serial"])
        self.assertTrue(report["warnings"])
        self.assertIn(report["warnings"][0], discovery.format_report(report))
        with patch.object(discovery.shutil, "which", return_value=None):
            report = discovery.discover_cameras()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(set(report["placeholder_devices"]), {"hasselbla-01", "action6-01", "osmo360ii-01"})
        self.assertEqual(report["devices"]["hasselbla-01"]["hasselblad"]["executable"], "/REPLACE_WITH_HB_CLI_PATH")
        self.assertIn("hasselblad.executable", report["placeholder_devices"]["hasselbla-01"])
        self.assertTrue(report["warnings"])
        self.assertEqual(report["tools"]["hb_cli"]["status"], "not_found")

    def test_adb_only_works_without_hb_tool(self):
        with patch.object(discovery.shutil, "which", side_effect=lambda n: "/adb" if n == "adb" else None):
            report = discovery.discover_cameras(query=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, ADB_LIST, b""))
        self.assertEqual(report["status"], "complete", report)
        self.assertEqual(set(report["devices"]), {"action6-01", "osmo360ii-01", "hasselbla-01"})
        self.assertEqual(set(report["placeholder_devices"]), {"hasselbla-01"})

    def test_multiple_hasselblads_do_not_query_arbitrary_camera(self):
        report = self.run_discovery(hb=HB_LIST + b"vid=0x2756 pid=0x000a bus=1 addr=3\n")
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("hasselbla-01", report["devices"])
        self.assertFalse(any(q["argv"][-1] == "info" for q in report["queries"]))
        text = discovery.format_report(report)
        for location in ("bus=1 addr=2", "bus=1 addr=3"):
            self.assertIn(location, text)

    def test_query_failure_timeout_or_malformed_response_is_preserved(self):
        for kind in ("failure", "timeout", "invalid_adb", "invalid_info", "invalid_utf8"):
            with self.subTest(kind=kind):
                def extra(argv):
                    if kind == "failure":
                        return subprocess.CompletedProcess(argv, 2, b"", b"USB permission denied")
                    if kind == "timeout":
                        raise subprocess.TimeoutExpired(argv, 30, output=b"partial", stderr=b"blocked")
                report = self.run_discovery(extra=extra,
                    adb=b"invalid" if kind == "invalid_adb" else ADB_LIST,
                    info=b"\xff" if kind == "invalid_utf8" else b"serial :\n" if kind == "invalid_info" else b"serial : H123\n")
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["error"])
                if kind == "failure":
                    self.assertIn("USB permission denied", report["queries"][0]["stderr"])
                if kind == "timeout":
                    self.assertTrue(report["queries"][0]["timed_out"])
                    self.assertEqual(report["queries"][0]["stdout"], "partial")
                if kind == "invalid_utf8":
                    self.assertEqual(base64.b64decode(report["queries"][-1]["stdout_base64"]), b"\xff")

    def test_explicit_hb_path_overrides_path_search(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "custom hb"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o700)
            with patch.object(discovery.shutil, "which", return_value="/other/hb_cli"):
                self.assertEqual(discovery.resolve_hb_cli(tool), str(tool))
                self.assertEqual(discovery.resolve_hb_cli(), "/other/hb_cli")


if __name__ == "__main__":
    unittest.main()
