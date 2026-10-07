"""Unit tests only: no systemd units, namespaces or installed programs started."""
import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from runner.microservices import Microservices, ServiceError, SystemdServices, file_lock, registry, read_file

ID = '11111111-1111-4111-8111-111111111111'


class Backend:
    manifest = {'contract_version': 3}
    def __init__(self):
        self.live = set()
        self.starts = []
    group_path = staticmethod(SystemdServices.group_path)
    def eligible(self, instance):
        return 99999999
    def untracked(self, *args):
        return False
    def start(self, record, *args):
        self.starts.append(record.copy())
        self.live.add(record['run_id'])
    def inspect(self, record):
        return {'empty': record['run_id'] not in self.live, 'pid': 0}
    def stop(self, record):
        self.live.discard(record['run_id'])


class MicroserviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.instance = SimpleNamespace(id=ID, unit='proxy.service', slice_unit='sas-test.slice',
            state='running', desired_state='running', deadline='')
        self.manager = SimpleNamespace(runtime_root=self.base / 'instances',
            lock=threading.RLock(), peek=lambda value: self.instance if value == ID else None,
            snapshot=lambda: [self.instance], location=lambda _: {'namespace_path': '/run/netns/test'})
        # Test fixtures live below world-writable /tmp, unlike production state.
        self.trust = patch('runner.microservices.trusted_directory')
        self.trust.start()
        self.addCleanup(self.trust.stop)
        self.chown = patch('runner.microservices.os.chown')
        self.chown.start()
        self.addCleanup(self.chown.stop)
        self.backend = Backend()
        self.service = Microservices(self.manager, self.backend)
        self.install = self.service.provision(ID)
        (self.install / '10-start.sh').write_text('#!/bin/sh\n')
        (self.install / '10-start.sh').chmod(0o600)

    def register(self, text='10 tuntom 232 # metadata\n'):
        (self.install / 'db.info').write_text(text)
        (self.install / 'db.info').chmod(0o600)

    def test_explicit_check_is_per_instance_and_obeys_installer_lock(self):
        self.register()
        self.manager.snapshot = Mock(side_effect=AssertionError('no global scan'))
        with file_lock(self.install / '10.lock'):
            result = self.service.check_instance(ID)
            self.assertEqual(result['state'], 'deferred')
            self.assertEqual(result['blocked'], ['10'])
        result = self.service.check_instance(ID)
        self.assertEqual(result['state'], 'checked')
        self.assertEqual(len(self.backend.starts), 1)
        self.service.check_instance(ID)
        self.assertEqual(len(self.backend.starts), 1)
        with self.assertRaises(ServiceError):
            self.service.check_instance('22222222-2222-4222-8222-222222222222')

    def test_reserved_service_runs_before_external_services(self):
        self.register()
        self.manager.system_start = Mock()
        self.manager.system_start.check.side_effect = ServiceError('network_not_ready')
        with self.assertRaises(ServiceError):
            self.service.check_instance(ID)
        self.assertEqual(self.backend.starts, [])

    def start(self):
        self.register()
        self.service.scan()
        self.assertEqual(1, len(self.backend.starts))
        return self.backend.starts[0]

    def test_only_registered_services_start_once_and_survive_runner_restart(self):
        self.service.scan()
        self.assertEqual([], self.backend.starts)
        record = self.start()
        restarted = Microservices(self.manager, self.backend)
        restarted.scan()
        self.assertEqual(1, len(self.backend.starts))
        self.assertEqual(record['run_id'], restarted.status(ID, '10')['run_id'])

    def test_fabric_lock_skips_start(self):
        self.register()
        with file_lock(self.install / '10.lock'):
            self.service.scan()
            self.assertEqual([], self.backend.starts)
        self.service.scan()
        self.assertEqual(1, len(self.backend.starts))

    def test_exact_stop_works_with_fabric_locks_and_is_idempotent(self):
        record = self.start()
        with self.assertRaises(ServiceError) as failure:
            self.service.stop(ID, '10', 'wrong', record['run_id'])
        self.assertEqual('identity_conflict', failure.exception.error)
        with self.assertRaises(ServiceError) as failure:
            self.service.stop(ID, '10', 'tuntom', record['run_id'])
        self.assertEqual('registration_present', failure.exception.error)
        with file_lock(self.install / 'db.lock'), file_lock(self.install / '10.lock'):
            self.register('')
            self.assertEqual(record['run_id'], self.service.status(ID, '10')['run_id'])
            self.assertEqual('stopped', self.service.stop(ID, '10', 'tuntom', record['run_id'])['result'])
            self.assertEqual('already_stopped', self.service.stop(ID, '10', 'tuntom', record['run_id'])['result'])

    def test_old_stop_does_not_touch_new_run(self):
        first = self.start()
        self.register('')
        self.service.stop(ID, '10', 'tuntom', first['run_id'])
        old = self.service.runs(ID, '10')[0]
        old['created_epoch'] -= 4000
        self.service.save(old)
        self.register()
        self.service.scan()
        second = self.backend.starts[-1]
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual('already_stopped', self.service.stop(ID, '10', 'tuntom', first['run_id'])['result'])
        self.assertIn(second['run_id'], self.backend.live)

    def test_backoff_and_lifetime(self):
        record = self.start()
        self.backend.live.clear()
        self.service.scan()
        self.assertEqual(1, len(self.backend.starts))
        self.instance.desired_state = 'stopped'
        self.service.scan()
        self.assertEqual(1, len(self.backend.starts))
        self.assertEqual('stopped', self.service.status(ID, '10')['state'])

    def test_foreign_pid_is_not_trusted_or_killed(self):
        record = self.start()
        path = self.service.runtime / ID / '10' / record['run_id'] / '10.pid'
        path.write_text(str(os.getpid()))
        with self.assertRaises(ServiceError):
            self.service.status(ID, '10')
        self.service.scan()
        self.assertEqual(1, len(self.backend.starts))
        self.assertIn(record['run_id'], self.backend.live)

    def test_never_started_unknown_run_and_untracked_unit(self):
        self.register()
        self.assertEqual('never_started', self.service.status(ID, '10')['state'])
        with self.assertRaises(ServiceError):
            self.service.status(ID, '10', 'nonexistent')
        self.backend.untracked = lambda *args: True
        with self.assertRaises(ServiceError):
            self.service.status(ID, '10')
        self.service.scan()
        self.assertEqual([], self.backend.starts)

    def test_registry_and_symlink_rejected(self):
        self.register('10 tuntom\n10 other\n')
        with self.assertRaises(ServiceError):
            registry(self.install)
        self.register()
        (self.install / '10-start.sh').unlink()
        (self.install / '10-start.sh').symlink_to('/bin/sh')
        self.service.scan()
        self.assertEqual([], self.backend.starts)
        with self.assertRaises(OSError):
            read_file(self.install / '10-start.sh')

    def test_removed_instance_retains_completion(self):
        record = self.start()
        self.service.stop_instance(ID)
        self.service.mark_removed(ID)
        self.manager.peek = lambda _: None
        result = self.service.status(ID, '10', record['run_id'])
        self.assertEqual('removed_clean', result['instance_state'])
        self.assertTrue(result['process_group_empty'])

    def test_sandbox_has_no_host_work_or_proc(self):
        backend = SystemdServices.__new__(SystemdServices)
        backend.rootfs = Path('/image')
        backend.command = Mock(return_value=SimpleNamespace(returncode=0))
        record = {'unit': 'test.service', 'run_id': 'run', 'prefix': '10'}
        backend.start(record, self.instance, Path('/installation'), Path('/runtime'), '/run/netns/instance')
        command = backend.command.call_args.args[0]
        self.assertIn('--property=InaccessiblePaths=/proc /sys', command)
        self.assertIn('--property=BindReadOnlyPaths=/installation:/microservices', command)
        self.assertIn('--property=NetworkNamespacePath=/proc/1/ns/net', command)
        self.assertNotIn('--property=NetworkNamespacePath=/run/netns/instance', command)
        self.assertIn('--property=BindReadOnlyPaths=/run/netns/instance:/run/sas/instance.netns', command)
        self.assertIn('--property=KillMode=control-group', command)
        self.assertFalse(any('/work' in arg for arg in command))

    def test_host_execution_ignores_profile_transport_namespace(self):
        self.manager.location = lambda _: {'namespace_path': '/run/netns/dataplane',
                                           'transport_namespace_path': '/run/netns/transport'}
        record = self.start()
        self.assertEqual('host', record['execution_namespace'])
        self.assertEqual('/run/netns/dataplane', record['target_namespace_path'])
