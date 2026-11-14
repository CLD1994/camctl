"""检查数据库基础设施的真实传递依赖，不执行项目源码。"""

from pathlib import Path

from support.storage_dependencies import Source, _violations


ROOTS = (
    "camctl.persistence.atomic",
    "camctl.persistence.transaction_data",
    "camctl.persistence.runtime",
    "camctl.persistence.executor",
    "camctl.persistence.business",
)


def _sources(root: Path) -> dict[str, Source]:
    result = {}
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        package = parts[-1] == "__init__"
        name = ".".join(("camctl", *(parts[:-1] if package else parts)))
        result[name] = Source(path.read_text(encoding="utf-8"), package)
    return result


def test_database_infrastructure_has_no_business_dependency_closure():
    root = Path(__file__).resolve().parents[3] / "src" / "camctl"
    violations = _violations(_sources(root), ROOTS)
    assert not violations, "数据库基础设施可达业务模块或动态导入:\n" + "\n".join(violations)
