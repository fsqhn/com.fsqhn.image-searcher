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


if __name__ == "__main__":
    unittest.main()
