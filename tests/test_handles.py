"""从候选标题里抽疑似 X 账号：只测纯函数，联网验证在插件里。"""

from __future__ import annotations

import unittest

from core import handles


class ExtractTest(unittest.TestCase):
    def test_picks_names_from_a_messy_title(self) -> None:
        """搬运站标题里的别名列表要能挑出来，平台和通用词要滤掉。"""
        title = 'Kuuko / kuukow002 / kuuko_w Nude OnlyFans Leak Picture'
        found = [name.lower() for name in handles.extract([title])]
        self.assertIn("kuukow002", found)
        self.assertIn("kuuko_w", found)
        self.assertIn("kuuko", found)
        for junk in ("nude", "onlyfans", "leak", "picture"):
            self.assertNotIn(junk, found)

    def test_skips_role_and_platform_words(self) -> None:
        title = "KuukoW - Nahida 18+ Cosplay (Genshin Impact), 35 Photos"
        found = [name.lower() for name in handles.extract([title])]
        self.assertIn("kuukow", found)
        for junk in ("nahida", "cosplay", "genshin", "impact", "photos"):
            self.assertNotIn(junk, found)

    def test_dedupes_and_respects_limit(self) -> None:
        texts = ["alpha_one beta_two", "alpha_one gamma_three", "delta_four"]
        found = handles.extract(texts, limit=2)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].lower(), "alpha_one")

    def test_ignores_repeated_characters(self) -> None:
        self.assertEqual(handles.extract(["xxxxx aaaaa"]), [])

    def test_skips_plain_english_words(self) -> None:
        """实测：'Nothing To Hide (Apron)' 抽出 106 万粉的 @nothing，跟图无关。"""
        found = [name.lower() for name in handles.extract(["Nothing To Hide (Apron) by Annham"])]
        self.assertNotIn("nothing", found)
        self.assertNotIn("apron", found)
        self.assertEqual(handles.extract(["Design"]), [])


class MatchScoreTest(unittest.TestCase):
    def test_trailing_digits_fall_back_to_the_stem(self) -> None:
        """kuukow002 的主干 kuukow 出现在 KuukoW 里就算命中。"""
        texts = ["KuukoW - Nahida 18+ Cosplay"]
        self.assertEqual(handles.stem("kuukow002"), "kuukow")
        self.assertGreater(handles.match_score("kuukow002", texts), 0)

    def test_unknown_name_scores_zero(self) -> None:
        self.assertEqual(handles.match_score("someone999", ["KuukoW - Cosplay"]), 0)

    def test_too_short_stem_is_rejected(self) -> None:
        """主干太短的名字误报率太高，直接不算。"""
        self.assertEqual(handles.match_score("abc1", ["abc def"]), 0)

    def test_common_words_do_not_count(self) -> None:
        """4~5 字的常用词不算：站名和作品名会把无关账号顶上来。"""
        # jxporn.net 的「精选Porn」里抽出来的 @porn 是个 9.8 万粉成人号。
        self.assertEqual(handles.match_score("porn", ["精选Porn"]), 0)
        # 「Ghost Sweeper Mikami」是动画名，@Ghost 有 5 万粉但跟 coser 无关。
        self.assertEqual(handles.match_score("ghost", ["Ghost Sweeper Mikami"]), 0)

    def test_site_category_words_are_not_extracted(self) -> None:
        found = [name.lower() for name in handles.extract(["精选Porn hentai"])]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()


class AnchorTest(unittest.TestCase):
    """推出来的名字必须和「已经靠图像对上」的那几条对得上。"""

    def test_nene_family_matches(self) -> None:
        anchors = ["NENE - Sunna Maid-erohere24 | NENE - Sunna Maid Cosplay"]
        self.assertTrue(handles.shares_anchor("nenechi890", anchors))
        self.assertTrue(handles.shares_anchor("nenechi", anchors))

    def test_kuukow_family_matches(self) -> None:
        anchors = ["KuukoW - Nahida 18+ Cosplay, 35 photos"]
        self.assertTrue(handles.shares_anchor("kuukow002", anchors))

    def test_unrelated_artist_is_dropped(self) -> None:
        """skeb 那条接稿人的名字跟这张图没关系，不能顶成疑似账号。"""
        anchors = ["NENE - Sunna Maid-erohere24 | NENE - Sunna Maid Cosplay"]
        self.assertFalse(handles.shares_anchor("murabitoufu", anchors))

    def test_empty_anchors(self) -> None:
        self.assertFalse(handles.shares_anchor("murabitoufu", []))

    def test_alias_in_same_title_is_kept(self) -> None:
        """同一条标题里并列的别名可以采信：NERUchi890 跟 NENEchi890 是同一个人。"""
        anchors = ["NENE - Sunna Maid-erohere24 | NENE - Sunna Maid Cosplay"]
        texts = ["NENEchi890, NERUchi890 #1304 - Cosplay Girl Porn - Fapello"]
        self.assertFalse(handles.shares_anchor("neruchi890", anchors))
        self.assertTrue(handles.supported_by_context("neruchi890", texts, anchors))

    def test_lone_handle_title_is_refused(self) -> None:
        """标题只有 @murabitoufu、同条里没有任何已命中名字，不能采信。"""
        anchors = ["NENE - Sunna Maid-erohere24"]
        self.assertFalse(
            handles.supported_by_context("murabitoufu", ["@murabitoufu"], anchors)
        )


class BlockedSourceTest(unittest.TestCase):
    """约稿平台、电商页的标题写的是页面主人，不能拿来推作者。"""

    def test_skeb_blocked(self) -> None:
        self.assertTrue(handles.is_blocked_source("skeb.jp"))
        self.assertTrue(handles.is_blocked_source("www.skeb.jp"))

    def test_ecommerce_blocked(self) -> None:
        self.assertTrue(handles.is_blocked_source("joom.com"))
        self.assertTrue(handles.is_blocked_source("aliexpress.ru"))

    def test_normal_site_kept(self) -> None:
        self.assertFalse(handles.is_blocked_source("galleryepic.com"))
        self.assertFalse(handles.is_blocked_source("masterfap.net"))
        self.assertFalse(handles.is_blocked_source(""))
