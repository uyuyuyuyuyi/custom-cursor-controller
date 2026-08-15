"""test_web_api.py — gui_server 端到端测试（会短暂替换系统光标，随即恢复）"""
import base64
import io
import json
import os
import sys
import tempfile
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
    """手工构造 multipart/form-data；每项可为 PIL Image 或已编码的 bytes。"""
    boundary = "----testboundary1234"
    parts = []
    for i, item in enumerate(images):
        if isinstance(item, bytes):
            buf = io.BytesIO(item)
        else:
            buf = io.BytesIO()
            item.save(buf, format="PNG")
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

def make_gif(duration_ms=200):
    """两帧透明背景 GIF（白箭头 + 黄箭头）。"""
    frames = [make_arrow((255, 255, 255, 255)), make_arrow((255, 200, 60, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True,
                   append_images=frames[1:], duration=duration_ms, loop=0,
                   disposal=2, transparency=0)
    return buf.getvalue()

def main():
    store_root = tempfile.mkdtemp(prefix="cockroach_test_")
    app = CursorApp(store_root=store_root)
    server = start_server(app)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"服务: {url}")
    print(f"存储: {store_root}")

    # 1. 初始状态
    _, st = req("GET", url + "/api/state")
    check("初始 enabled=False", st["enabled"] is False, f"frames={st['frames']}")
    check("默认画布 64", st.get("canvas_size") == 64, f"canvas={st.get('canvas_size')}")

    # 2. 上传两张箭头（动画）
    _, up = upload(url, [make_arrow(), make_arrow((255, 200, 60, 255))])
    check("上传成功 frames=2", up["frames"] == 2, f"verdict={up['verdict']} score={up['score']}")
    check("previews 有 2 张", len(up.get("previews", [])) == 2)
    check("hotspot 已给出", isinstance(up.get("hotspot"), list) and len(up["hotspot"]) == 2)
    check("上传返回光标 id/名称", up.get("cursor_id") and up.get("cursor_name"),
          f"name={up.get('cursor_name')}")
    check("上传返回 upload_ids", len(up.get("upload_ids", [])) == 2)
    check("data_dir 存在", os.path.isdir(up.get("data_dir", "nonexistent")))

    # 2b. /api/previews 可恢复读取（紧跟上传，工作帧未变）
    _, pv = req("GET", url + "/api/previews")
    check("previews 接口返回 2 张", len(pv.get("previews", [])) == 2)

    # 2a. 图库: 条目已入库，文件已落盘
    _, gal = req("GET", url + "/api/gallery")
    check("图库有 2 个上传", len(gal["uploads"]) == 2, f"n={len(gal['uploads'])}")
    check("图库有 1 个光标", len(gal["cursors"]) == 1, f"n={len(gal['cursors'])}")
    cid = gal["cursors"][0]["id"]
    uid0 = gal["uploads"][0]["id"]
    up_entry = gal["uploads"][0]
    with urllib.request.urlopen(url + f"/api/gallery/uploads/{uid0}/thumb", timeout=15) as r:
        thumb = r.read()
    check("上传缩略图可访问 (PNG)", thumb[:8] == b"\x89PNG\r\n\x1a\n")
    with urllib.request.urlopen(url + f"/api/gallery/cursors/{cid}/preview", timeout=15) as r:
        pv_img = r.read()
    check("光标预览可访问 (PNG)", pv_img[:8] == b"\x89PNG\r\n\x1a\n")
    original_file = os.path.join(store_root, up_entry["filename"])
    check("上传原图文件已落盘", os.path.isfile(original_file))

    # 2b. 重命名 / 归类
    _, rn = req("POST", url + f"/api/gallery/cursors/{cid}/rename",
                json.dumps({"name": "我的箭头"}).encode(),
                {"Content-Type": "application/json"})
    check("重命名光标", rn.get("name") == "我的箭头", str(rn.get("name")))
    _, ct = req("POST", url + f"/api/gallery/cursors/{cid}/category",
                json.dumps({"category": "常用"}).encode(),
                {"Content-Type": "application/json"})
    check("光标归类", ct.get("category") == "常用", str(ct.get("category")))
    _, ct2 = req("POST", url + f"/api/gallery/uploads/{uid0}/category",
                 json.dumps({"category": "素材"}).encode(),
                 {"Content-Type": "application/json"})
    check("图片归类", ct2.get("category") == "素材")

    # 2c. 从图库应用光标（恢复工作状态 + 替换系统光标）
    _, ga = req("POST", url + f"/api/gallery/cursors/{cid}/apply")
    check("图库应用光标 enabled=True", ga["enabled"] is True)
    check("图库应用恢复画布尺寸", ga["canvas_size"] == 64, str(ga["canvas_size"]))
    check("图库应用恢复帧数", ga["frames"] == 2, str(ga["frames"]))
    req("POST", url + "/api/restore")

    # 2d. 删除
    _, dl = req("POST", url + f"/api/gallery/cursors/{cid}/delete")
    check("删除光标", dl.get("deleted") is True)
    _, gal2 = req("GET", url + "/api/gallery")
    check("删除后图库光标为 0", len(gal2["cursors"]) == 0)
    check("删除后上传仍保留", len(gal2["uploads"]) == 2)
    req("POST", url + f"/api/gallery/uploads/{uid0}/delete")
    _, gal3 = req("GET", url + "/api/gallery")
    check("删除后上传为 1", len(gal3["uploads"]) == 1)

    # 2e. 查重（SHA-256 内容哈希）
    arrow_bytes = io.BytesIO()
    make_arrow().save(arrow_bytes, format="PNG")
    payload = arrow_bytes.getvalue()

    _, up_d1 = upload(url, [payload])
    check("首次上传无重复提示", len(up_d1["duplicates"]["uploads"]) == 0
          and len(up_d1["duplicates"]["cursors"]) == 0)

    _, up_d2 = upload(url, [payload])
    check("重复上传命中已有图片", len(up_d2["duplicates"]["uploads"]) == 1)
    check("重复上传命中已有光标", len(up_d2["duplicates"]["cursors"]) == 1)

    _, up_d1b = upload(url, [payload, payload])
    check("同请求双份相同文件 → 图片重复命中", len(up_d1b["duplicates"]["uploads"]) == 1)
    check("同请求双份相同文件 frames=2", up_d1b["frames"] == 2)

    other = io.BytesIO()
    make_arrow((10, 200, 60, 255)).save(other, format="PNG")
    _, up_d3 = upload(url, [other.getvalue()])
    check("同名不同内容不误报", len(up_d3["duplicates"]["uploads"]) == 0
          and len(up_d3["duplicates"]["cursors"]) == 0)

    # 取消重复流程: 删除本次新生成的条目
    cid_new = up_d2["cursor_id"]
    req("POST", url + f"/api/gallery/cursors/{cid_new}/delete")
    for uid in up_d2["upload_ids"]:
        req("POST", url + f"/api/gallery/uploads/{uid}/delete")
    _, gal_clean = req("GET", url + "/api/gallery")
    check("取消后新条目已清理", all(c["id"] != cid_new for c in gal_clean["cursors"]))

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

    # 5a. 切换到 48 画布（旋转用例的像素坐标按 48 画布设计）
    _, sz48 = req("POST", url + "/api/size",
                  json.dumps({"size": 48}).encode(),
                  {"Content-Type": "application/json"})
    check("切到 48 画布", sz48.get("canvas_size") == 48)

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

    # 5c. 画布尺寸切换: 先重置热点 (10,20)，48 → 96 等比缩放为 (20,40)
    req("POST", url + "/api/hotspot",
        json.dumps({"x": 10, "y": 20}).encode(),
        {"Content-Type": "application/json"})
    _, sz96 = req("POST", url + "/api/size",
                  json.dumps({"size": 96}).encode(),
                  {"Content-Type": "application/json"})
    check("切到 96 画布", sz96.get("canvas_size") == 96)
    check("96 画布热点等比缩放 (20,40)", sz96["hotspot"] == [20, 40], str(sz96["hotspot"]))
    check("96 画布箭头仍在 (48,48)", preview_pixel(sz96["previews"], 0, 48, 48)[3] > 200)
    _, sz48b = req("POST", url + "/api/size",
                   json.dumps({"size": 48}).encode(),
                   {"Content-Type": "application/json"})
    check("切回 48 热点还原 (10,20)", sz48b["hotspot"] == [10, 20], str(sz48b["hotspot"]))
    try:
        req("POST", url + "/api/size",
            json.dumps({"size": 55}).encode(),
            {"Content-Type": "application/json"})
        check("非法尺寸被拒绝", False)
    except urllib.error.HTTPError as e:
        check("非法尺寸被拒绝 (400)", e.code == 400)

    # 5d. GIF 动画: 提取全部帧 + 使用 GIF 自带帧时长
    gif_bytes = make_gif()
    _, gif_up = upload(url, [gif_bytes])
    check("GIF 提取 2 帧", gif_up["frames"] == 2, f"frames={gif_up['frames']}")
    check("GIF 预览 2 张", len(gif_up["previews"]) == 2)
    req("POST", url + "/api/apply")
    import struct as _struct
    ani_path = os.path.join(tempfile.gettempdir(), "cockroach_cursor_web", "custom.ani")
    with open(ani_path, "rb") as f:
        ani = f.read()
    rate_idx = ani.find(b"rate")
    rate_size = _struct.unpack_from("<I", ani, rate_idx + 4)[0]
    jiffies = list(_struct.unpack_from("<" + "I" * (rate_size // 4), ani, rate_idx + 8))
    check("GIF 帧时长 200ms → jiffies=[12,12]", jiffies == [12, 12], str(jiffies))
    req("POST", url + "/api/restore")

    # 5e. 真实 GIF（白底动画）: 所有帧背景统一透明（修复白底闪现）
    gif_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "test_res", "cockroach-dancing.gif")
    if os.path.exists(gif_path):
        with open(gif_path, "rb") as f:
            real_gif = f.read()
        _, rg = upload(url, [real_gif])
        check("真实 GIF 提取多帧", rg["frames"] > 1,
              f"frames={rg['frames']} verdict={rg['verdict']} score={rg['score']}")
        corners_ok = True
        worst_corner = 255
        for pv in rg["previews"]:
            img = Image.open(io.BytesIO(base64.b64decode(pv.split(",", 1)[1]))).convert("RGBA")
            for (cx, cy) in ((2, 2), (img.width - 3, 2),
                             (2, img.height - 3), (img.width - 3, img.height - 3)):
                a = img.getpixel((cx, cy))[3]
                worst_corner = min(worst_corner, a)
                if a > 120:
                    corners_ok = False
        check("GIF 所有帧四角透明（无白底闪现）", corners_ok,
              f"worst_corner_alpha={worst_corner}")
    else:
        print("  (跳过真实 GIF 测试: test_res/cockroach-dancing.gif 不存在)")

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
