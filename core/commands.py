"""指令正则构建。

MaiBot 传给指令匹配的是消息纯文本，带图的消息会带前置占位符，
例如「[image] 麦麦寻人」。如果正则用 ^ 严格锚定指令本身就会漏匹配，
所以开头的方括号占位符要允许出现零到多次。

引用消息时占位符里还套着方括号：「[回复了某人的消息: [image]] 麦麦寻人」。
只按「不含 ]」截断的写法会在内层 [image] 的右括号处提前收尾，
后面的指令就被当成多余内容，整条消息匹配失败，所以占位符要允许嵌套。
"""

from __future__ import annotations

import re


def _bracket_placeholder(depth: int = 2) -> str:
    """方括号占位符，内部允许再套 depth - 1 层方括号。"""
    item = r"[^\[\]]"
    placeholder = r"\[[^\[\]]*\]"
    for _ in range(max(depth, 1) - 1):
        placeholder = r"\[(?:" + item + r"|" + placeholder + r")*\]"
    return placeholder


PLACEHOLDER_PREFIX = r"(?:" + _bracket_placeholder() + r"\s*)*"


def build_command_pattern(names: list[str]) -> str:
    body = "|".join(re.escape(item) for item in names if item)
    return r"^" + PLACEHOLDER_PREFIX + r"(?:" + body + r")(?:\s+(?P<arg>[\s\S]*))?$"


def build_more_pattern(keywords: list[str]) -> str:
    body = "|".join(re.escape(item) for item in keywords if item)
    # 允许在「更多」后面跟任务编号：「更多 s-20261103-0832-d7d9」。
    # 不编号时取当前会话最近一次，保持原有用法不变。
    return r"^" + PLACEHOLDER_PREFIX + r"(?:" + body + r")(?:\s+(?P<task>[A-Za-z0-9][A-Za-z0-9\-_]{3,}))?\s*$"
