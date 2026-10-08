"""B7 正式发行物在源码仓库之外的安装与运行验证（组件集成测试）。

从仓库权威输入构建正式 wheel，在仓库之外的独立虚拟环境中按锁文件
安装运行时依赖并安装发行物；随后完全经安装后的 camctl CLI 执行
init、describe、submit 与设备替身 run，工作目录与进程环境都不依赖
源码仓库。另验证两条故障分支（包资源缺失、SQLite 条件不满足）明确
失败，以及安装环境不携带测试依赖。

这是集成测试：执行包构建、依赖导出与安装，不连接真实设备；设备
行为由与跨组件测试同源的受契约约束替身提供，替身脚本随测试复制
到部署目录，不把仓库路径带入被测进程。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = PROJECT_DIR.parents[1]
_CROSS_DIR = REPO_ROOT / "tests" / "integration"
_BRIDGE_SOURCE = _CROSS_DIR / "_camctl_stub_entry.py"
_FIXTURES_SOURCE = _CROSS_DIR / "camctl_fixtures.py"

#: 直接运行时依赖（wheel 声明与锁文件导出都应只包含这些包）。
_DIRECT_DEPENDENCIES = ("concurrent-log-handler", "janus", "jsonschema")

#: 模拟不满足统一运行条件的 SQLite 版本（低于主线最低版本）。
_NONCOMPLIANT_SQLITE = (3, 40, 0)


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, (
        f"命令失败: {command}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _venv_python(env_dir: Path) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _clean_environment() -> dict[str, str]:
    """运行环境：不继承任何指向源码仓库的 Python 查找路径。"""
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    return environment


@dataclass(frozen=True)
class _Installed:
    """仓库之外的安装环境：解释器、部署目录与状态库路径。"""

    python: Path
    deploy: Path
    state_db: Path
    config: Path
    ready: Path
    wheel: Path
    requirements: Path

    def cli(self, *args: str, driver: dict | None = None,
            timeout_s: float = 180.0) -> subprocess.CompletedProcess:
        """经部署装配桥运行安装后的 camctl；driver 提供时接入替身。

        工作目录保持在仓库之外；桥与替身脚本都来自部署目录中的
        副本，被测进程的 import 查找不经过源码仓库。
        """
        environment = _clean_environment()
        environment["CAMCTL_TEST_STATE_DB"] = str(self.state_db)
        command = [str(self.python), str(self.deploy / "_camctl_stub_entry.py"), *args]
        if driver is not None:
            spec = dict(driver)
            spec["gate_dir"] = str(self.deploy / "gates")
            spec_path = self.deploy / "driver-spec.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment["CAMCTL_TEST_DRIVER_SPEC"] = str(spec_path)
        return subprocess.run(
            command, capture_output=True, text=True, env=environment,
            cwd=str(self.deploy), timeout=timeout_s)


def _toml_path(path: Path) -> str:
    """TOML 基本字符串路径：反斜杠转义后可移植。"""
    return str(path).replace("\\", "\\\\")


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> _Installed:
    """构建正式 wheel 并安装到仓库之外的独立环境。"""
    root = tmp_path_factory.mktemp("b7-distribution")

    dist_dir = root / "dist"
    dist_dir.mkdir()
    _run(["uv", "build", "--project", str(PROJECT_DIR), "--wheel",
          "--out-dir", str(dist_dir)])
    wheels = sorted(dist_dir.glob("camctl-*.whl"))
    assert len(wheels) == 1, f"期望恰好一个 wheel，实际: {wheels}"
    wheel = wheels[0]

    # 运行时依赖只从锁文件导出版本取得；--no-dev 排除测试组。
    requirements = root / "requirements.txt"
    _run(["uv", "export", "--project", str(PROJECT_DIR), "--frozen",
          "--no-dev", "--no-emit-project", "-o", str(requirements)])

    # 独立解释器环境：复用当前测试解释器（目标 Python 3.11，
    # 实际链接的 SQLite 已满足统一运行条件）。
    env_dir = root / "env"
    _run(["uv", "venv", "--python", sys.executable, str(env_dir)])
    python = _venv_python(env_dir)
    _run(["uv", "pip", "install", "--python", str(python),
          "-r", str(requirements)])
    _run(["uv", "pip", "install", "--no-deps", "--python", str(python),
          str(wheel)])

    deploy = root / "deploy"
    home = deploy / "deployment"
    for directory in ((home / "staging"), (home / "ready"),
                      (home / "processing"), (deploy / "gates")):
        directory.mkdir(parents=True)
    state_db = home / "state.db"
    config = home / "config.toml"
    config.write_text(
        "\n".join([
            "[paths]",
            f'state_db = "{_toml_path(state_db)}"',
            f'log_file = "{_toml_path(home / "camctl.log")}"',
            f'staging = "{_toml_path(home / "staging")}"',
            f'ready = "{_toml_path(home / "ready")}"',
            f'processing = "{_toml_path(home / "processing")}"',
            "",
            "[clock]",
            'min_plausible_date = "2025-01-01"',
            "",
            "[devices.cam-1]",
            'kind = "camera"',
            'driver = "test-stub"',
            "",
            "[devices.cam-1.result_check]",
            'retry_interval_s = "0"',
            "",
            "[devices.cam-1.cleanup]",
            'retry_interval_s = "0"',
            "",
        ]),
        encoding="utf-8",
    )

    # 替身基础设施以副本进入部署目录：被测进程不经仓库路径取用。
    shutil.copy2(_BRIDGE_SOURCE, deploy / _BRIDGE_SOURCE.name)
    shutil.copy2(_FIXTURES_SOURCE, deploy / _FIXTURES_SOURCE.name)

    return _Installed(
        python=python, deploy=deploy, state_db=state_db,
        config=config, ready=home / "ready", wheel=wheel,
        requirements=requirements)


def _probe(installed: _Installed, code: str) -> str:
    result = _run([str(installed.python), "-c", code],
                  cwd=str(installed.deploy), env=_clean_environment())
    return result.stdout.strip()


def _locked_versions(requirements_text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _DIRECT_DEPENDENCIES:
        match = re.search(rf"^{re.escape(name)}==([^\s\\]+)",
                          requirements_text, re.MULTILINE)
        assert match is not None, f"锁文件导出缺少 {name} 的钉定版本"
        versions[name] = match.group(1)
    return versions


def _photo_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def _photo_driver() -> dict:
    """单张照片替身剧本：首个动作的列举结果是一张完整照片。"""
    return {
        "driver_id": "test-stub",
        "timelapse_duration_s": 3.0,
        "record_duration_s": 1.0,
        "files": {"1": [{
            "identity": "shot-1",
            "locator": {"path": "/DCIM/shot-1"},
            "size_bytes": 4096,
            "complete": True,
            "kind": "photo",
            "original_name": "shot-1.jpg",
            "media_type": "image/jpeg",
        }]},
        "gates": {},
        "device_files": {},
        "delete_error": None,
        "photo_preview_supported": False,
    }


def test_distribution_works_outside_repository(installed: _Installed) -> None:
    """安装后的发行物独立完成 init/describe/submit 与设备替身 run。"""
    # 安装来源：包来自安装环境，不经源码仓库；包资源自包含可读。
    origin = _probe(
        installed,
        "import camctl; import sys; sys.stdout.write(camctl.__file__)")
    assert str(installed.python.parents[1]) in origin, (
        f"camctl 未从安装环境装载: {origin}")
    assert "workspace" not in origin.replace(str(installed.python.parents[1]), ""), (
        f"camctl 解析到了仓库路径: {origin}")
    resources = _probe(
        installed,
        "from camctl.resources import available_resources, resource_bytes;"
        "import json, sys;"
        "names = sorted(available_resources());"
        "data = resource_bytes('sql/core.sql');"
        "sys.stdout.write(json.dumps({'names': names, 'sql_bytes': len(data)}))")
    payload = json.loads(resources)
    assert "sql/core.sql" in payload["names"]
    assert payload["sql_bytes"] > 0

    # 依赖边界：wheel 只声明运行时依赖；直接依赖按锁文件版本安装，
    # 测试组不进入安装环境。
    with zipfile.ZipFile(installed.wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist()
            if name.endswith(".dist-info/METADATA"))
        metadata_text = archive.read(metadata_name).decode("utf-8")
    declared = re.findall(r"^Requires-Dist: ([A-Za-z0-9._-]+)",
                          metadata_text, re.MULTILINE)
    assert sorted(declared) == sorted(_DIRECT_DEPENDENCIES), (
        f"wheel 声明的依赖越界: {declared}")
    locked = _locked_versions(installed.requirements.read_text(encoding="utf-8"))
    installed_versions = json.loads(_probe(
        installed,
        "import importlib.metadata, json, sys;"
        "names = " + repr(list(_DIRECT_DEPENDENCIES)) + ";"
        "sys.stdout.write(json.dumps({n: importlib.metadata.version(n)"
        " for n in names}))"))
    assert installed_versions == locked, (
        f"安装版本与锁文件不一致: {installed_versions} != {locked}")
    for test_package in ("pytest", "pytest_asyncio", "pytest_mock"):
        found = _probe(
            installed,
            f"import importlib.util, sys;"
            f"sys.stdout.write(str(importlib.util.find_spec({test_package!r}) is not None))")
        assert found == "False", f"安装环境携带了测试依赖: {test_package}"

    # init：显式初始化状态库成功。
    initialized = installed.cli("init", "--config", str(installed.config))
    assert initialized.returncode == 0, initialized.stderr
    assert initialized.stdout == ""
    assert installed.state_db.is_file()

    # describe：替身登记的设备动作出现在能力说明中。
    described = installed.cli(
        "describe", "--config", str(installed.config), driver=_photo_driver())
    assert described.returncode == 0, described.stderr
    document = json.loads(described.stdout)
    assert len(document["devices"]) == 1, document
    assert "camera_take_photo" in described.stdout

    # submit：受理计划保存成功。
    scheduled = datetime.now(timezone.utc) + timedelta(seconds=2)
    plan_path = installed.deploy / "plan-8001.json"
    plan_path.write_text(json.dumps(
        _photo_plan("8001", scheduled.strftime("%Y-%m-%d %H:%M:%S")),
        ensure_ascii=False), encoding="utf-8")
    submitted = installed.cli(
        "submit", str(plan_path), "--config", str(installed.config),
        driver=_photo_driver())
    assert submitted.returncode == 0, submitted.stderr
    assert json.loads(submitted.stdout)["kind"] == "succeeded"

    # run：设备替身完成拍摄，会话收场并发布报告。
    finished = installed.cli(
        "run", "--config", str(installed.config), driver=_photo_driver())
    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout) == {"kind": "succeeded"}

    reports = list(installed.ready.glob("status-report-*.json"))
    assert reports, "run 会话未发布任何状态报告"
    uri = f"file:{installed.state_db.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        statuses = [row[0] for row in connection.execute(
            "SELECT status FROM actions ORDER BY id")]
    assert statuses == [3], f"照片动作未成功终态: {statuses}"

    # 版本入口：安装物自带版本说明。
    versioned = installed.cli("--version")
    assert versioned.returncode == 0, versioned.stderr
    assert versioned.stdout.strip() == "0.1.0", versioned.stdout

    # 运行日志：会话失败记录经日志链写入配置的日志文件；干净会话
    # 无记录不创建文件是既定行为，日志验证走失败路径。
    failed_home = installed.deploy / "deployment-failure"
    for name in ("staging", "ready", "processing"):
        (failed_home / name).mkdir(parents=True)
    failed_state = failed_home / "state.db"
    failed_config = failed_home / "config.toml"
    failed_config.write_text(
        "\n".join([
            "[paths]",
            f'state_db = "{_toml_path(failed_state)}"',
            f'log_file = "{_toml_path(failed_home / "camctl.log")}"',
            f'staging = "{_toml_path(failed_home / "staging")}"',
            f'ready = "{_toml_path(failed_home / "ready")}"',
            f'processing = "{_toml_path(failed_home / "processing")}"',
            "",
            "[clock]",
            'min_plausible_date = "2025-01-01"',
            "",
            "[devices.cam-1]",
            'kind = "camera"',
            'driver = "test-stub"',
            "",
        ]),
        encoding="utf-8",
    )
    # 日常入口对缺失状态库的拒绝与安装物边界：不创建库、明确失败。
    refused = installed.cli(
        "run", "--config", str(failed_config), driver=_photo_driver())
    assert refused.returncode == 1, (
        f"缺失状态库仍成功: stdout={refused.stdout!r}")
    assert refused.stdout.strip() == "", refused.stdout
    assert "状态库不存在" in refused.stderr, refused.stderr
    assert not failed_state.exists(), "日常入口创建了状态库"

    # 运行日志：显式初始化后损坏状态库，会话层失败经日志链写入
    # 配置的日志文件；干净会话无记录不创建文件是既定行为。
    initialized_failure = installed.cli(
        "init", "--config", str(failed_config))
    assert initialized_failure.returncode == 0, initialized_failure.stderr
    failed_state.write_bytes(b"not a sqlite database at all")
    failed = installed.cli(
        "run", "--config", str(failed_config), driver=_photo_driver())
    log_file = failed_home / "camctl.log"
    assert failed.returncode == 1, (
        f"损坏状态库仍成功: stdout={failed.stdout!r}")
    failure_message = json.loads(failed.stdout)
    assert failure_message["kind"] == "error", failed.stdout
    assert failure_message["body"]["reason"] == "state_db_error", failed.stdout
    assert log_file.is_file() and log_file.stat().st_size > 0, (
        f"会话失败未写运行日志: stdout={failed.stdout!r} "
        f"stderr={failed.stderr!r}")


def test_missing_package_resource_fails_init(installed: _Installed) -> None:
    """包资源缺失时 init 明确失败，不静默创建不完整状态库。"""
    origin = _probe(installed, "import camctl, sys; sys.stdout.write(camctl.__file__)")
    resource = Path(origin).parent / "_resources" / "runtime" / "sqlite-runtime.json"
    assert resource.is_file(), f"安装包缺少运行条件资源: {resource}"
    saved = resource.read_bytes()
    resource.unlink()
    try:
        failed = installed.cli("init", "--config", str(installed.config))
    finally:
        resource.write_bytes(saved)
    assert failed.returncode != 0, (
        f"资源缺失仍成功: stdout={failed.stdout!r}")
    assert "sqlite-runtime.json" in failed.stderr, failed.stderr


def test_noncompliant_sqlite_fails_init(installed: _Installed) -> None:
    """实际链接的 SQLite 不满足统一运行条件时 init 明确拒绝。

    版本条件判定本身有单元测试；此处以解释器内属性替换模拟不达标
    的 SQLite，验证 CLI 入口把运行库条件不满足报告为明确失败。
    """
    wrapper = installed.deploy / "bad_sqlite_entry.py"
    wrapper.write_text(
        "\n".join([
            "import sqlite3",
            "import sys",
            f"sqlite3.sqlite_version_info = {_NONCOMPLIANT_SQLITE!r}",
            f"sqlite3.sqlite_version = {'.'.join(map(str, _NONCOMPLIANT_SQLITE))!r}",
            "from camctl.cli import main",
            "raise SystemExit(main(sys.argv[1:]))",
        ]),
        encoding="utf-8",
    )
    environment = _clean_environment()
    environment["CAMCTL_TEST_STATE_DB"] = str(installed.state_db)
    failed = subprocess.run(
        [str(installed.python), str(wrapper), "init", "--config", str(installed.config)],
        capture_output=True, text=True, env=environment,
        cwd=str(installed.deploy), timeout=60)
    assert failed.returncode != 0, (
        f"运行条件不满足仍成功: stdout={failed.stdout!r}")
    assert "运行库条件不满足" in failed.stderr, failed.stderr
    assert "统一运行条件" in failed.stderr, failed.stderr
    assert failed.stdout == ""


@pytest.mark.skipif(os.name != 'posix', reason='部署侧通知描述符继承')
def test_installed_motor_plan_pipe_and_report_use_only_packaged_resources(installed):
    """在仓库外消费共享 Schema、SQL、登记以及真实 submit/run 报告。"""
    config = installed.deploy / 'motor.toml'
    config.write_text(installed.config.read_text().split('[devices.cam-1]')[0]
                      .replace('state.db', 'motor.db'))
    plan = installed.deploy / 'motor-plan.json'
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    plan.write_text(json.dumps({'request_id':'8801','created_at':now,'name':'独立电机发行',
        'actions':[{'name':'位置','type':'motor_control','scheduled_at':now,
                    'params':{'position':2147483647},'policy':{'max_delay_ms':60000}}]}))
    for command,args in [('init',[]),('submit',[str(plan)])]:
        result=installed.cli(command,*args,'--config',str(config))
        assert result.returncode==0,result.stderr
    read_fd,write_fd=os.pipe()
    try:
        result=subprocess.run([str(installed.python),'-m','camctl','run','--config',str(config),
                               '--host-notification-fd',str(write_fd)],pass_fds=(write_fd,),
                              capture_output=True,text=True,cwd=installed.deploy,
                              env=_clean_environment(),timeout=60)
    finally:
        os.close(write_fd)
    try:
        payload=os.read(read_fd,4096)
        assert os.read(read_fd,1)==b''
    finally:
        os.close(read_fd)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'kind':'succeeded'}
    assert json.loads(payload)=={'type':'motor_control','action_instance_id':'1',
                                 'params':{'position':2147483647}}
    # 安装侧校验实际报告；跨文件 $ref 必须完全由 wheel 资源解析。
    code=f'''
from pathlib import Path
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
count=0
for path in Path({str(installed.ready)!r}).glob('status-report-*.json'):
    document=parse_exact_json(path.read_text())
    validate_document('protocol/status-report.schema.json', document)
    count+=sum(a['type']=='motor_control' and a['status']=='succeeded'
               for p in document.get('plans',[]) for a in p['actions'])
assert count > 0
'''
    _probe(installed,code)
