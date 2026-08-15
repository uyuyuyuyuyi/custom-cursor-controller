"""
cockroach_gui_web.pyw — 自定义光标控制器（pywebview 桌面窗口版）

用 pywebview (WebView2) 承载 Vue 3 前端，Python 后端提供本地 HTTP API。

用法:
    pythonw cockroach_gui_web.pyw          # 正常启动
    python cockroach_gui_web.pyw --smoke   # 冒烟测试: 6 秒后自动退出
"""

import atexit
import sys
import threading

import pystray
import webview

import gui_server
from gui_server import CursorApp, start_server

SMOKE_SECONDS = 6


def main() -> None:
    smoke = "--smoke" in sys.argv

    app = CursorApp()
    server = start_server(app)
    url = f"http://127.0.0.1:{server.server_address[1]}"

    window = webview.create_window(
        "自定义光标控制器 · Custom Cursor Controller",
        url,
        width=920,
        height=720,
        min_size=(760, 560),
        background_color="#0e1116",
    )

    quitting = {"flag": False}

    # /api/quit → 真正退出（先恢复光标）
    gui_server.quit_callback = lambda: on_quit()

    def on_quit():
        if quitting["flag"]:
            return
        quitting["flag"] = True
        try:
            window.destroy()
        except Exception:
            pass

    def on_closing():
        # 光标生效中 → 最小化到托盘；否则真正关闭
        if app.enabled and not quitting["flag"]:
            window.hide()
            if tray is not None:
                try:
                    tray.notify("光标保持生效，程序在后台运行；右键托盘图标可退出",
                                title="自定义光标控制器")
                except Exception:
                    pass
            return False  # 取消关闭
        return True

    # ── 托盘 ──────────────────────────────────────────
    tray = None

    def setup_tray():
        nonlocal tray

        def _show(_i=None, _m=None):
            try:
                window.show()
                window.restore()
            except Exception:
                pass

        def _toggle(_i=None, _m=None):
            try:
                if app.enabled:
                    app.restore()
                elif app.frames:
                    app.apply()
                tray.notify("自定义光标已启用" if app.enabled else "已恢复默认光标",
                            title="自定义光标控制器")
            except Exception as e:
                tray.notify(f"操作失败: {e}", title="自定义光标控制器")

        def _restore(_i=None, _m=None):
            try:
                app.restore()
                tray.notify("已恢复默认光标", title="自定义光标控制器")
            except Exception as e:
                tray.notify(f"恢复失败: {e}", title="自定义光标控制器")

        def _quit(_i=None, _m=None):
            on_quit()

        tray = pystray.Icon(
            name="cockroach_cursor_web",
            title="自定义光标控制器",
            icon=_placeholder_icon(64),
            menu=pystray.Menu(
                pystray.MenuItem("打开主界面", _show, default=True),
                pystray.MenuItem("启用/停用自定义光标", _toggle),
                pystray.MenuItem("恢复默认光标", _restore),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出（恢复光标）", _quit),
            ),
        )
        tray.run()

    def _placeholder_icon(size: int = 64):
        from PIL import Image, ImageDraw
        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(icon)
        d.ellipse((5, 5, size - 5, size - 5), fill=(30, 36, 46, 255))
        d.ellipse((size * 0.32, size * 0.32, size * 0.68, size * 0.68),
                  fill=(79, 140, 255, 255))
        return icon

    try:
        threading.Thread(target=setup_tray, daemon=True).start()
    except Exception:
        tray = None

    window.events.closing += on_closing

    def after_start():
        if smoke:
            threading.Timer(SMOKE_SECONDS, on_quit).start()

    atexit.register(app.cleanup)
    webview.start(after_start)
    app.cleanup()


if __name__ == "__main__":
    main()
