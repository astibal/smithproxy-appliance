"""Fabric V3 microservices: registry intent, per-run cgroups, durable evidence.

PID files are observations, never targets for kill(2). No installed script runs
on the host filesystem. Fabric locks never block the status/stop API.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .deployments import atomic_json, boot_id
from .systemd import BackendError

PREFIX = re.compile(r'[1-9][0-9]{0,8}')
OWNER = re.compile(r'[A-Za-z0-9_.-]{1,64}')


class ServiceError(BackendError):
    def __init__(self, error, http=503):
        super().__init__(error)
        self.error, self.http = error, http


def now():
    return datetime.now(timezone.utc).isoformat()


def identifiers(instance_id, prefix):
    try:
        if str(uuid.UUID(instance_id)) != instance_id or not PREFIX.fullmatch(prefix):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ServiceError('invalid_request', 400)


def read_file(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ServiceError('state_unknown')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ServiceError('state_unknown')
        return data.decode('utf-8')
    finally:
        os.close(fd)


def registry(directory):
    try:
        text = read_file(directory / 'db.info')
    except FileNotFoundError:
        return {}
    rows = {}
    for line in text.splitlines():
        tokens = line.split('#', 1)[0].split()
        if not tokens:
            continue
        if len(tokens) < 2 or not PREFIX.fullmatch(tokens[0]) or not OWNER.fullmatch(tokens[1]) or tokens[0] in rows:
            raise ServiceError('invalid_registry')
        rows[tokens[0]] = tokens[1]
    return rows


def trusted_directory(path):
    """Reject writable ancestors and symlinks; privileged inputs are host-owned."""
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ServiceError('unsafe_installation')


@contextlib.contextmanager
def file_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ServiceError('unsafe_lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class SystemdServices:
    def __init__(self, rootfs):
        self.rootfs = Path(rootfs).resolve()
        self.manifest = json.loads(read_file(self.rootfs / 'rootfs.json', 1024 * 1024))
        if self.manifest.get('contract_version') != 3:
            raise ServiceError('invalid_rootfs')
        trusted_directory(self.rootfs)
        for relative, digest in self.manifest['files'].items():
            path = self.rootfs / relative
            if path.is_symlink() or not path.resolve().is_relative_to(self.rootfs) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ServiceError('invalid_rootfs')

    @staticmethod
    def command(args, timeout=3):
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ServiceError('stop_timeout', 504)
        except OSError:
            raise ServiceError('state_unknown')
        return result

    def show(self, unit):
        result = self.command(['systemctl', 'show', unit,
            '--property=LoadState,ActiveState,MainPID,ControlGroup,Description,Job,RootDirectory,InvocationID'])
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not values or (result.returncode and values.get('LoadState') != 'not-found'):
            raise ServiceError('state_unknown')
        return values

    @staticmethod
    def group_path(slice_unit, unit):
        pieces = slice_unit.removesuffix('.slice').split('-')
        return '/' + '/'.join('-'.join(pieces[:i]) + '.slice' for i in range(1, len(pieces) + 1)) + '/' + unit

    def inspect(self, record):
        if record['boot_id'] != boot_id():
            return {'empty': True, 'pid': 0}
        values = self.show(record['unit'])
        if values.get('LoadState') != 'not-found':
            if values.get('Description') != 'SAS microservice ' + record['run_id']:
                raise ServiceError('identity_conflict', 409)
            group = values.get('ControlGroup', '')
            if group and group != record['cgroup']:
                raise ServiceError('identity_conflict', 409)
            invocation = values.get('InvocationID', '')
            if record.get('invocation_id') and invocation and invocation != record['invocation_id']:
                raise ServiceError('identity_conflict', 409)
        if values.get('Job', '0') not in ('0', '') or values.get('ActiveState') in ('activating', 'deactivating'):
            return {'empty': False, 'pid': int(values.get('MainPID', '0')), 'pending': True}
        group_path = Path('/sys/fs/cgroup') / record['cgroup'].lstrip('/')
        try:
            events = dict(line.split() for line in (group_path / 'cgroup.events').read_text().splitlines())
            empty = events.get('populated') == '0'
        except FileNotFoundError:
            if not Path('/sys/fs/cgroup/cgroup.controllers').is_file():
                raise ServiceError('state_unknown')
            empty = True
        if empty and values.get('LoadState') == 'not-found' and time.time() < record['created_epoch'] + 30:
            raise ServiceError('state_unknown')  # an interrupted start may still be in flight
        if empty and values.get('ActiveState') == 'active':
            raise ServiceError('state_unknown')
        return {'empty': empty, 'pid': int(values.get('MainPID', '0')),
                'invocation_id': values.get('InvocationID', '')}

    def eligible(self, instance):
        values = self.show(instance.unit)
        if values.get('ActiveState') != 'active' or not values.get('RootDirectory'):
            raise ServiceError('instance_requires_active_rootfs')
        pid = int(values.get('MainPID', '0'))
        if not pid or not Path(f'/proc/{pid}/root').is_dir():
            raise ServiceError('state_unknown')
        return pid

    def untracked(self, instance_id, prefix, known):
        pattern = f'sas-ms-{instance_id.replace("-", "")}-{prefix}-*.service'
        result = self.command(['systemctl', 'list-units', '--all', '--plain', '--no-legend', pattern])
        if result.returncode:
            raise ServiceError('state_unknown')
        return any(line.split()[0] not in known for line in result.stdout.splitlines() if line.strip())

    def start(self, record, instance, installation, runtime, target_ns):
        # A failed or uncertain start stays recorded and is reconciled, not retried blindly.
        capabilities = 'CAP_NET_ADMIN CAP_NET_RAW CAP_SYS_ADMIN CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE'
        properties = {
            'Type': 'exec', 'ExitType': 'cgroup', 'KillMode': 'control-group',
            'Slice': instance.slice_unit, 'BindsTo': instance.unit, 'After': instance.unit,
            'Description': 'SAS microservice ' + record['run_id'],
            'RootDirectory': str(self.rootfs), 'WorkingDirectory': '/microservices',
            # Transport belongs to the host, not the appliance dataplane or
            # profile's transport veth. systemd resolves this before RootDirectory.
            'NetworkNamespacePath': '/proc/1/ns/net', 'MountAPIVFS': 'no',
            'ProtectSystem': 'strict', 'ProtectHome': 'yes', 'PrivateDevices': 'yes',
            'NoNewPrivileges': 'yes', 'ProtectControlGroups': 'yes',
            'ProtectKernelTunables': 'yes', 'ProtectKernelModules': 'yes',
            'ProtectKernelLogs': 'yes', 'ProtectClock': 'yes', 'ProtectHostname': 'yes',
            'RestrictNamespaces': 'net', 'RestrictSUIDSGID': 'yes',
            'RestrictRealtime': 'yes', 'LockPersonality': 'yes',
            'CapabilityBoundingSet': capabilities, 'AmbientCapabilities': capabilities,
            'SystemCallArchitectures': 'native',
            'SystemCallFilter': '~@mount @module @reboot @swap @raw-io @clock ptrace process_vm_readv process_vm_writev open_by_handle_at name_to_handle_at unshare bpf io_uring_setup',
            'InaccessiblePaths': '/proc /sys',
            'RestrictAddressFamilies': 'AF_UNIX AF_INET AF_INET6 AF_NETLINK',
            'DevicePolicy': 'closed', 'DeviceAllow': '/dev/net/tun rw',
            'ReadWritePaths': '/run/sas-microservice', 'UMask': '0077',
            'TasksMax': '128', 'MemoryMax': '512M', 'TimeoutStopSec': '10s',
            'SendSIGKILL': 'yes', 'Restart': 'no',
        }
        if instance.deadline:
            remaining = datetime.fromisoformat(instance.deadline).timestamp() - time.time()
            if remaining <= 0:
                raise ServiceError('instance_expired', 409)
            properties['RuntimeMaxSec'] = str(max(1, int(remaining)))
        args = ['systemd-run', '--quiet', '--unit=' + record['unit']]
        args += [f'--property={key}={value}' for key, value in properties.items()]
        args += [f'--property=BindReadOnlyPaths={installation}:/microservices',
                 f'--property=BindReadOnlyPaths={target_ns}:/run/sas/instance.netns',
                 f'--property=BindPaths={runtime}:/run/sas-microservice',
                 '--property=BindPaths=/dev/net/tun:/dev/net/tun']
        environment = {
            'PATH': '/bin:/usr/bin', 'HOME': '/run/sas-microservice',
            'TMPDIR': '/run/sas-microservice', 'SAS_MICROSERVICE_PREFIX': record['prefix'],
            'SAS_MICROSERVICE_RUN_ID': record['run_id'], 'SAS_MICROSERVICE_DIR': '/microservices',
            'SAS_MICROSERVICE_RUNTIME_DIR': '/run/sas-microservice',
            'SAS_MICROSERVICE_PID_FILE': '/run/sas-microservice/' + record['prefix'] + '.pid',
            'SAS_INSTANCE_NETNS': '/run/sas/instance.netns',
        }
        args += [f'--setenv={key}={value}' for key, value in environment.items()]
        args += ['--', '/bin/sh', './' + record['prefix'] + '-start.sh']
        if self.command(args, timeout=10).returncode:
            raise ServiceError('start_failed')

    def stop(self, record):
        result = self.command(['systemctl', 'stop', record['unit']], timeout=20)
        if result.returncode:
            raise ServiceError('state_unknown')
        if not self.inspect(record)['empty']:
            raise ServiceError('stop_timeout', 504)


class Microservices:
    def __init__(self, manager, backend, interval=60):
        if not 1 <= interval <= 86400:
            raise ValueError('microservice interval must be 1..86400 seconds')
        self.manager, self.backend, self.interval = manager, backend, interval
        self.base = manager.runtime_root.parent
        self.installations = self.base / 'microservices'
        self.runtime = self.base / 'microservice-runtime'
        self.records = self.base / 'microservice-state'
        self.lock = threading.RLock()
        for directory in (self.installations, self.runtime, self.records):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            trusted_directory(directory)

    @contextlib.contextmanager
    def guard(self):
        if not self.lock.acquire(timeout=1):
            raise ServiceError('state_unknown')
        try:
            yield
        finally:
            self.lock.release()

    def runs(self, instance_id, prefix=None):
        path = self.records / instance_id
        values = [json.loads(read_file(p)) for p in path.glob('*.json')]
        if prefix:
            values = [v for v in values if v['prefix'] == prefix]
        return sorted(values, key=lambda r: (r['created_epoch'], r['run_id']))

    def save(self, record):
        atomic_json(self.records / record['instance_id'] / (record['run_id'] + '.json'), record)

    def provision(self, instance_id):
        identifiers(instance_id, '1')
        directory = self.installations / instance_id
        directory.mkdir(mode=0o700, exist_ok=True)
        trusted_directory(directory)
        return directory

    def refresh(self, record):
        if record.get('state') == 'stopped':
            return record
        state = self.backend.inspect(record)
        if state['empty']:
            record.update(state='stopped', process_group_empty=True, verified_at=now())
        else:
            record.update(process_group_empty=False, verified_at=now())
            if state.get('invocation_id') and not record.get('invocation_id'):
                record['invocation_id'] = state['invocation_id']
            pid_path = self.runtime / record['instance_id'] / record['prefix'] / record['run_id'] / (record['prefix'] + '.pid')
            try:
                value = read_file(pid_path, 32).strip()
                if not value.isdecimal() or int(value) <= 0:
                    raise ValueError()
                pid = int(value)
                membership = Path(f'/proc/{pid}/cgroup').read_text()
                actual_group = next(line[3:] for line in membership.splitlines() if line.startswith('0::'))
                if actual_group != record['cgroup'] and not actual_group.startswith(record['cgroup'] + '/'):
                    raise ValueError()
                start_time = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                # New PID within the same run's cgroup is allowed (Fabric never signals it).
                record.update(pid=pid, pid_start_time=start_time, state='running', error='')
            except FileNotFoundError:
                record.update(state='starting' if time.time() < record['created_epoch'] + 30 else 'unknown', error='pid_unavailable')
            except (OSError, ValueError, StopIteration, ServiceError):
                record.update(state='unknown', error='pid_identity_unverified')
        self.save(record)
        return record

    def status(self, instance_id, prefix, run_id=None):
        identifiers(instance_id, prefix)
        with self.guard():
            runs = self.runs(instance_id, prefix)
            record = next((r for r in runs if r['run_id'] == run_id), None) if run_id else (runs[-1] if runs else None)
            instance = self.manager.peek(instance_id)
            try:
                rows = registry(self.installations / instance_id)
                registered = prefix in rows
            except (ServiceError, OSError, ValueError):
                rows, registered = {}, None
            common = dict(contract_version=3, instance_id=instance_id, prefix=prefix,
                          origin=socket.gethostname(), registered=registered,
                          current_run_id=runs[-1]['run_id'] if runs else None,
                          instance_state='present' if instance else 'unknown')
            if not record:
                # Only a registered, managed slot can be proven never started.
                if run_id or not instance or registered is None or not (self.installations / instance_id).is_dir():
                    raise ServiceError('not_found', 404)
                if self.backend.untracked(instance_id, prefix, {r['unit'] for r in runs}):
                    raise ServiceError('state_unknown')
                return {**common, 'owner': rows.get(prefix), 'run_id': None,
                        'state': 'never_started', 'process_group_empty': True, 'verified_at': now()}
            record = self.refresh(record)
            if not instance and (self.records / 'removed' / (instance_id + '.json')).is_file() and all(self.refresh(r)['state'] == 'stopped' for r in self.runs(instance_id)):
                common['instance_state'] = 'removed_clean'
            result = {**record, **common}
            if record['state'] == 'unknown':
                raise ServiceError('state_unknown')
            return result

    def stop(self, instance_id, prefix, owner, run_id, internal=False):
        identifiers(instance_id, prefix)
        if not isinstance(owner, str) or not OWNER.fullmatch(owner) or not isinstance(run_id, str):
            raise ServiceError('invalid_request', 400)
        with self.guard():
            record = next((r for r in self.runs(instance_id, prefix) if r['run_id'] == run_id), None)
            if not record:
                raise ServiceError('unknown_run', 404)
            if record['owner'] != owner:
                raise ServiceError('identity_conflict', 409)
            record = self.refresh(record)
            if record['state'] == 'stopped':
                return {**record, 'contract_version': 3, 'result': 'already_stopped'}
            if not internal and prefix in registry(self.installations / instance_id):
                raise ServiceError('registration_present', 409)
            record['state'] = 'stopping'
            self.save(record)
            self.backend.stop(record)
            record.update(state='stopped', process_group_empty=True, verified_at=now())
            self.save(record)
            return {**record, 'contract_version': 3, 'result': 'stopped'}

    def stop_instance(self, instance_id):
        with self.guard():
            for record in self.runs(instance_id):
                if record['state'] != 'stopped':
                    self.stop(instance_id, record['prefix'], record['owner'], record['run_id'], internal=True)

    def mark_removed(self, instance_id):
        self.stop_instance(instance_id)
        atomic_json(self.records / 'removed' / (instance_id + '.json'),
                    {'instance_id': instance_id, 'verified_at': now()})

    def scan_instance(self, instance):
        directory = self.installations / instance.id
        if not directory.exists():
            return []
        trusted_directory(directory)
        blocked = []
        with file_lock(directory / 'db.lock'):
            for prefix, owner in sorted(registry(directory).items(), key=lambda row: int(row[0])):
                try:
                    with file_lock(directory / (prefix + '.lock')):
                        self.start_or_refresh(instance, prefix, owner, directory)
                except BlockingIOError:
                    blocked.append(prefix)
        return blocked

    def check_instance(self, instance_id, *, nonblocking=False):
        """Explicit reconciliation of one instance, never a global scan.

        Shares the periodic scanner's lock order and restart/backoff semantics.
        Installer locks are reported as deferred, never as a successful check.
        """
        identifiers(instance_id, '1')
        acquired = (self.manager.lock.acquire(blocking=False) if nonblocking
                    else self.manager.lock.acquire(timeout=1))
        if not acquired:
            raise ServiceError('instance_busy', 409)
        try:
            instance = self.manager.peek(instance_id)
            if not instance:
                raise ServiceError('instance_not_found', 404)
            system_start = getattr(self.manager, 'system_start', None)
            with self.guard():
                for record in self.runs(instance.id):
                    if record['state'] == 'stopping':
                        self.stop(instance.id, record['prefix'], record['owner'], record['run_id'], internal=True)
                    else:
                        self.refresh(record)
            system_result = system_start.check(instance) if system_start else None
            if instance.desired_state != 'running' or instance.state != 'running':
                self.stop_instance(instance.id)
                blocked = []
            else:
                try:
                    blocked = self.scan_instance(instance) or []
                except BlockingIOError:
                    blocked = ['registry']
            return {'contract_version': 3, 'instance_id': instance_id,
                    'system_start': system_result,
                    'checked_at': now(), 'state': 'deferred' if blocked else 'checked',
                    'blocked': blocked, 'runs': self.runs(instance_id)}
        finally:
            self.manager.lock.release()

    def start_or_refresh(self, instance, prefix, owner, installation):
        with self.guard():
            runs = self.runs(instance.id, prefix)
            if self.backend.untracked(instance.id, prefix, {r['unit'] for r in runs}):
                raise ServiceError('untracked_microservice')
            for record in runs:
                if self.refresh(record)['state'] != 'stopped':
                    return
            if runs:
                last = runs[-1]
                # Persisted bounded exponential backoff; old history is retained.
                recent = sum(r['created_epoch'] > time.time() - 3600 for r in runs)
                delay = min(900, 30 * 2 ** min(recent, 5))
                if time.time() < last['created_epoch'] + delay:
                    return
            current = self.manager.peek(instance.id)
            if not current or current.desired_state != 'running' or current.state != 'running':
                return
            if current.deadline and datetime.fromisoformat(current.deadline).timestamp() <= time.time():
                return
            if registry(installation).get(prefix) != owner:
                return
            pid = self.backend.eligible(current)
            # Rootfs must not expose the installation through its absolute host path.
            if Path(f'/proc/{pid}/root{installation}').exists():
                raise ServiceError('installation_visible_to_appliance')
            for path in installation.rglob('*'):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise ServiceError('unsafe_installation')
            script = installation / (prefix + '-start.sh')
            if not script.is_file():
                raise ServiceError('start_script_missing')
            run_id = uuid.uuid4().hex
            unit = f'sas-ms-{instance.id.replace("-", "")}-{prefix}-{run_id}.service'
            record = dict(instance_id=instance.id, prefix=prefix, owner=owner, run_id=run_id,
                          unit=unit, cgroup=self.backend.group_path(current.slice_unit, unit),
                          created_epoch=time.time(), boot_id=boot_id(), state='starting',
                          process_group_empty=None, verified_at=None)
            runtime = self.runtime / instance.id / prefix / run_id
            runtime.mkdir(parents=True, mode=0o700)
            os.chown(runtime, 65532, 65532)
            runtime.chmod(0o700)
            coordinates = self.manager.location(instance.id)
            target_ns = coordinates['namespace_path']
            record['execution_namespace'] = 'host'
            record['target_namespace_path'] = target_ns
            self.save(record)  # durable intent before systemd sees a start
            try:
                self.backend.start(record, current, installation, runtime, target_ns)
            except ServiceError as exc:
                record['error'] = exc.error
                self.save(record)
                raise

    def scan(self):
        # Same lock order as instance stop: manager -> supervisor. Never wait
        # for Fabric flocks; never hold manager lock while sleeping between scans.
        for instance in self.manager.snapshot():
            try:
                self.check_instance(instance.id, nonblocking=True)
            except BlockingIOError:
                pass
            except Exception as exc:
                # Do not print script contents, config or secrets.
                print(f'microservice scan {instance.id}: {type(exc).__name__}: {exc}')
        # Deleted records do not orphan independently running cgroups.
        for directory in self.records.iterdir():
            if directory.name != 'removed' and directory.is_dir() and not self.manager.peek(directory.name):
                try:
                    self.stop_instance(directory.name)
                except Exception as exc:
                    print(f'microservice orphan cleanup {directory.name}: {exc}')

    def run(self, stopping):
        while not stopping.is_set():
            self.scan()
            stopping.wait(self.interval)
