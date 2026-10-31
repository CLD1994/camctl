"""CLI 唯一 stderr 拥有者保留完整异常图及格式化失败时的原始事实。"""

from io import StringIO
import json

from camctl import cli


def test_cli_error_is_single_json_document_and_same_error_is_not_resent():
    original = ExceptionGroup("original", [OSError(5, "read failed", "/source"), ValueError("bad")])
    err = StringIO()
    cli._diagnose_error(err, "测试阶段", original)
    cli._diagnose_error(err, "再次消费", original)
    records = [json.loads(line) for line in err.getvalue().splitlines()]
    assert len(records) == 1
    assert records[0]["level"] == "ERROR" and records[0]["stage"] == "cli"
    assert records[0]["exception"]["exceptions"][0]["errno"] == 5
    assert records[0]["exception"]["exceptions"][1]["message"] == "bad"


def test_cli_message_format_failure_preserves_original_graph_arguments():
    class BrokenMessage(Exception):
        def __str__(self):
            raise ValueError("format failed")

    original = ExceptionGroup("root", [BrokenMessage("original-argument"), OSError(5, "io")])
    err = StringIO()
    cli._diagnose_error(err, "测试阶段", original)
    record = json.loads(err.getvalue())
    assert record["exception"]["exceptions"][0]["arguments"] == ["original-argument"]
    assert record["exception"]["exceptions"][1]["errno"] == 5
    assert "format_error" in record
