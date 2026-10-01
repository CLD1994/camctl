"""能力导出编码与真实公共 Schema 的集成。"""
import json
import pytest
from camctl.cli import encode_describe_document


class TestDescribeEncoding:
    def test_empty_devices_document_validated_and_encoded(self) -> None:
        output = encode_describe_document({"devices": []})
        assert output.endswith(b"\n")
        assert output.count(b"\n") == 1
        assert json.loads(output) == {"devices": []}

    def test_invalid_document_rejected_before_encoding(self) -> None:
        with pytest.raises(Exception, match="capabilities"):
            encode_describe_document({"devices": [], "unexpected": 1})
        with pytest.raises(Exception):
            encode_describe_document({})

    def test_schema_failure_raises_validation_error_only(self) -> None:
        from camctl.contracts.schemas import SchemaValidationError

        with pytest.raises(SchemaValidationError):
            encode_describe_document({"devices": [{"device_id": ""}]})
