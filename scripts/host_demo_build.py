"""跨组件测试使用的 host-demo 构建配方和本轮共享构建。"""
from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import stat

from round_test_build import build_once


def build_steps(source: str, build: str) -> tuple[str, str]:
    """直接运行和本轮共享构建使用同一份 Release 配方。"""
    source, build = shlex.quote(source), shlex.quote(build)
    return (f"cmake -S {source} -B {build} -DCMAKE_BUILD_TYPE=Release >/dev/null",
            f"cmake --build {build} --target host-demo -j4 >/dev/null")


def build(source: str, destination: str, run) -> None:
    for step in build_steps(source, destination):
        result = run(step)
        if result.returncode:
            raise RuntimeError(f"host-demo 构建步骤失败: {step}\n{result.stderr}")


def copy_round_build(source: Path, shared: Path, destination: Path, receipt: Path, run) -> None:
    """首次请求时构建，再复制只读产物到调用方独占目录。"""
    def produce():
        build(str(source), str(shared / 'build'), run)
        for name in ('host-demo', 'libcamctl_host.a'):
            artifact = shared / 'build' / name
            artifact.chmod(artifact.stat().st_mode & ~0o222)

    build_once(shared, receipt, produce, label='host-demo')
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('host-demo', 'libcamctl_host.a'):
        target = destination / name
        shutil.copyfile(shared / 'build' / name, target)
        mode = (shared / 'build' / name).stat().st_mode
        target.chmod(stat.S_IMODE(mode) | stat.S_IWUSR)
