"""从泄露站的收录页读出「这个人有 OnlyFans 付费内容」。

搬运站和泄露站按名字建页：leakgallery.com/<名字> 有页就说明站方收录过这个
人的付费内容，没页返回 404。这不是官方数据，但比从标题里瞎猜靠谱——先要求
名字原样出现在候选标题里，再要求站上有页，两道都过才采信，结果里仍然只标
「疑似」。

这里只做纯函数：把页面读成结构化信息，把 X 资料里挂的 IG 抠出来。联网由
插件负责。
"""

from __future__ import annotations

import re

_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
# 标题格式固定成「名字 / 说明」或「名字 / 标签 / 说明」，名字可能是空格分开的两个词。
_DESC_IG = re.compile(r"(?:ig|instagram)\s*[:：]\s*@?([A-Za-z0-9_.]{2,30})", re.I)
_URL_IG = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def parse_page(html: str, handle: str = "") -> dict | None:
    """认出「这个页面是收费内容的收录页」，返回称呼。

    站方标题有两种格式：「名字 / 说明」是作者页，「名字 / 标签 / 说明」是标签页。
    标签页必须排除——`leakgallery.com/Vtuber` 返回的是「Rayariot / Vtuber / …」，
    名字其实是 Rayariot，跟 Vtuber 毫无关系。404 页的标题里没有 OnlyFans 字样，
    同一条判据就能把三种情况分开。
    """
    match = _TITLE.search(html or "")
    if not match:
        return None
    title = " ".join(match.group(1).split())
    parts = [part.strip() for part in title.split("/")]
    if len(parts) != 2:
        return None
    if "onlyfans" not in parts[1].lower():
        return None
    name = parts[0]
    if not name or (handle and not name_matches(handle, name)):
        return None
    return {"name": name[:40], "title": title[:80]}


def name_matches(handle: str, name: str) -> bool:
    """页面上写的名字要和查的那个 slug 对得上。

    去掉非字母数字再比：`dreamtamu` 对得上「Dream Tamu」。结尾数字也去掉，
    `kuukow002` 的 slug 对上「Kuuko」也算。
    """
    core = _squash(handle)
    core = re.sub(r"[0-9]+$", "", core)
    squashed = _squash(name)
    return bool(core) and bool(squashed) and core in squashed


def instagram_of(user: dict | None) -> str:
    """从 X 资料里找 Instagram 主页。没有就返回空串。"""
    user = user or {}
    website = user.get("website")
    if isinstance(website, dict):
        found = _URL_IG.search(str(website.get("url") or ""))
        if found:
            return f"https://instagram.com/{found.group(1).rstrip('.')}"
    found = _DESC_IG.search(str(user.get("description") or ""))
    if not found:
        return ""
    handle = found.group(1).rstrip(".")
    if len(handle) < 2:
        return ""
    return f"https://instagram.com/{handle}"
