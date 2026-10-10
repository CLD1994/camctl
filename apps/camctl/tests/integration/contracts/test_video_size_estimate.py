"""共同原始文本经过精确解析、包内能力 Schema 和完整编码。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.cli import encode_describe_document
from camctl.contracts.json_values import JsonParseError, parse_exact_json
from camctl.contracts.schemas import SchemaValidationError

_CASES = json.loads((Path(__file__).resolve().parents[5]
    / "protocol/examples/video-size-estimate/cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_common_estimate_text_matches_exact_encoding_expectation(case):
    if not case["encode_valid"]:
        with pytest.raises((JsonParseError, SchemaValidationError)):
            encode_describe_document(parse_exact_json(case["json"]))
        return
    document = parse_exact_json(case["json"])
    payload = encode_describe_document(document)
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert parse_exact_json(payload.decode("utf-8")) == document
