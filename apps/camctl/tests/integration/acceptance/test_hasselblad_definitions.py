"""真实部署目录、首次受理和历史恢复固定 Hasselblad 正式源选择。"""

from dataclasses import replace

import pytest

from camctl.acceptance.definitions import read_action_spec
from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, PlanDisposition, accept_input
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts
from .test_acceptance import RealFileReader, _NOW, _plan_body, _write_input
from .test_fields import _action_facts
from ..persistence.test_runtime import _create_valid_database

pytestmark = pytest.mark.asyncio


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
    read = await read_input(str(_write_input(tmp_path, body)), RealFileReader())
    return await accept_input(parse_input(read), context, new_operation_key(), owned)


def _catalog(storage=None):
    camera = {"executable": "/usr/bin/hb_cli", "serial": "SERIAL-1"}
    if storage is not None:
        camera["output_storage"] = storage
    config = load_config({"devices": {"cam-1": {
        "kind": "camera", "driver": DRIVER_ID, "hasselblad": camera,
    }}}, ConfigDefaults())
    return build_catalog(config, DriverDefinitions({DRIVER_ID: driver_definition()}))


@pytest.mark.parametrize("storage,expected", [(None, "host"), ("host", "host"), ("device", "device")])
async def test_admission_saves_deployed_storage_and_complete_deadline(environment, tmp_path, storage, expected):
    owned, context = environment
    connection = owned.connection
    body = _plan_body()
    body["actions"][0]["params"] = {"type": "hasselblad_photo"}
    outcome = await _accept((owned, replace(context, catalog=_catalog(storage))), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    row = _action_facts(connection).tables["actions"][1]
    assert row["status"] == int(enum_for("actions.status").PENDING)
    assert parse_exact_json(row["effective_params_json"]) == {"type": "hasselblad_photo"}
    assert read_action_spec(row) == {"output_storage": expected, "start_call_timeout_s": 180}
    history = HistoryRepository(tmp_path / "state.db")
    saved = history.restore_entity("action", 1, history.current_boundary())[("actions", 1)]
    assert read_action_spec(saved) == {"output_storage": expected, "start_call_timeout_s": 180}


@pytest.mark.parametrize("first,current", [("host", "device"), ("device", "host")])
async def test_repeated_request_and_history_keep_original_storage(environment, tmp_path, first, current):
    owned, context = environment
    connection = owned.connection
    body = _plan_body()
    body["actions"][0]["params"] = {"type": "hasselblad_photo"}
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
    body["actions"][0]["params"] = {"type": "hasselblad_photo", field: "device"}
    outcome = await _accept((owned, replace(context, catalog=_catalog())), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    row = _action_facts(connection).tables["actions"][1]
    assert row["status"] == int(enum_for("actions.status").FAILED)
    assert read_action_spec(row) is None
    assert row["effective_params_json"] is None
    assert connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == 0
