"""配置兼容：把老版本塞在 [plugin] 下的字段搬进各自的配置节。

0.5.5 起 `vision_*` 与 `grok_*` 从 `[plugin]` 拆成独立的 `[vision]` / `[grok]`
配置节，WebUI 里各占一列。老配置的键还留在 `[plugin]` 下，不搬就等于丢失
（Grok 的 key 尤其难补），因此保留这张映射表做一次性迁移。

纯函数，不依赖插件上下文，改动语义先补 `tests/test_config.py`。
"""

from __future__ import annotations

from typing import Any

# 配置节名 -> (老字段前缀, 老字段去掉前缀后的名字到新字段名的映射)
LEGACY_SECTIONS: dict[str, tuple[str, dict[str, str]]] = {
    "vision": (
        "vision_",
        {
            "fallback": "enabled",
            "top_n": "top_n",
            "task": "task",
            "model": "model",
            "timeout": "timeout",
        },
    ),
    "grok": (
        "grok_",
        {
            "fallback": "enabled",
            "base_url": "base_url",
            "api_key": "api_key",
            "model": "model",
            "timeout": "timeout",
        },
    ),
}


def migrate_legacy_sections(config_data: Any) -> tuple[dict[str, Any], bool]:
    """把 `[plugin]` 下的 vision_* / grok_* 搬进 `[vision]` / `[grok]`。

    只在新配置节还没有同名键时写入：用户在 WebUI 里改过的值不会被旧配置覆盖。
    老键从返回值里删掉，宿主下次写回时文件里就只剩新结构。
    """
    if not isinstance(config_data, dict):
        return {}, False
    data = dict(config_data)
    legacy_plugin = data.get("plugin")
    if not isinstance(legacy_plugin, dict):
        return data, False
    plugin_section = dict(legacy_plugin)

    changed = False
    for name, (prefix, mapping) in LEGACY_SECTIONS.items():
        legacy = {key: value for key, value in plugin_section.items() if key.startswith(prefix)}
        if not legacy:
            continue
        section = dict(data.get(name) or {})
        for key, value in legacy.items():
            target = mapping.get(key[len(prefix) :])
            if target and target not in section:
                section[target] = value
        for key in legacy:
            plugin_section.pop(key, None)
        data[name] = section
        changed = True

    if changed:
        data["plugin"] = plugin_section
    return data, changed
