"""Free-RAM reader (GlobalMemoryStatusEx) and the launch gate used by the scheduler."""
import ctypes

from cp import config


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _status() -> _MemoryStatusEx:
    st = _MemoryStatusEx()
    st.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx failed")
    return st


def free_gb() -> float:
    """Available physical memory in GiB."""
    return _status().ullAvailPhys / (1024 ** 3)


def can_launch(active: int, floor_gb: float | None = None, max_agents: int | None = None) -> bool:
    """True when free RAM is at least `floor_gb` (default RAM_FLOOR_GB) and fewer than `max_agents`
    (default MAX_AGENTS) agent processes are active."""
    floor_gb = config.RAM_FLOOR_GB if floor_gb is None else floor_gb
    max_agents = config.MAX_AGENTS if max_agents is None else max_agents
    return active < max_agents and free_gb() >= floor_gb
