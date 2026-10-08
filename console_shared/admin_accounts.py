"""Small JSON admin catalogue; no passwords or hashes in API responses."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
import secrets
import uuid

from flask import g, jsonify, request
from werkzeug.security import generate_password_hash


@contextmanager
def account_lock(path, thread_lock):
    # Separate lock inode survives atomic replacement of the JSON catalogue.
    with thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.geteuid() == 0:
                owner = path.stat() if path.exists() else path.parent.stat()
                os.fchown(fd, owner.st_uid, owner.st_gid)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def register(app, load, save, guard, audit):
    def public(item):
        return {key: item.get(key) for key in ('id', 'email', 'disabled', 'created_at', 'password_changed_at')}

    @app.get('/next-api/admins')
    def admin_list():
        if not g.admin:
            return jsonify(error='Authentication required'), 401
        return jsonify(items=[public(item) for item in load()], current_id=g.admin['id'])

    @app.route('/next-api/admins', methods=['POST'])
    @app.route('/next-api/admins/<admin_id>', methods=['PUT', 'DELETE'])
    def admin_write(admin_id=''):
        if not g.admin:
            return jsonify(error='Authentication required'), 401
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error='Expected object'), 400
        with guard():
            admins = load()
            current = next((a for a in admins if a['id'] == g.admin['id']), None)
            if not current or current.get('disabled') or current.get('auth_version', '') != g.admin.get('auth_version', ''):
                return jsonify(error='Session expired'), 401
            item = next((a for a in admins if a['id'] == admin_id), None) if admin_id else None
            if admin_id and not item:
                return jsonify(error='Account not found'), 404
            if request.method == 'DELETE':
                if admin_id == current['id']:
                    return jsonify(error='Cannot delete your own account'), 409
                if not item.get('disabled') and sum(not a.get('disabled') for a in admins) <= 1:
                    return jsonify(error='Last active administrator must be retained'), 409
                admins.remove(item)
                save(admins)
                audit('admin.delete', admin_id)
                return jsonify(deleted=admin_id)
            email = body.get('email', item.get('email') if item else '')
            password = body.get('password', '')
            disabled = body.get('disabled', item.get('disabled', False) if item else False)
            if not isinstance(email, str) or not isinstance(password, str) or not isinstance(disabled, bool):
                return jsonify(error='Invalid account fields'), 400
            email = email.strip().lower()
            if len(email) > 254 or '@' not in email or any(c.isspace() or ord(c) < 32 for c in email):
                return jsonify(error='Invalid email'), 400
            if any(a['id'] != admin_id and a['email'].lower() == email for a in admins):
                return jsonify(error='Email already exists'), 409
            if (not item or password) and not 12 <= len(password) <= 1024:
                return jsonify(error='Password must contain 12–1024 characters'), 400
            if admin_id == current['id'] and (disabled or password):
                return jsonify(error='Use Preferences to change your password; self-deactivation is not allowed'), 409
            if item and disabled and not item.get('disabled') and sum(not a.get('disabled') for a in admins) <= 1:
                return jsonify(error='Last active administrator must be retained'), 409
            created = item is None
            if created:
                item = {'id': str(uuid.uuid4()), 'created_at': datetime.now(timezone.utc).isoformat()}
                admins.append(item)
            if password:
                item['password_hash'] = generate_password_hash(password)
                item['password_changed_at'] = datetime.now(timezone.utc).isoformat()
            if password or disabled != item.get('disabled', False):
                item['auth_version'] = secrets.token_urlsafe(24)
            item.update(email=email, disabled=disabled)
            save(admins)
            audit('admin.create' if created else 'admin.update', item['id'])
            return jsonify(public(item)), 201 if created else 200
