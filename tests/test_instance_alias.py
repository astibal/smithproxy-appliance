import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from runner.app import Instance, Manager
from runner.config import ConfigError
from sas_client.cli import build_parser, dispatch

ID = '11111111-1111-4111-8111-111111111111'
SECOND = '22222222-2222-4222-8222-222222222222'


class AliasTests(unittest.TestCase):
    def test_identity_persistence_collision_and_clear(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manager = Manager(root / 'state', root / 'instances', root / 'template', Mock())
            for identifier in (ID, SECOND):
                manager._save(Instance(identifier, 'unit', 'stopped', '', '', 0))
            manager.set_alias(ID, 'tatka-smoula')
            self.assertEqual(ID, manager.location('tatka-smoula')['id'])
            self.assertEqual('tatka-smoula', manager.location(ID)['alias'])
            restarted = Manager(root / 'state', root / 'instances', root / 'template', Mock())
            self.assertEqual(ID, restarted.resolve_alias('tatka-smoula'))
            with self.assertRaises(ConfigError):
                manager.set_alias(SECOND, 'tatka-smoula')
            for invalid in ('../escape', 'Name', 'two words', ID, 'x' * 64, None):
                with self.assertRaises(ConfigError):
                    manager.set_alias(ID, invalid)
            manager.set_alias(ID, 'new-name')
            self.assertIsNone(manager.location('tatka-smoula'))
            manager.set_alias(ID, '')
            self.assertIsNone(manager.location('new-name'))
            self.assertEqual(ID, manager.location(ID)['id'])

    def test_cli_resolves_alias_to_uuid_before_mutation(self):
        client = Mock()
        client.get.return_value = {'instances': [{'id': ID, 'alias': 'tatka-smoula'}]}
        args = build_parser().parse_args(['instance', 'alias', 'tatka-smoula', 'new-name'])
        dispatch(client, args)
        client.post.assert_called_once_with(f'/v1/instances/{ID}/alias', {'alias': 'new-name'})
