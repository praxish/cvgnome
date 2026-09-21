# SPDX-License-Identifier: MPL-2.0
"""Platform policy for parser memory supervision.

Linux uses a verified RLIMIT_AS in the parser child. macOS does not implement
that limit usefully, so the parent samples the resident size of the isolated
process group and kills it when the configured threshold is observed. This is
a watchdog, not a hard allocation cap: memory can exceed the threshold between
samples. Counting the group also covers a frozen PyInstaller worker.

Other platforms fail closed until a tested memory-control implementation exists.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from collections.abc import Callable


class MemoryMonitorUnavailable(RuntimeError):
    """The parser cannot be supervised on this platform."""


class _DarwinTaskInfo(ctypes.Structure):
    # Public proc_taskinfo ABI from Apple's <sys/proc_info.h>.
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("counters", ctypes.c_int32 * 12),
    ]


def _darwin_sampler() -> Callable[[int], int]:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        listpids = libproc.proc_listpids
        listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        listpids.restype = ctypes.c_int
        pidinfo = libproc.proc_pidinfo
        pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        raise MemoryMonitorUnavailable from exc

    def resident_bytes(pid: int) -> int | None:
        info = _DarwinTaskInfo()
        ctypes.set_errno(0)
        written = pidinfo(pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info))  # PROC_PIDTASKINFO
        if written == ctypes.sizeof(info):
            return int(info.resident_size)
        if written == 0 and ctypes.get_errno() == errno.ESRCH:
            return None  # An enumerated worker exited before it could be sampled.
        raise MemoryMonitorUnavailable

    # Verify the ABI and inspection permission before a document request is sent.
    if resident_bytes(os.getpid()) is None:
        raise MemoryMonitorUnavailable

    def sample(process_group: int) -> int:
        pids = (ctypes.c_int * 256)()
        ctypes.set_errno(0)
        written = listpids(2, process_group, pids, ctypes.sizeof(pids))  # PROC_PGRP_ONLY
        if written < 0 or written >= ctypes.sizeof(pids) or written % ctypes.sizeof(ctypes.c_int):
            raise MemoryMonitorUnavailable
        if written == 0:
            # An empty group after process exit is normal; the caller decides
            # whether the child is still running and therefore fails closed.
            raise MemoryMonitorUnavailable
        total = 0
        inspected = 0
        for pid in pids[: written // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            resident = resident_bytes(pid)
            if resident is not None:
                total += resident
                inspected += 1
        if not inspected:
            raise MemoryMonitorUnavailable
        return total

    return sample


def memory_sampler() -> Callable[[int], int] | None:
    """Return the macOS RSS sampler, or defer to Linux's verified child limit."""

    if sys.platform == "darwin":
        return _darwin_sampler()
    if sys.platform.startswith("linux"):
        return None
    raise MemoryMonitorUnavailable
