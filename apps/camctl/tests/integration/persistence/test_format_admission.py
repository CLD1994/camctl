"""数据库整体格式限制新的 fixedspec 受理事实。"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode, ProcessInput
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.values import new_operation_key, to_utc_micros
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import (
    DRIVER_ID,
    driver_definition,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.runtime import (
    DatabaseUnsupportedFormatError,
    DbConfig,
    DbOpenMode,
    open_existing,
)

from .test_runtime import _create_valid_database

_NOW = to_utc_micros("2026-10-12 09:00:00")


def _catalog(tmp_path: Path):
    config = load_config(
        {
            "paths": {
                "state_db": str(tmp_path / "state.db"),
                "staging": str(tmp_path / "staging"),
                "ready": str(tmp_path / "ready"),
                "processing": str(tmp_path / "processing"),
            },
            "devices": {
                "cam-1": {
                    "kind": "camera",
                    "driver": DRIVER_ID,
                    "hasselblad": {
                        "executable": "/usr/bin/hb_cli",
                        "serial": "SERIAL-1",
                    },
                }
            },
        },
        ConfigDefaults(),
    )
    return build_catalog(config, DriverDefinitions({DRIVER_ID: driver_definition()}))


def _host_photo_input() -> ParsedInput:
    return ParsedInput(
        "hasselblad.json",
        {
            "request_id": "101",
            "created_at": "2026-10-12 08:00:00",
            "name": "主机 RAW 拍摄",
            "actions": [
                {
                    "name": "单张 RAW",
                    "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": "2026-10-12 09:00:00",
                    "params": {
                        "type": "hasselblad_photo", "capture_format": "raw",
                        "exposure": {"mode": "M", "iso": 800, "aperture": 8, "shutter_s": Decimal("0.008")},
                        "white_balance": {"mode": "manual", "temperature_k": 5500},
                        "focus": {"mode": "MF", "relative_move": 5},
                    },
                    "policy": {"max_delay_ms": 5000},
                }
            ],
        },
    )


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def test_legacy_v1_refuses_new_host_photo_spec_before_any_history(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    _create_valid_database(target, format_version=1)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    register_acceptance_guards()
    try:
        before = _row_counts(owned.connection)
        key = new_operation_key()
        outcome = AcceptanceRepository().process_input(
            ProcessInput(
                _host_photo_input(),
                _catalog(tmp_path),
                CommandMode.RUN,
                _NOW,
            ),
            key,
            owned,
        )

        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(outcome.error, DatabaseUnsupportedFormatError)
        assert _row_counts(owned.connection) == before
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key = ?",
            (str(key),),
        ).fetchone() == (0,)
    finally:
        owned.connection.close()
