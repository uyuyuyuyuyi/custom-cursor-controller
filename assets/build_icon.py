# -*- coding: utf-8 -*-
"""生成应用图标: 深色圆角底 + 蓝紫渐变光标箭头, 输出多尺寸 ICO 与 256px PNG 预览。

配色与 webui/src/style.css 主题一致:
  --bg: #0e1116 / --accent: #4f8cff / --accent-2: #7c5cff
"""
import os
from PIL import Image, ImageDraw, ImageFilter

BASE = 256          # 基准画布尺寸
SS = 8              # 超采样倍数(抗锯齿)

BG_TOP = (26, 34, 48, 255)      # #1a2230
BG_BOT = (13, 16, 22, 255)      # #0d1016
ACCENT_A = (79, 140, 255)       # #4f8cff
ACCENT_B = (124, 92, 255)       # #7c5cff
RIM = (216, 230, 255)           # 箭头描边高光色

# 经典鼠标箭头多边形(单位坐标)
ARROW = [(0, 0), (0, 20), (4, 16), (8, 23), (12, 20), (8, 13), (15, 13), (15, 10)]


def lerp(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def build(size=BASE):
    W = H = size * SS

    # ---- 背景: 圆角矩形 + 纵向渐变 ----
    radius = int(56 / 256 * W)
    bg_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(bg_mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=radius, fill=255)

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(0, H, 2):
        t = y / (H - 1)
        gd.line([(0, y), (W, y)], fill=lerp(BG_TOP, BG_BOT, t) + (255,), width=2)
    img = Image.composite(grad, Image.new("RGBA", (W, H), (0, 0, 0, 0)), bg_mask)

    # ---- 光标后方的柔光 ----
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_d = ImageDraw.Draw(glow)
    gw = gh = int(0.78 * W)
    glow_d.ellipse([(W - gw) / 2, (H - gh) / 2, (W + gw) / 2, (H + gh) / 2],
                   fill=ACCENT_A + (36,))
    glow = glow.filter(ImageFilter.GaussianBlur(int(0.10 * W)))
    img = Image.alpha_composite(img, glow)

    # ---- 箭头几何 ----
    xs = [p[0] for p in ARROW]
    ys = [p[1] for p in ARROW]
    bw, bh = max(xs) - min(xs), max(ys) - min(ys)          # 15 x 23
    scale = (150 * SS) / bh                                  # 箭头高约 150/256
    aw, ah = bw * scale, bh * scale
    x0, y0 = (W - aw) / 2, (H - ah) / 2
    pts = [(x0 + x * scale, y0 + y * scale) for (x, y) in ARROW]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # 描边层(略微放大一点, 做出高光边)
    rim_pts = [(cx + (x - cx) * 1.022, cy + (y - cy) * 1.022) for (x, y) in pts]
    rim_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(rim_mask).polygon(rim_pts, fill=255)
    rim_layer = Image.new("RGBA", (W, H), RIM + (235,))
    overlay = Image.composite(rim_layer, overlay, rim_mask)

    # 箭头主体: 沿左上->右下方向 蓝->紫 渐变
    arrow_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(arrow_mask).polygon(pts, fill=255)
    agrad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ag = ImageDraw.Draw(agrad)
    total = W + H
    for k in range(0, total, 4):                             # 沿 x+y=const 的对角渐变
        t = k / total
        x1, y1 = max(0, k - H), min(H - 1, k)
        x2, y2 = min(W - 1, k), max(0, k - W)
        ag.line([(x1, y1), (x2, y2)], fill=lerp(ACCENT_A, ACCENT_B, t) + (255,), width=4)
    overlay = Image.composite(agrad, overlay, arrow_mask)

    # 头部高光小圆斑
    gloss = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gx, gy = x0 + 7.5 * scale, y0 + 8.5 * scale
    gr = 2.4 * scale
    ImageDraw.Draw(gloss).ellipse([gx - gr, gy - gr * 0.85, gx + gr, gy + gr * 0.85],
                                  fill=(255, 255, 255, 90))
    gloss = gloss.filter(ImageFilter.GaussianBlur(0.9 * SS))
    overlay = Image.alpha_composite(overlay, gloss)

    # ---- 圆角边框 ----
    border = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle([0, 0, W - 1, H - 1], radius=radius,
                                             outline=(255, 255, 255, 30), width=max(1, int(1.8 * SS)))
    overlay = Image.alpha_composite(overlay, border)

    img = Image.alpha_composite(img, overlay)
    return img.resize((size, size), Image.LANCZOS)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    icon = build(256)
    icon.save(os.path.join(here, "app.ico"), format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.save(os.path.join(here, "app_icon_preview.png"))
    print("OK ->", os.path.join(here, "app.ico"))


if __name__ == "__main__":
    main()
