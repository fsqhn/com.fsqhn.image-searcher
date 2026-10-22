"""指令正则构建。

MaiBot 传给指令匹配的是消息纯文本，带图的消息会带前置占位符，
例如「[image] 麦麦寻人」。如果正则用 ^ 严格锚定指令本身就会漏匹配，
所以开头的方括号占位符要允许出现零到多次。
"""

from __future__ import annotations

import re

PLACEHOLDER_PREFIX = r"(?:\[[^\]]*\]\s*)*"


def build_command_pattern(names: list[str]) -> str:
    body = "|".join(re.escape(item) for item in names if item)
    return r"^" + PLACEHOLDER_PREFIX + r"(?:" + body + r")(?:\s+(?P<arg>[\s\S]*))?$"


def build_more_pattern(keywords: list[str]) -> str:
    body = "|".join(re.escape(item) for item in keywords if item)
    return r"^" + PLACEHOLDER_PREFIX + r"(?:" + body + r")$"
