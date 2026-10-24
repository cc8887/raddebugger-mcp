from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .errors import RadError


def list_processes(name_contains: str = "") -> list[dict[str, int | str]]:
    if os.name != "nt":
        raise RadError("UNSUPPORTED_HOST", "This release supports Windows x64 hosts.")

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for name in ("Process32FirstW", "Process32NextW"):
        fn = getattr(kernel, name)
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        fn.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise RadError("PROCESS_LIST_FAILED", f"Windows error {ctypes.get_last_error()}.")
    entry = Entry(dwSize=ctypes.sizeof(Entry))
    result = []
    try:
        ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            if name_contains.casefold() in entry.szExeFile.casefold():
                result.append({"pid": entry.th32ProcessID, "name": entry.szExeFile, "parent_pid": entry.th32ParentProcessID})
            ok = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return sorted(result, key=lambda p: p["pid"])
