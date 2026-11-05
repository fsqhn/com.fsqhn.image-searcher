"""FixTweet（fxtwitter）只读 API：已知推文就拉元数据与配图直链。

为什么要有这一层：Grok 偶尔会给一条对不上的链接，甚至编一条不存在的帖子。
光看到 ``x.com/xxx/status/...`` 就当结论，会把用户带到别人的帖子上。拿到推文
ID 之后调这里，把帖子里的图取回来跟用户发的图做 dHash，对得上才算真找到。

这个 API 不需要登录态，也不用 Cookie；同样的能力还有 vxtwitter，字段接近。
全部是纯函数，网络请求放在 plugin.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

API_BASE = "https://api.fxtwitter.com"

_STATUS_PATH = re.compile(r"status/(\d{10,25})")
_BARE_ID = re.compile(r"^\d{10,25}$")
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,20}$")


@dataclass
class Tweet:
    """一条推文里我们关心的东西。"""

    ok: bool = False
    error: str = ""
    tweet_id: str = ""
    url: str = ""
    handle: str = ""
    name: str = ""
    text: str = ""
    media: list[str] = field(default_factory=list)
    protected: bool = False
    created_at: str = ""
    # 引用帖（quote）指向的那条原帖。引用帖自己没有配图时，图上一定来自原帖，
    # 结论要换成原帖作者——之前 YuMIkAnime 引用 @qyuuuz 的图吐槽，被当成了作者。
    quote_url: str = ""
    quote_id: str = ""
    quote_handle: str = ""
    quote_name: str = ""
    quote_text: str = ""


def parse_tweet_id(text: str) -> str:
    """从链接、带评论的分享文案或裸数字里取出推文 ID；取不到返回空串。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    found = _STATUS_PATH.search(raw)
    if found:
        return found.group(1)
    # 用户可能直接贴一串 ID，也可能写成「xid 2105…」之后的参数。
    for token in raw.split():
        if _BARE_ID.match(token):
            return token
    return ""


def build_api_url(tweet_id: str, handle: str = "") -> str:
    """拼 API 地址。带用户名更贴近官方路径；没有就用 /i/status/<id>。"""
    ident = (tweet_id or "").strip()
    user = (handle or "").strip().lstrip("@")
    if user and _HANDLE.match(user):
        return f"{API_BASE}/{user}/status/{ident}"
    return f"{API_BASE}/i/status/{ident}"


def parse_tweet(payload: dict | None, *, tweet_id: str = "") -> Tweet:
    """把 API 返回的 JSON 变成 Tweet。code 不是 200 或没有 tweet 就算失败。"""
    if not isinstance(payload, dict):
        return Tweet(error="返回不是 JSON 对象", tweet_id=tweet_id)
    code = payload.get("code")
    if code not in (None, 200):
        message = str(payload.get("message") or "").strip()
        text = f"{message}（code {code}）" if message else f"code={code}"
        return Tweet(error=text, tweet_id=tweet_id)
    data = payload.get("tweet")
    if not isinstance(data, dict):
        return Tweet(error="返回里没有 tweet", tweet_id=tweet_id)

    author = data.get("author") or {}
    handle = str(author.get("screen_name") or "").strip()
    name = str(author.get("name") or "").strip()
    media = data.get("media") or {}
    urls: list[str] = []
    for photo in media.get("photos") or []:
        url = str((photo or {}).get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    # 视频只留海报图：海报能和原图比指纹，视频本身比不了。
    for video in media.get("videos") or []:
        url = str((video or {}).get("thumbnail_url") or "").strip()
        if url and url not in urls:
            urls.append(url)

    quote = data.get("quote") if isinstance(data.get("quote"), dict) else {}
    quote_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}

    return Tweet(
        ok=True,
        tweet_id=str(data.get("id") or tweet_id),
        url=str(data.get("url") or ""),
        handle=handle,
        name=name,
        text=str(data.get("text") or ""),
        media=urls,
        protected=bool(author.get("protected")),
        created_at=str(data.get("created_at") or ""),
        quote_url=str(quote.get("url") or ""),
        quote_id=str(quote.get("id") or ""),
        quote_handle=str(quote_author.get("screen_name") or "").strip(),
        quote_name=str(quote_author.get("name") or "").strip(),
        quote_text=str(quote.get("text") or ""),
    )


def judge(distances: list[int], threshold: int) -> str:
    """按指纹距离判定这条帖子是不是原图：hit / miss / unknown。

    一张图都没下到或者都解不出指纹时返回 unknown——那种情况不能当成「不是」，
    否则图床 403 一次就会把真结果打成假的。
    """
    usable = [d for d in distances if isinstance(d, int) and d >= 0]
    if not usable:
        return "unknown"
    return "hit" if min(usable) <= max(0, threshold) else "miss"


@dataclass
class MediaPost:
    """账号「自己的」一条带图帖子（``/2/profile/<id>/media`` 里的一项）。"""

    tweet_id: str = ""
    url: str = ""
    text: str = ""
    created_at: str = ""
    media: list[str] = field(default_factory=list)


def build_media_url(handle: str) -> str:
    """账号最近的带图帖子列表。只包含它自己发的，不含转发。"""
    user = (handle or "").strip().lstrip("@")
    if not user or not _HANDLE.match(user):
        return ""
    return f"{API_BASE}/2/profile/{user}/media"


def parse_media_list(payload: dict | None) -> list[MediaPost]:
    """把 ``/media`` 的返回拆成一串帖子。

    这个接口是判断「图是不是她自己发的」唯一不花钱的办法：转发页里的图不会
    出现在列表里，只有亲手发过的才会。所以列表为空就是「她自己没发过」，不能
    当成「查不到」——但调用方要自己记住窗口只有最近十几条。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    posts: list[MediaPost] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        urls: list[str] = []
        media = item.get("media")
        if isinstance(media, dict):
            for entry in media.get("all") or []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("type") or "") not in ("photo", "video", "gif"):
                    continue
                link = str(entry.get("url") or "").strip()
                if link.startswith("http"):
                    urls.append(link)
        if not urls:
            continue
        tweet_id = str(item.get("id") or "")
        posts.append(
            MediaPost(
                tweet_id=tweet_id,
                url=str(item.get("url") or "") or (f"https://x.com/i/status/{tweet_id}" if tweet_id else ""),
                text=str(item.get("text") or ""),
                created_at=str(item.get("created_at") or ""),
                media=urls,
            )
        )
    return posts
