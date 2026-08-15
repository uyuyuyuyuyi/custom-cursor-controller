"""test_web_api.py — gui_server 端到端测试（会短暂替换系统光标，随即恢复）"""
import io
import json
import sys
import urllib.request

from PIL import Image, ImageDraw

import gui_server
from gui_server import CursorApp, start_server

PASS, FAIL = 0, 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name} {extra}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")

def req(method, url, body=None, headers=None):
    r = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))

def upload(url, images):
    """手工构造 multipart/form-data"""
    boundary = "----testboundary1234"
    parts = []
    for i, img in enumerate(images):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="f{i}.png"\r\n'
            f"Content-Type: image/png\r\n\r\n".encode() + buf.getvalue() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return req("POST", url + "/api/upload", body,
               {"Content-Type": f"multipart/form-data; boundary={boundary}"})

def make_arrow(color=(255, 255, 255, 255)):
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(6, 40), (6, 14), (18, 14), (24, 4), (30, 14), (42, 14), (42, 40)], fill=color)
    return img

def make_blank():
    return Image.new("RGBA", (48, 48), (0, 0, 0, 0))

def main():
    app = CursorApp()
    server = start_server(app)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"服务: {url}")

    # 1. 初始状态
    _, st = req("GET", url + "/api/state")
    check("初始 enabled=False", st["enabled"] is False, f"frames={st['frames']}")

    # 2. 上传两张箭头（动画）
    _, up = upload(url, [make_arrow(), make_arrow((255, 200, 60, 255))])
    check("上传成功 frames=2", up["frames"] == 2, f"verdict={up['verdict']} score={up['score']}")
    check("previews 有 2 张", len(up.get("previews", [])) == 2)
    check("hotspot 已给出", isinstance(up.get("hotspot"), list) and len(up["hotspot"]) == 2)

    # 2b. /api/previews 可恢复读取
    _, pv = req("GET", url + "/api/previews")
    check("previews 接口返回 2 张", len(pv.get("previews", [])) == 2)

    # 3. 启用（真实替换系统光标！）
    _, ap = req("POST", url + "/api/apply")
    check("apply 后 enabled=True", ap["enabled"] is True)

    # 4. 改热点
    _, hs = req("POST", url + "/api/hotspot",
                json.dumps({"x": 10, "y": 12}).encode(),
                {"Content-Type": "application/json"})
    check("hotspot 后仍启用", hs["enabled"] is True)

    # 5. 恢复
    _, rs = req("POST", url + "/api/restore")
    check("restore 后 enabled=False", rs["enabled"] is False)

    # 6. 空白图 → 硬拒绝
    _, bad = upload(url, [make_blank()])
    check("空白图判不适合", bad["verdict"] == "不适合", f"score={bad['score']}")
    check("空白图 hard_reject=True", bad.get("hard_reject") is True)

    # 7. 静态首页
    r = urllib.request.urlopen(url + "/", timeout=15)
    html = r.read().decode("utf-8")
    check("首页返回 HTML", r.status == 200 and "<div id=\"app\"" in html)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    app.cleanup()
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
