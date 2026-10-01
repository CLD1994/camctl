"""正式产物登记及文件关系的纯计算。

登记保留原设备文件或中间文件身份；没有归属或文件完成依据不能
登记；未知摘要合法保存，已有可靠摘要不得丢失。原片、修复与预览
的关联按明确引用配对，任意目录顺序不能替代配对。本模块只计算
登记变化，由采集完整终态事务在同一事务中应用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "FileReference",
    "OutputCatalogFacts",
    "OutputDraft",
    "OutputKind",
    "RegisteredOutput",
    "RegistrationChanges",
    "validate_output_registration",
]


class OutputKind(Enum):
    """正式产物种类；成员名与整数登记一致。"""

    ORIGINAL = "original"
    REPAIRED = "repaired"
    PREVIEW = "preview"


@dataclass(frozen=True)
class FileReference:
    """产物承载文件的身份：设备文件与中间文件恰好其一。"""

    device_file_id: int | None = None
    intermediate_file_id: int | None = None

    def __post_init__(self) -> None:
        if (self.device_file_id is None) == (self.intermediate_file_id is None):
            raise ValueError("文件身份必须恰好指向设备文件或中间文件之一")


@dataclass(frozen=True)
class OutputCatalogFacts:
    """登记所依据的目录事实。"""

    action_id: int
    ownership_confirmed: bool


@dataclass(frozen=True)
class OutputDraft:
    """一份待登记产物：种类、文件、完成依据与关联。

    sha256 为空表示摘要未知（合法，不强制作登记前置计算）；已有
    可靠值原样保留。修复与预览必须提供 original_output_id（既有
    产物）或 original_batch_file_id（同批原片的文件身份）之一；
    原片不携带关联。
    """

    kind: OutputKind
    file: FileReference
    file_complete: bool
    sha256: str | None = None
    original_output_id: int | None = None
    original_batch_file_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind is OutputKind.ORIGINAL and (
            self.original_output_id is not None
            or self.original_batch_file_id is not None
        ):
            raise ValueError("原片不携带产物关联")
        if self.kind is not OutputKind.ORIGINAL:
            has_existing = self.original_output_id is not None
            has_batch = self.original_batch_file_id is not None
            if has_existing == has_batch:
                raise ValueError(
                    f"{self.kind.value} 必须恰好提供既有产物或同批文件身份之一"
                )


@dataclass(frozen=True)
class RegisteredOutput:
    """一份通过登记校验的产物：文件身份与关联保持原事实。"""

    kind: OutputKind
    device_file_id: int | None
    intermediate_file_id: int | None
    sha256: str | None
    pairs_with: int | None = None


@dataclass(frozen=True)
class RegistrationChanges:
    """一次登记的全部产物变化；由采集终态事务共同应用。"""

    outputs: tuple[RegisteredOutput, ...]


def validate_output_registration(
    drafts: tuple[OutputDraft, ...], facts: OutputCatalogFacts
) -> RegistrationChanges:
    """校验一批产物登记并计算登记变化。

    没有归属或文件完成依据的草稿拒绝登记；文件身份不得重复；预
    览与修复的关联按明确引用解析，与输入顺序无关。
    """
    if not facts.ownership_confirmed:
        raise ValueError("产物归属未确认，不能登记")
    seen_files: set[int] = set()
    registered: list[RegisteredOutput] = []
    batch_files: dict[int, OutputKind] = {}
    for draft in drafts:
        file_id = draft.file.device_file_id or draft.file.intermediate_file_id or 0
        if file_id in seen_files:
            raise ValueError(f"文件身份重复登记: {file_id}")
        seen_files.add(file_id)
        if not draft.file_complete:
            raise ValueError(f"文件尚未完成，不能登记: {file_id}")
        batch_files[file_id] = draft.kind
    for draft in drafts:
        file_id = draft.file.device_file_id or draft.file.intermediate_file_id or 0
        pairs_with: int | None = None
        if draft.original_output_id is not None:
            pairs_with = draft.original_output_id
        elif draft.original_batch_file_id is not None:
            referenced = draft.original_batch_file_id
            if batch_files.get(referenced) is not OutputKind.ORIGINAL:
                raise ValueError(
                    f"同批配对必须指向原片文件: {referenced}"
                )
            pairs_with = referenced
        registered.append(
            RegisteredOutput(
                kind=draft.kind,
                device_file_id=draft.file.device_file_id,
                intermediate_file_id=draft.file.intermediate_file_id,
                sha256=draft.sha256,
                pairs_with=pairs_with,
            )
        )
    return RegistrationChanges(outputs=tuple(registered))
