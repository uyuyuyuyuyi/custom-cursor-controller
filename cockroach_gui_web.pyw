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

# 兼容 Windows GBK 控制台（调试输出用）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SMOKE_SECONDS = 6


def main() -> None:
    smoke = "--smoke" in sys.argv
    demo = "--demo" in sys.argv
    verify = "--verify" in sys.argv

    app = CursorApp()
    server = start_server(app)
    url = f"http://127.0.0.1:{server.server_address[1]}"

    # --demo: 启动前注入一张测试图，供可视化验证预览渲染
    if demo:
        import io
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.polygon([(6, 40), (6, 14), (18, 14), (24, 4), (30, 14), (42, 14), (42, 40)],
                  fill=(255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        r = app.upload([("demo.png", buf.getvalue())])
        print("DEMO_UPLOADED frames=%s verdict=%s" % (r["frames"], r["verdict"]), flush=True)

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
        if verify:
            # 探针: 采样画布像素，验证预览是否铺满整个画布
            probe_js = """(function(){
                var c = document.querySelector('canvas');
                if (!c) return 'NO_CANVAS';
                var ctx = c.getContext('2d');
                function px(x, y) {
                    var d = ctx.getImageData(x, y, 1, 1).data;
                    return [d[0], d[1], d[2], d[3]];
                }
                return JSON.stringify({
                    canvasW: c.width, canvasH: c.height,
                    far_bottom_right: px(160, 160),
                    near_top_left: px(16, 16),
                    center: px(96, 96),
                    hotspot_area: px(96, 88),
                    errorBanner: !!document.querySelector('.error-banner'),
                    verdictBadge: !!document.querySelector('.verdict-badge'),
                    metaTags: document.querySelectorAll('.tag').length,
                    bodySnippet: document.body.innerText.slice(0, 200)
                });
            })()"""

            def on_result(result):
                print("PROBE_RESULT:", result, flush=True)

            def probe():
                try:
                    result = window.evaluate_js(probe_js, callback=on_result)
                    if result is not None:
                        print("PROBE_DIRECT:", result, flush=True)
                except Exception as e:
                    print("PROBE_ERROR:", e, flush=True)

            # 页面加载完成后 5 秒再探测（确保前端已拉取状态并完成绘制）
            threading.Timer(5.0, probe).start()

            # 第二阶段: 点击"光标大小=48"按钮，验证尺寸切换真正生效
            click_js = """(function(){
                var btns = document.querySelectorAll('.size-btn');
                if (!btns.length) return 'NO_SIZE_BTN';
                btns[0].click();
                return 'CLICKED';
            })()"""

            def click_probe():
                try:
                    r1 = window.evaluate_js(click_js)
                    print("SIZE_CLICK:", r1, flush=True)
                except Exception as e:
                    print("SIZE_CLICK_ERROR:", e, flush=True)

                import json as _json
                import urllib.request as _ur

                def fetch_state():
                    try:
                        with _ur.urlopen(url + "/api/state", timeout=10) as resp:
                            st = _json.loads(resp.read().decode("utf-8"))
                        print("STATE_AFTER_CLICK: canvas_size=%s frames=%s" % (
                            st.get("canvas_size"), st.get("frames")), flush=True)
                    except Exception as e:
                        print("STATE_AFTER_CLICK_ERROR:", e, flush=True)

                threading.Timer(2.0, fetch_state).start()

            threading.Timer(8.0, click_probe).start()

            # 第三阶段: 图库→控制台切换后，画布必须自动重绘（修复画布丢失）
            def tab_probe():
                try:
                    r = window.evaluate_js("""(function(){
                        var tabs = document.querySelectorAll('.tab');
                        if (tabs.length < 2) return 'NO_TABS';
                        tabs[1].click();
                        return 'TO_GALLERY';
                    })()""")
                    print("TAB_TO_GALLERY:", r, flush=True)
                except Exception as e:
                    print("TAB_TO_GALLERY_ERROR:", e, flush=True)

                def back_and_check():
                    try:
                        window.evaluate_js("""(function(){
                            var tabs = document.querySelectorAll('.tab');
                            tabs[0].click();
                            return 'BACK';
                        })()""")
                    except Exception as e:
                        print("TAB_BACK_ERROR:", e, flush=True)

                    def check_pixels():
                        js = """(function(){
                            var c = document.querySelector('canvas');
                            if (!c) return 'NO_CANVAS';
                            var ctx = c.getContext('2d');
                            var d = ctx.getImageData(160, 160, 1, 1).data;
                            return JSON.stringify({w: c.width, h: c.height,
                                                   far: [d[0], d[1], d[2], d[3]]});
                        })()"""
                        try:
                            print("AFTER_TAB_BACK:", window.evaluate_js(js), flush=True)
                        except Exception as e:
                            print("AFTER_TAB_BACK_ERROR:", e, flush=True)

                    threading.Timer(1.5, check_pixels).start()

                threading.Timer(1.5, back_and_check).start()

            threading.Timer(11.0, tab_probe).start()

        if smoke:
            print("SMOKE_STARTED", flush=True)
            # demo 模式给截图/探针留时间
            seconds = 25 if demo else SMOKE_SECONDS
            threading.Timer(seconds, on_quit).start()

    atexit.register(app.cleanup)
    webview.start(after_start)
    app.cleanup()


if __name__ == "__main__":
    main()
