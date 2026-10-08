import secrets
import threading
from urllib.parse import quote
from flask import g, request, session
from flask_sock import Sock
from websockets.sync.client import connect as websocket_connect

def install(app, audit):
    sock = Sock(app)
    @sock.route("/ws/instances/<instance_id>/<terminal_kind>")
    def terminal_websocket(ws, instance_id, terminal_kind):
        if terminal_kind not in {"cli", "gdb", "netns"}:
            ws.close()
            return
        if not g.admin or not session.get('csrf_token') or not secrets.compare_digest(
            request.args.get("csrf", ""), session.get("csrf_token", "")
        ):
            ws.close()
            return
        upstream_url = (
            app.config["RUNNER_WS_URL"].rstrip("/")
            + f"/v1/instances/{quote(instance_id, safe='')}/{terminal_kind}"
        )
        if terminal_kind == 'netns':
            audit('instance.netns-shell', instance_id)
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
        if not g.admin or not session.get('csrf_token') or not secrets.compare_digest(
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
