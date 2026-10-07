import unittest
from runner.restart_policy import flags, policy, properties
from runner.systemd import BackendError


class RestartPolicyTests(unittest.TestCase):
    def test_all_combinations(self):
        for exit_, failure, expected in [(False, False, 'no'), (True, False, 'on-success'),
                                         (False, True, 'on-failure'), (True, True, 'always')]:
            value = {'on_exit': exit_, 'on_failure': failure}
            self.assertEqual(expected, policy(value))
            self.assertIn('--property=Restart=' + expected, properties(value))
            self.assertEqual(expected != 'no', '--property=StartLimitBurst=5' in properties(value))

    def test_legacy_and_validation(self):
        self.assertEqual('on-failure', policy(True))
        self.assertEqual('no', policy(False))
        for value in ('always', 1, {'on_exit': 'yes'}, {'typo': True}):
            with self.assertRaises(BackendError):
                flags(value)
