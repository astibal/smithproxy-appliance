"""Reserved SAS 00-start service; trusted runner code, never a guest script.

Repairs only the addresses/routes described by SAS's durable allocations. It
does not discover configuration from the guest, allocate cable IPs, or flush
unrelated interfaces/routes. QEMU/blackboxes are not passed to this controller.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
import threading
import uuid
from datetime import datetime, timezone

from .deployments import atomic_json
from .systemd import BackendError


class SystemStart:
    def __init__(self, manager, *, test_drive=False):
        self.manager = manager
        self.test_drive = test_drive
        self.root = manager.state_dir.parent / 'system-start'
        self.lock = threading.RLock()
        self.wiring = None

    def status(self, instance_id):
        if str(uuid.UUID(instance_id)) != instance_id:
            raise BackendError('invalid instance id')
        path = self.root / (instance_id + '.json')
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            value = {'enabled': True, 'state': 'pending'}
        return {**value, 'prefix': '00', 'name': '00-start', 'owner': 'sas',
                'instance_id': instance_id, 'execution': 'runner-oneshot'}

    def configure(self, instance_id, enabled):
        if not isinstance(enabled, bool):
            raise BackendError('enabled must be boolean')
        with self.manager.lock, self.lock:
            if not self.manager.peek(instance_id):
                raise BackendError('instance not found')
            value = self.status(instance_id)
            value.update(enabled=enabled, state='pending' if enabled else 'disabled')
            atomic_json(self.root / (instance_id + '.json'), value)
            return value

    def plan(self, instance):
        backend = self.manager.backend
        ingress_id = (backend.test_drive_ingress_id(instance.id) if self.test_drive
                      else backend.ingress_id(instance.id))
        if backend.network_settings:
            leases = backend.network_settings.allocations()
            if instance.id not in leases or ingress_id not in leases:
                raise BackendError('00-start: durable network allocation missing; refusing to guess')
        allocation = backend.allocation(instance.id)
        ingress = backend.allocation(ingress_id) if self.test_drive else backend.ingress_allocation(instance.id)
        namespace = 'cz-' + instance.id[:8]
        if allocation.namespace != namespace or instance.namespace != namespace:
            raise BackendError('00-start: namespace ownership mismatch')
        addresses, routes = [], []
        via = allocation.topology == 'tuntom-via'
        if allocation.topology not in {'split-veth', 'veth-out', 'on-a-stick', 'tuntom-via', 'none'}:
            raise BackendError('00-start: unsupported network topology')

        def address(device, value, prefix):
            if value:
                addresses.append({'device': device, 'address': str(ipaddress.ip_interface(f'{value}/{prefix}'))})

        def route(version, destination, device, gateway='', source='', table='main', kind='unicast', rule_source=''):
            routes.append(dict(version=version, destination=destination, device=device,
                               gateway=gateway, source=source, table=table, kind=kind, rule_source=rule_source))

        if allocation.topology in {'split-veth', 'veth-out', 'tuntom-via'}:
            for version, local, gateway in ((4, allocation.guest_ip, allocation.host_ip),
                                             (6, allocation.guest_ip_v6, allocation.host_ip_v6)):
                if local:
                    address(allocation.guest_if, local, (32 if version == 4 else 128) if via else (30 if version == 4 else 126))
                    route(version, 'default', allocation.guest_if, '' if via else gateway)
        if ingress.namespace == namespace and ingress.topology != 'none':
            for version, local, gateway in ((4, ingress.guest_ip, ingress.host_ip),
                                             (6, ingress.guest_ip_v6, ingress.host_ip_v6)):
                if local:
                    address(ingress.guest_if, local, (32 if version == 4 else 128) if via else (30 if version == 4 else 126))
                    if allocation.topology == 'on-a-stick':
                        route(version, 'default', ingress.guest_if, gateway)
                    if self.test_drive:
                        route(version, 'default', ingress.guest_if, gateway, table='101',
                              rule_source=local)
            for source in getattr(instance, 'source_ips', []):
                parsed = ipaddress.ip_address(source)
                gateway = ingress.host_ip if parsed.version == 4 else ingress.host_ip_v6
                route(parsed.version, f'{parsed}/{parsed.max_prefixlen}', ingress.guest_if,
                      '' if via else gateway)
        if getattr(instance, 'network_ingress_driver', '') == 'authorized-veth':
            route(4, '0.0.0.0/0', 'lo', table='100', kind='local')
            if allocation.guest_ip_v6:
                route(6, '::/0', 'lo', table='100', kind='local')
        if via:
            # Read only the switch address; never publish the secret/start options.
            document = json.loads(self.manager._deployment_path(instance.id).read_text())
            switch = ipaddress.ip_address(document['start_options']['tuntom_switch_ip'])
            local = allocation.fabric_ip if switch.version == 4 else allocation.fabric_ip_v6
            address('fabric0', local, switch.max_prefixlen)
            route(switch.version, f'{switch}/{switch.max_prefixlen}', 'fabric0', source=local)
        return {'namespace': namespace, 'addresses': addresses, 'routes': routes}

    @staticmethod
    def read(command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        value = json.loads(result.stdout)
        if not isinstance(value, list):
            raise BackendError('00-start: invalid network observation')
        return value

    def check(self, instance, *, starting=False):
        with self.lock:
            value = self.status(instance.id)
            if not value['enabled']:
                return {**value, 'state': 'disabled'}
            if not starting and (instance.state != 'running' or getattr(instance, 'desired_state', 'running') != 'running'):
                return {**value, 'state': 'inactive'}
            try:
                plan = self.plan(instance)
                backend = self.manager.backend
                prefix = ['ip', '-n', plan['namespace']]
                links = self.read(prefix + ['-j', 'addr', 'show'])
                interfaces = {link['ifname']: link for link in links}
                operations = []
                # Validate every required interface BEFORE touching any addresses.
                for device in sorted({'lo', *(a['device'] for a in plan['addresses']),
                                      *(r['device'] for r in plan['routes'])}):
                    if device not in interfaces:
                        raise BackendError(f'00-start: interface {device} is missing')
                for address in plan['addresses']:
                    existing = {str(ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}"))
                                for a in interfaces[address['device']].get('addr_info', [])}
                    if address['address'] not in existing:
                        operations.append(prefix + ['addr', 'replace', address['address'], 'dev', address['device']])
                observed_routes = {version: self.read(prefix + (['-6'] if version == 6 else [])
                                   + ['-j', 'route', 'show', 'table', 'all'])
                                   for version in {r['version'] for r in plan['routes']}}
                for item in plan['routes']:
                    family = ['-6'] if item['version'] == 6 else []
                    args = prefix + family + ['route', 'replace']
                    if item['kind'] == 'local':
                        args.append('local')
                    args += [item['destination'], 'dev', item['device'], 'table', item['table']]
                    if item['gateway']:
                        args += ['via', item['gateway']]
                    if item['source']:
                        args += ['src', item['source']]
                    def matches(actual):
                        table = str(actual.get('table', 'main'))
                        table = 'main' if table == '254' else table
                        destination = actual.get('dst', 'default')
                        desired = item['destination']
                        if desired in {'0.0.0.0/0', '::/0'}:
                            desired = 'default'
                        elif '/' in desired and desired.endswith(('/32', '/128')):
                            desired = desired.split('/')[0]
                        return (table == item['table'] and destination == desired
                                and actual.get('dev') == item['device']
                                and actual.get('gateway', '') == item['gateway']
                                and actual.get('type', 'unicast') == item['kind']
                                and (not item['source'] or actual.get('prefsrc') == item['source']))
                    if not any(matches(actual) for actual in observed_routes[item['version']]):
                        operations.append(args)
                    if item['table'] in {'100', '101'}:
                        rules = self.read(prefix + family + ['-j', 'rule', 'show'])
                        priority = int(item['table'])
                        matching = [r for r in rules if r.get('priority') == priority]
                        selector = (['from', item['rule_source']] if item['rule_source']
                                    else ['fwmark', '0x1'])
                        if not matching:
                            operations.append(prefix + family + ['rule', 'add', *selector,
                                              'table', item['table'], 'priority', item['table']])
                        else:
                            valid = len(matching) == 1 and str(matching[0].get('table')) == item['table']
                            if item['rule_source']:
                                expected = item['rule_source']
                                valid = valid and matching[0].get('src', '') in {
                                    expected, expected + ('/128' if item['version'] == 6 else '/32')}
                            else:
                                valid = valid and str(matching[0].get('fwmark')) in {'1', '0x1'}
                                valid = valid and str(matching[0].get('fwmask', '0xffffffff')) in {'4294967295', '0xffffffff'}
                            if not valid:
                                raise BackendError(f'00-start: conflicting policy rule priority {priority}')
                for command in operations:
                    backend._run(command)
                if self.wiring:
                    value['wiring'] = self.wiring.prepare_instance(instance)
                value.update(state='ready', error='', plan=plan,
                             applied_operations=len(operations),
                             checked_at=datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                value.update(state='failed', error=str(exc),
                             checked_at=datetime.now(timezone.utc).isoformat())
                atomic_json(self.root / (instance.id + '.json'), value)
                raise
            atomic_json(self.root / (instance.id + '.json'), value)
            return value
