"""为独立安装的测试环境准备标准 camctl wheel。"""

from pathlib import Path
import shutil
import sys
from typing import Callable

from round_test_build import build_once, clone_build_source, load_round_context


def prepare_standard_wheel(
    project: Path, destination: Path, run: Callable[[list[str]], object],
) -> tuple[Path, Path]:
    """为调用方提供独立包文件和锁定运行依赖清单。"""
    context = load_round_context("python-wheel")

    def build(source_project: Path, output: Path) -> tuple[Path, Path]:
        dist = output / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        requirements = output / "requirements.txt"
        run(["uv", "export", "--project", str(source_project), "--frozen", "--no-dev",
             "--no-emit-project", "-o", str(requirements)])
        run(["uv", "build", "--project", str(source_project), "--python", sys.executable, "--wheel", "--out-dir",
             str(dist), "--build-constraints", str(requirements)])
        wheels = list(dist.glob("camctl-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"构建应生成一个 camctl wheel，实际为: {wheels}")
        return wheels[0], requirements

    if context is None:
        return build(project, destination)

    artifacts = context.workspace / "artifacts"

    def action() -> None:
        source = clone_build_source(context)
        wheel, requirements = build(source / "apps/camctl", artifacts)
        wheel.chmod(0o444)
        requirements.chmod(0o444)

    build_once(context.workspace, context.receipt, action, label="python-wheel")
    wheels = list((artifacts / "dist").glob("camctl-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"共享构建应提供一个 camctl wheel，实际为: {wheels}")
    dist = destination / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    wheel = dist / wheels[0].name
    requirements = destination / "requirements.txt"
    shutil.copyfile(wheels[0], wheel)
    shutil.copyfile(artifacts / "requirements.txt", requirements)
    return wheel, requirements
