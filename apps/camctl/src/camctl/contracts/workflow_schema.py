"""从公共错误登记派生报告与详情规则。"""

from copy import deepcopy


_SCHEMA_VERSION = "https://json-schema.org/draft/2020-12/schema"


def derive_workflow_report(report, registry):
    report = deepcopy(report)
    rules = report["$defs"]["error"].setdefault("allOf", [])
    for code, spec in registry["codes"].items():
        pointer = code.replace("~", "~0").replace("/", "~1")
        rules.append({
            "if": {"properties": {"code": {"const": code}}, "required": ["code"]},
            "then": {"properties": {
                "stage": {"const": spec["stage"]},
                "details": {"$ref": f"workflow-codes.json#/codes/{pointer}/details_schema"},
            }},
        })
    return report


def workflow_detail_rules(registry):
    return [{"$schema": _SCHEMA_VERSION, **spec["details_schema"]}
            for spec in registry["codes"].values()]
