"""麦麦寻人：以图搜出处的 MaiBot 插件。

Tool 给 planner 用（只回纯文字），Command 给群友用（合并转发）。
两条路的搜索逻辑完全一致，区别只在呈现方式。
"""

from __future__ import annotations

import asyncio
import base64
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .core import formatting
from .core.commands import build_command_pattern, build_more_pattern
from .core.engines import (
    BaiduEngine,
    Candidate,
    EngineError,
    LensEngine,
    SauceNaoEngine,
    YandexEngine,
    detect_ai,
    verify_candidate,
)
from .core.extract import is_pixiv_platform, is_x_platform
from .core.hashing import dhash, prepare_upload
from .core.httpclient import HttpError, download_image_multi
from .core.keypool import KeyPool
from .core.maibot_compat import unwrap_capability
from .core.tasks import SearchOutcome, SearchTask, TaskStore

DEFAULT_COMMAND = "麦麦寻人"
PLUGIN_NAME = "麦麦寻人"
_BASE64_KEYS = ("binary_data_base64", "base64", "image_base64", "file_base64")
_URL_KEYS = ("url", "image_url", "file_url", "download_url")


# ---------------------------------------------------------------------------
# 命令名在模块导入时读取配置，装饰器的 pattern 只能用这种方式做到可配置。
# 改指令不会自动生效：配置热更新只调 on_config_update，不会重新导入本模块，
# 必须手动重载插件（插件管理页或 reload 能力），否则装饰器仍是旧 pattern。
# ---------------------------------------------------------------------------


def _read_command_names() -> tuple[str, list[str]]:
    path = Path(__file__).with_name("config.toml")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # noqa: BLE001 - 首次加载时配置可能还没生成
        return DEFAULT_COMMAND, []
    section = data.get("plugin") or {}
    command = str(section.get("command") or DEFAULT_COMMAND).strip() or DEFAULT_COMMAND
    aliases = [
        str(item).strip()
        for item in (section.get("aliases") or [])
        if str(item).strip() and str(item).strip() != command
    ]
    return command, aliases


COMMAND, ALIASES = _read_command_names()
_COMMAND_PATTERN = build_command_pattern([COMMAND, *ALIASES])
_MORE_PATTERN = build_more_pattern(["更多", "more"])
# 只含媒体占位符的消息（如 "[image]"），用于接住等待中的单图消息
_CAPTURE_PATTERN = r"^(?:\[[^\]]*\]\s*)+$"


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


def _ui(label: str, hint: str = "", **extra: Any) -> dict[str, Any]:
    """字段在 WebUI 上显示的中文名与填写提示。"""
    payload: dict[str, Any] = {"label": label}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


class EngineConfig(PluginConfigBase):
    """单个搜索引擎的配置。"""

    __ui_label__ = "搜索引擎"

    enabled: bool = Field(
        default=True,
        description="关掉后这个引擎完全不参与搜索，也不再消耗它的额度。",
        json_schema_extra=_ui("启用该引擎"),
    )
    keys: list[str] = Field(
        default_factory=list,
        description="这个引擎的 API Key，可以填多个。插件优先用没被限流的那个，被限流或失效的会自动跳过。",
        json_schema_extra=_ui(
            "API Key",
            "点「添加」可以加多个 key；不需要 key 的引擎留空即可。",
        ),
    )
    max_results: int = Field(
        default=5,
        description="这个引擎最多保留几条候选。取小一点只是少下载、少验证，不省额度（额度按调用次数算）。",
        json_schema_extra=_ui("每次保留几条", "Lens 5、Yandex 3、百度 3、SauceNAO 4（免费档上限）。"),
        ge=1,
        le=20,
    )


class LensConfig(EngineConfig):
    """Google Lens（走 SerpAPI），命中率最高，需要自己的 key。"""

    __ui_label__ = "Lens 引擎（Google 识图 / SerpAPI）"
    __ui_icon__ = "scan-search"

    max_results: int = Field(
        default=5,
        description="Lens 每次最多保留几条候选。建议 5，它本来也只返回四五条。",
        json_schema_extra=_ui("每次保留几条", "建议 5；调小不省额度（按调用次数计费）。"),
        ge=1,
        le=20,
    )


class YandexConfig(EngineConfig):
    """Yandex 以图搜图，免费、不用 key，返回条数多但噪声大。"""

    __ui_label__ = "Yandex 引擎（免费，不用 key）"
    __ui_icon__ = "globe"

    max_results: int = Field(
        default=3,
        description="Yandex 一次返回近百条，大多是账号主页，取前几条就够。",
        json_schema_extra=_ui("每次保留几条", "建议 3。必须配好代理，否则容易超时。"),
        ge=1,
        le=20,
    )


class BaiduConfig(EngineConfig):
    """百度识图，免费、不用 key，主要覆盖国内站点。"""

    __ui_label__ = "百度引擎（免费，不用 key）"
    __ui_icon__ = "search"

    max_results: int = Field(
        default=3,
        description="百度返回的是裁剪缩略图，后几条基本无关，取前 3 条即可。",
        json_schema_extra=_ui("每次保留几条", "建议 3。结果多标「疑似」，因为缩略图会被裁剪。"),
        ge=1,
        le=20,
    )


class SauceNaoConfig(EngineConfig):
    """SauceNAO 插画库，需要 key，还负责判断这张图是不是插画。"""

    __ui_label__ = "SauceNAO 引擎（插画库，需要 key）"
    __ui_icon__ = "images"

    min_similarity: int = Field(
        default=60,
        description="相似度低于这个分数只算疑似，不会当权威结果。",
        json_schema_extra=_ui("最低相似度（%）", "建议 60。调高更严格、候选更少。"),
        ge=0,
        le=100,
    )
    route_similarity: float = Field(
        default=75.0,
        description="相似度达到这个分数，就认为这张图是插画，自动改用插画路线的引擎顺序。",
        json_schema_extra=_ui("判定插画的相似度（%）", "建议 75。调低更容易走插画路线，调高更保守。"),
        ge=0,
        le=100,
    )
    max_results: int = Field(
        default=4,
        description="SauceNAO 每次最多保留几条候选。免费档单次最多只返回 4 条。",
        json_schema_extra=_ui("每次保留几条", "免费档最多 4 条，调大也拿不到更多。"),
        ge=1,
        le=20,
    )
    db: str = Field(
        default="999",
        description="全量检索时传给 SauceNAO 的 db 参数。999 表示全部数据库。",
        json_schema_extra=_ui("全量检索的库", "默认 999（全库）。一般不用改。", placeholder="999"),
    )
    db_strict: str = Field(
        default="5,6,21,24",
        description="收紧检索时用的 db 参数，逗号分隔。默认只查 pixiv、twitter、anime-pictures 等常用插画库。",
        json_schema_extra=_ui(
            "收紧检索的库",
            "默认 5,6,21,24。收得越窄，越不容易带出成人站点，但冷门图可能搜不到。",
            placeholder="5,6,21,24",
        ),
    )
    db_policy_enabled: bool = Field(
        default=True,
        description="是否按群启用名单策略。关闭时所有群都用上面的「全量检索的库」。",
        json_schema_extra=_ui(
            "启用按群名单",
            "打开后：名单里的群全量检索，其他群收紧。名单留空就是所有群都收紧。想反过来把「名单反转」打开。",
        ),
    )
    db_full_groups: list[str] = Field(
        default_factory=list,
        description="名单里的群号。填群号即可（例如 123456789），也认会话 ID。",
        json_schema_extra=_ui(
            "名单（群号）",
            "填群号，点「添加」可以加多个。这些群走全量，其他群收紧；留空就是所有群都收紧。",
        ),
    )
    db_policy_invert: bool = Field(
        default=False,
        description="反转名单含义：打开后名单里的群收紧，其他群全量。",
        json_schema_extra=_ui(
            "名单反转",
            "默认名单=全量。打开后名单=收紧，其他群=全量，适合只想在个别群限制的情况。",
        ),
    )


class EnginesConfig(PluginConfigBase):
    """引擎组配置。"""

    __ui_label__ = "引擎优先级"

    priority: list[str] = Field(
        default_factory=lambda: ["lens", "yandex", "baidu", "saucenao"],
        description="照片路线（默认）的引擎优先级。数组顺序就是优先级，越靠前越优先采用、越靠前展示。",
        json_schema_extra=_ui(
            "照片路线 · 引擎优先级（调整顺序即可）",
            "可填 lens、yandex、baidu、saucenao。把想优先用的引擎放到前面就是提高它的优先级，删掉某个名字即不使用它。",
        ),
    )
    priority_drawn: list[str] = Field(
        default_factory=lambda: ["saucenao", "yandex", "baidu", "lens"],
        description="插画路线的引擎优先级。SauceNAO 命中到判定分数时自动切到这条顺序。",
        json_schema_extra=_ui(
            "插画路线 · 引擎优先级（调整顺序即可）",
            "通常 SauceNAO 放第一位。可填的值与照片路线相同。",
        ),
    )


class AiDetectConfig(PluginConfigBase):
    """AI 生成图检测配置。"""

    __ui_label__ = "AI 图检测"

    enabled: bool = Field(
        default=False,
        description="开启后，搜不到可靠匹配时会调用 Sightengine 判断这张图是不是 AI 生成的。",
        json_schema_extra=_ui("启用 AI 生成图检测", "需要 Sightengine 账号；不开也能正常搜图。"),
    )
    keys: list[str] = Field(
        default_factory=list,
        description="Sightengine 的密钥，格式是 api_user:api_secret，中间用英文冒号连起来。",
        json_schema_extra=_ui("Sightengine 密钥", "形如 123456:abcdefg；可填多个轮询。"),
    )
    only_when_low_confidence: bool = Field(
        default=True,
        description="只在没有可靠匹配时才调用检测，避免每次搜图都消耗 Sightengine 额度。",
        json_schema_extra=_ui("仅在没找到匹配时检测", "建议保持开启。"),
    )


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "scan-search"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="插件总开关，关掉后指令和工具都不再响应。",
        json_schema_extra=_ui("启用插件"),
    )
    config_version: str = Field(
        default="0.3.0",
        description="配置版本号，由插件维护，不需要手动修改。",
        json_schema_extra=_ui("配置版本", hidden=True),
    )
    command: str = Field(
        default=DEFAULT_COMMAND,
        description="群里发这个词就触发搜图。改完必须重载插件才生效。",
        json_schema_extra=_ui("触发指令", "例如填「麦麦寻人」，群里就发「麦麦寻人」。改完记得重载插件。", placeholder="麦麦寻人"),
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="额外的触发词，可以留空。填几个就多几个出发方式，同样需要重载插件。",
        json_schema_extra=_ui("指令别名", "例如再加一个「搜图」；留空表示只有上面那个指令。"),
    )
    nickname: str = Field(
        default="麦麦寻人",
        description="结果以合并转发发出去时，每条消息上方显示的昵称。",
        json_schema_extra=_ui("转发昵称", "随便改，只影响显示。"),
    )
    task_ttl_minutes: int = Field(
        default=15,
        description="搜完的结果保留多久。这段时间内回复「更多」可以直接重发，不用重新搜。",
        json_schema_extra=_ui("结果保留时间（分钟）", "默认 15 分钟，超时后要重新发图。"),
        ge=1,
        le=1440,
    )
    show_reference: bool = Field(
        default=True,
        description="除了最佳匹配，是否把其他候选也列出来。关掉后只发最佳匹配。",
        json_schema_extra=_ui("展示其他候选"),
    )
    reference_limit: int = Field(
        default=3,
        description="第一次回复里，每个引擎最多列几条候选。",
        json_schema_extra=_ui("首次展示条数（每引擎）", "默认 3。每个引擎单独分组，各列几条。"),
        ge=1,
        le=50,
    )
    more_limit: int = Field(
        default=10,
        description="回复「更多」时，每个引擎最多列几条候选（这些候选都会带图）。",
        json_schema_extra=_ui("「更多」展示条数（每引擎）", "默认 10，基本等于全部结果。"),
        ge=1,
        le=50,
    )
    verify_top_n: int = Field(
        default=3,
        description="Yandex 和百度各下载前 N 条候选做指纹比对；Lens 更准，比对 2N 条。条数越多越慢。",
        json_schema_extra=_ui("每个引擎验证几条", "默认 3。调大更不容易漏，但会多下载几张图。"),
        ge=1,
        le=10,
    )
    deep_scan: bool = Field(
        default=False,
        description=(
            "深度检索：每个引擎不只取前几条，而是按下面的上限尽量多取，并加大指纹比对数量。"
            "能提高找出来的概率，但一次搜索会明显变慢。"
        ),
        json_schema_extra=_ui(
            "深度检索（取更多结果）",
            "打开后每个引擎按「深度检索时每引擎上限」取数，找出来的概率更高，代价是慢一倍左右。",
        ),
    )
    deep_scan_limit: int = Field(
        default=20,
        description="深度检索时每个引擎最多取多少条候选。设大接近全量。",
        json_schema_extra=_ui(
            "深度检索时每引擎上限",
            "默认 20。想接近全量可以调到 60~100，但耗时和流量会跟着涨。SauceNAO 免费档每次最多 4 条，不受这个值影响。",
        ),
        ge=5,
        le=100,
    )
    drawing_other_limit: int = Field(
        default=3,
        description=(
            "SauceNAO 判定为插画后，其他引擎每边只留几条做参考，不再下载图片比对。"
            "插画库命中已经足够确定，没必要再逐张验证。"
        ),
        json_schema_extra=_ui(
            "插画路线每引擎留几条",
            "默认 3。判成插画后其他引擎只留这么多条参考，不下载图，速度快很多。",
        ),
        ge=1,
        le=10,
    )
    deep_more_limit: int = Field(
        default=20,
        description=(
            "深度检索时回复「更多」，每个引擎最多列几条候选。"
            "深度模式取到的条数比平时多，这个值要跟着放开才看得到全部结果。"
        ),
        json_schema_extra=_ui(
            "深度检索时「更多」条数（每引擎）",
            "默认 20。开着深度检索却只看到 10 条时，就是这个值没调。",
        ),
        ge=1,
        le=100,
    )
    deep_verify_top_n: int = Field(
        default=8,
        description="深度检索时每个引擎下载前 N 条候选做指纹比对。这个值直接决定耗时。",
        json_schema_extra=_ui(
            "深度检索时每引擎验证几条",
            "默认 8。调大更容易找出漏掉的结果，但每多一条就多下载一张图。",
        ),
        ge=1,
        le=30,
    )
    image_wait_seconds: int = Field(
        default=60,
        description="用户只发了指令没带图时，插件等他补图的时间。",
        json_schema_extra=_ui("等补图的秒数", "默认 60 秒。这段时间内发一张图就会直接开始搜。"),
        ge=10,
        le=600,
    )
    hash_threshold: int = Field(
        default=8,
        description="两张图的指纹差异小于这个值才算同一张图。数字越小越严格，越大越容易把相似图当成同一张。",
        json_schema_extra=_ui("判为同一张图的严格程度", "默认 8（严格）。裁剪过的图常在 10~20，会被标成「疑似」。"),
        ge=0,
        le=32,
    )
    engine_timeout: int = Field(
        default=50,
        description="单个搜索引擎最多等多少秒，超时就跳过它、继续用其他引擎的结果。",
        json_schema_extra=_ui("单个引擎超时（秒）", "默认 50。Yandex 走代理容易抖，网络差可以再调到 60。"),
        ge=5,
        le=180,
    )
    download_timeout: int = Field(
        default=12,
        description="下载一张候选图最多等多少秒。",
        json_schema_extra=_ui("下载图片超时（秒）", "默认 12。"),
        ge=3,
        le=120,
    )
    proxy: str = Field(
        default="",
        description="唯一一个代理设置：所有搜索引擎、候选图下载、AI 检测都走它。留空表示直连。",
        json_schema_extra=_ui(
            "代理地址",
            "强烈建议填写。例如 http://127.0.0.1:6789。Lens、Yandex 对这些服务的国内直连基本不通，不填代理通常搜不出结果。",
            placeholder="http://127.0.0.1:6789",
        ),
    )
    verbose_log: bool = Field(
        default=False,
        description="开启后把取图、每个引擎的耗时与错误、候选验证过程都写进日志，排查问题时打开，平时关着。",
        json_schema_extra=_ui("详细日志开关", "遇到问题打开它，重载插件，复现后把日志贴到 issues。"),
    )
    max_concurrent_tasks: int = Field(
        default=3,
        description="同时最多搜几张图，超过的排队。",
        json_schema_extra=_ui("同时搜索上限", "默认 3，够用就不用改。"),
        ge=1,
        le=10,
    )


class ImageSearcherConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    lens: LensConfig = Field(default_factory=LensConfig)
    yandex: YandexConfig = Field(default_factory=YandexConfig)
    baidu: BaiduConfig = Field(default_factory=BaiduConfig)
    saucenao: SauceNaoConfig = Field(default_factory=SauceNaoConfig)
    ai_detect: AiDetectConfig = Field(default_factory=AiDetectConfig)


# ---------------------------------------------------------------------------
# 消息里的图片解析
# ---------------------------------------------------------------------------


def _segments_of(message: Any) -> list[dict]:
    if not isinstance(message, dict):
        return []
    for key in ("message_segments", "raw_message"):
        value = message.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _segment_inline_image(segment: dict) -> str:
    for key in _BASE64_KEYS:
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = segment.get("data")
    if isinstance(nested, dict):
        for key in _BASE64_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _segment_image_url(segment: dict) -> str:
    for key in _URL_KEYS:
        value = segment.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()
    nested = segment.get("data")
    if isinstance(nested, dict):
        for key in _URL_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value.strip()
    if isinstance(nested, str) and nested.startswith(("http://", "https://")):
        return nested.strip()
    return ""


def _reply_target(segments: list[dict], message: dict) -> str:
    for segment in segments:
        if str(segment.get("type") or "") != "reply":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            target = data.get("target_message_id") or data.get("message_id") or ""
        else:
            target = data or ""
        if str(target).strip():
            return str(target).strip()
    return str(message.get("reply_to") or "").strip()


def _decode_base64(value: str) -> bytes | None:
    payload = value.strip()
    if payload.startswith("base64://"):
        payload = payload[len("base64://") :]
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001
        return None
    return raw or None


def _first_image(message: Any) -> tuple[bytes | None, str]:
    """从消息里取第一张图的字节与原始链接。"""
    for segment in _segments_of(message):
        if str(segment.get("type") or "") != "image":
            continue
        inline = _segment_inline_image(segment)
        if inline:
            raw = _decode_base64(inline)
            if raw:
                return raw, _segment_image_url(segment)
        url = _segment_image_url(segment)
        if url:
            return None, url
    return None, ""


def _proxy_of(value: str) -> str:
    """只有一个全局代理：direct/none/off/- 表示强制直连，其余原样返回。"""
    text = str(value or "").strip()
    if text.lower() in {"direct", "none", "off", "-"}:
        return ""
    return text


def _stream_id_of(message: Any) -> str:
    """钩子侧可能不给 stream_id，从消息里兜底取会话 ID。"""
    if not isinstance(message, dict):
        return ""
    for key in ("session_id", "stream_id", "chat_id"):
        value = message.get(key)
        if value:
            return str(value)
    info = message.get("message_info")
    if isinstance(info, dict):
        for group_key in ("group_info", "session_info"):
            group = info.get(group_key)
            if isinstance(group, dict):
                for key in ("session_id", "stream_id", "chat_id"):
                    if group.get(key):
                        return str(group[key])
    return ""


def _group_id_of(message: Any) -> str:
    """取群号，SauceNAO 的名单按群号匹配；私聊返回空。"""
    if not isinstance(message, dict):
        return ""
    info = message.get("message_info")
    if isinstance(info, dict):
        group = info.get("group_info")
        if isinstance(group, dict):
            for key in ("group_id", "id", "group_code"):
                value = group.get(key)
                if value:
                    return str(value)
    return ""


def _user_id_of(message: Any) -> str:
    """从消息字典里尽量挖出用户 ID：钩子侧和指令侧的结构不完全一致。"""
    if not isinstance(message, dict):
        return ""
    info = message.get("message_info")
    if isinstance(info, dict):
        user = info.get("user_info")
        if isinstance(user, dict):
            for key in ("user_id", "id", "uid"):
                if user.get(key):
                    return str(user[key])
    for key in ("user_id", "sender_id", "sender"):
        value = message.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return ""


def _has_image(message: Any) -> bool:
    return any(str(segment.get("type") or "") == "image" for segment in _segments_of(message))


def _send_ok(result: Any) -> bool:
    """send 能力返回 bool 或 {success: bool}，两种形态都要认。"""
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("success", True))
    return result is not None


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class ImageSearcherPlugin(MaiBotPlugin):
    """以图搜出处插件。"""

    config_model = ImageSearcherConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._tasks: TaskStore | None = None
        self._pools: dict[str, KeyPool] = {}
        self._engines: dict[str, Any] = {}
        self._background: set[asyncio.Task] = set()
        self._pending: dict[tuple[str, str], float] = {}
        self._pending_timers: dict[tuple[str, str], asyncio.Task] = {}
        # 合并转发节点的 uin 不能为空，否则 QQ 端会提示消息异常；从入站消息里记机器人账号。
        self._self_id: str = ""
        # SauceNAO 的库策略按群配置，但 stream_id 是哈希，需要群号映射。
        self._stream_groups: dict[str, str] = {}
        # 消息入库有延迟：指令触发时按 ID 查库往往还是空，所以在入站钩子里先存一份二进制。
        self._image_cache: dict[str, tuple[float, bytes]] = {}
        # 引擎请求与候选图验证分池：引擎是长请求，验证是短下载，
        # 共用一个默认线程池会让验证排队，wait_for 的超时把排队时间也算进去。
        self._engine_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="imgsearch-engine")
        self._verify_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="imgsearch-verify")

    # -- 日志 -------------------------------------------------------------

    def _vlog(self, message: str, *args: Any) -> None:
        """详细日志：开关打开时写 info（默认可见），关闭时降到 debug。"""
        verbose = False
        try:
            verbose = bool(self.config.plugin.verbose_log)
        except Exception:  # noqa: BLE001 - 配置尚未注入时按关闭处理
            verbose = False
        if verbose:
            self.ctx.logger.info(message, *args)
        else:
            self.ctx.logger.debug(message, *args)

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        self._rebuild()
        restored = self._tasks.load() if self._tasks else 0
        enabled = [name for name in self.config.engines.priority if self._enabled(name)]
        self.ctx.logger.info(
            "%s已加载：指令=%s，引擎=%s，上限=%s，详细日志=%s，恢复任务=%s",
            PLUGIN_NAME,
            COMMAND,
            "、".join(enabled) or "无",
            self._describe_limits(),
            "开" if self.config.plugin.verbose_log else "关",
            restored,
        )
        if not enabled:
            self.ctx.logger.warning("没有启用任何搜索引擎，插件将无法返回结果")
        self._vlog("指令正则：%s", _COMMAND_PATTERN)

    async def on_unload(self) -> None:
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        for timer in self._pending_timers.values():
            timer.cancel()
        self._pending_timers.clear()
        self._pending.clear()
        self._image_cache.clear()
        self._engine_pool.shutdown(wait=False, cancel_futures=True)
        self._verify_pool.shutdown(wait=False, cancel_futures=True)
        self.ctx.logger.info("%s已卸载", PLUGIN_NAME)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        self._rebuild(keep_tasks=True)
        section = config_data.get("plugin") if isinstance(config_data, dict) else None
        if isinstance(section, dict):
            configured = str(section.get("command") or "").strip()
            if configured and configured != COMMAND:
                self.ctx.logger.warning(
                    "搜图指令已改为 %s，但装饰器不会随配置热更新（当前仍是 %s），需重载插件才生效",
                    configured,
                    COMMAND,
                )
        self.ctx.logger.info("%s配置已更新：上限=%s，版本=%s", PLUGIN_NAME, self._describe_limits(), version or "无")

    def _describe_limits(self) -> str:
        parts = []
        for name in self.config.engines.priority:
            engine = getattr(self.config, name, None)
            if engine is None:
                continue
            parts.append(f"{name}={engine.max_results}")
        return "、".join(parts) or "无"

    def _rebuild(self, keep_tasks: bool = True) -> None:
        config = self.config
        section = config.plugin
        if not keep_tasks or self._tasks is None:
            self._tasks = TaskStore(self.ctx.paths.data_dir, section.task_ttl_minutes * 60)
        else:
            self._tasks.set_ttl(section.task_ttl_minutes * 60)

        global_proxy = _proxy_of(section.proxy)
        self._pools = {
            "lens": KeyPool(config.lens.keys),
            "saucenao": KeyPool(config.saucenao.keys),
            "ai": KeyPool(config.ai_detect.keys),
        }
        self._engines = {
            "lens": LensEngine(self._pools["lens"], global_proxy, section.engine_timeout),
            "yandex": YandexEngine(global_proxy, section.engine_timeout),
            "baidu": BaiduEngine(global_proxy, section.engine_timeout),
            "saucenao": SauceNaoEngine(
                self._pools["saucenao"],
                global_proxy,
                section.engine_timeout,
                config.saucenao.min_similarity,
                db_full=config.saucenao.db,
                db_strict=config.saucenao.db_strict,
                policy_enabled=config.saucenao.db_policy_enabled,
                invert=config.saucenao.db_policy_invert,
                groups=config.saucenao.db_full_groups,
                group_of=self._group_of,
            ),
        }

    def _group_of(self, stream_id: str) -> str:
        """会话 ID → 群号，SauceNAO 的名单按群号匹配用。"""
        if not stream_id:
            return ""
        return self._stream_groups.get(stream_id, "")

    def _remember_group(self, stream_id: str, group_id: str) -> None:
        self._stream_groups[stream_id] = group_id
        if len(self._stream_groups) > 200:
            self._stream_groups.pop(next(iter(self._stream_groups)), None)

    def _enabled(self, name: str) -> bool:
        config = getattr(self.config, name, None)
        if config is None or not config.enabled:
            return False
        if name in {"lens", "saucenao"} and not config.keys:
            return False
        return True

    def _stream_id(self, kwargs: dict) -> str:
        message = kwargs.get("message")
        if isinstance(message, dict):
            value = message.get("session_id") or message.get("stream_id") or message.get("chat_id")
            if value:
                return str(value)
        for key in ("stream_id", "session_id", "chat_id"):
            if kwargs.get(key):
                return str(kwargs[key])
        return ""

    def _user_id(self, kwargs: dict) -> str:
        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                user = info.get("user_info")
                if isinstance(user, dict) and user.get("user_id"):
                    return str(user["user_id"])
            if message.get("user_id"):
                return str(message["user_id"])
        return str(kwargs.get("user_id") or "")

    def _message_id(self, kwargs: dict) -> str:
        """消息 ID 可能在 kwargs 顶层，也可能在 message / message_base_info 里。"""
        if kwargs.get("message_id"):
            return str(kwargs["message_id"])
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return ""
        if message.get("message_id"):
            return str(message["message_id"])
        base = message.get("message_base_info")
        if isinstance(base, dict) and base.get("message_id"):
            return str(base["message_id"])
        return ""

    def _latest_task(self, stream_id: str, user_id: str) -> SearchTask | None:
        """先按 群+用户 找，再退回本群最近一条；Tool 与 Command 的 user_id 未必一致。"""
        if self._tasks is None:
            return None
        return self._tasks.latest_for(stream_id, user_id) or self._tasks.latest_in_stream(stream_id)

    def _proxy(self) -> str:
        """引擎、候选图下载、AI 检测共用的那一个代理。"""
        return _proxy_of(self.config.plugin.proxy)

    # -- 取图 -------------------------------------------------------------

    @HookHandler(
        "chat.receive.before_process",
        name="inbound_image_cache",
        description="在图片二进制被清理前缓存一份，供搜图按消息 ID 取图",
        mode=HookMode.OBSERVE,
    )
    async def cache_inbound_image(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not isinstance(message, dict):
            return True, True, None, None, None
        try:
            account_id = str(message.get("account_id") or message.get("self_id") or "").strip()
            if account_id:
                self._self_id = account_id
            group_id = _group_id_of(message)
            if group_id:
                key = stream_id or _stream_id_of(message)
                if key:
                    self._remember_group(key, group_id)
            raw = self._remember_image(message)
            # 等待窗口内的补图直接在这里接住：靠指令正则的话，VLM 没出描述时
            # 消息文本是空字符串，压根匹配不上。
            if raw:
                self._catch_pending_image(raw, message, stream_id)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("缓存入站图片失败：%s", error)
        return True, True, None, None, None

    def _catch_pending_image(self, raw: bytes, message: dict, stream_id: str) -> None:
        current = stream_id or _stream_id_of(message)
        if not current:
            self._vlog("入站图片没有会话 ID，无法匹配等待窗口：id=%s", message.get("message_id"))
            return
        user_id = _user_id_of(message)
        key = self._match_pending(current, user_id)
        if key is None:
            if self._pending:
                self._vlog(
                    "入站图片未匹配到等待窗口：stream=%s user=%s pending=%s",
                    current,
                    user_id,
                    list(self._pending),
                )
            return
        task = asyncio.create_task(self._start_from_pending(raw, key))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _match_pending(self, stream_id: str, user_id: str) -> tuple[str, str] | None:
        """只认同会话同一用户的等待窗口：群里可能同时有好几个人在等图。"""
        if not user_id:
            return None
        now = time.time()
        for key, deadline in self._pending.items():
            if key[0] == stream_id and key[1] == user_id and deadline >= now:
                return key
        return None

    async def _start_from_pending(self, raw: bytes, key: tuple[str, str]) -> None:
        if self._pending.pop(key, None) is None:
            return
        timer = self._pending_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        current, user_id = key
        try:
            self._start_task(raw, "", current, user_id, notify=True)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("等待窗口补图启动搜图失败：%s", error)
            await self.ctx.send.text(f"启动搜图失败：{error}", current)
            return
        self.ctx.logger.info("等待窗口内收到图片，开始搜图：stream=%s user=%s", current, user_id)
        await self.ctx.send.text(f"🔍 收到图片，开始搜图，{self._eta_text()}。", current)

    def _remember_image(self, message: dict) -> bytes | None:
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            return None
        for segment in _segments_of(message):
            if str(segment.get("type") or "") != "image":
                continue
            inline = _segment_inline_image(segment)
            raw = _decode_base64(inline) if inline else None
            if not raw:
                continue
            self._image_cache[message_id] = (time.time(), raw)
            self._vlog("已缓存入站图片：id=%s 体积=%sKB", message_id, len(raw) // 1024)
            self._prune_image_cache()
            return raw
        self._prune_image_cache()
        return None

    def _prune_image_cache(self, ttl: float = 1800.0, keep: int = 60) -> None:
        now = time.time()
        expired = [mid for mid, (stamp, _) in self._image_cache.items() if now - stamp > ttl]
        for mid in expired:
            self._image_cache.pop(mid, None)
        if len(self._image_cache) <= keep:
            return
        ordered = sorted(self._image_cache.items(), key=lambda item: item[1][0])
        for mid, _ in ordered[: len(self._image_cache) - keep]:
            self._image_cache.pop(mid, None)

    def _cached_image(self, message_id: str) -> bytes | None:
        entry = self._image_cache.get(message_id)
        return entry[1] if entry else None

    async def _resolve_image(
        self,
        kwargs: dict,
        *,
        allow_recent: bool = False,
    ) -> tuple[bytes | None, str, str, int]:
        """返回 (图片字节, 图片链接, 来源说明, 过期秒数)。

        过期秒数大于 0 表示「找到了图，但早于取图窗口」——这种情况要让模型去引导用户引用，
        而不是笼统说一句「没有图片」。
        """
        message = kwargs.get("message")
        stream_id = self._stream_id(kwargs)
        segments = _segments_of(message)

        raw, inline_url = _first_image(message)
        url = inline_url
        if raw:
            return raw, url, "当前消息", 0

        message_id = self._message_id(kwargs)
        if message_id:
            cached = self._cached_image(message_id)
            if cached:
                return cached, "", "当前消息", 0

        # Command 传入的消息不带二进制，先按 message_id 找 Host 要一次，
        # 比直接下载 QQ 图链更稳（图链可能过期或被防盗链挡）。
        if message_id:
            found_raw, found_url = await self._image_by_id(message_id, stream_id)
            if found_raw:
                return found_raw, found_url or url, "当前消息", 0

        if url:
            downloaded = await self._download(url)
            if downloaded:
                return downloaded, url, "当前消息", 0

        reply_id = _reply_target(segments, message if isinstance(message, dict) else {})
        if reply_id:
            found_raw, found_url = await self._image_by_id(reply_id, stream_id)
            if found_raw:
                return found_raw, found_url, "引用的消息", 0

        if allow_recent and stream_id:
            found_raw, found_url, stale = await self._recent_images(stream_id)
            if found_raw:
                return found_raw, found_url, "最近的图片消息", 0
            if stale:
                return None, url, "", stale

        return None, url, "", 0

    async def _image_by_id(self, message_id: str, stream_id: str) -> tuple[bytes | None, str]:
        if not message_id:
            return None, ""
        cached = self._cached_image(message_id)
        if cached:
            return cached, ""
        # message_id 全局唯一，先不带 chat_id 查：宿主按 session_id 精确匹配，
        # 而指令侧拿到的 stream_id 和库里存的 session_id 未必同一种形式，带上反而查不到。
        for chat_id in ("", stream_id):
            try:
                result = await self.ctx.message.get_by_id(
                    message_id,
                    chat_id=chat_id,
                    include_binary_data=True,
                )
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.warning("按消息 ID 取图失败：%s", error)
                continue
            payload = unwrap_capability(result, "message")
            if not isinstance(payload, dict):
                continue
            raw, url = _first_image(payload)
            if raw is None and url:
                raw = await self._download(url)
            if raw:
                self._image_cache[message_id] = (time.time(), raw)
                self._prune_image_cache()
                if chat_id:
                    self._vlog("按消息 ID 取图成功（带 chat_id）：id=%s", message_id)
                return raw, url
        self.ctx.logger.warning("按消息 ID 没有取回消息体：id=%s", message_id)
        return None, ""

    async def _recent_images(self, stream_id: str) -> tuple[bytes | None, str, int]:
        """返回 (图片字节, 链接, 过期秒数)。过期秒数只在「有图但太旧」时非 0。"""
        try:
            result = await self.ctx.message.get_recent(chat_id=stream_id, limit=6)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("读取最近消息失败：%s", error)
            return None, "", 0
        messages = unwrap_capability(result, "messages")
        if not isinstance(messages, list):
            return None, "", 0
        now = time.time()
        stale = 0
        for item in reversed(messages):
            if not _has_image(item):
                continue
            try:
                stamp = float(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                stamp = 0.0
            # 只认最近 3 分钟内的图，避免模型在闲聊时抓到很久以前的图
            if stamp and now - stamp > 180:
                stale = max(stale, int(now - stamp))
                continue
            found_raw, found_url = await self._image_by_id(str(item.get("message_id") or ""), stream_id)
            if found_raw:
                return found_raw, found_url, 0
        return None, "", stale

    async def _download(self, url: str) -> bytes | None:
        try:
            return await asyncio.to_thread(
                download_image_multi,
                url,
                self._proxy(),
                self.config.plugin.download_timeout,
            )
        except HttpError as error:
            self.ctx.logger.info("图片下载失败（两条线路都试过）：%s → %s", url[:80], error)
            return None

    async def _best_image(
        self,
        best: Candidate | None,
        ordered: list[Candidate],
        upload: bytes | None,
    ) -> tuple[str, str]:
        """给结果配图：多个候选并发下载，按优先级取第一张成功的。

        best 为空（没找到可靠出处）时照样给一张参考图，方便用户核对是不是同一张；
        候选全失败时退回用户原图；没有原图（比如重放磁盘恢复的任务）就返回空。
        """
        urls: list[str] = []
        for item in [best, *ordered]:
            if item is None:
                continue
            if item.image_url and item.image_url not in urls:
                urls.append(item.image_url)
            if len(urls) >= 4:
                break
        if urls:
            results = await asyncio.gather(*(self._download(url) for url in urls), return_exceptions=True)
            for url, data in zip(urls, results):
                if isinstance(data, BaseException) or not data:
                    continue
                try:
                    compact, _ = prepare_upload(data, limit=1_200_000)
                except Exception:  # noqa: BLE001 - 解不开就换下一张
                    continue
                return base64.b64encode(compact).decode("ascii"), url
        if upload is None:
            return "", ""
        compact, _ = prepare_upload(upload, limit=1_200_000)
        return base64.b64encode(compact).decode("ascii"), "upload"

    async def _ensure_image(self, task: SearchTask) -> None:
        """重放或磁盘恢复的任务没有图片时，按候选图地址补一张。"""
        outcome = task.outcome
        if outcome is None or outcome.image_b64:
            return
        if outcome.best is None and not outcome.candidates:
            return
        image_b64, source = await self._best_image(outcome.best, outcome.candidates, None)
        if image_b64:
            outcome.image_b64 = image_b64
            outcome.image_source = source

    _DISPLAY_IMAGE_BYTES = 60 * 1024
    _DISPLAY_CONCURRENCY = 5

    async def _display_image(self, url: str, semaphore: asyncio.Semaphore) -> str:
        """下载一张候选图并压成展示用大小，失败返回空串。"""
        async with semaphore:
            raw = await self._download(url)
        if not raw:
            return ""
        try:
            compact, _mime = await asyncio.to_thread(prepare_upload, raw, self._DISPLAY_IMAGE_BYTES)
        except Exception:  # noqa: BLE001 - 解不开就不附图，不影响文字结果
            return ""
        return base64.b64encode(compact).decode("ascii")

    async def _ensure_engine_images(self, task: SearchTask, *, limit: int, whole: bool) -> None:
        """给参考区的候选配图。

        首次回复只取每个引擎最接近的那张，回「更多」时把展示范围内的候选全配上。
        展示图压到 100KB 左右，避免合并转发因为体积太大被拒。
        """
        outcome = task.outcome
        if outcome is None or not outcome.candidates:
            return
        targets: list[str] = []
        for engine, items in formatting.engine_groups(outcome, outcome.best, limit):
            if not whole and outcome.best is not None and engine == outcome.best.engine:
                continue  # 首次回复只补其他引擎的图，命中那条所在引擎不重复给
            picked = items if whole else items[:1]
            targets.extend(item.image_url for item in picked if item.image_url)
        pending: list[str] = []
        for url in targets:
            if url in outcome.images or url in pending:
                continue
            if outcome.best is None and url == outcome.image_source:
                continue  # 已经作为参考图贴过，不重复
            pending.append(url)
        if not pending:
            return
        semaphore = asyncio.Semaphore(self._DISPLAY_CONCURRENCY)
        results = await asyncio.gather(*(self._display_image(url, semaphore) for url in pending))
        for url, data in zip(pending, results):
            if data:
                outcome.images[url] = data
        self._vlog(
            "参考区配图：task=%s 目标=%s 成功=%s",
            task.task_id,
            len(pending),
            sum(1 for data in results if data),
        )

    # -- 搜索 -------------------------------------------------------------

    def _call_engine(self, name: str, image: bytes, image_url: str, stream_id: str) -> list[Candidate]:
        engine = self._engines.get(name)
        if engine is None:
            raise EngineError(f"未知引擎 {name}")
        return engine.search(image, image_url, stream_id)

    async def _search(self, raw: bytes, image_url: str = "", stream_id: str = "") -> SearchOutcome:
        config = self.config
        section = config.plugin
        started = time.time()
        target_hash = dhash(raw)
        upload, _mime = prepare_upload(raw)

        names = [name for name in config.engines.priority if self._enabled(name)]
        if not names:
            raise RuntimeError("没有启用任何搜索引擎，请先在配置里开启至少一个引擎")

        timeout = section.engine_timeout + 5
        loop = asyncio.get_running_loop()
        coros = [
            asyncio.wait_for(
                loop.run_in_executor(self._engine_pool, self._call_engine, name, upload, image_url, stream_id),
                timeout=timeout,
            )
            for name in names
        ]
        settled = await asyncio.gather(*coros, return_exceptions=True)

        grouped: dict[str, list[Candidate]] = {}
        errors: dict[str, str] = {}
        engine_counts: dict[str, int] = {}
        # 深度检索：每个引擎按 deep_scan_limit 取数（不超过引擎实际返回），换更高的命中概率。
        deep = bool(section.deep_scan)
        deep_cap = max(1, int(section.deep_scan_limit))
        for name, outcome in zip(names, settled):
            if isinstance(outcome, BaseException):
                errors[name] = f"{type(outcome).__name__}: {str(outcome)[:120]}"
                continue
            engine_counts[name] = len(outcome)
            limit = max(1, int(getattr(config, name).max_results))
            if deep:
                limit = max(limit, deep_cap)
            grouped[name] = outcome[:limit]

        candidates: list[Candidate] = []
        for name in names:
            candidates.extend(grouped.get(name, []))
        total = len(candidates)
        self._vlog(
            "引擎返回：%s；错误：%s；原始候选=%s；深度检索=%s",
            {name: engine_counts.get(name, 0) for name in names},
            errors or "无",
            total,
            "开" if deep else "关",
        )

        # 结果驱动分流：SauceNAO 索引的是插画站点，真人照片基本没有高相似度命中。
        # 这一步只看相似度、不需要下载图片，所以放在验证之前。
        sauce = self._sauce_route(candidates)
        route = "drawing" if sauce else "photo"
        route_reason = f"SauceNAO 相似度 {sauce.similarity:g}%" if sauce else "无插画库命中"
        if sauce:
            names = _dedupe([name for name in config.engines.priority_drawn if name in names] + names)
            # 插画路线：SauceNAO 自带相似度已经能定论，再逐张下载比对没有意义。
            # 其他引擎只留几条意思一下，全部不做下载验证。
            other_limit = max(1, int(section.drawing_other_limit))
            for name in names:
                if name == "saucenao":
                    continue
                if name not in grouped:
                    continue
                grouped[name] = grouped[name][:other_limit]
                # 这些候选没下载比对过，状态和备注都要如实改写，
                # 否则会把引擎自带的「精确匹配」标签和账号原样发出去。
                for item in grouped[name]:
                    item.status = "unverified"
                    item.note = "插画路线参考，未做指纹比对"
            candidates = [item for name in names for item in grouped.get(name, [])]
        else:
            await self._verify(candidates, grouped, names, target_hash)

        ordered = self._rank(candidates, names, prefer_pixiv=sauce is not None)
        # 最佳匹配按指纹质量挑，不受引擎顺序影响；指纹距离优先，其次相似度。
        # 插画路线优先取 pixiv 的命中。
        best = self._pick_best(ordered, prefer_pixiv=sauce is not None)
        self._vlog(
            "分流结果：路线=%s 原因=%s；匹配=%s 疑似=%s 不符=%s 未验证=%s",
            route,
            route_reason,
            sum(1 for c in ordered if c.status == "match"),
            sum(1 for c in ordered if c.status == "suspect"),
            sum(1 for c in ordered if c.status == "mismatch"),
            sum(1 for c in ordered if c.status == "unverified"),
        )

        ai = None
        ai_pool = self._pools.get("ai")
        if (
            config.ai_detect.enabled
            and ai_pool
            and (best is None or not config.ai_detect.only_when_low_confidence)
        ):
            ai = await asyncio.to_thread(
                detect_ai,
                upload,
                ai_pool,
                _proxy_of(section.proxy),
                section.engine_timeout,
            )

        outcome = SearchOutcome(
            status="ok" if best is not None else "not_found",
            best=best,
            candidates=ordered,
            ai=ai,
            route=route,
            route_reason=route_reason,
            errors=errors,
            engine_counts=engine_counts,
            elapsed_ms=int((time.time() - started) * 1000),
            total_candidates=total,
        )

        if best is not None or ordered or upload:
            outcome.image_b64, outcome.image_source = await self._best_image(best, ordered, upload)
        return outcome

    async def _verify(
        self,
        candidates: list[Candidate],
        grouped: dict[str, list[Candidate]],
        names: list[str],
        target_hash: int | None,
    ) -> None:
        section = self.config.plugin
        deep = bool(section.deep_scan)
        targets: list[Candidate] = []
        for name in names:
            if name == "saucenao":
                continue
            items = grouped.get(name) or []
            # Lens 会把 visual_matches 与 exact_matches 一起返回，条数可能几十条，
            # 全量下载验证太慢；取 2 倍配置条数，足够覆盖最相关的前几条。
            normal = max(1, section.verify_top_n) * 2 if name == "lens" else max(1, section.verify_top_n)
            limit = max(1, int(section.deep_verify_top_n), normal) if deep else normal
            targets.extend(items[:limit])
        if not targets:
            return

        loop = asyncio.get_running_loop()

        def check(candidate: Candidate) -> Candidate:
            return verify_candidate(
                candidate,
                target_hash,
                section.hash_threshold,
                self._proxy(),
                section.download_timeout,
            )

        results = await asyncio.gather(
            *(loop.run_in_executor(self._verify_pool, check, candidate) for candidate in targets),
            return_exceptions=True,
        )
        for candidate, result in zip(targets, results):
            if isinstance(result, BaseException):
                candidate.status = "unverified"
                candidate.note = f"验证异常：{str(result)[:50]}"

    def _more_limit(self) -> int:
        """回复「更多」时每个引擎列几条：深度检索取到的多，展示上限也跟着放开。"""
        section = self.config.plugin
        return max(1, section.deep_more_limit if section.deep_scan else section.more_limit)

    def _eta_text(self) -> str:
        """给用户看的耗时提示。深度检索要明显更久，别按乐观值写死。"""
        return "约 2 分钟" if self.config.plugin.deep_scan else "约 60 秒"

    def _sauce_route(self, candidates: list[Candidate]) -> Candidate | None:
        """SauceNAO 高相似度命中即认为这张是插画。"""
        if not self._enabled("saucenao"):
            return None
        threshold = self.config.saucenao.route_similarity
        for item in candidates:
            if item.engine != "saucenao" or item.status != "match":
                continue
            if (item.similarity or 0) >= threshold:
                return item
        return None

    @staticmethod
    def _is_x(item: Candidate) -> bool:
        """X / Twitter 平台的结果在指纹距离相同时优先采用。"""
        return is_x_platform(item.platform)

    @staticmethod
    def _pick_best(ordered: list[Candidate], prefer_pixiv: bool = False) -> Candidate | None:
        matches = [item for item in ordered if item.status == "match"]
        if not matches:
            return None
        if prefer_pixiv:
            # 插画路线：SauceNAO 常同时给出转载站和 pixiv 原图，优先当作画师原帖。
            # 但只在相似度接近时才让 pixiv 插队，避免低分 pixiv 命中盖掉高分结果。
            best_score = max(item.similarity or 0 for item in matches)
            pixiv = [
                item
                for item in matches
                if is_pixiv_platform(item.platform, item.url) and (item.similarity or 0) >= best_score - 5
            ]
            if pixiv:
                matches = pixiv
        hashed = [item for item in matches if item.distance is not None]
        if hashed:
            return min(
                hashed,
                key=lambda item: (
                    item.distance if item.distance is not None else 999,
                    0 if ImageSearcherPlugin._is_x(item) else 1,
                ),
            )
        return max(matches, key=lambda item: item.similarity or 0)

    def _rank(
        self,
        candidates: list[Candidate],
        names: list[str],
        prefer_pixiv: bool = False,
    ) -> list[Candidate]:
        order = {name: index for index, name in enumerate(names)}
        status_weight = {"match": 0, "suspect": 1, "unverified": 2, "mismatch": 3, "error": 4}

        def key(item: Candidate) -> tuple:
            distance = item.distance if item.distance is not None else 999
            similarity = -(item.similarity if item.similarity is not None else 0)
            pixiv_first = 0 if prefer_pixiv and is_pixiv_platform(item.platform, item.url) else 1
            # 引擎顺序放在质量之前：优先级就是展示顺序，照片 Lens 先、插画 SauceNAO 先。
            # X / Twitter 只在指纹距离完全相同时插队，不会盖过更接近的图。
            return (
                status_weight.get(item.status, 2),
                pixiv_first,
                order.get(item.engine, 99),
                item.rank,
                distance,
                0 if self._is_x(item) else 1,
                similarity,
            )

        return sorted(candidates, key=key)

    # -- 任务 -------------------------------------------------------------

    def _start_task(self, raw: bytes, image_url: str, stream_id: str, user_id: str, *, notify: bool) -> SearchTask:
        if self._tasks is None:
            raise RuntimeError("插件尚未初始化")
        if not self.config.plugin.enabled:
            raise RuntimeError("插件已禁用")
        if self._tasks.running_count() >= max(1, self.config.plugin.max_concurrent_tasks):
            raise RuntimeError("当前搜索任务太多，请稍后再试")

        task = self._tasks.new_task(stream_id, user_id)
        background = asyncio.create_task(self._run_task(task, raw, image_url, notify))
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return task

    async def _run_task(self, task: SearchTask, raw: bytes, image_url: str, notify: bool) -> None:
        try:
            outcome = await self._search(raw, image_url, task.stream_id)
            if self._tasks:
                self._tasks.finish(task, outcome)
            self._vlog(
                "搜图完成：task=%s 状态=%s 路线=%s 候选=%s 引擎=%s 耗时=%sms 错误=%s",
                task.task_id,
                outcome.status,
                outcome.route,
                outcome.total_candidates,
                outcome.engine_counts,
                outcome.elapsed_ms,
                outcome.errors or "无",
            )
            if outcome.best is not None:
                self._vlog(
                    "最佳匹配：引擎=%s 平台=%s 名字=%s 账号=%s 距离=%s 相似度=%s 图源=%s",
                    outcome.best.engine,
                    outcome.best.platform,
                    outcome.best.name,
                    outcome.best.handle,
                    outcome.best.distance,
                    outcome.best.similarity,
                    outcome.image_source or "无",
                )
            if notify:
                await self._send_forward(task, self.config.plugin.reference_limit)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("搜图任务失败：%s", error, exc_info=True)
            if self._tasks:
                self._tasks.fail(task, str(error))
            if notify and task.stream_id:
                await self.ctx.send.text(f"搜图失败：{error}", task.stream_id)

    # QQ 单条合并转发的图片总量有上限，超过后 send_group_forward_msg 会被对端拒绝。
    # 实测 6 张图能过、13 张失败，所以「更多」按这个预算拆成几条发。
    _MAX_FORWARD_IMAGES = 6

    @staticmethod
    def _planned_images(outcome: SearchOutcome, groups: list[tuple[str, list[Candidate]]]) -> int:
        total = 1 if outcome.image_b64 else 0
        for _engine, items in groups:
            total += sum(1 for item in items if formatting.candidate_image(outcome, item))
        return total

    async def _send_engine_forwards(
        self,
        task: SearchTask,
        groups: list[tuple[str, list[Candidate]]],
        section: Any,
    ) -> None:
        """图太多时按引擎拆成几条合并转发，每条不超过 6 张图。"""
        outcome = task.outcome
        if outcome is None:
            return
        batches: list[tuple[str, list[Candidate]]] = []
        for engine, items in groups:
            chunk: list[Candidate] = []
            used = 0
            for item in items:
                cost = 1 if formatting.candidate_image(outcome, item) else 0
                if chunk and used + cost > self._MAX_FORWARD_IMAGES:
                    batches.append((engine, chunk))
                    chunk, used = [], 0
                chunk.append(item)
                used += cost
            if chunk:
                batches.append((engine, chunk))
        if not batches:
            return

        total = len(batches)
        delivered = 0
        for index, (engine, items) in enumerate(batches, start=1):
            nodes = formatting.build_engine_forward_nodes(
                task,
                engine,
                items,
                command=COMMAND,
                nickname=section.nickname,
                ttl_minutes=section.task_ttl_minutes,
                user_id=self._self_id or "0",
                part=(index, total),
                with_best=(index == 1),
                include_footer=(index == total),
            )
            try:
                sent = await self.ctx.send.forward(nodes, task.stream_id)
            except Exception as error:  # noqa: BLE001 - 单批失败不影响后续批次
                self.ctx.logger.warning("分条转发异常：批次=%s/%s 引擎=%s err=%s", index, total, engine, error)
                continue
            self._vlog(
                "分条合并转发：task=%s 批次=%s/%s 引擎=%s 候选=%s 图=%s 返回类型=%s 成功=%s",
                task.task_id,
                index,
                total,
                engine,
                len(items),
                sum(1 for item in nodes for part in item.get("content") or [] if part.get("type") == "image"),
                type(sent).__name__,
                _send_ok(sent),
            )
            if _send_ok(sent):
                delivered += 1
            else:
                self.ctx.logger.warning("分条转发被拒绝：批次=%s/%s 引擎=%s", index, total, engine)
            await asyncio.sleep(1)
        if delivered == 0:
            text = formatting.build_text_summary(
                task,
                command=COMMAND,
                show_reference=section.show_reference,
                limit=self.config.plugin.reference_limit,
                eta_text=self._eta_text(),
            )
            fallback = await self.ctx.send.text(text, task.stream_id)
            if not _send_ok(fallback):
                self.ctx.logger.warning("分条转发全部失败，文字退路也失败：task=%s", task.task_id)

    async def _send_forward(self, task: SearchTask, limit: int, *, replay: bool = False) -> None:
        if self._tasks is None or not task.stream_id:
            return
        await self._ensure_image(task)
        await self._ensure_engine_images(task, limit=limit, whole=replay)
        section = self.config.plugin
        outcome = task.outcome
        if replay and outcome is not None:
            groups = formatting.engine_groups(outcome, outcome.best, limit)
            if len(groups) > 1 and self._planned_images(outcome, groups) > self._MAX_FORWARD_IMAGES:
                await self._send_engine_forwards(task, groups, section)
                return
        nodes = formatting.build_forward_nodes(
            task,
            command=COMMAND,
            show_reference=section.show_reference,
            limit=limit,
            nickname=section.nickname,
            ttl_minutes=section.task_ttl_minutes,
            replay=replay,
            user_id=self._self_id or "0",
        )
        try:
            sent = await self.ctx.send.forward(nodes, task.stream_id)
            self._vlog(
                "合并转发已发送：task=%s replay=%s 节点=%s 图=%s 返回类型=%s",
                task.task_id,
                replay,
                len(nodes),
                sum(1 for item in nodes for part in item.get("content") or [] if part.get("type") == "image"),
                type(sent).__name__,
            )
            if not _send_ok(sent):
                raise RuntimeError(f"合并转发被拒绝：{sent}")
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("合并转发失败，退回文字消息：%s", error)
            text = formatting.build_text_summary(
                task,
                command=COMMAND,
                show_reference=section.show_reference,
                limit=limit,
                eta_text=self._eta_text(),
            )
            fallback = await self.ctx.send.text(text, task.stream_id)
            if not _send_ok(fallback):
                self.ctx.logger.warning("转发退路也失败：task=%s 文本长度=%s", task.task_id, len(text))

    # -- 等图 -------------------------------------------------------------

    def _pending_key(self, kwargs: dict, stream_id: str) -> tuple[str, str]:
        return (self._stream_id(kwargs) or stream_id, self._user_id(kwargs))

    async def _wait_for_image(self, current: str, user_id: str) -> None:
        """指令没带图：提示一次，并在超时时间内接住用户补发的单图。"""
        key = (current, user_id)
        seconds = max(10, self.config.plugin.image_wait_seconds)
        self._pending[key] = time.time() + seconds
        self._vlog("进入等待图片窗口：key=%s 秒数=%s", key, seconds)
        sent = await self.ctx.send.text(
            f"没看到图片。把图发出来，或者引用一条带图的消息再发「{COMMAND}」，我在 {seconds} 秒内等你。",
            current,
        )
        if not _send_ok(sent):
            self.ctx.logger.warning("等待图片提示发送失败：key=%s result=%s", key, sent)
        old = self._pending_timers.pop(key, None)
        if old is not None:
            old.cancel()
        timer = asyncio.create_task(self._expire_pending(key, seconds))
        self._pending_timers[key] = timer
        self._background.add(timer)
        timer.add_done_callback(self._background.discard)

    async def _expire_pending(self, key: tuple[str, str], seconds: int) -> None:
        try:
            await asyncio.sleep(seconds + 1)
        except asyncio.CancelledError:
            return
        if self._pending.pop(key, None) is None:
            return
        self._pending_timers.pop(key, None)
        if not key[0]:
            return
        sent = await self.ctx.send.text("等图超时了，重新发图再发指令就行。", key[0], set_reply=True)
        if not _send_ok(sent):
            self.ctx.logger.warning("等待图片超时提示（引用回复）发送失败，改用普通消息重试：key=%s", key)
            await self.ctx.send.text("等图超时了，重新发图再发指令就行。", key[0])

    # -- Tool 组件 --------------------------------------------------------

    @Tool(
        "image_search",
        brief_description="用户发来图片、想查这张图的原始出处或作者社交账号时使用。",
        detailed_description=(
            "从当前聊天里取一张图片，反查它的原始出处与账号。"
            "取图范围：用户引用的那张图，或当前会话最近 3 分钟内收到的图；"
            "更早的图取不到，此时应让用户在消息里引用那张图再试，不要反复重试本工具。"
            "搜索通常要 1 分钟上下（开了深度检索更久），因此本工具立即返回 task_id，不等待结果。"
            "返回后请告诉用户「正在搜索，约 1 分钟」，稍后再调用 search_status 取结果。"
            "本工具只返回文字摘要，不会发图片。"
        ),
        parameters=[
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="可选，本次搜索的备注，会写进日志",
                required=False,
            ),
        ],
    )
    async def image_search(self, note: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = self._stream_id(kwargs)
        user_id = self._user_id(kwargs)
        raw, image_url, source, stale = await self._resolve_image(kwargs, allow_recent=True)
        self._vlog("Tool 取图：来源=%s 体积=%sKB 链接=%s", source or "无", len(raw) // 1024 if raw else 0, (image_url or "无")[:80])
        if not raw:
            if stale:
                message = formatting.stale_image_hint(stale)
            else:
                message = "没有找到可用的图片。请让用户先发一张图，或引用一条带图的消息再试。"
            return {"success": False, "content": message, "message": message}

        try:
            task = self._start_task(raw, image_url, stream_id, user_id, notify=True)
        except Exception as error:  # noqa: BLE001
            return {"success": False, "content": f"启动搜图失败：{error}", "message": str(error)}

        message = (
            f"已开始搜索（task_id={task.task_id}，图片来源：{source}），预计 {self._eta_text()}。"
            f"结果会以合并转发发到聊天里；也可以用 search_status 凭 task_id 取文字摘要。"
        )
        self.ctx.logger.info("Tool 启动搜图：task=%s user=%s note=%s", task.task_id, user_id, note)
        return {
            "success": True,
            "task_id": task.task_id,
            "content": message,
            "message": message,
        }

    @Tool(
        "search_status",
        brief_description="查询以图搜图任务的进度或结果。",
        detailed_description=(
            "凭 task_id 查询 image_search 启动的搜索任务；不传 task_id 时取当前会话最近一次任务。"
            "任务还在跑就返回进度提示；已完成则返回最佳匹配的平台、名字、账号、链接与 AI 检测结论。"
            "结果只包含文字，不含图片。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="image_search 返回的任务编号；留空表示最近一次任务",
                required=False,
            ),
        ],
    )
    async def search_status(self, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if self._tasks is None:
            return {"success": False, "content": "插件尚未初始化"}
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            task = self._latest_task(self._stream_id(kwargs), self._user_id(kwargs))
        if task is None:
            message = f"没有找到对应的搜索任务，可能已过期（结果保留 {self.config.plugin.task_ttl_minutes} 分钟）。"
            return {"success": False, "content": message, "message": message}

        if task.status == "running":
            message = f"任务 {task.task_id} 还在搜索中，已等待 {task.elapsed:.0f} 秒，请稍后再查。"
            return {"success": True, "task_id": task.task_id, "status": "running", "content": message}

        section = self.config.plugin
        summary = formatting.build_text_summary(
            task,
            command=COMMAND,
            show_reference=section.show_reference,
            limit=section.reference_limit,
            eta_text=self._eta_text(),
        )
        return {
            "success": task.status == "done",
            "task_id": task.task_id,
            "status": task.status,
            "content": summary,
            "message": summary,
        }

    # -- Command 组件 -----------------------------------------------------

    @Command(
        "image_search_command",
        description="以图搜出处：群里发图后带上指令即可",
        pattern=_COMMAND_PATTERN,
    )
    async def image_search_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        return await self._handle_command(stream_id, kwargs)

    @Command(
        "image_search_more",
        description="重发上次搜图结果",
        pattern=_MORE_PATTERN,
    )
    async def image_search_more(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        if self._tasks is None:
            return False, "插件尚未初始化", 0
        current = self._stream_id(kwargs) or stream_id
        user_id = self._user_id(kwargs)
        task = self._latest_task(current, user_id)
        if task is None or task.status != "done":
            return False, "", 0  # 没有可重发的任务，交给正常聊天流程
        await self._send_forward(task, self._more_limit(), replay=True)
        return True, "已重发上次结果", 2

    @Command(
        "image_search_capture",
        description="接住等待中的单张图片消息",
        pattern=_CAPTURE_PATTERN,
    )
    async def image_search_capture(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        current = self._stream_id(kwargs) or stream_id
        user_id = self._user_id(kwargs)
        key = (current, user_id)
        deadline = self._pending.get(key)
        if not deadline or time.time() > deadline:
            return False, "", 0

        raw, image_url, _source, _stale = await self._resolve_image(kwargs, allow_recent=False)
        if not raw:
            self.ctx.logger.warning("等待中的图片消息没有取到图，继续等：key=%s", key)
            await self.ctx.send.text("这张图我这边没取到，再发一次试试。", current)
            return False, "", 0

        self._pending.pop(key, None)
        timer = self._pending_timers.pop(key, None)
        if timer is not None:
            timer.cancel()

        try:
            self._start_task(raw, image_url, current, user_id, notify=True)
        except Exception as error:  # noqa: BLE001
            await self.ctx.send.text(f"启动搜图失败：{error}", current)
            return False, str(error), 1

        await self.ctx.send.text(f"🔍 收到图片，开始搜图，{self._eta_text()}。", current)
        return True, "搜图中", 2

    async def _handle_command(self, stream_id: str, kwargs: dict) -> tuple[bool, str, int]:
        current = self._stream_id(kwargs) or stream_id
        user_id = self._user_id(kwargs)
        matched = kwargs.get("matched_groups") or {}
        arg = str(matched.get("arg") or "").strip()

        if arg in {"更多", "more", "全部"}:
            if self._tasks is None:
                return False, "插件尚未初始化", 1
            task = self._latest_task(current, user_id)
            if task is None or task.status != "done":
                await self.ctx.send.text(
                    f"没有可展开的结果，可能已超过 {self.config.plugin.task_ttl_minutes} 分钟。请重新发图并发送 {COMMAND}",
                    current,
                )
                return True, "没有可用的历史结果", 2
            await self._send_forward(task, self._more_limit(), replay=True)
            return True, "已重发完整结果", 2

        if arg in {"状态", "status"}:
            if self._tasks is not None:
                task = self._latest_task(current, user_id)
                if task is None:
                    await self.ctx.send.text("当前没有进行中的搜索任务。", current)
                elif task.status == "running":
                    await self.ctx.send.text(f"任务 {task.task_id} 搜索中，已等待 {task.elapsed:.0f} 秒。", current)
                else:
                    await self.ctx.send.text(f"任务 {task.task_id} 状态：{task.status}。", current)
            return True, "状态已发送", 2

        if not self.config.plugin.enabled:
            return False, "插件已禁用", 0
        if not current:
            return False, "无法获取会话 ID", 1

        raw, image_url, source, _stale = await self._resolve_image(kwargs)
        self._vlog("指令取图：来源=%s 体积=%sKB 链接=%s", source or "无", len(raw) // 1024 if raw else 0, (image_url or "无")[:80])
        if not raw:
            await self._wait_for_image(current, user_id)
            return True, "等待图片", 2

        try:
            task = self._start_task(raw, image_url, current, user_id, notify=True)
        except Exception as error:  # noqa: BLE001
            await self.ctx.send.text(f"启动搜图失败：{error}", current)
            return False, str(error), 1

        await self.ctx.send.text(
            f"🔍 正在搜图（{source}），{self._eta_text()}，结果稍后以合并转发发出。",
            current,
        )
        return True, "搜图中", 2


def create_plugin() -> ImageSearcherPlugin:
    """创建插件实例。"""
    return ImageSearcherPlugin()
