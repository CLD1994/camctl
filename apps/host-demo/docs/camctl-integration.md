# 与真实 camctl 联调

[返回接入说明](../README.md)

本指南使用已安装的 host-demo 和独立交付的 camctl，验证“递交计划、执行电机动作、发送 NDJSON 通知、调用 C 回调、领取状态报告”的完整软件链路。NDJSON 是每行一条 JSON 消息的文本格式，通知通道由 host 模块自动建立。

host-demo 的电机回调只打印收到的 `position`，不操作实际电机。实际电机控制由主程序注册的回调执行，接入方式见[电机通知回调](../README.md#电机通知回调)。

## 一、准备运行账户和交付文件

在运行 host-demo 的 Linux 主机上准备以下条件：

- 已按[构建与安装](../README.md#构建与安装)安装 host-demo。以下命令使用默认位置 `$HOME/.camctl/host/bin/host-demo`；安装前缀不同时，替换相应路径。
- 已安装可用的 uv 和 Python 3.11，Python 实际链接的 SQLite 满足所交付 camctl 的要求。初始化命令会检查运行库条件。
- camctl 与 host-demo 使用同一个普通账户。以下命令不需要 `sudo`，所有默认业务路径位于该账户的 `$HOME/.camctl`。
- 主机日期与时间正确。生成的计划使用 UTC（协调世界时），不要求主机显示时区为 UTC。
- 安装依赖时可以访问配置的 Python 包源。

先创建接收交付文件的目录：

```bash
mkdir -p "$HOME/.camctl/packages"
```

向交付人员取得下面两个配套文件，**人工复制到运行主机的 `$HOME/.camctl/packages` 目录**。跨机器时可使用文件传输工具或移动存储介质，复制完成后再执行安装步骤。

| 文件 | 用途 |
| --- | --- |
| `requirements.txt` | 与交付版本配套的运行依赖清单，包含锁定版本和校验哈希。 |
| `camctl-x.y.z-py3-none-any.whl` | camctl 的 Python 安装包；`x.y.z` 代表实际交付版本，保留收到的真实文件名。 |

检查文件已经到位：

```bash
ls -l "$HOME/.camctl/packages"
uv --version
python3.11 --version
date -u
```

## 二、使用 uv 安装 camctl

首次部署时，在最终使用位置创建虚拟环境：

```bash
uv venv --python 3.11 "$HOME/.camctl/venv"
```

如果该环境已经存在，跳过创建命令，先核对其解释器版本。即使环境由 `python -m venv` 创建，也可以直接使用 uv 安装依赖。

```bash
"$HOME/.camctl/venv/bin/python" -c \
  'import sys, sqlite3; print(sys.version); print("SQLite:", sqlite3.sqlite_version)'
```

确认解释器为 Python 3.11 后，安装配套依赖，再安装 camctl 本体。**把第二条命令中的 `x.y.z` 替换成实际文件名中的版本号**：

```bash
uv pip install \
  --python "$HOME/.camctl/venv/bin/python" \
  --require-hashes \
  -r "$HOME/.camctl/packages/requirements.txt"

uv pip install \
  --python "$HOME/.camctl/venv/bin/python" \
  --no-deps \
  --reinstall-package camctl \
  "$HOME/.camctl/packages/camctl-x.y.z-py3-none-any.whl"
```

逐条确认命令成功后继续。`--require-hashes` 核对依赖文件哈希，`--no-deps` 使安装本体时沿用前一步的配套依赖，`--reinstall-package camctl` 确保同版本号的交付包也更新实际安装内容。安装后入口为 `$HOME/.camctl/venv/bin/camctl`；运行时无需激活环境，也不依赖 uv。

已有 camctl 部署时，在停止相关 host 与 CLI 进程后再更新安装，保留原配置、状态库和历史。

## 三、准备配置并初始化

首次部署时，从 host-demo 安装目录复制配置示例。`cp -n` 保留已经存在的配置文件：

```bash
cp -n "$HOME/.camctl/host/share/doc/camctl_host/examples/config.toml" \
  "$HOME/.camctl/config.toml"

"$HOME/.camctl/venv/bin/camctl" init
echo "初始化退出码：$?"
```

成功时退出码为 `0`，标准输出为空。`init` 创建状态库和交接目录；对于已有合法数据库，它验证并保留原有数据。若运行库或数据库检查失败，先处理标准错误中的具体原因，不继续启动 host-demo，也不删除已有状态库来绕过错误。

本指南使用[配置示例](../examples/config.toml)的默认路径：

```text
$HOME/.camctl/
    config.toml
    state.db
    venv/bin/camctl
    host/bin/host-demo
    staging/
    ready/
    processing/
```

已有配置若覆盖 `[paths]`，后面的目录检查应使用实际路径，host-demo 也必须通过 `--ready` 和 `--processing` 使用同一组目录。指定其他配置文件时，初始化命令与 host-demo 均传入同一个 `--config` 绝对路径。具体对应关系见[准备同一份部署配置](../README.md#准备同一份部署配置)。

## 四、启动 host-demo

打开终端 A，运行：

```bash
"$HOME/.camctl/host/bin/host-demo"
```

预期看到模块初始化完成，并提示可以输入 `submit`、`claim`、`logs` 或 `help`。这只表明 host 的本地初始化完成，CLI 的后续启动结果记录在日志中。

保持终端 A 运行，另开终端 B 执行下一步。camctl 的启动由 host-demo 管理，无需另行执行 `camctl run`。

## 五、生成并递交电机计划

在终端 B 执行以下脚本。脚本生成新的请求 ID，安排 10 秒后向位置 `100` 发送控制通知，允许延迟 5 分钟。文件完整写入并关闭后，脚本打印可直接递交的命令。

```bash
"$HOME/.camctl/venv/bin/python" - <<'PY'
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

now = datetime.now(timezone.utc)
request_id = str(time.time_ns())
plan = {
    "request_id": request_id,
    "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    "name": "电机通知联调",
    "actions": [{
        "name": "移动到位置100",
        "type": "motor_control",
        "scheduled_at": (now + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),
        "policy": {"max_delay_ms": 300000},
        "params": {"position": 100}
    }]
}

directory = Path.home() / ".camctl" / "manual-tests"
directory.mkdir(parents=True, exist_ok=True)
path = directory / f"motor-{request_id}.json"
path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"本次 request_id：{request_id}")
print("复制下面这一行到 host-demo 终端：")
print(f"submit {path}")
PY
```

记录打印的 `request_id`，并将最后一行完整复制到终端 A。host-demo 的交互命令接收实际绝对路径，不展开 `$HOME` 或 `~`，路径外也不加引号。

先看到路径已接收提示，随后在动作进入执行窗口后，应看到：

```text
收到电机位置通知：position=100。
```

这条输出表明 camctl 已发送通知，host 已解析 `params.position` 并调用 C 回调。若生成计划后超过有效窗口才递交，动作会过期；重新执行脚本可生成新的请求和时间窗口。

## 六、检查并领取状态报告

在终端 B 查看 camctl 发布的文件：

```bash
ls -lt "$HOME/.camctl/ready"
```

报告异步生成，刚收到回调时可能尚未出现，稍等几秒后再次检查。在终端 A 输入：

```text
claim
```

在终端 B 检查领取目录：

```bash
ls -lt "$HOME/.camctl/processing"
```

选择其中实际存在的 `status-report-*.json` 文件，将下面的示意文件名替换为该文件名后查看：

```bash
"$HOME/.camctl/venv/bin/python" -m json.tool \
  "$HOME/.camctl/processing/status-report-实际文件名.json"
```

在报告的 `plans` 中根据本次 `request_id` 找到计划，再查看其 `actions`。最终报告中，`移动到位置100` 的 `status` 应为 `succeeded`。若领取到的是执行中的报告，等待后再次 `claim`，检查包含该动作更新的报告。

| 观察结果 | 能够证明的行为 |
| --- | --- |
| host-demo 提示路径已接收。 | host 已接收计划路径；计划受理和执行结果仍需后续核实。 |
| host-demo 打印 `position=100`。 | host 已收到通知、解析参数并调用回调。 |
| 报告中的电机动作状态为 `succeeded`。 | camctl 已完整写入通知，不代表实际电机已经到位。 |
| 报告从 `ready` 移到 `processing`。 | host 已领取该文件，后续传输由主程序负责。 |

## 七、验证重复提交

在确认本次动作已经成功后，在终端 A 再次输入同一条 `submit` 命令，递交同一个文件。相同请求已执行的动作应保留结果，不再次触发位置回调。

若要安排一次新的电机动作，重新执行第五步脚本，生成新的请求 ID 和时间窗口。修改已有文件的 `position` 但沿用已受理的请求 ID，不会产生新的执行请求。对于发送后结果保存不完整的情况，恢复也不重发；本步骤只验证成功后的重复提交，不验证中断恢复。

## 八、结束与排错

等到动作结束并完成报告检查后，可在终端 A 按 `Ctrl+C` 结束本次演示。`Ctrl+D` 只结束终端输入，host-demo 仍继续运行。

遇到异常时，在仍运行的 host-demo 中输入 `logs` 查看模块日志；也可以在终端 B 执行：

```bash
tail -n 100 "$HOME/.camctl/host.log"
tail -n 100 "$HOME/.camctl/camctl.log"
```

| 现象 | 检查步骤 |
| --- | --- |
| `camctl init` 返回非零退出码。 | 检查标准错误和实际 Python、SQLite 版本；数据库已有数据时保留现场，按具体错误处理。 |
| host 初始化后没有回调输出。 | 先检查 `host.log` 中 CLI 是否成功启动，再检查本次请求的报告，区分尚未到时、过期、受理失败与通知发送失败。 |
| `ready` 一直没有报告。 | 检查 `camctl.log` 中数据库及报告发布错误，并核对 CLI 与 host 使用的配置和目录。 |
| 执行 `claim` 后未找到预期文件。 | 检查文件是否已发布到实际 `ready` 目录，核对两个目录是否位于同一文件系统，并查看 `host.log` 中领取步骤的错误。领取调用返回本身不证明所有文件移动成功。 |
| 重复提交没有再次打印位置。 | 对照请求 ID；已成功执行的同一请求不会再次发送，安排新动作应生成新请求。 |

保留本次计划文件、请求 ID、相关报告，以及 CLI 启动、动作执行和文件领取时的日志，便于按发生错误的步骤定位问题。
