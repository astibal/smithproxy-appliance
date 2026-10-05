from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .systemd import BackendError


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}")


class QemuImageLibrary:
    """Atomic catalogue for QEMU blackboxes with N disks and N NICs.

    This is deliberately a manifest store.  Large qcow2 files are staged by an
    operator below ``import_root`` and imported by path; they never travel as a
    base64 JSON body through the console/runner control API.
    """

    def __init__(self, root: Path, import_root: Path) -> None:
        self.root = root
        self.import_root = import_root.resolve()
        self.index = root / "images.json"
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _load(self) -> list[dict[str, Any]]:
        try:
            document = json.loads(self.index.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendError(f"cannot read QEMU image library: {exc}") from exc
        images = document.get("images") if isinstance(document, dict) else None
        if not isinstance(images, list):
            raise BackendError("QEMU image index must contain an images array")
        return [item for item in images if isinstance(item, dict)]

    def _save(self, images: list[dict[str, Any]]) -> None:
        temporary = self.index.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema": 1, "images": images}, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(self.index)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return sorted(self._load(), key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, image_id: str) -> dict[str, Any]:
        item = next((item for item in self.list() if item.get("image_id") == image_id), None)
        if not item:
            raise BackendError("QEMU image is unavailable")
        return item

    def _source(self, value: object) -> Path:
        raw = Path(str(value or "").strip())
        candidate = raw.resolve() if raw.is_absolute() else (self.import_root / raw).resolve()
        try:
            candidate.relative_to(self.import_root)
        except ValueError as exc:
            raise BackendError("disk source must be below the QEMU import directory") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise BackendError(f"disk source is not a regular file: {candidate.name}")
        if candidate.suffix.lower() not in {".qcow2", ".qcow"}:
            raise BackendError("disk source must be a qcow2/qcow image")
        return candidate

    def create(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BackendError("QEMU image manifest must be an object")
        name = str(payload.get("name", "")).strip()
        if not SAFE_NAME.fullmatch(name):
            raise BackendError("QEMU image name is invalid")
        disks = payload.get("disks")
        nics = payload.get("nics", [])
        if not isinstance(disks, list) or not 1 <= len(disks) <= 16:
            raise BackendError("QEMU image requires 1 to 16 disks")
        if not isinstance(nics, list) or len(nics) > 16:
            raise BackendError("QEMU image supports at most 16 network interfaces")
        forensic = payload.get("forensic", {})
        if not isinstance(forensic, dict):
            raise BackendError("forensic policy must be an object")
        forensic_enabled = forensic.get("enabled", True)
        if not isinstance(forensic_enabled, bool):
            raise BackendError("forensic enabled flag must be boolean")
        requested_artifacts = forensic.get(
            "artifacts", ["disk-overlays", "memory", "pcap", "qemu-log", "manifest"]
        )
        allowed_artifacts = {"disk-overlays", "memory", "pcap", "qemu-log", "serial-log", "manifest"}
        if (not isinstance(requested_artifacts, list)
                or any(item not in allowed_artifacts for item in requested_artifacts)):
            raise BackendError("unsupported forensic artifact")
        requested_hash = str(forensic.get("hash", "sha256"))
        if requested_hash not in {"sha256", "sha512"}:
            raise BackendError("forensic hash must be sha256 or sha512")

        clean_disks, seen_targets = [], set()
        for position, raw in enumerate(disks):
            if not isinstance(raw, dict):
                raise BackendError("each QEMU disk must be an object")
            source = self._source(raw.get("source"))
            target = str(raw.get("target", f"vd{chr(97 + position)}")).strip()
            bus = str(raw.get("bus", "virtio")).strip()
            role = str(raw.get("role", "system" if position == 0 else "data")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", target) or target in seen_targets:
                raise BackendError("QEMU disk targets must be unique safe names")
            if bus not in {"virtio", "scsi", "sata", "ide"} or role not in {"system", "data"}:
                raise BackendError("unsupported QEMU disk bus or role")
            seen_targets.add(target)
            clean_disks.append({"source": str(source), "target": target, "bus": bus,
                                "role": role, "boot_order": position + 1,
                                # The imported image is always immutable. A future
                                # runtime writes only into a per-instance overlay.
                                "base_read_only": True, "overlay": "per-instance"})

        clean_nics = []
        for position, raw in enumerate(nics):
            if not isinstance(raw, dict):
                raise BackendError("each QEMU network interface must be an object")
            model = str(raw.get("model", "virtio-net-pci"))
            purpose = str(raw.get("purpose", "dataplane"))
            if model not in {"virtio-net-pci", "e1000", "e1000e", "rtl8139"}:
                raise BackendError("unsupported QEMU network model")
            if purpose not in {"dataplane", "telemetry"}:
                raise BackendError("network purpose must be dataplane or telemetry")
            clean_nics.append({"slot": position, "model": model, "purpose": purpose})

        with self.lock:
            images = self._load()
            image = {"image_id": str(uuid.uuid4()), "name": name,
                     "description": str(payload.get("description", "")).strip()[:1000],
                     "architecture": str(payload.get("architecture", "x86_64")),
                     "machine": str(payload.get("machine", "q35")),
                     "disks": clean_disks, "nics": clean_nics,
                     "storage_policy": {
                         "base_images": "immutable",
                         "runtime_writes": "qcow2-overlay",
                         "reset": "discard-all-overlays",
                         "snapshot_scope": "all-writable-disks",
                     },
                     "forensic_policy": {
                         "enabled": forensic_enabled,
                         "artifacts": list(dict.fromkeys(requested_artifacts)),
                         "hash": requested_hash,
                         "seal_before_reset": forensic_enabled,
                         "evidence": "immutable-export",
                         "working_copy": "separate",
                     },
                     "state": "staged", "created_at": datetime.now(timezone.utc).isoformat()}
            if image["architecture"] not in {"x86_64", "aarch64"}:
                raise BackendError("unsupported QEMU architecture")
            if image["machine"] not in {"q35", "pc", "virt"}:
                raise BackendError("unsupported QEMU machine")
            images.append(image)
            self._save(images)
            return image

    def delete(self, image_id: str) -> dict[str, Any]:
        with self.lock:
            images = self._load()
            remaining = [item for item in images if item.get("image_id") != image_id]
            if len(remaining) == len(images):
                raise BackendError("QEMU image is unavailable")
            self._save(remaining)
            return {"image_id": image_id, "deleted": True}
