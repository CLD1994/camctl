"""验收映射的结构与引用检查。

权威验收条目来自 docs/camctl/database/consistency-verification.md，映射
档案为 docs/camctl/software-acceptance.md。本检查验证每条正式验收都有
映射行、字段完整、结论合法、引用的生产入口与测试路径真实存在，且部
分覆盖/开放条目的未核验前提非空。一行映射不等于行为通过：行为证据以
映射所列用例的实际通过记录为准，本文件不重复执行那些用例。
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSISTENCY = _REPO_ROOT / "docs/camctl/database/consistency-verification.md"
_ACCEPTANCE = _REPO_ROOT / "docs/camctl/software-acceptance.md"

#: 条目块标题：`#### 验收 W-01` 等。
_ENTRY_HEADING = re.compile(r"^#### 验收 (\S+)$", re.MULTILINE)

#: 权威文档中的条目标记：`- **验收 01**：`、`- **验收 W-01**：`。
_AUTHORITATIVE_ITEM = re.compile(r"^- \*\*验收 (\S+?)\*\*：", re.MULTILINE)

#: 映射块内的字段行。
_FIELDS = (
    "原文", "结论", "归属", "生产入口", "测试", "证据", "未核验前提",
)

_VALID_CONCLUSIONS = ("已覆盖", "部分覆盖", "开放")

_SUMMARY_COUNTS = re.compile(
    r"已覆盖 (\d+) 条，部分覆盖 (\d+) 条，开放 (\d+) 条")


def _authoritative_ids() -> set[str]:
    text = _CONSISTENCY.read_text(encoding="utf-8")
    return set(_AUTHORITATIVE_ITEM.findall(text))


def _mapped_entries() -> dict[str, dict[str, str]]:
    """解析映射档案，返回条目 ID 到字段值的映射。"""
    text = _ACCEPTANCE.read_text(encoding="utf-8")
    parts = re.split(r"^#### 验收 ", text, flags=re.MULTILINE)
    entries: dict[str, dict[str, str]] = {}
    for part in parts[1:]:
        lines = part.splitlines()
        entry_id = lines[0].strip()
        fields: dict[str, str] = {}
        for line in lines[1:]:
            match = re.match(r"^- ([^：]+)：(.*)$", line)
            if match is not None and match.group(1) in _FIELDS:
                fields[match.group(1)] = match.group(2).strip()
        entries[entry_id] = fields
    return entries


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
            path_text = fields.get(role, "")
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
