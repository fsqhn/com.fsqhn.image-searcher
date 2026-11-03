"""Grok 兜底的纯函数：请求体、返回解析、分档。

不碰网络。返回体按中转站 /v1/responses 的真实形状构造。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import grok  # noqa: E402
from core import formatting  # noqa: E402
from core.tasks import SearchOutcome  # noqa: E402


def _body(text: str, urls: list[str] | None = None, tools: list[str] | None = None) -> dict:
    """拼一个最小可用的 Responses 返回：一条 message + 可选 annotations。"""
    content = [{"type": "output_text", "text": text}]
    if urls:
        content[0]["annotations"] = [{"type": "url_citation", "url": url} for url in urls]
    output = [{"type": "reasoning", "summary": []}]
    for name in tools or []:
        output.append({"type": "custom_tool_call", "name": name, "input": "{}"})
    output.append({"type": "message", "role": "assistant", "content": content})
    return {"model": "grok-4.7", "output": output, "usage": {"input_tokens": 100}}


class BuildPayloadTest(unittest.TestCase):
    def test_uses_responses_shape(self) -> None:
        body = grok.build_payload("grok-4.7", "data:image/jpeg;base64,AAA")
        self.assertEqual(body["model"], "grok-4.7")
        self.assertEqual(body["tools"], [{"type": "x_search"}])
        content = body["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/jpeg;base64,"))
        # 走 chat/completions 的 search_parameters 会被上游打回 410，这里不能出现。
        self.assertNotIn("search_parameters", body)

    def test_prompt_forbids_guessing(self) -> None:
        body = grok.build_payload("grok-4.7", "data:image/jpeg;base64,AAA")
        text = body["input"][0]["content"][0]["text"]
        self.assertIn("没找到", text)
        self.assertIn("不要根据水印猜", text)

    def test_prompt_requires_image_search_when_hints_miss(self) -> None:
        """候选都对不上时必须拿图去 x_search：自拍图只靠关键词必然搜不到。"""
        body = grok.build_payload("grok-4.7", "data:image/jpeg;base64,AAA", hints=["@someone | X | 标题"])
        text = body["input"][0]["content"][0]["text"]
        self.assertIn("这张图本身作为检索输入", text)
        self.assertIn("算没完成任务", text)

    def test_hints_are_passed_into_prompt(self) -> None:
        """已有候选要写进 prompt，让它先核对候选而不是凭空猜关键词。"""
        body = grok.build_payload(
            "grok-4.7",
            "data:image/jpeg;base64,AAA",
            hints=["@someone | X (Twitter) | 某条推文"],
        )
        text = body["input"][0]["content"][0]["text"]
        self.assertIn("@someone", text)
        self.assertIn("先", text)

    def test_build_hints_dedupes_and_prefers_identity(self) -> None:
        class _Item:
            def __init__(self, handle="", name="", platform=""):
                self.handle = handle
                self.name = name
                self.platform = platform

        hints = grok.build_hints(
            [
                _Item(handle="@a", name="第一条", platform="X (Twitter)"),
                _Item(handle="@a", name="重复的", platform="X (Twitter)"),
                _Item(name="某论坛帖子", platform="example.com"),
                _Item(),
                _Item(handle="@b", name="", platform="pixiv.net"),
            ]
        )
        self.assertEqual(len(hints), 3)
        self.assertTrue(hints[0].startswith("@a"))
        self.assertTrue(hints[2].startswith("@b"))

    def test_build_hints_respects_limit(self) -> None:
        class _Item:
            def __init__(self, index):
                self.handle = f"@u{index}"
                self.name = ""
                self.platform = "X (Twitter)"

        self.assertEqual(len(grok.build_hints([_Item(i) for i in range(20)], limit=5)), 5)

    def test_no_hints_keeps_original_prompt(self) -> None:
        body = grok.build_payload("grok-4.7", "data:image/jpeg;base64,AAA", hints=[])
        text = body["input"][0]["content"][0]["text"]
        self.assertNotIn("搜索引擎这边", text)


class ParseOutputTest(unittest.TestCase):
    def test_post_url_wins(self) -> None:
        body = _body(
            "账号：@ste11ar021\n帖子：https://x.com/ste11ar021/status/2117169702099370423\n依据：评论区在聊这张图的姿势",
            urls=["https://x.com/ste11ar021/status/2117169702099370423"],
            tools=["x_user_search", "x_keyword_search"],
        )
        result = grok.parse_output(body)
        self.assertTrue(result.ok)
        self.assertEqual(result.tier, "post")
        self.assertEqual(result.handle, "ste11ar021")
        self.assertEqual(result.post_url, "https://x.com/ste11ar021/status/2117169702099370423")
        self.assertIn("评论区", result.reason)
        self.assertEqual(result.tools, ["x_user_search", "x_keyword_search"])

    def test_post_only_in_annotations(self) -> None:
        body = _body("我找到了这条帖子。", urls=["https://twitter.com/abc_123/status/1234567890"])
        result = grok.parse_output(body)
        self.assertEqual(result.tier, "post")
        self.assertEqual(result.handle, "abc_123")
        self.assertEqual(result.post_url, "https://x.com/abc_123/status/1234567890")

    def test_account_only(self) -> None:
        body = _body("账号：@ste11ar021\n帖子：无\n依据：水印和主页配图一致")
        result = grok.parse_output(body)
        self.assertEqual(result.tier, "account")
        self.assertEqual(result.handle, "ste11ar021")
        self.assertEqual(result.post_url, "")

    def test_none_when_no_link(self) -> None:
        body = _body("没找到。我搜了 ste11ar021 和 伊落玛丽，都没有能对上的。")
        result = grok.parse_output(body)
        self.assertTrue(result.ok)
        self.assertEqual(result.tier, "none")
        self.assertEqual(result.handle, "")
        # 正文里裸写的账号名不能当成账号，必须带链接。
        self.assertEqual(result.post_url, "")

    def test_reserved_paths_ignored(self) -> None:
        body = _body("参考 https://x.com/i/status/1234567890 和 https://x.com/home ，都不是。")
        result = grok.parse_output(body)
        # x.com/i/status/... 里的 i 是保留路径，不能当账号。
        self.assertNotEqual(result.handle, "i")
        self.assertEqual(result.tier, "none")

    def test_empty_output(self) -> None:
        result = grok.parse_output({"output": []})
        self.assertFalse(result.ok)
        self.assertEqual(result.tier, "none")
        self.assertEqual(result.error, "返回空内容")

    def test_fence_and_noise(self) -> None:
        body = _body(
            "```\n账号：@foo_bar\n帖子：https://x.com/foo_bar/status/999888777666\n"
            "```\n依据：同一套衣服",
        )
        result = grok.parse_output(body)
        self.assertEqual(result.tier, "post")
        self.assertEqual(result.handle, "foo_bar")
        self.assertEqual(result.reason, "同一套衣服")

    def test_duplicate_links_keep_order(self) -> None:
        body = _body(
            "https://x.com/a_one/status/1111111111 然后 https://x.com/a_one/status/1111111111",
            urls=["https://x.com/a_one/status/1111111111"],
        )
        posts = grok.find_posts("https://x.com/a_one/status/1111111111", ["https://x.com/a_one/status/1111111111"])
        self.assertEqual(len(posts), 1)
        self.assertEqual(grok.parse_output(body).post_url, "https://x.com/a_one/status/1111111111")

    def test_reason_fallback_strips_links(self) -> None:
        self.assertEqual(grok.reason_of(""), "")
        self.assertIn("同款", grok.reason_of("看起来是同款 https://x.com/a/status/1234567890 的人"))


class FormattingTest(unittest.TestCase):
    def test_post_tier_reads_as_found(self) -> None:
        outcome = SearchOutcome(
            status="ok",
            grok={"tier": "post", "handle": "ste11ar021",
                  "post_url": "https://x.com/ste11ar021/status/2117169702099370423",
                  "reason": "评论区在聊这张图"},
        )
        text = formatting._best_block(outcome)
        self.assertIn("找到 X 上的原帖", text)
        self.assertIn("status/2117169702099370423", text)
        self.assertIn("评论区在聊这张图", text)

    def test_account_tier_reads_as_lead(self) -> None:
        outcome = SearchOutcome(status="ok", grok={"tier": "account", "handle": "ste11ar021"})
        text = formatting._best_block(outcome)
        self.assertIn("没核到原帖", text)
        self.assertIn("x.com/ste11ar021", text)

    def test_none_tier_says_searched(self) -> None:
        outcome = SearchOutcome(status="not_found", grok={"tier": "none", "reason": "无"})
        text = formatting._best_block(outcome)
        self.assertIn("未找到可靠出处", text)
        self.assertIn("Grok 在 X 上搜过", text)

    def test_grok_error_reported(self) -> None:
        outcome = SearchOutcome(status="not_found", grok={"tier": "none", "error": "Grok 70 秒内没返回"})
        text = formatting._best_block(outcome)
        self.assertIn("没做成", text)

    def test_without_grok_block(self) -> None:
        outcome = SearchOutcome(status="not_found", grok=None)
        text = formatting._best_block(outcome)
        self.assertNotIn("Grok", text)


if __name__ == "__main__":
    unittest.main()
