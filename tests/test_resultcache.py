"""按图缓存：键、过期、淘汰与落盘。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.resultcache import (  # noqa: E402
    ResultCache,
    cache_get,
    cache_key,
    cache_put,
    load_store,
    save_store,
)

NOW = 1_800_000_000.0
DAY = 86400.0


class CacheKeyTest(unittest.TestCase):
    def test_same_image_same_key(self) -> None:
        self.assertEqual(cache_key(b"abc", "v1"), cache_key(b"abc", "v1"))

    def test_version_bump_invalidates(self) -> None:
        self.assertNotEqual(cache_key(b"abc", "v1"), cache_key(b"abc", "v2"))

    def test_different_image_different_key(self) -> None:
        self.assertNotEqual(cache_key(b"abc", "v1"), cache_key(b"abd", "v1"))


class CacheGetTest(unittest.TestCase):
    def _store(self, status: str, at: float) -> dict:
        return {"k": {"at": at, "result": {"status": status}}}

    def test_fresh_hit(self) -> None:
        hit = cache_get(self._store("ok", NOW - 10 * DAY), "k", NOW, 30 * DAY)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertAlmostEqual(hit.age_seconds, 10 * DAY)

    def test_hit_expires_after_ttl(self) -> None:
        self.assertIsNone(cache_get(self._store("ok", NOW - 31 * DAY), "k", NOW, 30 * DAY))

    def test_miss_entries_are_ignored(self) -> None:
        """旧版本可能存过「没找到」，读到时当没命中。"""
        self.assertIsNone(cache_get(self._store("not_found", NOW), "k", NOW, 30 * DAY))

    def test_unknown_key(self) -> None:
        self.assertIsNone(cache_get({}, "nope", NOW))

    def test_broken_entry(self) -> None:
        self.assertIsNone(cache_get({"k": {"at": NOW}}, "k", NOW))
        self.assertIsNone(cache_get({"k": "not-a-dict"}, "k", NOW))


class CachePutTest(unittest.TestCase):
    def test_put_replaces(self) -> None:
        store = cache_put({}, "k", {"status": "ok"}, NOW - 5)
        store = cache_put(store, "k", {"status": "not_found"}, NOW)
        self.assertEqual(store["k"]["result"]["status"], "not_found")

    def test_prune_keeps_newest(self) -> None:
        store: dict = {}
        for index in range(5):
            store = cache_put(store, f"k{index}", {"status": "ok"}, NOW + index, max_entries=3)
        self.assertEqual(sorted(store), ["k2", "k3", "k4"])

    def test_input_not_mutated(self) -> None:
        original: dict = {}
        cache_put(original, "k", {"status": "ok"}, NOW)
        self.assertEqual(original, {})


class StoreFileTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            save_store(path, {"k": {"at": NOW, "result": {"status": "ok"}}})
            self.assertEqual(load_store(path)["k"]["result"]["status"], "ok")

    def test_broken_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_store(path), {})

    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_store(Path(tmp) / "nope.json"), {})

    def test_result_cache_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResultCache(tmp, ttl_seconds=30 * DAY)
            cache.put("k", {"status": "ok"})
            self.assertEqual(cache.size, 1)
            reopened = ResultCache(tmp, ttl_seconds=30 * DAY)
            hit = reopened.get("k")
            self.assertIsNotNone(hit)
