import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from runner.netns_shell import open_shell, NetnsTransport
from runner.systemd import BackendError


class NetnsShellTests(unittest.TestCase):
    def test_rejects_other_namespace(self):
        with self.assertRaises(BackendError):
            open_shell('12345678-1234-4234-8234-123456789abc', 'host')

    def test_missing_namespace_never_falls_back_to_host(self):
        with patch('runner.netns_shell.os.open', side_effect=FileNotFoundError), \
                patch('runner.netns_shell.subprocess.Popen') as process:
            with self.assertRaises(BackendError):
                open_shell('12345678-1234-4234-8234-123456789abc', 'cz-12345678')
            process.assert_not_called()

    def test_clean_environment_and_pinned_namespace(self):
        with tempfile.TemporaryFile() as namespace:
            pinned = os.dup(namespace.fileno())
            with patch('runner.netns_shell.os.open', return_value=pinned), \
                    patch('runner.netns_shell.subprocess.Popen') as process, \
                    patch.dict(os.environ, {'CZ_RUNNER_TOKEN': 'never-forward'}):
                process.return_value.poll.return_value = 0
                transport = open_shell('12345678-1234-4234-8234-123456789abc', 'cz-12345678')
                args, kwargs = process.call_args
                self.assertNotIn('CZ_RUNNER_TOKEN', kwargs['env'])
                self.assertEqual((pinned,), kwargs['pass_fds'])
                self.assertTrue(kwargs['start_new_session'])
                self.assertIn('runner.netns_shell', args[0])
                transport.close()

    def test_resize_rejects_invalid_dimensions(self):
        transport = NetnsTransport(Mock(), -1)
        for cols, rows in ((0, 24), (100, 9999), ('80', 24)):
            with self.assertRaises(ValueError):
                transport.resize(cols, rows)
