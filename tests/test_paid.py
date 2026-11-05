"""泄露站收录页与 IG 提取：只测纯函数，联网由插件负责。"""

from __future__ import annotations

import unittest

from core import paid

# 实测 2026-11-05 的真实页面标题。
HIT = (
    "<html><head><title>Dream Tamu / Exclusive Leaked Nude OnlyFans</title></head>"
    "<body>648 medias</body></html>"
)
MISS = "<html><head><title>Page not found / Leak Gallery</title></head></html>"


class ParsePageTest(unittest.TestCase):
    def test_reads_the_display_name(self) -> None:
        info = paid.parse_page(HIT)
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "Dream Tamu")

    def test_404_page_is_not_a_hit(self) -> None:
        self.assertIsNone(paid.parse_page(MISS))
        self.assertIsNone(paid.parse_page(""))

    def test_tag_page_is_not_a_creator(self) -> None:
        """实测：leakgallery.com/Vtuber 返回的是标签页，作者叫 Rayariot。"""
        tag = "<html><head><title>Rayariot / Vtuber / Exclusive Leaked Nude OnlyFans</title></head></html>"
        self.assertIsNone(paid.parse_page(tag))
        self.assertIsNone(paid.parse_page(tag, "Vtuber"))
        self.assertIsNone(paid.parse_page(tag, "Rayariot"))

    def test_name_must_match_the_slug(self) -> None:
        self.assertIsNotNone(paid.parse_page(HIT, "dreamtamu"))
        self.assertIsNone(paid.parse_page(HIT, "nookkizzcos"))


class InstagramTest(unittest.TestCase):
    def test_from_website_field(self) -> None:
        user = {"website": {"url": "https://www.instagram.com/dream_tamu_?igsh=abc"}, "description": ""}
        self.assertEqual(paid.instagram_of(user), "https://instagram.com/dream_tamu_")

    def test_from_description(self) -> None:
        # 实测 @DreamTamuu 的简介：IG 只写在正文里。
        user = {"website": None, "description": "My new account! Please follow me there too 💕\n\nIG : dream_tamu_"}
        self.assertEqual(paid.instagram_of(user), "https://instagram.com/dream_tamu_")

    def test_none_when_missing(self) -> None:
        self.assertEqual(paid.instagram_of({"description": "Hi, my name is Nook."}), "")
        self.assertEqual(paid.instagram_of(None), "")


if __name__ == "__main__":
    unittest.main()
