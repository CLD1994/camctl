# Hasselblad CFV 100C 安装演示

本文说明如何从已安装的 camctl 发行包导出独立演示资源，在单独数据目录中初始化 Hasselblad 配置，按实际能力生成单张拍摄计划，并分别取回原图和预览。该流程面向已安装 Python 3.11、camctl 及配套依赖的 ARM Linux 主机；安装步骤见[camctl 主机联调指南](../../../apps/host-demo/docs/camctl-integration.md)。本文给出执行步骤，不记录目标主机或真实相机已完成验证的结论。

拍摄能力、主机文件及已有的取回和保留规则见[Hasselblad 驱动设计](../../camctl/hasselblad-driver.md)和[正式产物与取回清理](../../architecture/outputs.md)。相机端是否另行保存文件仍须按[单张拍摄接口核验记录](../../superpowers/notes/2026-10-12-hasselblad-cfv100c-probe.md)确认。

## 导出已安装发行包中的样例

以下命令只从安装后的 `camctl.resources` 读取配置、拍摄模板、原片与预览取回模板及唯一计划生成器。文件使用独占创建，遇到同名文件时命令会报错，以免覆盖已经填写的配置或计划。

```bash
"$HOME/.camctl/venv/bin/python" - <<'PY'
from pathlib import Path

from camctl.resources import resource_bytes

root = Path.home() / ".camctl" / "hasselblad-demo"
root.mkdir(parents=True, exist_ok=True)
names = (
    "hasselblad-config.toml",
    "hasselblad-photo.json",
    "obtain.json",
    "obtain-preview.json",
    "prepare-plan.py",
)
prefix = "examples/camera-demo/"
for name in names:
    target = root / name
    with target.open("xb") as file:
        file.write(resource_bytes(prefix + name))
print(root)
PY
```

编辑 `$HOME/.camctl/hasselblad-demo/hasselblad-config.toml`，将 `REPLACE_HASSELBLAD_SERIAL` 替换为目标相机的实际序列号，并确认 `executable` 指向目标机上的绝对工具路径。样例路径为 `/home/cld/hb_cli/hb_cli`，产品标识为 `10`。配置不需要相机 IP 地址或密码。`[paths]` 将状态库、日志及交接目录放在 `$HOME/.camctl/hasselblad-demo/` 下，与其他演示配置使用的状态库和目录分开。

## 初始化、说明能力并执行拍摄

初始化并导出本次配置的能力说明：

```bash
"$HOME/.camctl/venv/bin/camctl" init \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
"$HOME/.camctl/venv/bin/camctl" describe \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml" \
  > "$HOME/.camctl/hasselblad-demo/capabilities.json"
```

从安装包的 `prepare-plan.py` 生成拍摄计划。生成器使用实际 `describe` 结果核对 `hb0` 的 `camera_take_photo` 能力及唯一的 `hasselblad_photo` 参数类型，同时更新请求 ID、创建时间和 UTC 执行时间。能力说明未提供唯一匹配时，生成器拒绝写入计划。

```bash
"$HOME/.camctl/venv/bin/python" \
  "$HOME/.camctl/hasselblad-demo/prepare-plan.py" hasselblad-photo \
  --capabilities "$HOME/.camctl/hasselblad-demo/capabilities.json" \
  --device-id hb0 \
  --output "$HOME/.camctl/hasselblad-demo/hasselblad-photo-plan.json"
"$HOME/.camctl/venv/bin/camctl" submit \
  "$HOME/.camctl/hasselblad-demo/hasselblad-photo-plan.json" \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
"$HOME/.camctl/venv/bin/camctl" run \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
```

运行结束后，在 `$HOME/.camctl/hasselblad-demo/ready/` 中查看 `status-report-*.json`，按拍摄计划生成器输出的 `request_id` 找到 `camera_take_photo` 动作，确认状态为 `succeeded`，并使用该报告中的 `action_instance_id`。可以运行下面的只读脚本，从对应计划的成功动作中取得身份；将 `REQUEST_ID` 替换为生成器刚才打印的实际值：

```bash
REQUEST_ID='<拍摄计划的 request_id>'
ACTION_ID=$("$HOME/.camctl/venv/bin/python" - \
  "$HOME/.camctl/hasselblad-demo/ready" "$REQUEST_ID" <<'PY'
import json
import sys
from pathlib import Path

ready = Path(sys.argv[1])
request_id = sys.argv[2]
matches = set()
for report_path in ready.glob("status-report-*.json"):
    report = json.loads(report_path.read_bytes())
    for plan in report.get("plans", []):
        if plan.get("request_id") != request_id:
            continue
        for action in plan.get("actions", []):
            if action.get("type") == "camera_take_photo" and action.get("status") == "succeeded":
                matches.add(action["action_instance_id"])
if len(matches) != 1:
    raise SystemExit(f"需要一条成功拍摄动作身份，实际取得 {len(matches)} 条")
print(next(iter(matches)))
PY
)
printf '拍摄 action_instance_id=%s\n' "$ACTION_ID"
```

## 分别取回原图与预览

用上一步从成功报告取得的动作 ID 分别生成两个独立计划。`obtain` 按默认选择取回原图，`obtain-preview` 通过 `filter: preview` 选择配对预览；不要把报告中的样例 ID `1` 当作真实动作 ID。

```bash
"$HOME/.camctl/venv/bin/python" \
  "$HOME/.camctl/hasselblad-demo/prepare-plan.py" obtain \
  --source-action-id "$ACTION_ID" \
  --output "$HOME/.camctl/hasselblad-demo/obtain-original-plan.json"
"$HOME/.camctl/venv/bin/python" \
  "$HOME/.camctl/hasselblad-demo/prepare-plan.py" obtain-preview \
  --source-action-id "$ACTION_ID" \
  --output "$HOME/.camctl/hasselblad-demo/obtain-preview-plan.json"
"$HOME/.camctl/venv/bin/camctl" submit \
  "$HOME/.camctl/hasselblad-demo/obtain-original-plan.json" \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
"$HOME/.camctl/venv/bin/camctl" submit \
  "$HOME/.camctl/hasselblad-demo/obtain-preview-plan.json" \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
"$HOME/.camctl/venv/bin/camctl" run \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
```

检查新报告中两个取回动作的 delivery 及文件名、长度和摘要。两次取回各自形成独立 delivery；领取或传输 delivery 不删除长期保留的主机原图和预览。来源文件的清理仍须通过正式产物清理流程明确发起。

## 相机端保存情况

现有接口核验观察到工具将 3FR 原图和 JPEG 缩略图写到主机；拍摄前后 `images` 返回的条目相同。因此，这些观察没有证明本次拍摄也在相机存储中留下新文件。若演示需要确认相机端保存位置或相机目录中的新文件，应按接口核验记录补充对应设备观察，再记录实际结论。
