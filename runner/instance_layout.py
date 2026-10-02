from __future__ import annotations

import os
import uuid
from pathlib import Path


KINDS = {"managed", "test-drive"}


def _validate(instance_id: str, kind: str) -> None:
    if str(uuid.UUID(instance_id)) != instance_id:
        raise ValueError("instance id must be a canonical UUID")
    if kind not in KINDS:
        raise ValueError("unsupported instance kind")


def prepare_layout(root: Path, kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError("unsupported instance kind")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    index = root / kind
    index.mkdir(exist_ok=True, mode=0o700)
    return index


def ensure_type_link(root: Path, kind: str, instance_id: str, target: Path) -> Path:
    _validate(instance_id, kind)
    index = prepare_layout(root, kind)
    link = index / instance_id
    target = target.absolute()
    if link.is_symlink():
        if link.resolve(strict=False) == target:
            return link
        link.unlink()
    elif link.exists():
        raise OSError(f"instance type index entry is not a symlink: {link}")
    temporary = index / f".{instance_id}.tmp"
    temporary.unlink(missing_ok=True)
    os.symlink(os.path.relpath(target, index), temporary, target_is_directory=True)
    temporary.replace(link)
    return link


def remove_type_link(root: Path, kind: str, instance_id: str) -> None:
    _validate(instance_id, kind)
    link = root / kind / instance_id
    if link.is_symlink():
        link.unlink()
