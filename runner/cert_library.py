from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .systemd import BackendError


SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,126}[A-Za-z0-9]$|[A-Za-z0-9]$")
SAFE_STEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_CERT_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(?:pem|crt|cer)$", re.I)


class CertBundleLibrary:
    """Filesystem-backed CA keypair and certificate bundle library."""

    def __init__(self, root: Path, openssl: str = "openssl") -> None:
        self.root = root
        self.openssl = openssl
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _clean_name(value: str, label: str = "name") -> str:
        value = value.strip()[:128]
        if not value or not SAFE_NAME_RE.fullmatch(value):
            raise BackendError(f"invalid certificate {label}")
        return value

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [self.openssl, *args], check=True, capture_output=True, text=True,
                timeout=30, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except FileNotFoundError as exc:
            raise BackendError("openssl is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("openssl operation timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "openssl failed").strip().splitlines()[-1]
            raise BackendError(f"openssl failed: {detail[:300]}") from exc
        return result.stdout

    @staticmethod
    def _fingerprint(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _certificate_info(self, path: Path) -> dict:
        output = self._run(
            "x509", "-in", str(path), "-noout", "-subject", "-issuer",
            "-serial", "-dates", "-fingerprint", "-sha256",
        )
        result: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                result[key.strip().lower().replace(" ", "_")] = value.strip()
        result["sha256"] = self._fingerprint(path)
        return result

    def _load(self, path: Path) -> dict | None:
        try:
            item = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
            if item.get("bundle_id") != path.name or str(uuid.UUID(path.name)) != path.name:
                return None
            certs = path / "certs"
            if not (certs / "ca-cert.pem").is_file() or not (certs / "ca-key.pem").is_file():
                return None
            item["certificates"] = [
                entry for entry in item.get("certificates", [])
                if isinstance(entry, dict)
                and Path(str(entry.get("filename", ""))).name == entry.get("filename")
                and (not entry.get("key_filename") or (
                    Path(str(entry["key_filename"])).name == entry["key_filename"]
                ))
                and (certs / str(entry.get("filename", ""))).is_file()
            ]
            if not any(
                entry.get("kind") == "ca" and entry.get("filename") == "ca-cert.pem"
                for entry in item["certificates"]
            ):
                return None
            return item
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _save(self, root: Path, item: dict) -> None:
        target = root / "metadata.json"
        temporary = root / ".metadata.tmp"
        temporary.write_text(json.dumps(item, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(target)

    def list(self) -> list[dict]:
        with self.lock:
            try:
                items = [item for path in self.root.iterdir() if path.is_dir() and (item := self._load(path))]
            except OSError:
                return []
            return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, bundle_id: str) -> dict:
        try:
            if str(uuid.UUID(bundle_id)) != bundle_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("invalid certificate bundle id") from exc
        item = self._load(self.root / bundle_id)
        if not item:
            raise BackendError("certificate bundle is unavailable")
        return item

    def resolve(self, bundle_id: str) -> Path:
        item = self.get(bundle_id)
        certs = self.root / bundle_id / "certs"
        try:
            for entry in item["certificates"]:
                certificate = certs / entry["filename"]
                if self._fingerprint(certificate) != entry.get("sha256"):
                    raise BackendError("certificate bundle failed integrity verification")
                key_filename = entry.get("key_filename")
                if entry.get("kind") == "ca":
                    key_filename = "ca-key.pem"
                if key_filename:
                    key = certs / key_filename
                    if not key.is_file() or self._fingerprint(key) != entry.get("key_sha256"):
                        raise BackendError("certificate private key failed integrity verification")
        except OSError as exc:
            raise BackendError(f"cannot verify certificate bundle: {exc}") from exc
        certificate_public = self._run(
            "x509", "-in", str(certs / "ca-cert.pem"), "-pubkey", "-noout"
        )
        private_public = self._run("pkey", "-in", str(certs / "ca-key.pem"), "-pubout")
        if certificate_public.strip() != private_public.strip():
            raise BackendError("CA certificate and private key do not match")
        return certs

    def create(self, name: str, common_name: str, days: int = 3650) -> dict:
        name = self._clean_name(name, "bundle name")
        common_name = self._clean_name(common_name, "CA common name")
        if not 1 <= days <= 7300:
            raise BackendError("CA validity must be between 1 and 7300 days")
        bundle_id = str(uuid.uuid4())
        temporary = self.root / f".{bundle_id}.new"
        target = self.root / bundle_id
        with self.lock:
            try:
                temporary.mkdir(mode=0o700)
                certs = temporary / "certs"
                certs.mkdir(mode=0o700)
                key = certs / "ca-key.pem"
                cert = certs / "ca-cert.pem"
                self._run(
                    "req", "-x509", "-newkey", "rsa:3072", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", str(days),
                    "-sha256", "-subj", f"/CN={common_name}",
                    "-addext", "basicConstraints=critical,CA:TRUE,pathlen:1",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext", "subjectKeyIdentifier=hash",
                )
                os.chmod(key, 0o600)
                os.chmod(cert, 0o644)
                ca_entry = {
                    "certificate_id": "ca",
                    "name": common_name,
                    "filename": "ca-cert.pem",
                    "kind": "ca",
                    "has_private_key": True,
                    "key_sha256": self._fingerprint(key),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **self._certificate_info(cert),
                }
                item = {
                    "bundle_id": bundle_id,
                    "name": name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "ca_common_name": common_name,
                    "ca_key_password": "",
                    "certificates": [ca_entry],
                }
                self._save(temporary, item)
                temporary.replace(target)
                return item
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    def generate_certificate(self, bundle_id: str, name: str, common_name: str,
                             days: int = 825, file_stem: str = "") -> dict:
        name = self._clean_name(name)
        common_name = self._clean_name(common_name, "common name")
        if not 1 <= days <= 825:
            raise BackendError("certificate validity must be between 1 and 825 days")
        with self.lock:
            item = self.get(bundle_id)
            root = self.root / bundle_id
            certs = root / "certs"
            certificate_id = str(uuid.uuid4())
            stem = file_stem.strip() or f"cert-{certificate_id}"
            if not SAFE_STEM_RE.fullmatch(stem) or stem.lower() == "ca":
                raise BackendError("certificate file stem is invalid or reserved")
            key = certs / f"{stem}-key.pem"
            csr = certs / f".{stem}.csr"
            ext = certs / f".{stem}.ext"
            cert = certs / f"{stem}-cert.pem"
            if key.exists() or cert.exists():
                raise BackendError("certificate file names already exist in this bundle")
            try:
                self._run(
                    "req", "-new", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={common_name}",
                )
                ext.write_text(
                    "basicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage=serverAuth,clientAuth\n",
                    encoding="ascii",
                )
                self._run(
                    "x509", "-req", "-in", str(csr),
                    "-CA", str(certs / "ca-cert.pem"), "-CAkey", str(certs / "ca-key.pem"),
                    "-set_serial", str(secrets.randbits(127) | 1), "-out", str(cert),
                    "-days", str(days), "-sha256", "-extfile", str(ext),
                )
                os.chmod(key, 0o600)
                os.chmod(cert, 0o644)
                entry = {
                    "certificate_id": certificate_id,
                    "name": name,
                    "filename": cert.name,
                    "key_filename": key.name,
                    "kind": "generated",
                    "has_private_key": True,
                    "key_sha256": self._fingerprint(key),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **self._certificate_info(cert),
                }
                item["certificates"].append(entry)
                self._save(root, item)
                return entry
            except Exception:
                key.unlink(missing_ok=True)
                cert.unlink(missing_ok=True)
                raise
            finally:
                csr.unlink(missing_ok=True)
                ext.unlink(missing_ok=True)

    def import_certificate(self, bundle_id: str, name: str, content: str,
                           filename: str = "") -> dict:
        name = self._clean_name(name)
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 256 * 1024 or b"\0" in encoded:
            raise BackendError("certificate must contain 1 to 262144 UTF-8 bytes without NUL")
        with self.lock:
            item = self.get(bundle_id)
            root = self.root / bundle_id
            certificate_id = str(uuid.uuid4())
            filename = filename.strip() or f"cert-{certificate_id}.pem"
            if (not SAFE_CERT_FILE_RE.fullmatch(filename)
                    or filename.lower() in {"ca-cert.pem", "ca-key.pem"}):
                raise BackendError("certificate filename is invalid or reserved")
            cert = root / "certs" / filename
            if cert.exists():
                raise BackendError("certificate filename already exists in this bundle")
            try:
                cert.write_text(content, encoding="utf-8")
                os.chmod(cert, 0o644)
                info = self._certificate_info(cert)
                entry = {
                    "certificate_id": certificate_id,
                    "name": name,
                    "filename": cert.name,
                    "kind": "imported",
                    "has_private_key": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **info,
                }
                item["certificates"].append(entry)
                self._save(root, item)
                return entry
            except Exception:
                cert.unlink(missing_ok=True)
                raise

    def read_ca_certificate(self, bundle_id: str) -> str:
        return (self.resolve(bundle_id) / "ca-cert.pem").read_text(encoding="utf-8")

    def delete(self, bundle_id: str) -> dict | None:
        with self.lock:
            try:
                item = self.get(bundle_id)
            except BackendError:
                return None
            shutil.rmtree(self.root / bundle_id)
            return item
