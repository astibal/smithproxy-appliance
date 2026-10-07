"""Versioned, content-addressed auxiliary-service rootfs (no workload ELF execution)."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from .builder import SmithproxyBuilder
from .deployments import atomic_json

TOOLS = ('sh', 'mv', 'mkdir', 'rm', 'sleep', 'cat', 'chmod', 'ip', 'flock',
         'stat', 'date', 'readlink', 'basename', 'dirname', 'id', 'touch',
         'mktemp', 'env', 'timeout', 'kill', 'grep', 'sed', 'awk', 'head', 'tail')


def prepare(library: Path) -> Path:
    """Snapshot trusted host tools and the C/C++ ABI; never replace an image."""
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix='.building-', dir=library))
    temporary.chmod(0o755)
    try:
        for directory in ('bin', 'usr/bin', 'etc', 'dev/net', 'proc', 'sys', 'tmp',
                          'run/sas-microservice', 'run/sas', 'microservices'):
            (temporary / directory).mkdir(parents=True, exist_ok=True)
        tools = {}
        for name in TOOLS:
            source = shutil.which(name)
            if not source:
                raise ValueError(f'microservice rootfs requires host tool: {name}')
            source = Path(source)
            tools[name] = str(source.resolve())
            SmithproxyBuilder._copy_rootfs_file(source, temporary, Path('/bin') / name)
            dependencies, loader = SmithproxyBuilder._resolve_elf_closure(source.resolve())
            for dependency in dependencies | ({loader} if loader else set()):
                SmithproxyBuilder._copy_rootfs_file(dependency, temporary)
        cache = SmithproxyBuilder._library_cache()
        for name in ('libstdc++.so.6', 'libgcc_s.so.1', 'libm.so.6', 'libc.so.6'):
            candidates = cache.get(name, [])
            source = next((p for p in candidates if p.is_file()), None)
            if source is None:
                raise ValueError(f'microservice rootfs requires {name}')
            dependencies, _ = SmithproxyBuilder._resolve_elf_closure(source)
            for dependency in dependencies | {source}:
                SmithproxyBuilder._copy_rootfs_file(dependency, temporary)
        for name in ('nsswitch.conf', 'hosts', 'resolv.conf', 'protocols', 'services', 'os-release'):
            source = Path('/etc') / name
            if source.is_file():
                SmithproxyBuilder._copy_rootfs_file(source, temporary)
        (temporary / 'etc/passwd').write_text(
            'root:x:0:0:root:/:/bin/sh\ntuntom:x:65532:65532:tuntom:/run/sas-microservice:/bin/sh\n')
        (temporary / 'etc/group').write_text('root:x:0:\ntuntom:x:65532:\n')
        hashes = {}
        for path in sorted(temporary.rglob('*')):
            if path.is_file():
                hashes[str(path.relative_to(temporary))] = hashlib.sha256(path.read_bytes()).hexdigest()
                path.chmod(0o555 if os.access(path, os.X_OK) else 0o444)
        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        manifest = {
            'schema': 1, 'contract_version': 3, 'sha256': identity,
            'architecture': platform.machine(), 'files': hashes, 'tools': tools,
            'runtime_uid': 65532, 'runtime_gid': 65532, 'shell': tools['sh'],
            'libc': subprocess.run(['getconf', 'GNU_LIBC_VERSION'], text=True,
                                    capture_output=True, check=True).stdout.strip(),
        }
        manifest['abi_versions'] = {}
        for name in ('libc.so.6', 'libstdc++.so.6'):
            source = next(p for p in cache[name] if p.is_file())
            output = subprocess.run(['readelf', '--version-info', str(source)],
                                    capture_output=True, text=True, check=True).stdout
            import re
            manifest['abi_versions'][name] = sorted(set(re.findall(r'Name: ((?:GLIBC|GLIBCXX|CXXABI)_[0-9.]+)', output)))
        atomic_json(temporary / 'rootfs.json', manifest)
        target = library / identity
        if target.exists():
            return target
        temporary.rename(target)
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('library', type=Path)
    print(prepare(parser.parse_args().library))
