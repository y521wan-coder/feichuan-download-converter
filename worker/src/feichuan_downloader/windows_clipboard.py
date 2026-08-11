"""Write sensitive direct-link text to the interactive Windows clipboard.

The caller must never log ``text`` or return it through the worker protocol.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardWriteError(RuntimeError):
    """A user-facing clipboard failure that never contains clipboard text."""


def copy_text_to_clipboard(text: str, *, attempts: int = 10, delay: float = 0.05) -> None:
    """Place Unicode text on the clipboard without crossing stdout or JSON."""

    value = str(text or "")
    if not value:
        raise ClipboardWriteError("没有可复制的直连。")
    if attempts <= 0 or delay < 0:
        raise ValueError("剪贴板重试参数无效。")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    owner = user32.CreateWindowExW(
        0,
        "STATIC",
        "FeichuanClipboardOwner",
        0,
        0,
        0,
        0,
        0,
        None,
        None,
        None,
        None,
    )
    if not owner:
        raise ClipboardWriteError("无法创建系统剪贴板所有者窗口。")

    encoded = value.encode("utf-16-le") + b"\x00\x00"
    memory = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not memory:
        user32.DestroyWindow(owner)
        raise ClipboardWriteError("系统内存不足，无法写入剪贴板。")

    clipboard_open = False
    ownership_transferred = False
    try:
        pointer = kernel32.GlobalLock(memory)
        if not pointer:
            raise ClipboardWriteError("无法准备系统剪贴板内容。")
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(memory)

        for attempt in range(attempts):
            if user32.OpenClipboard(owner):
                clipboard_open = True
                break
            if attempt + 1 < attempts:
                time.sleep(delay)
        if not clipboard_open:
            raise ClipboardWriteError("系统剪贴板正被其它程序占用，请稍后重试。")
        if not user32.EmptyClipboard():
            raise ClipboardWriteError("无法清空系统剪贴板，请稍后重试。")
        if not user32.SetClipboardData(CF_UNICODETEXT, memory):
            raise ClipboardWriteError("无法把直连写入系统剪贴板，请稍后重试。")
        ownership_transferred = True
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        if not ownership_transferred:
            kernel32.GlobalFree(memory)
        user32.DestroyWindow(owner)
