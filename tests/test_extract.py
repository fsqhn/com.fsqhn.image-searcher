"""链接解析、账号提取与 key 池的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.extract import (  # noqa: E402
    extract_identity,
    is_x_platform,
    platform_of,
    promote_sotwe,
    relevance_rank,
)
from core.keypool import KeyPool  # noqa: E402


class ExtractIdentityTest(unittest.TestCase):
    """账号直接从链接或标题取，不抓页面。"""

    def test_x_status_url(self) -> None:
        name, handle = extract_identity(
            "https://x.com/NERUchi890/status/2111815792396493110",
            "NENE on X: \"...\"",
        )
        self.assertEqual(handle, "@NERUchi890")

    def test_x_profile_url(self) -> None:
        _name, handle = extract_identity("https://x.com/usagi_rr", "荣小兔头")
        self.assertEqual(handle, "@usagi_rr")

    def test_title_handle(self) -> None:
        name, handle = extract_identity("https://example.com/post/1", "荣小兔头 @usagi_rr - Twitter")
        self.assertEqual(handle, "@usagi_rr")
        self.assertEqual(name, "荣小兔头")

    def test_bilibili_space(self) -> None:
        _name, handle = extract_identity("https://space.bilibili.com/123456", "某个UP")
        self.assertEqual(handle, "UID 123456")

    def test_bilibili_video(self) -> None:
        _name, handle = extract_identity("https://www.bilibili.com/video/BV1xx411c7mD", "视频标题")
        self.assertEqual(handle, "BV1xx411c7mD")

    def test_douyin_user(self) -> None:
        _name, handle = extract_identity("https://www.douyin.com/user/MS4wLjABAAAA", "某人")
        self.assertEqual(handle, "@MS4wLjABAAAA")

    def test_no_identity(self) -> None:
        name, handle = extract_identity("https://example.com/gallery/9", "一张相册图")
        self.assertEqual(handle, "")
        self.assertEqual(name, "一张相册图")


class PromoteSotweTest(unittest.TestCase):
    """sotwe 镜像还原成 x.com；handle 以标题为准，避免路径多余下划线。"""

    def test_promote_uses_title_handle(self) -> None:
        url, title = promote_sotwe("https://sotwe.com/usagi_rr_", "荣小兔头 @usagi_rr - Twitter")
        self.assertEqual(url, "https://x.com/usagi_rr")
        self.assertEqual(title, "荣小兔头 @usagi_rr")

    def test_promote_falls_back_to_path(self) -> None:
        url, _title = promote_sotwe("https://sotwe.com/miaomiaonyaw", "喵喵")
        self.assertEqual(url, "https://x.com/miaomiaonyaw")

    def test_non_sotwe_untouched(self) -> None:
        url, title = promote_sotwe("https://x.com/a/status/1", "标题")
        self.assertEqual((url, title), ("https://x.com/a/status/1", "标题"))


class PlatformTest(unittest.TestCase):
    def test_platform_names(self) -> None:
        self.assertEqual(platform_of("https://x.com/a/status/1"), "X (Twitter)")
        self.assertEqual(platform_of("https://www.bilibili.com/video/BV1"), "B站")
        self.assertEqual(platform_of("https://www.douyin.com/user/x"), "抖音")
        self.assertEqual(platform_of("https://weibo.com/u/123"), "微博")
        self.assertEqual(platform_of("https://www.pinterest.com/pin/1"), "Pinterest")

    def test_relevance_rank(self) -> None:
        self.assertEqual(relevance_rank("https://x.com/a"), 0)
        self.assertEqual(relevance_rank("https://pinterest.com/pin/1"), 2)


class KeyPoolTest(unittest.TestCase):
    """多 key 轮询与冷却。"""

    def test_round_robin(self) -> None:
        pool = KeyPool(["a", "b"])
        self.assertEqual(pool.acquire(), "a")
        self.assertEqual(pool.acquire(), "b")
        self.assertEqual(pool.acquire(), "a")

    def test_failure_switches_key(self) -> None:
        pool = KeyPool(["a", "b"])
        first = pool.acquire()
        pool.mark_failure(first, cooldown_seconds=60)
        self.assertEqual(pool.acquire(), "b")
        pool.mark_failure("b", cooldown_seconds=60)
        self.assertIsNone(pool.acquire())
        self.assertEqual(pool.available_count(), 0)

    def test_success_clears_cooldown(self) -> None:
        pool = KeyPool(["a"])
        pool.mark_failure("a", cooldown_seconds=60)
        self.assertIsNone(pool.acquire())
        pool.mark_success("a")
        self.assertEqual(pool.acquire(), "a")

    def test_empty_pool(self) -> None:
        pool = KeyPool([])
        self.assertFalse(pool)
        self.assertIsNone(pool.acquire())


class XPlatformPreferenceTest(unittest.TestCase):
    def test_detects_x_platform(self) -> None:
        self.assertTrue(is_x_platform("X (Twitter)"))
        self.assertTrue(is_x_platform("x.com"))
        self.assertTrue(is_x_platform("twitter"))

    def test_other_platforms_are_not_x(self) -> None:
        for platform in ("picazor.com", "B站", "抖音", "pixiv.net", ""):
            with self.subTest(platform=platform):
                self.assertFalse(is_x_platform(platform))


if __name__ == "__main__":
    unittest.main()