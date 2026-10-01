"""公共请求身份不接受隐式类型或写法转换。"""
import pytest

from camctl.acceptance import rules
from camctl.contracts.json_values import MISSING


@pytest.mark.parametrize("raw", [MISSING, None, True, 42, [], {}, "", "0", "042", "+42", " 42 ", "4e1", "４２", "9223372036854775808"])
def test_extract_identity_preserves_invalid_input(raw):
    document = {} if raw is MISSING else {"request_id": raw}
    decision = rules.extract_request_identity(document)
    assert decision.request_id is None
    assert decision.error["details"]["field"] == "request_id"
    if raw is MISSING:
        assert "value" not in decision.error["details"]
    else:
        assert decision.error["details"]["value"] == raw


@pytest.mark.parametrize("raw", ["1", "42", "9007199254740992", "9223372036854775807"])
def test_extract_identity_accepts_full_integer_range(raw):
    assert rules.extract_request_identity({"request_id": raw}).request_id == int(raw)


@pytest.mark.parametrize("raw", [None, [], 1, "object"])
def test_nonobject_has_no_identity(raw):
    assert rules.extract_request_identity(raw).request_id is None


def test_oversized_canonical_identity_is_range_failure():
    raw = "9" * 5000
    decision = rules.extract_request_identity({"request_id":raw})
    assert decision.request_id is None
    assert decision.error["details"] == {"field":"request_id","reason":"range","value":raw}
