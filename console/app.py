from __future__ import annotations

import json
import base64
import os
import secrets
import threading
from io import BytesIO
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import click
from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_sock import Sock
from websockets.sync.client import connect as websocket_connect
from werkzeug.security import check_password_hash, generate_password_hash


def partition_branches(branches, *, now=None, attic_days=365):
    """Keep old remote refs available without crowding the active catalogue."""
    current = now or datetime.now(timezone.utc)
    active, attic = [], []
    for source in branches:
        item = dict(source)
        try:
            committed = datetime.fromisoformat(str(item.get("commit_at", "")).replace("Z", "+00:00"))
            if committed.tzinfo is None:
                committed = committed.replace(tzinfo=timezone.utc)
            item["age_days"] = max(0, int((current - committed).total_seconds() // 86400))
        except (TypeError, ValueError):
            item["age_days"] = None
        (attic if item["age_days"] is not None and item["age_days"] > attic_days else active).append(item)
    return active, attic


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        ADMIN_FILE=os.getenv(
            "SMITHPROXY_APPLIANCE_CONSOLE_ADMINS",
            os.getenv(
                "SMITHPROXY_APPLIACE_CONSOLE_ADMINS",
                str(Path(app.instance_path) / "admins.json"),
            ),
        ),
        RUNNER_URL=os.getenv("CAPTURE_RUNNER_URL", "http://127.0.0.1:9080"),
        RUNNER_WS_URL=os.getenv("CAPTURE_RUNNER_WS_URL", "ws://127.0.0.1:9081"),
        RUNNER_TOKEN=os.getenv("CZ_RUNNER_TOKEN", ""),
        RUNNER_TIMEOUT=float(os.getenv("CAPTURE_RUNNER_TIMEOUT", "5")),
        SEND_FILE_MAX_AGE_DEFAULT=0,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=24 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    sock = Sock(app)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.secret_key = (
        app.config.get("SECRET_KEY")
        or os.getenv("SMITHPROXY_APPLIANCE_CONSOLE_SECRET")
        or os.getenv("SMITHPROXY_APPLIACE_CONSOLE_SECRET")
        or secrets.token_hex(32)
    )

    admin_lock = threading.RLock()

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
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"admins": admins}, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def api(method, path, payload=None, request_timeout=None):
        token = app.config["RUNNER_TOKEN"]
        if not token:
            raise RuntimeError("CZ_RUNNER_TOKEN is not configured")
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(app.config["RUNNER_URL"].rstrip("/") + path, data=body, method=method,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            # Namespace creation and graceful systemd shutdown can legitimately
            # take longer than status polling.  Keep GET failures responsive,
            # but do not abandon a mutating operation while it is still being
            # completed by the runner.
            timeout = (
                float(request_timeout) if request_timeout is not None
                else app.config["RUNNER_TIMEOUT"] if method == "GET"
                else max(90, app.config["RUNNER_TIMEOUT"])
            )
            with urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get("error", str(exc))
            except Exception:
                message = str(exc)
            raise RuntimeError(message) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise RuntimeError(f"runner unavailable: {exc}") from exc

    def enqueue(method, path, payload, label, kind="runner-action"):
        return api("POST", "/v1/task-actions", {
            "method": method, "path": path, "payload": payload,
            "label": label, "kind": kind,
        })

    def flash_queued(result, message="Akce zařazena"):
        prefix = "Stejná akce už ve frontě je" if result.get("deduplicated") else message
        flash(f"{prefix}: úloha {result.get('task_id', '')[:8]}.", "success")

    def audit(action, detail=""):
        app.logger.info("audit admin=%s action=%s detail=%s",
                        session.get("admin_id", "-"), action, str(detail)[:1000])

    def artifact_time_view(item):
        result = dict(item)
        now = datetime.now(timezone.utc)
        parsed = {}
        for key in ("built_at", "commit_at"):
            try:
                parsed[key] = datetime.fromisoformat(str(item.get(key, "")).replace("Z", "+00:00"))
                if parsed[key].tzinfo is None:
                    parsed[key] = parsed[key].replace(tzinfo=timezone.utc)
                result[f"{key}_display"] = parsed[key].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                result[f"{key}_age_days"] = max(0, int((now - parsed[key]).total_seconds() // 86400))
            except (TypeError, ValueError):
                result[f"{key}_display"] = "neznámé"
                result[f"{key}_age_days"] = None
        if "built_at" in parsed and "commit_at" in parsed:
            result["build_lag_days"] = max(
                0, int((parsed["built_at"] - parsed["commit_at"]).total_seconds() // 86400)
            )
        else:
            result["build_lag_days"] = None
        return result

    def artifact_library_view(items):
        artifacts = [artifact_time_view(item) for item in items]
        groups = {}
        for item in artifacts:
            key = (item.get("ref", ""), item.get("build_type", "Release"))
            try:
                commit_at = datetime.fromisoformat(
                    str(item.get("commit_at", "")).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                continue
            groups.setdefault(key, []).append((commit_at, item))
        for candidates in groups.values():
            newest_at, newest = max(candidates, key=lambda candidate: candidate[0])
            for commit_at, item in candidates:
                item["newer_build_available"] = commit_at < newest_at
                item["code_behind_days"] = max(
                    0, int((newest_at - commit_at).total_seconds() // 86400)
                )
                item["newest_build_id"] = newest.get("build_id", newest.get("commit_id", ""))
                item["newest_commit_id"] = newest.get("commit_id", "")
        return artifacts

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.admin:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def security():
        g.admin = None
        if session.get("admin_id"):
            g.admin = next(
                (item for item in load_admins()
                 if str(item.get("id")) == str(session["admin_id"])), None
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "login":
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied, session.get("csrf_token", "")):
                abort(400, "invalid CSRF token")

    @app.context_processor
    def globals_():
        return {"csrf_token": session.setdefault("csrf_token", secrets.token_urlsafe(24))}

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
            if admin and check_password_hash(admin["password_hash"], request.form.get("password", "")):
                session.clear(); session["admin_id"] = admin["id"]; session["csrf_token"] = secrets.token_urlsafe(24)
                audit("admin.login", admin["email"])
                return redirect(url_for("console"))
            flash("Neplatné přihlášení.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def console():
        error = None
        try:
            status = api("GET", "/v1/status")
            instances = api("GET", "/v1/instances")["instances"]
            sources = api("GET", "/v1/sources")["sources"]
            runtime_profiles = api("GET", "/v1/runtime-profiles")["profiles"]
        except RuntimeError as exc:
            status, instances, sources, runtime_profiles, error = {
                "status": "unavailable",
                "build": {"state": "unavailable", "revision": "", "log": ""},
                "instances": {"running": 0},
            }, [], [], [], str(exc)
        return render_template(
            "console.html", status=status, instances=instances, sources=sources,
            runtime_profiles=runtime_profiles, error=error,
        )

    @app.get("/binaries")
    @login_required
    def binaries():
        try:
            status = api("GET", "/v1/status")
            status["build"]["artifacts"] = artifact_library_view(
                status["build"].get("artifacts", [])
            )
            refs = status["build"].setdefault("refs", {})
            active, attic = partition_branches(refs.get("branches", []))
            refs["active_branches"] = active
            refs["attic_branches"] = attic
            return render_template("binaries.html", status=status, error=None)
        except RuntimeError as exc:
            return render_template(
                "binaries.html", status={"build": {"state": "unavailable", "artifacts": []}},
                error=str(exc),
            )

    @app.get("/test-drives")
    @login_required
    def test_drive_list():
        try:
            drives = api("GET", "/v1/test-drives")["test_drives"]
            return render_template("test_drives.html", drives=drives, drive=None,
                                   files=[], logs="", artifacts=[], error=None)
        except RuntimeError as exc:
            return render_template("test_drives.html", drives=[], drive=None,
                                   files=[], logs="", artifacts=[], error=str(exc))

    @app.get("/test-drives/<drive_id>")
    @login_required
    def test_drive_detail(drive_id):
        try:
            encoded = quote(drive_id, safe="")
            drive = api("GET", f"/v1/test-drives/{encoded}")
            drives = api("GET", "/v1/test-drives")["test_drives"]
            files = api("GET", f"/v1/test-drives/{encoded}/files")["files"]
            logs = api("GET", f"/v1/test-drives/{encoded}/logs").get("output", "")
            status = api("GET", "/v1/status")
            artifacts = status.get("build", {}).get("artifacts", [])
            return render_template("test_drives.html", drives=drives, drive=drive,
                                   files=files, logs=logs, artifacts=artifacts, error=None)
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("test_drive_list"))

    @app.post("/test-drives")
    @login_required
    def start_test_drive():
        try:
            build_id = request.form.get("build_id", "")
            ttl = int(request.form.get("ttl_seconds", "1800"))
            result = enqueue("POST", "/v1/test-drives", {
                "build_id": build_id, "ttl_seconds": ttl,
                "config_mode": "rw" if request.form.get("config_rw") == "yes" else "ro",
            }, f"Spustit Test Drive {build_id[:12]}", "test-drive-spawn")
            audit("test-drive.start", f"{build_id}:{result.get('task_id', '')}")
            flash_queued(result, "Test Drive zařazen")
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("binaries"))

    @app.post("/test-drives/<drive_id>/destroy")
    @login_required
    def destroy_test_drive(drive_id):
        try:
            result = enqueue(
                "DELETE", f"/v1/test-drives/{quote(drive_id, safe='')}", None,
                f"Zničit Test Drive {drive_id[:12]}", "test-drive-destroy",
            )
            audit("test-drive.destroy", drive_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_list"))

    @app.post("/test-drives/<drive_id>/upgrade")
    @login_required
    def upgrade_test_drive(drive_id):
        build_id = request.form.get("build_id", "")
        try:
            result = enqueue(
                "POST", f"/v1/test-drives/{quote(drive_id, safe='')}/upgrade",
                {"build_id": build_id},
                f"Dirty upgrade Test Drive {drive_id[:12]} → {build_id[:12]}",
                "test-drive-upgrade",
            )
            audit("test-drive.upgrade", f"{drive_id}:{build_id}")
            flash_queued(result, "Dirty upgrade zařazen")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.post("/test-drives/<drive_id>/extend")
    @login_required
    def extend_test_drive(drive_id):
        try:
            seconds = int(request.form.get("additional_seconds", "1800"))
            result = enqueue(
                "POST", f"/v1/test-drives/{quote(drive_id, safe='')}/extend",
                {"additional_seconds": seconds},
                f"Prodloužit Test Drive {drive_id[:12]} o {seconds} s",
                "test-drive-extend",
            )
            audit("test-drive.extend", f"{drive_id}:{seconds}")
            flash_queued(result, "Prodloužení Test Drive zařazeno")
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.post("/test-drives/<drive_id>/restart")
    @login_required
    def restart_test_drive(drive_id):
        try:
            result = enqueue(
                "POST", f"/v1/test-drives/{quote(drive_id, safe='')}/restart", {},
                f"Znovu spustit Test Drive {drive_id[:12]}", "test-drive-restart",
            )
            audit("test-drive.restart", drive_id)
            flash_queued(result, "Restart Test Drive zařazen")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.post("/test-drives/<drive_id>/config-mode")
    @login_required
    def set_test_drive_config_mode(drive_id):
        mode = request.form.get("config_mode", "ro")
        try:
            result = enqueue(
                "POST", f"/v1/test-drives/{quote(drive_id, safe='')}/config-mode",
                {"config_mode": mode},
                f"Test Drive {drive_id[:12]} config {mode.upper()}",
                "test-drive-config-mode",
            )
            audit("test-drive.config-mode", f"{drive_id}:{mode}")
            flash_queued(result, "Změna config režimu zařazena")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.post("/test-drives/<drive_id>/config-preview")
    @login_required
    def save_test_drive_config(drive_id):
        try:
            result = enqueue(
                "POST", f"/v1/test-drives/{quote(drive_id, safe='')}/config/preview",
                {
                    "name": request.form.get("name", "").strip(),
                    "description": request.form.get("description", ""),
                },
                f"Extrahovat config Test Drive {drive_id[:12]}",
                "test-drive-config-preview",
            )
            audit("test-drive.config-preview", drive_id)
            flash_queued(result, "Extrakce configu zařazena")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.post("/test-drives/<drive_id>/files")
    @login_required
    def upload_test_drive_file(drive_id):
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            flash("Vyber soubor.", "error")
            return redirect(url_for("test_drive_detail", drive_id=drive_id))
        path = request.form.get("path", "").strip() or Path(uploaded.filename).name
        try:
            api("POST", f"/v1/test-drives/{quote(drive_id, safe='')}/files", {
                "path": path,
                "content_base64": base64.b64encode(uploaded.read()).decode("ascii"),
            })
            audit("test-drive.upload", f"{drive_id}:{path}")
            flash(f"Nahráno do /work/{path}.", "success")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.get("/test-drives/<drive_id>/files/download")
    @login_required
    def download_test_drive_file(drive_id):
        path = request.args.get("path", "")
        try:
            result = api(
                "GET", f"/v1/test-drives/{quote(drive_id, safe='')}/files"
                f"?path={quote(path, safe='')}",
            )
            content = base64.b64decode(result["content_base64"], validate=True)
            # Python repr() uses apostrophes, but Content-Disposition doesn't:
            # browsers treated those delimiters as part of the downloaded name.
            return send_file(
                BytesIO(content), mimetype="application/octet-stream",
                as_attachment=True, download_name=Path(result["name"]).name,
                max_age=0,
            )
        except (RuntimeError, ValueError, KeyError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("test_drive_detail", drive_id=drive_id))

    @app.get("/runtime-profiles")
    @login_required
    def runtime_profiles():
        error = None
        try:
            status = api("GET", "/v1/status")
            profiles = api("GET", "/v1/runtime-profiles")["profiles"]
            bundles = api("GET", "/v1/cert-bundles")["bundles"]
        except RuntimeError as exc:
            status, profiles, bundles, error = {"build": {"artifacts": [], "configs": []}}, [], [], str(exc)
        return render_template(
            "runtime_profiles.html", status=status, profiles=profiles, bundles=bundles, error=error
        )

    @app.get("/settings")
    @login_required
    def settings():
        try:
            networking = api("GET", "/v1/settings/networking")
            return render_template("settings.html", networking=networking, error=None)
        except RuntimeError as exc:
            return render_template("settings.html", networking={}, error=str(exc))

    @app.post("/settings/networking")
    @login_required
    def update_networking_settings():
        try:
            source_ips = [
                line.strip() for line in request.form.get("authorized_source_ips", "").splitlines()
                if line.strip()
            ]
            result = enqueue("PUT", "/v1/settings/networking", {
                "namespace_cidr": request.form.get("namespace_cidr", ""),
                "allocation_prefix": 30,
                "egress_mode": request.form.get("egress_mode", "masquerade"),
                "sas_route_via": request.form.get("sas_route_via", ""),
                "sas_interface": request.form.get("sas_interface", ""),
                "route_table_start": int(request.form.get("route_table_start", "60000")),
                "mark_start": int(request.form.get("mark_start", "268435456"), 0),
                "authorized_source_ips": source_ips,
            }, "Uložit síťová a routing nastavení", "network-settings-update")
            audit("settings.networking.update", result.get("task_id", ""))
            flash_queued(result)
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings"))

    @app.post("/runtime-profiles")
    @login_required
    def create_runtime_profile():
        try:
            ttl_seconds = (
                None if request.form.get("ttl_unlimited") == "on"
                else int(request.form.get("ttl_seconds", "1800"))
            )
            result = enqueue("POST", "/v1/runtime-profiles", {
                "name": request.form.get("name", ""),
                "build_id": request.form.get("build_id", ""),
                "config_id": request.form.get("config_id", ""),
                "cert_bundle_id": request.form.get("cert_bundle_id", ""),
                "auto_restart": request.form.get("auto_restart") == "on",
                "ttl_seconds": ttl_seconds,
            }, "Vytvořit runtime profil", "profile-create")
            audit("runtime-profile.create", result.get("task_id", ""))
            flash_queued(result)
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("runtime_profiles"))

    @app.route("/runtime-profiles/<profile_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_runtime_profile(profile_id):
        try:
            profile = api("GET", f"/v1/runtime-profiles/{quote(profile_id, safe='')}")
            status = api("GET", "/v1/status")
            bundles = api("GET", "/v1/cert-bundles")["bundles"]
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("runtime_profiles"))
        if request.method == "GET":
            return render_template(
                "runtime_profile_editor.html", profile=profile, status=status,
                bundles=bundles, error=None,
            )
        values = {}
        try:
            values = {
                "name": request.form.get("name", ""),
                "build_id": request.form.get("build_id", ""),
                "config_id": request.form.get("config_id", ""),
                "cert_bundle_id": request.form.get("cert_bundle_id", ""),
                "auto_restart": request.form.get("auto_restart") == "on",
                "ttl_seconds": (
                    None if request.form.get("ttl_unlimited") == "on"
                    else int(request.form.get("ttl_seconds", "1800"))
                ),
            }
            result = enqueue("PUT", f"/v1/runtime-profiles/{quote(profile_id, safe='')}", values,
                             f"Upravit runtime profil {profile_id[:12]}", "profile-update")
            audit("runtime-profile.edit", profile_id)
            flash_queued(result)
            return redirect(url_for("runtime_profiles"))
        except (RuntimeError, ValueError) as exc:
            return render_template(
                "runtime_profile_editor.html", profile={**profile, **values}, status=status,
                bundles=bundles, error=str(exc),
            ), 400

    @app.get("/cert-bundles")
    @login_required
    def cert_bundles():
        try:
            bundles = api("GET", "/v1/cert-bundles")["bundles"]
            return render_template("cert_bundles.html", bundles=bundles, error=None)
        except RuntimeError as exc:
            return render_template("cert_bundles.html", bundles=[], error=str(exc))

    @app.post("/cert-bundles")
    @login_required
    def create_cert_bundle():
        try:
            result = enqueue("POST", "/v1/cert-bundles", {
                "name": request.form.get("name", ""),
                "common_name": request.form.get("common_name", ""),
                "days": int(request.form.get("days", "3650")),
            }, "Vygenerovat CA bundle", "cert-bundle-create")
            audit("cert-bundle.create", result.get("task_id", ""))
            flash_queued(result)
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("cert_bundles"))

    @app.post("/cert-bundles/<bundle_id>/certificates/generate")
    @login_required
    def generate_bundle_certificate(bundle_id):
        try:
            result = enqueue("POST", f"/v1/cert-bundles/{quote(bundle_id, safe='')}/certificates", {
                "action": "generate", "name": request.form.get("name", ""),
                "common_name": request.form.get("common_name", ""),
                "days": int(request.form.get("days", "825")),
                "file_stem": request.form.get("file_stem", ""),
            }, f"Vygenerovat certifikát v bundle {bundle_id[:12]}", "certificate-generate")
            audit("cert-bundle.certificate-generate", result.get("task_id", ""))
            flash_queued(result)
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("cert_bundles"))

    @app.post("/cert-bundles/<bundle_id>/certificates/import")
    @login_required
    def import_bundle_certificate(bundle_id):
        try:
            uploaded = request.files.get("certificate")
            if not uploaded or not uploaded.filename:
                raise RuntimeError("Vyber PEM certifikát.")
            raw = uploaded.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise RuntimeError("Certifikát je větší než 256 KiB.")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("Certifikát musí být PEM v UTF-8/ASCII.") from exc
            result = enqueue("POST", f"/v1/cert-bundles/{quote(bundle_id, safe='')}/certificates", {
                "action": "import",
                "name": request.form.get("name", "").strip() or uploaded.filename,
                "content": content,
                "filename": request.form.get("filename", "").strip() or uploaded.filename,
            }, f"Importovat certifikát do bundle {bundle_id[:12]}", "certificate-import")
            audit("cert-bundle.certificate-import", result.get("task_id", ""))
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("cert_bundles"))

    @app.get("/cert-bundles/<bundle_id>/ca.pem")
    @login_required
    def download_ca_certificate(bundle_id):
        try:
            result = api("GET", f"/v1/cert-bundles/{quote(bundle_id, safe='')}/ca.pem")
            return Response(
                result["content"], mimetype="application/x-pem-file",
                headers={"Content-Disposition": f'attachment; filename="smithproxy-ca-{bundle_id[:12]}.pem"'},
            )
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("cert_bundles"))

    @app.post("/cert-bundles/<bundle_id>/delete")
    @login_required
    def delete_cert_bundle(bundle_id):
        try:
            result = enqueue("DELETE", f"/v1/cert-bundles/{quote(bundle_id, safe='')}", None,
                             f"Smazat CA bundle {bundle_id[:12]}", "cert-bundle-delete")
            audit("cert-bundle.delete", bundle_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("cert_bundles"))

    @app.post("/runtime-profiles/<profile_id>/delete")
    @login_required
    def delete_runtime_profile(profile_id):
        try:
            result = enqueue("DELETE", f"/v1/runtime-profiles/{quote(profile_id, safe='')}", None,
                             f"Smazat runtime profil {profile_id[:12]}", "profile-delete")
            audit("runtime-profile.delete", profile_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("runtime_profiles"))

    @app.get("/configs")
    @login_required
    def configs():
        error = None
        try:
            status = api("GET", "/v1/status")
            items = api("GET", "/v1/configs")["configs"]
            instances = api("GET", "/v1/instances")["instances"]
        except RuntimeError as exc:
            status, items, instances, error = {"build": {"artifacts": []}}, [], [], str(exc)
        builtin_configs = [item for item in items if item.get("source_kind") in {"active", "preset"}]
        build_kinds = {"build", "native-build-default"}
        build_configs = [item for item in items if item.get("source_kind") in build_kinds]
        custom_configs = [
            item for item in items
            if item.get("source_kind") not in {"active", "preset", *build_kinds}
        ]
        return render_template(
            "configs.html", status=status, configs=items, instances=instances, error=error,
            builtin_configs=builtin_configs, build_configs=build_configs,
            custom_configs=custom_configs,
        )

    @app.route("/config-observer", methods=["GET", "POST"])
    @login_required
    def config_observer():
        result = None
        error = None
        status = {"build": {"artifacts": []}}
        configs = []
        selected_build = request.form.get("build_id", "")
        selected_config = request.form.get("config_id", "")
        try:
            status = api("GET", "/v1/status")
            configs = api("GET", "/v1/configs")["configs"]
            status["build"]["artifacts"] = artifact_library_view(
                status["build"].get("artifacts", [])
            )
            if request.method == "POST":
                task = enqueue("POST", "/v1/config-observer", {
                    "build_id": selected_build,
                    "config_id": selected_config,
                }, "Porovnat nativní konfigurace", "config-observer")
                audit("config.observe", f"{selected_build}:{selected_config}")
                flash_queued(task, "Observer zařazen")
                return redirect(url_for("config_observer"))
        except RuntimeError as exc:
            error = str(exc)
        return render_template(
            "config_observer.html", status=status, configs=configs, result=result,
            error=error, selected_build=selected_build, selected_config=selected_config,
        ), (400 if error and request.method == "POST" else 200)

    @app.post("/build")
    @login_required
    def build():
        try:
            ref = request.form.get("ref", "master")
            build_type = request.form.get("build_type", "Release")
            result = api("POST", "/v1/build", {
                "ref": ref,
                "build_type": build_type,
            }, request_timeout=app.config["RUNNER_TIMEOUT"])
            audit("build.start", result.get("task_id", ""))
            message = (
                f"'{ref}' {build_type} build is already enqueued"
                if result.get("deduplicated")
                else f"'{ref}' {build_type} build task enqueued"
            )
            if request.headers.get("X-Requested-With") == "task-fetch":
                return jsonify({**result, "message": message}), 202
            flash(f"{message} · task {result.get('task_id', '')[:8]}", "success")
        except RuntimeError as exc:
            if request.headers.get("X-Requested-With") == "task-fetch":
                return jsonify(error=str(exc)), 400
            flash(str(exc), "error")
        return redirect(url_for("binaries"))

    @app.post("/binaries/refresh-branches")
    @login_required
    def refresh_branches():
        try:
            result = api("POST", "/v1/refs/refresh", {})
            audit("refs.refresh", result.get("task_id", ""))
            flash_queued(result, "Git fetch zařazen")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("binaries"))

    @app.post("/binaries/<build_id>/delete")
    @login_required
    def delete_binary(build_id):
        try:
            result = enqueue("DELETE", f"/v1/builds/{quote(build_id, safe='')}", None,
                             f"Smazat build {build_id[:12]}", "build-delete")
            audit("binary.delete", build_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("binaries"))

    @app.post("/binaries/<build_id>/extract-default-config")
    @login_required
    def extract_default_config(build_id):
        try:
            result = enqueue(
                "POST", f"/v1/builds/{quote(build_id, safe='')}/config/preview", {},
                f"Extrahovat default config z {build_id[:12]}", "build-config-preview",
            )
            audit("binary.extract-default-config", build_id)
            flash_queued(result, "Extrakce configu zařazena")
            return redirect(url_for("binaries"))
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("binaries"))

    @app.post("/instances")
    @login_required
    def create_instance():
        try:
            result = api("POST", "/v1/instances", {
                "source_ip": request.form.get("source_ip", ""),
                "build_id": request.form.get("build_id", "active"),
                "config_id": request.form.get("config_id", "active"),
                "runtime_profile_id": request.form.get("runtime_profile_id", ""),
                "user_id": request.form.get("user_id", "admin-console"),
                "template_values": {
                    key.removeprefix("placeholder__"): value
                    for key, value in request.form.items()
                    if key.startswith("placeholder__")
                },
                "config_mode": request.form.get("config_mode", "ro"),
                "persistent": request.form.get("persistent") == "on",
                "runtime_seconds": int(request.form.get("runtime_seconds", "3600")),
                "parameters": {"socks_port": 1080, "plaintext_port": 50080, "tls_port": 50443,
                               "http_port": 3128, "cli_port": 50000,
                               "workers": 1, "pcap_quota_mb": 100},
            }, request_timeout=app.config["RUNNER_TIMEOUT"])
            audit("instance.create", result.get("task_id", ""))
            message = "Stejné spuštění už ve frontě je" if result.get("deduplicated") else "Spuštění instance zařazeno"
            if request.headers.get("X-Requested-With") == "task-fetch":
                return jsonify({**result, "message": message}), 202
            flash(f"{message}: úloha {result.get('task_id', '')[:8]}.", "success")
        except (RuntimeError, ValueError) as exc:
            if request.headers.get("X-Requested-With") == "task-fetch":
                return jsonify(error=str(exc)), 400
            flash(str(exc), "error")
        return redirect(url_for("console"))

    @app.post("/instances/<instance_id>/extend")
    @login_required
    def extend_instance(instance_id):
        try:
            seconds = int(request.form.get("additional_seconds", "1800"))
            result = api("POST", f"/v1/instances/{quote(instance_id, safe='')}/extend", {
                "additional_seconds": seconds,
            })
            audit("instance.extend", f"{instance_id}:{seconds}")
            flash(f"Prodloužení zařazeno: úloha {result.get('task_id', '')[:8]}.", "success")
        except (RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("console") + f"?instance={quote(instance_id, safe='')}")

    @app.post("/instances/<instance_id>/stop")
    @login_required
    def stop_instance(instance_id):
        try:
            result = enqueue("DELETE", f"/v1/instances/{quote(instance_id, safe='')}", None,
                             f"Zastavit instanci {instance_id[:12]}", "instance-stop")
            audit("instance.stop", instance_id); flash_queued(result)
        except RuntimeError as exc: flash(str(exc), "error")
        return redirect(url_for("console"))

    @app.post("/instances/<instance_id>/restart")
    @login_required
    def restart_instance(instance_id):
        try:
            result = enqueue("POST", f"/v1/instances/{quote(instance_id, safe='')}/restart", {},
                             f"Restartovat instanci {instance_id[:12]}", "instance-restart")
            audit("instance.restart", instance_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("console"))

    @app.post("/instances/<instance_id>/delete")
    @login_required
    def delete_instance(instance_id):
        try:
            result = enqueue("DELETE", f"/v1/instances/{quote(instance_id, safe='')}/record", None,
                             f"Smazat záznam instance {instance_id[:12]}", "instance-record-delete")
            audit("instance.delete", instance_id); flash_queued(result)
        except RuntimeError as exc: flash(str(exc), "error")
        return redirect(url_for("console"))

    @app.post("/instances/cleanup")
    @login_required
    def cleanup_instances():
        try:
            result = enqueue(
                "POST", "/v1/instances/cleanup", {},
                "Cleanup stopped non-persistent instances", "instance-cleanup",
            )
            audit("instance.cleanup", result.get("task_id", ""))
            flash_queued(result, "Cleanup zařazen")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("console"))

    @app.post("/configs/<config_id>/delete")
    @login_required
    def delete_config(config_id):
        try:
            result = enqueue("DELETE", f"/v1/configs/{quote(config_id, safe='')}", None,
                             f"Smazat konfiguraci {config_id[:12]}", "config-delete")
            audit("config.delete", config_id); flash_queued(result)
        except RuntimeError as exc: flash(str(exc), "error")
        return redirect(url_for("configs"))

    @app.route("/configs/<config_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_config(config_id):
        task_fetch = request.headers.get("X-Requested-With") == "task-fetch"
        try:
            original = api("GET", f"/v1/configs/{quote(config_id, safe='')}")
            status = api("GET", "/v1/status")
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("configs"))
        if request.method == "GET":
            return render_template(
                "config_editor.html", original=original, status=status,
                content=original["content"], error=None,
            )
        content = request.form.get("content", "")
        form_values = {
            "metadata_name": request.form.get("metadata_name", "").strip() or original["name"],
            "metadata_description": request.form.get(
                "metadata_description", original.get("description", "")
            ),
            "copy_name": request.form.get("copy_name", "").strip(),
            "profile": request.form.get("profile", original.get("profile", "custom")),
            "build_id": request.form.get("build_id", ""),
        }
        try:
            save_mode = request.form.get("save_mode", "replace")
            if save_mode == "metadata":
                result = enqueue(
                    "PUT", f"/v1/configs/{quote(config_id, safe='')}/metadata",
                    {
                        "name": form_values["metadata_name"],
                        "description": form_values["metadata_description"],
                    },
                    f"Upravit metadata configu {config_id[:12]}", "config-metadata",
                )
                flash_queued(result, "Přejmenování configu zařazeno")
                return redirect(url_for("configs"))
            if save_mode == "copy" and not form_values["copy_name"]:
                raise RuntimeError("Pro kopii zadej nový název.")
            if not form_values["build_id"]:
                raise RuntimeError("Vyber validující build.")
            payload = {
                "name": (
                    form_values["copy_name"] if save_mode == "copy" else original["name"]
                ),
                "description": original.get("description", ""),
                "profile": form_values["profile"],
                "build_id": form_values["build_id"],
                "content": content,
                "action": "create" if save_mode == "copy" else "update",
                "config_id": "" if save_mode == "copy" else config_id,
            }
            result = enqueue("POST", "/v1/configs/preview", payload,
                             f"Připravit změnu configu {config_id[:12]}", "config-preview")
            if task_fetch:
                return jsonify({**result, "message": "Validace configu zařazena"}), 202
            flash_queued(result, "Příprava diffu zařazena")
            return redirect(url_for("configs"))
        except RuntimeError as exc:
            if task_fetch:
                return jsonify(error=str(exc)), 400
            return render_template(
                "config_editor.html", original=original, status=status,
                content=content, error=str(exc), form_values=form_values,
            ), 400

    @app.post("/configs/upload")
    @login_required
    def upload_config():
        try:
            uploaded = request.files.get("config_file")
            if not uploaded or not uploaded.filename:
                raise RuntimeError("Vyber konfigurační soubor.")
            raw = uploaded.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("Konfigurace je větší než 1 MiB.")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("Konfigurace musí být UTF-8.") from exc
            result = enqueue("POST", "/v1/configs/preview", {
                "action": "create",
                "name": request.form.get("name", "").strip() or uploaded.filename,
                "description": request.form.get("description", ""),
                "content": content,
                "profile": request.form.get("profile", "custom"),
                "build_id": request.form.get("build_id", ""),
            }, "Importovat a normalizovat konfiguraci", "config-preview")
            flash_queued(result, "Import configu zařazen")
            return redirect(url_for("configs"))
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("configs"))

    @app.post("/configs/native-commit")
    @login_required
    def commit_native_config():
        try:
            result = enqueue("POST", "/v1/configs/commit", {
                "preview_id": request.form.get("preview_id", ""),
                "approved": request.form.get("approved") == "yes",
                "approved_by": str(g.admin.get("email", g.admin.get("id", "admin"))),
            }, "Schválit a uložit nativní konfiguraci", "config-commit")
            audit("config.native-commit", result.get("task_id", ""))
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("configs"))

    @app.post("/configs/native-preview/<preview_id>/cancel")
    @login_required
    def cancel_native_config_preview(preview_id):
        try:
            result = enqueue("DELETE", f"/v1/configs/previews/{quote(preview_id, safe='')}", None,
                             f"Zrušit config preview {preview_id[:12]}", "config-preview-cancel")
            audit("config.native-preview.cancel", preview_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("configs"))

    @app.get("/configs/<config_id>/download")
    @login_required
    def download_config(config_id):
        try:
            result = api("GET", f"/v1/configs/{quote(config_id, safe='')}")
            return Response(
                result["content"], mimetype="text/plain",
                headers={"Content-Disposition": f'attachment; filename="smithproxy-{config_id[:12]}.cfg"'},
            )
        except RuntimeError as exc:
            flash(str(exc), "error"); return redirect(url_for("configs"))

    @app.get("/instances/<instance_id>/config/download")
    @login_required
    def download_instance_config(instance_id):
        try:
            result = api("GET", f"/v1/instances/{quote(instance_id, safe='')}/config")
            return Response(
                result["content"], mimetype="text/plain",
                headers={"Content-Disposition": f'attachment; filename="smithproxy-instance-{instance_id[:12]}.cfg"'},
            )
        except RuntimeError as exc:
            flash(str(exc), "error"); return redirect(url_for("console"))

    @app.get("/instances/<instance_id>/diagnostics")
    @login_required
    def instance_diagnostics(instance_id):
        try:
            diagnostics = api(
                "GET", f"/v1/instances/{quote(instance_id, safe='')}/diagnostics"
            )
            return render_template("instance_diagnostics.html", diagnostics=diagnostics)
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("console"))

    @app.post("/instances/<instance_id>/debug/start")
    @login_required
    def start_instance_debug(instance_id):
        try:
            result = enqueue("POST", f"/v1/instances/{quote(instance_id, safe='')}/debug", {},
                             f"Spustit GDB helper {instance_id[:12]}", "debug-start")
            audit("instance.debug-start", instance_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("console", instance=instance_id, view="diag"))

    @app.post("/instances/<instance_id>/debug/stop")
    @login_required
    def stop_instance_debug(instance_id):
        try:
            result = enqueue("DELETE", f"/v1/instances/{quote(instance_id, safe='')}/debug", None,
                             f"Zastavit GDB helper {instance_id[:12]}", "debug-stop")
            audit("instance.debug-stop", instance_id)
            flash_queued(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("console", instance=instance_id, view="diag"))

    @app.post("/instances/<instance_id>/config/save")
    @login_required
    def save_instance_config(instance_id):
        try:
            result = enqueue("POST", f"/v1/instances/{quote(instance_id, safe='')}/config/preview", {
                "name": request.form.get("name", "").strip() or f"Instance {instance_id[:12]}",
            }, f"Uložit config instance {instance_id[:12]}", "instance-config-preview")
            flash_queued(result, "Uložení a diff configu zařazeny")
            return redirect(url_for("configs"))
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("configs"))

    @app.get("/api/build")
    @login_required
    def build_status():
        try: return jsonify(api("GET", "/v1/build"))
        except RuntimeError as exc: return jsonify(error=str(exc), state="unavailable"), 503

    @app.get("/api/instances")
    @login_required
    def instance_statuses():
        try: return jsonify(api("GET", "/v1/instances"))
        except RuntimeError as exc: return jsonify(error=str(exc)), 503

    @app.get("/api/tasks")
    @login_required
    def task_statuses():
        try: return jsonify(api("GET", "/v1/tasks"))
        except RuntimeError as exc: return jsonify(error=str(exc)), 503

    @app.get("/tasks/<task_id>/result")
    @login_required
    def task_result(task_id):
        try:
            task = api("GET", f"/v1/tasks/{quote(task_id, safe='')}")
            if task.get("state") != "succeeded":
                raise RuntimeError("Úloha ještě nemá dokončený výsledek.")
            result = api("GET", f"/v1/tasks/{quote(task_id, safe='')}/result")
            if task.get("kind") in {
                "config-preview", "build-config-preview", "instance-config-preview",
                "test-drive-config-preview",
            }:
                cancel = "binaries" if task.get("kind") == "build-config-preview" else "configs"
                return render_template(
                    "config_change_preview.html", preview=result,
                    cancel_url=url_for(cancel),
                )
            if task.get("kind") == "config-observer":
                status = api("GET", "/v1/status")
                configs = api("GET", "/v1/configs")["configs"]
                status["build"]["artifacts"] = artifact_library_view(
                    status["build"].get("artifacts", [])
                )
                return render_template(
                    "config_observer.html", status=status, configs=configs, result=result,
                    error=None, selected_build=result.get("build_id", ""),
                    selected_config=result.get("config_id", ""),
                )
            if task.get("kind") in {
                "test-drive-spawn", "test-drive-upgrade",
                "test-drive-extend", "test-drive-restart", "test-drive-config-mode",
            } and result.get("id"):
                return redirect(url_for("test_drive_detail", drive_id=result["id"]))
            return jsonify(result)
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("console"))

    @app.get("/api/instances/<instance_id>/logs")
    @login_required
    def logs(instance_id):
        try: return jsonify(api("GET", f"/v1/instances/{quote(instance_id, safe='')}/logs?lines=300"))
        except RuntimeError as exc: return jsonify(error=str(exc)), 503

    @app.get("/api/instances/<instance_id>/diagnostics")
    @login_required
    def instance_diagnostics_api(instance_id):
        try:
            return jsonify(api(
                "GET", f"/v1/instances/{quote(instance_id, safe='')}/diagnostics"
            ))
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 503

    @app.post("/api/instances/<instance_id>/cli")
    @login_required
    def cli(instance_id):
        try:
            payload = request.get_json(silent=True) or {}
            return jsonify(api("POST", f"/v1/instances/{quote(instance_id, safe='')}/cli",
                               {"input": payload.get("input", ""),
                                "session_id": payload.get("session_id", ""),
                                "action": payload.get("action", "command")}))
        except RuntimeError as exc: return jsonify(error=str(exc)), 503

    @sock.route("/ws/instances/<instance_id>/<terminal_kind>")
    def terminal_websocket(ws, instance_id, terminal_kind):
        if terminal_kind not in {"cli", "gdb"}:
            ws.close()
            return
        if not g.admin or not secrets.compare_digest(
            request.args.get("csrf", ""), session.get("csrf_token", "")
        ):
            ws.close()
            return
        upstream_url = (
            app.config["RUNNER_WS_URL"].rstrip("/")
            + f"/v1/instances/{quote(instance_id, safe='')}/{terminal_kind}"
        )
        try:
            with websocket_connect(
                upstream_url,
                additional_headers={"Authorization": f"Bearer {app.config['RUNNER_TOKEN']}"},
                compression=None, max_size=64 * 1024, proxy=None,
            ) as upstream:
                finished = threading.Event()

                def copy_output():
                    try:
                        for message in upstream:
                            if finished.is_set(): break
                            ws.send(message)
                    except Exception as exc:
                        app.logger.warning("%s upstream %s closed: %s", terminal_kind, instance_id, exc)
                        try:
                            ws.send(f"\r\n\x1b[31m{terminal_kind.upper()} upstream closed: {exc}\x1b[0m\r\n")
                        except Exception:
                            pass
                    finally:
                        finished.set()
                        try: ws.close()
                        except Exception: pass

                reader = threading.Thread(target=copy_output, daemon=True, name=f"admin-{terminal_kind}-output")
                reader.start()
                try:
                    while not finished.is_set():
                        message = ws.receive()
                        if message is None: break
                        upstream.send(message)
                finally:
                    finished.set()
                    try: upstream.close()
                    except Exception: pass
                    reader.join(timeout=2)
        except Exception as exc:
            app.logger.warning("%s bridge %s failed: %s", terminal_kind, instance_id, exc)
            try:
                ws.send(f"\r\n\x1b[31m{terminal_kind.upper()} connection failed: {exc}\x1b[0m\r\n")
            except Exception:
                pass
            try: ws.close()
            except Exception: pass

    @sock.route("/ws/test-drives/<drive_id>/<terminal_kind>")
    def test_drive_websocket(ws, drive_id, terminal_kind):
        if terminal_kind not in {"cli", "shell"}:
            ws.close()
            return
        if not g.admin or not secrets.compare_digest(
            request.args.get("csrf", ""), session.get("csrf_token", "")
        ):
            ws.close()
            return
        upstream_url = (
            app.config["RUNNER_WS_URL"].rstrip("/")
            + f"/v1/test-drives/{quote(drive_id, safe='')}/{terminal_kind}"
        )
        try:
            with websocket_connect(
                upstream_url,
                additional_headers={"Authorization": f"Bearer {app.config['RUNNER_TOKEN']}"},
                compression=None, max_size=64 * 1024, proxy=None,
            ) as upstream:
                finished = threading.Event()

                def copy_output():
                    try:
                        for message in upstream:
                            if finished.is_set():
                                break
                            ws.send(message)
                    except Exception as exc:
                        app.logger.warning("test drive %s %s closed: %s", drive_id, terminal_kind, exc)
                    finally:
                        finished.set()
                        try:
                            ws.close()
                        except Exception:
                            pass

                reader = threading.Thread(
                    target=copy_output, daemon=True,
                    name=f"test-drive-{terminal_kind}-output",
                )
                reader.start()
                try:
                    while not finished.is_set():
                        message = ws.receive()
                        if message is None:
                            break
                        upstream.send(message)
                finally:
                    finished.set()
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    reader.join(timeout=2)
        except Exception as exc:
            app.logger.warning("test drive bridge %s failed: %s", drive_id, exc)
            try:
                ws.send(f"\r\n\x1b[31m{terminal_kind.upper()} connection failed: {exc}\x1b[0m\r\n")
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass

    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.password_option()
    def create_admin(email, password):
        if len(password) < 12: raise click.ClickException("heslo musí mít alespoň 12 znaků")
        normalized = email.lower().strip()
        with admin_lock:
            admins = load_admins()
            if any(item.get("email") == normalized for item in admins):
                raise click.ClickException("admin už existuje")
            admins.append({
                "id": secrets.token_hex(16), "email": normalized,
                "password_hash": generate_password_hash(password),
            })
            save_admins(admins)
        click.echo("Admin vytvořen.")

    return app


if __name__ == "__main__":
    create_app().run("127.0.0.1", 5001)
