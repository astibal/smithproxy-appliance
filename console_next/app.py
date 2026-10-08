"""Standalone responsive SAS console; does not import the legacy console."""
import base64
import os
import re
import secrets
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, session, stream_with_context
from werkzeug.exceptions import HTTPException
from console_shared.assets import install as install_assets
from console_shared.auth import install as install_auth
from console_shared.runner_client import client
from console_shared.terminals import install as install_terminals
from .api import register


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True,
                instance_path=os.getenv('SMITHPROXY_APPLIANCE_CONSOLE_STATE') or None)
    app.config.from_mapping(
        ADMIN_FILE=os.getenv('SMITHPROXY_APPLIANCE_CONSOLE_ADMINS',
            os.getenv('SMITHPROXY_APPLIACE_CONSOLE_ADMINS', str(Path(app.instance_path) / 'admins.json'))),
        RUNNER_URL=os.getenv('CAPTURE_RUNNER_URL', 'http://127.0.0.1:9080'),
        RUNNER_WS_URL=os.getenv('CAPTURE_RUNNER_WS_URL', 'ws://127.0.0.1:9081'),
        RUNNER_TOKEN=os.getenv('CZ_RUNNER_TOKEN', ''),
        RUNNER_TIMEOUT=float(os.getenv('CAPTURE_RUNNER_TIMEOUT', '5')),
        SEND_FILE_MAX_AGE_DEFAULT=0,
        SESSION_COOKIE_NAME='sas_next_session', SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict', MAX_CONTENT_LENGTH=24 * 1024 * 1024,
    )
    app.config.update(test_config or {})
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.secret_key = (app.config.get('SECRET_KEY')
        or os.getenv('SMITHPROXY_APPLIANCE_CONSOLE_SECRET')
        or os.getenv('SMITHPROXY_APPLIACE_CONSOLE_SECRET') or secrets.token_hex(32))

    def audit(action, detail=''):
        app.logger.info('audit admin=%s action=%s detail=%s',
                        session.get('admin_id', '-'), action, str(detail)[:1000])

    auth = install_auth(app, audit, modern=True)
    transport = client(app)
    register(app, transport.api, transport.enqueue, audit)
    install_assets(app)
    install_terminals(app, audit)

    @app.get('/')
    @auth.login_required
    def console():
        return render_template('next_shell.html')

    def identifier(value):
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value):
            abort(400, 'Invalid resource ID')
        return value

    @app.get('/api/instances/<instance_id>/<section>')
    @auth.login_required
    def inspection(instance_id, section):
        if section not in ('logs', 'diagnostics'):
            abort(404)
        path = f'/v1/instances/{identifier(instance_id)}/{section}'
        return jsonify(transport.api('GET', path + ('?lines=300' if section == 'logs' else '')))

    @app.get('/configs/<config_id>/download')
    @auth.login_required
    def download_config(config_id):
        item = transport.api('GET', f'/v1/configs/{identifier(config_id)}')
        return download_text(item['content'], f'smithproxy-{config_id[:12]}.cfg')

    @app.get('/instances/<instance_id>/config/download')
    @auth.login_required
    def download_instance_config(instance_id):
        item = transport.api('GET', f'/v1/instances/{identifier(instance_id)}/config')
        return download_text(item['content'], f'smithproxy-instance-{instance_id[:12]}.cfg')

    @app.get('/cert-bundles/<bundle_id>/ca.pem')
    @auth.login_required
    def download_ca(bundle_id):
        item = transport.api('GET', f'/v1/cert-bundles/{identifier(bundle_id)}/ca.pem')
        return download_text(item['content'], f'smithproxy-ca-{bundle_id[:12]}.pem', 'application/x-pem-file')

    def download_text(content, name, mime='text/plain'):
        return send_file(BytesIO(content.encode('utf-8')), mimetype=mime,
                         as_attachment=True, download_name=name, max_age=0)

    @app.get('/test-drives/<drive_id>/files/download')
    @auth.login_required
    def download_drive_file(drive_id):
        path = quote(request.args.get('path', ''), safe='')
        item = transport.api('GET', f'/v1/test-drives/{identifier(drive_id)}/files?path={path}')
        return send_file(BytesIO(base64.b64decode(item['content_base64'], validate=True)),
                         mimetype='application/octet-stream', as_attachment=True,
                         download_name=Path(item['name']).name, max_age=0)

    @app.get('/appliance-exports/<export_id>/download')
    @auth.login_required
    def download_export(export_id):
        upstream = transport.download(f'/v1/appliance-exports/{identifier(export_id)}/download')

        @stream_with_context
        def chunks():
            try:
                while chunk := upstream.read(1024 * 1024):
                    yield chunk
            finally:
                upstream.close()

        headers = {'Cache-Control': 'private, no-store', 'Content-Disposition':
            upstream.headers.get('Content-Disposition', f'attachment; filename="{export_id}.tar.gz"')}
        if upstream.headers.get('Content-Length'):
            headers['Content-Length'] = upstream.headers['Content-Length']
        response = Response(chunks(), mimetype='application/gzip', headers=headers)
        response.call_on_close(upstream.close)
        return response

    @app.errorhandler(RuntimeError)
    def runner_error(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(HTTPException)
    def api_http_error(error):
        if request.path.startswith(('/next-api/', '/api/instances/')):
            code = 'csrf' if error.code == 400 and error.description == 'invalid CSRF token' else 'http_error'
            return jsonify(error=error.description, error_code=code), error.code
        return error

    return app
