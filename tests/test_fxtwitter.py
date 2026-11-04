"""fxtwitter（FixTweet）解析：从链接/ID 认出推文，把返回的 JSON 变成可核对的数据。"""

from __future__ import annotations

import unittest

from core import fxtwitter


class ParseTweetIdTest(unittest.TestCase):
    def test_plain_status_url(self) -> None:
        self.assertEqual(
            fxtwitter.parse_tweet_id("https://x.com/muyuqwq233/status/2105595040806707433"),
            "2105595040806707433",
        )

    def test_url_with_query_and_trailing_text(self) -> None:
        text = "看这个 https://twitter.com/a/status/1234567890123456789?s=20 哈哈"
        self.assertEqual(fxtwitter.parse_tweet_id(text), "1234567890123456789")

    def test_bare_id(self) -> None:
        self.assertEqual(fxtwitter.parse_tweet_id("2105595040806707433"), "2105595040806707433")

    def test_internal_link_without_handle(self) -> None:
        """x.com/i/status/<id> 是 Grok 最常给的形式，也得能取出 ID。"""
        self.assertEqual(
            fxtwitter.parse_tweet_id("https://x.com/i/status/2104951546119410067"),
            "2104951546119410067",
        )

    def test_short_number_is_not_an_id(self) -> None:
        self.assertEqual(fxtwitter.parse_tweet_id("12345"), "")

    def test_empty(self) -> None:
        self.assertEqual(fxtwitter.parse_tweet_id(""), "")


class BuildApiUrlTest(unittest.TestCase):
    def test_without_handle_uses_i_path(self) -> None:
        self.assertEqual(
            fxtwitter.build_api_url("2105595040806707433"),
            "https://api.fxtwitter.com/i/status/2105595040806707433",
        )

    def test_with_handle(self) -> None:
        self.assertEqual(
            fxtwitter.build_api_url("2105595040806707433", "@muyuqwq233"),
            "https://api.fxtwitter.com/muyuqwq233/status/2105595040806707433",
        )


class ParseTweetTest(unittest.TestCase):
    payload = {
        "code": 200,
        "message": "OK",
        "tweet": {
            "id": "2105595040806707433",
            "url": "https://x.com/muyuqwq233/status/2105595040806707433",
            "text": "之前的自己腿好瘦 现在好粗呜呜呜 #男娘",
            "author": {
                "screen_name": "muyuqwq233",
                "name": "暮雨不是笨蛋雨喵",
                "protected": False,
            },
            "media": {
                "photos": [
                    {"url": "https://pbs.twimg.com/media/HTiThDBbkAAvqrE.jpg?name=orig"},
                    {"url": "https://pbs.twimg.com/media/HTiThmmboAASCMe.jpg?name=orig"},
                ]
            },
        },
    }

    def test_parses_author_and_media(self) -> None:
        tweet = fxtwitter.parse_tweet(self.payload)
        self.assertTrue(tweet.ok)
        self.assertEqual(tweet.handle, "muyuqwq233")
        self.assertEqual(tweet.name, "暮雨不是笨蛋雨喵")
        self.assertEqual(len(tweet.media), 2)
        self.assertFalse(tweet.protected)

    def test_video_uses_thumbnail(self) -> None:
        payload = {
            "code": 200,
            "tweet": {
                "id": "1",
                "author": {"screen_name": "a"},
                "media": {"videos": [{"thumbnail_url": "https://pbs.twimg.com/t.jpg"}]},
            },
        }
        tweet = fxtwitter.parse_tweet(payload)
        self.assertEqual(tweet.media, ["https://pbs.twimg.com/t.jpg"])

    def test_error_code(self) -> None:
        tweet = fxtwitter.parse_tweet({"code": 404, "message": "Tweet not found"})
        self.assertFalse(tweet.ok)
        self.assertIn("404", tweet.error)

    def test_missing_tweet(self) -> None:
        self.assertFalse(fxtwitter.parse_tweet({"code": 200}).ok)
        self.assertFalse(fxtwitter.parse_tweet(None).ok)


class JudgeTest(unittest.TestCase):
    """链接核对的三态：命中、对不上、没法判断。"""

    def test_hit(self) -> None:
        self.assertEqual(fxtwitter.judge([1, 30], 8), "hit")

    def test_miss(self) -> None:
        self.assertEqual(fxtwitter.judge([12, 34], 8), "miss")

    def test_unknown_when_no_distance(self) -> None:
        self.assertEqual(fxtwitter.judge([], 8), "unknown")
        self.assertEqual(fxtwitter.judge([-1], 8), "unknown")
