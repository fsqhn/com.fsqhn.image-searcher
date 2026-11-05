"""用同一条外链判断两个 X 号是同一个人：只测纯函数。"""

from __future__ import annotations

import unittest

from core import same_person


class ExternalLinksTest(unittest.TestCase):
    def test_website_and_description(self) -> None:
        user = {
            "website": {"url": "https://www.patreon.com/NENE318?utm_campaign=x"},
            "description": "patreon: https://www.patreon.com/NENE318",
        }
        self.assertEqual(same_person.external_links(user), ["patreon.com/NENE318"])

    def test_self_links_are_dropped(self) -> None:
        """指向 X 自己的链接是账号本身，不能当身份证据。"""
        user = {"website": None, "description": "follow https://x.com/haru007312 and https://twitter.com/a"}
        self.assertEqual(same_person.external_links(user), [])

    def test_bare_domain_is_ignored(self) -> None:
        user = {"website": None, "description": "https://onlyfans.com/ 有空来看看"}
        self.assertEqual(same_person.external_links(user), [])


class FindSharedTest(unittest.TestCase):
    def test_two_accounts_sharing_one_patreon(self) -> None:
        """实测：@NENEchi8890 和 @haru007312 挂的是同一个 patreon.com/NENE318。"""
        profiles = {
            "NENEchi8890": {
                "website": {"url": "https://www.patreon.com/NENE318"},
                "description": "",
            },
            "haru007312": {
                "website": None,
                "description": "patreon: https://www.patreon.com/NENE318?utm_campaign=creatorshare",
            },
            "someone_else": {"website": {"url": "https://www.patreon.com/OTHER1"}, "description": ""},
        }
        groups = same_person.find_shared(profiles)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["link"], "patreon.com/NENE318")
        self.assertEqual(groups[0]["handles"], ["haru007312", "nenechi8890"])

    def test_no_group_without_a_shared_link(self) -> None:
        profiles = {
            "a_one": {"website": {"url": "https://patreon.com/A1"}, "description": ""},
            "b_two": {"website": {"url": "https://patreon.com/B2"}, "description": ""},
        }
        self.assertEqual(same_person.find_shared(profiles), [])

    def test_anchor_filters_unrelated_group(self) -> None:
        """图对不上的两个号互相共用外链，但跟这张图无关，不能挂到结论下面。"""
        profiles = {
            "maro2ki3": {"website": {"url": "https://patreon.com/MARO2"}, "description": ""},
            "iruri_rapulras": {"website": {"url": "https://patreon.com/YUIYUI8754"}, "description": ""},
            "yunyun8754": {"website": {"url": "https://patreon.com/YUIYUI8754"}, "description": ""},
        }
        self.assertEqual(same_person.find_shared(profiles, anchors={"maro2ki3"}), [])

    def test_anchor_keeps_its_own_group(self) -> None:
        profiles = {
            "neruchi890": {"website": {"url": "https://patreon.com/NENE318"}, "description": ""},
            "haru007312": {"website": {"url": "https://patreon.com/NENE318"}, "description": ""},
            "iruri_rapulras": {"website": {"url": "https://patreon.com/YUIYUI8754"}, "description": ""},
            "yunyun8754": {"website": {"url": "https://patreon.com/YUIYUI8754"}, "description": ""},
        }
        groups = same_person.find_shared(profiles, anchors={"neruchi890"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["link"], "patreon.com/NENE318")
        self.assertEqual(groups[0]["handles"], ["haru007312", "neruchi890"])

    def test_empty_anchor_set_reports_nothing(self) -> None:
        profiles = {
            "a_one": {"website": {"url": "https://patreon.com/A1"}, "description": ""},
            "b_two": {"website": {"url": "https://patreon.com/A1"}, "description": ""},
        }
        self.assertEqual(same_person.find_shared(profiles, anchors=set()), [])


class EligibleTest(unittest.TestCase):
    """参与归并的号必须先跟这张图有关系。"""

    def test_all_mismatch_is_not_enough(self) -> None:
        """s-20261105-1404 的实况：两个图对不上的号被凑成「同一个人」。"""
        pairs = [
            ("iruri_rapulras", "mismatch"),
            ("yunyun8754", "mismatch"),
        ]
        self.assertEqual(same_person.eligible(pairs), [])

    def test_mismatch_sibling_still_counts_when_anchored(self) -> None:
        """同一个人的另一个号发的往往是别的图，距离对不上很正常，不能剔掉。"""
        pairs = [("NENEchi8890", "vision"), ("haru007312", "mismatch")]
        self.assertEqual(same_person.eligible(pairs), ["nenechi8890", "haru007312"])

    def test_same_handle_takes_best_status(self) -> None:
        """同一个号出现两次（一次图对不上、一次对上），按对上的那次算。"""
        pairs = [("NENE", "mismatch"), ("nene", "match"), ("other_one", "unverified")]
        self.assertEqual(same_person.eligible(pairs), ["nene", "other_one"])

    def test_all_unverified_is_not_enough(self) -> None:
        pairs = [("a_one", "unverified"), ("b_two", "unverified")]
        self.assertEqual(same_person.eligible(pairs), [])


if __name__ == "__main__":
    unittest.main()
