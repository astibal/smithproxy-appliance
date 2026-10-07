"""Immutable runtime images. Copy ELF dependencies without executing inputs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .builder import SmithproxyBuilder
from .systemd import BackendError

VARIANTS = {
    'barebone': (),
    'utils': ('sh', 'cat', 'ls', 'cp', 'mv', 'mkdir', 'rm', 'sleep'),
    'network': ('sh', 'cat', 'ls', 'ip', 'ss', 'ping', 'nft', 'iptables', 'ip6tables'),
}


def variant(value):
    if not isinstance(value, str) or value not in VARIANTS:
        raise BackendError('rootfs_variant must be barebone, utils or network')
    return value


def copy_elf(source: Path, root: Path, destination: Path):
    source = source.resolve(strict=True)
    with source.open('rb') as stream:
        magic = stream.read(4)
    if not source.is_file() or magic != b'\x7fELF':
        raise BackendError(f'ELF executable required: {source}')
    SmithproxyBuilder._copy_rootfs_file(source, root, destination)
    dependencies, loader = SmithproxyBuilder._resolve_elf_closure(source)
    for dependency in dependencies | ({loader} if loader else set()):
        SmithproxyBuilder._copy_rootfs_file(dependency, root)


def prepare(library: Path, selected='barebone', *, application='', settings=None, base=None):
    """Same image contract for built-in programs and an existing Smithproxy base."""
    selected = variant(selected)
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='.building-', dir=library))
    settings = settings or {}
    try:
        if base:
            shutil.copytree(base, root, dirs_exist_ok=True, symlinks=True)
        for name in ('usr/bin', 'bin', 'etc', 'work', 'logs', 'run', 'tmp', 'dev', 'proc', 'sys'):
            (root / name).mkdir(parents=True, exist_ok=True)
        argv = []
        if application:
            tool = {'router': 'sleep', 'webfsd': 'webfsd'}.get(application)
            source = shutil.which(tool) if tool else None
            imported = library.parent / 'program-sources' / (tool or 'unknown')
            if not source and imported.is_file():
                source = str(imported)
            if not source:
                raise BackendError(f'required program is not installed on origin: {tool or application}')
            copy_elf(Path(source), root, Path('/usr/bin') / tool)
            argv = ['/usr/bin/sleep', 'infinity'] if application == 'router' else [
                '/usr/bin/webfsd', '-F', '-r', '/work', '-p', str(settings.get('port', 8000)), '-u', 'root', '-g', 'root', '-f', 'index.html', '-l', '-']
            if application == 'webfsd' and Path('/etc/mime.types').is_file():
                shutil.copyfile('/etc/mime.types', root / 'etc/mime.types')
            (root / 'etc/passwd').write_text('root:x:0:0:root:/work:/bin/sh\n')
            (root / 'etc/group').write_text('root:x:0:\n')
        for tool in VARIANTS[selected]:
            source = shutil.which(tool)
            if not source:
                raise BackendError(f'rootfs variant {selected} requires host tool: {tool}')
            copy_elf(Path(source), root, Path('/usr/bin') / tool)
            if tool == 'sh' and not (root / 'bin/sh').exists():
                (root / 'bin/sh').symlink_to('../usr/bin/sh')
        # xtables extensions are dlopen modules, not ELF DT_NEEDED dependencies.
        if selected == 'network':
            for directory in Path('/usr/lib').glob('*/xtables'):
                for module in directory.glob('*.so'):
                    copy_elf(module, root, module)
        files = {}
        size = 0
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                files[str(path.relative_to(root))] = 'link:' + os.readlink(path)
            elif path.is_file():
                files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
                size += path.stat().st_size
        contract = dict(schema=1, variant=selected, application=application,
                        argv=argv, files=files, size_bytes=size)
        digest = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        (root / 'runtime-image.json').write_text(json.dumps({**contract, 'id': digest}))
        # The manager keeps its deployment sentinel under the historical config filename.
        (root / 'program.cfg').write_text('# SAS program deployment\n')
        target = library / digest
        if not target.exists():
            try:
                root.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        return target
    finally:
        if root.exists() and root.name.startswith('.building-'):
            shutil.rmtree(root)
