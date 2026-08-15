"""
gui_server.py — 自定义光标控制器 Web 后端

为 Vue 前端提供本地 HTTP API，复用项目现有的全部核心逻辑:
  - pointer_analyzer: 图片适合度分析 + 自动抠背景
  - ani_builder:      生成 .ani 光标文件
  - cockroach_cursor:  Win32 光标替换 / 恢复（CursorManager）

API:
  GET  /api/state          当前状态 {enabled, frames, hotspot, verdict, ...}
  POST /api/upload         multipart(files[]) 上传图片 → 分析 → 暂存帧
  POST /api/apply          用已暂存帧生成 .ani 并替换系统光标
  POST /api/restore        恢复系统默认光标
  POST /api/hotspot        {x, y} 设置热点，启用中则立即重新生效
  POST /api/quit           恢复光标并退出程序（关闭窗口时调用）
  GET  /                   静态前端 (webui/dist)
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PIL import Image

from ani_builder import save_ani_file
from pointer_analyzer import HARD_REJECT_CODES, analyze, auto_remove_background
from cockroach_cursor import CursorManager

CANVAS_SIZE = 48
FRAME_MS = 150

# 静态前端目录: 源码运行时在 webui/dist，打包后在 _MEIPASS/webui
def _find_webui_dir() -> str | None:
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "webui"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "dist"))
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "index.html")):
            return c
    return None


class CursorApp:
    """后端业务逻辑（线程安全）。"""

    def __init__(self):
        self.mgr = CursorManager()
        self.mgr.backup_original()
        self.lock = threading.Lock()
        self.frames: list[Image.Image] = []
        self.hotspot = (CANVAS_SIZE // 2, CANVAS_SIZE // 2)
        self.enabled = False
        self.verdict: str | None = None
        self.score: int | None = None
        self.issues: list[dict] = []
        self.src_size: tuple[int, int] | None = None

    # ── 上传 → 分析 ────────────────────────────────────
    def upload(self, file_items: list[tuple[str, bytes]]) -> dict:
        processed: list[tuple[Image.Image, bool]] = []  # (处理后图, 抠背景是否成功)
        for _name, data in file_items:
            img = Image.open(io.BytesIO(data))
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            has_alpha = img.getchannel("A").getextrema()[0] < 250
            if has_alpha:
                processed.append((img, True))
            else:
                out, ok = auto_remove_background(img)
                processed.append((out, ok))

        # 逐张分析，取最差
        verdicts = []
        for img, bg_ok in processed:
            has_alpha = img.getchannel("A").getextrema()[0] < 250
            removal = None if has_alpha else bg_ok
            verdicts.append(analyze(img, removal_ok=removal))
        worst = min(verdicts, key=lambda a: a.score)

        # 统一缩放到 48×48
        frames = []
        for img, _bg in processed:
            canvas = self._fit_to_canvas(img, CANVAS_SIZE)
            frames.append(canvas)

        with self.lock:
            self.frames = frames
            self.hotspot = analyze(frames[0]).hotspot
            self.verdict = worst.verdict
            self.score = worst.score
            self.issues = [{"level": i.level, "message": i.message} for i in worst.issues]
            self.src_size = (processed[0][0].width, processed[0][0].height)

        # 预览: 每帧 4x 放大 PNG → base64
        previews = self.previews()

        hard_reject = any(
            it.level == "error" and it.code in HARD_REJECT_CODES for it in worst.issues
        )

        return {
            "frames": len(frames),
            "hotspot": list(self.hotspot),
            "verdict": worst.verdict,
            "score": worst.score,
            "issues": self.issues,
            "src_size": list(self.src_size),
            "previews": previews,
            "hard_reject": hard_reject,
        }

    def previews(self) -> list[str]:
        """当前暂存帧的 base64 预览（每帧 4x 放大 PNG）。"""
        with self.lock:
            out = []
            for f in self.frames:
                big = f.resize((CANVAS_SIZE * 4, CANVAS_SIZE * 4), Image.NEAREST)
                buf = io.BytesIO()
                big.save(buf, format="PNG")
                out.append("data:image/png;base64,"
                           + base64.b64encode(buf.getvalue()).decode())
            return out

    @staticmethod
    def _fit_to_canvas(img: Image.Image, size: int) -> Image.Image:
        scale = min(size / img.width, size / img.height, 1.0)
        new_w = max(1, round(img.width * scale))
        new_h = max(1, round(img.height * scale))
        scaled = img.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(scaled, ((size - new_w) // 2, (size - new_h) // 2), scaled)
        return canvas

    # ── 生成 / 应用 / 恢复 ─────────────────────────────
    def _rebuild_ani(self) -> str:
        """写入临时 .ani，返回路径。"""
        import tempfile
        os.makedirs(os.path.join(tempfile.gettempdir(), "cockroach_cursor_web"), exist_ok=True)
        ani_path = os.path.join(tempfile.gettempdir(), "cockroach_cursor_web", "custom.ani")
        save_ani_file(
            ani_path,
            frames=self.frames,
            hotspots=[self.hotspot] * len(self.frames),
            frame_durations_ms=[FRAME_MS] * len(self.frames),
            title="Custom Cursor",
            author="Custom Cursor Web",
        )
        return ani_path

    def apply(self) -> bool:
        with self.lock:
            if not self.frames:
                raise ValueError("还没有可用图片，请先上传")
            ani_path = self._rebuild_ani()
            self.mgr.replace_with_cockroach(ani_path)
            self.enabled = True
            return True

    def restore(self) -> bool:
        with self.lock:
            self.mgr.restore_original()
            self.enabled = False
            return False

    def set_hotspot(self, x: int, y: int) -> bool:
        with self.lock:
            self.hotspot = (max(0, min(CANVAS_SIZE - 1, x)), max(0, min(CANVAS_SIZE - 1, y)))
            if self.enabled and self.frames:
                self.mgr.replace_with_cockroach(self._rebuild_ani())
            return self.enabled

    def state(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "frames": len(self.frames),
                "hotspot": list(self.hotspot),
                "verdict": self.verdict,
                "score": self.score,
                "issues": self.issues,
                "src_size": list(self.src_size) if self.src_size else None,
            }

    def cleanup(self) -> None:
        """退出时恢复光标并释放资源。"""
        try:
            if self.enabled:
                self.mgr.restore_original()
        except Exception:
            try:
                self.mgr.reload_system_defaults()
            except Exception:
                pass
        self.mgr.cleanup()


# ── HTTP 服务 ─────────────────────────────────────────

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


def make_handler(app: CursorApp, webui_dir: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ── 工具 ──
        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, msg: str) -> None:
            self._send_json({"error": msg}, status)

        def log_message(self, fmt, *args):  # 静默访问日志
            pass

        # ── 路由 ──
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                self._send_json(app.state())
                return
            if path == "/api/previews":
                self._send_json({"previews": app.previews()})
                return
            if path.startswith("/api/"):
                self._send_error(404, f"未知接口: {path}")
                return
            self._serve_static(path)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/upload":
                    self._handle_upload()
                elif path == "/api/apply":
                    app.apply()
                    self._send_json({"enabled": True})
                elif path == "/api/restore":
                    app.restore()
                    self._send_json({"enabled": False})
                elif path == "/api/hotspot":
                    length = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(length) or b"{}")
                    enabled = app.set_hotspot(int(data.get("x", 24)), int(data.get("y", 24)))
                    self._send_json({"enabled": enabled})
                elif path == "/api/quit":
                    self._send_json({"bye": True})
                    threading.Thread(target=quit_callback, daemon=True).start()
                else:
                    self._send_error(404, f"未知接口: {path}")
            except ValueError as e:
                self._send_error(400, str(e))
            except Exception as e:
                self._send_error(500, str(e))

        def _handle_upload(self):
            import cgi
            ctype, pdict = cgi.parse_header(self.headers.get("Content-Type", ""))
            if ctype != "multipart/form-data" or "boundary" not in pdict:
                self._send_error(400, "需要 multipart/form-data")
                return
            boundary = pdict["boundary"].encode()
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)
            files: list[tuple[str, bytes]] = []
            for part in raw.split(b"--" + boundary):
                if not part or part in (b"--\r\n", b"--", b"\r\n"):
                    continue
                header_end = part.find(b"\r\n\r\n")
                if header_end < 0:
                    continue
                headers = part[:header_end].decode("latin-1", "replace")
                body = part[header_end + 4:]
                if body.endswith(b"\r\n"):
                    body = body[:-2]
                name = None
                for line in headers.split("\r\n"):
                    if line.lower().startswith("content-disposition"):
                        for seg in line.split(";"):
                            seg = seg.strip()
                            if seg.startswith("filename="):
                                name = seg[9:].strip('"')
                if name:
                    files.append((name, body))
            if not files:
                self._send_error(400, "没有收到文件")
                return
            result = app.upload(files)
            self._send_json(result)

        def _serve_static(self, path: str):
            if not webui_dir:
                self._send_error(500, "前端资源缺失: webui/dist 未找到")
                return
            if path in ("/", ""):
                path = "/index.html"
            rel = path.lstrip("/").replace("\\", "/")
            target = os.path.normpath(os.path.join(webui_dir, rel))
            if not target.startswith(os.path.normpath(webui_dir)):
                self._send_error(403, "禁止访问")
                return
            if not os.path.isfile(target):
                # SPA 回退
                target = os.path.join(webui_dir, "index.html")
            ext = os.path.splitext(target)[1].lower()
            with open(target, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


# 全局引用，供 /api/quit 触发关闭
quit_callback = lambda: None  # noqa: E731


def start_server(app: CursorApp, port: int = 0) -> ThreadingHTTPServer:
    """在后台线程启动 HTTP 服务。返回 server（可读取实际端口）。"""
    webui_dir = _find_webui_dir()
    handler = make_handler(app, webui_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


if __name__ == "__main__":
    # 独立运行（无 pywebview 窗口）: python gui_server.py --port 8765
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    app = CursorApp()
    server = start_server(app, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"服务已启动: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.cleanup()
