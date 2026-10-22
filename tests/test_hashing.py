"""指纹与压缩的单元测试：python -m unittest discover -s tests -v"""

from __future__ import annotations

import io
import random
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hashing import dhash, hamming, prepare_upload  # noqa: E402


def _sample(seed: int, size: tuple[int, int] = (768, 768)) -> bytes:
    """生成一张低频结构图，用来模拟真实照片而非随机噪声。"""
    rnd = random.Random(seed)
    image = Image.new("RGB", size, (18, 20, 28))
    draw = ImageDraw.Draw(image)
    width, height = size
    for y in range(height):
        shade = int(30 + 180 * y / height)
        draw.line([(0, y), (width, y)], fill=(shade, shade // 2 + 20, 200 - shade // 3))
    for _ in range(14):
        x0 = rnd.randint(0, width - 80)
        y0 = rnd.randint(0, height - 80)
        x1 = x0 + rnd.randint(60, 260)
        y1 = y0 + rnd.randint(60, 260)
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        if rnd.random() < 0.5:
            draw.rectangle([x0, y0, x1, y1], fill=color)
        else:
            draw.ellipse([x0, y0, x1, y1], fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(raw: bytes, quality: int) -> bytes:
    with Image.open(io.BytesIO(raw)) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()


def _resize(raw: bytes, ratio: float) -> bytes:
    with Image.open(io.BytesIO(raw)) as image:
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        buffer = io.BytesIO()
        image.resize(size, Image.Resampling.LANCZOS).save(buffer, format="PNG")
        return buffer.getvalue()


def _crop(raw: bytes, ratio: float) -> bytes:
    with Image.open(io.BytesIO(raw)) as image:
        left = int(image.width * (1 - ratio) / 2)
        top = int(image.height * (1 - ratio) / 2)
        box = (left, top, image.width - left, image.height - top)
        buffer = io.BytesIO()
        image.crop(box).save(buffer, format="PNG")
        return buffer.getvalue()


class DHashingTest(unittest.TestCase):
    """同一张图的缩放、重压不应改变指纹；不同图应当明显拉开。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.original = _sample(1)
        cls.other = _sample(2)
        cls.hash = dhash(cls.original)
        assert cls.hash is not None

    def test_identical(self) -> None:
        self.assertEqual(hamming(self.hash, dhash(self.original) or 0), 0)

    def test_resize_keeps_hash(self) -> None:
        for ratio in (0.25, 0.5, 0.75):
            distance = hamming(self.hash, dhash(_resize(self.original, ratio)) or 0)
            self.assertLessEqual(distance, 2, f"缩放 {ratio} 后距离 {distance}，阈值应 ≤2")

    def test_jpeg_recompress_keeps_hash(self) -> None:
        for quality in (50, 70, 90):
            distance = hamming(self.hash, dhash(_jpeg(self.original, quality)) or 0)
            self.assertLessEqual(distance, 2, f"JPEG {quality} 后距离 {distance}，阈值应 ≤2")

    def test_different_images_far_apart(self) -> None:
        distance = hamming(self.hash, dhash(self.other) or 0)
        self.assertGreater(distance, 20, f"不同图距离只有 {distance}，指纹区分度不足")

    def test_crop_is_not_silently_matched(self) -> None:
        """裁剪版应当被判定为「疑似」而不是直接匹配。"""
        distance = hamming(self.hash, dhash(_crop(self.original, 0.7)) or 0)
        self.assertGreater(distance, 0, "裁剪后距离仍为 0，说明指纹过于宽松")

    def test_undecodable_returns_none(self) -> None:
        self.assertIsNone(dhash(b"not an image"))


class PrepareUploadTest(unittest.TestCase):
    """上传前压缩：必须小于接口上限，且仍可解码。"""

    def test_large_image_is_compressed(self) -> None:
        raw = _sample(3, size=(2600, 1800))
        compact, mime = prepare_upload(raw, limit=480 * 1024)
        self.assertLessEqual(len(compact), 480 * 1024)
        self.assertIn(mime, {"image/jpeg", "image/png", "image/webp"})
        with Image.open(io.BytesIO(compact)) as image:
            self.assertGreater(image.width, 0)

    def test_oversized_image_reencoded(self) -> None:
        """把上限压到很小，确认输出一定落在上限以内。"""
        raw = _sample(5, size=(1600, 1200))
        compact, _mime = prepare_upload(raw, limit=60 * 1024)
        self.assertLessEqual(len(compact), 60 * 1024)

    def test_small_image_untouched(self) -> None:
        raw = _sample(4, size=(320, 320))
        compact, _mime = prepare_upload(raw, limit=480 * 1024)
        self.assertEqual(compact, raw)


if __name__ == "__main__":
    unittest.main()
