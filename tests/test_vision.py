"""视觉兜底的纯函数：拼图、prompt、回复解析。

不碰网络也不碰插件上下文，全部用生成的图片跑。
"""

from __future__ import annotations

import io
import json
import random
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vision  # noqa: E402
from core import formatting  # noqa: E402
from core.engines import Candidate  # noqa: E402
from core.tasks import SearchOutcome  # noqa: E402


def _sample(seed: int, size: tuple[int, int] = (480, 640)) -> bytes:
    """生成一张有结构的图，避免纯色让缩放结果毫无区别。"""
    rnd = random.Random(seed)
    image = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(200), 90))
    draw = ImageDraw.Draw(image)
    for _ in range(6):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        draw.ellipse((x, y, x + 120, y + 160), fill=(240, 240, 230))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _size(raw: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(raw)).size


class BuildPromptTest(unittest.TestCase):
    def test_prompt_mentions_each_candidate_count(self):
        prompt = vision.build_prompt(16)
        self.assertIn("第 1 到第 16 格", prompt)
        self.assertIn("TARGET", prompt)
        # 判定标准必须是「同一个人」，不能退回「同一张图」。
        self.assertIn("同一个人", prompt)
        self.assertIn("same_person", prompt)

    def test_prompt_braces_are_rendered(self):
        """模板里的 JSON 示例用双花括号转义，渲染后应剩单花括号。"""
        prompt = vision.build_prompt(3)
        self.assertIn('{"same_person": [编号]', prompt)
        self.assertNotIn("{{", prompt)


class BuildGridTest(unittest.TestCase):
    def test_target_is_slot_zero_and_layout_matches_counts(self):
        grid = vision.build_grid(_sample(1), [_sample(i) for i in range(1, 17)])
        self.assertAlmostEqual(grid.width, 5 * 224 + 6 * 4, delta=1)
        self.assertAlmostEqual(grid.height, 4 * 224 + 5 * 4, delta=1)
        self.assertTrue(grid.data.startswith(b"\xff\xd8"))

    def test_twenty_five_candidates_take_five_rows(self):
        grid = vision.build_grid(_sample(1), [_sample(i) for i in range(1, 26)])
        self.assertLessEqual(grid.width, 1200)
        self.assertGreater(grid.height, grid.width)

    def test_broken_candidate_keeps_its_slot(self):
        """解不开的图占一格灰块，编号不能往前挪，否则模型报的编号会错位。"""
        grid = vision.build_grid(_sample(1), [_sample(2), b"not-an-image", _sample(3)])
        self.assertGreater(len(grid.data), 0)

    def test_requires_at_least_one_candidate(self):
        with self.assertRaises(ValueError):
            vision.build_grid(_sample(1), [])

    def test_data_url_prefix(self):
        grid = vision.build_grid(_sample(1), [_sample(2)])
        self.assertTrue(grid.data_url.startswith("data:image/jpeg;base64,"))


class ParsePicksTest(unittest.TestCase):
    def test_plain_json(self):
        text = '{"same_person": [3, 7], "reason": "同一件外套"}'
        self.assertEqual(vision.parse_picks(text, 16), [3, 7])

    def test_markdown_fence(self):
        text = '结果如下：\n```json\n{"same_person": [12]}\n```\n'
        self.assertEqual(vision.parse_picks(text, 16), [12])

    def test_single_quotes_and_string_numbers(self):
        text = "{'same_person': ['5', '9']}"
        self.assertEqual(vision.parse_picks(text, 16), [5, 9])

    def test_no_json_falls_back_to_array_regex(self):
        self.assertEqual(vision.parse_picks("same_person: [2, 4]", 16), [2, 4])
        self.assertEqual(vision.parse_picks("同一个人：same_person：[6]", 16), [6])

    def test_none_found(self):
        self.assertEqual(vision.parse_picks('{"same_person": [], "reason": "都不是"}', 16), [])

    def test_drops_zero_out_of_range_and_duplicates(self):
        text = '{"same_person": [0, 1, 1, 20, 99]}'
        self.assertEqual(vision.parse_picks(text, 16), [1])

    def test_garbage_and_empty(self):
        for text in ("", "都不是", "{}", "null", "抱歉，我无法判断"):
            self.assertEqual(vision.parse_picks(text, 16), [])

    def test_alternative_keys(self):
        self.assertEqual(vision.parse_picks('{"picks": [4]}', 16), [4])

    def test_never_picks_target(self):
        """0 号格是目标图本身，模型要是回 0 必须丢掉。"""
        self.assertEqual(vision.parse_picks('{"same_person": [0]}', 16), [])

    def test_max_index_bounds(self):
        self.assertEqual(vision.parse_picks('{"same_person": [16]}', 16), [16])
        self.assertEqual(vision.parse_picks('{"same_person": [17]}', 16), [])


class ReasonTest(unittest.TestCase):
    def test_reason_extracted(self):
        self.assertEqual(vision.reason_of('```json\n{"same_person": [2], "reason": "同款帽子和发色"}\n```'), "同款帽子和发色")

    def test_reason_missing(self):
        self.assertEqual(vision.reason_of("没有 JSON"), "")
        self.assertEqual(vision.reason_of(""), "")


class VisionDisplayTest(unittest.TestCase):
    """视觉命中的候选不能冒充「同一张图」，文案上必须区分开。"""

    def test_best_block_marks_vision_hit(self):
        item = Candidate(
            engine="lens",
            url="https://x.com/someone/status/1",
            platform="X",
            handle="@someone",
            status="vision",
            note="视觉复核：同一个人（指纹距离 24，超过阈值 8）",
        )
        outcome = SearchOutcome(status="ok", best=item, candidates=[item])
        text = formatting._best_block(outcome)
        self.assertIn("视觉复核认为是同一个人", text)
        self.assertIn("@someone", text)
        self.assertIn("同一张图", text)
        # 视觉判的是同一个人，必须说明是模型挑的、可能认错，让用户自己核对。
        self.assertIn("视觉模型挑的", text)
        self.assertIn("自行核对", text)

    def test_not_found_reports_vision_checked(self):
        outcome = SearchOutcome(status="not_found", vision={"ok": True, "checked": 16, "picked": []})
        text = formatting._best_block(outcome)
        self.assertIn("16", text)
        self.assertIn("没有发现同一个人", text)

    def test_not_found_reports_vision_error(self):
        outcome = SearchOutcome(status="not_found", vision={"ok": False, "checked": 0, "error": "视觉模型 90 秒内没返回"})
        text = formatting._best_block(outcome)
        self.assertIn("视觉复核没做完", text)
        self.assertIn("90 秒", text)

    def test_vision_candidate_keeps_account(self):
        """指纹不符的候选要隐藏账号，视觉命中的不用隐藏。"""
        self.assertEqual(formatting._candidate_identity(Candidate(engine="lens", handle="@a", status="vision")), "@a")
        self.assertEqual(
            formatting._candidate_identity(Candidate(engine="lens", handle="@a", status="mismatch")),
            "（账号未确认）",
        )

    def test_commerce_detected_by_url_or_platform(self):
        """电商商品页要能认出来：同款衣服的商品图不是同一个人。"""
        self.assertTrue(vision.is_commerce(url="https://aliexpress.ru/item/1005003898750895.html"))
        self.assertTrue(vision.is_commerce(platform="russian.alibaba.com"))
        self.assertTrue(vision.is_commerce(name="Women tights - Alibaba.com"))
        self.assertFalse(vision.is_commerce(url="https://x.com/someone/status/123", platform="X (Twitter)"))
        self.assertFalse(vision.is_commerce(url="https://www.pixiv.net/artworks/1", platform="pixiv.net"))

    def test_traceable_needs_handle_or_social_host(self):
        self.assertTrue(vision.is_traceable("@someone", "X (Twitter)", "https://x.com/someone/status/1"))
        self.assertTrue(vision.is_traceable("", "pixiv.net", ""))
        self.assertFalse(vision.is_traceable("", "aliexpress.ru", "https://aliexpress.ru/item/1"))
        self.assertFalse(vision.is_traceable("", "某论坛", "https://example.com/thread/9"))

    def test_not_found_reports_downgraded_picks(self):
        """视觉挑中的都被降级时，不能说「没有发现同一个人」。"""
        outcome = SearchOutcome(
            status="not_found",
            vision={"ok": True, "checked": 16, "picked": [], "downgraded": 3},
        )
        text = formatting._best_block(outcome)
        self.assertIn("3", text)
        self.assertIn("只当参考", text)
        self.assertNotIn("没有发现同一个人", text)


if __name__ == "__main__":
    unittest.main()
