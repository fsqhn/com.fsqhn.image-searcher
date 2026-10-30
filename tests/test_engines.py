"""引擎结果解析的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.engines import Candidate, to_yandex_candidate, verify_candidate  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
