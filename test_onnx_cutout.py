"""onnx_cutout 单元测试：模型元数据、假会话推理、alpha 合成与降级路径。

不访问网络，不替换系统光标。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import onnx_cutout
from onnx_cutout import (
    LazyOnnxSegmenter,
    OnnxNotInstalledError,
    composite_alpha,
)

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:  # pragma: no cover
    HAS_NUMPY = False


class FakeSession:
    """最小可用的 onnxruntime 会话替身，返回常量 logits/概率图。"""

    def __init__(self, value: float, size: int = 1024):
        self._value = float(value)
        self._size = size

    def get_inputs(self):
        return [SimpleNamespace(name="image")]

    def get_outputs(self):
        return [SimpleNamespace(name="output")]

    def run(self, out_names, feeds):
        return [np.full((1, 1, self._size, self._size),
                        self._value, dtype=np.float32)]


class OnnxModelMetaTests(unittest.TestCase):
    def test_default_model_is_commercial_safe(self):
        self.assertEqual(onnx_cutout.MODEL_LICENSE, "apache-2.0")
        # 不使用 BRIA RMBG（CC BY-NC 4.0 非商用）
        self.assertNotIn("briaai", onnx_cutout.MODEL_BASE_URL.lower())
        self.assertNotIn("bria", onnx_cutout.MODEL_BASE_URL.lower())

    def test_variants_default_fp16_with_fallbacks(self):
        names = [v["name"] for v in onnx_cutout.MODEL_VARIANTS]
        self.assertEqual(names[0], "fp16")          # 默认精度优先
        self.assertIn("fp32", names)                # 兼容性回退
        self.assertIn("uint8", names)               # 最小/最快回退
        for variant in onnx_cutout.MODEL_VARIANTS:
            self.assertTrue(variant["sha256"], variant["name"])
            self.assertGreater(variant["size"], 0)
            self.assertTrue(variant["file"].endswith(".onnx"))
        self.assertLess(onnx_cutout.MODEL_VARIANTS[0]["size"],
                        onnx_cutout.MODEL_VARIANTS[1]["size"])
        self.assertLess(onnx_cutout.MODEL_VARIANTS[2]["size"],
                        onnx_cutout.MODEL_VARIANTS[0]["size"])


@unittest.skipUnless(HAS_NUMPY, "需要 numpy")
class LazySegmenterTests(unittest.TestCase):
    def _segmenter_with_fake_session(self, value: float) -> LazyOnnxSegmenter:
        with tempfile.TemporaryDirectory() as td:
            seg = LazyOnnxSegmenter(Path(td))
            seg._session = FakeSession(value)
            seg._loaded_variant = "fake"
            # 不依赖真实 onnxruntime：只验证锁可重入 + 预处理/后处理路径
            seg.require_runtime = lambda: None
            img = Image.new("RGBA", (96, 64), (120, 60, 30, 255))
            return seg, img

    def test_logits_output_maps_to_mask(self):
        seg, img = self._segmenter_with_fake_session(5.0)
        mask = seg.segment(img)
        self.assertIsNotNone(mask)
        self.assertEqual(mask.size, img.size)
        self.assertEqual(mask.mode, "L")
        extrema = mask.getextrema()
        self.assertGreater(extrema[0], 200)   # sigmoid(5) ≈ 0.993

    def test_probability_output_passes_through(self):
        seg, img = self._segmenter_with_fake_session(0.3)
        mask = seg.segment(img)
        self.assertIsNotNone(mask)
        value = mask.getpixel((10, 10))
        self.assertAlmostEqual(value, 0.3 * 255, delta=3)

    def test_negative_logits_map_to_background(self):
        seg, img = self._segmenter_with_fake_session(-5.0)
        mask = seg.segment(img)
        self.assertIsNotNone(mask)
        self.assertLess(mask.getextrema()[1], 60)   # sigmoid(-5) ≈ 0.007

    def test_inference_failure_returns_none(self):
        seg, img = self._segmenter_with_fake_session(0.0)
        seg._session = None  # 无会话 → 加载路径失败 → 返回 None

        def _boom():
            raise onnx_cutout.ModelDownloadError("no model available")

        seg._ensure_loaded = _boom  # 受控失败，避免测试触发真实下载
        self.assertIsNone(seg.segment(img))

    def test_status_reports_model_state(self):
        with tempfile.TemporaryDirectory() as td:
            seg = LazyOnnxSegmenter(Path(td))
            with mock.patch.object(onnx_cutout, "onnx_installed",
                                   return_value=(True, None)):
                status = seg.status()
                self.assertTrue(status["onnx_installed"])
                self.assertFalse(status["model"]["downloaded"])
                self.assertEqual(status["model"]["license"], "apache-2.0")

    def test_require_runtime_raises_when_missing(self):
        seg = LazyOnnxSegmenter()
        with mock.patch.object(onnx_cutout, "onnx_installed",
                               return_value=(False, "no onnxruntime")):
            with self.assertRaises(OnnxNotInstalledError):
                seg.require_runtime()


class DownloadAndCompositeTests(unittest.TestCase):
    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int = -1) -> bytes:
            if not self._payload:
                return b""
            chunk = self._payload[:size]
            self._payload = self._payload[size:]
            return chunk

    def test_download_skips_existing_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / onnx_cutout.MODEL_VARIANTS[0]["file"]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"fake-model")
            seg = LazyOnnxSegmenter(root)
            got = seg.download()
            self.assertEqual(got, target)

    def test_download_creates_nested_dirs_and_verifies_hash(self):
        payload = b"fake-model-content"
        fake_variant = {
            "name": "uint8",
            "file": "onnx/model_uint8.onnx",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(onnx_cutout, "MODEL_VARIANTS", [fake_variant]), \
                    mock.patch.object(onnx_cutout.urllib.request, "urlopen",
                                      return_value=self._FakeResp(payload)):
                seg = LazyOnnxSegmenter(root)
                got = seg.download(variant_name="uint8")
            self.assertEqual(got.read_bytes(), payload)

    def test_sha256_file_helper(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.bin"
            p.write_bytes(b"hello")
            self.assertEqual(
                onnx_cutout._sha256_file(p),
                hashlib.sha256(b"hello").hexdigest(),
            )

    def test_composite_alpha_keeps_existing_transparency(self):
        img = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
        img.putpixel((0, 0), (255, 0, 0, 0))          # 原有透明点
        mask = Image.new("L", (8, 8), 128)            # 半透明 AI 掩码
        out = composite_alpha(img, mask)
        self.assertEqual(out.getpixel((0, 0))[3], 0)  # min(0, 128) = 0
        self.assertEqual(out.getpixel((1, 1))[3], 128)

    def test_composite_alpha_without_existing_alpha(self):
        img = Image.new("RGB", (4, 4), (10, 20, 30))
        mask = Image.new("L", (4, 4), 255)
        out = composite_alpha(img, mask)
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((0, 0))[3], 255)


if __name__ == "__main__":
    unittest.main()
