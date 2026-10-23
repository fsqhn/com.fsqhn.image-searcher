"""合并转发节点拼装：重点是没找到匹配时也要带一张参考图。"""

from __future__ import annotations

import unittest

from core.engines import Candidate
from core.formatting import build_forward_nodes
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

    def _grouped_outcome(self) -> SearchOutcome:
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
            status="ok",
            best=best,
            candidates=[lens_a, lens_b, yandex_a, best],
            image_b64="BEST",
            image_source=best.image_url,
        )

    def test_reference_is_grouped_by_engine(self) -> None:
        """参考区按引擎分开，每个引擎一个节点，标题带引擎名和条数。"""
        nodes = build_forward_nodes(_task(self._grouped_outcome()), command="麦麦寻人")
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

    def test_first_reply_carries_one_image_per_engine(self) -> None:
        """首次回复：每个引擎最接近的那张带图，其余候选只列文字。"""
        outcome = self._grouped_outcome()
        outcome.images = {"https://img.example/1.jpg": "LENS1", "https://img.example/3.jpg": "YANDEX1"}
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人")
        self.assertEqual(_images(nodes), [(1, "BEST"), (2, "LENS1"), (3, "YANDEX1")])

    def test_more_reply_carries_every_candidate_image(self) -> None:
        outcome = self._grouped_outcome()
        outcome.images = {
            "https://img.example/1.jpg": "LENS1",
            "https://img.example/2.jpg": "LENS2",
            "https://img.example/3.jpg": "YANDEX1",
        }
        nodes = build_forward_nodes(_task(outcome), command="麦麦寻人", limit=10, replay=True)
        self.assertEqual(
            _images(nodes),
            [(1, "BEST"), (2, "LENS1"), (2, "LENS2"), (3, "YANDEX1")],
        )

    def test_engine_limit_caps_candidates_per_engine(self) -> None:
        nodes = build_forward_nodes(_task(self._grouped_outcome()), command="麦麦寻人", limit=1)
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


if __name__ == "__main__":
    unittest.main()
