"""test_path_traversal.py — webui 静态文件路径穿越回归测试。

用原始 socket 发送攻击性请求，验证正常路径 200、合法 `..` 200、
任何逃逸 webui_dir 的路径 403。
"""
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui_server import CursorApp, start_server  # noqa: E402


def raw_request(port, path):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return data.split(b"\r\n", 1)[0].decode("latin-1")


def main():
    with tempfile.TemporaryDirectory(prefix="custom_cursor_pt_") as store_root:
        app = CursorApp(store_root=store_root)
        server = start_server(app, 0)
        try:
            port = server.server_address[1]
            cases = [
                ("/index.html", 200, "正常首页"),
                ("/assets/../index.html", 200, "合法 .. (未逃逸)"),
                ("/../gui_server.py", 403, "父目录穿越"),
                ("/..\\..\\gui_server.py", 403, "反斜杠穿越"),
                ("/..%2f..%2fgui_server.py", 403, "编码斜杠穿越"),
                ("/../webui/dist/index.html", 403, "跨目录直达"),
            ]
            failed = 0
            for path, expect, desc in cases:
                status = int(raw_request(port, path).split(" ")[1])
                ok = status == expect
                failed += 0 if ok else 1
                print(f"{'PASS' if ok else 'FAIL'}  {desc:<22} {path:<28} -> {status} (expect {expect})")
            assert failed == 0, f"{failed} case(s) failed"
            print("PATH_TRAVERSAL_OK")
        finally:
            server.shutdown()
            app.cleanup()


if __name__ == "__main__":
    main()
