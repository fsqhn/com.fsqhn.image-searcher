"""从结果链接里提取平台、名字与账号。

不抓页面：x.com 需要登录且靠 JS 渲染，handle 直接从 URL 或标题里取更快更稳。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

SOTWE_URL_RE = re.compile(r"^https?://(?:www\.)?sotwe\.com/([A-Za-z0-9_]+)", re.I)
X_STATUS_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})(?:/status/|/?)", re.I)
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,20})")
SOTWE_TITLE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(?:Twitter|X).*$", re.I)
BILIBILI_SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)")
BILIBILI_VIDEO_RE = re.compile(r"bilibili\.com/video/(BV[A-Za-z0-9]+)")
DOUYIN_USER_RE = re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]+)")
WEIBO_UID_RE = re.compile(r"weibo\.(?:com|cn)/(?:u/)?(\d{5,})")

HIGH_VALUE_HOSTS = (
    "sotwe.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "weibo.com",
    "weibo.cn",
    "douyin.com",
    "bilibili.com",
    "tiktok.com",
    "telegra.ph",
    "v2ph",
    "ocosplay",
    "fanbox",
    "patreon.com",
)
LOW_VALUE_HOSTS = ("pinterest.", "google.", "bing.", "gstatic.")


def promote_sotwe(url: str, title: str = "") -> tuple[str, str]:
    """把 sotwe 镜像链接还原成 x.com 主页，并校正标题。

    返回 (url, title)。标题里的 @handle 比 sotwe 路径可靠，路径有时会多带下划线。
    """
    match = SOTWE_URL_RE.match(url or "")
    if not match:
        return url, title
    cleaned = SOTWE_TITLE_SUFFIX_RE.sub("", title or "").strip()
    found = HANDLE_RE.search(cleaned)
    handle = found.group(1) if found else match.group(1).rstrip("_")
    return f"https://x.com/{handle}", cleaned


def is_x_platform(platform: str) -> bool:
    """是否为 X / Twitter 结果。

    用于「指纹距离相同优先用 X」的排序规则，只认平台名，不猜链接。
    """
    text = (platform or "").strip().lower()
    return "twitter" in text or text in {"x", "x.com"}


def platform_of(url: str = "", source: str = "", title: str = "") -> str:
    """判断结果所在平台。"""
    host = (urlparse(url).netloc or source or "").lower()
    if any(token in host for token in ("sotwe.com", "twitter.com", "x.com")):
        return "X (Twitter)"
    if "bilibili.com" in host or "b23.tv" in host:
        return "B站"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "抖音"
    if "weibo.com" in host or "weibo.cn" in host:
        return "微博"
    if "instagram.com" in host:
        return "Instagram"
    if "tiktok.com" in host:
        return "TikTok"
    if "pinterest." in host:
        return "Pinterest"
    if "telegra.ph" in host:
        return "Telegram"
    if "fanbox.cc" in host:
        return "Pixiv FANBOX"
    if "patreon.com" in host:
        return "Patreon"
    if host:
        return host.removeprefix("www.")
    if title:
        return "网页"
    return "未知来源"


def extract_identity(url: str, title: str = "") -> tuple[str, str]:
    """返回 (名字, 账号)。

    账号优先从链接里取；X 的结果链接本身就是账号或推文页，最可靠。
    """
    clean_title = SOTWE_TITLE_SUFFIX_RE.sub("", title or "").strip()
    handle = ""

    x_match = X_STATUS_RE.match(url or "")
    if x_match:
        handle = "@" + x_match.group(1)
    elif BILIBILI_SPACE_RE.search(url or ""):
        handle = "UID " + BILIBILI_SPACE_RE.search(url or "").group(1)
    elif DOUYIN_USER_RE.search(url or ""):
        handle = "@" + DOUYIN_USER_RE.search(url or "").group(1)
    elif WEIBO_UID_RE.search(url or ""):
        handle = "UID " + WEIBO_UID_RE.search(url or "").group(1)
    elif BILIBILI_VIDEO_RE.search(url or ""):
        handle = BILIBILI_VIDEO_RE.search(url or "").group(1)

    # 标题里通常长这样：「荣小兔头 @usagi_rr - Twitter」
    if not handle:
        found = HANDLE_RE.search(clean_title)
        if found:
            handle = "@" + found.group(1)

    name = HANDLE_RE.sub("", clean_title).strip(" -–—|·")
    return name[:60], handle


def relevance_rank(url: str = "", source: str = "") -> int:
    """来源价值：账号页/原创站 0，普通网页 1，相册转载站 2。"""
    host = (urlparse(url).netloc or source or "").lower()
    if any(token in host for token in HIGH_VALUE_HOSTS):
        return 0
    if any(token in host for token in LOW_VALUE_HOSTS):
        return 2
    return 1


def clean_url(url: str) -> str:
    """去掉常见追踪参数，并解码一次。"""
    if not url:
        return ""
    return unquote(url.strip())
