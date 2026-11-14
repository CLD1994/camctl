"""目录绑定值分类。"""

import os
from pathlib import Path
import uuid
import pytest
from camctl.persistence.runtime import (
    DirectoryBindingError, canonical_directory_binding, verify_directory_binding,
)

_ROOT = Path("C:/old") if os.name == "nt" else Path("/old")
DIRECTORIES = tuple(canonical_directory_binding(_ROOT / name)
                    for name in ("staging", "ready", "processing"))

def test_directory_binding_verification() -> None:
    from camctl.persistence.directory_switch import canonical_to_path
    metadata_ok, metadata_off = (
        _metadata(staging=s, ready=r, processing=p)
        for s, r, p in (DIRECTORIES, (
            canonical_directory_binding(_ROOT.parent / "other" / "staging"),
            *DIRECTORIES[1:]))
    )
    configured = tuple(str(canonical_to_path(value)) for value in DIRECTORIES)
    verify_directory_binding(metadata_ok, *configured)
    with pytest.raises(DirectoryBindingError):
        verify_directory_binding(metadata_off, *configured)


def _metadata(staging: str, ready: str, processing: str):
    from camctl.persistence.runtime import DatabaseMetadata

    return DatabaseMetadata(
        application_id="camctl",
        instance_id=uuid.uuid4().hex,
        format_version=1,
        staging_path=staging,
        ready_path=ready,
        processing_path=processing,
    )
