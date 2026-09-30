from __future__ import annotations

from dataclasses import dataclass


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnitStatus:
    active_state: str
    sub_state: str
    result: str
    main_pid: int = 0
