"""SauceNAO 的库策略：按群在「全量」和「收紧」之间切换。"""

from __future__ import annotations

import unittest

from core.engines import group_listed, resolve_saucenao_db

FULL = "999"
STRICT = "5,6,21,24"


def _db(**kwargs):
    params = dict(
        db_full=FULL,
        db_strict=STRICT,
        policy_enabled=True,
        invert=False,
        groups=["123456789"],
        stream_id="d8dc875f91b84fdbf837b083b49ae51b",
        group_id="999999999",
    )
    params.update(kwargs)
    return resolve_saucenao_db(**params)


class DbPolicyTest(unittest.TestCase):
    def test_disabled_always_full(self) -> None:
        self.assertEqual(_db(policy_enabled=False), FULL)

    def test_empty_list_means_everyone_unlisted(self) -> None:
        self.assertEqual(_db(groups=[]), STRICT)
        self.assertEqual(_db(groups=[], invert=True), FULL)

    def test_listed_group_uses_full_by_default(self) -> None:
        self.assertEqual(_db(group_id="123456789"), FULL)

    def test_unlisted_group_is_strict_by_default(self) -> None:
        self.assertEqual(_db(), STRICT)

    def test_invert_flips_meaning(self) -> None:
        self.assertEqual(_db(group_id="123456789", invert=True), STRICT)
        self.assertEqual(_db(invert=True), FULL)

    def test_stream_id_can_be_used_in_list(self) -> None:
        stream = "d8dc875f91b84fdbf837b083b49ae51b"
        self.assertTrue(group_listed([stream], stream, "999999999"))
        self.assertEqual(_db(groups=[stream]), FULL)

    def test_group_id_with_g_prefix(self) -> None:
        self.assertTrue(group_listed(["g123456789"], "d8dc", "123456789"))

    def test_blank_entries_are_ignored(self) -> None:
        self.assertFalse(group_listed(["", "  "], "d8dc", "123456789"))


if __name__ == "__main__":
    unittest.main()
