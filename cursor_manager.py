"""
cursor_manager.py — Win32 系统光标管理（备份 / 替换 / 恢复）

从最初版本（cockroach_cursor.pyw）提取的核心模块：
通过 LoadCursorFromFileW → SetSystemCursor 替换系统箭头光标 (OCR_NORMAL)，
备份原始光标句柄，恢复时广播 SPI_SETCURSORS 让所有窗口重新加载光标方案。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

# ── Windows API 常量 ───────────────────────────────────
OCR_NORMAL = 32512          # 普通箭头光标
IMAGE_CURSOR = 2
LR_COPYFROMRESOURCE = 0x00004000
SPI_SETCURSORS = 0x0057
SPIF_SENDCHANGE = 0x0002

# ── ctypes 绑定 ───────────────────────────────────────
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# HCURSOR is a typedef of HICON which is a typedef of HANDLE
HCURSOR = wintypes.HANDLE

# HCURSOR LoadCursorW(HINSTANCE, LPCWSTR)
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = HCURSOR

# HANDLE CopyImage(HANDLE, UINT, int, int, UINT)
user32.CopyImage.argtypes = [
    wintypes.HANDLE, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.CopyImage.restype = wintypes.HANDLE

# HCURSOR LoadCursorFromFileW(LPCWSTR)
user32.LoadCursorFromFileW.argtypes = [ctypes.c_wchar_p]
user32.LoadCursorFromFileW.restype = HCURSOR

# BOOL SetSystemCursor(HCURSOR, DWORD)
user32.SetSystemCursor.argtypes = [HCURSOR, wintypes.DWORD]
user32.SetSystemCursor.restype = wintypes.BOOL

# BOOL DestroyCursor(HCURSOR)
user32.DestroyCursor.argtypes = [HCURSOR]
user32.DestroyCursor.restype = wintypes.BOOL

# BOOL SystemParametersInfoW(UINT, UINT, PVOID, UINT)
user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT,
]
user32.SystemParametersInfoW.restype = wintypes.BOOL


def _makeintresource(i: int) -> wintypes.LPCWSTR:
    """将整数 ID 转换为 Win32 资源指针。"""
    return ctypes.cast(i, wintypes.LPCWSTR)


def _check(ok: bool, msg: str) -> None:
    """检查 API 调用结果，失败时抛出 WinError。"""
    if not ok:
        err = kernel32.GetLastError()
        raise ctypes.WinError(err, msg)


# ── 光标管理 ──────────────────────────────────────────

class CursorManager:
    """管理系统光标的备份、替换和恢复。"""

    def __init__(self):
        self._saved_original: int | None = None  # 原始箭头光标备份句柄

    def backup_original(self) -> None:
        """备份当前系统箭头光标句柄（供后续恢复）。

        注意: 不能用 LR_COPYFROMRESOURCE —— 该标志要求 hImage 是资源句柄,
        而 LoadCursorW 返回的是已加载的共享句柄, 两者不匹配会导致备份
        句柄无效, 恢复时 SetSystemCursor 看似成功实则不生效。
        """
        # 获取系统默认箭头的共享句柄
        h_shared = user32.LoadCursorW(None, _makeintresource(OCR_NORMAL))
        _check(h_shared, "LoadCursorW 失败")

        # 复制一份独立句柄保存 (flags=0: 从已加载句柄复制)
        h_copy = user32.CopyImage(h_shared, IMAGE_CURSOR, 0, 0, 0)
        _check(h_copy, "CopyImage 备份原始光标失败")

        self._saved_original = h_copy

    def replace_cursor(self, ani_path: str) -> None:
        """用 .ani 动画光标替换系统箭头。

        Args:
            ani_path: 要加载的 .ani 文件路径（由调用方生成/管理）。
        """
        h_new = user32.LoadCursorFromFileW(ani_path)
        _check(h_new, f"LoadCursorFromFileW 失败: {ani_path}")

        ok = user32.SetSystemCursor(h_new, OCR_NORMAL)
        if not ok:
            # SetSystemCursor 失败时 h_new 未被销毁，需手动清理
            user32.DestroyCursor(h_new)
            _check(False, "SetSystemCursor 替换光标失败")
        # 成功：SetSystemCursor 已接管 h_new，无需手动销毁

    def restore_original(self) -> None:
        """恢复原始系统箭头光标，并广播刷新所有窗口的光标方案。

        Windows 上 SetSystemCursor 修改后，explorer 等窗口会缓存光标，
        必须再发 SPI_SETCURSORS 广播 (SPIF_SENDCHANGE) 让所有窗口
        从系统设置重新加载光标，否则用户看到的光标不会更新。
        """
        errors = []

        # 1) 立即恢复箭头 (SetSystemCursor 会销毁传入的句柄, 故从备份复制)
        if self._saved_original is not None:
            h_restore = user32.CopyImage(self._saved_original, IMAGE_CURSOR, 0, 0, 0)
            if h_restore:
                ok = user32.SetSystemCursor(h_restore, OCR_NORMAL)
                if not ok:
                    user32.DestroyCursor(h_restore)
                    errors.append(f"SetSystemCursor 恢复光标失败 (err={kernel32.GetLastError()})")
            else:
                errors.append(f"CopyImage 恢复光标失败 (err={kernel32.GetLastError()})")

        # 2) 广播刷新: 让所有窗口(含 explorer)从系统设置重新加载光标方案
        ok = user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, SPIF_SENDCHANGE)
        if not ok:
            errors.append(f"SPI_SETCURSORS 广播失败 (err={kernel32.GetLastError()})")

        if errors:
            raise ctypes.WinError(kernel32.GetLastError(), "; ".join(errors))

    def reload_system_defaults(self) -> None:
        """通过 SPI_SETCURSORS 广播恢复所有系统光标默认值。"""
        ok = user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, SPIF_SENDCHANGE)
        if not ok:
            raise ctypes.WinError(
                kernel32.GetLastError(), "SPI_SETCURSORS 广播失败")

    def cleanup(self) -> None:
        """释放备份句柄。"""
        if self._saved_original is not None:
            user32.DestroyCursor(self._saved_original)
            self._saved_original = None
