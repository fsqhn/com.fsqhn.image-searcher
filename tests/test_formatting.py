"""合并转发节点拼装：重点是没找到匹配时也要带一张参考图。"""

from __future__ import annotations

import json
import unittest

from core.engines import Candidate
from core.formatting import (
    build_engine_forward_nodes,
    build_forward_nodes,
    build_result_forwards,
    build_text_summary,
    engine_groups,
    stale_image_hint,
    ttl_text,
)
from core.tasks import SearchOutcome, SearchTask


def _task(outcome: SearchOutcome) -> SearchTask:
    return SearchTask(
        task_id="s-test",
        stream_id="g1",
        user_id="u1",
        status="done",
        outcome=outcome,
    )


def _images(nodes: list[dict]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for index, node in enumerate(nodes):
        for part in node.get("content") or []:
            if part.get("type") == "image":
                found.append((index, part.get("content") or ""))
    return found


class ForwardNodesTest(unittest.TestCase):
    def test_best_candidate_image_goes_to_best_node(self) -> None:
        best = Candidate(engine="lens", url="https://x.com/a/status/1", distance=4, status="match")
        best.finalize()
        outcome = SearchOutcome(status="ok", best=best, candidates=[best], image_b64="BESTIMG")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        images = _images(nodes)
        self.assertEqual(images, [(1, "BESTIMG")])

    def test_not_found_still_carries_a_reference_image(self) -> None:
        other = Candidate(engine="yandex", url="https://www.pixiv.net/artworks/1", distance=21, status="suspect")
        other.finalize()
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[other],
            image_b64="REFIMG",
            image_source=other.image_url,
        )
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        images = _images(nodes)
        self.assertEqual(images, [(2, "REFIMG")])
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("未经指纹确认", text)

    def test_not_found_without_candidates_still_shows_image(self) -> None:
        outcome = SearchOutcome(status="not_found", best=None, candidates=[], image_b64="UPIMG", image_source="upload")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", show_reference=False)
        images = _images(nodes)
        self.assertEqual(images, [(2, "UPIMG")])
        joined = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("参考图", joined)

    def _grouped_outcome(self, *, hit: bool = True) -> SearchOutcome:
        best = Candidate(
            engine="saucenao",
            url="https://www.pixiv.net/artworks/9",
            distance=3,
            status="match",
        )
        lens_a = Candidate(
            engine="lens",
            url="https://www.pixiv.net/artworks/1",
            image_url="https://img.example/1.jpg",
            distance=9,
            status="suspect",
        )
        lens_b = Candidate(
            engine="lens",
            url="https://www.pixiv.net/artworks/2",
            image_url="https://img.example/2.jpg",
            distance=11,
            status="suspect",
        )
        yandex_a = Candidate(
            engine="yandex",
            url="https://x.com/a/status/1",
            image_url="https://img.example/3.jpg",
            distance=12,
            status="suspect",
        )
        for item in (best, lens_a, lens_b, yandex_a):
            item.finalize()
        return SearchOutcome(
            status="ok" if hit else "not_found",
            best=best if hit else None,
            candidates=[lens_a, lens_b, yandex_a, best],
            image_b64="BEST",
            image_source=best.image_url,
        )

    def test_reference_is_grouped_by_engine(self) -> None:
        """没找到时的参考区按引擎分开，每个引擎一个节点，标题带引擎名和条数。"""
        nodes = build_forward_nodes(_task(self._grouped_outcome(hit=False)), command="麦麦寻人")
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("▍Google Lens（2 条）", text)
        self.assertIn("▍Yandex（1 条）", text)
        # 最佳匹配那一条不重复出现在参考区
        self.assertEqual(text.count("artworks/9"), 1)

    def test_hit_hides_reference(self) -> None:
        """命中时首次回复只给结论，参考区留给「更多」。"""
        nodes = build_forward_nodes(_task(self._grouped_outcome()), command="麦麦寻人")
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertNotIn("▍Google Lens", text)
        self.assertNotIn("▍Yandex", text)
        self.assertIn("pixiv.net/artworks/9", text)

    def test_footer_carries_task_id(self) -> None:
        """首次回复要给出任务编号，用户靠它回「更多」定位到具体那一次。"""
        task = _task(self._grouped_outcome())
        nodes = build_forward_nodes(task, command="麦麦寻人", ttl_minutes=1440)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn(task.task_id, text)
        self.assertIn("1 天", text)
        self.assertIn("更多", text)

    def test_footer_offers_force_research_after_more(self) -> None:
        """页脚在「更多」后面给出重搜入口：缓存秒回时也知道怎么强制重跑。"""
        task = _task(self._grouped_outcome())
        nodes = build_forward_nodes(task, command="麦麦寻人", ttl_minutes=1440)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("重搜", text)
        self.assertGreater(text.index("重搜"), text.index("更多"))

    def test_ttl_text_reads_in_human_units(self) -> None:
        self.assertEqual(ttl_text(15), "15 分钟")
        self.assertEqual(ttl_text(60), "1 小时")
        self.assertEqual(ttl_text(90), "1.5 小时")
        self.assertEqual(ttl_text(1440), "1 天")
        self.assertEqual(ttl_text(2880), "2 天")

    def test_first_reply_carries_one_image_per_engine(self) -> None:
        """没找到时首次回复：每个引擎最接近的那张带图，其余候选只列文字。"""
        outcome = self._grouped_outcome(hit=False)
        outcome.images = {"https://img.example/1.jpg": "LENS1", "https://img.example/3.jpg": "YANDEX1"}
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        self.assertEqual(_images(nodes), [(2, "BEST"), (3, "LENS1"), (4, "YANDEX1")])

    def test_more_reply_carries_every_candidate_image(self) -> None:
        outcome = self._grouped_outcome(hit=False)
        outcome.images = {
            "https://img.example/1.jpg": "LENS1",
            "https://img.example/2.jpg": "LENS2",
            "https://img.example/3.jpg": "YANDEX1",
        }
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", limit=10, replay=True)
        self.assertEqual(
            _images(nodes),
            [(2, "BEST"), (3, "LENS1"), (3, "LENS2"), (4, "YANDEX1")],
        )

    def test_engine_limit_caps_candidates_per_engine(self) -> None:
        nodes = build_forward_nodes(_task(self._grouped_outcome(hit=False)), command="麦麦寻人", limit=1)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("▍Google Lens（1 条）", text)
        self.assertNotIn("artworks/2", text)

    def test_reference_image_is_not_repeated_in_engine_group(self) -> None:
        """没找到匹配时那张参考图已经单独贴过，分组里不能再贴一次同一张。"""
        outcome = self._grouped_outcome()
        outcome.best = None
        outcome.status = "not_found"
        outcome.image_b64 = "REF"
        outcome.image_source = "https://img.example/1.jpg"
        outcome.images = {"https://img.example/1.jpg": "REF", "https://img.example/3.jpg": "YANDEX1"}
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        self.assertEqual(_images(nodes), [(2, "REF"), (4, "YANDEX1")])

    def test_engine_group_sorted_by_distance(self) -> None:
        """组内按指纹距离从近到远排，第一条就是该引擎最接近的那张。"""
        outcome = self._grouped_outcome(hit=False)
        lens_one = next(item for item in outcome.candidates if item.url.endswith("/1"))
        lens_two = next(item for item in outcome.candidates if item.url.endswith("/2"))
        lens_one.distance, lens_two.distance = 15, 6
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", limit=1)
        text = "\n".join(
            part.get("content", "")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("artworks/2", text)
        self.assertNotIn("artworks/1", text)

    def test_engine_forward_carries_best_only_on_first_batch(self) -> None:
        """图多时按引擎拆成多条，最佳匹配只在第一条出现，页脚只在最后一条。"""
        outcome = self._grouped_outcome()
        outcome.images = {
            "https://img.example/1.jpg": "LENS1",
            "https://img.example/2.jpg": "LENS2",
            "https://img.example/3.jpg": "YANDEX1",
        }
        groups = engine_groups(outcome, outcome.best, 10)
        self.assertTrue(groups)
        engine, items = groups[0]
        first = build_engine_forward_nodes(
            _task(outcome), engine, items, command="麦麦寻人", part=(1, 2), with_best=True
        )
        last = build_engine_forward_nodes(
            _task(outcome), groups[-1][0], groups[-1][1], command="麦麦寻人", part=(2, 2), include_footer=True
        )
        first_text = "\n".join(
            part.get("content", "")
            for node in first
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        last_text = "\n".join(
            part.get("content", "")
            for node in last
            for part in node.get("content") or []
            if part.get("type") == "text"
        )
        self.assertIn("1/2", first_text)
        self.assertIn("平台", first_text)  # 最佳匹配块在第一批
        self.assertIn("以上为全部参考结果", last_text)
        self.assertNotIn("平台", last_text)
        # 第一批的图只来自本批候选（含最佳匹配那张），不重复
        self.assertEqual(_images(first)[0], (1, "BEST"))

    def test_header_follows_configured_command(self) -> None:
        """指令可配置，转发标题也要跟着改，不能写死默认名。"""
        outcome = SearchOutcome(status="ok", best=None, candidates=[], image_b64="")
        nodes = build_forward_nodes(_task(outcome), command="示例指令")
        header = nodes[0]["content"][0]["content"]
        self.assertEqual(header, "🔍 示例指令结果")

    def test_nodes_carry_sender_uin(self) -> None:
        """uin 为空时 QQ 端会提示消息异常，所以每个节点都要带 user_id。"""
        outcome = SearchOutcome(status="not_found", best=None, candidates=[], image_b64="", image_source="")
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", user_id="2377510578")
        self.assertTrue(all(node.get("user_id") == "2377510578" for node in nodes))
        self.assertGreaterEqual(len(nodes), 3)


class CandidateAccountTest(unittest.TestCase):
    def test_unverified_candidate_hides_account(self) -> None:
        item = Candidate(
            engine="lens",
            url="https://x.com/someone",
            handle="@someone",
            name="Someone",
            status="unverified",
            note="插画路线参考，未做指纹比对",
        )
        item.finalize()
        outcome = SearchOutcome(status="not_found", best=None, candidates=[item])
        text = build_text_summary(_task(outcome), command="麦麦寻人")
        self.assertIn("账号未确认", text)
        self.assertNotIn("@someone", text)

    def test_matched_candidate_keeps_account(self) -> None:
        item = Candidate(engine="lens", url="https://x.com/someone", handle="@someone", distance=4, status="match")
        item.finalize()
        outcome = SearchOutcome(status="ok", best=item, candidates=[item])
        text = build_text_summary(_task(outcome), command="麦麦寻人")
        self.assertIn("@someone", text)


class EtaTextTest(unittest.TestCase):
    def test_running_task_uses_given_eta(self) -> None:
        task = SearchTask(task_id="s-test", stream_id="g1", user_id="u1", status="running")
        text = build_text_summary(task, command="麦麦寻人", eta_text="约 2 分钟")
        self.assertIn("约 2 分钟", text)
        self.assertNotIn("40 秒", text)


class StaleImageHintTest(unittest.TestCase):
    def test_hint_mentions_minutes_and_msg_id(self) -> None:
        text = stale_image_hint(15_600)
        self.assertIn("260 分钟", text)
        self.assertIn("msg_id", text)
        self.assertIn("引用", text)
        self.assertIn("不是图片服务故障", text)

    def test_under_one_minute_rounds_up(self) -> None:
        self.assertIn("1 分钟", stale_image_hint(30))


class GrokReferenceTest(unittest.TestCase):
    """Grok 给的帖子配图对不上时，链接仍要放出来，但必须标明是存疑参考。"""
    def test_account_tier_keeps_post_as_reference(self) -> None:
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={
                "tier": "account",
                "handle": "mian84232594",
                "post_url": "https://x.com/mian84232594/status/1",
                "reason": "帖子配图对不上（最近距离 28），帖子只当参考",
            },
            total_candidates=0,
        )
        text = build_text_summary(_task(outcome), command="盆地找鸡")
        self.assertIn("没核到原帖", text)
        self.assertIn("存疑参考", text)
        self.assertIn("https://x.com/mian84232594/status/1", text)
        self.assertIn("请自行核对", text)

    def test_account_without_post_has_no_reference_line(self) -> None:
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={"tier": "account", "handle": "catxiu233", "post_url": "", "reason": "名字对上了"},
            total_candidates=0,
        )
        text = build_text_summary(_task(outcome), command="盆地找鸡")
        self.assertIn("catxiu233", text)
        self.assertNotIn("存疑参考", text)

    def test_reference_image_comes_from_closest_tweet_photo(self) -> None:
        """Grok 帖子核对失败时，参考图用帖子里距离最近的那张，并说明来源。"""
        ref = "https://pbs.twimg.com/media/abc.jpg"
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={
                "tier": "account",
                "handle": "mian84232594",
                "post_url": "https://x.com/mian84232594/status/1",
                "reference_url": ref,
                "distance": 20,
                "reason": "帖子配图对不上（最近距离 20），帖子只当参考",
            },
            image_b64="AAAA",
            image_source=ref,
            total_candidates=0,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("Grok 帖子配图里跟原图最接近", blob)
        # 距离要写在图上，免得把这张图当成结论。
        self.assertIn("指纹距离 20", blob)
        self.assertIn("不是同一张", blob)

    def test_reference_image_reports_candidate_distance(self) -> None:
        """参考图来自候选池时，也要标出它跟原图的距离。"""
        item = Candidate(
            engine="lens",
            url="https://example.com/a",
            image_url="https://img/a.jpg",
            distance=22,
            status="mismatch",
        )
        outcome = SearchOutcome(
            status="not_found",
            best=None,
            candidates=[item],
            image_b64="AAAA",
            image_source="https://img/a.jpg",
            total_candidates=1,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("候选里跟原图最接近的一张", blob)
        self.assertIn("指纹距离 22", blob)

    def test_footer_explains_grok_reference_image(self) -> None:
        ref = "https://pbs.twimg.com/media/abc.jpg"
        outcome = SearchOutcome(
            status="ok",
            best=None,
            candidates=[],
            grok={"tier": "account", "handle": "x", "post_url": "https://x.com/x/status/1",
                  "reference_url": ref, "reason": "对不上"},
            image_b64="AAAA",
            image_source=ref,
            total_candidates=0,
        )
        nodes = build_forward_nodes(_task(outcome), command="盆地找鸡")
        blob = json.dumps(nodes, ensure_ascii=False)
        self.assertIn("Grok 给的帖子里跟原图最接近的一张", blob)


class TwoSuggestionsTest(unittest.TestCase):
    """视觉和 Grok 同时给出结论时，两张图都要发。"""

    def _make_outcome(self, with_grok_image: bool) -> SearchOutcome:
        best = Candidate(
            engine="lens",
            url="https://x.com/a/status/1",
            distance=6,
            status="vision",
            image_url="https://img/vision.jpg",
        )
        return SearchOutcome(
            status="ok",
            best=best,
            candidates=[best],
            image_b64="VIS",
            image_source="https://img/vision.jpg",
            grok_image_b64="GROK" if with_grok_image else "",
            grok={
                "tier": "account",
                "handle": "someone",
                "post_url": "https://x.com/someone/status/2",
                "reference_url": "https://img/grok.jpg",
                "reason": "帖子配图对不上（最近距离 20），帖子只当参考",
            },
            total_candidates=1,
        )

    def test_both_images_are_sent(self) -> None:
        nodes = build_forward_nodes(_task(self._make_outcome(True)), command="盆地找鸡")
        images = [
            part.get("content")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "image"
        ]
        self.assertIn("VIS", images)
        self.assertIn("GROK", images)

    def test_only_vision_image_when_grok_has_none(self) -> None:
        nodes = build_forward_nodes(_task(self._make_outcome(False)), command="盆地找鸡")
        images = [
            part.get("content")
            for node in nodes
            for part in node.get("content") or []
            if part.get("type") == "image"
        ]
        self.assertEqual(images, ["VIS"])

    def test_vision_best_stays_in_reference_groups(self) -> None:
        """结论说「见参考区」，那条就得真的在参考区里。"""
        outcome = self._make_outcome(False)
        groups = engine_groups(outcome, outcome.best, 8)
        flat = [item for _, items in groups for item in items]
        self.assertIn(outcome.best, flat)


class SplitForwardsTest(unittest.TestCase):
    """视觉、Grok、页脚各自成一条，方便对照，不再挤在同一条转发里。"""

    def _make_outcome(self, *, with_grok: bool = True, best: bool = True) -> SearchOutcome:
        item = Candidate(
            engine="baidu",
            url="https://www.douyin.com/note/123",
            distance=27,
            status="vision",
            note="视觉复核：同一个人（距离 27）",
            handle="someone",
        )
        item.finalize()
        outcome = SearchOutcome(
            status="ok",
            best=item if best else None,
            candidates=[item],
            image_b64="VISIMG",
            image_source=item.image_url,
            grok={
                "ok": True,
                "tier": "account" if with_grok else "none",
                "handle": "someone",
                "post_url": "https://x.com/someone/status/1",
                "reference_url": "https://pbs.twimg.com/media/AAA.jpg",
                "distance": 25,
                "reason": "帖子配图对不上（最近距离 25），帖子只当参考",
            }
            if with_grok
            else None,
        )
        outcome.grok_image_b64 = "GROKIMG"
        return outcome

    def test_vision_and_grok_split_into_three_messages(self) -> None:
        blocks = build_result_forwards(_task(self._make_outcome()), command="盆地找鸡")
        self.assertEqual(len(blocks), 3)

        vision_text = blocks[0][0]["content"][0]["content"]
        self.assertIn("视觉复核认为是同一个人", vision_text)
        self.assertIn("可能认错", vision_text)
        self.assertEqual(
            [p["content"] for p in blocks[0][0]["content"] if p["type"] == "image"], ["VISIMG"]
        )

        grok_text = "\n".join(
            part["content"] for part in blocks[1][0]["content"] if part["type"] == "text"
        )
        self.assertIn("Grok 核对", grok_text)
        self.assertIn("someone", grok_text)
        self.assertIn("不是同一张", grok_text)
        self.assertEqual(
            [p["content"] for p in blocks[1][0]["content"] if p["type"] == "image"], ["GROKIMG"]
        )

        footer = blocks[2][0]["content"][0]["content"]
        self.assertIn("任务编号", footer)

    def test_not_found_with_grok_keeps_footer_block_separate(self) -> None:
        outcome = self._make_outcome(best=False)
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        self.assertEqual(len(blocks), 2)
        # Grok 单独一条，尾部那条只讲「没找到」和参考区，不再重复 Grok 结论。
        tail = "\n".join(
            part["content"]
            for node in blocks[1]
            for part in node.get("content") or []
            if part["type"] == "text"
        )
        self.assertIn("未找到可靠出处", tail)
        self.assertNotIn("X 搜索指向这个账号", tail)
        self.assertIn("任务编号", tail)

    def test_no_fallback_content_stays_single_message(self) -> None:
        outcome = self._make_outcome(with_grok=False, best=False)
        outcome.grok = {"ok": True, "tier": "none", "error": ""}
        blocks = build_result_forwards(_task(outcome), command="盆地找鸡")
        self.assertEqual(len(blocks), 1)


if __name__ == "__main__":
    unittest.main()
