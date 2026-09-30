from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .systemd import BackendError


class ConfigPreviewLibrary:
    """Short-lived, file-backed native config approval transactions."""

    def __init__(self, root: Path, ttl_seconds: int = 1800) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def create(self, document: dict, assets: Path | None = None) -> dict:
        with self.lock:
            preview_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            stored = {
                **document,
                "preview_id": preview_id,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            }
            temporary = self.root / f".{preview_id}.new"
            target = self.root / f"{preview_id}.json"
            temporary.write_text(json.dumps(stored, separators=(",", ":")), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(target)
            if assets is not None:
                if not assets.is_dir():
                    target.unlink(missing_ok=True)
                    raise BackendError("config preview assets are unavailable")
                assets_target = self.root / f"{preview_id}.assets"
                try:
                    shutil.copytree(assets, assets_target)
                except OSError as exc:
                    target.unlink(missing_ok=True)
                    shutil.rmtree(assets_target, ignore_errors=True)
                    raise BackendError(f"cannot snapshot config preview assets: {exc}") from exc
            return stored

    @staticmethod
    def _validate_id(preview_id: str) -> None:
        try:
            if str(uuid.UUID(preview_id)) != preview_id:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise BackendError("config preview id is invalid") from exc

    def _read(self, path: Path) -> dict:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(document["expires_at"])
            if datetime.now(timezone.utc) >= expires:
                path.unlink(missing_ok=True)
                raise BackendError("config preview expired")
            return document
        except FileNotFoundError as exc:
            raise BackendError("config preview not found") from exc
        except BackendError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise BackendError("config preview is invalid") from exc

    def get(self, preview_id: str) -> dict:
        self._validate_id(preview_id)
        with self.lock:
            return self._read(self.root / f"{preview_id}.json")

    def claim(self, preview_id: str) -> dict:
        """Atomically hide a preview from concurrent commit requests."""
        self._validate_id(preview_id)
        with self.lock:
            source = self.root / f"{preview_id}.json"
            claimed = self.root / f"{preview_id}.claimed"
            try:
                source.replace(claimed)
            except FileNotFoundError as exc:
                raise BackendError("config preview not found or already claimed") from exc
            return self._read(claimed)

    def release(self, preview_id: str) -> None:
        self._validate_id(preview_id)
        with self.lock:
            claimed = self.root / f"{preview_id}.claimed"
            if claimed.exists():
                claimed.replace(self.root / f"{preview_id}.json")

    def resolve_assets(self, preview_id: str) -> Path:
        self._validate_id(preview_id)
        with self.lock:
            assets = self.root / f"{preview_id}.assets"
            if not assets.is_dir():
                raise BackendError("config preview assets are unavailable")
            return assets

    def delete(self, preview_id: str) -> None:
        self._validate_id(preview_id)
        with self.lock:
            (self.root / f"{preview_id}.json").unlink(missing_ok=True)
            (self.root / f"{preview_id}.claimed").unlink(missing_ok=True)
            shutil.rmtree(self.root / f"{preview_id}.assets", ignore_errors=True)
