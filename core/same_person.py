"""用「挂在同一个外链」判断两个 X 号是同一个人。

coser 经常开好几个号，大号挂 Patreon、小号也挂同一条链接；互相转发也很常见，
所以光看谁转发过谁的图分不清作者。但同一个人的几个号往往指向同一个收款页或
聚合页——这条是硬证据，不是猜名字。

这里只做纯函数：把资料里的外链抠出来、归一化，再把共用的挑出来。联网由插件
负责。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_URL = re.compile(r"https?://[^\s\"'<>）)】]+", re.I)
# 指向 X 自己的链接不算身份证据，那是账号本身。
_SELF = ("x.com", "twitter.com")


def _normalize(url: str) -> str:
    """去掉协议、www、结尾斜杠和查询串，只留 host/path。"""
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    # 只有域名没有路径的不算：一堆人都会写「onlyfans.com」这种根地址。
    if not host or not path:
        return ""
    return f"{host}{path}"


def external_links(user: dict | None) -> list[str]:
    """资料里挂的外链：官网字段 + 简介里出现的地址。"""
    user = user or {}
    raw: list[str] = []
    site = user.get("website")
    if isinstance(site, dict):
        raw.append(str(site.get("url") or ""))
    raw.extend(_URL.findall(str(user.get("description") or "")))
    found: list[str] = []
    seen: set[str] = set()
    for url in raw:
        key = _normalize(url)
        if not key:
            continue
        if any(key == self_host or key.startswith(self_host + "/") for self_host in _SELF):
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found


_STATUS_RANK = {"match": 0, "vision": 1, "suspect": 2, "unverified": 3, "mismatch": 4, "error": 5}
# 这些状态说明号跟这张图有点关系；unverified 只是图没下到，算不上证据。
TRUSTED = frozenset({"match", "vision", "suspect"})


def eligible(pairs: list[tuple[str, str]]) -> list[str]:
    """挑出能参与「同一个人」归并的 X 号，按候选顺序去重。

    指纹对不上的号不剔除：同一个人的另一个号发的往往是别的图，距离对不上很正常，
    剔掉就漏了「大号发图、小号发另一张」这种真情况。约束放在分组上——只有含
    跟这张图对上的号（match / vision / suspect）的分组才算数，见 find_shared。
    一个对上的号都没有时直接不参与，省得白拉一堆资料。
    """
    best: dict[str, str] = {}
    order: list[str] = []
    for handle, status in pairs:
        key = str(handle or "").strip().lstrip("@").lower()
        if not key:
            continue
        status = str(status or "")
        if key not in best:
            best[key] = status
            order.append(key)
        elif _STATUS_RANK.get(status, 5) < _STATUS_RANK.get(best[key], 5):
            best[key] = status
    if len(order) < 2:
        return []
    if not any(best.get(key) in TRUSTED for key in order):
        return []
    return order


def find_shared(profiles: dict[str, dict], anchors: set[str] | None = None) -> list[dict]:
    """找出共用同一条外链的号。返回 [{link, handles}]，号多的排前面。

    anchors 给定时，只保留至少含一个锚点的分组：锚点是跟这张图对上的号，
    不含它的分组说明这条外链跟这张图没关系，列出来就是无关账号。传了空集合
    就一个分组都不留——宁可不出，也不挂无关的人。
    """
    pool = {item.lower() for item in (anchors or set())}
    buckets: dict[str, list[str]] = {}
    for handle, user in profiles.items():
        for link in external_links(user):
            buckets.setdefault(link, []).append(handle)
    groups = [
        {"link": link, "handles": sorted({item.lower() for item in handles})}
        for link, handles in buckets.items()
        if len({item.lower() for item in handles}) >= 2
    ]
    if anchors is not None:
        groups = [group for group in groups if pool & set(group["handles"])]
    groups.sort(key=lambda group: (-len(group["handles"]), group["link"]))
    return groups
