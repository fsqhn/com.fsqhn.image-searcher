"""Grok 兜底：把原图交给中转站的 Grok，让它自己去 X 上找发布者。

dHash 只能判断「是不是同一张图」，视觉拼图只能判断「是不是同一个人」，两者都
摸不到账号。Grok 带 ``x_search`` 能自己看图、在 X 里检索、给出账号或帖子链接。

实测过一件事，直接决定了这里的做法：让它单纯读图上的水印，会把
``@ste11ar021`` 读成 ``@butter021``；但让它带着图去 X 里搜人，能正确找回原
账号。所以这里不做「先认字再核实」，一步到位让它找人，判据只看它返回的链接。

全部是纯函数：写 prompt、拼请求体、解析返回。网络请求与超时在 plugin.py。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PROMPT_TEMPLATE = """有人在找这张图的出处。

{hints}请你先看清画面：人物、服装、姿势、场景，以及画面上所有的水印、签名、文字。
然后用 X 搜索找出这张照片的发布者。

要求：
1. 找到原帖就给帖子地址；只能确认到账号就给账号主页；都对不上就说没找到。
2. 必须真的搜过再回答。不要根据水印猜，也不要给出你没在搜索结果里见到的账号。
3. 判断依据要写清楚：你搜了什么、看到哪些账号、为什么认为是这个人。
4. 结尾单独一行写「依据：」，用不超过 40 个字说明关键证据。

输出格式：
账号：@xxx
帖子：https://x.com/xxx/status/1234567890
依据：不超过 40 字

只能确认到账号时，帖子一行写「无」。完全找不到时，账号一行写「无」，并在依据里说明搜了什么、没有结果。
"""

HINT_BLOCK = """搜索引擎这边已经返回了这些候选（账号 | 平台 | 标题）：
{hints}

请先把这些候选逐个和画面对比，看哪一个是这张图真正的发布者——找到就直接给它的帖子地址。
这些都对不上，再去 X 上另找别人。
"""


def build_hints(candidates: list, limit: int = 8) -> list[str]:
    """把候选整理成给 Grok 看的清单：账号优先，其次标题。

    图片反搜拿到的候选里，账号和标题往往就是最有价值的线索——让人去核实
    「这个账号是不是发过这张图」，比让它凭空看图猜关键词靠谱得多。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        handle = str(getattr(item, "handle", "") or "").strip()
        name = str(getattr(item, "name", "") or "").strip()
        platform = str(getattr(item, "platform", "") or "").strip()
        if not handle and not name:
            continue
        key = (handle or name).lower()
        if key in seen:
            continue
        seen.add(key)
        parts = [handle or "无账号"]
        if platform:
            parts.append(platform[:24])
        if name:
            parts.append(" ".join(name.split())[:40])
        lines.append(" | ".join(parts))
        if len(lines) >= max(1, limit):
            break
    return lines


def build_prompt(hints: list[str] | None = None) -> str:
    """有候选清单就先核对候选，没有就保留原来的盲搜。"""
    if not hints:
        return PROMPT_TEMPLATE.format(hints="")
    block = HINT_BLOCK.format(hints="\n".join(f"{index}. {line}" for index, line in enumerate(hints, 1)))
    return PROMPT_TEMPLATE.format(hints=block)


# X 的保留路径，出现在 x.com/<x> 里不能当账号。
RESERVED = {
    "i",
    "home",
    "search",
    "explore",
    "hashtag",
    "intent",
    "settings",
    "messages",
    "notifications",
    "compose",
    "login",
    "signup",
    "tos",
    "privacy",
    "share",
    "download",
    "account",
    "about",
}

_STATUS = re.compile(
    r"(?:x|twitter)\.com/(?P<handle>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<sid>\d{5,})",
    re.I,
)
_PROFILE = re.compile(
    r"(?:x|twitter)\.com/(?P<handle>[A-Za-z0-9_]{1,15})(?=[/?#\s)\]}>\"']|$)",
    re.I,
)
_REASON = re.compile(r"依据\s*[:：]\s*(.+)")
# prompt 里约定了「账号：@xxx」这一行。只认这个结构化字段，不去正文里捞
# 裸写的 @名字——模型可能在复述「我搜了某某但没有结果」，扫全文会误判。
_HANDLE_FIELD = re.compile(r"账号\s*[:：]\s*@?([A-Za-z0-9_]{1,15})")
_URL = re.compile(r"https?://\S+")


@dataclass
class GrokResult:
    """一次 Grok 找人的结果。"""

    ok: bool = False
    # post=给出具体帖子；account=只给出账号主页；none=没找到
    tier: str = "none"
    post_url: str = ""
    handle: str = ""
    reason: str = ""
    text: str = ""
    model: str = ""
    error: str = ""
    tools: list[str] = field(default_factory=list)


def build_payload(
    model: str,
    image_data_url: str,
    *,
    hints: list[str] | None = None,
    max_output_tokens: int = 1500,
) -> dict:
    """拼 Responses 接口的请求体。

    中转站的 ``chat/completions`` 带 ``search_parameters`` 会被上游打回 410，
    只有这里这条 ``/responses`` + ``tools`` 的路能通。
    """
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": build_prompt(hints)},
                    {"type": "input_image", "image_url": image_data_url},
                ],
            }
        ],
        "tools": [{"type": "x_search"}],
        "max_output_tokens": max_output_tokens,
    }


def collect_text_and_urls(body: dict) -> tuple[str, list[str], list[str]]:
    """从 Responses 的返回里取出正文、链接、调用过的工具名。"""
    texts: list[str] = []
    urls: list[str] = []
    tools: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "custom_tool_call":
            name = str(item.get("name") or "")
            if name and name not in tools:
                tools.append(name)
            continue
        if kind != "message":
            continue
        for piece in item.get("content") or []:
            if not isinstance(piece, dict):
                continue
            if piece.get("type") in {"output_text", "text"}:
                texts.append(str(piece.get("text") or ""))
            for note in piece.get("annotations") or []:
                if not isinstance(note, dict):
                    continue
                url = str(note.get("url") or "")
                if url:
                    urls.append(url)
    for note in body.get("citations") or []:
        if isinstance(note, dict) and note.get("url"):
            urls.append(str(note["url"]))
        elif isinstance(note, str):
            urls.append(note)
    text = "\n".join(part for part in texts if part).strip()
    return text, urls, tools


def find_posts(text: str, urls: list[str]) -> list[tuple[str, str]]:
    """按出现顺序收集帖子链接，返回 [(handle, url)]，去重保序。"""
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for blob in [text, *urls]:
        for match in _STATUS.finditer(blob or ""):
            handle = match.group("handle")
            # x.com/i/status/... 是 X 自己的内部跳转链接，不是某个账号发的帖子。
            if handle.lower() in RESERVED:
                continue
            sid = match.group("sid")
            if sid in seen:
                continue
            seen.add(sid)
            found.append((handle, f"https://x.com/{handle}/status/{sid}"))
    return found


def find_accounts(text: str, urls: list[str]) -> list[str]:
    """收集只出现过主页、没有具体帖子的账号。"""
    seen: set[str] = set()
    found: list[str] = []
    for blob in [text, *urls]:
        for match in _PROFILE.finditer(blob or ""):
            handle = match.group("handle")
            if handle.lower() in RESERVED or handle in seen:
                continue
            # 帖子链接里的账号已经算进帖子档，这里不重复收录。
            tail = (blob[match.end() : match.end() + 9] or "").lower()
            if tail.startswith("/status"):
                continue
            seen.add(handle)
            found.append(handle)
    # 模型没写链接、只按格式写了「账号：@xxx」时，认这一处。
    for match in _HANDLE_FIELD.finditer(text or ""):
        handle = match.group(1)
        if handle.lower() in RESERVED or handle in seen:
            continue
        seen.add(handle)
        found.append(handle)
    return found


def reason_of(text: str) -> str:
    """取「依据：」那一行；模型没按格式写就退化成去掉链接后的开头。"""
    if not text:
        return ""
    match = _REASON.search(text)
    if match:
        return match.group(1).strip()[:80]
    plain = _URL.sub("", text)
    plain = " ".join(plain.split())
    return plain[:80]


def parse_output(body: dict, *, model: str = "") -> GrokResult:
    """把 Responses 的返回整理成分档结果。

    只认真实出现在正文或 annotations 里的链接：模型可能编一个账号出来，而编
    造出来的名字不会带着可点击的帖子地址。
    """
    result = GrokResult(model=str(body.get("model") or model or ""))
    text, urls, tools = collect_text_and_urls(body)
    result.text = text
    result.tools = tools
    if not text and not urls:
        result.error = "返回空内容"
        return result

    posts = find_posts(text, urls)
    result.reason = reason_of(text)
    if posts:
        handle, url = posts[0]
        result.ok = True
        result.tier = "post"
        result.handle = handle
        result.post_url = url
        return result

    accounts = find_accounts(text, urls)
    if accounts:
        result.ok = True
        result.tier = "account"
        result.handle = accounts[0]
        return result

    result.ok = True
    result.tier = "none"
    return result


def dumps(result: GrokResult) -> str:
    """日志用：一行说清这一轮 Grok 干了什么。"""
    return json.dumps(
        {
            "tier": result.tier,
            "handle": result.handle,
            "post": result.post_url,
            "reason": result.reason,
            "tools": result.tools,
            "error": result.error,
        },
        ensure_ascii=False,
    )
