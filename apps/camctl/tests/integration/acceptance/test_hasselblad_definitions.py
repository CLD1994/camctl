"""真实部署目录、首次受理和历史恢复固定 Hasselblad 正式源选择。"""

import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from io import StringIO

import pytest

from camctl.acceptance.definitions import read_action_spec
from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, PlanDisposition, accept_input
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.cli import main
from camctl.contracts.enums import enum_for
from camctl.contracts.input_fields import reconstruct_action_input
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import (
    DRIVER_ID,
    PARAMETER_TYPE,
    driver_definition,
)
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import encode_json_value, row_facts
from .test_acceptance import RealFileReader, _NOW, _plan_body
from .test_fields import _action_facts
from ..persistence.test_runtime import _create_valid_database

pytestmark = pytest.mark.asyncio


PHOTO_PARAMS = {
    "type": "hasselblad_photo",
    "capture_format": "raw",
    "exposure": {
        "mode": "M", "iso": 800, "aperture": Decimal("8"),
        "shutter_s": Decimal("0.000667"),
    },
    "white_balance": {"mode": "manual", "temperature_k": 5500},
    "focus": {"mode": "MF", "relative_move": 5},
}


@pytest.fixture
def environment(tmp_path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    context = AcceptanceContext(
        mode=CommandMode.RUN, catalog=_catalog(), repository=AcceptanceRepository(),
        clock=type("Clock", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
    )
    yield owned, context
    owned.connection.close()


async def _accept(environment, tmp_path, body):
    owned, context = environment
    input_path = tmp_path / "input.json"
    input_path.write_text(encode_json_value(body), encoding="utf-8")
    read = await read_input(str(input_path), RealFileReader())
    return await accept_input(parse_input(read), context, new_operation_key(), owned)


def _catalog(storage=None):
    camera = {"executable": "/no/such/hb_cli", "serial": "SERIAL-1"}
    if storage is not None:
        camera["output_storage"] = storage
    config = load_config({"devices": {"cam-1": {
        "kind": "camera", "driver": DRIVER_ID, "hasselblad": camera,
    }}}, ConfigDefaults())
    return build_catalog(config, DriverDefinitions({DRIVER_ID: driver_definition()}))


@pytest.mark.parametrize("storage,expected", [(None, "host"), ("host", "host"), ("device", "device")])
async def test_photo_acceptance_saves_explicit_params_and_call_budget(environment, tmp_path, storage, expected):
    owned, context = environment
    connection = owned.connection
    body = _plan_body()
    body["actions"][0]["params"] = deepcopy(PHOTO_PARAMS)
    outcome = await _accept((owned, replace(context, catalog=_catalog(storage))), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    row = _action_facts(connection).tables["actions"][1]
    assert row["status"] == int(enum_for("actions.status").PENDING)
    expected_params = parse_exact_json(encode_json_value(PHOTO_PARAMS))
    assert reconstruct_action_input(row)["params"] == expected_params
    assert parse_exact_json(row["effective_params_json"]) == expected_params
    assert read_action_spec(row) == {"output_storage": expected, "start_call_timeout_s": 180}
    history = HistoryRepository(tmp_path / "state.db")
    saved = history.restore_entity("action", 1, history.current_boundary())[("actions", 1)]
    assert read_action_spec(saved) == {"output_storage": expected, "start_call_timeout_s": 180}


@pytest.mark.parametrize("first,current", [("host", "device"), ("device", "host")])
async def test_repeated_request_and_history_keep_original_storage(environment, tmp_path, first, current):
    owned, context = environment
    connection = owned.connection
    body = _plan_body()
    body["actions"][0]["params"] = deepcopy(PHOTO_PARAMS)
    await _accept((owned, replace(context, catalog=_catalog(first))), tmp_path, body)
    history = HistoryRepository(tmp_path / "state.db")
    boundary = history.current_boundary()
    outcome = await _accept((owned, replace(context, catalog=_catalog(current))), tmp_path, {"request_id": "42"})
    assert outcome.plan_disposition is PlanDisposition.REUSED
    assert read_action_spec(_action_facts(connection).tables["actions"][1]) == {
        "output_storage": first, "start_call_timeout_s": 180,
    }
    historical = history.restore_entity("action", 1, boundary)[("actions", 1)]
    assert read_action_spec(historical) == {"output_storage": first, "start_call_timeout_s": 180}
    body["request_id"] = "43"
    await _accept((owned, replace(context, catalog=_catalog(current))), tmp_path, body)
    assert read_action_spec(row_facts(connection, "actions", 2)) == {
        "output_storage": current, "start_call_timeout_s": 180,
    }


@pytest.mark.parametrize("field", ["output_storage", "storage"])
async def test_plan_storage_field_is_rejected_by_the_original_parameter_schema(environment, tmp_path, field):
    owned, context = environment
    connection = owned.connection
    body = _plan_body()
    body["actions"][0]["params"] = {**deepcopy(PHOTO_PARAMS), field: "device"}
    outcome = await _accept((owned, replace(context, catalog=_catalog())), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    row = _action_facts(connection).tables["actions"][1]
    assert row["status"] == int(enum_for("actions.status").FAILED)
    assert read_action_spec(row) is None
    assert row["effective_params_json"] is None
    assert connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == 0


async def test_describe_exports_hasselblad_without_device_or_executable(tmp_path):
    missing_tool = tmp_path / "missing-hb_cli"
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            (
                "[devices.hb0]",
                'kind = "camera"',
                'driver = "hasselblad_cfv100c"',
                "[devices.hb0.hasselblad]",
                f'executable = "{missing_tool}"',
                'serial = "SERIAL-H1"',
                "",
            )
        ),
        encoding="utf-8",
    )
    stdout, stderr = StringIO(), StringIO()

    assert not missing_tool.exists()
    assert main(["describe", "--config", str(config)], stdout, stderr) == 0
    described = json.loads(stdout.getvalue(), parse_float=Decimal)
    parameter = described["devices"][0]["actions"][0]["parameter_types"][0]

    assert parameter["type"] == PARAMETER_TYPE
    assert parameter["name"] == "Hasselblad 单张拍照"
    assert parameter["preview_supported"] is True
    assert Decimal("0.000667") in {
        choice["const"]
        for branch in parameter["schema"]["properties"]["exposure"]["oneOf"]
        for choice in branch["properties"].get("shutter_s", {}).get("oneOf", ())
    }
    assert stdout.getvalue().count("\n") == 1
    assert stderr.getvalue() == ""


async def test_first_import_under_low_decimal_precision_keeps_shutter_domain_exact():
    script = """
import sys
sys.dont_write_bytecode = True
from decimal import Decimal, localcontext
with localcontext() as context:
    context.prec = 1
    from camctl.devices.drivers.hasselblad.parameters import parse_photo_settings, photo_schema

schema = photo_schema()
manual = next(
    branch for branch in schema["properties"]["exposure"]["oneOf"]
    if branch["properties"]["mode"]["const"] == "M"
)
shutters = {choice["const"] for choice in manual["properties"]["shutter_s"]["oneOf"]}
assert Decimal("0.00025") in shutters
assert Decimal("0.0002") not in shutters
assert Decimal("0.167") in shutters
assert Decimal("0.00556") in shutters

def params(shutter):
    return {
        "type": "hasselblad_photo",
        "capture_format": "raw",
        "exposure": {
            "mode": "M", "iso": 800, "aperture": Decimal("8"),
            "shutter_s": Decimal(shutter),
        },
        "white_balance": {"mode": "auto"},
        "focus": {"mode": "MF"},
    }

assert parse_photo_settings(params("0.00025")).exposure.shutter_s == Decimal("0.00025")
from camctl.acceptance.schema import BodySchemaError
try:
    parse_photo_settings(params("0.0002"))
except BodySchemaError:
    pass
else:
    raise AssertionError("0.0002 must be rejected")
print("finite=0.00025; invalid=0.0002; recurring=0.167,0.00556")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "finite=0.00025; invalid=0.0002; recurring=0.167,0.00556"
