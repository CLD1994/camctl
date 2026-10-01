"""X1 正式产物登记及文件关系的单元测试。

正式登记保留原设备文件身份；没有归属或文件完成依据不能登记；
未知摘要合法保存，已有可靠摘要不得丢失；原片、修复与预览的关
联按明确引用配对，任意目录顺序不能替代配对。
"""

from __future__ import annotations

import pytest

from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
    validate_output_registration,
)

_FACTS = OutputCatalogFacts(action_id=1, ownership_confirmed=True)


def _original(file_id: int = 11, *, complete: bool = True, sha256: str | None = None):
    return OutputDraft(
        kind=OutputKind.ORIGINAL,
        file=FileReference(device_file_id=file_id),
        file_complete=complete,
        sha256=sha256,
    )


class TestRegistrationBasics:
    def test_registration_preserves_device_file_identity(self) -> None:
        changes = validate_output_registration(
            (_original(11, sha256="aa" * 32),), _FACTS
        )
        assert len(changes.outputs) == 1
        registered = changes.outputs[0]
        assert registered.device_file_id == 11
        assert registered.intermediate_file_id is None

    def test_registration_without_ownership_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_output_registration(
                (_original(),),
                OutputCatalogFacts(action_id=1, ownership_confirmed=False),
            )

    def test_incomplete_file_cannot_register(self) -> None:
        with pytest.raises(ValueError):
            validate_output_registration((_original(complete=False),), _FACTS)

    def test_unknown_checksum_is_legal(self) -> None:
        changes = validate_output_registration((_original(11,),), _FACTS)
        assert changes.outputs[0].sha256 is None

    def test_known_checksum_is_preserved(self) -> None:
        digest = "ab" * 32
        changes = validate_output_registration(
            (_original(11, sha256=digest),), _FACTS
        )
        assert changes.outputs[0].sha256 == digest

    def test_duplicate_file_identity_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_output_registration((_original(11), _original(11)), _FACTS)

    def test_file_reference_must_be_exclusive(self) -> None:
        with pytest.raises(ValueError):
            OutputDraft(
                kind=OutputKind.ORIGINAL,
                file=FileReference(device_file_id=1, intermediate_file_id=2),
                file_complete=True,
                sha256=None,
            )
        with pytest.raises(ValueError):
            OutputDraft(
                kind=OutputKind.ORIGINAL,
                file=FileReference(),
                file_complete=True,
                sha256=None,
            )

    def test_intermediate_file_registration_keeps_identity(self) -> None:
        draft = OutputDraft(
            kind=OutputKind.REPAIRED,
            file=FileReference(intermediate_file_id=31),
            file_complete=True,
            sha256=None,
            original_output_id=5,
        )
        changes = validate_output_registration((draft,), _FACTS)
        assert changes.outputs[0].intermediate_file_id == 31
        assert changes.outputs[0].device_file_id is None


class TestExplicitPairing:
    def test_preview_pairs_by_explicit_reference_not_order(self) -> None:
        """预览按明确引用配对；打乱输入顺序不改变关联。"""
        original = _original(11)
        preview = OutputDraft(
            kind=OutputKind.PREVIEW,
            file=FileReference(device_file_id=12),
            file_complete=True,
            sha256=None,
            original_batch_file_id=11,
        )
        first = validate_output_registration((original, preview), _FACTS)
        second = validate_output_registration((preview, original), _FACTS)
        assert {o.device_file_id: o for o in first.outputs} == {
            o.device_file_id: o for o in second.outputs
        }
        preview_row = next(
            o for o in first.outputs if o.device_file_id == 12
        )
        original_row = next(o for o in first.outputs if o.device_file_id == 11)
        assert preview_row.pairs_with == original_row.device_file_id

    def test_preview_without_pairing_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            preview = OutputDraft(
                kind=OutputKind.PREVIEW,
                file=FileReference(device_file_id=12),
                file_complete=True,
                sha256=None,
            )
            validate_output_registration((preview,), _FACTS)

    def test_repaired_pairs_with_existing_output(self) -> None:
        draft = OutputDraft(
            kind=OutputKind.REPAIRED,
            file=FileReference(intermediate_file_id=31),
            file_complete=True,
            sha256=None,
            original_output_id=5,
        )
        changes = validate_output_registration((draft,), _FACTS)
        assert changes.outputs[0].pairs_with == 5

    def test_original_does_not_carry_pairing(self) -> None:
        with pytest.raises(ValueError):
            OutputDraft(
                kind=OutputKind.ORIGINAL,
                file=FileReference(device_file_id=11),
                file_complete=True,
                sha256=None,
                original_output_id=5,
            )

    def test_preview_reference_to_missing_batch_file_is_rejected(self) -> None:
        preview = OutputDraft(
            kind=OutputKind.PREVIEW,
            file=FileReference(device_file_id=12),
            file_complete=True,
            sha256=None,
            original_batch_file_id=99,
        )
        with pytest.raises(ValueError):
            validate_output_registration((_original(11), preview), _FACTS)
