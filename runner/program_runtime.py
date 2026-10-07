"""Launch a program in the existing appliance Slice and owned netns, never host fs."""
from pathlib import Path
from .restart_policy import policy as restart_policy, properties as restart_properties
from .systemd import BackendError


def launch(backend, instance_id, allocation, work: Path, root: Path, program, restart, maximum):
    argv = program.get('argv')
    if (not root.is_dir() or not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and '\0' not in arg for arg in argv)
            or not argv[0].startswith('/') or '..' in Path(argv[0]).parts
            or not (root / argv[0].lstrip('/')).is_file()):
        raise BackendError('invalid prepared program rootfs')
    logs = work / 'logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    if program['application'] == 'router':
        # Only the trusted host-side controller gets network administration.
        backend._run(['ip', 'netns', 'exec', allocation.namespace, 'sysctl', '-q', '-w',
                      'net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1'])
    unit = backend.unit_name(instance_id)
    command = [
        'systemd-run', '--quiet', '--expand-environment=no', f'--unit={unit}',
        f'--slice={backend.slice_name(instance_id)}',
        '--property=Type=exec', '--property=KillMode=control-group',
        '--property=TimeoutStopSec=10s', '--property=MemoryMax=256M', '--property=TasksMax=64',
        f'--property=RootDirectory={root}',
        f'--property=NetworkNamespacePath=/run/netns/{allocation.namespace}',
        '--property=WorkingDirectory=/work', '--property=MountAPIVFS=yes',
        '--property=PrivatePIDs=yes',
        '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
        '--property=PrivateDevices=yes', '--property=PrivateTmp=yes',
        '--property=NoNewPrivileges=yes',
        '--property=CapabilityBoundingSet=' + ('CAP_NET_BIND_SERVICE' if program['application'] == 'webfsd' else ''),
        '--property=ProtectKernelTunables=yes', '--property=ProtectKernelModules=yes',
        '--property=ProtectControlGroups=yes', '--property=RestrictSUIDSGID=yes',
        '--property=RestrictNamespaces=yes',
        f'--property=BindPaths={work}:/work', f'--property=BindPaths={logs}:/logs',
        '--property=ReadWritePaths=/work /logs', '--setenv=HOME=/work',
        '--setenv=PATH=/usr/bin:/bin',
    ]
    if maximum:
        command.append(f'--property=RuntimeMaxSec={maximum}s')
    command += restart_properties(restart)
    if restart_policy(restart) == 'no':
        command.append('--collect')
    # systemd performs specifier expansion even without an intermediate shell.
    command += ['--', *(arg.replace('%', '%%') for arg in argv)]
    backend._run(command)
    return unit
