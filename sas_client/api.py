from __future__ import annotations

import json
import ssl
import time
import shutil
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import ClientConfig


@dataclass
class APIError(RuntimeError):
    message: str
    status: int = 0
    payload: Any = None

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.message}" if self.status else self.message


class RunnerClient:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.ssl_context = self._ssl_context(config)

    @staticmethod
    def _ssl_context(config: ClientConfig) -> ssl.SSLContext | None:
        if not config.url.startswith("https://") and not config.ws_url.startswith("wss://"):
            return None
        context = ssl.create_default_context(cafile=config.ca_file or None)
        if config.insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if config.cert_file:
            context.load_cert_chain(config.cert_file, config.key_file or None)
        return context

    def request(self, method: str, path: str, payload: Any = None,
                query: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        if query:
            values = {key: value for key, value in query.items() if value is not None}
            path += ("&" if "?" in path else "?") + urlencode(values)
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        request = Request(
            self.config.url + path, data=body, method=method.upper(),
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json", "Content-Type": "application/json",
                "User-Agent": "sasctl/0.1",
            },
        )
        try:
            with urlopen(request, timeout=timeout or self.config.timeout,
                         context=self.ssl_context) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read())
            except (ValueError, json.JSONDecodeError):
                error_payload = None
            message = error_payload.get("error", exc.reason) if isinstance(error_payload, dict) else str(exc.reason)
            raise APIError(str(message), exc.code, error_payload) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise APIError(f"runner unavailable: {exc}") from exc

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, payload: Any = None) -> Any:
        return self.request("POST", path, payload if payload is not None else {})

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def download(self, path: str, destination: str) -> str:
        request = Request(
            self.config.url + path, method="GET",
            headers={"Authorization": f"Bearer {self.config.token}",
                     "Accept": "application/octet-stream", "User-Agent": "sasctl/0.1"},
        )
        try:
            with urlopen(request, timeout=max(90, self.config.timeout),
                         context=self.ssl_context) as response, open(destination, "wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            return destination
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read())
            except (ValueError, json.JSONDecodeError):
                error_payload = None
            message = error_payload.get("error", exc.reason) if isinstance(error_payload, dict) else str(exc.reason)
            raise APIError(str(message), exc.code, error_payload) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise APIError(f"runner unavailable: {exc}") from exc

    def enqueue(self, method: str, path: str, payload: dict[str, Any] | None,
                label: str, kind: str) -> dict[str, Any]:
        return self.post("/v1/task-actions", {
            "method": method.upper(), "path": path, "payload": payload,
            "label": label, "kind": kind,
        })

    def wait_task(self, task_id: str, timeout: float = 1800,
                  interval: float = 0.5) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            task = self.get(f"/v1/tasks/{task_id}")
            if task.get("state") == "succeeded":
                result = self.get(f"/v1/tasks/{task_id}/result")
                return {**task, "completed_result": result}
            if task.get("state") == "failed":
                raise APIError(task.get("error", "task failed"), payload=task)
            if time.monotonic() >= deadline:
                raise APIError(f"timed out waiting for task {task_id}", payload=task)
            time.sleep(interval)

    def terminal_url(self, instance_id: str, kind: str = "cli") -> str:
        if kind not in {"cli", "gdb"}:
            raise ValueError("terminal kind must be cli or gdb")
        return f"{self.config.ws_url}/v1/instances/{instance_id}/{kind}"
