"""指令正则的回归测试：带图消息的前置占位符不能挡住匹配。"""

from __future__ import annotations

import re
import unittest

from core.commands import build_command_pattern, build_more_pattern


class CommandPatternTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = re.compile(build_command_pattern(["麦麦寻人", "找鸡"]))
        self.more = re.compile(build_more_pattern(["更多", "more"]))

    def test_plain(self) -> None:
        match = self.pattern.search("麦麦寻人")
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_with_image_placeholder(self) -> None:
        match = self.pattern.search("[image] 麦麦寻人")
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_with_image_and_argument(self) -> None:
        match = self.pattern.search("[image] 麦麦寻人 更多")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["arg"], "更多")

    def test_multiple_placeholders(self) -> None:
        self.assertIsNotNone(self.pattern.search("[image] [face] 麦麦寻人"))

    def test_alias(self) -> None:
        self.assertIsNotNone(self.pattern.search("找鸡"))

    def test_not_matched_in_sentence(self) -> None:
        self.assertIsNone(self.pattern.search("今天用麦麦寻人试了一下"))

    def test_more_with_placeholder(self) -> None:
        self.assertIsNotNone(self.more.search("[image] 更多"))


if __name__ == "__main__":
    unittest.main()
