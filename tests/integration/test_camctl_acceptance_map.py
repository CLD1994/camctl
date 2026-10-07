"""验收映射的结构与引用检查。

权威验收条目来自 docs/camctl/database/consistency-verification.md，映射
档案为 docs/camctl/software-acceptance.md。本检查验证每条正式验收都有
映射行、字段完整、结论合法、引用的生产入口与测试路径真实存在，且部
分覆盖/开放条目的未核验前提非空。跨模块契约场景的权威清单来自
docs/camctl/verification.md 的跨模块契约检查表，同样逐项验证结构与引
用。一行映射不等于行为通过：行为证据以映射所列用例的实际通过记录为
准，本文件不重复执行那些用例。
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSISTENCY = _REPO_ROOT / "docs/camctl/database/consistency-verification.md"
_VERIFICATION = _REPO_ROOT / "docs/camctl/verification.md"
_ACCEPTANCE = _REPO_ROOT / "docs/camctl/software-acceptance.md"

#: 条目块标题：`#### 验收 W-01` 等。
_ENTRY_HEADING = re.compile(r"^#### 验收 (\S+)$", re.MULTILINE)

#: 权威文档中的条目标记：`- **验收 01**：`、`- **验收 W-01**：`。
_AUTHORITATIVE_ITEM = re.compile(r"^- \*\*验收 (\S+?)\*\*：", re.MULTILINE)

#: 映射块内的字段行。
_FIELDS = (
    "原文", "结论", "归属", "生产入口", "测试", "证据", "未核验前提",
)

#: 场景块内的字段行（无编号原文与归属，规则链接代替原文）。
_SCENARIO_FIELDS = (
    "规则", "结论", "生产入口", "测试", "证据", "未核验前提",
)

_VALID_CONCLUSIONS = ("已覆盖", "部分覆盖", "开放")

_SUMMARY_COUNTS = re.compile(
    r"已覆盖 (\d+) 条，部分覆盖 (\d+) 条，开放 (\d+) 条")


def _authoritative_ids() -> set[str]:
    text = _CONSISTENCY.read_text(encoding="utf-8")
    return set(_AUTHORITATIVE_ITEM.findall(text))


def _parse_blocks(prefix: str, fields: tuple[str, ...]) -> dict[str, dict]:
    """解析映射档案中 `#### <prefix> <名称>` 块到字段的映射。

    名称可含空格（如契约场景名）。字段行只在下一个标题前生效，
    不读取后续章节。
    """
    text = _ACCEPTANCE.read_text(encoding="utf-8")
    parts = re.split(rf"^#### {prefix} ", text, flags=re.MULTILINE)
    blocks: dict[str, dict[str, str]] = {}
    for part in parts[1:]:
        lines = part.splitlines()
        name = lines[0].strip()
        block_fields: dict[str, str] = {}
        for line in lines[1:]:
            if line.startswith("#"):
                break
            match = re.match(r"^- ([^：]+)：(.*)$", line)
            if match is not None and match.group(1) in fields:
                block_fields[match.group(1)] = match.group(2).strip()
        blocks[name] = block_fields
    return blocks


def _mapped_entries() -> dict[str, dict[str, str]]:
    """解析映射档案，返回条目 ID 到字段值的映射。"""
    return _parse_blocks("验收", _FIELDS)


def _authoritative_scenarios() -> list[str]:
    """verification.md 跨模块契约检查表第一列的场景清单（保持表序）。"""
    text = _VERIFICATION.read_text(encoding="utf-8")
    section = text.split("## 跨模块契约检查", 1)[1]
    section = section.split("\n## ", 1)[0]
    scenarios: list[str] = []
    for line in section.splitlines():
        match = re.match(r"^\| ([^|]+?) \|", line)
        if match is None:
            continue
        first = match.group(1).strip()
        if first in ("场景",) or set(first) <= {"-", " "}:
            continue
        scenarios.append(first)
    return scenarios


def _resolve_existing(path_text: str) -> list[str]:
    """按仓库根解析路径（允许通配），返回真实存在的匹配。"""
    pattern = str(_REPO_ROOT / path_text)
    return [name for name in glob.glob(pattern)]


def test_every_software_contract_has_evidence_owner() -> None:
    """每条正式验收都有结构完整的映射行，引用的路径真实存在。"""
    expected = _authoritative_ids()
    assert len(expected) == 163, (
        f"权威验收条目应为 163 条，实际解析到 {len(expected)} 条；"
        "解析器或权威文档发生了变化")
    entries = _mapped_entries()

    missing = sorted(expected - entries.keys())
    extra = sorted(entries.keys() - expected)
    assert not missing, f"缺少映射的正式验收条目: {missing}"
    assert not extra, f"映射了不存在的验收条目: {extra}"

    problems: list[str] = []
    for entry_id in sorted(entries):
        fields = entries[entry_id]
        for name in _FIELDS:
            if name not in fields or not fields[name]:
                problems.append(f"{entry_id}: 缺少字段 {name}")
        conclusion = fields.get("结论", "")
        if conclusion not in _VALID_CONCLUSIONS:
            problems.append(f"{entry_id}: 结论非法 {conclusion!r}")
            continue
        for role in ("生产入口", "测试"):
            for path_text in fields.get(role, "").split("、"):
                path_text = path_text.strip()
                if path_text and not _resolve_existing(path_text):
                    problems.append(
                        f"{entry_id}: {role}路径不存在 {path_text}")
        unverified = fields.get("未核验前提", "")
        if conclusion == "已覆盖":
            if unverified != "无":
                problems.append(
                    f"{entry_id}: 已覆盖条目的未核验前提应为无，"
                    f"实际 {unverified!r}")
        elif unverified in ("", "无"):
            problems.append(
                f"{entry_id}: {conclusion}条目必须列出未核验前提及归属")
    assert not problems, "映射结构或引用问题:\n" + "\n".join(problems)


def test_summary_counts_match_entries() -> None:
    """执行记录中的结论统计与条目实际分布一致，防止映射漂移。"""
    entries = _mapped_entries()
    actual = {
        conclusion: sum(
            1 for fields in entries.values()
            if fields.get("结论") == conclusion)
        for conclusion in _VALID_CONCLUSIONS
    }
    text = _ACCEPTANCE.read_text(encoding="utf-8")
    match = _SUMMARY_COUNTS.search(text)
    assert match is not None, "执行记录缺少结论统计行"
    stated = tuple(int(group) for group in match.groups())
    actual_tuple = (
        actual["已覆盖"], actual["部分覆盖"], actual["开放"])
    assert stated == actual_tuple, (
        f"执行记录统计 {stated} 与条目实际分布 {actual_tuple} 不一致")


def test_every_contract_scenario_is_mapped() -> None:
    """跨模块契约检查的每个场景都有结构完整的映射与真实引用。"""
    expected = _authoritative_scenarios()
    assert len(expected) == 10, (
        f"跨模块契约场景应为 10 项，实际解析到 {len(expected)} 项: "
        f"{expected}；解析器或权威表格发生了变化")
    mapped = _parse_blocks("场景", _SCENARIO_FIELDS)

    missing = [name for name in expected if name not in mapped]
    extra = sorted(set(mapped) - set(expected))
    assert not missing, f"缺少映射的契约场景: {missing}"
    assert not extra, f"映射了不在权威清单中的场景: {extra}"

    problems: list[str] = []
    for name in expected:
        fields = mapped[name]
        for field in _SCENARIO_FIELDS:
            if not fields.get(field):
                problems.append(f"{name}: 缺少字段 {field}")
        conclusion = fields.get("结论", "")
        if conclusion not in _VALID_CONCLUSIONS:
            problems.append(f"{name}: 结论非法 {conclusion!r}")
            continue
        for role in ("生产入口", "测试"):
            for path_text in fields.get(role, "").split("、"):
                path_text = path_text.strip()
                if path_text and not _resolve_existing(path_text):
                    problems.append(f"{name}: {role}路径不存在 {path_text}")
        # 规则字段的文档链接目标必须真实存在（相对 docs/camctl 解析）。
        for target in re.findall(r"\]\(([^)]+)\)", fields.get("规则", "")):
            document = target.split("#", 1)[0].strip()
            if document and not (
                    _REPO_ROOT / "docs/camctl" / document).is_file():
                problems.append(f"{name}: 规则链接目标不存在 {document}")
        unverified = fields.get("未核验前提", "")
        if conclusion == "已覆盖":
            if unverified != "无":
                problems.append(
                    f"{name}: 已覆盖场景的未核验前提应为无，"
                    f"实际 {unverified!r}")
        elif unverified in ("", "无"):
            problems.append(
                f"{name}: {conclusion}场景必须列出未核验前提及归属")
    assert not problems, "场景映射结构或引用问题:\n" + "\n".join(problems)
