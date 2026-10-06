"""Durable, private runner state. No deployment secrets belong in API records."""
import fcntl
import json
import os
import tempfile
import socket
from pathlib import Path


def atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(document, separators=(",", ":"))
    try:
        if path.read_text() == encoded and path.stat().st_mode & 0o777 == 0o600:
            return
    except FileNotFoundError:
        pass
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def acquire_runner_lock(state_dir: Path):
    """Hold the returned file open for the entire process lifetime."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_dir / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    stream = os.fdopen(fd, "w")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise SystemExit("another runner already owns this state directory")
    return stream


def notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.connect(address)
        connection.sendall(message.encode())
