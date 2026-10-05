from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .systemd import BackendError
from .config import PARAMETERS, TOKEN_RE, render_template


REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
BUILD_ID_RE = re.compile(r"^([0-9a-f]{40,64})-(release|debug)$")
READELF_NEEDED_RE = re.compile(r"\(NEEDED\).*Shared library: \[([^]]+)]")
READELF_INTERPRETER_RE = re.compile(r"Requesting program interpreter: ([^]]+)")
LDCONFIG_RE = re.compile(r"^\s*(\S+)\s+\([^)]*\)\s+=>\s+(/\S+)\s*$")
ROOTFS_SCHEMA = 4


@dataclass
class BuildState:
    state: str = "idle"
    ref: str = "master"
    started_at: str = ""
    finished_at: str = ""
    revision: str = ""
    error: str = ""
    log: str = ""
    build_type: str = "Release"


@dataclass(frozen=True)
class BuildBundle:
    build_id: str
    binary: Path
    config: Path


class SmithproxyBuilder:
    def __init__(self, source_dir: Path, output: Path,
                 repository: str = "https://github.com/astibal/smithproxy.git",
                 jobs: int | None = None, config_library: Any = None,
                 native_cache_dir: Path | None = None) -> None:
        self.source_dir = source_dir
        self.output = output
        self.library_dir = output.parent / "builds"
        self.rootfs_library_dir = output.parent / "rootfs"
        self.native_cache_dir = native_cache_dir or output.parent / "native-config-cache"
        self.repository = repository
        self.config_library = config_library
        if jobs is None:
            try:
                jobs = len(os.sched_getaffinity(0))
            except (AttributeError, OSError):
                jobs = os.cpu_count() or 1
        if not 1 <= jobs <= 256:
            raise ValueError("build jobs must be between 1 and 256")
        self.jobs = jobs
        self.lock = threading.RLock()
        self.repo_lock = threading.Lock()
        self.refs_lock = threading.Lock()
        self.refs_state: dict[str, Any] = {
            "state": "idle", "refreshed_at": "", "error": "", "branches": [],
        }
        self.refs_refresh_started = False
        ready = (
            self.output.is_file()
            and self.output.with_suffix(".cfg").is_file()
            and (self.output.parent / f"{self.output.name}.assets").is_dir()
        )
        self.state = BuildState(state="ready" if ready else "unavailable")

    def status(self) -> dict:
        # Never hold the state lock while scanning artifacts/configs. A build
        # worker only needs this lock for tiny state/log updates; HTTP status
        # requests must remain responsive during a full parallel compile.
        with self.lock:
            result = asdict(self.state)
        result["binary"] = {
            "path": str(self.output),
            "ready": self.output.is_file() and os.access(self.output, os.X_OK),
            "size_bytes": self.output.stat().st_size if self.output.is_file() else 0,
        }
        artifacts = self.artifacts()
        self._prune_default_configs(artifacts)
        result["artifacts"] = artifacts
        result["configs"] = self.configs(artifacts)
        result["jobs"] = self.jobs
        with self.refs_lock:
            refs = json.loads(json.dumps(self.refs_state))
        built_by_ref: dict[str, list[dict]] = {}
        for artifact in artifacts:
            built_by_ref.setdefault(str(artifact.get("ref", "")), []).append(artifact)
        for branch in refs.get("branches", []):
            builds = built_by_ref.get(str(branch.get("name", "")), [])
            remote_commit = str(branch.get("commit_id", ""))
            branch["builds"] = [{
                "build_id": item.get("build_id", ""),
                "commit_id": item.get("commit_id", ""),
                "build_type": item.get("build_type", "Release"),
                "built_at": item.get("built_at", ""),
            } for item in builds]
            branch["has_build"] = bool(builds)
            branch["update_available"] = bool(builds) and all(
                item.get("commit_id") != remote_commit for item in builds
            )
            branch["current_types"] = sorted({
                str(item.get("build_type", "Release")) for item in builds
                if item.get("commit_id") == remote_commit
            })
        result["refs"] = refs
        return result

    def request_ref_refresh(self) -> dict:
        """Start a non-blocking remote-branch refresh."""
        with self.refs_lock:
            if self.refs_state["state"] == "running":
                return dict(self.refs_state)
            self.refs_state = {
                **self.refs_state, "state": "running", "error": "",
            }
        threading.Thread(
            target=self._refresh_refs, daemon=True, name="smithproxy-ref-refresh",
        ).start()
        with self.refs_lock:
            return dict(self.refs_state)

    def start_ref_refresh(self, interval: int = 300) -> None:
        """Refresh immediately and then periodically without serving-thread work."""
        if interval < 30:
            raise ValueError("ref refresh interval must be at least 30 seconds")
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
            target=periodic, daemon=True, name="smithproxy-ref-refresh-timer",
        ).start()

    def _refresh_refs(self) -> None:
        try:
            with self.repo_lock:
                if not (self.source_dir / ".git").is_dir():
                    raise BackendError("source checkout is not available yet")
                completed = subprocess.run(
                    ["git", "fetch", "--prune", "origin"], cwd=self.source_dir,
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if completed.returncode:
                    raise BackendError(
                        (completed.stderr or completed.stdout).strip()[-4000:]
                        or "git fetch failed"
                    )
                completed = subprocess.run([
                    "git", "for-each-ref",
                    "--format=%(refname:strip=3)%09%(objectname)%09%(committerdate:iso-strict)",
                    "refs/remotes/origin",
                ], cwd=self.source_dir, capture_output=True, text=True, timeout=30, check=False)
                if completed.returncode:
                    raise BackendError(completed.stderr.strip() or "cannot list remote branches")
            branches = []
            for line in completed.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3 or parts[0] == "HEAD" or not COMMIT_RE.fullmatch(parts[1]):
                    continue
                branches.append({
                    "name": parts[0], "commit_id": parts[1], "commit_at": parts[2],
                })
            branches.sort(key=lambda item: item["name"])
            with self.refs_lock:
                self.refs_state = {
                    "state": "ready",
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    "error": "", "branches": branches,
                }
        except Exception as exc:
            with self.refs_lock:
                self.refs_state = {
                    **self.refs_state, "state": "failed", "error": str(exc),
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                }

    @staticmethod
    def native_cache_key(commit_id: str, ref: str, build_type: str) -> str:
        """Stable identity for native snapshots belonging to one logical build."""
        identity = f"{commit_id}\0{ref}\0{build_type}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()[:24]

    def artifacts(self) -> list[dict]:
        """Return verified archived binaries, newest first."""
        result = []
        try:
            paths = list(self.library_dir.iterdir())
        except OSError:
            return result
        for path in paths:
            if not path.is_dir() or not (
                COMMIT_RE.fullmatch(path.name) or BUILD_ID_RE.fullmatch(path.name)
            ):
                continue
            try:
                metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
                binary = path / "smithproxy"
                config = path / "smithproxy.cfg"
                assets = path / "smithproxy.assets"
                commit_id = str(metadata.get("commit_id", ""))
                if (not COMMIT_RE.fullmatch(commit_id) or not binary.is_file()
                        or not config.is_file() or not assets.is_dir()):
                    continue
                if "commit_at" not in metadata:
                    commit_at = ""
                    try:
                        completed = subprocess.run(
                            ["git", "show", "-s", "--format=%cI", commit_id],
                            cwd=self.source_dir, capture_output=True, text=True,
                            timeout=5, check=False,
                        )
                        if completed.returncode == 0:
                            commit_at = completed.stdout.strip().splitlines()[-1]
                    except (OSError, subprocess.TimeoutExpired, IndexError):
                        pass
                    metadata["commit_at"] = commit_at
                    temporary_metadata = path / ".metadata.json.new"
                    temporary_metadata.write_text(
                        json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
                    )
                    os.chmod(temporary_metadata, 0o600)
                    temporary_metadata.replace(path / "metadata.json")
                result.append({
                    "build_id": path.name,
                    "commit_id": commit_id,
                    "ref": str(metadata.get("ref", ""))[:128],
                    "built_at": str(metadata.get("built_at", "")),
                    "commit_at": str(metadata.get("commit_at", "")),
                    "build_type": str(metadata.get("build_type", "Release")),
                    "size_bytes": binary.stat().st_size,
                    **self.rootfs_info(path.name, binary),
                })
            except (OSError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda item: item["built_at"], reverse=True)[:50]

    def _prune_default_configs(self, artifacts: list[dict]) -> list[dict]:
        if not self.config_library:
            return []
        build_ids = {
            str(item.get("build_id", item.get("commit_id", "")))
            for item in artifacts
        }
        return self.config_library.prune_missing_build_defaults(build_ids)

    def configs(self, artifacts: list[dict] | None = None) -> list[dict]:
        """List exact Smithproxy configs retained with successful builds."""
        if self.config_library:
            self._prune_default_configs(artifacts if artifacts is not None else self.artifacts())
            return self.config_library.list()
        return [{
            "config_id": item["build_id"],
            "commit_id": item["commit_id"],
            "ref": item["ref"],
            "created_at": item["built_at"],
        } for item in self.artifacts()]

    def resolve_binary(self, build_id: str) -> Path:
        if build_id == "active":
            binary = self.output
        elif COMMIT_RE.fullmatch(build_id) or BUILD_ID_RE.fullmatch(build_id):
            binary = self.library_dir / build_id / "smithproxy"
        else:
            raise BackendError("invalid build_id")
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise BackendError("selected binary is unavailable")
        return binary

    def artifact(self, build_id: str) -> dict:
        item = next(
            (item for item in self.artifacts()
             if item.get("build_id", item.get("commit_id")) == build_id), None,
        )
        if not item:
            raise BackendError("selected archived build metadata is unavailable")
        return item

    def delete_artifact(self, build_id: str) -> dict | None:
        if not (COMMIT_RE.fullmatch(build_id) or BUILD_ID_RE.fullmatch(build_id)):
            raise BackendError("invalid build_id")
        with self.lock:
            if self.state.state == "running":
                raise BackendError("cannot delete an archived build while a build is running")
            item = next(
                (candidate for candidate in self.artifacts()
                 if candidate.get("build_id", candidate.get("commit_id")) == build_id), None,
            )
            if not item:
                return None
            target = self.library_dir / build_id
            if target.parent != self.library_dir or not target.is_dir():
                raise BackendError("archived build path is unavailable")
            shutil.rmtree(target)
            shutil.rmtree(self.rootfs_library_dir / build_id, ignore_errors=True)
            cache_key = self.native_cache_key(
                item.get("commit_id", ""), item.get("ref", ""),
                item.get("build_type", "Release"),
            )
            shutil.rmtree(self.native_cache_dir / cache_key, ignore_errors=True)
            self._prune_default_configs(self.artifacts())
            return item

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def rootfs_info(self, build_id: str, binary: Path | None = None) -> dict[str, Any]:
        """Describe a verified optional rootfs without creating it."""
        root = self.rootfs_library_dir / build_id
        try:
            metadata = json.loads((root / "rootfs.json").read_text(encoding="utf-8"))
            selected_binary = binary or self.resolve_binary(build_id)
            wanted = self._file_sha256(selected_binary)
            ready = (
                metadata.get("schema") == ROOTFS_SCHEMA
                and metadata.get("build_id") == build_id
                and metadata.get("binary_sha256") == wanted
                and (root / "usr/bin/smithproxy").is_file()
                and (root / "bin/sh").is_file()
                and (root / "opt/sas/bin").is_dir()
            )
            return {
                "rootfs_ready": bool(ready),
                "rootfs_path": str(root) if ready else "",
                "rootfs_size_bytes": int(metadata.get("size_bytes", 0)) if ready else 0,
                "rootfs_created_at": str(metadata.get("created_at", "")) if ready else "",
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError, BackendError):
            return {
                "rootfs_ready": False, "rootfs_path": "",
                "rootfs_size_bytes": 0, "rootfs_created_at": "",
            }

    @staticmethod
    def _readelf(path: Path, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["readelf", *arguments, str(path)], capture_output=True, text=True,
                timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"cannot inspect ELF dependency metadata: {exc}") from exc
        if completed.returncode:
            raise BackendError(
                (completed.stderr or completed.stdout).strip()[-2000:]
                or "readelf rejected the selected binary"
            )
        return completed.stdout

    @classmethod
    def _elf_dependencies(cls, path: Path) -> tuple[list[str], str, list[str]]:
        dynamic = cls._readelf(path, "-d")
        program = cls._readelf(path, "-l")
        needed = READELF_NEEDED_RE.findall(dynamic)
        interpreter_match = READELF_INTERPRETER_RE.search(program)
        interpreter = interpreter_match.group(1) if interpreter_match else ""
        runpaths = []
        for line in dynamic.splitlines():
            if "(RPATH)" not in line and "(RUNPATH)" not in line:
                continue
            match = re.search(r"Library (?:rpath|runpath): \[([^]]*)]", line)
            if match:
                runpaths.extend(item for item in match.group(1).split(":") if item)
        return needed, interpreter, runpaths

    @staticmethod
    def _library_cache() -> dict[str, list[Path]]:
        try:
            completed = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True,
                timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"cannot inspect host library cache: {exc}") from exc
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "ldconfig -p failed")
        result: dict[str, list[Path]] = {}
        for line in completed.stdout.splitlines():
            match = LDCONFIG_RE.match(line)
            if match:
                result.setdefault(match.group(1), []).append(Path(match.group(2)))
        return result

    @classmethod
    def _resolve_elf_closure(cls, binary: Path) -> tuple[set[Path], Path | None]:
        """Resolve DT_NEEDED recursively from metadata; never execute the ELF."""
        cache = cls._library_cache()
        resolved: set[Path] = set()
        pending = [binary]
        interpreter: Path | None = None
        while pending:
            current = pending.pop()
            needed, current_interpreter, runpaths = cls._elf_dependencies(current)
            if current == binary and current_interpreter:
                interpreter = Path(current_interpreter)
                if not interpreter.is_file():
                    raise BackendError("ELF interpreter is unavailable on this host")
            origin = current.parent
            search = []
            for raw in runpaths:
                expanded = raw.replace("$ORIGIN", str(origin)).replace("${ORIGIN}", str(origin))
                if expanded.startswith("/"):
                    search.append(Path(expanded))
            for name in needed:
                candidates = [directory / name for directory in search]
                candidates.extend(cache.get(name, []))
                dependency = next((item for item in candidates if item.is_file()), None)
                if dependency is None:
                    raise BackendError(f"rootfs dependency is unavailable: {name}")
                destination_identity = Path(os.path.abspath(dependency))
                if destination_identity not in resolved:
                    resolved.add(destination_identity)
                    pending.append(dependency.resolve())
        return resolved, interpreter

    @staticmethod
    def _copy_rootfs_file(source: Path, root: Path, destination: Path | None = None) -> None:
        target = root / str(destination or source).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copy2(source.resolve(), target, follow_symlinks=True)

    @staticmethod
    def _copy_rootfs_certificates(source: Path, target: Path) -> None:
        """Snapshot CApath without links escaping the isolated filesystem.

        Keep OpenSSL hash links, but materialize certificates whose distro
        links point to /usr/share or /usr/local/share. No host bind is needed.
        """
        source = source.resolve()
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        for entry in source.rglob("*"):
            if not entry.is_symlink():
                continue
            try:
                resolved = entry.resolve(strict=True)
                if not resolved.is_file():
                    raise ValueError("certificate link is not a regular file")
                raw = Path(os.readlink(entry))
                lexical = Path(os.path.abspath(raw if raw.is_absolute() else entry.parent / raw))
                destination = target / entry.relative_to(source)
                destination.unlink()
                if lexical.is_relative_to(source):
                    # Rebase absolute internal links too: they must not resolve
                    # against the host while inspecting/exporting this image.
                    internal = target / lexical.relative_to(source)
                    destination.symlink_to(os.path.relpath(internal, destination.parent))
                else:
                    shutil.copy2(resolved, destination)
            except (OSError, RuntimeError, ValueError) as exc:
                raise BackendError(f"cannot package CA certificate {entry}: {exc}") from exc
        for entry in target.rglob("*"):
            if entry.is_symlink() and (not entry.is_file() or not entry.resolve().is_relative_to(target.resolve())):
                raise BackendError(f"invalid packaged CA certificate link: {entry}")

    def prepare_rootfs(self, build_id: str) -> dict[str, Any]:
        """Create an atomic minimal RootDirectory tree for one archived build."""
        if not BUILD_ID_RE.fullmatch(build_id):
            raise BackendError("rootfs mode requires an archived Release or Debug build")
        binary = self.resolve_binary(build_id)
        current = self.rootfs_info(build_id, binary)
        if current["rootfs_ready"]:
            return current
        with self.lock:
            current = self.rootfs_info(build_id, binary)
            if current["rootfs_ready"]:
                return current
            self.rootfs_library_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.rootfs_library_dir / build_id
            temporary = self.rootfs_library_dir / f".{build_id}.new"
            previous = self.rootfs_library_dir / f".{build_id}.old"
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(previous, ignore_errors=True)
            for relative in (
                "usr/bin", "bin", "opt/sas/bin", "etc", "etc/ssl/certs", "run", "tmp", "work",
                "var/tmp", "var/smithproxy/data", "proc", "sys", "dev",
            ):
                (temporary / relative).mkdir(parents=True, exist_ok=True, mode=0o755)
            (temporary / "var/run").symlink_to("../run")
            # Preserve the existing absolute read-only build mount. Runtime
            # configs and diagnostics may legitimately refer to this path.
            (temporary / str(binary.parent).lstrip("/")).mkdir(
                parents=True, exist_ok=True, mode=0o755
            )
            self._copy_rootfs_file(binary, temporary, Path("/usr/bin/smithproxy"))
            dependencies, interpreter = self._resolve_elf_closure(binary)
            for dependency in sorted(dependencies, key=str):
                self._copy_rootfs_file(dependency, temporary)
            if interpreter:
                self._copy_rootfs_file(interpreter, temporary)
            # VIA components load their systemd credential through a tiny
            # shell wrapper. Keep the shell and its ELF closure in the base
            # image; the selected immutable Tuntom binaries are mounted RO by
            # their individual units.
            shell = Path("/bin/sh").resolve()
            if not shell.is_file():
                raise BackendError("rootfs shell is unavailable on this host")
            self._copy_rootfs_file(shell, temporary, Path("/bin/sh"))
            shell_dependencies, shell_interpreter = self._resolve_elf_closure(shell)
            for dependency in sorted(shell_dependencies, key=str):
                self._copy_rootfs_file(dependency, temporary)
            if shell_interpreter:
                self._copy_rootfs_file(shell_interpreter, temporary)
            for name in ("nsswitch.conf", "hosts", "resolv.conf", "services", "protocols"):
                source = Path("/etc") / name
                if source.is_file():
                    self._copy_rootfs_file(source, temporary)
            (temporary / "etc/passwd").write_text("root:x:0:0:root:/root:/usr/sbin/nologin\n")
            (temporary / "etc/group").write_text("root:x:0:\n")
            host_certificates = Path("/etc/ssl/certs")
            if host_certificates.is_dir():
                self._copy_rootfs_certificates(host_certificates, temporary / "etc/ssl/certs")
            # OpenSSL providers are loaded with dlopen() and consequently do
            # not appear in DT_NEEDED. Preserve their absolute host paths.
            for provider_root in Path("/usr/lib").glob("*/ossl-modules"):
                if provider_root.is_dir():
                    shutil.copytree(
                        provider_root,
                        temporary / str(provider_root).lstrip("/"),
                        dirs_exist_ok=True, symlinks=True,
                    )
            total_size = sum(
                item.stat().st_size for item in temporary.rglob("*")
                if item.is_file() and not item.is_symlink()
            )
            metadata = {
                "schema": ROOTFS_SCHEMA, "build_id": build_id,
                "binary_sha256": self._file_sha256(binary),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "entrypoint": "/usr/bin/smithproxy",
                "size_bytes": total_size,
            }
            (temporary / "rootfs.json").write_text(
                json.dumps(metadata, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            if target.exists():
                target.replace(previous)
            temporary.replace(target)
            shutil.rmtree(previous, ignore_errors=True)
            return self.rootfs_info(build_id, binary)

    def resolve_rootfs(self, build_id: str) -> Path:
        info = self.rootfs_info(build_id)
        if not info["rootfs_ready"]:
            raise BackendError("selected build rootfs is unavailable; save the profile again")
        return Path(info["rootfs_path"])

    def resolve_config(self, config_id: str) -> Path:
        if config_id == "active":
            config = self.output.with_suffix(".cfg")
            assets = self.output.parent / f"{self.output.name}.assets"
        elif self.config_library:
            return self.config_library.resolve(config_id)
        elif COMMIT_RE.fullmatch(config_id) or BUILD_ID_RE.fullmatch(config_id):
            root = self.library_dir / config_id
            config = root / "smithproxy.cfg"
            assets = root / "smithproxy.assets"
        else:
            raise BackendError("invalid config_id")
        if not config.is_file() or not assets.is_dir():
            raise BackendError("selected configuration is unavailable")
        return config

    def resolve_config_assets(self, config_id: str) -> Path:
        if config_id == "active":
            assets = self.output.parent / f"{self.output.name}.assets"
            if not assets.is_dir():
                raise BackendError("active configuration assets are unavailable")
            return assets
        if self.config_library:
            return self.config_library.resolve_assets(config_id)
        config = self.resolve_config(config_id)
        return config.parent / "smithproxy.assets"

    def resolve(self, build_id: str) -> BuildBundle:
        """Resolve only the active bundle or an archived immutable commit."""
        return BuildBundle(build_id, self.resolve_binary(build_id), self.resolve_config(build_id))

    def validate_config_text(self, content: str, build_id: str, assets: Path) -> str:
        binary = self.resolve_binary(build_id)
        if len(content.encode("utf-8")) > 1024 * 1024 or "\0" in content:
            raise BackendError("configuration is too large or contains NUL")
        with tempfile.TemporaryDirectory(prefix="config-check-") as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source.cfg"
            runtime = temporary / "runtime"
            runtime.mkdir(mode=0o700)
            source.write_text(content, encoding="utf-8")
            placeholder_values = {
                key.lower(): "placeholder.example" for key in TOKEN_RE.findall(content)
                if key not in ({name.upper() for name in PARAMETERS} | {"RUNTIME_DIR"})
            }
            placeholder_values.update({
                "rewrite_sni": "source.example",
                "rewrite_sni_to": "target.example",
            })
            rendered = render_template(source, {
                "socks_port": 1080, "plaintext_port": 50080, "tls_port": 50443,
                "cli_port": 50000, "workers": 1, "pcap_quota_mb": 100,
            }, runtime, assets_dir=assets, text_parameters=placeholder_values)
            checked = temporary / "checked.cfg"
            checked.write_text(rendered, encoding="utf-8")
            completed = subprocess.run(
                [str(binary), "--config-file", str(checked), "--config-check-only"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()
            if completed.returncode:
                raise BackendError(output[-4000:] or "Smithproxy rejected the configuration")
            return output[-4000:]

    def _archive_bundle(self, revision: str, ref: str, candidate: Path,
                        source_config: Path, source_certs: Path, source_messages: Path,
                        build_type: str = "Release", commit_at: str = "") -> None:
        if not COMMIT_RE.fullmatch(revision):
            raise BackendError("cannot archive an invalid commit id")
        if build_type not in {"Release", "Debug"}:
            raise BackendError("build_type must be Release or Debug")
        build_id = f"{revision}-{build_type.lower()}"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        target = self.library_dir / build_id
        temporary = self.library_dir / f".{build_id}.new"
        previous = self.library_dir / f".{build_id}.old"
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        temporary.mkdir(mode=0o700)
        archived_binary = temporary / "smithproxy"
        shutil.copy2(candidate, archived_binary)
        os.chmod(archived_binary, 0o755)
        archived_config = temporary / "smithproxy.cfg"
        shutil.copy2(source_config, archived_config)
        os.chmod(archived_config, 0o600)
        archived_assets = temporary / "smithproxy.assets"
        (archived_assets / "certs").mkdir(parents=True)
        shutil.copytree(source_certs, archived_assets / "certs", dirs_exist_ok=True)
        shutil.copytree(source_messages, archived_assets / "msg")
        metadata = {
            "commit_id": revision,
            "build_id": build_id,
            "ref": ref,
            "build_type": build_type,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "commit_at": commit_at,
            "size_bytes": archived_binary.stat().st_size,
        }
        metadata_path = temporary / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, separators=(",", ":")), encoding="utf-8")
        os.chmod(metadata_path, 0o600)
        if target.exists():
            target.replace(previous)
        temporary.replace(target)
        shutil.rmtree(previous, ignore_errors=True)

    def start(self, ref: str = "master", build_type: str = "Release") -> dict:
        if not REF_RE.fullmatch(ref) or ".." in ref:
            raise BackendError("invalid git ref")
        if build_type not in {"Release", "Debug"}:
            raise BackendError("build_type must be Release or Debug")
        with self.lock:
            if self.state.state == "running":
                raise BackendError("a build is already running")
            self.state = BuildState(
                state="running", ref=ref, started_at=datetime.now(timezone.utc).isoformat(),
                build_type=build_type,
            )
            threading.Thread(
                target=self._build, args=(ref, build_type), daemon=True,
                name="smithproxy-build",
            ).start()
            return asdict(self.state)

    def _command(self, command: list[str], cwd: Path | None = None,
                 timeout: int | None = None) -> str:
        effective = command
        if command and command[0] == "git":
            # Build workers run as root and intentionally do not inherit the
            # interactive user's SSH agent.  Public GitHub submodules may
            # still be recorded as git@github.com URLs; rewrite only those to
            # HTTPS so a deleted submodule can be reconstructed unattended.
            effective = [
                "git", "-c",
                "url.https://github.com/.insteadOf=git@github.com:",
                *command[1:],
            ]
        if command[:2] == ["cmake", "--build"]:
            # Keep all requested compiler jobs, but let the API/web processes
            # win CPU and I/O scheduling immediately under load.
            effective = ["nice", "-n", "10"]
            if shutil.which("ionice"):
                effective += ["ionice", "-c", "2", "-n", "7"]
            effective += command
        if timeout is None:
            timeout = 600 if command and command[0] == "git" else 1800
        try:
            completed = subprocess.run(
                effective, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial = "\n".join(filter(None, [
                exc.stdout if isinstance(exc.stdout, str) else "",
                exc.stderr if isinstance(exc.stderr, str) else "",
            ])).strip()
            with self.lock:
                self.state.log = (
                    self.state.log + "\n$ " + " ".join(command) + "\n"
                    + partial + f"\ncommand timed out after {timeout}s"
                )[-100_000:]
            raise BackendError(
                f"{command[0]} timed out after {timeout}s"
            ) from exc
        output = (completed.stdout + "\n" + completed.stderr).strip()
        with self.lock:
            self.state.log = (self.state.log + "\n$ " + " ".join(command) + "\n" + output)[-100_000:]
        if completed.returncode:
            raise BackendError(output[-4000:] or f"{command[0]} failed")
        return output

    def _resolve_ref(self, ref: str) -> str:
        """Resolve an administrator-selected ref to one immutable commit.

        Prefer the freshly fetched remote branch over a potentially stale local
        branch with the same name. Exact tags and commit IDs remain supported.
        Passing the resulting SHA to ``git switch`` also avoids checkout's
        branch-creation/DWIM argument handling.
        """
        candidates = [f"refs/remotes/origin/{ref}^{{commit}}", f"{ref}^{{commit}}"]
        for candidate in candidates:
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", candidate],
                cwd=self.source_dir, capture_output=True, text=True, timeout=30, check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()
            with self.lock:
                self.state.log = (
                    self.state.log + "\n$ git rev-parse --verify --quiet --end-of-options "
                    + candidate + "\n" + output
                )[-100_000:]
            if completed.returncode == 0:
                revision = completed.stdout.strip().splitlines()[-1]
                if COMMIT_RE.fullmatch(revision):
                    return revision
        raise BackendError(f"git ref not found: {ref}")

    def _build(self, ref: str, build_type: str) -> None:
        with self.repo_lock:
            self._build_locked(ref, build_type)
        self.request_ref_refresh()

    def _build_locked(self, ref: str, build_type: str) -> None:
        try:
            self.source_dir.parent.mkdir(parents=True, exist_ok=True)
            if not (self.source_dir / ".git").is_dir():
                self._command(["git", "clone", "--recursive", self.repository, str(self.source_dir)])
            else:
                self._command(["git", "fetch", "--prune", "origin"], self.source_dir)
            revision = self._resolve_ref(ref)
            self._command(["git", "switch", "--detach", revision], self.source_dir)
            # This is a dedicated disposable build checkout. A previous tool
            # or Smithproxy `save config` must never leak tracked modifications
            # into an archived build bundle.
            self._command([
                "git", "restore", "--source", revision,
                "--staged", "--worktree", "--", ".",
            ], self.source_dir)
            self._command(["git", "submodule", "update", "--init", "--recursive"], self.source_dir)
            build_dir = self.source_dir / f"build-capture-zone-{build_type.lower()}"
            self._command(["cmake", "-S", str(self.source_dir), "-B", str(build_dir),
                           f"-DCMAKE_BUILD_TYPE={build_type}", "-DBUILD_TESTING=OFF"])
            self._command([
                "cmake", "--build", str(build_dir), "--target", "smithproxy",
                "-j", str(self.jobs),
            ])
            candidate = build_dir / "smithproxy"
            if not candidate.is_file():
                raise BackendError("build completed but smithproxy binary was not found")
            self.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.output.with_suffix(".new")
            shutil.copy2(candidate, temporary)
            os.chmod(temporary, 0o755)
            temporary.replace(self.output)
            source_config = self.source_dir / "etc" / "smithproxy.cfg"
            if not source_config.is_file():
                raise BackendError("build completed but matching etc/smithproxy.cfg was not found")
            committed_config = subprocess.run(
                ["git", "show", f"{revision}:etc/smithproxy.cfg"], cwd=self.source_dir,
                capture_output=True, timeout=30, check=False,
            )
            if (committed_config.returncode != 0
                    or source_config.read_bytes() != committed_config.stdout):
                raise BackendError(
                    "build checkout etc/smithproxy.cfg does not match the selected commit"
                )
            source_certs = self.source_dir / "etc" / "certs"
            source_messages = self.source_dir / "etc" / "msg"
            if not source_certs.is_dir() or not source_messages.is_dir():
                raise BackendError("build completed but matching Smithproxy runtime assets were not found")

            assets_output = self.output.parent / f"{self.output.name}.assets"
            assets_temporary = self.output.parent / f".{self.output.name}.assets.new"
            assets_previous = self.output.parent / f".{self.output.name}.assets.old"
            shutil.rmtree(assets_temporary, ignore_errors=True)
            shutil.rmtree(assets_previous, ignore_errors=True)
            (assets_temporary / "certs").mkdir(parents=True)
            shutil.copytree(source_certs, assets_temporary / "certs", dirs_exist_ok=True)
            shutil.copytree(source_messages, assets_temporary / "msg")
            if assets_output.exists():
                assets_output.replace(assets_previous)
            assets_temporary.replace(assets_output)
            shutil.rmtree(assets_previous, ignore_errors=True)

            config_output = self.output.with_suffix(".cfg")
            config_temporary = config_output.with_suffix(".cfg.new")
            shutil.copy2(source_config, config_temporary)
            os.chmod(config_temporary, 0o600)
            config_temporary.replace(config_output)
            revision = self._command(["git", "rev-parse", "HEAD"], self.source_dir).splitlines()[-1]
            commit_at = self._command(
                ["git", "show", "-s", "--format=%cI", revision], self.source_dir
            ).splitlines()[-1]
            self._archive_bundle(
                revision, ref, candidate, source_config, source_certs, source_messages,
                build_type, commit_at,
            )
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
