"""从原动作读取不可替换的照片格式，不依赖当前设备配置。"""

from collections.abc import Mapping

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.host_files.photo_formats import CaptureFormat


def saved_photo_format(action: Mapping) -> CaptureFormat:
    params = action.get("effective_params_json")
    try:
        params = parse_exact_json(params) if isinstance(params, str) else params
        if not isinstance(params, Mapping):
            raise ValueError("生效参数不是对象")
        return CaptureFormat(params["capture_format"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("单张拍摄缺少有效的原保存格式") from error
