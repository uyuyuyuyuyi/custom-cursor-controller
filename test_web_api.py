"""test_web_api.py — gui_server 端到端测试（会短暂替换系统光标，随即恢复）"""
import base64
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

def make_left_arrow():
    """左箭头（不对称，用于验证水平翻转）。尖在左 x=6，尾 x=18..42。"""
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(6, 24), (18, 10), (18, 20), (42, 20), (42, 28), (18, 28), (18, 38)],
              fill=(255, 255, 255, 255))
    return img

def preview_pixel(previews, i, x, y):
    """读取第 i 帧预览图在逻辑坐标 (x, y) 处的 RGBA（预览为 4 倍放大）。"""
    b64 = previews[i].split(",", 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")
    return img.getpixel((x * 4 + 2, y * 4 + 2))

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

    # 5b. 旋转 90°（左箭头: 尖在左 x=6..18，尾 x=18..42）
    _, up2 = upload(url, [make_left_arrow()])
    check("上传左箭头", up2["frames"] == 1)

    _, hs2 = req("POST", url + "/api/hotspot",
                 json.dumps({"x": 10, "y": 20}).encode(),
                 {"Content-Type": "application/json"})
    check("手动设置热点 (10,20)", hs2["enabled"] is False)

    # 逆时针: (x,y)→(47-y,x); 尖(6,24)→(23,6)，附近 (23,8) 应为白色
    _, ccw = req("POST", url + "/api/rotate",
                 json.dumps({"direction": "ccw"}).encode(),
                 {"Content-Type": "application/json"})
    check("逆时针后热点 (27,10)", ccw["hotspot"] == [27, 10], str(ccw["hotspot"]))
    check("逆时针后上方是箭头", preview_pixel(ccw["previews"], 0, 23, 8)[3] > 200)
    check("逆时针后下方是空白", preview_pixel(ccw["previews"], 0, 23, 45)[3] < 50)

    # 顺时针转回: 热点应回到 (10,20)，方向复原
    _, cw = req("POST", url + "/api/rotate",
                json.dumps({"direction": "cw"}).encode(),
                {"Content-Type": "application/json"})
    check("顺时针转回后热点 (10,20)", cw["hotspot"] == [10, 20], str(cw["hotspot"]))
    check("转回后左侧是箭头", preview_pixel(cw["previews"], 0, 8, 24)[3] > 200)

    # 启用状态下顺时针旋转仍生效: (10,20)→(20,37); 尖(6,24)→(24,39)
    req("POST", url + "/api/apply")
    _, cw2 = req("POST", url + "/api/rotate",
                 json.dumps({"direction": "cw"}).encode(),
                 {"Content-Type": "application/json"})
    check("启用中顺时针后 enabled=True", cw2["enabled"] is True)
    check("顺时针后热点 (20,37)", cw2["hotspot"] == [20, 37], str(cw2["hotspot"]))
    check("顺时针后下方是箭头", preview_pixel(cw2["previews"], 0, 24, 39)[3] > 200)
    check("顺时针后上方是空白", preview_pixel(cw2["previews"], 0, 24, 2)[3] < 50)
    req("POST", url + "/api/restore")

    # 非法方向 → 400
    try:
        req("POST", url + "/api/rotate",
            json.dumps({"direction": "diagonal"}).encode(),
            {"Content-Type": "application/json"})
        check("非法方向被拒绝", False)
    except urllib.error.HTTPError as e:
        check("非法方向被拒绝 (400)", e.code == 400)

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
