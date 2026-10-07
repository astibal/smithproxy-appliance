import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from runner.app import Manager
from sas_client.cli import build_parser, dispatch

ID = '11111111-1111-4111-8111-111111111111'


class InstanceLocationTests(unittest.TestCase):
    def test_lookup_and_invalid_id(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(runtime_root=Path(directory), peek=Mock(return_value=SimpleNamespace(
                id=ID, namespace='', state='stopped', unit='example.service', slice_unit='example.slice')))
            (Path(directory) / ID).mkdir()
            with patch('runner.app.socket.gethostname', return_value='runner-one'):
                result = Manager.location(manager, ID)
            self.assertEqual('runner-one', result['origin'])
            self.assertEqual(str(Path(directory) / ID), result['work_dir'])
            self.assertTrue(result['work_dir_exists'])
            self.assertFalse(result['namespace_exists'])
            self.assertEqual(3, result['microservice_contract_version'])
            self.assertEqual('installation_only', result['microservice_mode'])
            self.assertFalse(result['microservice_execution_enabled'])
            self.assertFalse(result['microservice_capabilities']['supervision'])
            self.assertEqual(str(Path(directory).parent / 'microservices' / ID), result['microservices_dir'])
            manager.peek.reset_mock()
            self.assertIsNone(Manager.location(manager, '../invalid'))
            manager.peek.assert_not_called()
            manager.peek.return_value = None
            self.assertIsNone(Manager.location(manager, ID))

    def test_cli_queries_exact_id_and_returns_plain_field(self):
        client = Mock()
        client.get.return_value = {'work_dir': '/host/work', 'work_dir_exists': True}
        args = build_parser().parse_args(['instance', 'location', ID, '--work-dir'])
        self.assertEqual(('/host/work', None), dispatch(client, args))
        client.get.assert_called_once_with(f'/v1/instances/{ID}/location')
        client.get.return_value['work_dir_exists'] = False
        with self.assertRaises(ValueError):
            dispatch(client, args)

    def test_origin_comes_from_runner_not_client(self):
        client = Mock()
        client.get.return_value = {'origin': 'remote-runner'}
        args = build_parser().parse_args(['instance', 'location', ID, '--origin'])
        self.assertEqual(('remote-runner', None), dispatch(client, args))

    def test_microservices_selector_requires_provisioned_directory(self):
        client = Mock()
        client.get.return_value = {'microservices_dir': '/host/microservices/' + ID,
                                   'microservices_dir_exists': True}
        args = build_parser().parse_args(['instance', 'location', ID, '--microservices-dir'])
        self.assertEqual(('/host/microservices/' + ID, None), dispatch(client, args))
        client.get.return_value['microservices_dir_exists'] = False
        with self.assertRaises(ValueError):
            dispatch(client, args)
