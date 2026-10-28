"""安装版 Hasselblad 参数在 Python、浏览器和客户端后端保持一致。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from camctl.contracts.json_values import parse_exact_json


REPO = Path(__file__).resolve().parents[2]
PROJECT = REPO / "apps/camctl"
CLIENT = REPO / "apps/client"
CLIENT_DRIVER = REPO / "tests/integration/client_driver.ts"
DEVICE_ID = "hb-host"
ACTION_TYPE = "camera_take_photo"
ACTION_NAME = "Hasselblad 参数合同"


def _photo_params(
    *,
    capture_format: str = '"raw"',
    exposure: str = '{"mode":"M","iso":400,"aperture":5.6,"shutter_s":"1/1000"}',
    white_balance: str = '{"mode":"auto"}',
    focus: str = '{"mode":"MF"}',
    type_value: str = '"hasselblad_photo"',
    omit: frozenset[str] = frozenset(),
    extra_fields: tuple[tuple[str, str], ...] = (),
) -> str:
    fields = (
        ("type", type_value),
        ("capture_format", capture_format),
        ("exposure", exposure),
        ("white_balance", white_balance),
        ("focus", focus),
        *extra_fields,
    )
    return "{" + ",".join(
        f"{json.dumps(name)}:{value}" for name, value in fields if name not in omit
    ) + "}"


def _case(name: str, params_text: str, valid: bool) -> dict[str, Any]:
    return {"name": name, "params_text": params_text, "valid": valid}


# 每项只保存一个代表输入及独立预期，不在测试中复制完整设备档位。
PARAMETER_CASES = (
    _case("raw-manual-short-1-1000", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6,"shutter_s":"1/1000"}'), True),
    _case("raw-manual-short-1-2000", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6,"shutter_s":"1/2000"}'), True),
    _case("raw-manual-short-1-4000", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6,"shutter_s":"1/4000"}'), True),
    _case("jpeg-aperture-priority", _photo_params(capture_format='"jpeg"', exposure='{"mode":"A","iso":800,"aperture":8}', white_balance='{"mode":"daylight"}', focus='{"mode":"AF-S"}'), True),
    _case("jpeg-shutter-priority", _photo_params(capture_format='"jpeg"', exposure='{"mode":"S","iso":200,"shutter_s":"1/1000"}', white_balance='{"mode":"cloudy"}', focus='{"mode":"MF","relative_move":-5}'), True),
    _case("raw-program", _photo_params(exposure='{"mode":"P","iso":100}', white_balance='{"mode":"shade"}', focus='{"mode":"MF","relative_move":5}'), True),
    _case("white-balance-auto", _photo_params(white_balance='{"mode":"auto"}'), True),
    _case("white-balance-daylight", _photo_params(white_balance='{"mode":"daylight"}'), True),
    _case("white-balance-cloudy", _photo_params(white_balance='{"mode":"cloudy"}'), True),
    _case("white-balance-shade", _photo_params(white_balance='{"mode":"shade"}'), True),
    _case("white-balance-tungsten", _photo_params(white_balance='{"mode":"tungsten"}'), True),
    _case("white-balance-fluorescent", _photo_params(white_balance='{"mode":"fluorescent"}'), True),
    _case("white-balance-flash", _photo_params(white_balance='{"mode":"flash"}'), True),
    _case("white-balance-manual-minimum", _photo_params(white_balance='{"mode":"manual","temperature_k":2000}'), True),
    _case("white-balance-manual-maximum", _photo_params(white_balance='{"mode":"manual","temperature_k":10000}'), True),
    _case("focus-manual-without-move", _photo_params(focus='{"mode":"MF"}'), True),
    _case("focus-manual-with-move", _photo_params(focus='{"mode":"MF","relative_move":5}'), True),
    _case("focus-autofocus", _photo_params(focus='{"mode":"AF-S"}'), True),
    _case("numeric-shutter-rejected", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6,"shutter_s":0.0002500000000000000001}'), False),
    _case("white-balance-manual-below-minimum", _photo_params(white_balance='{"mode":"manual","temperature_k":1999}'), False),
    _case("white-balance-manual-above-maximum", _photo_params(white_balance='{"mode":"manual","temperature_k":10001}'), False),
    _case("manual-exposure-extra-compensation", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6,"shutter_s":"1/1000","compensation_ev":0}'), False),
    _case("aperture-priority-extra-shutter", _photo_params(exposure='{"mode":"A","iso":400,"aperture":5.6,"shutter_s":"1/1000"}'), False),
    _case("shutter-priority-extra-aperture", _photo_params(exposure='{"mode":"S","iso":400,"shutter_s":"1/1000","aperture":5.6}'), False),
    _case("program-extra-aperture", _photo_params(exposure='{"mode":"P","iso":400,"aperture":5.6}'), False),
    _case("program-extra-shutter", _photo_params(exposure='{"mode":"P","iso":400,"shutter_s":"1/1000"}'), False),
    _case("auto-white-balance-extra-temperature", _photo_params(white_balance='{"mode":"auto","temperature_k":5600}'), False),
    _case("manual-white-balance-extra-tint", _photo_params(white_balance='{"mode":"manual","temperature_k":5600,"tint":0}'), False),
    _case("auto-white-balance-extra-tint", _photo_params(white_balance='{"mode":"auto","tint":0}'), False),
    _case("autofocus-extra-relative-move", _photo_params(focus='{"mode":"AF-S","relative_move":5}'), False),
    _case("root-extra-tint", _photo_params(extra_fields=(("tint", "0"),)), False),
    _case("missing-type", _photo_params(omit=frozenset({"type"})), False),
    _case("null-type", _photo_params(type_value="null"), False),
    _case("missing-capture-format", _photo_params(omit=frozenset({"capture_format"})), False),
    _case("null-capture-format", _photo_params(capture_format="null"), False),
    _case("missing-exposure", _photo_params(omit=frozenset({"exposure"})), False),
    _case("null-exposure", _photo_params(exposure="null"), False),
    _case("missing-white-balance", _photo_params(omit=frozenset({"white_balance"})), False),
    _case("null-white-balance", _photo_params(white_balance="null"), False),
    _case("missing-focus", _photo_params(omit=frozenset({"focus"})), False),
    _case("null-focus", _photo_params(focus="null"), False),
    _case("missing-exposure-mode", _photo_params(exposure='{"iso":400,"aperture":5.6,"shutter_s":"1/1000"}'), False),
    _case("null-exposure-mode", _photo_params(exposure='{"mode":null,"iso":400,"aperture":5.6,"shutter_s":"1/1000"}'), False),
    _case("missing-exposure-iso", _photo_params(exposure='{"mode":"M","aperture":5.6,"shutter_s":"1/1000"}'), False),
    _case("null-exposure-iso", _photo_params(exposure='{"mode":"M","iso":null,"aperture":5.6,"shutter_s":"1/1000"}'), False),
    _case("missing-exposure-aperture", _photo_params(exposure='{"mode":"M","iso":400,"shutter_s":"1/1000"}'), False),
    _case("null-exposure-aperture", _photo_params(exposure='{"mode":"M","iso":400,"aperture":null,"shutter_s":"1/1000"}'), False),
    _case("missing-exposure-shutter", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6}'), False),
    _case("null-exposure-shutter", _photo_params(exposure='{"mode":"M","iso":400,"aperture":5.6,"shutter_s":null}'), False),
    _case("missing-white-balance-mode", _photo_params(white_balance="{}"), False),
    _case("null-white-balance-mode", _photo_params(white_balance='{"mode":null}'), False),
    _case("missing-manual-temperature", _photo_params(white_balance='{"mode":"manual"}'), False),
    _case("null-manual-temperature", _photo_params(white_balance='{"mode":"manual","temperature_k":null}'), False),
    _case("missing-focus-mode", _photo_params(focus="{}"), False),
    _case("null-focus-mode", _photo_params(focus='{"mode":null}'), False),
    _case("null-optional-relative-move", _photo_params(focus='{"mode":"MF","relative_move":null}'), False),
)


class InstalledCamctl:
    def __init__(self, root: Path, python: Path, environment: dict[str, str]) -> None:
        self.root = root
        self.python = python
        self.environment = environment

    def run(self, command, *, cwd: Path | None = None, timeout: int = 180):
        argv = [str(part) for part in command]
        working_directory = cwd or self.root
        started = datetime.now(timezone.utc).isoformat()
        completed = subprocess.run(
            argv,
            cwd=working_directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        log_directory = self.root / "commands"
        log_directory.mkdir(exist_ok=True)
        log_path = log_directory / f"{len(list(log_directory.glob('*.json'))) + 1:03}.json"
        log_path.write_text(json.dumps({
            "argv": argv,
            "cwd": str(working_directory),
            "python": sys.version,
            "node": subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip(),
            "head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip(),
            "started_utc": started,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        assert completed.returncode == 0, (
            f"安装版命令失败，详情见 {log_path}: {argv!r}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
        return completed

    def cli(self, *args: str):
        return self.run([self.python, "-m", "camctl", *args])


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in {"PYTHONPATH", "PYTHONHOME", "PWD"} or name.startswith("CAMCTL_TEST_"):
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["UV_PYTHON"] = sys.executable
    environment["UV_OFFLINE"] = "1"
    return environment


@pytest.fixture(scope="module")
def installed_camctl(tmp_path_factory) -> InstalledCamctl:
    assert sys.version_info[:2] == (3, 11), "安装版验证使用部署版 Python 3.11"
    root = tmp_path_factory.mktemp("hasselblad-photo-installed")
    environment = _clean_environment()
    package = InstalledCamctl(root, root / "venv/bin/python", environment)
    package.run(["uv", "build", "--project", PROJECT, "--wheel", "--out-dir", root / "dist"])
    wheels = list((root / "dist").glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    requirements = root / "requirements.txt"
    package.run(["uv", "export", "--project", PROJECT, "--frozen", "--no-dev",
                 "--no-emit-project", "-o", requirements])
    package.run(["uv", "venv", "--python", sys.executable, root / "venv"])
    package.run(["uv", "pip", "install", "--python", package.python,
                 "--require-hashes", "-r", requirements])
    package.run(["uv", "pip", "install", "--python", package.python, "--no-deps", wheel])

    guard = root / "bin"
    guard.mkdir()
    marker = root / "device-calls"
    executable = guard / "hb_cli"
    executable.write_text(
        f"#!{package.python}\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('called')\nraise SystemExit(91)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    environment["PATH"] = str(guard) + os.pathsep + environment["PATH"]
    import_path = package.run([package.python, "-c", "import camctl; print(camctl.__file__)"]).stdout.strip()
    assert Path(import_path).is_relative_to(root / "venv"), import_path
    package.device_marker = marker
    package.config_path = root / "config.toml"
    package.state_db = root / "state.db"
    package.config_path.write_text("\n".join((
        "[paths]",
        f"state_db = {json.dumps(str(package.state_db))}",
        f"log_file = {json.dumps(str(root / 'camctl.log'))}",
        f"staging = {json.dumps(str(root / 'staging'))}",
        f"ready = {json.dumps(str(root / 'ready'))}",
        f"processing = {json.dumps(str(root / 'processing'))}",
        "",
        "[devices.hb-host]",
        'kind = "camera"',
        'driver = "hasselblad_cfv100c"',
        "",
        "[devices.hb-host.hasselblad]",
        f"executable = {json.dumps(str(executable))}",
        'serial = "offline-hasselblad"',
        'output_storage = "host"',
        "",
    )), encoding="utf-8")
    package.cli("init", "--config", str(package.config_path))
    yield package


def _client_driver(input_path: Path, store: Path, receipt_path: Path):
    argv = [
        "node", "--import", "tsx", str(CLIENT_DRIVER), "validate-parameters",
        str(store), str(input_path), str(receipt_path),
    ]
    started = datetime.now(timezone.utc).isoformat()
    completed = subprocess.run(
        argv,
        cwd=CLIENT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    log_directory = input_path.parent / "client-commands"
    log_directory.mkdir(exist_ok=True)
    log_path = log_directory / f"{len(list(log_directory.glob('*.json'))) + 1:03}.json"
    log_path.write_text(json.dumps({
        "argv": argv,
        "cwd": str(CLIENT),
        "python": sys.version,
        "node": subprocess.run(["node", "--version"], cwd=CLIENT, capture_output=True, text=True).stdout.strip(),
        "head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip(),
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return completed


def _describe_hasselblad(package: InstalledCamctl) -> str:
    completed = package.cli("describe", "--config", str(package.config_path))
    assert not package.device_marker.exists(), "describe 调用了设备工具"
    return completed.stdout


def _scheduled_at(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _installed_plan(cases: tuple[dict[str, Any], ...], scheduled_at: str, request_id: str) -> str:
    action_type = json.dumps(ACTION_TYPE)
    device_id = json.dumps(DEVICE_ID)
    action_name = json.dumps(ACTION_NAME, ensure_ascii=False)
    actions = ",".join(
        "{" + ",".join((
            f'"name":{action_name}',
            f'"type":{action_type}',
            f'"device_id":{device_id}',
            f'"scheduled_at":{json.dumps(scheduled_at)}',
            '"policy":{"max_delay_ms":5000}',
            f'"params":{case["params_text"]}',
        )) + "}"
        for case in cases
    )
    body = (
        "{" + ",".join((
            f'"request_id":{json.dumps(request_id)}',
            '"name":"Hasselblad 参数判定矩阵"',
            f'"created_at":{json.dumps(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))}',
            f'"actions":[{actions}]',
        )) + "}\n"
    )
    parse_exact_json(body)
    return body


def _value_token(document: str, field: str) -> str:
    match = re.search(
        rf'"{re.escape(field)}"\s*:\s*([^,}}\s]+)',
        document,
    )
    assert match is not None, f"找不到字段 {field} 的原始值词元: {document}"
    return match.group(1)


def test_installed_hasselblad_parameter_validation_agrees_across_consumers(
    tmp_path: Path,
    installed_camctl: InstalledCamctl,
):
    description = _describe_hasselblad(installed_camctl)
    document = parse_exact_json(description)
    device = next(item for item in document["devices"] if item["device_id"] == DEVICE_ID)
    assert any(action["type"] == ACTION_TYPE for action in device["actions"])

    store = tmp_path / "client-store"
    store.mkdir()
    (store / "device-capabilities.json").write_text(description, encoding="utf-8")
    input_path = tmp_path / "hasselblad-parameter-cases.json"
    input_path.write_text(json.dumps({
        "action_type": ACTION_TYPE,
        "device_id": DEVICE_ID,
        "action_name": ACTION_NAME,
        "scheduled_at": _scheduled_at(3600),
        "cases": PARAMETER_CASES,
    }, ensure_ascii=False), encoding="utf-8")
    receipt_path = tmp_path / "client-receipt.json"
    client = _client_driver(input_path, store, receipt_path)
    assert client.returncode == 0, f"Node/Chromium 参数接缝失败:\n{client.stdout}\n{client.stderr}"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert len(receipt["cases"]) == len(PARAMETER_CASES)
    for case, observed in zip(PARAMETER_CASES, receipt["cases"], strict=True):
        assert observed["name"] == case["name"]
        assert observed["browser_valid"] == case["valid"], case["name"]
        assert observed["shared_valid"] == case["valid"], case["name"]
        assert observed["export_valid"] == case["valid"], case["name"]
        assert observed["browser_params"] == case["params_text"], case["name"]
        assert observed["shared_params"] == case["params_text"], case["name"]
        if case["valid"]:
            exported = observed["exported"]
            request = json.loads(exported)
            action = request["actions"][0]
            assert action["name"] == ACTION_NAME
            assert action["type"] == ACTION_TYPE
            assert action["device_id"] == DEVICE_ID
            assert parse_exact_json(exported)["actions"][0]["params"] == parse_exact_json(case["params_text"])
        else:
            assert observed["export_error"] == "invalid_plan", case["name"]

    for case in PARAMETER_CASES:
        if '"shutter_s"' not in case["params_text"]:
            continue
        expected_token = _value_token(case["params_text"], "shutter_s")
        observed = next(row for row in receipt["cases"] if row["name"] == case["name"])
        for consumer_text in (observed["browser_params"], observed["shared_params"]):
            assert _value_token(consumer_text, "shutter_s") == expected_token, case["name"]
        if case["valid"]:
            assert _value_token(observed["exported"], "shutter_s") == expected_token, case["name"]

    scheduled_at = _scheduled_at(3600)
    status_text = installed_camctl.run([
        installed_camctl.python,
        "-c",
        "from camctl.contracts.enums import enum_for; import json; "
        "status=enum_for('actions.status'); "
        "print(json.dumps({'pending':int(status.PENDING),'failed':int(status.FAILED)}))",
    ]).stdout
    statuses = json.loads(status_text)

    for index, case in enumerate(PARAMETER_CASES):
        request_id = str(826102100000 + index)
        plan_path = tmp_path / f"installed-parameter-plan-{index}.json"
        plan_path.write_text(_installed_plan((case,), scheduled_at, request_id), encoding="utf-8")
        installed_camctl.cli("submit", str(plan_path), "--config", str(installed_camctl.config_path))
        assert not installed_camctl.device_marker.exists(), "submit 调用了设备工具"
        with sqlite3.connect(installed_camctl.state_db) as connection:
            row = connection.execute(
                "SELECT a.status,a.error_code,a.effective_params_json,a.driver_id "
                "FROM actions AS a JOIN plans AS p ON p.id=a.plan_id "
                "WHERE p.request_id=?",
                (int(request_id),),
            ).fetchone()
        assert row is not None, case["name"]
        status, error_code, effective, driver_id = row
        if case["valid"]:
            assert status == statuses["pending"], case["name"]
            assert error_code is None, case["name"]
            assert driver_id == "hasselblad_cfv100c", case["name"]
            assert parse_exact_json(effective) == parse_exact_json(case["params_text"]), case["name"]
        else:
            assert status == statuses["failed"], case["name"]
            assert error_code is not None, case["name"]
            assert effective is None, case["name"]
            assert driver_id is None, case["name"]
    assert not installed_camctl.device_marker.exists(), "参数受理调用了设备工具"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("action_type", None),
        ("action_type", ""),
        ("device_id", None),
        ("device_id", " "),
        ("action_name", None),
        ("action_name", ""),
    ),
)
def test_validate_parameters_requires_explicit_action_identity(
    tmp_path: Path,
    field: str,
    invalid_value: str | None,
):
    value = {
        "action_type": ACTION_TYPE,
        "device_id": DEVICE_ID,
        "action_name": ACTION_NAME,
        "scheduled_at": _scheduled_at(3600),
        "cases": [],
    }
    value[field] = invalid_value
    input_path = tmp_path / "invalid-input.json"
    input_path.write_text(json.dumps(value), encoding="utf-8")
    completed = _client_driver(input_path, tmp_path / "missing-store", tmp_path / "receipt.json")
    assert completed.returncode != 0
    assert field in completed.stderr
    assert not (tmp_path / "receipt.json").exists()
