"""
gui_server.py — 自定义光标控制器 Web 后端

为 Vue 前端提供本地 HTTP API，复用项目现有的全部核心逻辑:
  - pointer_analyzer: 图片适合度分析 + 自动抠背景
  - ani_builder:      生成 .ani 光标文件
  - cursor_manager:  Win32 光标替换 / 恢复（CursorManager）

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
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from PIL import Image, ImageChops, ImageFilter

from ani_builder import build_ani_file, save_ani_file
from pointer_analyzer import (
    HARD_REJECT_CODES,
    RemovalDiagnostics,
    analyze,
    auto_remove_background,
)
from cursor_manager import CursorManager

CANVAS_SIZE = 48            # 旧默认（兼容引用）
DEFAULT_CANVAS_SIZE = 64    # 光标画布尺寸（可在界面选择 48/64/96）
FRAME_MS = 150              # 默认帧时长
SIZE_CHOICES = (32, 48, 64, 96, 128)


def _resize_rgba_premultiplied(
    img: Image.Image,
    size: tuple[int, int],
    resample=Image.Resampling.LANCZOS,
) -> Image.Image:
    """用预乘 alpha 缩放 RGBA，避免透明像素隐藏 RGB 造成黑/白色边缘泄漏。"""
    rgba = img.convert("RGBA")
    if rgba.size == size:
        return rgba.copy()
    # Pillow 的 RGBa 是预乘 alpha 模式；缩放后转回直 alpha 的 RGBA。
    return rgba.convert("RGBa").resize(size, resample).convert("RGBA")


CROP_MARGIN = 0.06        # 第二阶段: alpha bbox 向外留 6% 边距（主体自动裁剪）
CROP_FILL_RATIO = 0.75    # 主体较大边已占画布较大边 75% 时不裁剪（基本铺满的图标
                          # 裁剪收益小, 且会改变旋转/热点的既有布局预期）


def _feather_alpha_additive(img: Image.Image, radius: float) -> Image.Image:
    """最终尺寸受控羽化: 预乘 alpha 高斯模糊后, alpha 只增不减 (max)。

    模糊在预乘空间进行, 半透明像素的 RGB 随 alpha 一起带出主体颜色,
    不会出现黑边/白边; alpha 取 max(原值, 模糊值) 保证细腿/触角等
    1px 特征不会被模糊减弱。
    """
    if radius <= 0:
        return img.copy()
    rgba = img.convert("RGBA")
    blurred = (rgba.convert("RGBa")
               .filter(ImageFilter.GaussianBlur(radius))
               .convert("RGBA"))
    out = blurred.copy()
    out.putalpha(ImageChops.lighter(rgba.getchannel("A"),
                                    blurred.getchannel("A")))
    return out


def _subject_crop_box(frames: list[Image.Image]) -> tuple[int, int, int, int] | None:
    """多帧联合 alpha bbox + 边距（GIF 用联合框避免动画跳动）。

    主体已铺满画布 CROP_FILL_RATIO 时不裁剪。
    """
    boxes = []
    for frame in frames:
        bbox = frame.getchannel("A").point(
            lambda a: 255 if a > 32 else 0).getbbox()
        if bbox:
            boxes.append(bbox)
    if not boxes:
        return None
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)
    fw, fh = frames[0].size
    if max(right - left, bottom - top) >= CROP_FILL_RATIO * max(fw, fh):
        return None
    mx = max(1, round((right - left) * CROP_MARGIN))
    my = max(1, round((bottom - top) * CROP_MARGIN))
    return (max(0, left - mx), max(0, top - my),
            min(fw, right + mx), min(fh, bottom + my))


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


def _sanitize_name(name: str, fallback: str = "未命名") -> str:
    """清洗用户文件名/光标名，去掉路径与危险字符。"""
    name = os.path.basename(name or "").strip()
    name = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip()
    return name[:80] or fallback


def _decode_filename(raw: bytes) -> str:
    """将 multipart filename 的原始字节解码为正确文本。

    浏览器（Chrome/Edge/Firefox）把非 ASCII 文件名以 UTF-8 原始字节
    直接写入 filename="..."；少数旧客户端可能用 GBK。按
    UTF-8 → GBK → latin-1 依次尝试，保证不抛异常。
    """
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _repair_mojibake(name: str) -> str:
    """修复历史数据中因 latin-1 误解码产生的乱码（UTF-8 字节被当 latin-1 读）。

    仅当字符串整体能按 latin-1 还原为字节、且还原后是合法 UTF-8 文本
    （含非 ASCII 字符）时才修复，否则原样返回，避免误伤正常名称。
    """
    try:
        repaired = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    if repaired == name or not any(ord(ch) > 127 for ch in repaired):
        return name
    return repaired


class CursorStore:
    """数据存储: 保存上传原图与生成的光标。

    主位置: C:\\Program Files\\custom-cursor-controller\\data（跨目录固定）。
    结构:
      <root>/index.json              元数据索引
      <root>/uploads/<uid><ext>      上传的原始文件
      <root>/uploads/<uid>_thumb.png 缩略图
      <root>/cursors/<cid>/ani       生成的 .ani 光标
      <root>/cursors/<cid>/preview.png   首帧预览
      <root>/cursors/<cid>/frame_N.png   每帧 PNG（用于恢复工作状态）

    主位置不可写（如非管理员运行）时依次回退到程序目录 data/、系统临时目录。
    """

    def __init__(self, root: str | None = None):
        self.root = root or self._resolve_root()
        self.uploads_dir = os.path.join(self.root, "uploads")
        self.cursors_dir = os.path.join(self.root, "cursors")
        os.makedirs(self.uploads_dir, exist_ok=True)
        os.makedirs(self.cursors_dir, exist_ok=True)
        self.index_path = os.path.join(self.root, "index.json")
        self.index: dict = {"uploads": {}, "cursors": {}}
        self._load()

    def _resolve_root(self) -> str:
        """数据目录主位置: C:\\Program Files\\custom-cursor-controller\\data。

        优先使用固定位置（可迁移、不受程序所在目录影响）；不可写时依次回退到
        程序目录下的 data/、系统临时目录（非管理员运行 exe 时 Program Files 通常不可写）。
        """
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "custom-cursor-controller", "data"),
        ]
        base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
        candidates.append(os.path.join(base, "data"))
        candidates.append(os.path.join(tempfile.gettempdir(), "custom_cursor_data"))
        last_err: OSError | None = None
        for c in candidates:
            try:
                os.makedirs(c, exist_ok=True)
                probe = os.path.join(c, ".probe")
                with open(probe, "w", encoding="utf-8") as f:
                    f.write("ok")
                os.remove(probe)
                return c
            except OSError as e:
                last_err = e
        raise OSError(f"无法创建数据目录: {last_err}")

    def _load(self) -> None:
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                self.index = json.load(f)
        except Exception:
            self.index = {}
        self.index.setdefault("uploads", {})
        self.index.setdefault("cursors", {})
        self._repair_index_names()

    def _repair_index_names(self) -> None:
        """修复历史条目中因 multipart latin-1 误解码产生的乱码名称。"""
        changed = False
        for table, field in ((self.index.get("uploads", {}), "original"),
                             (self.index.get("cursors", {}), "name")):
            for entry in table.values():
                if isinstance(entry.get(field), str):
                    fixed = _repair_mojibake(entry[field])
                    if fixed != entry[field]:
                        entry[field] = fixed
                        changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False)
        os.replace(tmp, self.index_path)

    # ── 上传 ──────────────────────────────────────────
    def add_upload(self, original_name: str, data: bytes,
                   thumb_png: bytes, meta: dict) -> dict:
        uid = uuid.uuid4().hex[:12]
        ext = os.path.splitext(original_name)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
            ext = ".png"
        rel = f"uploads/{uid}{ext}"
        with open(os.path.join(self.root, rel), "wb") as f:
            f.write(data)
        thumb_rel = f"uploads/{uid}_thumb.png"
        with open(os.path.join(self.root, thumb_rel), "wb") as f:
            f.write(thumb_png)
        entry = {
            "id": uid,
            "filename": rel,
            "thumb": thumb_rel,
            "original": _sanitize_name(original_name, "图片.png"),
            "created": meta.get("created", ""),
            "category": meta.get("category", ""),
            "size": meta.get("size", [0, 0]),
            "verdict": meta.get("verdict", ""),
            "score": meta.get("score", 0),
            "sha256": meta.get("sha256", ""),
        }
        self.index["uploads"][uid] = entry
        self._save()
        return entry

    def find_upload_by_sha(self, sha256: str) -> dict | None:
        """按内容哈希查找已存在的上传（查重）。"""
        if not sha256:
            return None
        for e in self.index["uploads"].values():
            if e.get("sha256") == sha256:
                return e
        return None

    # ── 光标 ──────────────────────────────────────────
    def add_cursor(self, ani_bytes: bytes, preview_png: bytes,
                   frames_png: list[bytes], meta: dict) -> dict:
        cid = uuid.uuid4().hex[:12]
        folder = os.path.join(self.cursors_dir, cid)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "ani"), "wb") as f:
            f.write(ani_bytes)
        with open(os.path.join(folder, "preview.png"), "wb") as f:
            f.write(preview_png)
        for i, png in enumerate(frames_png):
            with open(os.path.join(folder, f"frame_{i}.png"), "wb") as f:
                f.write(png)
        entry = {
            "id": cid,
            "folder": f"cursors/{cid}",
            "name": _sanitize_name(meta.get("name", ""), "光标"),
            "created": meta.get("created", ""),
            "category": meta.get("category", ""),
            "size": meta.get("size", 64),
            "hotspot": meta.get("hotspot", [32, 32]),
            "frames": meta.get("frames", 1),
            "animated": meta.get("animated", False),
            "upload_ids": meta.get("upload_ids", []),
            "source_hashes": sorted(meta.get("source_hashes", [])),
        }
        self.index["cursors"][cid] = entry
        self._save()
        return entry

    def find_cursor_by_sources(self, source_hashes: list[str]) -> dict | None:
        """按来源内容哈希集合查找已存在的光标（查重）。"""
        target = sorted(source_hashes)
        if not target:
            return None
        for e in self.index["cursors"].values():
            if sorted(e.get("source_hashes", [])) == target:
                return e
        return None

    def cursors_for_upload(self, uid: str, exclude: str | None = None) -> list[dict]:
        """返回 upload_ids 包含 uid 的光标条目（exclude 可排除指定光标）。"""
        return [e for e in self.index["cursors"].values()
                if uid in e.get("upload_ids", []) and e.get("id") != exclude]

    def uploads_for_cursor(self, cid: str) -> list[dict]:
        """返回光标关联的、仍存在的上传图片条目。"""
        entry = self.index["cursors"].get(cid)
        if not entry:
            return []
        out = []
        for uid in entry.get("upload_ids", []):
            up = self.index["uploads"].get(uid)
            if up:
                out.append(up)
        return out

    def cursor_ani_path(self, cid: str) -> str | None:
        entry = self.index["cursors"].get(cid)
        if not entry:
            return None
        return os.path.join(self.cursors_dir, cid, "ani")

    def cursor_frames(self, cid: str) -> list[Image.Image] | None:
        entry = self.index["cursors"].get(cid)
        if not entry:
            return None
        folder = os.path.join(self.cursors_dir, cid)
        frames = []
        i = 0
        while True:
            p = os.path.join(folder, f"frame_{i}.png")
            if not os.path.exists(p):
                break
            frames.append(Image.open(p).convert("RGBA"))
            i += 1
        return frames or None

    # ── 列表 / 修改 / 删除 ────────────────────────────
    def list_uploads(self) -> list[dict]:
        return sorted(self.index["uploads"].values(),
                      key=lambda e: e.get("created", ""), reverse=True)

    def list_cursors(self) -> list[dict]:
        return sorted(self.index["cursors"].values(),
                      key=lambda e: e.get("created", ""), reverse=True)

    def get_upload(self, uid: str) -> dict | None:
        return self.index["uploads"].get(uid)

    def get_cursor(self, cid: str) -> dict | None:
        return self.index["cursors"].get(cid)

    def rename_cursor(self, cid: str, name: str) -> dict:
        entry = self.index["cursors"].get(cid)
        if not entry:
            raise KeyError("光标不存在")
        entry["name"] = _sanitize_name(name, "光标")
        self._save()
        return entry

    def set_category(self, kind: str, uid: str, category: str) -> dict:
        table = self.index.get(kind)
        if not table or uid not in table:
            raise KeyError("条目不存在")
        table[uid]["category"] = _sanitize_name(category, "")
        self._save()
        return table[uid]

    def delete(self, kind: str, uid: str) -> None:
        table = self.index.get(kind)
        if not table or uid not in table:
            raise KeyError("条目不存在")
        entry = table.pop(uid)
        self._save()
        # 删除关联文件（尽力而为）
        try:
            if kind == "uploads":
                for rel in (entry["filename"], entry["thumb"]):
                    p = os.path.join(self.root, rel)
                    if os.path.exists(p):
                        os.remove(p)
            else:
                folder = os.path.join(self.cursors_dir, uid)
                if os.path.isdir(folder):
                    for fn in os.listdir(folder):
                        os.remove(os.path.join(folder, fn))
                    os.rmdir(folder)
        except OSError:
            pass


class CursorApp:
    """后端业务逻辑（线程安全）。"""

    def __init__(self, store_root: str | None = None):
        self.mgr = CursorManager()
        self.mgr.backup_original()
        self.lock = threading.Lock()
        self.store = CursorStore(root=store_root)
        self.canvas_size = DEFAULT_CANVAS_SIZE
        self.frames: list[Image.Image] = []
        self.durations: list[int] = []
        self.hotspot = (self.canvas_size // 2, self.canvas_size // 2)
        self.enabled = False
        self.verdict: str | None = None
        self.score: int | None = None
        self.issues: list[dict] = []
        self.removal_confidence: str | None = None
        self.auto_apply_allowed = False
        self.removal_diagnostics: dict | None = None
        self.src_size: tuple[int, int] | None = None
        self.last_cursor_id: str | None = None

    # ── 上传 → 分析 ────────────────────────────────────
    def _process_files(self, file_items: list[tuple[str, bytes]]):
        """解析/抠背景/逐帧分析一组文件（upload 与图库"生成光标"共用）。

        Returns:
            (processed, durations, file_info, verdicts, worst, removal_diags)
            processed: [(处理后的 RGBA 图, 抠背景是否成功)]
            durations: 每帧时长 ms
            file_info: 每个文件的元信息 {name, data, start, count, size}
            verdicts:  每帧的分析结果
            worst:     得分最低的分析结果
            removal_diags: 每帧的抠图完整度/自动应用诊断
        """
        processed: list[tuple[Image.Image, bool]] = []  # (处理后图, 抠背景是否成功)
        removal_diags: list[RemovalDiagnostics] = []
        durations: list[int] = []
        file_info: list[dict] = []   # 每个文件: {name, data, frames 区间, 帧数}

        for _name, data in file_items:
            src = Image.open(io.BytesIO(data))
            n_frames = getattr(src, "n_frames", 1)
            start = len(processed)
            file_durations: list[int] = []
            file_frames: list[Image.Image] = []

            if n_frames > 1:
                # 动画图片（GIF 等）: 提取全部帧，用其自带帧时长
                for i in range(n_frames):
                    src.seek(i)
                    img = src.convert("RGBA")
                    dur = int(src.info.get("duration", FRAME_MS) or FRAME_MS)
                    file_durations.append(max(20, min(2000, dur)))
                    file_frames.append(img)
            else:
                file_durations.append(FRAME_MS)
                file_frames.append(src.convert("RGBA"))

            # 背景一致性: 任一帧无真实 alpha → 该文件全部帧统一抠背景，
            # 避免 GIF 动画播放时（如帧0透明、帧1白底）闪现背景色块。
            # 背景色取第一帧的边框主色作为整组帧的统一基准，
            # 防止个别帧边框被图案占满导致颜色估计失败。
            need_bg = any(f.getchannel("A").getextrema()[0] >= 250 for f in file_frames)
            if need_bg:
                from pointer_analyzer import estimate_background_color
                bg_hint = estimate_background_color(file_frames[0])
                for img in file_frames:
                    out, ok, diag = auto_remove_background(
                        img,
                        keep_existing_alpha=True,
                        bg_hint=bg_hint,
                        allow_mostly_background=n_frames > 1,
                        quality_size=self.canvas_size,
                        return_diagnostics=True,
                    )
                    processed.append((out, ok))
                    removal_diags.append(diag)
            else:
                for img in file_frames:
                    out, ok, diag = auto_remove_background(
                        img,
                        quality_size=self.canvas_size,
                        return_diagnostics=True,
                    )
                    processed.append((out, ok))
                    removal_diags.append(diag)
            durations.extend(file_durations)
            file_info.append({
                "name": _name,
                "data": data,
                "start": start,
                "count": len(file_frames),
                "size": [file_frames[0].width, file_frames[0].height],
            })

        # 逐张分析，取最差
        verdicts = []
        for img, bg_ok in processed:
            has_alpha = img.getchannel("A").getextrema()[0] < 250
            removal = None if has_alpha else bg_ok
            verdicts.append(analyze(img, removal_ok=removal))
        worst = min(verdicts, key=lambda a: a.score)
        return processed, durations, file_info, verdicts, worst, removal_diags

    @staticmethod
    def _quality_summary(worst, removal_diags: list[RemovalDiagnostics]) -> dict:
        """合并适合度与抠图完整度；抠图低置信度永远不能静默自动应用。"""
        rank = {"low": 0, "medium": 1, "high": 2}
        confidence = min(
            (diag.confidence for diag in removal_diags),
            key=lambda value: rank.get(value, 0),
            default="low",
        )
        representative = min(
            removal_diags,
            key=lambda diag: (
                rank.get(diag.confidence, 0),
                -diag.postfill_removed_ratio,
                -len(diag.issues),
            ),
            default=None,
        )
        issues = [{"level": issue.level, "code": issue.code, "message": issue.message}
                  for issue in worst.issues]
        seen_codes = {item.get("code") for item in issues}
        for diag in removal_diags:
            for issue in diag.issues:
                if issue.code not in seen_codes:
                    issues.append({"level": issue.level, "code": issue.code,
                                   "message": issue.message})
                    seen_codes.add(issue.code)

        verdict = worst.verdict
        score = worst.score
        if confidence == "low":
            verdict = "不适合"
            score = min(score, 39)
        elif confidence == "medium" and verdict == "适合":
            verdict = "有风险"
            score = min(score, 69)
        auto_apply = (
            confidence == "high"
            and verdict == "适合"
            and all(diag.auto_apply for diag in removal_diags)
        )
        return {
            "verdict": verdict,
            "score": score,
            "issues": issues,
            "removal_confidence": confidence,
            "auto_apply": auto_apply,
            "removal_diagnostics": representative.as_dict() if representative else None,
        }

    def upload(self, file_items: list[tuple[str, bytes]]) -> dict:
        import datetime
        (processed, durations, file_info, verdicts, worst,
         removal_diags) = self._process_files(file_items)
        quality = self._quality_summary(worst, removal_diags)

        # 统一缩放到画布尺寸（第二阶段: 先按多帧联合 alpha bbox 裁剪+边距,
        # 让主体尽量占满光标; GIF 用联合框避免动画跳动）
        source_frames = [img for img, _bg in processed]
        crop_box = _subject_crop_box(source_frames)
        frames = [self._fit_to_canvas(img, self.canvas_size, crop_box)
                  for img in source_frames]

        with self.lock:
            self.frames = frames
            self.durations = durations[: len(frames)]
            self.hotspot = analyze(frames[0], target_size=self.canvas_size).hotspot
            self.verdict = quality["verdict"]
            self.score = quality["score"]
            self.issues = quality["issues"]
            self.removal_confidence = quality["removal_confidence"]
            self.auto_apply_allowed = quality["auto_apply"]
            self.removal_diagnostics = quality["removal_diagnostics"]
            self.src_size = (processed[0][0].width, processed[0][0].height)

        # ── 存储: 上传原图 + 生成的光标快照 ──
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        upload_ids = []
        file_shas: list[str] = []
        dup_uploads: dict[str, dict] = {}
        for fi in file_info:
            sha = hashlib.sha256(fi["data"]).hexdigest()[:16]
            file_shas.append(sha)
            dup = self.store.find_upload_by_sha(sha)
            if dup:
                dup_uploads[dup["id"]] = dup
            # 该文件的综合结论（取其帧中最低分）
            f_verdicts = verdicts[fi["start"]: fi["start"] + fi["count"]]
            f_worst = min(f_verdicts, key=lambda a: a.score) if f_verdicts else worst
            f_diags = removal_diags[fi["start"]: fi["start"] + fi["count"]]
            f_quality = self._quality_summary(f_worst, f_diags)
            # 缩略图: 用该文件处理后首帧
            first_img = processed[fi["start"]][0]
            thumb = self._make_thumb(first_img, 128)
            entry = self.store.add_upload(
                fi["name"], fi["data"], thumb,
                {"created": now, "size": fi["size"],
                 "verdict": f_quality["verdict"], "score": f_quality["score"],
                 "sha256": sha},
            )
            upload_ids.append(entry["id"])

        # 光标快照: 当前工作帧 + 热点 + 尺寸 + 时长
        cursor_name = _sanitize_name(
            os.path.splitext(file_info[0]["name"])[0],
            datetime.datetime.now().strftime("光标 %m-%d %H:%M"))
        ani_bytes = build_ani_file(
            frames=frames,
            hotspots=[self.hotspot] * len(frames),
            frame_durations_ms=durations[: len(frames)],
            title=cursor_name,
            author="Custom Cursor Web",
        )
        preview_buf = io.BytesIO()
        big = frames[0].resize((frames[0].width * 4, frames[0].height * 4), Image.NEAREST)
        big.save(preview_buf, format="PNG")
        frames_png = []
        for f in frames:
            b = io.BytesIO()
            f.save(b, format="PNG")
            frames_png.append(b.getvalue())
        dup_cursor = self.store.find_cursor_by_sources(file_shas)
        c_entry = self.store.add_cursor(
            ani_bytes, preview_buf.getvalue(), frames_png,
            {"name": cursor_name, "created": now, "size": self.canvas_size,
             "hotspot": list(self.hotspot), "frames": len(frames),
             "animated": len(frames) > 1, "upload_ids": upload_ids,
             "source_hashes": file_shas},
        )
        with self.lock:
            self.last_cursor_id = c_entry["id"]

        # 预览: 每帧 4x 放大 PNG → base64
        previews = self.previews()

        hard_reject = any(
            it.level == "error" and it.code in HARD_REJECT_CODES for it in worst.issues
        )

        return {
            "frames": len(frames),
            "canvas_size": self.canvas_size,
            "hotspot": list(self.hotspot),
            "verdict": quality["verdict"],
            "score": quality["score"],
            "issues": self.issues,
            "removal_confidence": quality["removal_confidence"],
            "auto_apply": quality["auto_apply"],
            "removal_diagnostics": quality["removal_diagnostics"],
            "src_size": list(self.src_size),
            "crop_box": list(crop_box) if crop_box else None,
            "previews": previews,
            "hard_reject": hard_reject,
            "cursor_id": c_entry["id"],
            "cursor_name": c_entry["name"],
            "upload_ids": upload_ids,
            "data_dir": self.store.root,
            "duplicates": {
                "uploads": [{"id": e["id"], "original": e["original"]}
                            for e in dup_uploads.values()],
                "cursors": ([{"id": dup_cursor["id"], "name": dup_cursor["name"]}]
                            if dup_cursor else []),
            },
        }

    @staticmethod
    def _make_thumb(img: Image.Image, size: int = 128) -> bytes:
        """生成方形缩略图（棋盘格底 + 图案居中）。"""
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        scale = min(size / img.width, size / img.height, 1.0)
        w = max(1, round(img.width * scale))
        h = max(1, round(img.height * scale))
        scaled = _resize_rgba_premultiplied(img, (w, h))
        canvas.alpha_composite(scaled, dest=((size - w) // 2, (size - h) // 2))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()

    def generate_from_upload(self, uid: str) -> dict:
        """从图库中已保存的图片生成光标（复用上传分析流程，不重复入库图片）。

        内容查重：相同内容的光标已存在时恢复其工作状态直接复用（existing=True）。
        """
        entry = self.store.get_upload(uid)
        if not entry:
            raise KeyError("图片不存在")
        src_path = os.path.join(self.store.root, entry["filename"])
        if not os.path.isfile(src_path):
            raise ValueError("图片原始文件缺失")
        with open(src_path, "rb") as f:
            data = f.read()
        sha = entry.get("sha256", "") or hashlib.sha256(data).hexdigest()[:16]

        # 内容查重: 相同来源的光标已存在 → 直接复用
        existing = self.store.find_cursor_by_sources([sha])
        if existing:
            frames = self.store.cursor_frames(existing["id"])
            if not frames:
                raise ValueError("已有光标文件缺失")
            with self.lock:
                self.frames = frames
                self.canvas_size = existing["size"]
                self.hotspot = tuple(existing["hotspot"])
                self.durations = [FRAME_MS] * len(frames)
                self.src_size = (frames[0].width, frames[0].height)
                self.verdict = self.score = None
                self.issues = []
                self.removal_confidence = "high"
                self.auto_apply_allowed = True
                self.removal_diagnostics = None
                self.last_cursor_id = existing["id"]
            return {
                "existing": True,
                "frames": len(frames),
                "canvas_size": self.canvas_size,
                "hotspot": list(self.hotspot),
                "verdict": "适合",
                "score": 100,
                "issues": [],
                "removal_confidence": "high",
                "auto_apply": True,
                "removal_diagnostics": None,
                "src_size": list(self.src_size),
                "previews": self.previews(),
                "hard_reject": False,
                "cursor_id": existing["id"],
                "cursor_name": existing["name"],
                "data_dir": self.store.root,
            }

        import datetime
        (processed, durations, _file_info, verdicts, worst,
         removal_diags) = self._process_files([(entry["original"], data)])
        quality = self._quality_summary(worst, removal_diags)

        # 统一缩放到画布尺寸（第二阶段: 多帧联合 alpha bbox 裁剪 + 边距）
        source_frames = [img for img, _bg in processed]
        crop_box = _subject_crop_box(source_frames)
        frames = [self._fit_to_canvas(img, self.canvas_size, crop_box)
                  for img in source_frames]

        with self.lock:
            self.frames = frames
            self.durations = durations[: len(frames)]
            self.hotspot = analyze(frames[0], target_size=self.canvas_size).hotspot
            self.verdict = quality["verdict"]
            self.score = quality["score"]
            self.issues = quality["issues"]
            self.removal_confidence = quality["removal_confidence"]
            self.auto_apply_allowed = quality["auto_apply"]
            self.removal_diagnostics = quality["removal_diagnostics"]
            self.src_size = (processed[0][0].width, processed[0][0].height)

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor_name = _sanitize_name(
            entry["original"],
            datetime.datetime.now().strftime("光标 %m-%d %H:%M"))
        ani_bytes = build_ani_file(
            frames=frames,
            hotspots=[self.hotspot] * len(frames),
            frame_durations_ms=durations[: len(frames)],
            title=cursor_name,
            author="Custom Cursor Web",
        )
        preview_buf = io.BytesIO()
        big = frames[0].resize((frames[0].width * 4, frames[0].height * 4), Image.NEAREST)
        big.save(preview_buf, format="PNG")
        frames_png = []
        for f in frames:
            b = io.BytesIO()
            f.save(b, format="PNG")
            frames_png.append(b.getvalue())
        c_entry = self.store.add_cursor(
            ani_bytes, preview_buf.getvalue(), frames_png,
            {"name": cursor_name, "created": now, "size": self.canvas_size,
             "hotspot": list(self.hotspot), "frames": len(frames),
             "animated": len(frames) > 1, "upload_ids": [uid],
             "source_hashes": [sha]},
        )
        with self.lock:
            self.last_cursor_id = c_entry["id"]

        hard_reject = any(
            it.level == "error" and it.code in HARD_REJECT_CODES for it in worst.issues
        )
        return {
            "existing": False,
            "frames": len(frames),
            "canvas_size": self.canvas_size,
            "hotspot": list(self.hotspot),
            "verdict": quality["verdict"],
            "score": quality["score"],
            "issues": self.issues,
            "removal_confidence": quality["removal_confidence"],
            "auto_apply": quality["auto_apply"],
            "removal_diagnostics": quality["removal_diagnostics"],
            "src_size": list(self.src_size),
            "crop_box": list(crop_box) if crop_box else None,
            "previews": self.previews(),
            "hard_reject": hard_reject,
            "cursor_id": c_entry["id"],
            "cursor_name": c_entry["name"],
            "data_dir": self.store.root,
        }

    def previews(self) -> list[str]:
        """当前暂存帧的 base64 预览（每帧 4x 放大 PNG）。"""
        with self.lock:
            return self._previews_unlocked()

    def _previews_unlocked(self) -> list[str]:
        out = []
        for f in self.frames:
            big = f.resize((f.width * 4, f.height * 4), Image.NEAREST)
            buf = io.BytesIO()
            big.save(buf, format="PNG")
            out.append("data:image/png;base64,"
                       + base64.b64encode(buf.getvalue()).decode())
        return out

    def set_size(self, size: int) -> dict:
        """调整光标画布尺寸；已有帧和热点等比缩放；启用中立即重新生效。"""
        if size not in SIZE_CHOICES:
            raise ValueError(f"size 必须是 {'/'.join(map(str, SIZE_CHOICES))} 之一")
        with self.lock:
            old = self.canvas_size
            self.canvas_size = size
            if self.frames and size != old:
                scale = size / old
                self.frames = [
                    _resize_rgba_premultiplied(f, (size, size)) for f in self.frames
                ]
                self.hotspot = (
                    min(size - 1, round(self.hotspot[0] * scale)),
                    min(size - 1, round(self.hotspot[1] * scale)),
                )
            if self.enabled:
                self.mgr.replace_cursor(self._rebuild_ani())
            return {
                "canvas_size": size,
                "hotspot": list(self.hotspot),
                "enabled": self.enabled,
                "previews": self._previews_unlocked(),
            }

    def rotate(self, direction: str) -> dict:
        """将所有暂存帧旋转 90°；热点同步变换；启用中立即重新生效。

        Args:
            direction: "ccw"=逆时针 90° / "cw"=顺时针 90°
        """
        with self.lock:
            if not self.frames:
                raise ValueError("还没有可用图片，请先上传")
            hx, hy = self.hotspot
            n = self.canvas_size - 1
            if direction == "ccw":
                self.frames = [f.transpose(Image.Transpose.ROTATE_90) for f in self.frames]
                self.hotspot = (n - hy, hx)
            elif direction == "cw":
                self.frames = [f.transpose(Image.Transpose.ROTATE_270) for f in self.frames]
                self.hotspot = (hy, n - hx)
            else:
                raise ValueError("direction 必须是 'ccw' 或 'cw'")
            if self.enabled:
                self.mgr.replace_cursor(self._rebuild_ani())
            return {
                "hotspot": list(self.hotspot),
                "enabled": self.enabled,
                "previews": self._previews_unlocked(),
            }

    @staticmethod
    def _fit_to_canvas(img: Image.Image, size: int, crop_box=None) -> Image.Image:
        if crop_box is not None:
            img = img.crop(crop_box)
        scale = min(size / img.width, size / img.height)
        if crop_box is None:
            scale = min(scale, 1.0)   # 未裁剪时保持"小图不放大"
        new_w = max(1, round(img.width * scale))
        new_h = max(1, round(img.height * scale))
        scaled = _resize_rgba_premultiplied(img, (new_w, new_h))
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        # paste(..., mask=scaled) 会把半透明边缘的 alpha 再乘一次；alpha_composite
        # 只应用一次源 alpha，避免细腿/毛发在最终 48/64/96px 画布上继续变淡。
        canvas.alpha_composite(scaled, dest=((size - new_w) // 2, (size - new_h) // 2))
        # 最终尺寸受控羽化: 48→0.5px, 64→0.75px, 96→1px（预乘 + max, 不减弱细部）
        radius = 0.5 if size <= 48 else 0.75 if size <= 64 else 1.0
        return _feather_alpha_additive(canvas, radius)

    # ── 生成 / 应用 / 恢复 ─────────────────────────────
    def _rebuild_ani(self) -> str:
        """写入临时 .ani，返回路径。"""
        import tempfile
        os.makedirs(os.path.join(tempfile.gettempdir(), "custom_cursor_web"), exist_ok=True)
        ani_path = os.path.join(tempfile.gettempdir(), "custom_cursor_web", "custom.ani")
        durations = (self.durations if len(self.durations) == len(self.frames)
                     else [FRAME_MS] * len(self.frames))
        save_ani_file(
            ani_path,
            frames=self.frames,
            hotspots=[self.hotspot] * len(self.frames),
            frame_durations_ms=durations,
            title="Custom Cursor",
            author="Custom Cursor Web",
        )
        return ani_path

    def apply(self) -> bool:
        with self.lock:
            if not self.frames:
                raise ValueError("还没有可用图片，请先上传")
            ani_path = self._rebuild_ani()
            self.mgr.replace_cursor(ani_path)
            self.enabled = True
            return True

    def restore(self) -> bool:
        with self.lock:
            self.mgr.restore_original()
            self.enabled = False
            return False

    def set_hotspot(self, x: int, y: int) -> bool:
        with self.lock:
            n = self.canvas_size - 1
            self.hotspot = (max(0, min(n, x)), max(0, min(n, y)))
            if self.enabled and self.frames:
                self.mgr.replace_cursor(self._rebuild_ani())
            return self.enabled

    def state(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "canvas_size": self.canvas_size,
                "frames": len(self.frames),
                "hotspot": list(self.hotspot),
                "verdict": self.verdict,
                "score": self.score,
                "issues": self.issues,
                "removal_confidence": self.removal_confidence,
                "auto_apply": self.auto_apply_allowed,
                "removal_diagnostics": self.removal_diagnostics,
                "src_size": list(self.src_size) if self.src_size else None,
                "data_dir": self.store.root,
                "last_cursor_id": self.last_cursor_id,
                "last_cursor_name": (self.store.index["cursors"].get(self.last_cursor_id or "", {})
                                     .get("name")),
            }

    # ── 图库 ──────────────────────────────────────────
    def gallery(self) -> dict:
        return {"uploads": self.store.list_uploads(),
                "cursors": self.store.list_cursors()}

    def gallery_file_path(self, kind: str, uid: str, what: str = "file") -> str | None:
        """返回图库条目关联文件的绝对路径（用于下载/预览）。

        kind: uploads | cursors;  what: file(原文件) | thumb(缩略图) | preview(预览)
        """
        if kind == "uploads":
            entry = self.store.get_upload(uid)
            if not entry:
                return None
            rel = entry["filename"] if what == "file" else entry["thumb"]
            return os.path.join(self.store.root, rel)
        if kind == "cursors":
            entry = self.store.get_cursor(uid)
            if not entry:
                return None
            if what == "preview":
                return os.path.join(self.store.cursors_dir, uid, "preview.png")
            if what == "file":
                return os.path.join(self.store.cursors_dir, uid, "ani")
        return None

    def apply_cursor_from_gallery(self, cid: str) -> dict:
        """应用图库中保存的光标：恢复其工作状态并替换系统光标。"""
        with self.lock:
            entry = self.store.get_cursor(cid)
            if not entry:
                raise KeyError("光标不存在")
            ani_path = self.store.cursor_ani_path(cid)
            frames = self.store.cursor_frames(cid)
            if not ani_path or not frames:
                raise ValueError("光标文件缺失")
            self.frames = frames
            self.canvas_size = entry["size"]
            self.hotspot = tuple(entry["hotspot"])
            self.durations = [FRAME_MS] * len(frames)
            self.src_size = (frames[0].width, frames[0].height)
            self.verdict = self.score = None
            self.issues = []
            self.removal_confidence = "high"
            self.auto_apply_allowed = True
            self.removal_diagnostics = None
            self.last_cursor_id = cid
            self.mgr.replace_cursor(ani_path)
            self.enabled = True
            return {"enabled": True, "name": entry["name"], "cid": cid,
                    "canvas_size": self.canvas_size,
                    "hotspot": list(self.hotspot),
                    "frames": len(frames)}

    def delete_gallery(self, kind: str, uid: str,
                       with_uploads: bool = False,
                       with_cursors: bool = False) -> dict:
        """删除图库条目；可选级联删除关联条目。

        删除光标且 with_uploads=True 时，仅删除"独占"图片
        （不被其他光标引用的关联图片），共享图片保留。
        删除图片且 with_cursors=True 时，删除所有引用它的光标。

        若删除的光标是当前工作/应用中的光标：恢复系统光标并清空工作状态。
        """
        with self.lock:
            deleting_cursors: list[str] = []  # 将被删除的光标 id
            if kind == "cursors":
                entry = self.store.get_cursor(uid)
                if not entry:
                    raise KeyError("光标不存在")
                deleting_cursors.append(uid)
                deleted_uploads = []
                if with_uploads:
                    for up_id in entry.get("upload_ids", []):
                        up = self.store.get_upload(up_id)
                        if up and not self.store.cursors_for_upload(up_id, exclude=uid):
                            self.store.delete("uploads", up_id)
                            deleted_uploads.append(up_id)
                self.store.delete("cursors", uid)
                result = {"deleted": True, "uploads_deleted": deleted_uploads}
            elif kind == "uploads":
                if not self.store.get_upload(uid):
                    raise KeyError("图片不存在")
                deleted_cursors = []
                if with_cursors:
                    for c in self.store.cursors_for_upload(uid):
                        self.store.delete("cursors", c["id"])
                        deleted_cursors.append(c["id"])
                deleting_cursors.extend(deleted_cursors)
                self.store.delete("uploads", uid)
                result = {"deleted": True, "cursors_deleted": deleted_cursors}
            else:
                raise KeyError("未知类型")

            # 正在应用/工作中的光标被删除 → 恢复系统光标 + 清空工作状态
            if self.last_cursor_id and self.last_cursor_id in deleting_cursors:
                was_enabled = self.enabled
                self._reset_work_state()
                if was_enabled:
                    self.mgr.restore_original()
                result["restored"] = was_enabled
            else:
                result["restored"] = False
            return result

    def _reset_work_state(self) -> None:
        """清空工作状态（帧/分析/应用状态），保留画布尺寸选择。"""
        self.frames = []
        self.durations = []
        self.hotspot = (self.canvas_size // 2, self.canvas_size // 2)
        self.enabled = False
        self.verdict = None
        self.score = None
        self.issues = []
        self.removal_confidence = None
        self.auto_apply_allowed = False
        self.removal_diagnostics = None
        self.src_size = None
        self.last_cursor_id = None

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
            if path == "/api/gallery":
                self._send_json(app.gallery())
                return
            if path.startswith("/api/gallery/"):
                self._serve_gallery_file(path)
                return
            if path.startswith("/api/"):
                self._send_error(404, f"未知接口: {path}")
                return
            self._serve_static(path)

        def _serve_gallery_file(self, path: str):
            """GET /api/gallery/<kind>/<id>/<what>  (what: file|thumb|preview)"""
            parts = path.split("/")
            # ["", "api", "gallery", kind, uid, what]
            if len(parts) < 6:
                self._send_error(404, "资源不存在")
                return
            kind, uid, what = parts[3], parts[4], parts[5]
            real = app.gallery_file_path(kind, uid, what)
            if not real or not os.path.isfile(real):
                self._send_error(404, "资源不存在")
                return
            ext = os.path.splitext(real)[1].lower()
            with open(real, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

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
                elif path == "/api/rotate":
                    length = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(length) or b"{}")
                    self._send_json(app.rotate(str(data.get("direction", ""))))
                elif path == "/api/size":
                    length = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(length) or b"{}")
                    self._send_json(app.set_size(int(data.get("size", DEFAULT_CANVAS_SIZE))))
                elif path.startswith("/api/gallery/"):
                    self._handle_gallery_action(path)
                elif path == "/api/quit":
                    self._send_json({"bye": True})
                    threading.Thread(target=quit_callback, daemon=True).start()
                else:
                    self._send_error(404, f"未知接口: {path}")
            except ValueError as e:
                self._send_error(400, str(e))
            except Exception as e:
                self._send_error(500, str(e))

        def _handle_gallery_action(self, path: str):
            """POST /api/gallery/<kind>/<id>/<action>  (action: apply|rename|category|delete)"""
            parts = path.split("/")
            if len(parts) < 6:
                self._send_error(404, "资源不存在")
                return
            kind = parts[3]          # uploads | cursors
            uid = parts[4]
            action = parts[5]
            body = b""
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                body = self.rfile.read(length)
            data = json.loads(body or b"{}")

            try:
                if kind == "cursors" and action == "apply":
                    self._send_json(app.apply_cursor_from_gallery(uid))
                elif kind == "cursors" and action == "rename":
                    self._send_json(app.store.rename_cursor(uid, str(data.get("name", ""))))
                elif kind == "uploads" and action == "generate":
                    self._send_json(app.generate_from_upload(uid))
                elif action == "category":
                    self._send_json(app.store.set_category(kind, uid,
                                                           str(data.get("category", ""))))
                elif action == "delete":
                    self._send_json(app.delete_gallery(
                        kind, uid,
                        with_uploads=bool(data.get("with_uploads")),
                        with_cursors=bool(data.get("with_cursors"))))
                else:
                    self._send_error(404, "未知操作")
            except KeyError as e:
                self._send_error(404, str(e))

        def _handle_upload(self):
            # 手写解析 Content-Type（不依赖 cgi 模块：Python 3.13 已移除）
            raw_ct = self.headers.get("Content-Type", "")
            parts = [p.strip() for p in raw_ct.split(";")]
            ctype = parts[0].lower()
            pdict = {}
            for p in parts[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    pdict[k.strip().lower()] = v.strip().strip('"')
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
                # 头部用 latin-1 解码是字节无损的（每个字节映射到同码点），
                # 因此可借此恢复 filename 的原始字节再正确解码（UTF-8/GBK）。
                headers = part[:header_end].decode("latin-1", "replace")
                body = part[header_end + 4:]
                if body.endswith(b"\r\n"):
                    body = body[:-2]
                name = None
                name_star = None
                for line in headers.split("\r\n"):
                    if line.lower().startswith("content-disposition"):
                        for seg in line.split(";"):
                            seg = seg.strip()
                            low = seg.lower()
                            if low.startswith("filename*="):
                                # RFC 5987: filename*=UTF-8''%E5%9B%BE%E7%89%87.png
                                try:
                                    parts_ = seg.split("=", 1)[1].split("'", 2)
                                    if len(parts_) == 3:
                                        name_star = unquote(
                                            parts_[2],
                                            encoding=parts_[0] or "utf-8",
                                            errors="replace")
                                except (ValueError, IndexError, LookupError):
                                    pass
                            elif low.startswith("filename="):
                                raw_val = seg[9:].strip('"').encode("latin-1", "replace")
                                name = _decode_filename(raw_val)
                # 按 RFC 5987，filename* 优先于 filename
                if name_star:
                    name = name_star
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
