"""Immutable ELF imports; never execute an imported binary during inspection."""
import hashlib
import json
import os
import re
import stat
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .systemd import BackendError

LIMIT = 16 * 1024 * 1024


class ProgramArtifacts:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(self, artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch('[0-9a-f]{64}', artifact_id):
            raise BackendError('invalid ELF artifact ID')
        try:
            return json.loads((self.root / artifact_id / 'metadata.json').read_text())
        except FileNotFoundError:
            raise BackendError('ELF artifact not found') from None

    def list(self):
        return sorted([self.get(p.name) for p in self.root.iterdir()
                       if re.fullmatch('[0-9a-f]{64}', p.name)],
                      key=lambda item: item['created_at'], reverse=True)

    def binary(self, artifact_id):
        self.get(artifact_id)
        path = self.root / artifact_id / 'program'
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact_id:
            raise BackendError('ELF artifact integrity check failed')
        return path

    def import_file(self, path, name, version=''):
        source = Path(path)
        if not source.is_absolute():
            raise BackendError('origin path must be absolute')
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BackendError('origin path must be a regular file')
            content = stream.read(LIMIT + 1)
        return self.import_bytes(content, name, version, str(source), source.name)

    def import_bytes(self, content, name, version='', origin='upload', filename='program'):
        if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', filename):
            raise BackendError('invalid ELF filename')
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise BackendError('artifact name is required (maximum 128 characters)')
        if not isinstance(version, str) or len(version) > 128:
            raise BackendError('version must be a string up to 128 characters')
        if len(content) > LIMIT or len(content) < 64 or content[:4] != b'\x7fELF':
            raise BackendError('expected ELF executable, maximum 16 MiB')
        if content[4] not in (1, 2) or content[5] not in (1, 2):
            raise BackendError('unsupported ELF encoding')
        kind, machine = struct.unpack(('<' if content[5] == 1 else '>') + 'HH', content[16:20])
        with open('/proc/self/exe', 'rb') as stream:
            host = stream.read(64)
        if content[4:6] != host[4:6] or content[18:20] != host[18:20]:
            raise BackendError('ELF architecture does not match this origin')
        if kind not in (2, 3):
            raise BackendError('ELF must be executable or PIE')
        entry_size = 4 if content[4] == 1 else 8
        if not int.from_bytes(content[24:24 + entry_size], 'little' if content[5] == 1 else 'big'):
            raise BackendError('ELF has no executable entry point')
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / digest
        if target.exists():
            return self.get(digest)
        import shutil
        temp = Path(tempfile.mkdtemp(prefix='.import-', dir=self.root))
        try:
            binary = temp / 'program'
            binary.write_bytes(content)
            binary.chmod(0o500)
            # readelf parses metadata only; no ldd or execution of the input.
            import subprocess
            check = subprocess.run(['readelf', '-hW', str(binary)], capture_output=True, timeout=10)
            if check.returncode:
                raise BackendError('invalid ELF header')
            item = dict(artifact_id=digest, sha256=digest, name=name.strip(), version=version,
                        size_bytes=len(content), machine=machine, bits=32 if content[4] == 1 else 64,
                        origin=origin, filename=filename, created_at=datetime.now(timezone.utc).isoformat())
            (temp / 'metadata.json').write_text(json.dumps(item))
            try:
                temp.rename(target)
            except OSError:
                if not target.exists():
                    raise
            return self.get(digest)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
