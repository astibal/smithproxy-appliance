import json
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import runtime_images, program_runtime
from runner.systemd import BackendError


class RuntimeImages(unittest.TestCase):
    @staticmethod
    def archive(root):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for path in root.rglob('*'):
                archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
        return output.getvalue()

    def test_real_router_image_and_immutable_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            bare = runtime_images.prepare(library, application='router')
            self.assertTrue((bare / 'usr/bin/sleep').is_file())
            self.assertFalse((bare / 'bin/sh').exists())
            self.assertFalse((bare / 'usr/share/doc').exists())
            self.assertEqual(bare, runtime_images.prepare(library, application='router'))
            utils = runtime_images.prepare(library, 'utils', application='router')
            self.assertNotEqual(bare, utils)
            self.assertTrue((utils / 'bin/sh').exists())
            self.assertFalse((bare / 'bin/sh').exists())
            self.assertEqual(['/usr/bin/sleep', 'infinity'], json.loads((bare / 'runtime-image.json').read_text())['argv'])

    def test_missing_program_is_not_a_host_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime_images.shutil, 'which', return_value=None):
            with self.assertRaises(BackendError):
                runtime_images.prepare(Path(directory), application='webfsd')
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_import_portable_rootfs_and_resolve_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = runtime_images.prepare(root / 'source', application='router')
            item = runtime_images.import_bytes(root / 'target', self.archive(source), 'Router', 'v1')
            self.assertEqual('Router', item['name'])
            self.assertEqual([item], runtime_images.imported(root / 'target'))
            image, contract = runtime_images.resolve_imported(root / 'target', item['image_id'])
            self.assertTrue((image / 'usr/bin/sleep').is_file())
            self.assertEqual(['/usr/bin/sleep', 'infinity'], contract['argv'])

    def test_import_rejects_traversal_and_special_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w') as archive:
                member = tarfile.TarInfo('../escape')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))
            with self.assertRaises(BackendError):
                runtime_images.import_bytes(Path(directory), output.getvalue(), 'unsafe')

    def test_launch_is_rootfs_only_and_forwarding_is_namespaced(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            (root / 'usr/bin').mkdir(parents=True)
            (root / 'usr/bin/sleep').touch()
            work = Path(directory) / 'work'
            work.mkdir()
            commands = []
            backend = SimpleNamespace(_run=commands.append, unit_name=lambda _: 'test.service', slice_name=lambda _: 'test.slice')
            program_runtime.launch(backend, 'id', SimpleNamespace(namespace='cz-owned'), work, root,
                                   {'application': 'router', 'argv': ['/usr/bin/sleep', 'infinity']}, False, 30)
            self.assertEqual(['ip', 'netns', 'exec', 'cz-owned'], commands[0][:4])
            self.assertIn(f'--property=RootDirectory={root}', commands[1])
            self.assertIn('--property=CapabilityBoundingSet=', commands[1])
            self.assertIn('--property=ReadWritePaths=/work /logs', commands[1])
