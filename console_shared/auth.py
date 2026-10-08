import json
import os
import secrets
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
import click
from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from .admin_accounts import account_lock, register as register_admin_accounts
from .i18n import LANGUAGES, translate

def install(app, audit, *, modern=False):
    admin_lock = threading.RLock()
    def admin_guard():
        return account_lock(Path(app.config['ADMIN_FILE']), admin_lock)

    def tr(key):
        return translate(getattr(g, "locale", session.get("locale", "cs")), key)

    def load_admins():
        path = Path(app.config["ADMIN_FILE"])
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read admin JSON: {exc}") from exc
        admins = document.get("admins") if isinstance(document, dict) else None
        if not isinstance(admins, list):
            raise RuntimeError("admin JSON must contain an admins array")
        return [item for item in admins if isinstance(item, dict)]

    def save_admins(admins):
        path = Path(app.config["ADMIN_FILE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        owner = path.stat() if path.exists() else path.parent.stat()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"admins": admins}, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        if os.geteuid() == 0:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        temporary.replace(path)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.admin:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def security():
        requested_locale = session.get("locale")
        if requested_locale not in LANGUAGES:
            best = request.accept_languages.best_match(list(LANGUAGES))
            requested_locale = best or "cs"
        g.locale = requested_locale
        g.admin = None
        if session.get("admin_id"):
            g.admin = next(
                (item for item in load_admins()
                 if str(item.get("id")) == str(session["admin_id"])), None
            )
            if g.admin and (g.admin.get('disabled') or session.get('auth_version', '') != g.admin.get('auth_version', '')):
                session.pop('admin_id', None)
                g.admin = None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "login":
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not session.get('csrf_token') or not secrets.compare_digest(supplied, session['csrf_token']):
                abort(400, "invalid CSRF token")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # xterm.js computes glyph dimensions at runtime and applies them through
        # generated <style> blocks and style attributes. Scripts remain
        # restricted to same-origin files; only CSS needs inline permission.
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            admin = next((item for item in load_admins()
                          if item.get("email") == request.form.get("email", "").lower()), None)
            if admin and not admin.get('disabled') and check_password_hash(admin["password_hash"], request.form.get("password", "")):
                locale = g.locale
                session.clear(); session["locale"] = locale
                session["admin_id"] = admin["id"]; session["csrf_token"] = secrets.token_urlsafe(24)
                session['auth_version'] = admin.get('auth_version', '')
                audit("admin.login", admin["email"])
                return redirect(url_for("console"))
            flash(tr("login.invalid"), "error")
        return render_template("next_login.html" if modern else "login.html")

    @app.post("/language/<locale_code>")
    def set_language(locale_code):
        if locale_code not in LANGUAGES:
            abort(404)
        session["locale"] = locale_code
        target = request.form.get("next", "/")
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("console") if g.admin else url_for("login")
        return redirect(target)

    @app.post("/logout")
    def logout():
        locale = g.locale
        session.clear()
        session["locale"] = locale
        return redirect(url_for("login"))

    @login_required
    def preferences():
        json_mode = request.path == '/next-api/preferences'
        if request.method == "POST":
            values = (request.get_json(silent=True) or {}) if json_mode else request.form
            if not isinstance(values, dict) and json_mode:
                return jsonify(error='Expected object'), 400
            current = values.get("current_password", "")
            new = values.get("new_password", "")
            confirmation = values.get("confirm_password", "")
            if not all(isinstance(value, str) for value in (current, new, confirmation)):
                return jsonify(error='Expected password strings'), 400
            if any(len(value) > 1024 for value in (current, new, confirmation)):
                if json_mode:
                    return jsonify(error='Password exceeds 1024 characters'), 400
                abort(400, 'Password exceeds 1024 characters')
            error = ''
            if not check_password_hash(g.admin.get("password_hash", ""), current):
                error = tr("preferences.wrong_password")
            elif len(new) < 12:
                error = tr("preferences.password_short")
            elif new != confirmation:
                error = tr("preferences.password_mismatch")
            elif check_password_hash(g.admin.get("password_hash", ""), new):
                error = tr("preferences.password_same")
            else:
                with admin_guard():
                    admins = load_admins()
                    admin = next((item for item in admins
                                  if str(item.get("id")) == str(g.admin.get("id"))), None)
                    if not admin:
                        abort(409, "administrator account no longer exists")
                    if admin.get('disabled') or admin.get('auth_version', '') != g.admin.get('auth_version', ''):
                        if json_mode:
                            return jsonify(error='Session expired'), 401
                        abort(401, 'Session expired')
                    # Revalidate under the same lock used for the write. This
                    # avoids overwriting a concurrent password change.
                    if not check_password_hash(admin.get("password_hash", ""), current):
                        if json_mode:
                            return jsonify(error=tr('preferences.wrong_password')), 400
                        flash(tr("preferences.wrong_password"), "error")
                        return render_template("preferences.html"), 400
                    admin["password_hash"] = generate_password_hash(new)
                    admin['auth_version'] = secrets.token_urlsafe(24)
                    session['auth_version'] = admin['auth_version']
                    admin["password_changed_at"] = datetime.now(timezone.utc).isoformat()
                    save_admins(admins)
                audit("admin.password.change", g.admin.get("email", ""))
                session["csrf_token"] = secrets.token_urlsafe(24)
                if json_mode:
                    return jsonify(message=tr('preferences.password_changed'), csrf=session['csrf_token'])
                flash(tr("preferences.password_changed"), "success")
                return redirect(url_for("preferences"))
            if json_mode:
                return jsonify(error=error), 400
            flash(error, 'error')
            return render_template("preferences.html"), 400
        return render_template("preferences.html")

    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.password_option()
    def create_admin(email, password):
        if len(password) < 12: raise click.ClickException("heslo musí mít alespoň 12 znaků")
        normalized = email.lower().strip()
        with admin_guard():
            admins = load_admins()
            if any(item.get("email") == normalized for item in admins):
                raise click.ClickException("admin už existuje")
            admins.append({
                "id": secrets.token_hex(16), "email": normalized,
                "password_hash": generate_password_hash(password),
            })
            save_admins(admins)
        click.echo("Admin vytvořen.")

    app.add_url_rule('/next-api/preferences' if modern else '/preferences',
                     view_func=preferences, methods=['POST'] if modern else ['GET', 'POST'])
    @app.context_processor
    def auth_context():
        return {"csrf_token": session.setdefault("csrf_token", secrets.token_urlsafe(24)),
                "_": tr, "locale": g.locale, "languages": LANGUAGES}
    if modern:
        register_admin_accounts(app, load_admins, save_admins, admin_guard, audit)
    return SimpleNamespace(tr=tr, login_required=login_required)
