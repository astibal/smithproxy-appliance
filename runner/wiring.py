"""Desired port addressing and inventory. Observations never become configuration."""
import copy
import ipaddress
import json
import time
import re
import uuid
from datetime import datetime, timezone

from .deployments import atomic_json
from .systemd import BackendError


def bindings(value, *, profile=False):
    if not isinstance(value, list) or len(value) > 16:
        raise BackendError('wiring must be a list of at most 16 bindings')
    result, ports = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {'segment_id', 'interface', 'addressing'}:
            raise BackendError('invalid wiring binding')
        try:
            segment = str(uuid.UUID(item['segment_id']))
            port = item['interface']
            if segment != item['segment_id'] or not isinstance(port, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,14}', port):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise BackendError('wiring requires canonical segment UUID and interface name') from exc
        if port in ports or port in {'lo', 'di0', 'do0', 'fabric0', 'transport0'}:
            raise BackendError('duplicate or reserved Wiring interface')
        ports.add(port)
        entry = {'segment_id': segment, 'interface': port}
        if 'addressing' in item:
            if profile:
                raise BackendError('IP addressing belongs to Wiring/spawn, not a reusable profile')
            entry['addressing'] = addressing(item['addressing'])
        result.append(entry)
    return result


def addressing(payload):
    if not isinstance(payload, dict) or set(payload) - {'mode', 'addresses', 'routes'}:
        raise BackendError('addressing requires mode, addresses and routes')
    mode = payload.get('mode', 'none')
    if mode not in {'sas', 'guest', 'none'}:
        raise BackendError('addressing mode must be sas, guest or none')
    addresses, routes = payload.get('addresses', []), payload.get('routes', [])
    if not isinstance(addresses, list) or len(addresses) > 64 or not isinstance(routes, list) or len(routes) > 64:
        raise BackendError('addresses and routes must be lists of at most 64 items')
    result = {'mode': mode, 'addresses': [], 'routes': []}
    try:
        for text in addresses:
            if not isinstance(text, str) or '/' not in text or '%' in text:
                raise ValueError('address requires an explicit prefix, without a zone ID')
            value = ipaddress.ip_interface(text)
            if value.ip.is_multicast or value.ip.is_unspecified or value.ip.is_loopback:
                raise ValueError('not a unicast endpoint address')
            result['addresses'].append(str(value))
        result['addresses'] = sorted(set(result['addresses']))
        seen = set()
        for item in routes:
            if not isinstance(item, dict) or set(item) - {'destination', 'gateway'}:
                raise ValueError('route requires destination and optional gateway')
            destination = ipaddress.ip_network(item['destination'], strict=False)
            gateway = ipaddress.ip_address(item['gateway']) if item.get('gateway') else None
            if gateway and (gateway.version != destination.version or gateway.is_multicast or gateway.is_unspecified or '%' in str(gateway)):
                raise ValueError('invalid route gateway')
            if str(destination) in seen:
                raise ValueError('duplicate route destination on port')
            seen.add(str(destination))
            result['routes'].append({'destination': str(destination), 'gateway': str(gateway) if gateway else ''})
    except (ValueError, TypeError, KeyError) as exc:
        raise BackendError(f'invalid port addressing: {exc}') from exc
    if mode == 'none' and (result['addresses'] or result['routes']):
        raise BackendError('unaddressed mode cannot contain addresses or routes')
    return result


class Wiring:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if data.get('schema') != 1 or not isinstance(data.get('ports'), dict):
            raise BackendError('invalid wiring addressing catalogue')
        return data['ports']

    def save(self, ports):
        atomic_json(self.path, {'schema': 1, 'ports': ports})

    @staticmethod
    def key(endpoint):
        return endpoint['instance_id'] + '/' + endpoint['interface']

    def get(self, endpoint, ports=None):
        return copy.deepcopy((self.load() if ports is None else ports).get(self.key(endpoint), {
            'desired': addressing({}), 'state': 'unmanaged', 'error': '', 'observed': None,
        }))

    def configure(self, endpoint, payload):
        desired = addressing(payload)
        ports = self.load()
        key = self.key(endpoint)
        value = ports.get(key, {})
        value.update(instance_id=endpoint['instance_id'], interface=endpoint['interface'],
                     desired=desired, state='pending', error='')
        ports[key] = value
        self.save(ports)
        return copy.deepcopy(value)

    def apply(self, segment, endpoint, namespace, backend):
        """Only a tagged Wiring veth can be mutated; never flush an interface."""
        key = self.key(endpoint)
        ports = self.load()
        value = ports.get(key)
        if not value:
            return
        desired = value['desired']
        if desired['mode'] == 'guest':
            value.update(state='guest', observed=None, error='')
            ports[key] = value
            self.save(ports)
            return  # Explicit hand-off: don't alter guest configuration.
        device = endpoint['interface']
        try:
            link = next((i for i in backend.links(namespace) if i['ifname'] == device), {})
            backend.owned(link, backend.tag(segment, endpoint))
            links = json.loads(backend.run('-n', namespace, '-j', 'addr', 'show', 'dev', device))
            actual = {str(ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}"))
                      for link in links for a in link.get('addr_info', [])}
            previous = value.get('owned', {'addresses': [], 'routes': []})
            # Journal intended ownership before mutation for retry after a crash.
            value['owned'] = {'addresses': sorted(set(previous['addresses'] + desired['addresses'])),
                              'routes': list({json.dumps(r, sort_keys=True): r
                                              for r in previous['routes'] + desired['routes']}.values())}
            value['state'] = 'applying'
            self.save(ports)
            for version in (4, 6):
                family = '-4' if version == 4 else '-6'
                actual_routes = json.loads(backend.run('-n', namespace, family, '-N', '-j', 'route', 'show', 'table', 'main'))
                def matches(actual_route, route):
                    dest = actual_route.get('dst', 'default')
                    dest = ('0.0.0.0/0' if version == 4 else '::/0') if dest == 'default' else dest
                    return (ipaddress.ip_network(dest, strict=False) == ipaddress.ip_network(route['destination'])
                            and actual_route.get('dev') == device
                            and actual_route.get('gateway', '') == route['gateway']
                            and str(actual_route.get('protocol')) == '242'
                            and actual_route.get('metric') == 42760)
                def command(verb, route):
                    args = ['-n', namespace, family, 'route', verb, route['destination'],
                            'dev', device, 'proto', '242', 'metric', '42760']
                    if route['gateway']:
                        args += ['via', route['gateway']]
                    return args
                for route in previous['routes']:
                    if ipaddress.ip_network(route['destination']).version == version and route not in desired['routes']:
                        if any(matches(r, route) for r in actual_routes):
                            backend.run(*command('del', route))
                for address in previous['addresses']:
                    if ipaddress.ip_interface(address).version == version and address not in desired['addresses'] and address in actual:
                        backend.run('-n', namespace, 'addr', 'del', address, 'dev', device)
                for address in desired['addresses']:
                    if ipaddress.ip_interface(address).version == version and address not in actual:
                        backend.run('-n', namespace, 'addr', 'replace', address, 'dev', device)
                for route in desired['routes']:
                    if ipaddress.ip_network(route['destination']).version == version and not any(matches(r, route) for r in actual_routes):
                        # add, not replace: do not overwrite a route belonging to another port/operator.
                        backend.run(*command('add', route))
            # IPv6 DAD is asynchronous: don't release a workload with tentative IPs.
            deadline = time.monotonic() + 5
            while True:
                observed = json.loads(backend.run('-n', namespace, '-j', 'addr', 'show', 'dev', device))
                seen = {str(ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")): a
                        for link in observed for a in link.get('addr_info', [])}
                failed = [a for a in desired['addresses'] if seen.get(a, {}).get('dadfailed')
                          or 'dadfailed' in seen.get(a, {}).get('flags', [])]
                if failed:
                    raise BackendError('IPv6 duplicate address detection failed: ' + ', '.join(failed))
                pending = [a for a in desired['addresses'] if a not in seen
                           or seen[a].get('tentative') or 'tentative' in seen[a].get('flags', [])]
                if not pending:
                    break
                if time.monotonic() >= deadline:
                    raise BackendError('Wiring addresses not ready: ' + ', '.join(pending))
                time.sleep(.1)
            now = datetime.now(timezone.utc).isoformat()
            value.update(owned=copy.deepcopy(desired), state='applied', error='', checked_at=now,
                         observed={'source': 'wiring-check', 'checked_at': now,
                                   'addresses': list(desired['addresses'])})
        except Exception as exc:
            value.update(state='error', error=str(exc))
            self.save(ports)
            raise
        self.save(ports)

    def inventory(self, segments):
        ports, entries = self.load(), []
        attached = {self.key(ep): (segment, ep) for segment in segments for ep in segment['endpoints']}
        for key, port in ports.items():
            segment, endpoint = attached.get(key, ({}, {}))
            for text in port['desired']['addresses']:
                address = ipaddress.ip_interface(text)
                entries.append({'source': 'wiring', 'managed': port['desired']['mode'] == 'sas',
                    'record_type': 'desired' if port['desired']['mode'] == 'sas' else 'declared',
                    'server': 'local', 'instance_id': port['instance_id'],
                    'namespace': 'cz-' + port['instance_id'][:8], 'interface': port['interface'],
                    'segment_id': segment.get('id', ''), 'segment_name': segment.get('name', ''),
                    'endpoint_id': endpoint.get('id', ''), 'connected': bool(segment),
                    'address': str(address.ip), 'prefix': str(address.network), 'version': address.version})
        return {'entries': entries, 'tree': network_tree(entries),
                'discovery_enabled': False, 'scope': 'wiring-catalogue'}


def network_tree(entries):
    nodes = {}
    for entry in entries:
        network = ipaddress.ip_network(entry['prefix'])
        nodes.setdefault(network, {'prefix': str(network), 'version': network.version, 'summary': True, 'usages': [], 'children': []})
        nodes[network]['summary'] = False
        nodes[network]['usages'].append(entry)
        for prefix in ((8, 16) if network.version == 4 else (32, 48)):
            if prefix < network.prefixlen:
                parent = network.supernet(new_prefix=prefix)
                nodes.setdefault(parent, {'prefix': str(parent), 'version': parent.version, 'summary': True, 'usages': [], 'children': []})
    roots = []
    for network in sorted(nodes, key=lambda n: (n.version, int(n.network_address), n.prefixlen)):
        parent = next((network.supernet(new_prefix=p) for p in range(network.prefixlen - 1, -1, -1)
                       if network.supernet(new_prefix=p) in nodes), None)
        if parent is not None:
            nodes[parent]['children'].append(nodes[network])
        else:
            roots.append(nodes[network])
    return roots


def overlaps(entries, addresses, segment_id='', endpoint_id=''):
    desired = addressing({'mode': 'guest', 'addresses': addresses})
    result = []
    for text in desired['addresses']:
        address = ipaddress.ip_interface(text)
        for entry in entries:
            if endpoint_id and entry['endpoint_id'] == endpoint_id:
                continue
            network = ipaddress.ip_network(entry['prefix'])
            if address.version != network.version or not address.network.overlaps(network):
                continue
            duplicate = str(address.ip) == entry['address']
            if not duplicate and segment_id and entry['segment_id'] == segment_id:
                continue  # Sharing one subnet on a cable/switch is normal.
            result.append({'kind': 'duplicate' if duplicate else 'overlap', 'requested': text, 'usage': entry})
    return result
