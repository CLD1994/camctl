#!/usr/bin/env python3
"""从当前工作树和指定配置创建独立的真机验证环境。"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tomllib

from deploy_support import (add_path_arguments, configure_paths, install_host,
    path_overrides, require_new_state_db, write_config)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_tree(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        ".git", ".venv*", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="仓库工作树")
    parser.add_argument("--config", type=Path, required=True, help="旧环境 TOML 配置")
    parser.add_argument("--target", type=Path, required=True, help="尚不存在的新环境路径")
    parser.add_argument("--environment", type=Path, help="可选的 environment.sh；启动 host 时加载")
    parser.add_argument("--hasselblad-id", help="仅向客户端附加的 Hasselblad 设备 ID")
    parser.add_argument("--python", default="python3.11", help="Python 3.11 解释器")
    add_path_arguments(parser)
    args = parser.parse_args()
    repo, config_file, target = args.repo.resolve(), args.config.resolve(), args.target.absolute()
    environment = args.environment.resolve() if args.environment else None
    if target.exists() or target.is_symlink():
        parser.error(f"目标已存在: {target}")
    if not target.parent.is_dir():
        parser.error(f"目标父目录不存在: {target.parent}")
    if target.resolve().is_relative_to(repo) and not target.resolve().is_relative_to(repo / "tmp"):
        parser.error("仓库内的目标必须位于 tmp 目录下")
    for path in (config_file, *( [environment] if environment else [] )):
        if not path.is_file():
            parser.error(f"输入文件不存在: {path}")
    if args.hasselblad_id is not None and not args.hasselblad_id.strip():
        parser.error("Hasselblad 设备 ID 不能为空")
    try:
        config = tomllib.loads(config_file.read_text(encoding="utf-8"), parse_float=Decimal)
        devices = config.get("devices", {})
        if not isinstance(devices, dict):
            raise ValueError("devices 必须为表")
        if args.hasselblad_id in devices and devices[args.hasselblad_id].get("driver") != "hasselblad_cfv100c":
            raise ValueError("Hasselblad 设备 ID 与已有其他设备冲突")
        configure_paths(config, target, overrides=path_overrides(args))
        require_new_state_db(config)
        for name in ("uv", "cmake", "npm", "node", "git", args.python):
            if not shutil.which(name):
                raise ValueError(f"找不到工具: {name}")
        version = subprocess.check_output([args.python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"], text=True).strip()
        if version != "3.11":
            raise ValueError(f"需要 Python 3.11，实际为 {version}")
        if subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=repo, text=True).strip() != str(repo):
            raise ValueError("--repo 必须指向仓库工作树根目录")
    except (OSError, ValueError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as error:
        parser.error(str(error))

    target.mkdir(mode=0o700)
    log_path = target / "deploy.log"
    receipt = {"status": "running", "repo": str(repo), "config": str(config_file),
               "config_sha256": sha(config_file), "target": str(target), "steps": [],
               "paths": config["paths"],
               "hasselblad_id": args.hasselblad_id, "initialized": False,
               "not_run": ["host-demo", "client", "真实设备操作", "目标硬件验收"]}
    stage = "准备"
    def save():
        (target / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def run(label, command, cwd=None, stdout=None):
        nonlocal stage
        stage = label
        print(f"[{label}]", flush=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n$ " + shlex.join(map(str, command)) + "\n")
            log.flush()
            result = subprocess.run(list(map(str, command)), cwd=cwd, stdout=stdout or log, stderr=log,
                                    env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")})
            log.write(f"退出码: {result.returncode}\n")
        if result.returncode:
            raise RuntimeError(f"{label} 失败，退出码 {result.returncode}；详见 {log_path}")
        receipt["steps"].append(label)
        save()
    try:
        save()
        receipt["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        receipt["git_status"] = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo, text=True).splitlines()
        source = target / "source"
        for rel in ("apps/camctl", "apps/client", "apps/host-demo", "protocol", "docs/camctl"):
            stage = "复制当前源码 " + rel
            copy_tree(repo / rel, source / rel)
        write_config(target / "config.toml", config)
        if environment:
            shutil.copyfile(environment, target / "environment.sh")
            receipt["environment_sha256"] = sha(environment)
        dist = target / "dist"
        dist.mkdir()
        project = source / "apps/camctl"
        run("导出锁定依赖", ["uv", "export", "--project", project, "--frozen", "--no-dev", "--no-emit-project", "-o", dist / "requirements.txt"])
        run("构建 wheel", ["uv", "build", "--project", project, "--python", args.python, "--wheel", "--out-dir", dist])
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"wheel 数量异常: {len(wheels)}")
        venv = target / "venv"
        run("创建 Python 环境", ["uv", "venv", "--python", args.python, venv])
        run("安装锁定依赖", ["uv", "pip", "install", "--python", venv / "bin/python", "--require-hashes", "-r", dist / "requirements.txt"])
        run("安装 wheel", ["uv", "pip", "install", "--python", venv / "bin/python", "--no-deps", wheels[0]])
        run("检查安装依赖", ["uv", "pip", "check", "--python", venv / "bin/python"])
        install_host(run, source / "apps/host-demo", target)
        client = source / "apps/client"
        run("安装客户端锁定依赖", ["npm", "ci", "--engine-strict", "--no-audit", "--no-fund"], client)
        run("构建客户端", ["npm", "run", "build"], client)
        run("初始化独立状态库", [venv / "bin/camctl", "init", "--config", target / "config.toml"], target)
        receipt["initialized"] = True
        save()
        cap = target / "device-capabilities.json"
        with cap.open("w", encoding="utf-8") as output:
            run("导出能力说明", [venv / "bin/camctl", "describe", "--config", target / "config.toml"], target, output)
        if args.hasselblad_id and args.hasselblad_id not in devices:
            code = '''from copy import deepcopy
import sys
from pathlib import Path
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
from camctl.persistence.transaction import encode_json_value
from camctl.devices.drivers.hasselblad.definitions import driver_definition
from camctl.devices.parameter_schemas import validate_parameter_schema, schema_with_defaults
from camctl.host_files.declarations import HOST_FILE_DECLARATION
p=Path(sys.argv[1]); d=parse_exact_json(p.read_text()); definition=driver_definition(); actions=[]
for action_type, parameters in sorted(definition.actions.items()):
    items=[]
    for parameter in parameters:
        schema=schema_with_defaults(deepcopy(dict(parameter.schema)),parameter.defaults)
        validate_parameter_schema(parameter.parameter_type,schema)
        item={'type':parameter.parameter_type,'name':parameter.name,'description':parameter.description,'preview_supported':parameter.preview_supported,'schema':schema}
        if parameter.video_size_estimate is not None: item['video_size_estimate']=deepcopy(parameter.video_size_estimate)
        items.append(item)
    actions.append({'type':action_type,'parameter_types':items})
d['devices'].append({'device_id':sys.argv[2],'driver_id':definition.driver_id,'delete_supported':HOST_FILE_DECLARATION.delete_supported,'actions':actions})
validate_document('protocol/capabilities.schema.json',d)
p.write_text(encode_json_value(d)+'\\n')
'''
            run("生成客户端 Hasselblad 声明", [venv / "bin/python", "-c", code, cap, args.hasselblad_id], target)
            receipt["hasselblad_client_only"] = True
        else:
            receipt["hasselblad_client_only"] = False
        data = target / "client-data"
        data.mkdir()
        shutil.copyfile(cap, data / cap.name)
        receipt["capabilities_sha256"] = sha(cap)
        receipt["wheel_sha256"] = sha(wheels[0])
        receipt["device_ids"] = [item["device_id"] for item in json.loads(cap.read_text())["devices"]]
        host_script = '''#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/environment.sh" ]]; then source "$root/environment.sh"; fi
exec "$root/venv/bin/python" - "$root" <<'PY'
import os, sys, tomllib
from pathlib import Path
r=Path(sys.argv[1]); paths=tomllib.loads((r/'config.toml').read_text())['paths']; program=str(r/'host/bin/host-demo')
os.execv(program,[program,'--camctl',str(r/'venv/bin/camctl'),'--config',str(r/'config.toml'),'--ready',paths['ready'],'--processing',paths['processing'],'--log',str(r/'host.log')])
PY
'''
        client_script = '''#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export CAMCTL_DATA_DIR="$root/client-data"
cd "$root/source/apps/client"
exec npm start
'''
        for name, content in (("start-host-demo.sh", host_script), ("start-client.sh", client_script)):
            path = target / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o700)
        (target / "README.md").write_text("# 真机验证环境\n\n启动主程序：`./start-host-demo.sh`\n\n启动客户端：`./start-client.sh`\n\n部署日志见 `deploy.log`，结果见 `receipt.json`。部署未启动主程序或客户端，也未连接或验证真实设备。客户端专用 Hasselblad 声明不代表主机已绑定设备。\n", encoding="utf-8")
        receipt["status"] = "complete"
        save()
        print(f"部署完成: {target}\n收据: {target / 'receipt.json'}\n日志: {log_path}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        receipt.update(status="failed", failed_stage=stage, error=str(error))
        save()
        print(f"部署在 {stage} 失败: {error}\n保留目录 {target}；重试请使用新路径。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
