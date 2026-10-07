"""Restart flags shared by profiles, instances and systemd launchers."""
from .systemd import BackendError


def flags(value):
    if isinstance(value, bool):
        return {'on_exit': False, 'on_failure': value}
    if (not isinstance(value, dict) or set(value) - {'on_exit', 'on_failure'}
            or any(type(v) is not bool for v in value.values())):
        raise BackendError('auto_restart must contain boolean on_exit and on_failure flags')
    return {'on_exit': value.get('on_exit', False), 'on_failure': value.get('on_failure', False)}


def policy(value):
    selected = flags(value)
    return {(False, False): 'no', (True, False): 'on-success',
            (False, True): 'on-failure', (True, True): 'always'}[(selected['on_exit'], selected['on_failure'])]


def properties(value):
    mode = policy(value)
    return [f'--property=Restart={mode}'] + ([] if mode == 'no' else [
        '--property=RestartSec=2s', '--property=StartLimitIntervalSec=60s',
        '--property=StartLimitBurst=5'])
