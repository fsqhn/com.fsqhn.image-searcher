"""引擎结果解析的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.engines import (  # noqa: E402
    Candidate,
    LensEngine,
    to_yandex_candidate,
    verify_candidate,
)


class YandexCandidateTest(unittest.TestCase):
    """Yandex 必须拿站点原图比对，缩略图是裁过的方框，比不出东西。"""

    def test_prefers_original_image(self) -> None:
        site = {
            "title": "foo on X",
            "url": "https://x.com/foo/status/1",
            "domain": "x.com",
            "thumb": {"url": "//avatars.mds.yandex.net/i?id=1&w=296&h=180"},
            "originalImage": {"url": "https://pbs.twimg.com/media/abc.jpg"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://pbs.twimg.com/media/abc.jpg")
        self.assertEqual(candidate.note, "原图比对")

    def test_protocol_relative_original(self) -> None:
        site = {
            "url": "https://weibo.com/1/2",
            "originalImage": {"url": "//wx1.sinaimg.cn/large/abc.jpg"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://wx1.sinaimg.cn/large/abc.jpg")

    def test_original_keeps_thumb_as_fallback(self) -> None:
        site = {
            "url": "https://x.com/foo/status/1",
            "thumb": {"url": "//avatars.mds.yandex.net/i?id=1"},
            "originalImage": {"url": "https://pbs.twimg.com/media/abc.jpg"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://pbs.twimg.com/media/abc.jpg")
        self.assertEqual(candidate.fallback_image, "https://avatars.mds.yandex.net/i?id=1")

    def test_falls_back_to_thumb(self) -> None:
        site = {
            "url": "https://example.com/a",
            "thumb": {"url": "//avatars.mds.yandex.net/i?id=2&w=296&h=180"},
        }
        candidate = to_yandex_candidate(site, "yandex")
        self.assertEqual(candidate.image_url, "https://avatars.mds.yandex.net/i?id=2&w=296&h=180")
        self.assertEqual(candidate.fallback_image, "")
        self.assertEqual(candidate.note, "缩略图比对")


class VerifyFallbackTest(unittest.TestCase):
    """原图下不下来时，用缩略图补一次，别直接判「未验证」。"""

    def _png(self) -> bytes:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (32, 32), (200, 120, 60)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_retries_with_fallback_image(self) -> None:
        from unittest.mock import patch

        from core.hashing import dhash
        from core.httpclient import HttpError

        raw = self._png()
        target = dhash(raw)
        candidate = Candidate(
            engine="yandex",
            url="https://x.com/foo/status/1",
            image_url="https://cdn.donmai.us/original.jpg",
            fallback_image="https://avatars.mds.yandex.net/i?id=1",
        )

        with patch("core.engines.download_image_multi") as downloader:
            downloader.side_effect = [HttpError("403 拒绝"), raw]
            result = verify_candidate(candidate, target, 8, "", 5)

        self.assertEqual(downloader.call_count, 2)
        self.assertEqual(result.status, "match")
        self.assertEqual(result.distance, 0)

    def test_both_routes_fail_marks_unverified(self) -> None:
        from unittest.mock import patch

        from core.httpclient import HttpError

        candidate = Candidate(
            engine="yandex",
            url="https://x.com/foo/status/1",
            image_url="https://cdn.donmai.us/original.jpg",
            fallback_image="https://avatars.mds.yandex.net/i?id=1",
        )

        with patch("core.engines.download_image_multi", side_effect=HttpError("403")):
            result = verify_candidate(candidate, 12345, 8, "", 5)

        self.assertEqual(result.status, "unverified")
        self.assertIn("下载失败", result.note)


class LensRetryTest(unittest.TestCase):
    """Lens 对同一张图会随机返回 0 条，空返回必须重试，有结果就立刻停。"""

    def _engine(self, fake, retries: int = 2):
        from core.keypool import KeyPool

        engine = LensEngine(KeyPool(["k"]), retries=retries, retry_delay=0)
        engine._search_once = fake
        return engine

    def test_retries_until_non_empty(self) -> None:
        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            if len(calls) < 3:
                return []
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        items = self._engine(fake).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 3)

    def test_stops_on_first_non_empty(self) -> None:
        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        items = self._engine(fake, retries=3).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_retries(self) -> None:
        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            return []

        self.assertEqual(self._engine(fake).search(b"img"), [])
        self.assertEqual(len(calls), 3)

    def test_retries_after_engine_error(self) -> None:
        """上传失败也要重试，别让一次抖动整轮丢掉 Lens。"""
        from core.engines import EngineError

        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            if len(calls) == 1:
                raise EngineError("图片上传失败")
            return [Candidate(engine="lens", url="https://x.com/foo/status/1")]

        items = self._engine(fake).search(b"img")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 2)

    def test_raises_when_all_attempts_fail(self) -> None:
        from core.engines import EngineError

        calls: list[int] = []

        def fake(raw: bytes, image_url: str = "") -> list[Candidate]:
            calls.append(1)
            raise EngineError("图片上传失败")

        with self.assertRaises(EngineError):
            self._engine(fake).search(b"img")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
