"""Explicit BFF for the new console. Reuses authentication, never exposes runner token."""
import re
from flask import g, jsonify, request, session

CATALOG = {
    'sources': ('/v1/sources', 'sources'),
    'qemu': ('/v1/qemu-images', 'images'),
    'firewall': ('/v1/firewall', 'authorizations'),
    'settings': ('/v1/settings/networking', None),
    'instances': ('/v1/instances', 'instances'),
    'profiles': ('/v1/runtime-profiles', 'profiles'),
    'programs': ('/v1/program-artifacts', 'artifacts'),
    'binaries': ('/v1/build', 'artifacts'),
    'tuntom': ('/v1/tuntom/build', 'artifacts'),
    'configs': ('/v1/configs', 'configs'),
    'networks': ('/v1/network-profiles', 'profiles'),
    'wiring': ('/v1/l2-segments', 'segments'),
    'certificates': ('/v1/cert-bundles', 'bundles'),
    'endpoints': ('/v1/headless-endpoints', 'packages'),
    'test-drives': ('/v1/test-drives', 'test_drives'),
    'exports': ('/v1/appliance-exports', 'exports'),
    'tasks': ('/v1/tasks', 'tasks'),
}

# Only named application operations are exposed, never arbitrary runner URLs.
OPERATIONS = {
    ('qemu', 'create'): ('POST', '/v1/qemu-images', True),
    ('qemu', 'delete'): ('DELETE', '/v1/qemu-images/{id}', True),
    ('test-drives', 'upload-file'): ('POST', '/v1/test-drives/{id}/files', True),
    ('instances', 'check-services'): ('POST', '/v1/instances/{id}/microservices/check', True),
    ('instances', 'system-start'): ('POST', '/v1/instances/{id}/microservices/00/configure', True),
    ('certificates', 'create'): ('POST', '/v1/cert-bundles', False),
    ('certificates', 'certificate'): ('POST', '/v1/cert-bundles/{id}/certificates', False),
    ('certificates', 'delete'): ('DELETE', '/v1/cert-bundles/{id}', False),
    ('endpoints', 'create'): ('POST', '/v1/headless-endpoints', True),
    ('endpoints', 'delete'): ('DELETE', '/v1/headless-endpoints/{id}', True),
    ('exports', 'create'): ('POST', '/v1/appliance-exports', False),
    ('exports', 'delete'): ('DELETE', '/v1/appliance-exports/{id}', False),
    ('profiles', 'upload-file'): ('PUT', '/v1/runtime-profiles/{id}/work-files', True),
    ('profiles', 'delete-file'): ('DELETE', '/v1/runtime-profiles/{id}/work-files', True),
    ('configs', 'observe'): ('POST', '/v1/config-observer', False),
    ('firewall', 'create'): ('POST', '/v1/firewall/authorizations', False),
    ('firewall', 'delete'): ('DELETE', '/v1/firewall/authorizations/{id}', False),
    ('firewall', 'extend'): ('POST', '/v1/firewall/authorizations/{id}/extend', False),
    ('firewall', 'settings'): ('PUT', '/v1/firewall', False),
    ('firewall', 'attach'): ('POST', '/v1/instances/{id}/sources', False),
    ('settings', 'save'): ('PUT', '/v1/settings/networking', False),
    ('wiring', 'attach'): ('POST', '/v1/l2-segments/{id}/endpoints', True),
    ('wiring', 'detach'): ('DELETE', '/v1/l2-segments/{id}/endpoints/{endpoint}', True),
    ('wiring', 'addressing'): ('POST', '/v1/l2-segments/{id}/endpoints/{endpoint}/addressing', True),
    ('wiring', 'check-addressing'): ('POST', '/v1/l2-segments/addressing/preview', True),
    ('networks', 'create'): ('POST', '/v1/network-profiles', False),
    ('networks', 'save'): ('PUT', '/v1/network-profiles/{id}', False),
    ('networks', 'delete'): ('DELETE', '/v1/network-profiles/{id}', False),
    ('binaries', 'build'): ('POST', '/v1/build', True),
    ('binaries', 'fetch'): ('POST', '/v1/refs/refresh', True),
    ('binaries', 'delete'): ('DELETE', '/v1/builds/{id}', False),
    ('binaries', 'extract'): ('POST', '/v1/builds/{id}/config/preview', False),
    ('binaries', 'rootfs'): ('POST', '/v1/builds/{id}/rootfs', False),
    ('tuntom', 'build'): ('POST', '/v1/tuntom/build', True),
    ('tuntom', 'fetch'): ('POST', '/v1/tuntom/refs/refresh', True),
    ('tuntom', 'delete'): ('DELETE', '/v1/tuntom/builds/{id}', False),
    ('instances', 'create'): ('POST', '/v1/instances', True),
    ('instances', 'cleanup'): ('POST', '/v1/instances/cleanup', False),
    ('instances', 'extend'): ('POST', '/v1/instances/{id}/extend', True),
    ('instances', 'extract'): ('POST', '/v1/instances/{id}/config/preview', False),
    ('instances', 'debug-start'): ('POST', '/v1/instances/{id}/debug', False),
    ('instances', 'debug-stop'): ('DELETE', '/v1/instances/{id}/debug', False),
    ('configs', 'preview'): ('POST', '/v1/configs/preview', False),
    ('configs', 'commit'): ('POST', '/v1/configs/commit', False),
    ('configs', 'cancel'): ('DELETE', '/v1/configs/previews/{id}', False),
    ('configs', 'delete'): ('DELETE', '/v1/configs/{id}', False),
    ('configs', 'metadata'): ('PUT', '/v1/configs/{id}/metadata', False),
    ('test-drives', 'create'): ('POST', '/v1/test-drives', False),
    ('test-drives', 'delete'): ('DELETE', '/v1/test-drives/{id}', False),
    ('test-drives', 'restart'): ('POST', '/v1/test-drives/{id}/restart', False),
    ('test-drives', 'extend'): ('POST', '/v1/test-drives/{id}/extend', False),
    ('test-drives', 'upgrade'): ('POST', '/v1/test-drives/{id}/upgrade', False),
    ('test-drives', 'config-mode'): ('POST', '/v1/test-drives/{id}/config-mode', False),
    ('test-drives', 'extract'): ('POST', '/v1/test-drives/{id}/config/preview', False),
}
DETAILS = {
    ('test-drives', 'files'): '/v1/test-drives/{id}/files',
    ('test-drives', 'logs'): '/v1/test-drives/{id}/logs',
    ('profiles', 'profile'): '/v1/runtime-profiles/{id}',
    ('firewall', 'state'): '/v1/firewall',
    ('settings', 'networking'): '/v1/settings/networking',
    ('wiring', 'inventory'): '/v1/l2-segments/addressing',
    ('wiring', 'segment'): '/v1/l2-segments/{id}',
    ('configs', 'content'): '/v1/configs/{id}',
    ('tasks', 'result'): '/v1/tasks/{id}/result',
    ('binaries', 'status'): '/v1/build',
    ('tuntom', 'status'): '/v1/tuntom/build',
}


def register(app, api, enqueue, audit=None):
    @app.before_request
    def next_auth():
        if request.path.startswith('/next-api/') and not g.admin:
            return jsonify(error='Authentication required'), 401

    @app.get('/next-api/session')
    def next_session():
        return jsonify(csrf=session['csrf_token'], id=g.admin['id'], email=g.admin['email'], locale=g.locale)

    @app.post('/next-api/locale')
    def next_locale():
        body = request.get_json(silent=True)
        locale = body.get('locale') if isinstance(body, dict) else None
        if locale not in ('cs', 'en', 'fr'):
            return jsonify(error='Unsupported locale'), 400
        session['locale'] = locale
        return jsonify(locale=locale)

    @app.get('/next-api/catalog/<resource>')
    def next_catalog(resource):
        if resource == 'preferences':
            return jsonify(items=[{'id': 'preferences', 'name': g.admin['email']}])
        if resource not in CATALOG:
            return jsonify(error='Unknown resource'), 404
        path, key = CATALOG[resource]
        try:
            data = api('GET', path)
            return jsonify(items=data.get(key, []) if key else [{'id': resource, 'name': resource, **data}])
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 503

    @app.post('/next-api/action')
    def next_action():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='Expected object'), 400
        resource, action, identity = body.get('resource'), body.get('action'), body.get('id', '')
        if not isinstance(resource, str) or not isinstance(action, str):
            return jsonify(error='Invalid operation'), 400
        if not isinstance(identity, str) or (identity and not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', identity)):
            return jsonify(error='Invalid resource ID'), 400
        payload = body.get('payload', {})
        if not isinstance(payload, dict):
            return jsonify(error='Expected payload object'), 400
        method, path, direct = 'POST', '', False
        if resource == 'profiles':
            if action == 'spawn' and identity:
                path, payload, direct = '/v1/instances', {'runtime_profile_id': identity, 'user_id': 'admin-console'}, True
            elif action in ('save', 'create'):
                method = 'PUT' if identity else 'POST'
                path = '/v1/runtime-profiles' + ('/' + identity if identity else '')
            elif action == 'delete' and identity:
                method, path = 'DELETE', '/v1/runtime-profiles/' + identity
        elif resource == 'instances' and identity:
            paths = {'stop': ('DELETE', ''), 'restart': ('POST', '/restart'), 'delete': ('DELETE', '/record')}
            if action in paths:
                method, suffix = paths[action]
                path = '/v1/instances/' + identity + suffix
        elif resource == 'wiring':
            direct = True
            if action == 'create':
                path = '/v1/l2-segments'
            elif action == 'delete' and identity:
                method, path = 'DELETE', '/v1/l2-segments/' + identity
        elif resource == 'programs' and action == 'import':
            path, direct = '/v1/program-artifacts', True
        operation = OPERATIONS.get((resource, action))
        if operation:
            method, pattern, direct = operation
            if '{id}' in pattern and not identity:
                return jsonify(error='Resource ID required'), 400
            endpoint = payload.get('endpoint_id', '')
            if '{endpoint}' in pattern:
                if not isinstance(endpoint, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', endpoint):
                    return jsonify(error='Endpoint ID required'), 400
                payload = {key: value for key, value in payload.items() if key != 'endpoint_id'}
            path = pattern.format(id=identity, endpoint=endpoint)
        if resource == 'configs' and action == 'commit':
            payload = {**payload, 'approved_by': g.admin['email']}
        if not path:
            return jsonify(error='Action not supported in this preview'), 400
        try:
            result = api(method, path, payload) if direct else enqueue(method, path, payload, f'{resource}: {action}', f'{resource}-{action}')
            if audit:
                audit(f'next.{resource}.{action}', identity or result.get('task_id', ''))
            return jsonify(result), 202 if result.get('task_id') else 200
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 400

    @app.get('/next-api/detail/<resource>/<identity>/<section>')
    def next_detail(resource, identity, section):
        pattern = DETAILS.get((resource, section))
        if not pattern or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', identity):
            return jsonify(error='Unknown resource'), 404
        try:
            return jsonify(api('GET', pattern.format(id=identity)))
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 400
