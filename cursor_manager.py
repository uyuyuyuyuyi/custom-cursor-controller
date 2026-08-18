"""
cursor_manager.py — Win32 系统光标管理（备份 / 替换 / 恢复）

从最初版本（cockroach_cursor.pyw）提取的核心模块：
通过 LoadCursorFromFileW → SetSystemCursor 替换系统箭头光标 (OCR_NORMAL)，
备份原始光标句柄，恢复时广播 SPI_SETCURSORS 让所有窗口重新加载光标方案。
"""

from __future__ import annotations

import ctypes
import os
import time
import winreg
from ctypes import wintypes

# ── Windows API 常量 ───────────────────────────────────
OCR_NORMAL = 32512          # 普通箭头光标
IMAGE_CURSOR = 2
LR_COPYFROMRESOURCE = 0x00004000
LR_LOADFROMFILE = 0x00000010
SPI_SETCURSORS = 0x0057
SPIF_SENDCHANGE = 0x0002
SPIF_UPDATEINIFILE = 0x0001
CURSORS_REG_KEY = r"Control Panel\Cursors"
ACCESSIBILITY_REG_KEY = r"Software\Microsoft\Accessibility"
BASE_SIZE_NAME = "CursorBaseSize"
CURSOR_SIZE_NAME = "CursorSize"
ARROW_NAME = "Arrow"
MIN_BASE_SIZE = 16
MAX_BASE_SIZE = 256
MIN_APPLIED_SIZE = 32   # Windows 指针大小滑块的最小值（32px）
STEP_BASE_SIZE = 16     # Windows 指针大小每档 16px（32→1档, 48→2档, ...）

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

# HANDLE LoadImageW(HINSTANCE, LPCWSTR, UINT, int, int, UINT)
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE

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


def _read_base_size() -> int:
    """读取系统指针基础尺寸（默认 32，对应 Windows"鼠标指针大小"设置）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, BASE_SIZE_NAME)
            return max(MIN_BASE_SIZE, min(MAX_BASE_SIZE, int(value)))
    except OSError:
        return 32


def _read_reg_value(key_path: str, name: str):
    """读取注册表值；键或值不存在时返回 None。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def _write_reg_value(key_path: str, name: str, value, reg_type: int) -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, name, 0, reg_type, value)


def _delete_reg_value(key_path: str, name: str) -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass  # 值本来就不存在，视为已还原


def _write_base_size(size: int) -> None:
    _write_reg_value(
        CURSORS_REG_KEY, BASE_SIZE_NAME, _snap_base_size(size), winreg.REG_DWORD)


def _base_size_to_slider(size: int) -> int:
    """把像素尺寸映射为辅助功能滑块档位（1~15，每档 16px，32px=1 档）。

    Windows 的“鼠标指针大小”滑块只有 15 档：
    32→1, 48→2, 64→3, ..., 256→15。旧实现用 size//8-1 会把
    64px 写成 7 档（对应 128px），与 CursorBaseSize 不一致，
    导致系统忽略该设置。
    """
    snapped = _snap_base_size(size)
    return max(1, min(15, ((snapped - MIN_APPLIED_SIZE) // STEP_BASE_SIZE) + 1))


def _snap_base_size(size: int) -> int:
    """把请求尺寸钳制并吸附到 Windows 合法的 16px 档位（32~256）。"""
    size = max(MIN_APPLIED_SIZE, min(MAX_BASE_SIZE, int(size)))
    return MIN_APPLIED_SIZE + STEP_BASE_SIZE * (
        (size - MIN_APPLIED_SIZE + STEP_BASE_SIZE // 2) // STEP_BASE_SIZE
    )


# ── 光标管理 ──────────────────────────────────────────

class CursorManager:
    """管理系统光标的备份、替换和恢复。"""

    def __init__(self):
        self._saved_original: int | None = None  # 原始箭头光标备份句柄
        self._saved_base_size: int | None = None  # 原始指针基础尺寸
        self._saved_arrow: str | None = None      # 原始 Arrow 方案值（None=不存在）
        self._saved_cursor_size: int | None = None  # 原始辅助功能 CursorSize
        self._current_ani_path: str | None = None  # 当前自定义 .ani 路径
        self._current_size: int = _snap_base_size(_read_base_size())

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
        self._saved_base_size = _read_base_size()
        self._saved_arrow = _read_reg_value(CURSORS_REG_KEY, ARROW_NAME)
        self._saved_cursor_size = _read_reg_value(
            ACCESSIBILITY_REG_KEY, CURSOR_SIZE_NAME)

    @staticmethod
    def get_base_size() -> int:
        """读取当前系统指针基础尺寸。"""
        return _read_base_size()

    def replace_cursor(self, ani_path: str, size: int | None = None) -> None:
        """用 .ani 动画光标替换系统箭头。

        Args:
            ani_path: 要加载的 .ani 文件路径（由调用方生成/管理）。
            size: 目标显示尺寸（32~256，按 16px 档位吸附）。不能省略，
                 LoadCursorFromFileW 会按当前系统指针大小把文件缩到
                 32/48px，必须用 LoadImageW 显式指定目标尺寸。
        """
        size = _snap_base_size(size if size is not None else self._current_size)
        h_new = user32.LoadImageW(
            None, ani_path, IMAGE_CURSOR, size, size, LR_LOADFROMFILE)
        _check(h_new, f"LoadImageW 失败: {ani_path}")

        self._current_ani_path = ani_path
        self._current_size = size
        ok = user32.SetSystemCursor(h_new, OCR_NORMAL)
        if not ok:
            # SetSystemCursor 失败时 h_new 未被销毁，需手动清理
            user32.DestroyCursor(h_new)
            _check(False, "SetSystemCursor 替换光标失败")
        # 成功：SetSystemCursor 已接管 h_new，无需手动销毁
        # 同时把 .ani 写进系统光标方案：SPI_SETCURSORS 重载方案时
        # 会按当前指针尺寸加载我们的光标，保证尺寸调整真正生效。
        try:
            _write_reg_value(
                CURSORS_REG_KEY, ARROW_NAME, ani_path, winreg.REG_SZ)
        except OSError:
            pass  # 注册表不可写时仍以 SetSystemCursor 生效

    def apply_size(self, size: int) -> bool:
        """持久化指针尺寸设置（注册表）；实际显示尺寸由 replace_cursor
        的 LoadImageW(目标尺寸) + SetSystemCursor 立即生效。

        Windows 的"鼠标指针大小"注册表值（CursorBaseSize +
        Accessibility\\CursorSize）只会在注销/重启或 Explorer 重载后
        重新计算缩放；SPI_SETCURSORS 只重载光标方案、不会重新计算缩放，
        反而会把 SetSystemCursor 的大尺寸光标冲回系统尺寸，因此这里
        不再广播 SPI_SETCURSORS。
        """
        size = _snap_base_size(size)
        ok = True
        try:
            _write_base_size(size)
            _write_reg_value(
                ACCESSIBILITY_REG_KEY, CURSOR_SIZE_NAME,
                _base_size_to_slider(size), winreg.REG_DWORD)
            if self._current_ani_path:
                _write_reg_value(
                    CURSORS_REG_KEY, ARROW_NAME,
                    self._current_ani_path, winreg.REG_SZ)
        except OSError:
            ok = False
        return ok

    def _reapply_current(self) -> None:
        """重新 SetSystemCursor 当前自定义光标（用于方案重载后的复挂）。"""
        if not self._current_ani_path or not os.path.isfile(self._current_ani_path):
            return
        h = user32.LoadImageW(
            None, self._current_ani_path, IMAGE_CURSOR,
            self._current_size, self._current_size, LR_LOADFROMFILE)
        if not h:
            return
        if not user32.SetSystemCursor(h, OCR_NORMAL):
            user32.DestroyCursor(h)

    def restore_original(self) -> None:
        """恢复原始系统箭头光标，并广播刷新所有窗口的光标方案。

        Windows 上 SetSystemCursor 修改后，explorer 等窗口会缓存光标，
        必须再发 SPI_SETCURSORS 广播 (SPIF_SENDCHANGE) 让所有窗口
        从系统设置重新加载光标，否则用户看到的光标不会更新。
        """
        errors = []

        # 0) 恢复原始指针基础尺寸（先写注册表，让随后的广播按原尺寸重载方案）
        if self._saved_base_size is not None:
            try:
                _write_base_size(self._saved_base_size)
            except OSError:
                errors.append("恢复指针尺寸失败")
        # 0b) 恢复光标方案 Arrow 值（原值不存在则删除，回到系统默认）
        try:
            if self._saved_arrow is not None:
                _write_reg_value(
                    CURSORS_REG_KEY, ARROW_NAME,
                    self._saved_arrow, winreg.REG_SZ)
            else:
                _delete_reg_value(CURSORS_REG_KEY, ARROW_NAME)
        except OSError:
            errors.append("恢复光标方案失败")
        # 0c) 恢复辅助功能 CursorSize（原值不存在则删除）
        try:
            if self._saved_cursor_size is not None:
                _write_reg_value(
                    ACCESSIBILITY_REG_KEY, CURSOR_SIZE_NAME,
                    self._saved_cursor_size, winreg.REG_DWORD)
            else:
                _delete_reg_value(ACCESSIBILITY_REG_KEY, CURSOR_SIZE_NAME)
        except OSError:
            errors.append("恢复辅助功能指针尺寸失败")

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
        if self._saved_base_size is not None:
            try:
                _write_base_size(self._saved_base_size)
            except OSError:
                pass
        try:
            if self._saved_arrow is not None:
                _write_reg_value(
                    CURSORS_REG_KEY, ARROW_NAME,
                    self._saved_arrow, winreg.REG_SZ)
            else:
                _delete_reg_value(CURSORS_REG_KEY, ARROW_NAME)
        except OSError:
            pass
        try:
            if self._saved_cursor_size is not None:
                _write_reg_value(
                    ACCESSIBILITY_REG_KEY, CURSOR_SIZE_NAME,
                    self._saved_cursor_size, winreg.REG_DWORD)
            else:
                _delete_reg_value(ACCESSIBILITY_REG_KEY, CURSOR_SIZE_NAME)
        except OSError:
            pass
        ok = user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, SPIF_SENDCHANGE)
        if not ok:
            raise ctypes.WinError(
                kernel32.GetLastError(), "SPI_SETCURSORS 广播失败")

    def cleanup(self) -> None:
        """释放备份句柄。"""
        if self._saved_original is not None:
            user32.DestroyCursor(self._saved_original)
            self._saved_original = None
