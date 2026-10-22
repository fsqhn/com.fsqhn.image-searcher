"""MaiBot 能力返回值兼容。

SDK 的 `_normalize_capability_result` 会把单字段能力的结果解包成原始值：
`message.get_by_id` 返回消息字典本身，`message.get_recent` 返回消息列表。
如果 Host 没有套 RPC 包装字典（结果里没有 success 键），SDK 就原样返回。
两条路径都要兼容，否则取图会静默失败。
"""

from __future__ import annotations

from typing import Any


def unwrap_capability(result: Any, key: str) -> Any:
    """取出能力结果里的业务字段；失败返回 None。

    三种形态：
    1. SDK 已解包 → 直接是业务值；
    2. Host 仍返回包装字典 {"success": True, key: ...} → 取 key；
    3. 包装字典但 success 为假 → None。
    """
    if isinstance(result, dict) and "success" in result:
        if not result.get("success"):
            return None
        return result.get(key)
    return result
