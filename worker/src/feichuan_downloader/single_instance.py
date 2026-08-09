"""Windows single-instance guard used before importing the GUI."""

from __future__ import annotations

import atexit
import ctypes
from dataclasses import dataclass

from .config import APP_NAME, PRODUCT_KEY


ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


@dataclass
class SingleInstanceGuard:
    handle: int | None

    def close(self) -> None:
        if not self.handle:
            return
        try:
            ctypes.windll.kernel32.CloseHandle(self.handle)
        finally:
            self.handle = None


def activate_existing_window() -> bool:
    """Restore the already-running main window if Windows can find it."""

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def ensure_single_instance() -> SingleInstanceGuard | None:
    """Return a guard for the first process, or None for a duplicate launch."""

    kernel32 = ctypes.windll.kernel32
    mutex_name = f"Local\\{PRODUCT_KEY}_single_instance"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return SingleInstanceGuard(None)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        activate_existing_window()
        return None
    guard = SingleInstanceGuard(handle)
    atexit.register(guard.close)
    return guard
