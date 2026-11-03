"""配置迁移：老配置 [plugin] 下的 vision_* / grok_* 要搬进独立配置节。

纯函数，不碰插件上下文。改映射表先改这里的用例。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import LEGACY_SECTIONS, migrate_legacy_sections  # noqa: E402

LEGACY_GROK = {
    "grok_fallback": True,
    "grok_base_url": "https://example.com/v1",
    "grok_api_key": "sk-test",
    "grok_model": "grok-4.7",
    "grok_timeout": 120,
}

LEGACY_VISION = {
    "vision_fallback": True,
    "vision_top_n": 16,
    "vision_task": "vlm",
    "vision_model": "Qwen/Qwen3-VL-32B-Instruct",
    "vision_timeout": 90,
}


class MigrateLegacySectionsTest(unittest.TestCase):
    def test_mapping_covers_every_legacy_field(self) -> None:
        self.assertEqual(set(LEGACY_SECTIONS["vision"][1]), {"fallback", "top_n", "task", "model", "timeout"})
        self.assertEqual(
            set(LEGACY_SECTIONS["grok"][1]),
            {"fallback", "base_url", "api_key", "model", "timeout"},
        )

    def test_migrates_grok_into_own_section(self) -> None:
        raw = {"plugin": {"command": "麦麦寻人", **LEGACY_GROK}}

        data, changed = migrate_legacy_sections(raw)

        self.assertTrue(changed)
        self.assertEqual(
            data["grok"],
            {
                "enabled": True,
                "base_url": "https://example.com/v1",
                "api_key": "sk-test",
                "model": "grok-4.7",
                "timeout": 120,
            },
        )
        self.assertEqual(data["plugin"], {"command": "麦麦寻人"})

    def test_migrates_vision_into_own_section(self) -> None:
        raw = {"plugin": dict(LEGACY_VISION)}

        data, changed = migrate_legacy_sections(raw)

        self.assertTrue(changed)
        self.assertEqual(
            data["vision"],
            {
                "enabled": True,
                "top_n": 16,
                "task": "vlm",
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "timeout": 90,
            },
        )
        self.assertEqual(data["plugin"], {})

    def test_second_pass_changes_nothing(self) -> None:
        raw = {"plugin": {**LEGACY_GROK, **LEGACY_VISION}}

        once, _ = migrate_legacy_sections(raw)
        twice, changed = migrate_legacy_sections(once)

        self.assertFalse(changed)
        self.assertEqual(twice, once)

    def test_existing_section_value_wins(self) -> None:
        raw = {"plugin": dict(LEGACY_GROK), "grok": {"api_key": "sk-new", "enabled": False}}

        data, changed = migrate_legacy_sections(raw)

        self.assertTrue(changed)
        self.assertEqual(data["grok"]["api_key"], "sk-new")
        self.assertEqual(data["grok"]["enabled"], False)
        self.assertEqual(data["grok"]["base_url"], "https://example.com/v1")

    def test_unknown_plugin_keys_are_kept(self) -> None:
        raw = {"plugin": {"command": "麦麦寻人", "vision_fallback": False, "vision_whatever": 1}}

        data, changed = migrate_legacy_sections(raw)

        self.assertTrue(changed)
        # 没映射的老键直接丢掉，不往新配置节里塞无名字段。
        self.assertEqual(data["plugin"], {"command": "麦麦寻人"})
        self.assertEqual(data["vision"], {"enabled": False})

    def test_missing_plugin_section(self) -> None:
        raw = {"engines": {"priority": ["lens"]}}

        data, changed = migrate_legacy_sections(raw)

        self.assertFalse(changed)
        self.assertEqual(data, raw)

    def test_non_mapping_input(self) -> None:
        for value in (None, [], "x"):
            with self.subTest(value=value):
                self.assertEqual(migrate_legacy_sections(value), ({}, False))

    def test_original_dict_is_not_mutated(self) -> None:
        plugin = dict(LEGACY_GROK)
        raw = {"plugin": plugin}

        migrate_legacy_sections(raw)

        self.assertEqual(plugin, LEGACY_GROK)


if __name__ == "__main__":
    unittest.main()
