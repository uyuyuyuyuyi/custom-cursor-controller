"""onnx_cutout.py — 可选 ONNX 智能抠图（懒下载 + 懒加载）。

设计目标:
  - 体积策略: 模型文件绝不打进 exe。首次使用才下载到用户数据目录,
    下载时校验 SHA-256, 失败自动清理残缺文件。
  - 商用安全: 默认模型 ormbg (Apache-2.0, 可商用); 不使用 BRIA RMBG
    (CC BY-NC 4.0, 仅限非商业)。BiRefNet 官方权重为 MIT, 但 ONNX 导出
    体积过大(数百 MB), 暂不作为默认, 保留扩展位。
  - 可选依赖: onnxruntime / numpy 只在推理时 import; 未安装时所有接口
    优雅降级, 不阻塞启发式抠图主流程。

用法:
    seg = LazyOnnxSegmenter()
    seg.require_runtime()          # 未安装 onnxruntime 时抛 OnnxNotInstalledError
    seg.download(progress_cb=...)  # 首次使用: 下载并校验模型
    mask = seg.segment(img)        # 返回与原图同尺寸的 L 掩码 (0~255)
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops

MODEL_ID = "ormbg-1024"
MODEL_NAME = "ormbg (Open Remove Background Model)"
MODEL_LICENSE = "apache-2.0"
MODEL_LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
MODEL_HOMEPAGE = "https://huggingface.co/schirrmacher/ormbg"
MODEL_PREPROCESS_SIZE = 1024

# 变体按优先级排列: 默认 fp16(88MB) 兼顾精度与体积, 失败时依次回退
# fp32(176MB, 兼容性最好) / uint8(44MB, 最快最小)。
# 下载接口默认下载第一个 (fp16); 加载时若当前变体缺失或失败,
# 会自动尝试下载并加载下一个变体。
MODEL_BASE_URL = "https://huggingface.co/onnx-community/ormbg-ONNX/resolve/main/"
MODEL_VARIANTS: list[dict] = [
    {
        "name": "fp16",
        "file": "onnx/model_fp16.onnx",
        "size": 88_117_930,
        "sha256": "06e4236d2c2fae771f56b6e4723b6ed870a554b661ea4da4a74f650bbd53cf57",
    },
    {
        "name": "fp32",
        "file": "onnx/model.onnx",
        "size": 176_116_019,
        "sha256": "2830b6f461809cdc7e2f15d19fc1a898b14ad53504640d4147b525d53ebb09c9",
    },
    {
        "name": "uint8",
        "file": "onnx/model_uint8.onnx",
        "size": 44_315_205,
        "sha256": "ffbcae62a7b675d616e64cb392ee028786c4cf74f83596590fba13733ef00171",
    },
]


class OnnxNotInstalledError(RuntimeError):
    """onnxruntime / numpy 未安装，AI 抠图不可用。"""


class ModelDownloadError(RuntimeError):
    """模型下载失败（网络、校验和等）。"""


class ModelLoadError(RuntimeError):
    """模型文件存在但无法被 onnxruntime 加载。"""


def default_model_dir() -> Path:
    """模型存放目录: 用户级可写目录，绝不进程序安装目录/exe。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "custom-cursor-controller" / "models"


def onnx_installed() -> tuple[bool, str | None]:
    """返回 (是否安装, 错误说明)。"""
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:  # pragma: no cover - 取决于运行环境
        return False, f"onnxruntime 不可用: {e}"
    try:
        import numpy  # noqa: F401
    except Exception as e:  # pragma: no cover
        return False, f"numpy 不可用: {e}"
    return True, None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def composite_alpha(img: Image.Image, mask: Image.Image) -> Image.Image:
    """把 AI 掩码合成到 RGBA 图: 已有透明区域保留 (取 min)。

    与启发式路径的 keep_existing_alpha 语义一致，避免 GIF 动画帧
    闪现背景色块。
    """
    img = img.convert("RGBA")
    mask = mask.convert("L")
    if mask.size != img.size:
        mask = mask.resize(img.size, Image.Resampling.BILINEAR)
    out = img.copy()
    if img.getchannel("A").getextrema()[0] < 250:
        out.putalpha(ImageChops.darker(img.getchannel("A"), mask))
    else:
        out.putalpha(mask)
    return out


class LazyOnnxSegmenter:
    """线程安全的懒加载 ONNX 分割器。

    - 构造时不加载任何模型；
    - 首次 segment() 才创建 InferenceSession；
    - 模型文件首次使用时由 download() 下载并校验；
    - 全程不依赖全局状态，方便测试注入 fake session。
    """

    def __init__(self, models_dir: str | Path | None = None):
        self._dir = Path(models_dir) if models_dir else default_model_dir()
        self._session = None
        self._loaded_variant: str | None = None
        # RLock: segment() 持锁调用 _ensure_loaded()，后者也会加同一把锁
        self._lock = threading.RLock()

    # ── 状态 ──────────────────────────────────────────
    def model_path(self) -> Path | None:
        for variant in MODEL_VARIANTS:
            p = self._dir / variant["file"]
            if p.is_file():
                return p
        return None

    def status(self) -> dict:
        installed, err = onnx_installed()
        default_path = self._dir / MODEL_VARIANTS[0]["file"]
        return {
            "onnx_installed": installed,
            "onnx_error": err,
            "model": {
                "id": MODEL_ID,
                "name": MODEL_NAME,
                "license": MODEL_LICENSE,
                "license_url": MODEL_LICENSE_URL,
                "homepage": MODEL_HOMEPAGE,
                "size_mb": round(MODEL_VARIANTS[0]["size"] / (1024 * 1024), 1),
                "variant": MODEL_VARIANTS[0]["name"],
                "downloaded": default_path.is_file(),
                "loaded": self._session is not None,
            },
        }

    @staticmethod
    def require_runtime() -> None:
        """未安装 onnxruntime/numpy 时抛出 OnnxNotInstalledError。"""
        installed, err = onnx_installed()
        if not installed:
            raise OnnxNotInstalledError(
                err or "未安装 AI 依赖，请执行: pip install -r requirements-ai.txt")

    # ── 下载 ──────────────────────────────────────────
    def download(
        self,
        variant_name: str | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> Path:
        """下载指定变体（默认 fp16）；已存在则直接返回。失败时清理 .part 文件。"""
        with self._lock:
            if variant_name is None:
                variant = MODEL_VARIANTS[0]
            else:
                variant = next(
                    (v for v in MODEL_VARIANTS if v["name"] == variant_name),
                    None)
                if variant is None:
                    raise ModelDownloadError(f"未知模型变体: {variant_name}")
            target = self._dir / variant["file"]
            if target.is_file():
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_suffix(target.suffix + ".part")
            url = MODEL_BASE_URL + variant["file"]
            req = urllib.request.Request(
                url, headers={"User-Agent": "custom-cursor-controller/1.0"})
            h = hashlib.sha256()
            done = 0
            try:
                with urllib.request.urlopen(req, timeout=120) as resp, \
                        open(part, "wb") as f:
                    while chunk := resp.read(1 << 16):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if progress_cb is not None:
                            progress_cb(min(1.0, done / variant["size"]),
                                        f"下载模型 {done / variant['size']:.0%}")
                digest = h.hexdigest()
                if digest != variant["sha256"]:
                    raise ModelDownloadError(
                        f"模型校验失败: 期望 {variant['sha256']}，实际 {digest}")
                os.replace(part, target)
                return target
            except Exception:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    # ── 加载与推理 ────────────────────────────────────
    def _ensure_loaded(self) -> None:
        self.require_runtime()
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            last_error: str | None = None
            for variant in MODEL_VARIANTS:
                p = self._dir / variant["file"]
                if not p.is_file():
                    try:
                        self.download(variant_name=variant["name"])
                    except Exception as e:
                        last_error = f"{variant['name']} 下载失败: {e}"
                        continue
                    p = self._dir / variant["file"]
                    if not p.is_file():
                        continue
                try:
                    self._session = ort.InferenceSession(
                        str(p), providers=["CPUExecutionProvider"])
                    self._loaded_variant = variant["name"]
                    return
                except Exception as e:  # 该变体不可用，尝试下一个
                    last_error = f"{variant['name']}: {e}"
            if last_error is None:
                raise ModelDownloadError(
                    "模型尚未下载，请先调用 download()。")
            raise ModelLoadError(
                f"所有模型变体均不可用: {last_error}")

    def segment(self, img: Image.Image) -> Image.Image | None:
        """对图片推理并返回同尺寸 L 掩码；失败时返回 None（调用方回退）。"""
        try:
            with self._lock:
                self._ensure_loaded()
                import numpy as np
                session = self._session
                if session is None:  # pragma: no cover - _ensure_loaded 已保证
                    return None
                size = MODEL_PREPROCESS_SIZE
                rgb = img.convert("RGB").resize(
                    (size, size), Image.Resampling.BILINEAR)
                arr = np.asarray(rgb, dtype=np.float32) / 255.0
                blob = arr.transpose(2, 0, 1)[None]
                in_name = session.get_inputs()[0].name
                out_name = session.get_outputs()[0].name
                raw = session.run([out_name], {in_name: blob})[0]
                mask = np.squeeze(raw)
                if mask.ndim == 3:
                    mask = mask[0]
                lo = float(mask.min())
                hi = float(mask.max())
                if lo < 0.0 or hi > 1.0:  # 输出是 logits → sigmoid
                    mask = 1.0 / (1.0 + np.exp(-mask))
                mask = np.clip(mask, 0.0, 1.0)
                out = Image.fromarray(
                    (mask * 255.0 + 0.5).astype(np.uint8), "L")
                if out.size != img.size:
                    out = out.resize(img.size, Image.Resampling.BILINEAR)
                return out
        except Exception:
            # 任何推理失败都不应拖垮主流程；返回 None 由调用方降级。
            return None
