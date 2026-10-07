import json
import shutil
import tempfile
import unittest
from pathlib import Path
from runner.program_artifacts import ProgramArtifacts
from runner.runtime_images import prepare
from runner.runtime_profiles import RuntimeProfileLibrary, elf_settings
from runner.systemd import BackendError


class ProgramArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProgramArtifacts(self.root / 'artifacts')
        self.content = Path(shutil.which('sleep')).read_bytes()

    def test_import_is_immutable_and_deduplicated(self):
        first = self.store.import_bytes(self.content, 'Sleep', 'v1')
        second = self.store.import_bytes(self.content, 'Different label', 'v2')
        self.assertEqual(first, second)
        self.assertEqual([first], self.store.list())
        self.assertEqual(self.content, self.store.binary(first['artifact_id']).read_bytes())

    def test_invalid_inputs_and_paths(self):
        for content in (b'#!/bin/sh\necho hello', b'\x7fELF' + bytes(80)):
            with self.assertRaises(BackendError):
                self.store.import_bytes(content, 'bad')
        with self.assertRaises(BackendError):
            self.store.get('../../etc/passwd')
        with self.assertRaises(BackendError):
            self.store.import_file('relative', 'bad')

    def test_wrong_architecture(self):
        content = bytearray(self.content)
        content[18:20] = b'\xff\xff'
        with self.assertRaises(BackendError):
            self.store.import_bytes(bytes(content), 'wrong')

    def test_integrity(self):
        item = self.store.import_bytes(self.content, 'sleep')
        path = self.store.binary(item['artifact_id'])
        path.chmod(0o600)
        path.write_bytes(b'changed')
        with self.assertRaises(BackendError):
            self.store.binary(item['artifact_id'])

    def test_profile_and_rootfs(self):
        item = self.store.import_bytes(self.content, 'sleep')
        settings = {'artifact_id': item['artifact_id'], 'argv': ['300']}
        root = prepare(self.root / 'images', application='elf', settings=settings,
                       executable=self.store.binary(item['artifact_id']))
        self.assertEqual(['/opt/program/program', '300'], json.loads((root / 'runtime-image.json').read_text())['argv'])
        library = RuntimeProfileLibrary(self.root / 'profiles.json')
        profile = library.save_program({'application': 'elf', 'name': 'test',
                                      'program_settings': settings}, image_id=root.name)
        self.assertEqual(settings, profile['program_settings'])
        self.assertIsNone(profile['ttl_seconds'])

    def test_argument_validation(self):
        for args in ('not-an-array', ['x\0y'], [None]):
            with self.assertRaises(BackendError):
                elf_settings({'artifact_id': 'a' * 64, 'argv': args})
