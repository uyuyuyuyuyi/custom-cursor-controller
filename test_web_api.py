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

def upload(url, images, names=None):
    """手工构造 multipart/form-data；每项可为 PIL Image 或已编码的 bytes。

    names: 可选文件名列表（None 时用 f{i}.png）。
    非 ASCII 文件名按浏览器行为以 UTF-8 原始字节写入 filename="..."。
    """
    boundary = "----testboundary1234"
    parts = []
    for i, item in enumerate(images):
        if isinstance(item, bytes):
            buf = io.BytesIO(item)
        else:
            buf = io.BytesIO()
            item.save(buf, format="PNG")
        fname = names[i] if (names and i < len(names)) else f"f{i}.png"
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{fname}"\r\n'
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

def map_point(src_pt, src_size, cs, crop_box):
    """把源图坐标映射到"裁剪+适配"后的画布逻辑坐标（第二阶段 alpha bbox 裁剪）。"""
    if crop_box:
        l, t, r, b = crop_box
        scale = min(cs / (r - l), cs / (b - t))
        w = max(1, round((r - l) * scale))
        h = max(1, round((b - t) * scale))
        ox, oy = (cs - w) // 2, (cs - h) // 2
        return (round((src_pt[0] - l) * scale) + ox,
                round((src_pt[1] - t) * scale) + oy)
    scale = min(cs / src_size[0], cs / src_size[1], 1.0)
    w = max(1, round(src_size[0] * scale))
    h = max(1, round(src_size[1] * scale))
    ox, oy = (cs - w) // 2, (cs - h) // 2
    return (round(src_pt[0] * scale) + ox, round(src_pt[1] * scale) + oy)

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

def make_blue_id_photo():
    """蓝底证件照风格（回归: 彩色背景+照明渐变必须去除, 肤色/阴影皮肤必须保留）。

    结构: 240x240 RGB。蓝底径向渐变 (边缘深蓝灰 → 中心亮蓝); 肤色脸(高饱和)
    + 脸右侧阴影皮肤块(低饱和暖色) + 头顶深色头发。
    """
    img = Image.new("RGB", (240, 240))
    px = img.load()
    for y in range(240):
        for x in range(240):
            dx = (x - 120) / 120.0
            dy = (y - 120) / 90.0
            t = min(1.0, (dx * dx + dy * dy) ** 0.5)
            px[x, y] = (round(140 - 51 * t), round(206 - 77 * t), round(249 - 88 * t))
    d = ImageDraw.Draw(img)
    d.ellipse((86, 96, 158, 190), fill=(232, 190, 160, 255))   # 肤色脸
    d.ellipse((140, 120, 168, 178), fill=(180, 145, 140, 255)) # 脸右侧阴影皮肤
    d.rectangle((96, 84, 148, 98), fill=(50, 45, 55, 255))     # 头发
    return img

def make_shadow_plane():
    """白底 + 明显阴影 + 彩色主体（回归: 阴影背景必须整体去除）。

    结构: 160x160 RGB, 顶部白(255)到底部浅灰(120)的垂直渐变模拟大面积阴影;
    左上角白色高光块(硬边界); 主体为高饱和橙色椭圆(蟑螂体) + 深棕腿;
    主体下方一块中灰深阴影(连到渐变背景, 属于背景)。
    """
    img = Image.new("RGB", (160, 160))
    px = img.load()
    for y in range(160):
        g = 255 - int(135 * y / 159)
        for x in range(160):
            px[x, y] = (g, g, g)
    d = ImageDraw.Draw(img)
    d.ellipse((10, 0, 80, 45), fill=(255, 255, 255, 255))   # 白色高光(触顶边)
    d.ellipse((50, 40, 110, 104), fill=(195, 103, 18, 255)) # 橙色主体
    for i in range(24):                                     # 主体下方深阴影(渐隐)
        shade = 150 - i * 3
        d.ellipse((52 - i // 2, 104, 108 + i // 2, 104 + i * 2),
                  fill=(shade, shade, shade, 255))
    d.rectangle((54, 104, 62, 124), fill=(80, 40, 20, 255)) # 深棕腿(画在阴影之上)
    d.rectangle((98, 104, 106, 124), fill=(80, 40, 20, 255))
    return img

def main():
    store_root = (os.environ.get("CCC_TEST_ROOT")
                  or tempfile.mkdtemp(prefix="custom_cursor_test_"))
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
    check("干净透明图允许自动应用", up.get("auto_apply") is True
          and up.get("removal_confidence") == "high",
          f"confidence={up.get('removal_confidence')}")
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

    # 2f. 中文文件名: 浏览器以 UTF-8 原始字节发送 filename → 不得乱码
    _, up_cn = upload(url, [make_arrow((40, 120, 255, 255))], names=["我的图片.png"])
    check("中文文件名上传 cursor_name 正确", up_cn.get("cursor_name") == "我的图片",
          f"got={up_cn.get('cursor_name')!r}")
    _, gal_cn = req("GET", url + "/api/gallery")
    cn_upload = next((u for u in gal_cn["uploads"] if u["id"] in up_cn["upload_ids"]), None)
    check("图库图片名无乱码", cn_upload is not None
          and cn_upload["original"] == "我的图片.png",
          f"got={cn_upload and cn_upload.get('original')!r}")
    cn_cursor = next((c for c in gal_cn["cursors"] if c["id"] == up_cn["cursor_id"]), None)
    check("图库光标名无乱码", cn_cursor is not None and cn_cursor["name"] == "我的图片",
          f"got={cn_cursor and cn_cursor.get('name')!r}")

    # 2g. RFC 5987 filename*=UTF-8''%XX 形式（部分客户端/代理使用）
    import urllib.parse as _up
    star_name = _up.quote("小红点.png")
    boundary = "----testboundary5987"
    _png = io.BytesIO()
    make_arrow((255, 80, 80, 255)).save(_png, format="PNG")
    star_body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"files\"; "
        f"filename=\"fallback.png\"; filename*=UTF-8''{star_name}\r\n"
        f"Content-Type: image/png\r\n\r\n").encode() + _png.getvalue() + b"\r\n" \
        + f"--{boundary}--\r\n".encode()
    _, up_star = req("POST", url + "/api/upload", star_body,
                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    check("RFC5987 filename* 解码正确", up_star.get("cursor_name") == "小红点",
          f"got={up_star.get('cursor_name')!r}")

    # 2h. 历史乱码数据自动修复（latin-1 误解码的 UTF-8 名称）
    from gui_server import CursorStore, _decode_filename, _repair_mojibake
    check("_decode_filename UTF-8 解码", _decode_filename("图片.png".encode("utf-8"))
          == "图片.png")
    # GBK 回退: 选 UTF-8 严格解码必失败的字节（如"我的光标"的 GBK 编码）
    check("_decode_filename GBK 回退", _decode_filename("我的光标.png".encode("gbk"))
          == "我的光标.png")
    mjb = "图片.png".encode("utf-8").decode("latin-1")   # 旧 bug 产物: "å¾çç.png"
    check("_repair_mojibake 识别乱码", _repair_mojibake(mjb) == "图片.png",
          f"got={_repair_mojibake(mjb)!r}")
    check("_repair_mojibake 放过正常名", _repair_mojibake("正常.png") == "正常.png")
    fix_root = os.path.join(store_root, "repair_store")
    os.makedirs(fix_root, exist_ok=True)
    with open(os.path.join(fix_root, "index.json"), "w", encoding="utf-8") as f:
        json.dump({
            "uploads": {"u1": {"id": "u1", "original": mjb}},
            "cursors": {"c1": {"id": "c1", "name": mjb}},
        }, f, ensure_ascii=False)
    fix_store = CursorStore(root=fix_root)
    check("图库历史乱码自动修复(图片)",
          fix_store.index["uploads"]["u1"]["original"] == "图片.png",
          f"got={fix_store.index['uploads']['u1']['original']!r}")
    check("图库历史乱码自动修复(光标)",
          fix_store.index["cursors"]["c1"]["name"] == "图片.png",
          f"got={fix_store.index['cursors']['c1']['name']!r}")

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
    ani_path = os.path.join(tempfile.gettempdir(), "custom_cursor_web", "custom.ani")
    with open(ani_path, "rb") as f:
        ani = f.read()
    rate_idx = ani.find(b"rate")
    rate_size = _struct.unpack_from("<I", ani, rate_idx + 4)[0]
    jiffies = list(_struct.unpack_from("<" + "I" * (rate_size // 4), ani, rate_idx + 8))
    check("GIF 帧时长 200ms → jiffies=[12,12]", jiffies == [12, 12], str(jiffies))
    req("POST", url + "/api/restore")

    # 5d2. 图库应用 + 启用中重建仍保留 GIF 帧时长（回归: 不得丢成默认 150ms）
    cid_gif = gif_up["cursor_id"]
    _, ga_gif = req("POST", url + f"/api/gallery/cursors/{cid_gif}/apply")
    check("图库应用 GIF 光标", ga_gif["enabled"] is True)
    req("POST", url + "/api/hotspot",
        json.dumps({"x": 30, "y": 30}).encode(),
        {"Content-Type": "application/json"})      # 启用中 → 重建 .ani
    with open(ani_path, "rb") as f:
        ani2 = f.read()
    rate_idx2 = ani2.find(b"rate")
    rate_size2 = _struct.unpack_from("<I", ani2, rate_idx2 + 4)[0]
    jiffies2 = list(_struct.unpack_from("<" + "I" * (rate_size2 // 4),
                                        ani2, rate_idx2 + 8))
    check("图库应用后重建仍保留帧时长 jiffies=[12,12]",
          jiffies2 == [12, 12], str(jiffies2))
    req("POST", url + "/api/restore")

    # 5d3. 旧条目（无 durations 字段）: 从存储 .ani 的 rate 块回退恢复帧时长
    app.store.index["cursors"][cid_gif].pop("durations", None)
    app.store._save()
    _, ga_legacy = req("POST", url + f"/api/gallery/cursors/{cid_gif}/apply")
    check("旧条目图库应用", ga_legacy["enabled"] is True)
    req("POST", url + "/api/hotspot",
        json.dumps({"x": 20, "y": 20}).encode(),
        {"Content-Type": "application/json"})      # 启用中 → 重建 .ani
    with open(ani_path, "rb") as f:
        ani3 = f.read()
    rate_idx3 = ani3.find(b"rate")
    rate_size3 = _struct.unpack_from("<I", ani3, rate_idx3 + 4)[0]
    jiffies3 = list(_struct.unpack_from("<" + "I" * (rate_size3 // 4),
                                        ani3, rate_idx3 + 8))
    check("旧条目回退仍保留帧时长 jiffies=[12,12]",
          jiffies3 == [12, 12], str(jiffies3))
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
        check("真实 GIF 抠图高置信且可自动应用",
              rg.get("removal_confidence") == "high" and rg.get("auto_apply") is True,
              f"confidence={rg.get('removal_confidence')} auto={rg.get('auto_apply')}")
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

    # 6b. 白底+阴影照片 (回归: 白色带阴影的平面必须整体识别为背景)
    buf = io.BytesIO()
    make_shadow_plane().save(buf, format="PNG")
    _, sp = upload(url, [buf.getvalue()])
    check("阴影图非硬拒绝", sp.get("hard_reject") is False,
          f"verdict={sp.get('verdict')} score={sp.get('score')} issues={sp.get('issues')}")
    # 预览为 4 倍放大: 检查四角透明 / 主体中心不透明 / 主体下方深阴影处透明
    cs = sp["canvas_size"]                      # 此时画布可能不是 64 (前面测试切过尺寸)
    crop_box = sp.get("crop_box")               # 第二阶段: alpha bbox 自动裁剪
    body_pos = map_point((80, 72), (160, 160), cs, crop_box)      # 源坐标 (80,72)
    shadow_pos = map_point((80, 115), (160, 160), cs, crop_box)   # 源坐标 (80,115)
    pv_img = Image.open(io.BytesIO(base64.b64decode(
        sp["previews"][0].split(",", 1)[1]))).convert("RGBA")
    corners_ok = all(pv_img.getpixel(c)[3] <= 120 for c in
                     ((2, 2), (pv_img.width - 3, 2),
                      (2, pv_img.height - 3), (pv_img.width - 3, pv_img.height - 3)))
    check("阴影图四角透明（背景已去除）", corners_ok)
    body_a = pv_img.getpixel((body_pos[0] * 4 + 2, body_pos[1] * 4 + 2))[3]
    check("阴影图主体保留", body_a > 200, f"center_alpha={body_a}")
    shadow_a = pv_img.getpixel((shadow_pos[0] * 4 + 2, shadow_pos[1] * 4 + 2))[3]
    check("阴影图主体下方深阴影已去除", shadow_a <= 120, f"shadow_alpha={shadow_a}")

    # 6c. 蓝底证件照 (回归: 彩色背景+渐变必须去除, 肤色/阴影皮肤必须保留)
    buf = io.BytesIO()
    make_blue_id_photo().save(buf, format="PNG")
    _, bp = upload(url, [buf.getvalue()])
    check("证件照非硬拒绝", bp.get("hard_reject") is False,
          f"verdict={bp.get('verdict')} score={bp.get('score')}")
    cs = bp["canvas_size"]
    crop_box = bp.get("crop_box")
    bp_img = Image.open(io.BytesIO(base64.b64decode(
        bp["previews"][0].split(",", 1)[1]))).convert("RGBA")
    face_pos = map_point((122, 143), (240, 240), cs, crop_box)
    shade_pos = map_point((152, 148), (240, 240), cs, crop_box)
    # 蓝底检查点: 裁剪后原蓝底区域已被裁掉, 改取裁剪框内左侧边距处(必为背景)
    # (crop_box 是源图坐标, 需经 map_point 映射回画布坐标)
    if crop_box:
        src_pt = (crop_box[0] + 1, (crop_box[1] + crop_box[3]) // 2)
        blue_pos = map_point(src_pt, (240, 240), cs, crop_box)
    else:
        blue_pos = map_point((30, 120), (240, 240), cs, crop_box)
    face_a = bp_img.getpixel((face_pos[0] * 4 + 2, face_pos[1] * 4 + 2))[3]
    check("证件照脸部保留", face_a > 200, f"face_alpha={face_a}")
    shade_a = bp_img.getpixel((shade_pos[0] * 4 + 2, shade_pos[1] * 4 + 2))[3]
    check("证件照阴影皮肤保留", shade_a > 200, f"shade_alpha={shade_a}")
    blue_a = bp_img.getpixel((blue_pos[0] * 4 + 2, blue_pos[1] * 4 + 2))[3]
    check("证件照蓝底已去除", blue_a <= 120, f"blue_alpha={blue_a}")
    corner_a = bp_img.getpixel((2, 2))[3]
    check("证件照深色条带已去除", corner_a <= 120, f"corner_alpha={corner_a}")

    # 7. 关联删除 & 图库生成光标
    def upload_one(color):
        buf = io.BytesIO()
        make_arrow(color).save(buf, format="PNG")
        return buf.getvalue()

    def gallery():
        _, g = req("GET", url + "/api/gallery")
        return g

    def post_json(path, payload):
        return req("POST", path, json.dumps(payload).encode(),
                   {"Content-Type": "application/json"})

    # 7a. 删除图片 → 级联删除关联光标
    _, up1 = upload(url, [upload_one((60, 200, 255, 255))])
    u1, c1 = up1["upload_ids"][0], up1["cursor_id"]
    post_json(url + f"/api/gallery/uploads/{u1}/delete", {"with_cursors": True})
    g1 = gallery()
    check("删除图片级联删除关联光标", all(x["id"] != c1 for x in g1["cursors"])
          and all(x["id"] != u1 for x in g1["uploads"]))

    # 7b. 删除光标 → 级联删除独占图片
    _, up2 = upload(url, [upload_one((200, 60, 255, 255))])
    u2, c2 = up2["upload_ids"][0], up2["cursor_id"]
    post_json(url + f"/api/gallery/cursors/{c2}/delete", {"with_uploads": True})
    g2 = gallery()
    check("删除光标级联删除独占图片", all(x["id"] != c2 for x in g2["cursors"])
          and all(x["id"] != u2 for x in g2["uploads"]))

    # 7c. 仅删光标 → 图片保留（不级联）
    _, up3 = upload(url, [upload_one((255, 220, 90, 255))])
    u3, c3 = up3["upload_ids"][0], up3["cursor_id"]
    post_json(url + f"/api/gallery/cursors/{c3}/delete", {"with_uploads": False})
    g3 = gallery()
    check("仅删光标时图片保留", all(x["id"] != c3 for x in g3["cursors"])
          and any(x["id"] == u3 for x in g3["uploads"]))
    post_json(url + f"/api/gallery/uploads/{u3}/delete", {"with_cursors": False})

    # 7d. 图库生成光标: 无光标图片 → generate 生成并关联
    _, up4 = upload(url, [upload_one((120, 255, 180, 255))])
    u4, c4 = up4["upload_ids"][0], up4["cursor_id"]
    post_json(url + f"/api/gallery/cursors/{c4}/delete", {"with_uploads": False})
    _, gen = req("POST", url + f"/api/gallery/uploads/{u4}/generate", b"",
                 {"Content-Type": "application/json"})
    check("图库生成光标 existing=False", gen.get("existing") is False,
          str(gen.get("error", "")))
    g4 = gallery()
    u4_cursors = [x for x in g4["cursors"] if u4 in x.get("upload_ids", [])]
    check("生成的光标关联该图片", len(u4_cursors) == 1)

    # 7e. generate 内容查重: 已有相同光标 → existing=True 复用不新增
    n_before = len(g4["cursors"])
    _, gen2 = req("POST", url + f"/api/gallery/uploads/{u4}/generate", b"",
                  {"Content-Type": "application/json"})
    g5 = gallery()
    check("重复生成复用已有光标", gen2.get("existing") is True
          and len(g5["cursors"]) == n_before)

    # 7f. 共享图片保护: 多图光标中的图被另一光标引用时，删光标不删共享图
    _, up5 = upload(url, [upload_one((255, 120, 120, 255)),
                          upload_one((120, 120, 255, 255))])
    u5, u6 = up5["upload_ids"]
    c5 = up5["cursor_id"]
    req("POST", url + f"/api/gallery/uploads/{u5}/generate", b"",
        {"Content-Type": "application/json"})   # u5 再生成独立光标 → 共享
    post_json(url + f"/api/gallery/cursors/{c5}/delete", {"with_uploads": True})
    g6 = gallery()
    check("共享图片 u5 保留", any(x["id"] == u5 for x in g6["uploads"]))
    check("独占图片 u6 被级联删除", all(x["id"] != u6 for x in g6["uploads"]))
    check("多图光标已删除", all(x["id"] != c5 for x in g6["cursors"]))

    # 7g. 删除正在应用的光标 → 恢复系统光标并清空工作状态
    _, up7 = upload(url, [upload_one((90, 255, 90, 255))])
    u7, c7 = up7["upload_ids"][0], up7["cursor_id"]
    req("POST", url + "/api/apply")
    _, st7 = req("GET", url + "/api/state")
    check("删除前已启用", st7["enabled"] is True and st7["frames"] == 1)
    _, del7 = post_json(url + f"/api/gallery/cursors/{c7}/delete", {"with_uploads": True})
    _, st8 = req("GET", url + "/api/state")
    check("删除应用中的光标后恢复系统光标",
          del7.get("restored") is True and st8["enabled"] is False)
    check("删除应用中的光标后工作状态清空",
          st8["frames"] == 0 and st8["last_cursor_id"] is None)

    # 7h. 删除未应用但为当前工作来源的光标 → 清空工作状态（不恢复系统光标）
    _, up8 = upload(url, [upload_one((200, 90, 200, 255))])
    u8, c8 = up8["upload_ids"][0], up8["cursor_id"]
    _, del8 = post_json(url + f"/api/gallery/cursors/{c8}/delete", {"with_uploads": False})
    _, st9 = req("GET", url + "/api/state")
    check("删除未应用的工作光标仅清空状态",
          del8.get("restored") is False and st9["frames"] == 0
          and st9["enabled"] is False)
    post_json(url + f"/api/gallery/uploads/{u8}/delete", {"with_cursors": False})

    # 7i. 删除图片级联删除应用中的光标 → 同样恢复系统光标
    _, up9 = upload(url, [upload_one((90, 200, 200, 255))])
    u9, c9 = up9["upload_ids"][0], up9["cursor_id"]
    req("POST", url + "/api/apply")
    _, del9 = post_json(url + f"/api/gallery/uploads/{u9}/delete", {"with_cursors": True})
    _, st10 = req("GET", url + "/api/state")
    check("级联删除应用中的光标也恢复系统光标",
          del9.get("restored") is True and st10["enabled"] is False
          and st10["frames"] == 0)

    # 8. 静态首页
    r = urllib.request.urlopen(url + "/", timeout=15)
    html = r.read().decode("utf-8")
    check("首页返回 HTML", r.status == 200 and "<div id=\"app\"" in html)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    app.cleanup()
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
