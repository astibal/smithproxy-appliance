import tempfile
import unittest
from pathlib import Path
from runner.runtime_profiles import RuntimeProfileLibrary
from runner.systemd import BackendError


class ProgramProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = RuntimeProfileLibrary(Path(self.tmp.name) / 'profiles.json')

    def test_roundtrip_and_work_files(self):
        item = self.library.save_program({'application': 'webfsd', 'name': 'Lab web'})
        self.assertEqual({'port': 8000}, item['program_settings'])
        self.library.put_work_file(item['profile_id'], 'index.html', b'Hello', 0o644)
        updated = self.library.save_program({**item, 'name': 'New name', 'program_settings': {'port': 8080}}, item['profile_id'])
        self.assertEqual(8080, updated['program_settings']['port'])
        self.assertEqual(1, len(self.library.list_work_files(item['profile_id'])))
        self.assertEqual(updated, self.library.get(item['profile_id']))

    def test_validation(self):
        for extra in ({'application': 'shell'}, {'filesystem_mode': 'host'},
                      {'program_settings': {'port': True}}, {'program_settings': {'port': 0}},
                      {'program_settings': {'command': 'something'}}, {'build_id': 'some-build'},
                      {'ttl_seconds': -1}):
            with self.subTest(extra=extra), self.assertRaises(BackendError):
                self.library.save_program({'application': 'webfsd', 'name': 'web', **extra})

    def test_router_and_type_immutable(self):
        item = self.library.save_program({'application': 'router', 'name': 'Router', 'ttl_seconds': None})
        self.assertEqual('rootfs', item['filesystem_mode'])
        with self.assertRaises(BackendError):
            self.library.save_program({'application': 'webfsd', 'name': 'changed'}, item['profile_id'])

    def test_smithproxy_default(self):
        item = self.library.create('Smithproxy', 'build', 'config')
        self.assertEqual('smithproxy', self.library.get(item['profile_id'])['application'])
