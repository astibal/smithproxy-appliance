"""Immutable runtime images. Copy ELF dependencies without executing inputs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from .builder import SmithproxyBuilder
from .systemd import BackendError

VARIANTS = {
    'barebone': (),
    'utils': ('sh', 'cat', 'ls', 'cp', 'mv', 'mkdir', 'rm', 'sleep'),
    'network': ('sh', 'cat', 'ls', 'ip', 'ss', 'ping', 'nft', 'iptables', 'ip6tables'),
}

IMPORT_LIMIT = 128 * 1024 * 1024
EXTRACT_LIMIT = 1024 * 1024 * 1024
ENTRY_LIMIT = 100000


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


def prepare(library: Path, selected='barebone', *, application='', settings=None, base=None, executable=None, executable_name='program'):
    """Same image contract for built-in programs and an existing Smithproxy base."""
    selected = variant(selected)
    if application == 'elf':
        import re
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', executable_name):
            raise BackendError('invalid ELF executable name')
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
            tool = {'router': 'sleep', 'webfsd': 'webfsd', 'elf': 'sas-program'}.get(application)
            source = str(executable) if application == 'elf' and executable else (shutil.which(tool) if tool else None)
            imported = library.parent / 'program-sources' / (tool or 'unknown')
            if not source and imported.is_file():
                source = str(imported)
            if not source:
                raise BackendError(f'required program is not installed on origin: {tool or application}')
            destination = Path('/opt/program') / executable_name if application == 'elf' else Path('/usr/bin') / tool
            copy_elf(Path(source), root, destination)
            argv = ['/usr/bin/sleep', 'infinity'] if application == 'router' else [
                '/usr/bin/webfsd', '-F', '-r', '/work', '-p', str(settings.get('port', 8000)), '-u', 'root', '-g', 'root', '-f', 'index.html', '-l', '-']
            if application == 'elf':
                argv = [str(destination), *settings.get('argv', [])]
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


def _metadata_root(library: Path) -> Path:
    return library.parent / 'runtime-image-metadata'


def _image_id(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise BackendError('invalid rootfs image ID')
    return value


def _contract(root: Path) -> dict:
    try:
        value = json.loads((root / 'runtime-image.json').read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise BackendError('rootfs archive requires a valid runtime-image.json') from exc
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise BackendError('unsupported rootfs image schema')
    argv = value.get('argv')
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(arg, str) or '\0' in arg for arg in argv) or
            not argv[0].startswith('/')):
        raise BackendError('rootfs image argv must start with an absolute executable')
    executable = root / argv[0].lstrip('/')
    if not executable.is_file() or executable.is_symlink() or not executable.stat().st_mode & stat.S_IXUSR:
        raise BackendError('rootfs image executable is missing or not executable')
    if not (root / 'program.cfg').is_file() or (root / 'program.cfg').is_symlink():
        raise BackendError('rootfs image program.cfg sentinel is missing')
    files, size = {}, 0
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if relative == 'runtime-image.json' or (relative == 'program.cfg' and relative not in value.get('files', {})):
            continue
        if path.is_symlink():
            files[relative] = 'link:' + os.readlink(path)
        elif path.is_file():
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            size += path.stat().st_size
    declared = value.get('files')
    # Older SAS images include program.cfg and every payload file in this map.
    if not isinstance(declared, dict) or declared != files:
        raise BackendError('rootfs image file manifest does not match archive contents')
    canonical = {key: item for key, item in value.items() if key != 'id'}
    canonical.update(files=files, size_bytes=size)
    return canonical


def _extract_archive(content: bytes, root: Path) -> None:
    if len(content) > IMPORT_LIMIT:
        raise BackendError('rootfs archive exceeds 128 MiB')
    try:
        archive = tarfile.open(fileobj=BytesIO(content), mode='r:*')
    except (tarfile.TarError, OSError) as exc:
        raise BackendError('invalid rootfs tar archive') from exc
    total = 0
    links = []
    with archive:
        members = archive.getmembers()
        if len(members) > ENTRY_LIMIT:
            raise BackendError('rootfs archive contains too many entries')
        for member in members:
            name = member.name.removeprefix('./')
            path = Path(name)
            if not name or path.is_absolute() or '..' in path.parts:
                raise BackendError('rootfs archive contains an unsafe path')
            target = root / path
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                total += member.size
                if total > EXTRACT_LIMIT:
                    raise BackendError('rootfs archive expands beyond 1 GiB')
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise BackendError('rootfs archive member cannot be read')
                with target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            elif member.issym():
                links.append((target, member.linkname))
            else:
                raise BackendError('rootfs archive contains an unsupported special file')
        for target, linkname in links:
            if '\0' in linkname:
                raise BackendError('rootfs archive contains an unsafe link')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(linkname)


def import_bytes(library: Path, content: bytes, name: str, version: str = '', origin='upload') -> dict:
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise BackendError('rootfs image name is required (maximum 128 characters)')
    if not isinstance(version, str) or len(version) > 128:
        raise BackendError('rootfs image version must be at most 128 characters')
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='.import-', dir=library))
    try:
        _extract_archive(content, root)
        contract = _contract(root)
        digest = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        (root / 'runtime-image.json').write_text(json.dumps({**contract, 'id': digest}))
        target = library / digest
        if not target.exists():
            root.rename(target)
        metadata = _metadata_root(library)
        metadata.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = dict(image_id=digest, name=name.strip(), version=version,
                      origin=origin, imported=True,
                      created_at=datetime.now(timezone.utc).isoformat(),
                      application=contract.get('application', ''), argv=contract['argv'],
                      size_bytes=contract['size_bytes'])
        path = metadata / f'{digest}.json'
        if not path.exists():
            path.write_text(json.dumps(record))
            path.chmod(0o600)
        return json.loads(path.read_text())
    finally:
        if root.exists() and root.name.startswith('.import-'):
            shutil.rmtree(root)


def import_file(library: Path, source, name: str, version: str = '') -> dict:
    path = Path(source)
    if not path.is_absolute():
        raise BackendError('origin path must be absolute')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise BackendError('origin path must be a regular file')
        content = stream.read(IMPORT_LIMIT + 1)
    return import_bytes(library, content, name, version, str(path))


def imported(library: Path) -> list[dict]:
    metadata = _metadata_root(library)
    if not metadata.is_dir():
        return []
    result = []
    for path in metadata.glob('*.json'):
        try:
            item = json.loads(path.read_text())
            image_id = _image_id(item.get('image_id'))
            root = library / image_id
            if root.is_dir():
                _contract(root)
                result.append(item)
        except (BackendError, OSError, json.JSONDecodeError):
            continue
    return sorted(result, key=lambda item: item.get('created_at', ''), reverse=True)


def resolve_imported(library: Path, image_id: str) -> tuple[Path, dict]:
    image_id = _image_id(image_id)
    item = next((item for item in imported(library) if item['image_id'] == image_id), None)
    if not item:
        raise BackendError('imported rootfs image is unavailable')
    return library / image_id, json.loads((library / image_id / 'runtime-image.json').read_text())
