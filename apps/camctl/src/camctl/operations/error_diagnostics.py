"""把原异常图编码为可保存的诊断，保留完整原因与原有详情。"""

from collections.abc import Mapping
import traceback


_FACTS_ATTRIBUTE = "_camctl_exception_diagnostic_facts"


def attach_diagnostic_facts(error: BaseException, facts: Mapping) -> None:
    """事实所属模块附加有限诊断；原异常由共同编码器按对象身份引用。"""
    setattr(error, _FACTS_ATTRIBUTE, dict(facts))


class DiagnosticError(Exception):
    """明确携带 JSON 可编码诊断的异常；调用方负责提供原始事实。"""

    def __init__(self, message: str, *, diagnostic_details: Mapping) -> None:
        super().__init__(message)
        self.diagnostic_details = dict(diagnostic_details)


def _own_traceback(error: BaseException) -> list[dict]:
    # 保存每个原栈帧；不按排版压缩，不经 linecache 读取源码。
    return [{"filename": frame.f_code.co_filename, "lineno": lineno,
             "function": frame.f_code.co_name}
            for frame, lineno in traceback.walk_tb(error.__traceback__)]


def exception_diagnostic(error: BaseException | None) -> dict | None:
    """保存全部标准异常关系；循环和共享节点引用首次出现的路径。"""
    if error is None:
        return None
    result = {}
    seen = {id(error): "#"}
    remaining = [(error, result, "#")]

    def reference(original, path):
        if original is None:
            return None
        previous = seen.get(id(original))
        if previous is not None:
            return {"$ref": previous}
        seen[id(original)] = path
        node = {}
        remaining.append((original, node, path))
        return node

    def facts_document(value, path):
        if isinstance(value, BaseException):
            return reference(value, path)
        if isinstance(value, Mapping):
            return {name: facts_document(item, path + "/" + name)
                    for name, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [facts_document(item, path + f"/{index}")
                    for index, item in enumerate(value)]
        return value

    while remaining:
        original, node, path = remaining.pop()
        kind = type(original)
        node.update(
            type=f"{kind.__module__}.{kind.__qualname__}",
            message=str(original), traceback=_own_traceback(original),
            cause=reference(original.__cause__, path + "/cause"),
            context=reference(original.__context__, path + "/context"),
            suppress_context=original.__suppress_context__,
        )
        if hasattr(original, "__notes__"):
            node["notes"] = list(original.__notes__)
        if isinstance(original, BaseExceptionGroup):
            node["exceptions"] = [reference(child, f"{path}/exceptions/{index}")
                                  for index, child in enumerate(original.exceptions)]
        if isinstance(original, OSError):
            node["errno"] = original.errno
            for name in ("filename", "filename2"):
                value = getattr(original, name)
                node[name] = {"bytes_hex": value.hex()} if isinstance(value, bytes) else value
        if isinstance(original, DiagnosticError):
            node["diagnostic_details"] = dict(original.diagnostic_details)
        facts = getattr(original, _FACTS_ATTRIBUTE, None)
        if isinstance(facts, Mapping):
            node["diagnostic_facts"] = facts_document(facts, path + "/diagnostic_facts")
    return result
