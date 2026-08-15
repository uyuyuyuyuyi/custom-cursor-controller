"""
cockroach_cursor.pyw — 白色蟑螂鼠标指针替换工具

系统托盘驻留程序，将 Windows 箭头光标替换为白色蟑螂动画光标。
右键托盘图标可切换蟑螂光标 / 恢复默认箭头 / 退出。

使用方法:
    双击运行 (pythonw.exe cockroach_cursor.pyw)
    或命令行: python cockroach_cursor.pyw
"""

import os
import sys
import atexit
import ctypes
from ctypes import wintypes

import pystray
from PIL import Image

from cursor_drawer import (
    generate_frames, generate_tray_icon,
    NUM_FRAMES, FRAME_DURATION_MS, HOTSPOT,
)
from ani_builder import save_ani_file

# ── 路径常量 ──────────────────────────────────────────
# 兼容 PyInstaller 打包：exe 运行时用 sys.executable 所在目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCES_DIR = os.path.join(BASE_DIR, "resources")
ANI_PATH = os.path.join(RESOURCES_DIR, "cockroach.ani")
TRAY_ICON_PATH = os.path.join(RESOURCES_DIR, "tray_icon.png")

# PyInstaller 打包时资源内嵌在 _MEIPASS 中（onefile=临时解压目录, onedir=_internal）
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BUNDLED_RESOURCES_DIR = os.path.join(sys._MEIPASS, "resources")
else:
    BUNDLED_RESOURCES_DIR = None

# ── Windows API 常量 ───────────────────────────────────
OCR_NORMAL = 32512          # 普通箭头光标
IMAGE_CURSOR = 2
LR_COPYFROMRESOURCE = 0x00004000

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

SPI_SETCURSORS = 0x0057
SPIF_SENDCHANGE = 0x0002


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

    def replace_with_cockroach(self, ani_path: str | None = None) -> None:
        """用动画光标替换系统箭头。

        Args:
            ani_path: 要加载的 .ani 文件路径；None 时使用模块默认路径
                      （resources\\cockroach.ani 或 exe 内嵌资源）。
        """
        path = ani_path or ANI_PATH
        h_new = user32.LoadCursorFromFileW(path)
        _check(h_new, f"LoadCursorFromFileW 失败: {path}")

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


# ── 全局实例 ──────────────────────────────────────────
_cursor_mgr = CursorManager()


# ── 资源初始化 ────────────────────────────────────────

def ensure_resources() -> None:
    """确保 .ani 光标文件和托盘图标可用。

    打包版 (PyInstaller) 优先使用内嵌在 exe 中的资源，不写盘、不生成
    resources 文件夹；仅当无内嵌资源（源码直接运行时）才在磁盘上生成。
    """
    global ANI_PATH, TRAY_ICON_PATH

    # 打包版：使用内嵌资源
    if BUNDLED_RESOURCES_DIR is not None:
        bundled_ani = os.path.join(BUNDLED_RESOURCES_DIR, "cockroach.ani")
        bundled_icon = os.path.join(BUNDLED_RESOURCES_DIR, "tray_icon.png")
        if os.path.exists(bundled_ani) and os.path.exists(bundled_icon):
            ANI_PATH = bundled_ani
            TRAY_ICON_PATH = bundled_icon
            return

    # 源码运行：生成到 exe/脚本所在目录的 resources 文件夹
    os.makedirs(RESOURCES_DIR, exist_ok=True)

    if not os.path.exists(ANI_PATH):
        print(f"正在生成蟑螂光标: {ANI_PATH} ...")
        frames = generate_frames()
        hotspots = [HOTSPOT] * NUM_FRAMES
        durations = [FRAME_DURATION_MS] * NUM_FRAMES
        save_ani_file(ANI_PATH, frames, hotspots, durations,
                      title="Cockroach Cursor")
        print("光标文件生成完成。")

    if not os.path.exists(TRAY_ICON_PATH):
        icon = generate_tray_icon(64)
        icon.save(TRAY_ICON_PATH)


# ── 系统托盘菜单回调 ──────────────────────────────────

def on_enable_cockroach(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """启用蟑螂光标。"""
    try:
        _cursor_mgr.replace_with_cockroach()
        icon.notify("蟑螂光标已启用 🪳", title="光标替换")
    except Exception as e:
        icon.notify(f"启用失败: {e}", title="错误")

def on_restore_default(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """恢复默认箭头光标。"""
    try:
        _cursor_mgr.restore_original()
        icon.notify("已恢复默认箭头光标", title="光标替换")
    except Exception:
        # 备用方案：通过系统参数恢复全部光标
        try:
            _cursor_mgr.reload_system_defaults()
            icon.notify("已通过系统恢复默认光标", title="光标替换")
        except Exception as e:
            icon.notify(f"恢复失败: {e}", title="错误")

def on_quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """退出程序。"""
    icon.stop()


# ── 构建菜单 ──────────────────────────────────────────

def build_menu() -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem("🪳 启用蟑螂光标", on_enable_cockroach, default=True),
        pystray.MenuItem("↩ 恢复默认箭头", on_restore_default),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ 退出", on_quit),
    )


# ── 清理 ──────────────────────────────────────────────

def cleanup() -> None:
    """程序退出时恢复光标并释放资源。"""
    try:
        _cursor_mgr.restore_original()
    except Exception:
        try:
            _cursor_mgr.reload_system_defaults()
        except Exception:
            pass
    _cursor_mgr.cleanup()


# ── 主入口 ────────────────────────────────────────────

def main() -> None:
    # 注册退出清理
    atexit.register(cleanup)

    # 初始化资源
    ensure_resources()

    # 备份原始光标
    _cursor_mgr.backup_original()

    # 替换为蟑螂光标
    _cursor_mgr.replace_with_cockroach()

    # 加载托盘图标
    tray_image = Image.open(TRAY_ICON_PATH)

    # 创建系统托盘图标
    icon = pystray.Icon(
        name="cockroach_cursor",
        title="蟑螂光标 🪳",
        icon=tray_image,
        menu=build_menu(),
    )

    # 运行 (阻塞直到 icon.stop())
    icon.run()


if __name__ == "__main__":
    main()
