import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from types import SimpleNamespace

def client(app):
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
                result = json.load(response)
                return result
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

    def runner_download(path):
        token = app.config["RUNNER_TOKEN"]
        if not token:
            raise RuntimeError("CZ_RUNNER_TOKEN is not configured")
        req = Request(
            app.config["RUNNER_URL"].rstrip("/") + path, method="GET",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/gzip"},
        )
        try:
            return urlopen(req, timeout=max(90, app.config["RUNNER_TIMEOUT"]))
        except HTTPError as exc:
            try:
                message = json.load(exc).get("error", str(exc))
            except Exception:
                message = str(exc)
            raise RuntimeError(message) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise RuntimeError(f"runner unavailable: {exc}") from exc

    return SimpleNamespace(api=api, enqueue=enqueue, download=runner_download)
