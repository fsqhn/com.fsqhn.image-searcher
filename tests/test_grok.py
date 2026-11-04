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
        self.assertIn("只找到账号不算完成", text)
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

    def test_internal_status_link_keeps_the_tweet_id(self) -> None:
        """x.com/i/status/<id> 没有作者名，但 status id 是真的，能反查出作者。"""
        posts = grok.find_posts("参考 https://x.com/i/status/1234567890 这条", [])
        self.assertEqual(posts, [("", "https://x.com/i/status/1234567890")])
        result = grok.parse_output(_body("https://x.com/i/status/1234567890"))
        self.assertEqual(result.tier, "post")
        # 不能把 "i" 当成账号名发出去。
        self.assertEqual(result.handle, "")
        self.assertEqual(result.post_url, "https://x.com/i/status/1234567890")

    def test_other_reserved_paths_ignored(self) -> None:
        self.assertEqual(grok.find_posts("https://x.com/home/status/1234567890", []), [])
        self.assertEqual(grok.find_posts("https://x.com/search/status/1234567890", []), [])
        self.assertEqual(grok.find_posts("https://x.com/home", []), [])

    def test_all_candidate_posts_are_kept(self) -> None:
        """模型列了好几条帖子时都要留着，插件逐条比指纹，别只信第一条。"""
        body = _body(
            "https://x.com/a_one/status/1111111111\n"
            "https://x.com/a_two/status/2222222222\n"
            "https://x.com/i/status/3333333333"
        )
        result = grok.parse_output(body)
        self.assertEqual(result.post_url, "https://x.com/a_one/status/1111111111")
        self.assertEqual(
            [url for _handle, url in result.posts],
            [
                "https://x.com/a_one/status/1111111111",
                "https://x.com/a_two/status/2222222222",
                "https://x.com/i/status/3333333333",
            ],
        )

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


class PickBestCheckTest(unittest.TestCase):
    """Grok 给了多条帖子、逐条比完指纹之后，选哪条当结论。"""

    def test_hit_wins_even_when_it_is_not_the_first(self) -> None:
        checks = [
            {"verdict": "miss", "distance": 12},
            {"verdict": "hit", "distance": 3},
        ]
        self.assertEqual(grok.pick_best_check(checks)["verdict"], "hit")

    def test_no_hit_falls_back_to_the_closest(self) -> None:
        checks = [
            {"verdict": "miss", "distance": 30},
            {"verdict": "miss", "distance": 20},
            {"verdict": "unknown", "distance": -1},
        ]
        self.assertEqual(grok.pick_best_check(checks)["distance"], 20)

    def test_distance_zero_counts_as_usable(self) -> None:
        """距离 0 是完美命中，不能被当成「没有距离」丢掉。"""
        checks = [
            {"verdict": "unknown", "distance": -1},
            {"verdict": "miss", "distance": 0},
        ]
        self.assertEqual(grok.pick_best_check(checks)["distance"], 0)

    def test_all_unknown_keeps_the_first(self) -> None:
        checks = [
            {"verdict": "unknown", "distance": -1},
            {"verdict": "unknown", "distance": -1},
        ]
        self.assertIs(grok.pick_best_check(checks), checks[0])

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(grok.pick_best_check([]))


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
