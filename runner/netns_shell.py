"""Privileged administrator shell: join ONLY the instance network namespace."""
import fcntl
import os
from pathlib import Path
import pty
import struct
import subprocess
import sys
import termios
import uuid

from .namespace import GdbPtyTransport
from .systemd import BackendError


class NetnsTransport(GdbPtyTransport):
    def resize(self, columns, rows):
        if not (isinstance(columns, int) and isinstance(rows, int)
                and 2 <= columns <= 500 and 2 <= rows <= 300):
            raise ValueError('invalid terminal dimensions')
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))


def open_shell(instance_id, namespace):
    if str(uuid.UUID(instance_id)) != instance_id or namespace != 'cz-' + instance_id[:8]:
        raise BackendError('invalid instance network namespace')
    fd = master = slave = None
    try:
        # Pin this namespace before starting the child; never fall back to host netns.
        fd = os.open('/run/netns/' + namespace, os.O_RDONLY | os.O_NOFOLLOW)
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, '-m', 'runner.netns_shell', str(fd), namespace],
            cwd=Path(__file__).resolve().parent.parent,
            env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                 'TERM': 'xterm-256color', 'HOME': '/', 'HISTFILE': '/dev/null',
                 'LANG': 'C.UTF-8', 'PS1': '[NetNS ' + namespace + ' | HOST FS] \\w # '},
            stdin=slave, stdout=slave, stderr=slave, pass_fds=(fd,),
            start_new_session=True,
        )
        transport = NetnsTransport(process, master)
        master = None
        transport.resize(100, 28)
        return transport
    except OSError as exc:
        raise BackendError(f'cannot open NetNS shell: {exc}') from exc
    finally:
        for handle in (fd, master, slave):
            if handle is not None:
                os.close(handle)


if __name__ == '__main__':
    namespace_fd = int(sys.argv[1])
    os.setns(namespace_fd, os.CLONE_NEWNET)
    os.close(namespace_fd)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir('/')
    os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-i'], os.environ)
