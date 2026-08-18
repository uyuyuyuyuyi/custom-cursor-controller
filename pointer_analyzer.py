"""
pointer_analyzer.py — 图片"适不适合做光标"的分析 + 背景自动去除

鼠标指针很小（常见 32~48px），还要在深浅不同的桌面背景上都看得清。
不是所有图片都适合：纯 PIL 启发式分析，对明显不合适的图片给出
"不适合 / 有风险 / 适合" 三级结论，并列出具体原因。

检查维度:
  1. 空白 / 全透明            -> 硬性拒绝
  2. 无透明通道的背景         -> 自动抠背景（洪水填充去除纯色/渐变背景，先做边框背景色聚类）；
                                  支持白色平面 + 阴影的渐变背景：梯度规则沿
                                  平滑渐变穿越阴影，饱和度门槛保护彩色主体，
                                  孤岛填充补掉被主体包围的背景口袋）
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

from PIL import Image, ImageChops

# 光标画布尺寸（与 cursor_drawer / make_custom_cursor 保持一致）
TARGET_SIZE = 48
# 自动抠背景时的默认工作尺寸（兼容旧调用；实际工作尺寸 = 目标光标尺寸 × WORK_SCALE）
BG_WORK_SIZE = 160

# ── 第二阶段: 工作尺寸 / 多背景聚类 / 动态容差 / 贴边播种门控 ──
WORK_SCALE = 4.0          # 工作图边长 ≈ 目标光标尺寸 × 4（48→192, 64→256, 96→384）
WORK_MAX_SIDE = 512       # 工作图边长上限（更大原图按此等比缩小）
MAX_BG_CLUSTERS = 3       # 边框背景色聚类上限
CLUSTER_MIN_RATIO = 0.12  # 簇权重低于该比例则丢弃（视为贴边主体色/噪声）
CLUSTER_MERGE_DIST = 60.0  # 簇中心距离小于该值则合并为同一背景
TOL_K = 3.0               # 动态容差: tolerance = 边框距离中位数 + k × MAD
TOL_MIN = 24.0            # 动态容差下限（白底/JPEG 微噪声仍能整体去除）
TOL_MAX = 88.0            # 动态容差上限（防止复杂照片被无界放宽）
SEG_LEN_MIN = 8           # 边缘播种分段的最小长度

# ── 抠背景 (auto_remove_background) 参数 ──────────────
# 主洪水: 经典规则之外, 允许沿"平滑渐变"穿越阴影/光照/背景色渐变:
#   目的像素距当前像素 <= GRAD_STEP (逐像素变化小=渐变, 大=主体边缘), 且
#   距全局背景色 <= GRAD_CAP (漂移上限: 防止漫游进主体/无关颜色)。
#   注意: 不设饱和度门槛 —— 彩色背景(如证件照蓝底)的亮度渐变同样要能穿越;
#   彩色主体(棕色蟑螂/肤色)靠 GRAD_CAP + 主体轮廓的锐利跳变双重保护。
GRAD_STEP = 52.0        # 逐像素 RGB 欧氏距离
GRAD_CAP = 190.0        # 相对全局背景色的累计漂移上限
HUE_DELTA = 72          # 色相一致性: 目的像素 R-B 不得超过 (背景色R-B + HUE_DELTA)。
                        # 背景的照明/阴影渐变保持自身色相方向 (蓝底渐变只会更蓝/中性),
                        # 而主体的暖色 (皮肤/蟑螂 R-B 30~180) 会被挡在门外。
LUM_FLOOR = 70          # 亮度下限: 梯度规则拒绝亮度 < 70 的像素。深色 UI 条带与
                        # 黑色头发/领带/西服颜色完全相同 (中性深灰), 无法用颜色区分;
                        # 但真正需要抠掉的背景 (蓝底 L~130-190, 白色阴影平面 L~80-226)
                        # 都在 L>=70, 而主体的深色部分 (L~17-70) 被此下限保护。
SEED_SAT_LIMIT = 40     # 边框种子: 仅"低饱和且距背景色<=GRAD_CAP"才直接播种
                        # (随机噪声边框 41% 像素都在 CAP 内, 不加门控会大量播种;
                        #  彩色背景无需宽松种子——边框背景色中位像素 d=0 必被经典规则播种)
# 孤岛填充: 只填"被主体包围"的低饱和背景口袋 (与主体彩色连通块相邻)
ISLAND_SAT = 40         # 口袋像素饱和度上限
ISLAND_CAP = 120.0      # 口袋像素距背景色上限 (低: 保护浅色皮肤高光等低饱和主体部件)
ISLAND_WARM = 24        # 口袋像素 R-B 上限: 阴影口袋是中性/冷色, 暖色=阴影中的皮肤, 拒绝
SUBJECT_MIN = 100       # 主体彩色连通块最小像素数
COHERENCE = 60.0        # 主体内部相邻像素平均距离上限 (噪声图无平滑彩色块)
ISLAND_MAX = 600        # 单个口袋最大像素数 (避免误吃浅灰主体部件)

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


@dataclass
class RemovalDiagnostics:
    """背景去除质量诊断。

    ``success`` 只表示产生了可用 alpha；``confidence`` 才表示是否足以自动应用。
    复杂照片即使删除比例合理，也可能因主体碎裂/孔洞而处于 low confidence。
    """

    success: bool
    method: str = "heuristic"       # existing_alpha | heuristic | none
    selected_stage: str = "base"    # existing | base | island | aggressive | original
    confidence: str = "low"         # high | medium | low
    auto_apply: bool = False
    removed_ratio: float = 0.0
    postfill_removed_ratio: float = 0.0
    fallback_used: bool = False
    mask_stats: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "method": self.method,
            "selected_stage": self.selected_stage,
            "confidence": self.confidence,
            "auto_apply": self.auto_apply,
            "removed_ratio": round(self.removed_ratio, 4),
            "postfill_removed_ratio": round(self.postfill_removed_ratio, 4),
            "fallback_used": self.fallback_used,
            "mask_stats": self.mask_stats,
            "issues": [
                {"level": issue.level, "code": issue.code, "message": issue.message}
                for issue in self.issues
            ],
        }


def _fit_alpha_for_quality(alpha: Image.Image, target_size: int) -> Image.Image:
    """把 alpha 按真实光标的 contain 规则放进方形画布，供小尺寸质量检查。"""
    target_size = max(16, min(256, int(target_size)))
    scale = min(target_size / alpha.width, target_size / alpha.height, 1.0)
    new_w = max(1, round(alpha.width * scale))
    new_h = max(1, round(alpha.height * scale))
    resized = alpha.resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas = Image.new("L", (target_size, target_size), 0)
    canvas.paste(resized, ((target_size - new_w) // 2, (target_size - new_h) // 2))
    return canvas


def _mask_quality(alpha: Image.Image) -> dict:
    """在最终光标尺度度量主体碎裂、孔洞、软边与边框残留。"""
    if alpha.mode != "L":
        alpha = alpha.convert("L")
    w, h = alpha.size
    raw = _pixels(alpha)
    foreground = bytearray(1 if value > 32 else 0 for value in raw)
    total_pixels = w * h
    foreground_pixels = sum(foreground)

    # 8 连通前景区域。小于最终画布约 0.05% 的区域只视为亚像素噪点。
    seen = bytearray(total_pixels)
    components: list[int] = []
    for start in range(total_pixels):
        if not foreground[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        count = 0
        while stack:
            idx = stack.pop()
            count += 1
            x, y = idx % w, idx // w
            for dy in (-1, 0, 1):
                ny = y + dy
                if not 0 <= ny < h:
                    continue
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx = x + dx
                    if not 0 <= nx < w:
                        continue
                    nxt = ny * w + nx
                    if foreground[nxt] and not seen[nxt]:
                        seen[nxt] = 1
                        stack.append(nxt)
        components.append(count)
    components.sort(reverse=True)
    largest = components[0] if components else 0
    significant_min = max(2, round(total_pixels * 0.0005))
    significant = [size for size in components if size >= significant_min]

    # 4 连通透明区域从画布边缘向内泛洪；未触达边缘的透明区域是主体内部孔洞。
    exterior = bytearray(total_pixels)
    stack: list[int] = []
    for x in range(w):
        for y in (0, h - 1):
            idx = y * w + x
            if not foreground[idx] and not exterior[idx]:
                exterior[idx] = 1
                stack.append(idx)
    for y in range(1, h - 1):
        for x in (0, w - 1):
            idx = y * w + x
            if not foreground[idx] and not exterior[idx]:
                exterior[idx] = 1
                stack.append(idx)
    while stack:
        idx = stack.pop()
        x, y = idx % w, idx // w
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            nxt = ny * w + nx
            if not foreground[nxt] and not exterior[nxt]:
                exterior[nxt] = 1
                stack.append(nxt)
    hole_pixels = sum(
        1 for idx in range(total_pixels)
        if not foreground[idx] and not exterior[idx]
    )

    binary = alpha.point(lambda value: 255 if value > 32 else 0)
    bbox = binary.getbbox()
    bbox_area = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) if bbox else 0
    border_indices = (
        list(range(w)) + list(range((h - 1) * w, h * w))
        + [y * w for y in range(1, h - 1)]
        + [y * w + (w - 1) for y in range(1, h - 1)]
    )
    border_foreground = sum(foreground[idx] for idx in border_indices)
    soft_alpha = sum(1 for value in raw if 0 < value < 224)

    return {
        "foreground_ratio": round(foreground_pixels / max(1, total_pixels), 4),
        "largest_component_ratio": round(largest / max(1, foreground_pixels), 4),
        "significant_components": len(significant),
        "fragment_ratio": round((foreground_pixels - largest) / max(1, foreground_pixels), 4),
        "hole_ratio": round(hole_pixels / max(1, bbox_area), 4),
        "soft_alpha_ratio": round(soft_alpha / max(1, total_pixels), 4),
        "border_foreground_ratio": round(border_foreground / max(1, len(border_indices)), 4),
        "bbox": list(bbox) if bbox else None,
    }


def _removed_between(base_alpha: Image.Image, candidate_alpha: Image.Image) -> float:
    """候选后处理相对保守掩码额外删除的前景比例。"""
    base = [value > 32 for value in _pixels(base_alpha)]
    candidate = [value > 32 for value in _pixels(candidate_alpha)]
    base_count = sum(base)
    removed = sum(1 for old, new in zip(base, candidate) if old and not new)
    return removed / max(1, base_count)


def _diagnose_mask(
    stats: dict,
    *,
    fallback_used: bool = False,
) -> tuple[str, list[Issue]]:
    """把掩码统计映射为 high/medium/low；阈值由真实 test_res 样本校准。"""
    issues: list[Issue] = []
    fg = stats.get("foreground_ratio", 0.0)
    main = stats.get("largest_component_ratio", 0.0)
    components = stats.get("significant_components", 0)
    holes = stats.get("hole_ratio", 0.0)
    border = stats.get("border_foreground_ratio", 0.0)
    fragments = stats.get("fragment_ratio", 0.0)
    pristine_after_fallback = (
        main >= 0.99 and components <= 2 and fragments <= 0.01
        and holes <= 0.02 and border <= 0.01
    )

    low = False
    medium = fallback_used and not pristine_after_fallback
    if fg < 0.02:
        low = True
        issues.append(Issue("error", "MASK_EMPTY", "背景去除后几乎没有可见主体。"))
    elif fg < 0.06:
        medium = True
        issues.append(Issue("warning", "MASK_SPARSE", "背景去除后保留的主体面积很小。"))
    if components >= 10 or (components >= 4 and main < 0.80):
        low = True
        issues.append(Issue(
            "error", "MASK_SHATTERED",
            f"抠图结果被切成较多碎片（有效碎片 {components} 个，主块占 {main:.0%}）。",
        ))
    elif components >= 6 or (components >= 3 and main < 0.92):
        medium = True
        issues.append(Issue(
            "warning", "MASK_FRAGMENTED",
            f"抠图边缘存在较多碎片（有效碎片 {components} 个）。",
        ))
    if holes > 0.08:
        low = True
        issues.append(Issue("error", "MASK_HOLES", "主体内部出现大面积透明孔洞。"))
    elif holes > 0.03:
        medium = True
        issues.append(Issue("warning", "MASK_HOLES", "主体内部存在可见透明孔洞。"))
    if border > 0.30:
        low = True
        issues.append(Issue("error", "MASK_EDGE_LEAK", "大量未去除区域连接到光标画布边缘。"))
    elif border > 0.10:
        medium = True
        issues.append(Issue("warning", "MASK_EDGE_LEAK", "部分背景可能仍连接到画布边缘。"))
    if fallback_used and not pristine_after_fallback:
        issues.append(Issue(
            "warning", "POSTFILL_ROLLBACK",
            "激进补抠会破坏主体，已自动回退到更保守的结果。",
        ))

    return ("low" if low else "medium" if medium else "high"), issues


def diagnose_alpha_mask(
    mask: Image.Image,
    quality_size: int = TARGET_SIZE,
    method: str = "onnx",
    selected_stage: str = "onnx",
) -> RemovalDiagnostics:
    """为独立生成的掩码（如 ONNX 智能抠图）做光标尺度质量诊断。

    AI 掩码与启发式候选走同一套 QA: 先映射到最终光标尺寸,
    再检查碎片/孔洞/边缘泄漏/前景占比, 低置信度禁止静默自动应用。
    """
    quality_alpha = _fit_alpha_for_quality(mask.convert("L"), quality_size)
    stats = _mask_quality(quality_alpha)
    confidence, issues = _diagnose_mask(stats)
    return RemovalDiagnostics(
        success=True,
        method=method,
        selected_stage=selected_stage,
        confidence=confidence,
        auto_apply=confidence == "high",
        removed_ratio=1.0 - stats.get("foreground_ratio", 0.0),
        mask_stats=stats,
        issues=issues,
    )


def _pixels(img: Image.Image) -> list:
    """获取像素列表（兼容 Pillow 10 与新版 API）。"""
    if hasattr(img, "get_flattened_data"):
        return list(img.get_flattened_data())
    return list(img.getdata())


# ── 背景去除 ──────────────────────────────────────────

# 深色均匀条带 (截图 UI 条) 检测参数: 紧贴边框的均匀深色行/列视为"图像外的
# 装饰", 预标记为背景且不参与洪水播种 —— 否则洪水会从条带平滑漫入颜色相同的
# 主体深色部分 (黑色头发/领带/西服)
DARK_STRIP_LUM = 75     # 条带行/列中位亮度上限 (只检测深色条带)
EDGE_UNIFORM = 25.0     # 条带行/列的颜色跨度上限 (照片内容行跨度大, 会中断条带)
EDGE_STEP = 60.0        # 相邻条带行/列的中位色距离上限
EDGE_MAX_RATIO = 0.30   # 条带最大深度 (帧尺寸比例)


def _edge_strip_mask(px, w: int, h: int) -> list[list[bool]]:
    """检测紧贴四条边的深色均匀条带 (截图 UI 条), 返回掩码。

    从每条边向内逐行/列检查: 该行/列必须 (a) 中位亮度 < DARK_STRIP_LUM,
    (b) 颜色跨度 <= EDGE_UNIFORM (整行近似一色 —— 照片内容行会被主体/背景
    混合撑大跨度而中断), (c) 与上一条带行/列中位色渐进 (<= EDGE_STEP)。
    深度不超过帧尺寸的 EDGE_MAX_RATIO。
    """
    mask = [[False] * w for _ in range(h)]

    def _lum(c) -> float:
        return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]

    def _spread(cells) -> float:
        cells = sorted(cells, key=lambda c: sum(c))
        lo, hi = cells[1], cells[-2]
        return ((lo[0] - hi[0]) ** 2 + (lo[1] - hi[1]) ** 2
                + (lo[2] - hi[2]) ** 2) ** 0.5

    def _near(c1, c2, tol: float) -> bool:
        return (c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2 <= tol * tol

    def _walk(points_at_depth, cells_at_depth, max_depth):
        prev = None
        for d in range(max_depth):
            cells = points_at_depth(d)
            if _lum(cells[len(cells) // 2]) >= DARK_STRIP_LUM:
                break
            if _spread(cells) > EDGE_UNIFORM:
                break
            med = sorted(cells, key=lambda c: sum(c))[len(cells) // 2]
            if prev is not None and not _near(med, prev, EDGE_STEP):
                break
            prev = med
            for x, y in cells_at_depth(d):
                mask[y][x] = True

    max_h = int(h * EDGE_MAX_RATIO)
    max_w = int(w * EDGE_MAX_RATIO)

    def row_cells(y):
        return [(x, y) for x in range(w)]

    def row_points(y):
        return [px[x, y] for x in range(0, w, max(1, w // 11))]

    def col_cells(x):
        return [(x, y) for y in range(h)]

    def col_points(x):
        return [px[x, y] for y in range(0, h, max(1, h // 11))]

    _walk(row_points, row_cells, max_h)                     # 上边
    _walk(lambda d: row_points(h - 1 - d),
          lambda d: row_cells(h - 1 - d), max_h)            # 下边
    _walk(col_points, col_cells, max_w)                     # 左边
    _walk(lambda d: col_points(w - 1 - d),
          lambda d: col_cells(w - 1 - d), max_w)            # 右边
    return mask


def _work_dims(w0: int, h0: int, work_side: int) -> tuple[int, int]:
    """工作图尺寸: 保持原图长宽比, 只缩小不放大。"""
    scale = min(1.0, work_side / max(w0, h0))
    return max(1, round(w0 * scale)), max(1, round(h0 * scale))


def _d2(c1: tuple, c2: tuple) -> int:
    """RGB 欧氏距离平方。"""
    return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2
            + (c1[2] - c2[2]) ** 2)


def _median_color(cells) -> tuple:
    """按分量和排序取中位色。"""
    ordered = sorted(cells, key=lambda c: c[0] + c[1] + c[2])
    return ordered[len(ordered) // 2]


def _border_cells(px, w: int, h: int, strip: list[list[bool]]) -> list:
    """收集边框非条带单元格（由外向内最多约 3 圈 / 400 个）。"""
    cells: list = []
    for inner in range(max(1, min(w, h) // 2)):
        ring = []
        for x in range(w):
            if not strip[inner][x]:
                ring.append(px[x, inner])
            if not strip[h - 1 - inner][x]:
                ring.append(px[x, h - 1 - inner])
        for y in range(h):
            if not strip[y][inner]:
                ring.append(px[inner, y])
            if not strip[y][w - 1 - inner]:
                ring.append(px[w - 1 - inner, y])
        cells.extend(ring)
        if len(cells) >= 400 or (ring and inner >= 2):
            break
    return cells


def _kmeans_clusters(cells: list, k: int, iters: int = 10):
    """确定性 k-means（按颜色排序取等距初值），返回 (中心列表, 各簇大小)。"""
    ordered = sorted(cells, key=lambda c: (c[0], c[1], c[2]))
    n = len(ordered)
    centers = [ordered[min(n - 1, int((i + 0.5) * n / k))] for i in range(k)]
    for _ in range(iters):
        sums = [[0, 0, 0] for _ in centers]
        counts = [0] * k
        for c in cells:
            best = 0
            best_d = _d2(c, centers[0])
            for i in range(1, k):
                d = _d2(c, centers[i])
                if d < best_d:
                    best_d, best = d, i
            sums[best][0] += c[0]
            sums[best][1] += c[1]
            sums[best][2] += c[2]
            counts[best] += 1
        updated = [
            (s[0] // cnt, s[1] // cnt, s[2] // cnt) if cnt else centers[i]
            for i, (s, cnt) in enumerate(zip(sums, counts))
        ]
        if updated == centers:
            break
        centers = updated
    return centers, counts


def _estimate_background_clusters(img: Image.Image, work_size: int) -> list[tuple]:
    """把边框像素聚类为 1~3 个背景色（多背景色聚类）。

    双色/多色背景（白墙+灰地面等）会得到多个簇，洪水可分别从每个簇扩张；
    过小或与已保留簇过近的簇会被丢弃（视为贴边主体色/噪声）。
    """
    w, h = _work_dims(img.width, img.height, work_size)
    small = img.convert("RGB").resize((w, h), Image.LANCZOS)
    px = small.load()
    strip = _edge_strip_mask(px, w, h)
    cells = _border_cells(px, w, h, strip)
    if not cells:
        return [(0, 0, 0)]
    k = min(MAX_BG_CLUSTERS, len(cells))
    centers, counts = _kmeans_clusters(cells, k)
    pairs = sorted(zip(centers, counts), key=lambda p: -p[1])
    total = sum(counts)
    kept: list[tuple] = []
    for center, count in pairs:
        if count < total * CLUSTER_MIN_RATIO:
            continue
        if any(_d2(center, other) <= CLUSTER_MERGE_DIST ** 2 for other in kept):
            continue
        kept.append(center)
    return kept or [pairs[0][0]]


def _dynamic_tolerance(cells: list, bgs: list[tuple]) -> float:
    """从边框噪声估计容差: 边框距离中位数 + k × MAD, 限幅 TOL_MIN~TOL_MAX。"""
    dists = sorted(min(_d2(c, b) for b in bgs) ** 0.5 for c in cells)
    med = dists[len(dists) // 2]
    devs = sorted(abs(d - med) for d in dists)
    mad = devs[len(devs) // 2]
    tol = med + TOL_K * mad
    return min(TOL_MAX, max(TOL_MIN, tol))


def estimate_background_color(img: Image.Image, work_size: int = BG_WORK_SIZE) -> tuple[int, int, int]:
    """估计图片边框主色（背景色），供多帧统一抠图使用。

    深色均匀条带 (截图 UI 条) 不参与估计, 避免被其污染 (如证件照截图边框
    大部分是深色条带, 中位色会偏离照片真实背景)。极端情况 (边框全是条带)
    回退到边框内侧一圈单元格。
    """
    w, h = _work_dims(img.width, img.height, work_size)
    small = img.convert("RGB").resize((w, h), Image.LANCZOS)
    w, h = small.size
    px = small.load()
    strip = _edge_strip_mask(px, w, h)

    def _border_cells(inner: int = 0):
        cells = []
        for x in range(w):
            if not strip[inner][x]:
                cells.append(px[x, inner])
            if not strip[h - 1 - inner][x]:
                cells.append(px[x, h - 1 - inner])
        for y in range(h):
            if not strip[y][inner]:
                cells.append(px[inner, y])
            if not strip[y][w - 1 - inner]:
                cells.append(px[w - 1 - inner, y])
        return cells

    for inner in range(min(w, h) // 2):
        cells = _border_cells(inner)
        if cells:
            return tuple(sorted(cells, key=lambda c: sum(c))[len(cells) // 2])
    return (0, 0, 0)


def auto_remove_background(
    img: Image.Image,
    tolerance: int | None = None,
    keep_existing_alpha: bool = False,
    bg_hint: tuple[int, int, int] | list[tuple[int, int, int]] | None = None,
    allow_mostly_background: bool = False,
    *,
    quality_size: int = TARGET_SIZE,
    return_diagnostics: bool = False,
) -> tuple[Image.Image, bool] | tuple[Image.Image, bool, RemovalDiagnostics]:
    """
    对没有透明通道的图片，自动去除纯色/渐变背景（含阴影）。

    方法: 把边框像素聚类为 1~3 个背景色 (多背景色聚类), 从四条边框向内做
    BFS 洪水填充。tolerance 默认按边框噪声自动估计 (中位数 + k×MAD,
    限幅 24~88)。除"颜色与某背景色距离 <= tolerance"的经典规则外，还支持:

      0) 深色均匀条带 (截图 UI 条) 预抠 —— 紧贴边框的均匀深色行/列被识别为
         图像外的装饰, 直接标记为背景且不参与播种。否则洪水会从条带平滑漫入
         颜色完全相同的黑色头发/领带/西服 (深色主体部件与 UI 条无法用颜色区分,
         只能靠"条带=均匀+贴边"的结构特征分离)。
      1) 梯度规则 —— 逐像素颜色变化 <= GRAD_STEP 且距最近背景簇 <= GRAD_CAP
         的像素可沿渐变一路穿越。这使两类背景都能整体去除: 白色平面 +
         明显阴影 (亮度渐变), 以及彩色背景的色相/亮度漂移 (如证件照蓝底
         的照明渐变)。主体受多重保护: GRAD_CAP 挡住距背景色过远的主体
         (棕色蟑螂 260+、肤色 220+ 都超上限), 主体轮廓的锐利跳变
         (> GRAD_STEP) 挡住边缘本身, 色相一致性挡住暖色主体, 亮度下限
         挡住黑色头发/西服等深色部件。
      2) 孤岛填充 —— 被主体完全包围的背景口袋（浅灰阴影）与已抠区域不相邻，
         主洪水够不到；凡与"主体彩色连通块"（高饱和、内部平滑、足够大）相邻
         的低饱和背景像素，一并并入背景，上限 ISLAND_MAX 与收紧的
         ISLAND_CAP 防止误吃浅色皮肤高光等低饱和主体部件。
      3) 贴边主体门控 —— 每条边被分成若干小段, 段中位色距任何背景簇都超过
         动态容差时, 该段不参与播种 (很可能是贴在边上的主体, 洪水会从其内部
         开始); 全部段被拒时回退为逐像素播种, 由候选掩码回退与质量诊断兜底。

    工作尺寸取目标光标尺寸的 WORK_SCALE 倍 (48→192, 64→256, 96→384,
    上限 WORK_MAX_SIDE) 且保持原图长宽比; 小图不放大, 避免噪声被缩放的
    平滑效应抹平后误当渐变背景漫灌。

    Args:
        img: RGBA 或 RGB 图像
        tolerance: 颜色容差（欧氏距离上限）；None 时按边框噪声自动估计
        keep_existing_alpha: True 时即使图片已有透明通道也执行抠背景，
            最终 alpha = min(原 alpha, 抠图掩码)。
            用于动画文件（如 GIF）中部分帧透明、部分帧纯色背景的情况，
            保证所有帧背景一致，避免播放时闪现背景色块。
        bg_hint: 基准背景色（多帧动画共用）；也接受背景色列表
        quality_size: 在实际光标尺度上评估掩码质量（通常为 48/64/96）。
        return_diagnostics: True 时额外返回 RemovalDiagnostics；默认保持旧的
            ``(image, ok)`` 调用契约。

    Returns:
        默认返回 (处理后的 RGBA 图像, 是否成功)；启用诊断时返回
        (处理后的 RGBA 图像, 是否成功, RemovalDiagnostics)。
        失败时返回原图（转 RGBA），由调用方决定如何处理。
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # 已自带有效透明通道且无需强制 → 直接返回
    orig_alpha = img.getchannel("A")
    had_alpha = orig_alpha.getextrema()[0] < 250

    def _result(out_img: Image.Image, ok: bool, diag: RemovalDiagnostics):
        if return_diagnostics:
            return out_img, ok, diag
        return out_img, ok

    if had_alpha and not keep_existing_alpha:
        quality_alpha = _fit_alpha_for_quality(orig_alpha, quality_size)
        stats = _mask_quality(quality_alpha)
        confidence, mask_issues = _diagnose_mask(stats)
        diag = RemovalDiagnostics(
            success=True,
            method="existing_alpha",
            selected_stage="existing",
            confidence=confidence,
            auto_apply=confidence == "high",
            removed_ratio=1.0 - stats["foreground_ratio"],
            mask_stats=stats,
            issues=mask_issues,
        )
        return _result(img, True, diag)

    # 工作尺寸: 目标光标尺寸的 WORK_SCALE 倍且保持长宽比, 只缩小不放大。
    # 放大小图会把噪声/高频细节抹平成"平滑渐变", 导致梯度规则把噪声误当
    # 背景漫灌 (如 64px 随机噪声图)。
    work_side = min(WORK_MAX_SIDE, max(16, int(round(WORK_SCALE * quality_size))))
    w, h = _work_dims(img.width, img.height, work_side)
    small = img.convert("RGB").resize((w, h), Image.LANCZOS)
    px = small.load()

    # 深色均匀条带 (截图 UI 条): 预标记为已抠, 且不参与播种 —— 否则洪水会从
    # 条带平滑漫入颜色相同的黑色头发/领带/西服
    strip = _edge_strip_mask(px, w, h)

    # 背景色簇: 优先使用调用方给的基准色（多帧动画共用, 单簇），
    # 否则从边框聚类出 1~3 个背景色 (双色/多色背景分别扩张)。
    if bg_hint is None:
        bgs = _estimate_background_clusters(img, work_side)
    elif len(bg_hint) == 3 and all(isinstance(v, int) for v in bg_hint):
        bgs = [tuple(bg_hint)]
    else:
        bgs = [tuple(c) for c in bg_hint]

    border_cells = _border_cells(px, w, h, strip)
    tol = float(tolerance) if tolerance is not None else (
        _dynamic_tolerance(border_cells, bgs) if border_cells else 42.0)
    tol2 = tol * tol
    grad_step2 = GRAD_STEP * GRAD_STEP
    grad_cap2 = GRAD_CAP * GRAD_CAP
    island_cap2 = ISLAND_CAP * ISLAND_CAP

    def sat(c: tuple) -> int:
        return max(c) - min(c)

    def lum(c: tuple) -> float:
        return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]

    single_bg = len(bgs) == 1
    bg0 = bgs[0]

    def bg_min(c: tuple) -> tuple[int, int]:
        """返回 (距最近背景簇的平方距离, 簇下标)。"""
        if single_bg:
            return _d2(c, bg0), 0
        best_i = 0
        best_d = _d2(c, bgs[0])
        for i in range(1, len(bgs)):
            d = _d2(c, bgs[i])
            if d < best_d:
                best_d, best_i = d, i
        return best_d, best_i

    # ── 主洪水 (BFS, 从边框种子开始) ──
    # 经典规则: dist(P, 最近背景簇) <= tolerance —— 均匀背景快速扩散
    # 梯度规则: dist(P, Q) <= GRAD_STEP 且 dist(P, 最近背景簇) <= GRAD_CAP 且
    #           (P.R - P.B) <= 簇.R - 簇.B + HUE_DELTA 且亮度 >= LUM_FLOOR ——
    #   平滑渐变 (阴影/光照/彩色背景的色相漂移) 可以一路穿越; 主体由多层保护:
    #   GRAD_CAP (距背景色过远), 轮廓锐利跳变 (> GRAD_STEP), 色相一致性
    #   (主体是暖色), 亮度下限 (深色主体部件)。
    visited = [[False] * w for _ in range(h)]
    q: deque[tuple[int, int]] = deque()

    def seed_ok(c: tuple) -> bool:
        d2, _i = bg_min(c)
        return d2 <= tol2 or (
            sat(c) <= SEED_SAT_LIMIT and d2 <= grad_cap2
            and lum(c) >= LUM_FLOOR)

    def try_seed(x: int, y: int) -> None:
        if visited[y][x]:
            return
        c = px[x, y]
        if seed_ok(c):
            visited[y][x] = True
            q.append((x, y))

    # 贴边主体门控: 每条边分成若干小段; 段中位色距任何背景簇都超过动态容差
    # 时, 该段不播种 (很可能是贴在边上的主体, 洪水会从其内部开始)。全部段
    # 被拒时回退为逐像素播种, 结果质量由候选掩码回退与质量诊断兜底。
    seg_len = max(SEG_LEN_MIN, min(w, h) // 8)
    seeded_any = False

    def seed_segment(seg_cells: list) -> None:
        nonlocal seeded_any
        if len(seg_cells) < 3:
            return
        med = _median_color([px[x, y] for x, y in seg_cells])
        if min(_d2(med, b) for b in bgs) > tol2:
            return
        seeded_any = True
        for x, y in seg_cells:
            try_seed(x, y)

    for y in (0, h - 1):
        for sx in range(0, w, seg_len):
            seed_segment([(x, y) for x in range(sx, min(w, sx + seg_len))
                          if not strip[y][x]])
    for x in (0, w - 1):
        for sy in range(0, h, seg_len):
            seed_segment([(x, y) for y in range(sy, min(h, sy + seg_len))
                          if not strip[y][x]])

    if not seeded_any:
        # 所有边缘段都不属于背景 (主体贴满四周): 回退为逐像素播种,
        # 结果质量由候选掩码回退与质量诊断兜底。
        for x in range(w):
            for y in (0, h - 1):
                if not strip[y][x]:
                    try_seed(x, y)
        for y in range(h):
            for x in (0, w - 1):
                if not strip[y][x]:
                    try_seed(x, y)
    # 条带内侧边界单元格也作为候选种子 (条带被预抠后, 照片边缘露出)
    for y in range(h):
        for x in range(w):
            if visited[y][x] or strip[y][x]:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and strip[ny][nx]:
                    try_seed(x, y)
                    break

    while q:
        cx, cy = q.popleft()
        pc = px[cx, cy]
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < w and 0 <= ny < h and not visited[ny][nx]:
                nc = px[nx, ny]
                bd2, bi = bg_min(nc)
                ok = bd2 <= tol2 or (
                    bd2 <= grad_cap2
                    and _d2(nc, pc) <= grad_step2
                    and nc[0] - nc[2] <= bgs[bi][0] - bgs[bi][2] + HUE_DELTA
                    and lum(nc) >= LUM_FLOOR)
                if ok:
                    visited[ny][nx] = True
                    q.append((nx, ny))

    # 保留主洪水的保守掩码。后续孤岛/彩色补抠只作为候选；若造成主体孔洞、
    # 碎裂或额外删除过多，会自动回退到这里，而不是把激进结果直接交给用户。
    base_visited = [row[:] for row in visited]

    # ── 孤岛填充 ──
    # 白色平面+阴影照片里, 主体周围可能残留被主体完全包围的背景口袋
    # (浅灰阴影)。这些口袋与已抠区域不相邻, 主洪水够不到。这里只填
    # "与主体相邻"的口袋: 主体 = 未访问的高饱和(S>40) 8-连通块, 且内部
    # 平滑(相邻像素平均距离 <= COHERENCE, 排除随机噪声)、足够大。
    # 口袋 = 低饱和(S<=ISLAND_SAT) 且距背景色 <= ISLAND_CAP 的像素,
    # 从主体边界 8-邻接处开始向内扩展, 单块不超过 ISLAND_MAX 像素
    # (避免误吃浅灰/中性色的主体部件, 如白底上的浅灰箭头)。
    subject_mask = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if not visited[y][x] and sat(px[x, y]) > ISLAND_SAT:
                subject_mask[y][x] = True
    seen2 = [[False] * w for _ in range(h)]
    subjects: list[list[tuple[int, int]]] = []
    for y in range(h):
        for x in range(w):
            if not subject_mask[y][x] or seen2[y][x]:
                continue
            comp: list[tuple[int, int]] = []
            qq = deque([(x, y)])
            seen2[y][x] = True
            while qq:
                cx, cy = qq.popleft()
                comp.append((cx, cy))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx, ny = cx + dx, cy + dy
                        if (0 <= nx < w and 0 <= ny < h and subject_mask[ny][nx]
                                and not seen2[ny][nx]):
                            seen2[ny][nx] = True
                            qq.append((nx, ny))
            if len(comp) < SUBJECT_MIN:
                continue
            total_d = 0.0
            pairs = 0
            for cx, cy in comp:
                c = px[cx, cy]
                for dx, dy in ((1, 0), (0, 1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and subject_mask[ny][nx]:
                        nc = px[nx, ny]
                        total_d += ((c[0] - nc[0]) ** 2 + (c[1] - nc[1]) ** 2
                                    + (c[2] - nc[2]) ** 2) ** 0.5
                        pairs += 1
            if pairs and total_d / pairs <= COHERENCE:
                subjects.append(comp)

    def island_ok(c: tuple) -> bool:
        bd2, bi = bg_min(c)
        return (sat(c) <= ISLAND_SAT and bd2 <= island_cap2
                and c[0] - c[2] <= bgs[bi][0] - bgs[bi][2] + ISLAND_WARM
                and lum(c) >= LUM_FLOOR)

    island_visited = [[False] * w for _ in range(h)]
    for comp in subjects:
        for cx, cy in comp:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (-1, -1), (1, -1), (-1, 1)):
                nx, ny = cx + dx, cy + dy
                if (0 <= nx < w and 0 <= ny < h and not visited[ny][nx]
                        and not island_visited[ny][nx]):
                    c = px[nx, ny]
                    if island_ok(c):
                        island_visited[ny][nx] = True
    q2 = deque()
    for y in range(h):
        for x in range(w):
            if island_visited[y][x]:
                q2.append((x, y))
    while q2:
        cx, cy = q2.popleft()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = cx + dx, cy + dy
                if (0 <= nx < w and 0 <= ny < h and not visited[ny][nx]
                        and not island_visited[ny][nx]):
                    c = px[nx, ny]
                    if island_ok(c):
                        island_visited[ny][nx] = True
                        q2.append((nx, ny))
    filled = [[False] * w for _ in range(h)]
    seen3 = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if not island_visited[y][x] or seen3[y][x]:
                continue
            comp: list[tuple[int, int]] = []
            qq = deque([(x, y)])
            seen3[y][x] = True
            while qq:
                cx, cy = qq.popleft()
                comp.append((cx, cy))
                if len(comp) > ISLAND_MAX:
                    break
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx, ny = cx + dx, cy + dy
                        if (0 <= nx < w and 0 <= ny < h and island_visited[ny][nx]
                                and not seen3[ny][nx]):
                            seen3[ny][nx] = True
                            qq.append((nx, ny))
            if len(comp) <= ISLAND_MAX:
                for cx, cy in comp:
                    filled[cy][cx] = True
    for y in range(h):
        for x in range(w):
            if filled[y][x]:
                visited[y][x] = True
    island_stage_visited = [row[:] for row in visited]

    # ── 彩色口袋填充 (逐像素) ──
    # 高饱和背景 (如证件照蓝底) 可能被主体+UI 条带四面围死, 主洪水/低饱和
    # 孤岛都够不到。逐像素补抠: 未访问的高饱和(S>ISLAND_SAT)像素, 若
    # (a) 距最近背景簇 <= GRAD_CAP (脸 220+/蟑螂 260+ 超限被拒),
    # (b) 色相一致 (R-B <= 簇.R-簇.B+HUE_DELTA, 暖色皮肤/领带被拒),
    # (c) 局部平滑 (其彩色 4-邻域平均差 <= COHERENCE, 随机噪声邻域差大被拒),
    # 则并入背景。
    chroma_visited = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if not visited[y][x] and sat(px[x, y]) > ISLAND_SAT:
                chroma_visited[y][x] = True
    for y in range(h):
        for x in range(w):
            if not chroma_visited[y][x]:
                continue
            c = px[x, y]
            bd2, bi = bg_min(c)
            if bd2 > grad_cap2 or c[0] - c[2] > bgs[bi][0] - bgs[bi][2] + HUE_DELTA:
                continue
            total_d = 0.0
            pairs = 0
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if (0 <= nx < w and 0 <= ny < h and chroma_visited[ny][nx]):
                    nc = px[nx, ny]
                    total_d += ((c[0] - nc[0]) ** 2 + (c[1] - nc[1]) ** 2
                                + (c[2] - nc[2]) ** 2) ** 0.5
                    pairs += 1
            if pairs and total_d / pairs <= COHERENCE:
                visited[y][x] = True

    aggressive_visited = [row[:] for row in visited]

    def _work_mask(stage: list[list[bool]]) -> Image.Image:
        """背景=True 的工作掩码转为前景=255 的 L 图。"""
        mask = Image.new("L", (w, h), 0)
        mask.putdata([
            0 if stage[y][x] else 255
            for y in range(h) for x in range(w)
        ])
        return mask

    def _quality_alpha(stage: list[list[bool]]) -> Image.Image:
        """不构造全尺寸中间图，直接把候选掩码映射到最终光标尺度。"""
        target = max(16, min(256, int(quality_size)))
        scale = min(target / img.width, target / img.height, 1.0)
        new_w = max(1, round(img.width * scale))
        new_h = max(1, round(img.height * scale))
        alpha = _work_mask(stage).resize((new_w, new_h), Image.Resampling.BILINEAR)
        if had_alpha and keep_existing_alpha:
            source_alpha = orig_alpha.resize((new_w, new_h), Image.Resampling.LANCZOS)
            alpha = ImageChops.darker(source_alpha, alpha)
        canvas = Image.new("L", (target, target), 0)
        canvas.paste(alpha, ((target - new_w) // 2, (target - new_h) // 2))
        return canvas

    base_alpha = _quality_alpha(base_visited)
    island_alpha = _quality_alpha(island_stage_visited)
    aggressive_alpha = _quality_alpha(aggressive_visited)
    base_stats = _mask_quality(base_alpha)
    island_stats = _mask_quality(island_alpha)
    aggressive_stats = _mask_quality(aggressive_alpha)
    island_removed = _removed_between(base_alpha, island_alpha)
    aggressive_removed = _removed_between(base_alpha, aggressive_alpha)

    def _safe_candidate(candidate_stats: dict, extra_removed: float) -> bool:
        """后处理只能小幅清背景，不能显著损坏保守主体。"""
        if extra_removed > 0.08:
            return False
        if candidate_stats["largest_component_ratio"] + 0.05 < base_stats["largest_component_ratio"]:
            return False
        if candidate_stats["significant_components"] > base_stats["significant_components"] + 2:
            return False
        hole_growth = candidate_stats["hole_ratio"] - base_stats["hole_ratio"]
        if hole_growth > 0.015 and candidate_stats["hole_ratio"] > 0.025:
            return False
        return True

    # 优先使用最完整的清理结果；任何质量回退都记录在诊断中，并禁止无提示自动应用。
    selected_stage = "aggressive"
    selected_visited = aggressive_visited
    selected_alpha = aggressive_alpha
    selected_stats = aggressive_stats
    if not _safe_candidate(aggressive_stats, aggressive_removed):
        if _safe_candidate(island_stats, island_removed):
            selected_stage = "island"
            selected_visited = island_stage_visited
            selected_alpha = island_alpha
            selected_stats = island_stats
        else:
            selected_stage = "base"
            selected_visited = base_visited
            selected_alpha = base_alpha
            selected_stats = base_stats
    fallback_used = selected_stage != "aggressive" and aggressive_removed > 0.005

    # 成功仍要求确实去掉了背景；但 success 与是否可以自动应用已经分离。
    removed = sum(1 for row in selected_visited for value in row if value)
    ratio = removed / (w * h)
    if ratio < 0.015 or (ratio > 0.93 and not allow_mostly_background):
        original_quality = _fit_alpha_for_quality(orig_alpha, quality_size)
        original_stats = _mask_quality(original_quality)
        failure_issue = Issue(
            "error", "NO_BG",
            "无法可靠识别背景，已保留原图并停止自动应用。",
        )
        diag = RemovalDiagnostics(
            success=False,
            method="none",
            selected_stage="original",
            confidence="low",
            auto_apply=False,
            removed_ratio=ratio,
            postfill_removed_ratio=aggressive_removed,
            fallback_used=True,
            mask_stats=original_stats,
            issues=[failure_issue],
        )
        return _result(img, False, diag)

    # 二值分割掩码不再大倍数 LANCZOS 放大（会振铃/产生宽软边）；BILINEAR 仅形成
    # 单调过渡，最终 RGBA 缩放再由预乘 alpha 路径完成抗锯齿。
    mask = _work_mask(selected_visited).resize(img.size, Image.Resampling.BILINEAR)
    out = img.copy()
    if had_alpha and keep_existing_alpha:
        out.putalpha(ImageChops.darker(orig_alpha, mask))
    else:
        out.putalpha(mask)

    confidence, mask_issues = _diagnose_mask(
        selected_stats,
        fallback_used=fallback_used,
    )
    diag = RemovalDiagnostics(
        success=True,
        method="heuristic",
        selected_stage=selected_stage,
        confidence=confidence,
        auto_apply=confidence == "high",
        removed_ratio=ratio,
        postfill_removed_ratio=aggressive_removed,
        fallback_used=fallback_used,
        mask_stats=selected_stats,
        issues=mask_issues,
    )
    return _result(out, True, diag)


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

    # 白底 + 阴影平面 + 彩色主体（无 alpha）→ 阴影背景应整体去除（回归用例）
    img = Image.new("RGB", (160, 160))
    px = img.load()
    for y in range(160):                       # 垂直渐变: 白(255) → 浅灰(120)
        g = 255 - int(135 * y / 159)
        for x in range(160):
            px[x, y] = (g, g, g)
    d = ImageDraw.Draw(img)
    d.ellipse((10, 0, 80, 45), fill=(255, 255, 255, 255))   # 白色高光(硬边界)
    d.ellipse((50, 40, 110, 104), fill=(195, 103, 18, 255)) # 橙色主体
    for i in range(24):                                     # 主体下方深阴影
        shade = 150 - i * 3
        d.ellipse((52 - i // 2, 104, 108 + i // 2, 104 + i * 2),
                  fill=(shade, shade, shade, 255))
    d.rectangle((54, 104, 62, 124), fill=(80, 40, 20, 255)) # 深棕腿
    d.rectangle((98, 104, 106, 124), fill=(80, 40, 20, 255))
    out["shadow_plane"] = img

    # 蓝底证件照（无 alpha）→ 彩色背景+渐变应去除, 肤色/阴影皮肤应保留（回归用例）
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
    d.ellipse((140, 120, 168, 178), fill=(180, 145, 140, 255)) # 阴影皮肤
    d.rectangle((96, 84, 148, 98), fill=(50, 45, 55, 255))     # 头发
    out["blue_id_photo"] = img

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
    for name in ("white_bg_square", "noise_photo", "shadow_plane"):
        img, ok = auto_remove_background(images[name])
        a = analyze(img, removal_ok=ok)
        print(f"  {name:<18} 去除成功={ok}, 处理后结论={a.verdict}, "
              f"opaque={a.stats['opaque_ratio']}")
    # 阴影平面: 主体(橙色椭圆)必须保留, 白色高光与阴影必须去除
    sp, sp_ok = auto_remove_background(images["shadow_plane"])
    sp_px = sp.convert("RGBA")
    body_a = sp_px.getpixel((80, 72))[3]
    corner_a = sp_px.getpixel((3, 3))[3]
    shadow_a = sp_px.getpixel((80, 115))[3]
    print(f"  shadow_plane       主体alpha={body_a}(>200), 角落alpha={corner_a}(<=120), "
          f"深阴影alpha={shadow_a}(<=120)")

    # 蓝底证件照: 脸部/阴影皮肤保留, 蓝底/深色条带去除
    idp, idp_ok = auto_remove_background(images["blue_id_photo"])
    idp_px = idp.convert("RGBA")
    id_face = idp_px.getpixel((122, 143))[3]
    id_shade = idp_px.getpixel((152, 148))[3]
    id_blue = idp_px.getpixel((30, 120))[3]
    id_corner = idp_px.getpixel((3, 3))[3]
    print(f"  blue_id_photo      脸部alpha={id_face}(>200), 阴影皮肤alpha={id_shade}(>200), "
          f"蓝底alpha={id_blue}(<=120), 条带alpha={id_corner}(<=120)")

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
