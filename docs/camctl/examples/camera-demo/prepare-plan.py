"""从安装包样例和实际能力说明生成一份可递交的独立计划。"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import secrets

from camctl.acceptance.schema import validate_precise
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import MAX_OBJECT_ID
from camctl.persistence.transaction import encode_json_value
from camctl.resources import available_resources, resource_bytes


PREFIX = "examples/camera-demo/"


def entity_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None or int(value) > MAX_OBJECT_ID:
        raise ValueError("身份必须是范围内正整数的标准十进制字符串")
    return value


def main(argv=None):
    samples = sorted(name.removeprefix(PREFIX).removesuffix(".json")
                     for name in available_resources() if name.startswith(PREFIX) and name.endswith(".json"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", choices=samples)
    parser.add_argument("--capabilities", type=Path, help="本次实际 camctl describe 导出的能力说明")
    parser.add_argument("--device-id", help="拍摄时改用能力说明中的实际设备身份")
    parser.add_argument("--source-action-id", help="取回所选原拍摄动作的实例身份")
    parser.add_argument("--last-report-id", help="客户端已可靠导入的累计报告身份")
    parser.add_argument("--delay-s", type=int, default=10, help="从生成到安排执行的秒数，默认 10")
    parser.add_argument("--output", type=Path, required=True, help="新的计划文件；已有文件不会覆盖")
    args = parser.parse_args(argv)
    try:
        if args.delay_s < 1:
            raise ValueError("安排执行的延迟必须是正整数秒")
        plan = parse_exact_json(resource_bytes(PREFIX + args.sample + ".json").decode("utf-8"))
        action = plan["actions"][0]
        if action["type"] in ("camera_record", "camera_timelapse"):
            if args.capabilities is None:
                raise ValueError("拍摄必须提供本次实际能力说明")
            if args.source_action_id is not None:
                raise ValueError("拍摄计划不接收取回源身份")
            if args.device_id is not None:
                action["device_id"] = args.device_id
            capabilities = parse_exact_json(args.capabilities.read_text(encoding="utf-8"))
            validate_document("protocol/capabilities.schema.json", capabilities)
            parameters = [parameter for device in capabilities["devices"]
                          if device["device_id"] == action["device_id"]
                          for declared in device["actions"] if declared["type"] == action["type"]
                          for parameter in declared["parameter_types"] if parameter["type"] == action["params"]["type"]]
            if len(parameters) != 1:
                raise ValueError("实际能力说明没有唯一的完整拍摄参数类型")
            validate_precise(parameters[0]["schema"], action["params"])
        else:
            if args.device_id is not None or args.capabilities is not None:
                raise ValueError("取回和报告计划不接收拍摄设备参数")
            if action["type"] == "obtain_action_outputs":
                action["params"]["source"]["action_instance_id"] = entity_id(args.source_action_id)
            elif args.last_report_id is None:
                raise ValueError("确认计划必须提供客户端已可靠导入的累计报告身份")
            elif args.source_action_id is not None:
                raise ValueError("报告计划不接收取回源身份")
        if args.last_report_id is not None:
            plan["last_report_id"] = entity_id(args.last_report_id)
        now = datetime.now(timezone.utc)
        plan["request_id"] = str(secrets.randbelow(MAX_OBJECT_ID) + 1)
        plan["created_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        if "scheduled_at" in action:
            action["scheduled_at"] = (now + timedelta(seconds=args.delay_s)).strftime("%Y-%m-%d %H:%M:%S")
        validate_document("protocol/plan.schema.json", plan)
        with args.output.open("x", encoding="utf-8") as file:
            file.write(encode_json_value(plan) + "\n")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("request_id=" + plan["request_id"])
    print("submit " + str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
