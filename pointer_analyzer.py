"""
pointer_analyzer.py — 图片"适不适合做光标"的分析 + 背景自动去除

鼠标指针很小（常见 32~48px），还要在深浅不同的桌面背景上都看得清。
不是所有图片都适合：纯 PIL 启发式分析，对明显不合适的图片给出
"不适合 / 有风险 / 适合" 三级结论，并列出具体原因。

检查维度:
  1. 空白 / 全透明            -> 硬性拒绝
  2. 无透明通道的背景         -> 自动抠背景（洪水填充去除与边框相近的纯色）
  3. 图案尺寸 / 长宽比        -> 过小或过于细长，缩到光标尺寸后难以辨认
  4. 填充率（镂空/细线条）    -> 轮廓太碎
  5. 连通区域数 + 边缘密度    -> 细节过多，48px 下糊成一团
  6. 明度对比                 -> 中间色调在浅/深背景上都看不清；
                                  纯亮色/纯暗色在同类背景上可能看不清
  7. 原图分辨率               -> 低于 32px 放大后锯齿严重

注意: 这是启发式判断——能可靠拦截"明显不合适"的图片，但无法保证
美观性（那是主观的）。用户保留最终决定权（可强制使用）。
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from dataclasses import dataclass, field

from PIL import Image

# 光标画布尺寸（与 cursor_drawer / make_custom_cursor 保持一致）
TARGET_SIZE = 48
# 自动抠背景时的工作尺寸
BG_WORK_SIZE = 160

# 硬性拒绝: 出现这些错误码直接判"不适合"，不给强制使用的余地
HARD_REJECT_CODES = {"EMPTY", "TOO_SMALL", "NO_BG"}

# ── 数据结构 ──────────────────────────────────────────

@dataclass
class Issue:
    level: str            # "error" | "warning"
    code: str             # 机器可读代号
    message: str          # 中文说明


@dataclass
class Analysis:
    score: int
    verdict: str          # "适合" | "有风险" | "不适合"
    issues: list[Issue] = field(default_factory=list)
    hotspot: tuple[int, int] = (TARGET_SIZE // 2, TARGET_SIZE // 2)
    stats: dict = field(default_factory=dict)

    def issues_text(self) -> str:
        if not self.issues:
            return "未发现问题。"
        lines = []
        for it in self.issues:
            tag = "[严重]" if it.level == "error" else "[提示]"
            lines.append(f"{tag} {it.message}")
        return "\n".join(lines)


def _pixels(img: Image.Image) -> list:
    """获取像素列表（兼容 Pillow 10 与新版 API）。"""
    if hasattr(img, "get_flattened_data"):
        return list(img.get_flattened_data())
    return list(img.getdata())


# ── 背景去除 ──────────────────────────────────────────

def auto_remove_background(img: Image.Image, tolerance: int = 42) -> tuple[Image.Image, bool]:
    """
    对没有透明通道的图片，自动去除纯色背景。

    方法: 取边框像素的中位色作为背景色，从四条边框向内做 BFS 洪水填充，
    颜色与背景色距离 <= tolerance 的连通区域置为透明。

    Args:
        img: RGBA 或 RGB 图像
        tolerance: 颜色容差（欧氏距离上限）

    Returns:
        (处理后的 RGBA 图像, 是否成功)
        失败时返回原图（转 RGBA），由调用方决定如何处理。
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # 已经自带有效透明通道 → 无需处理
    alpha = img.getchannel("A")
    if alpha.getextrema()[0] < 250:
        return img, True

    # 缩到工作尺寸提速
    small = img.convert("RGB").resize((BG_WORK_SIZE, BG_WORK_SIZE), Image.LANCZOS)
    w, h = small.size
    px = small.load()

    # 边框像素集合
    border = []
    for x in range(w):
        border.append(px[x, 0])
        border.append(px[x, h - 1])
    for y in range(h):
        border.append(px[0, y])
        border.append(px[w - 1, y])

    # 中位色 = 背景色估计
    bg = tuple(sorted(border, key=lambda c: sum(c))[len(border) // 2])

    def near(c1: tuple, c2: tuple, tol: int) -> bool:
        return (c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2 <= tol * tol

    # BFS 从边框开始
    visited = [[False] * w for _ in range(h)]
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        if near(px[x, 0], bg, tolerance):
            q.append((x, 0)); visited[0][x] = True
        if near(px[x, h - 1], bg, tolerance):
            q.append((x, h - 1)); visited[h - 1][x] = True
    for y in range(h):
        if near(px[0, y], bg, tolerance):
            q.append((0, y)); visited[y][0] = True
        if near(px[w - 1, y], bg, tolerance):
            q.append((w - 1, y)); visited[y][w - 1] = True

    while q:
        cx, cy = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < w and 0 <= ny < h and not visited[ny][nx]:
                if near(px[nx, ny], bg, tolerance):
                    visited[ny][nx] = True
                    q.append((nx, ny))

    # 统计被去除比例
    removed = sum(1 for row in visited for v in row if v)
    ratio = removed / (w * h)

    # 成功判据: 去除了一部分（>1.5%）但不是几乎全部（背景与图案颜色相同）
    if not (0.015 <= ratio <= 0.93):
        return img, False

    # 把掩码放大回原尺寸，应用到 alpha
    mask = Image.new("L", (w, h), 0)
    mask_px = mask.load()
    for y in range(h):
        for x in range(w):
            mask_px[x, y] = 255 if not visited[y][x] else 0
    mask = mask.resize(img.size, Image.LANCZOS)

    out = img.copy()
    out.putalpha(mask)
    return out, True


# ── 核心分析 ──────────────────────────────────────────

def _opaque_pixels(img: Image.Image) -> list[tuple[int, int, int]]:
    """返回不透明像素的 (r,g,b) 列表（alpha > 32 视为不透明）。"""
    return [(r, g, b) for (r, g, b, a) in _pixels(img) if a > 32]


def _luminance(c: tuple[int, int, int]) -> int:
    return round(0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])


def _edge_density(img: Image.Image) -> float:
    """48px 图上的边缘密度: 相邻不透明像素亮度差 > 35 的比例。"""
    data = _pixels(img)
    lum = [(_luminance((r, g, b)) if a > 32 else -1) for (r, g, b, a) in data]
    edges = 0
    total = img.width * img.height
    for y in range(img.height):
        for x in range(img.width):
            i = y * img.width + x
            if lum[i] < 0:
                continue
            for dx, dy in ((1, 0), (0, 1)):
                nx, ny = x + dx, y + dy
                if nx >= img.width or ny >= img.height:
                    continue
                j = ny * img.width + nx
                if lum[j] >= 0 and abs(lum[i] - lum[j]) > 35:
                    edges += 1
    return edges / total


def _component_count(img: Image.Image) -> int:
    """alpha 掩码上的 8-连通区域数量（细节碎片的直接度量）。"""
    alpha = img.getchannel("A").point(lambda a: 255 if a > 32 else 0)
    w, h = img.size
    mask = alpha.load()
    visited = [[False] * w for _ in range(h)]
    count = 0
    for y in range(h):
        for x in range(w):
            if mask[x, y] and not visited[y][x]:
                count += 1
                q = deque([(x, y)])
                visited[y][x] = True
                while q:
                    cx, cy = q.popleft()
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            if dx == 0 and dy == 0:
                                continue
                            nx, ny = cx + dx, cy + dy
                            if (0 <= nx < w and 0 <= ny < h
                                    and not visited[ny][nx] and mask[nx, ny]):
                                visited[ny][nx] = True
                                q.append((nx, ny))
    return count


def analyze(
    img: Image.Image,
    target_size: int = TARGET_SIZE,
    removal_ok: bool | None = None,
) -> Analysis:
    """
    分析一张 RGBA 图片是否适合作为鼠标指针。

    Args:
        img: RGBA 图像（未缩放也可以，内部会降采样）
        target_size: 光标目标尺寸（默认 48）
        removal_ok: 背景去除结果
            None = 未尝试（原图自带透明通道，或调用方不关心）
            True  = 已成功自动去除背景
            False = 无透明通道且自动去除失败

    Returns:
        Analysis 对象
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    issues: list[Issue] = []
    stats: dict = {}

    # 0) 基本信息
    src_w, src_h = img.size
    stats["source_size"] = (src_w, src_h)
    alpha_all = img.getchannel("A").getextrema()
    stats["alpha_range"] = alpha_all
    has_real_alpha = alpha_all[0] < 250

    # 1) 空白检查（降采样到目标尺寸，统计与热点均在此坐标空间）
    small = img.resize((target_size, target_size), Image.LANCZOS)
    opaque = _opaque_pixels(small)
    opaque_ratio = len(opaque) / (target_size * target_size)
    stats["opaque_ratio"] = round(opaque_ratio, 3)

    if opaque_ratio < 0.02:
        issues.append(Issue("error", "EMPTY",
                            "图片基本为空白/全透明，无法生成可见的光标。"))
    elif opaque_ratio < 0.05:
        issues.append(Issue("warning", "SPARSE",
                            "图案只占画面很小一部分，生成的光标会很小。"))

    # 2) 背景情况
    if removal_ok is False:
        issues.append(Issue("error", "NO_BG",
                            "图片没有透明通道，且无法自动去除背景——"
                            "光标将是一个不透明的方块。建议使用带透明背景"
                            "的 PNG，或纯色背景的图片。"))
    elif removal_ok is True:
        stats["background"] = "auto_removed"
    elif not has_real_alpha:
        issues.append(Issue("warning", "NO_ALPHA",
                            "图片没有透明通道（未自动抠图），光标会带背景。"))
    if opaque_ratio > 0.92 and removal_ok is not True:
        issues.append(Issue("warning", "OPAQUE_BG",
                            "图案几乎铺满整个画面（背景未去除），"
                            "光标将是一个不透明的方块。"))

    # 3) 图案边界框
    bbox = small.getchannel("A").point(lambda a: 255 if a > 32 else 0).getbbox()
    stats["bbox"] = bbox
    if bbox is not None:
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        stats["bbox_size"] = (bw, bh)
        if bw < 10 or bh < 10:
            issues.append(Issue("error", "TOO_SMALL",
                                "图案本身过小，缩成光标后几乎看不见。"))
        # 长宽比
        if max(bw, bh) / max(1, min(bw, bh)) > 6:
            issues.append(Issue("warning", "THIN",
                                "图案过于细长，缩小后可能难以辨认。"))
        # 填充率
        fill = len(opaque) / max(1, bw * bh)
        stats["fill_ratio"] = round(fill, 3)
        if fill < 0.08:
            issues.append(Issue("warning", "SPARSE_SHAPE",
                                "图案很纤细或镂空较多，缩小后轮廓可能断裂。"))
        # 建议热点 = bbox 中心
        hotspot = (bbox[0] + bw // 2, bbox[1] + bh // 2)
    else:
        hotspot = (target_size // 2, target_size // 2)

    # 4) 细节复杂度: 连通区域数 + 边缘密度
    components = _component_count(small)
    density = _edge_density(small)
    stats["components"] = components
    stats["edge_density"] = round(density, 3)
    if components > 24 or density > 0.45:
        issues.append(Issue("error", "TOO_COMPLEX",
                            "图案细节过多（碎片数 %d，边缘密度 %.0f%%），"
                            "缩到光标尺寸后会糊成一团、难以辨认。"
                            % (components, density * 100)))
    elif components > 10 or density > 0.28:
        issues.append(Issue("warning", "COMPLEX",
                            "图案细节偏多（碎片数 %d，边缘密度 %.0f%%），"
                            "缩小后可能看不清细节。"
                            % (components, density * 100)))

    # 5) 明度对比
    if opaque:
        lums = sorted(_luminance(c) for c in opaque)
        p5 = lums[max(0, int(len(lums) * 0.05))]
        p95 = lums[min(len(lums) - 1, int(len(lums) * 0.95))]
        contrast = p95 - p5
        stats["contrast"] = contrast
        dark = sum(1 for v in lums if v < 80) / len(lums)
        light = sum(1 for v in lums if v > 175) / len(lums)
        stats["dark_ratio"] = round(dark, 3)
        stats["light_ratio"] = round(light, 3)

        if contrast < 55:
            if dark < 0.12 and light < 0.12:
                # 中间色调: 深浅背景上都看不清
                issues.append(Issue("error", "MIDTONE",
                                    "图案整体是中间色调（既不明显偏亮也不偏暗），"
                                    "在浅色和深色背景上都可能看不清。"))
            elif light >= 0.6:
                issues.append(Issue("warning", "PALE",
                                    "图案整体为亮色，在浅色桌面上可能看不清；"
                                    "建议加一圈深色描边。"))
            elif dark >= 0.6:
                issues.append(Issue("warning", "DARK",
                                    "图案整体为暗色，在深色桌面上可能看不清；"
                                    "建议加一圈亮色描边。"))
            else:
                issues.append(Issue("warning", "LOW_CONTRAST",
                                    "图案内部对比偏低（%d/255）。"
                                    % contrast))
        elif contrast < 90:
            issues.append(Issue("warning", "LOW_CONTRAST",
                                "图案内部对比偏低（%d/255），细节可能不清晰。"
                                % contrast))
    else:
        stats["contrast"] = 0

    # 6) 原图分辨率
    if min(src_w, src_h) < 32 and opaque_ratio >= 0.02:
        issues.append(Issue("warning", "LOW_RES",
                            "原图分辨率仅 %d×%d，放大后会有明显锯齿。"
                            % (src_w, src_h)))

    # ── 评分与结论 ──
    score = 100
    for it in issues:
        score -= 30 if it.level == "error" else 12
    score = max(0, min(100, score))
    stats["score"] = score

    error_codes = [it.code for it in issues if it.level == "error"]
    if any(code in HARD_REJECT_CODES for code in error_codes):
        verdict = "不适合"
    elif score < 40 or len(error_codes) >= 2:
        verdict = "不适合"
    elif score < 70 or error_codes:
        verdict = "有风险"
    else:
        verdict = "适合"

    return Analysis(
        score=score,
        verdict=verdict,
        issues=issues,
        hotspot=hotspot,
        stats=stats,
    )


# ── 自测 ──────────────────────────────────────────────

def _make_test_images() -> dict[str, Image.Image]:
    """构造一组覆盖各情况的测试图。"""
    from PIL import ImageDraw
    import random
    out: dict[str, Image.Image] = {}

    # 好图: 白色箭头 + 透明背景
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(6, 40), (6, 14), (18, 14), (24, 4), (30, 14), (42, 14), (42, 40)],
              fill=(255, 255, 255, 255))
    out["good_arrow"] = img

    # 全透明
    out["empty"] = Image.new("RGBA", (48, 48), (0, 0, 0, 0))

    # 低对比度: 浅灰圆在透明背景上（颜色浅，中间调偏亮? 215 是亮色 → PALE）
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 40, 40), fill=(215, 215, 215, 255))
    out["gray_circle"] = img

    # 中间色调: 中灰圆
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 40, 40), fill=(125, 125, 125, 255))
    out["midtone_circle"] = img

    # 复杂细节: 密集条纹 → 碎片多
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(0, 48, 3):
        d.line([(i, 0), (i, 48)], fill=(255, 255, 255, 255), width=1)
    out["stripes"] = img

    # 无透明通道的照片类（噪声背景 + 深色块）→ 抠背景应失败
    rng = random.Random(42)
    img = Image.new("RGB", (64, 64))
    d = ImageDraw.Draw(img)
    for y in range(64):
        for x in range(64):
            d.point((x, y), fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
    d.rectangle((20, 20, 44, 44), fill=(30, 30, 30))
    out["noise_photo"] = img

    # 白底黑方块（无 alpha）→ 抠背景应成功
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((18, 18, 46, 46), fill=(10, 10, 10))
    out["white_bg_square"] = img

    # 细线十字
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.line([(24, 6), (24, 42)], fill=(255, 255, 255, 255), width=2)
    d.line([(6, 24), (42, 24)], fill=(255, 255, 255, 255), width=2)
    out["thin_cross"] = img

    return out


def selftest() -> None:
    """运行内置测试图并打印分析结论（用于验证阈值合理性）。"""
    images = _make_test_images()
    print(f"{'名称':<18}{'结论':<6}{'分数':>5}  关键统计")
    print("-" * 96)
    for name, img in images.items():
        a = analyze(img)
        s = a.stats
        detail = (f"opaque={s['opaque_ratio']}, edges={s['edge_density']}, "
                  f"comp={s['components']}, contrast={s['contrast']}")
        print(f"{name:<18}{a.verdict:<6}{a.score:>5}  {detail}")
        for it in a.issues:
            print(f"    - [{it.level}] {it.message}")

    # 验证背景去除
    print("\n背景去除测试:")
    for name in ("white_bg_square", "noise_photo"):
        img, ok = auto_remove_background(images[name])
        a = analyze(img, removal_ok=ok)
        print(f"  {name:<18} 去除成功={ok}, 处理后结论={a.verdict}, "
              f"opaque={a.stats['opaque_ratio']}")

    print("\n自测完成。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="指针图片适合度分析（自测）")
    parser.add_argument("images", nargs="*", help="可选: 分析指定图片")
    args = parser.parse_args()

    if args.images:
        for path in args.images:
            img = Image.open(path)
            a = analyze(img)
            print(f"{path}: {a.verdict} (score={a.score})")
            print(a.issues_text())
        sys.exit(0)

    selftest()
