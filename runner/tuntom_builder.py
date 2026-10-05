from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .systemd import BackendError


REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
BUILD_ID_RE = re.compile(r"^([0-9a-f]{40,64})-(release|debug)$")


@dataclass
class TuntomBuildState:
    state: str = "idle"
    ref: str = "master"
    started_at: str = ""
    finished_at: str = ""
    revision: str = ""
    error: str = ""
    log: str = ""
    build_type: str = "Release"


class TuntomBuilder:
    """Git-backed immutable library of tuntom transport component builds."""

    def __init__(self, source_dir: Path, library_dir: Path, repository: str,
                 jobs: int | None = None) -> None:
        self.source_dir = source_dir
        self.library_dir = library_dir
        self.repository = repository
        if jobs is None:
            try:
                jobs = len(os.sched_getaffinity(0))
            except (AttributeError, OSError):
                jobs = os.cpu_count() or 1
        if not 1 <= jobs <= 256:
            raise ValueError("tuntom build jobs must be between 1 and 256")
        self.jobs = jobs
        self.lock = threading.RLock()
        self.repo_lock = threading.Lock()
        self.refs_lock = threading.Lock()
        self.refs_state: dict[str, Any] = {
            "state": "idle", "refreshed_at": "", "error": "", "branches": [],
        }
        self.refs_refresh_started = False
        self.state = TuntomBuildState(
            state="ready" if self.artifacts() else "unavailable"
        )

    def artifacts(self) -> list[dict[str, Any]]:
        result = []
        try:
            paths = list(self.library_dir.iterdir())
        except OSError:
            return []
        for path in paths:
            if not path.is_dir() or not BUILD_ID_RE.fullmatch(path.name):
                continue
            try:
                metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
                adapter = path / "tuntom-divert-adapter"
                tunnel = path / "tuntom"
                commit_id = str(metadata.get("commit_id", ""))
                if (
                    not COMMIT_RE.fullmatch(commit_id)
                    or not adapter.is_file()
                    or not tunnel.is_file()
                ):
                    continue
                result.append({
                    "build_id": path.name, "commit_id": commit_id,
                    "ref": str(metadata.get("ref", ""))[:128],
                    "build_type": str(metadata.get("build_type", "Release")),
                    "built_at": str(metadata.get("built_at", "")),
                    "commit_at": str(metadata.get("commit_at", "")),
                    "size_bytes": adapter.stat().st_size + tunnel.stat().st_size,
                    "components": list(metadata.get(
                        "components", ["tuntom-divert-adapter"]
                    )),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return sorted(result, key=lambda item: item["built_at"], reverse=True)[:100]

    def status(self) -> dict[str, Any]:
        with self.lock:
            result = asdict(self.state)
        artifacts = self.artifacts()
        result.update({"artifacts": artifacts, "jobs": self.jobs})
        with self.refs_lock:
            refs = json.loads(json.dumps(self.refs_state))
        by_ref: dict[str, list[dict]] = {}
        for artifact in artifacts:
            by_ref.setdefault(str(artifact.get("ref", "")), []).append(artifact)
        for branch in refs.get("branches", []):
            builds = by_ref.get(str(branch.get("name", "")), [])
            remote = str(branch.get("commit_id", ""))
            branch["builds"] = builds
            branch["has_build"] = bool(builds)
            branch["update_available"] = bool(builds) and all(
                item.get("commit_id") != remote for item in builds
            )
            branch["current_types"] = sorted({
                str(item.get("build_type", "Release")) for item in builds
                if item.get("commit_id") == remote
            })
        result["refs"] = refs
        return result

    def resolve_adapter(self, build_id: str) -> Path:
        if not BUILD_ID_RE.fullmatch(build_id):
            raise BackendError("invalid tuntom build id")
        adapter = self.library_dir / build_id / "tuntom-divert-adapter"
        if not adapter.is_file() or not os.access(adapter, os.X_OK):
            raise BackendError("selected tuntom adapter build is unavailable")
        return adapter

    def resolve_tunnel(self, build_id: str) -> Path:
        if not BUILD_ID_RE.fullmatch(build_id):
            raise BackendError("invalid tuntom build id")
        binary = self.library_dir / build_id / "tuntom"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise BackendError("selected tuntom tunnel build is unavailable")
        return binary

    def delete_artifact(self, build_id: str) -> dict[str, Any] | None:
        if not BUILD_ID_RE.fullmatch(build_id):
            raise BackendError("invalid tuntom build id")
        with self.lock:
            if self.state.state == "running":
                raise BackendError("cannot delete a tuntom build while compilation is running")
            item = next((item for item in self.artifacts() if item["build_id"] == build_id), None)
            if not item:
                return None
            shutil.rmtree(self.library_dir / build_id)
            return item

    def start(self, ref: str = "master", build_type: str = "Release") -> dict[str, Any]:
        if not REF_RE.fullmatch(ref) or ".." in ref:
            raise BackendError("invalid tuntom git ref")
        if build_type not in {"Release", "Debug"}:
            raise BackendError("tuntom build type must be Release or Debug")
        with self.lock:
            if self.state.state == "running":
                raise BackendError("a tuntom build is already running")
            self.state = TuntomBuildState(
                state="running", ref=ref,
                started_at=datetime.now(timezone.utc).isoformat(),
                build_type=build_type,
            )
        threading.Thread(
            target=self._build, args=(ref, build_type), daemon=True,
            name="tuntom-build",
        ).start()
        return asdict(self.state)

    def _command(self, command: list[str], cwd: Path | None = None,
                 timeout: int = 1800) -> str:
        effective = command
        if command and command[0] == "git":
            effective = [
                "git", "-c", "url.https://github.com/.insteadOf=git@github.com:",
                *command[1:],
            ]
        if command[:2] == ["cmake", "--build"]:
            effective = ["nice", "-n", "10"]
            if shutil.which("ionice"):
                effective += ["ionice", "-c", "2", "-n", "7"]
            effective += command
        completed = subprocess.run(
            effective, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        output = (completed.stdout + "\n" + completed.stderr).strip()
        with self.lock:
            self.state.log = (
                self.state.log + "\n$ " + " ".join(command) + "\n" + output
            )[-100_000:]
        if completed.returncode:
            raise BackendError(output[-4000:] or f"{command[0]} failed")
        return output

    def _resolve_ref(self, ref: str) -> str:
        for candidate in (f"refs/remotes/origin/{ref}^{{commit}}", f"{ref}^{{commit}}"):
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", candidate],
                cwd=self.source_dir, capture_output=True, text=True, timeout=30, check=False,
            )
            revision = completed.stdout.strip().splitlines()[-1:] if completed.returncode == 0 else []
            if revision and COMMIT_RE.fullmatch(revision[0]):
                return revision[0]
        raise BackendError(f"tuntom git ref not found: {ref}")

    def _build(self, ref: str, build_type: str) -> None:
        try:
            with self.repo_lock:
                self.source_dir.parent.mkdir(parents=True, exist_ok=True)
                if not (self.source_dir / ".git").is_dir():
                    self._command([
                        "git", "clone", "--recursive", self.repository, str(self.source_dir),
                    ], timeout=600)
                else:
                    self._command(["git", "fetch", "--prune", "origin"], self.source_dir, 600)
                revision = self._resolve_ref(ref)
                self._command(["git", "switch", "--detach", revision], self.source_dir)
                self._command([
                    "git", "restore", "--source", revision,
                    "--staged", "--worktree", "--", ".",
                ], self.source_dir)
                self._command(
                    ["git", "submodule", "update", "--init", "--recursive"],
                    self.source_dir, 600,
                )
                build_dir = self.source_dir / f"build-sas-{build_type.lower()}"
                self._command([
                    "cmake", "-S", str(self.source_dir), "-B", str(build_dir),
                    f"-DCMAKE_BUILD_TYPE={build_type}", "-DBUILD_TESTING=OFF",
                ])
                self._command([
                    "cmake", "--build", str(build_dir), "--target",
                    "tuntom", "tuntom-divert-adapter", "-j", str(self.jobs),
                ])
                candidates = {
                    "tuntom": build_dir / "tuntom",
                    "tuntom-divert-adapter": build_dir / "tuntom-divert-adapter",
                }
                missing = [name for name, candidate in candidates.items() if not candidate.is_file()]
                if missing:
                    raise BackendError(
                        "build completed but components were not found: " + ", ".join(missing)
                    )
                commit_at = self._command(
                    ["git", "show", "-s", "--format=%cI", revision], self.source_dir
                ).splitlines()[-1]
                build_id = f"{revision}-{build_type.lower()}"
                self.library_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                target = self.library_dir / build_id
                temporary = self.library_dir / f".{build_id}.new"
                previous = self.library_dir / f".{build_id}.old"
                shutil.rmtree(temporary, ignore_errors=True)
                shutil.rmtree(previous, ignore_errors=True)
                temporary.mkdir(mode=0o700)
                for name, candidate in candidates.items():
                    shutil.copy2(candidate, temporary / name)
                    os.chmod(temporary / name, 0o755)
                metadata = {
                    "schema": 1, "build_id": build_id, "commit_id": revision,
                    "ref": ref, "build_type": build_type,
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "commit_at": commit_at,
                    # One source identity supplies direct tunnels and the VIA
                    # adapter. The fabric switch remains a later component.
                    "components": ["tuntom", "tuntom-divert-adapter"],
                }
                (temporary / "metadata.json").write_text(
                    json.dumps(metadata, separators=(",", ":")) + "\n", encoding="utf-8"
                )
                os.chmod(temporary / "metadata.json", 0o600)
                if target.exists():
                    target.replace(previous)
                temporary.replace(target)
                shutil.rmtree(previous, ignore_errors=True)
                with self.lock:
                    self.state.state = "complete"
                    self.state.revision = revision
        except Exception as exc:
            with self.lock:
                self.state.state = "failed"
                self.state.error = str(exc)
        finally:
            with self.lock:
                self.state.finished_at = datetime.now(timezone.utc).isoformat()
            self.request_ref_refresh()

    def request_ref_refresh(self) -> dict[str, Any]:
        with self.refs_lock:
            if self.refs_state["state"] == "running":
                return dict(self.refs_state)
            self.refs_state = {**self.refs_state, "state": "running", "error": ""}
        threading.Thread(
            target=self._refresh_refs, daemon=True, name="tuntom-ref-refresh",
        ).start()
        with self.refs_lock:
            return dict(self.refs_state)

    def _refresh_refs(self) -> None:
        try:
            with self.repo_lock:
                if not (self.source_dir / ".git").is_dir():
                    self.source_dir.parent.mkdir(parents=True, exist_ok=True)
                    self._command([
                        "git", "clone", "--recursive", self.repository, str(self.source_dir),
                    ], timeout=600)
                else:
                    self._command(["git", "fetch", "--prune", "origin"], self.source_dir, 600)
                completed = subprocess.run([
                    "git", "for-each-ref",
                    "--format=%(refname:strip=3)%09%(objectname)%09%(committerdate:iso-strict)",
                    "refs/remotes/origin",
                ], cwd=self.source_dir, capture_output=True, text=True, timeout=30, check=False)
                if completed.returncode:
                    raise BackendError(completed.stderr.strip() or "cannot list tuntom branches")
            branches = []
            for line in completed.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3 and parts[0] != "HEAD" and COMMIT_RE.fullmatch(parts[1]):
                    branches.append({
                        "name": parts[0], "commit_id": parts[1], "commit_at": parts[2],
                    })
            branches.sort(key=lambda item: item["name"])
            with self.refs_lock:
                self.refs_state = {
                    "state": "ready", "error": "",
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    "branches": branches,
                }
        except Exception as exc:
            with self.refs_lock:
                self.refs_state = {
                    **self.refs_state, "state": "failed", "error": str(exc),
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                }

    def start_ref_refresh(self, interval: int = 300) -> None:
        if interval < 30:
            raise ValueError("tuntom ref refresh interval must be at least 30 seconds")
        with self.refs_lock:
            if self.refs_refresh_started:
                return
            self.refs_refresh_started = True
        self.request_ref_refresh()

        def periodic() -> None:
            while True:
                time.sleep(interval)
                self.request_ref_refresh()

        threading.Thread(
            target=periodic, daemon=True, name="tuntom-ref-refresh-timer",
        ).start()
