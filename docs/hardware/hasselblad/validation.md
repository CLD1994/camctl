# Hasselblad CFV 100C 安装演示

本文说明如何从已安装的 camctl 发行包导出独立演示资源，在单独数据目录中初始化 Hasselblad 配置，按实际能力生成单张拍摄计划，并分别取回原图和预览。该流程面向已安装 Python 3.11、camctl 及配套依赖的 ARM Linux 主机；安装步骤见[camctl 主机联调指南](../../../apps/host-demo/docs/camctl-integration.md)。本文给出执行步骤，不记录目标主机或真实相机已完成验证的结论；已完成的 ARM 单张拍摄与取回验收结论见[组件验证记录](../../camctl/verification.md#hasselblad-arm-真实拍摄与取回验收2026-10-12-utc)，其中也列出本地原始证据归档位置。

拍摄能力、正式源位置及取回和保留规则见[Hasselblad 驱动设计](../../camctl/hasselblad-driver.md)和[正式产物与取回清理](../../architecture/outputs.md)。当前参数支持 RAW／JPEG，用户须明确选择曝光、白平衡和对焦。软件参数域及现场验收分别以[单张参数规格](../../superpowers/specs/2026-10-20-hasselblad-photo-parameters-design.md)和[实施计划 H9](../../superpowers/plans/2026-10-21-hasselblad-photo-parameters.md#h9完成-arm-部署及真实相机验收)为依据；历史 RAW 现场结果不覆盖新增参数或 JPEG 相机源。

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

在受理拍摄前选择 `output_storage = "host"` 或 `"device"`，随后导出该配置的能力说明。源位置在受理时冻结；修改配置不改变旧动作的源位置。四组现场组合、实际参数与证据要求由 H9 的组合表维护，每组使用独立请求并保存原始计划。

## 初始化、说明能力并执行拍摄

初始化并导出本次配置的能力说明：

```bash
"$HOME/.camctl/venv/bin/camctl" init \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml"
"$HOME/.camctl/venv/bin/camctl" describe \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml" \
  > "$HOME/.camctl/hasselblad-demo/capabilities.json"
```

从安装包的 `prepare-plan.py` 生成拍摄计划。生成器使用实际 `describe` 结果核对 `hb0` 的 `camera_take_photo` 能力及唯一的 `hasselblad_photo` 参数类型，同时更新请求 ID、创建时间和 UTC 执行时间。能力说明未提供唯一匹配或模板参数不完整时，生成器拒绝写入计划。生成器消费包内模板；导出的 `hasselblad-photo.json` 用于查阅，修改该副本不会改变生成器读取的内容。

以下命令验证安装模板。完整参数验收时，将本次 `capabilities.json` 交给真实客户端，按 H9 组合选择格式、曝光、白平衡和对焦，保存并重开预设，再导出独立计划。分别保留三个短快门的精确数值；不能用修改模板的源码运行代替客户端导出。

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

检查新报告中两个取回动作的 delivery 及文件名、长度和摘要。两次取回各自形成独立 delivery；领取或传输 delivery 不删除原正式源。主机源可在相机离线后继续取回；相机源须连接原机身，按原机内文件名分别下载 original／thumbnail。JPEG 原图和 JPEG 预览仍是两个独立角色。来源文件的清理仍须通过正式产物清理流程明确发起。

## 通过 C host 完成现场业务链

完成 H8 软件门禁和目标安装核对后，用下列入口管理独立演示。配置中的 staging、ready、processing 必须与 host 使用相同路径；已有演示目录只在确认属于本次计划后复用。

```bash
"$HOME/.camctl/host/bin/host-demo" \
  --camctl "$HOME/.camctl/venv/bin/camctl" \
  --config "$HOME/.camctl/hasselblad-demo/hasselblad-config.toml" \
  --ready "$HOME/.camctl/hasselblad-demo/ready" \
  --processing "$HOME/.camctl/hasselblad-demo/processing" \
  --log "$HOME/.camctl/hasselblad-demo/host.log"
```

在该终端输入 `submit <客户端导出计划的绝对路径>`，再用 `claim` 领取文件。交互路径不展开 `$HOME` 或 `~`，也不加引号；此时 run 由 host 管理。分别递交原图和预览取回，再用新请求各重复一次，核对四份独立交付。把报告和媒体按 H9 要求的不同先后顺序导入真实客户端，将客户端导出的累计 ACK 经同一 host 递交并核对 camctl 水位。启动、领取及异常排查细节见[主程序联调指南](../../../apps/host-demo/docs/camctl-integration.md#四启动-host-demo)。

## 相机源的内容证据

当前工具的 `capture` 契约包含主机双文件和 `camera file:` 机内定位，或明确的未报告名称提示。主机名称与机内名称分别保存；明确缺名仅允许满足其他条件的主机源完成。相机源没有可靠机内定位时不得登记成功，也不得用主机名称猜测下载目标。

相机源原图和预览的大小及 SHA-256 固定于拍摄时核验的主机副本。现场按[字节一致性验收](../../superpowers/specs/2026-10-20-hasselblad-photo-parameters-design.md#jpeg-相机源的字节一致性验收)保留该副本或可靠摘要，与后续两种下载及再次下载逐一比较。双文件看起来相同或只有扩展名正确，不能证明内容一致；JPEG 缩略图的实际提取／生成路径没有依据时记录未知。

设置、调焦、capture、结果保存或取回出现失败、超时及未知结果时，保留原请求、原响应、工作文件和活动事实，沿原责任有限恢复。相对调焦和 capture 不因失败重发；`hb_cli stop` 是关机命令。现场记录分别列出安装、实际采集、文件字节、客户端媒体导入、ACK 和 180 秒调用预算的实际证据，未验证项保持未完成。
