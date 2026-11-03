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

    def test_quoted_message_placeholder(self) -> None:
        """引用消息时占位符里套方括号，不能在内层右括号处截断。"""
        text = "[回复了男的也能当娘吗？的消息: [image]] 麦麦寻人"
        match = self.pattern.search(text)
        self.assertIsNotNone(match)
        self.assertIsNone(match.groupdict()["arg"])

    def test_quoted_message_with_nested_task(self) -> None:
        text = "[回复了某人的消息: [image]] 更多 s-20261103-0832-d7d9"
        match = self.more.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["task"], "s-20261103-0832-d7d9")

    def test_alias(self) -> None:
        self.assertIsNotNone(self.pattern.search("找鸡"))

    def test_not_matched_in_sentence(self) -> None:
        self.assertIsNone(self.pattern.search("今天用麦麦寻人试了一下"))

    def test_more_with_placeholder(self) -> None:
        self.assertIsNotNone(self.more.search("[image] 更多"))

    def test_more_with_task_id(self) -> None:
        """「更多 + 任务编号」要能取出编号；裸「更多」保持原来的用法。"""
        match = self.more.search("更多 s-20261103-0832-d7d9")
        self.assertIsNotNone(match)
        self.assertEqual(match.groupdict()["task"], "s-20261103-0832-d7d9")
        plain = self.more.search("更多")
        self.assertIsNotNone(plain)
        self.assertIsNone(plain.groupdict()["task"])

    def test_more_does_not_swallow_sentences(self) -> None:
        self.assertIsNone(self.more.search("更多这个我不知道"))


if __name__ == "__main__":
    unittest.main()
