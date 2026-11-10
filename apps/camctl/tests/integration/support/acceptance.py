"""经正式受理服务、真实数据库线程提交输入，并完整关闭执行器。"""

from pathlib import Path

from camctl.acceptance.input import FileInputReader, parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, accept_input
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.transactions import encode_json_value

from .database import business_database


async def accept_source(source, context: AcceptanceContext, path: Path, *, key=None):
    """向受理服务提交已解析输入或输入诊断；实际事务由生产仓储处理。"""
    async with business_database(path) as database:
        operation_key = new_operation_key() if key is None else key
        return await accept_input(source, context, operation_key, database)


async def accept_document(document, context: AcceptanceContext, path: Path, *,
                          input_path: Path | None = None, key=None):
    """保存真实输入文件，经读取、解析和正式受理入口提交一次请求。"""
    target = Path(path).parent / "input.json" if input_path is None else input_path
    target.write_text(encode_json_value(document), encoding="utf-8")
    source = parse_input(await read_input(str(target), FileInputReader()))
    return await accept_source(source, context, path, key=key)
