from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    task_id: str
    kind: str
    label: str
    dedupe_key: str
    resource: str
    state: str = "pending"
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    result: Any = None
    error: str = ""


class TaskQueue:
    """Small JSON-backed async task queue with active-task deduplication."""

    def __init__(self, state_file: Path, workers: int = 4, history_limit: int = 200) -> None:
        if not 1 <= workers <= 32:
            raise ValueError("task workers must be between 1 and 32")
        self.state_file = state_file
        self.history_limit = history_limit
        self.lock = threading.RLock()
        self.work: queue.Queue[tuple[str, Callable[[], Any]]] = queue.Queue()
        self.tasks: dict[str, Task] = {}
        self.active_keys: dict[str, str] = {}
        self.resource_locks: dict[str, threading.Lock] = {}
        self._load()
        for number in range(workers):
            threading.Thread(
                target=self._worker, daemon=True, name=f"runner-task-{number + 1}",
            ).start()

    def _load(self) -> None:
        try:
            values = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if not isinstance(values, list):
            return
        for value in values[-self.history_limit:]:
            try:
                task = Task(**value)
                if task.state in {"pending", "running"}:
                    task.state = "failed"
                    task.error = "runner restarted before the task completed"
                    task.finished_at = _now()
                self.tasks[task.task_id] = task
            except (TypeError, ValueError):
                continue
        self._persist()

    def _persist(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        values = [asdict(item) for item in sorted(
            self.tasks.values(), key=lambda item: item.created_at,
        )[-self.history_limit:]]
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_file)

    def submit(self, kind: str, label: str, dedupe_key: str, resource: str,
               function: Callable[[], Any]) -> tuple[Task, bool]:
        with self.lock:
            existing_id = self.active_keys.get(dedupe_key)
            if existing_id and existing_id in self.tasks:
                return self.tasks[existing_id], False
            task = Task(
                task_id=str(uuid.uuid4()), kind=kind, label=label[:200],
                dedupe_key=dedupe_key[:500], resource=resource[:200],
            )
            self.tasks[task.task_id] = task
            self.active_keys[dedupe_key] = task.task_id
            self._trim()
            self._persist()
            self.work.put((task.task_id, function))
            return task, True

    def _trim(self) -> None:
        finished = sorted(
            (item for item in self.tasks.values() if item.state in {"succeeded", "failed"}),
            key=lambda item: item.created_at,
        )
        while len(self.tasks) > self.history_limit and finished:
            removed = finished.pop(0)
            self.tasks.pop(removed.task_id, None)
            (self.state_file.parent / "task-results" / f"{removed.task_id}.json").unlink(
                missing_ok=True)

    def _compact_result(self, task_id: str, result: Any) -> Any:
        encoded = json.dumps(result, separators=(",", ":")).encode()
        if len(encoded) <= 64 * 1024:
            return result
        result_dir = self.state_file.parent / "task-results"
        result_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = result_dir / f"{task_id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        summary: dict[str, Any] = {"stored": True, "size_bytes": len(encoded)}
        if isinstance(result, dict):
            for key in ("id", "preview_id", "config_id", "build_id", "profile_id", "bundle_id"):
                if key in result:
                    summary[key] = result[key]
        return summary

    def _worker(self) -> None:
        while True:
            task_id, function = self.work.get()
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    self.work.task_done()
                    continue
                resource_lock = self.resource_locks.setdefault(task.resource, threading.Lock())
            if not resource_lock.acquire(blocking=False):
                # Do not occupy a worker while an earlier task for the same
                # resource is running; unrelated tasks may still proceed.
                self.work.put((task_id, function))
                self.work.task_done()
                threading.Event().wait(0.05)
                continue
            try:
                with self.lock:
                    task.state = "running"
                    task.started_at = _now()
                    self._persist()
                result = function()
                with self.lock:
                    task.result = self._compact_result(task.task_id, result)
                    task.state = "succeeded"
                    task.finished_at = _now()
                    self.active_keys.pop(task.dedupe_key, None)
                    self._persist()
            except Exception as exc:
                with self.lock:
                    task.state = "failed"
                    task.error = str(exc)[-8000:]
                    task.finished_at = _now()
                    self.active_keys.pop(task.dedupe_key, None)
                    self._persist()
            finally:
                resource_lock.release()
                self.work.task_done()

    def get(self, task_id: str) -> Task | None:
        with self.lock:
            return self.tasks.get(task_id)

    def list(self) -> list[dict]:
        with self.lock:
            return [asdict(item) for item in sorted(
                self.tasks.values(), key=lambda item: item.created_at, reverse=True,
            )]

    def read_result(self, task_id: str) -> Any:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            result = task.result
            stored = isinstance(result, dict) and result.get("stored") is True
        if not stored:
            return result
        return json.loads((
            self.state_file.parent / "task-results" / f"{task_id}.json"
        ).read_text(encoding="utf-8"))

    @staticmethod
    def view(task: Task, deduplicated: bool = False) -> dict:
        return {**asdict(task), "deduplicated": deduplicated}
