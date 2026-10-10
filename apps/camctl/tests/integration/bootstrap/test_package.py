"""B1 可安装包与权威资源来源的组件集成测试。

从仓库权威输入构建 wheel，在独立环境中安装，核对包内资源与权威来源逐字节一致。
这是集成测试：执行包构建与安装，不连接网络下载 Schema，不访问真实设备。
"""

import hashlib
import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = PROJECT_DIR.parents[1]

# 资源名和来源映射只由构建登记维护；这里独立核对安装后的实际字节。
_RESOURCE_SOURCES = runpy.run_path(str(PROJECT_DIR / "scripts" / "sync_resources.py"))["RESOURCE_SOURCES"]
AUTHORITATIVE_RESOURCES = {name: REPO_ROOT / source for name, source in _RESOURCE_SOURCES.items()}

# 在安装环境中执行：读取全部包资源并输出摘要，避免与被测代码共享同一计算路径。
_PROBE_SCRIPT = """
import hashlib
import json
import runpy
import sys

from camctl.resources import available_resources, resource_bytes

payload = {}
for name in sorted(available_resources()):
    data = resource_bytes(name)
    payload[name] = hashlib.sha256(data).hexdigest()
sys.stdout.write(json.dumps(payload))
"""


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


def _venv_cli(env_dir: Path) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / "camctl.exe"
    return env_dir / "bin" / "camctl"


def test_wheel_contains_authoritative_resources(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _run(["uv", "build", "--project", str(PROJECT_DIR), "--wheel", "--out-dir", str(dist_dir)])
    wheels = list(dist_dir.glob("camctl-*.whl"))
    assert len(wheels) == 1, f"期望恰好一个 wheel，实际: {wheels}"

    env_dir = tmp_path / "env"
    _run(["uv", "venv", "--python", sys.executable, str(env_dir)])
    # resource_bytes 只依赖标准库；--no-deps 使安装不依赖网络解析生产依赖。
    _run(["uv", "pip", "install", "--no-deps", "--python", str(_venv_python(env_dir)), str(wheels[0])])

    probe = _run([str(_venv_python(env_dir)), "-c", _PROBE_SCRIPT])
    installed_digests = json.loads(probe.stdout)

    # 软件演示必须能在没有源码目录的部署上取得四份拍摄、取回和确认样例。
    assert {"examples/camera-demo/action6-record.json", "examples/camera-demo/action6-timelapse.json",
            "examples/camera-demo/osmo360ii-record.json", "examples/camera-demo/osmo360ii-timelapse.json",
            "examples/camera-demo/obtain.json", "examples/camera-demo/report-ack.json",
            "examples/camera-demo/config.toml", "examples/camera-demo/prepare-plan.py"} <= installed_digests.keys()
    expected_digests = {}
    for name, source_path in AUTHORITATIVE_RESOURCES.items():
        assert source_path.is_file(), f"权威来源缺失: {source_path}"
        expected_digests[name] = hashlib.sha256(source_path.read_bytes()).hexdigest()

    assert installed_digests == expected_digests, (
        "安装包资源与权威来源不一致；"
        f"仅安装侧: {sorted(set(installed_digests) - set(expected_digests))}；"
        f"仅预期侧: {sorted(set(expected_digests) - set(installed_digests))}；"
        "内容差异见上方逐项摘要"
    )

    # CLI 入口随安装物存在。
    assert _venv_cli(env_dir).exists()


def test_resource_names_reject_arbitrary_paths(tmp_path: Path) -> None:
    """资源访问只接受构建资源目录中的登记名称，不能穿透到任意路径。"""
    from camctl.resources import ResourceError, resource_bytes

    for bad in [
        "../resources.py",
        "..\\..\\..\\..\\pyproject.toml",
        "/etc/passwd",
        "protocol/../../../pyproject.toml",
        "protocol/nonexistent.schema.json",
        "",
    ]:
        try:
            resource_bytes(bad)
        except ResourceError:
            continue
        raise AssertionError(f"非法资源名未被拒绝: {bad!r}")


def test_generated_resources_match_authoritative_sources() -> None:
    """同步脚本以 --check 核对已生成资源与权威来源一致，缺失或漂移时失败。"""
    sync_script = PROJECT_DIR / "scripts" / "sync_resources.py"
    assert sync_script.is_file(), f"同步脚本缺失: {sync_script}"
    _run([sys.executable, str(sync_script), "--check", "--root", str(REPO_ROOT)])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_wheel_contains_authoritative_resources(Path(tmp))
