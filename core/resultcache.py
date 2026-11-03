"""按图缓存：同一张图搜过一次，下次直接复用结果。

缓存键是「图片内容 sha256 + 引擎解析版本」：
- 用内容而不是消息号，同一张图在不同群反复发也能命中；
- 版本号跟着解析逻辑走，解析规则一改就自动作废，不会读到旧格式的结果。

只缓存「搜到了」的结果：没找到的图不缓存。实测同一张图过半天就能搜到，
把「没找到」存下来的意义不大，还会挡住后来才出现的原帖。要绕开缓存重搜，
调用方传 force 就行。

全部是纯函数，落盘由 ResultCache 负责。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

STORE_NAME = "image_searcher_cache.json"
DEFAULT_MAX_ENTRIES = 500


@dataclass
class CacheHit:
    """一次缓存命中。"""

    payload: dict
    stored_at: float
    age_seconds: float


def cache_key(upload: bytes, engines_version: str) -> str:
    """图片内容 + 引擎版本 → 缓存键。"""
    digest = hashlib.sha256(upload or b"").hexdigest()
    return f"{digest}:{engines_version}"


def cache_get(
    store: dict,
    key: str,
    now: float | None = None,
    ttl_seconds: float = 30 * 86400,
) -> CacheHit | None:
    """取一条缓存。过期返回 None。"""
    entry = (store or {}).get(key)
    if not isinstance(entry, dict):
        return None
    payload = entry.get("result")
    if not isinstance(payload, dict):
        return None
    # 只认「搜到了」的缓存：旧版本可能存过没找到的结果，读到时直接忽略。
    if str(payload.get("status") or "") != "ok":
        return None
    stored_at = float(entry.get("at") or 0)
    age = float(now if now is not None else time.time()) - stored_at
    if age < 0 or age > ttl_seconds:
        return None
    return CacheHit(payload=payload, stored_at=stored_at, age_seconds=age)


def cache_put(
    store: dict,
    key: str,
    payload: dict,
    now: float | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict:
    """写入一条缓存，超出条数上限时按时间淘汰最旧的。返回新字典。"""
    next_store = dict(store or {})
    next_store[key] = {"at": float(now if now is not None else time.time()), "result": payload}
    limit = max(1, int(max_entries or DEFAULT_MAX_ENTRIES))
    if len(next_store) > limit:
        ordered = sorted(next_store.items(), key=lambda item: float(item[1].get("at") or 0))
        for stale, _entry in ordered[: len(next_store) - limit]:
            next_store.pop(stale, None)
    return next_store


def load_store(path: str | Path) -> dict:
    """读缓存文件。坏文件当空缓存，不让它挡住搜图。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 损坏 / 不存在都当空的
        return {}
    return raw if isinstance(raw, dict) else {}


def save_store(path: str | Path, store: dict) -> None:
    """原子写：先写临时文件再替换，避免半截文件。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(store or {}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001 - 落盘失败不影响这一次搜索
        pass


class ResultCache:
    """落盘的结果缓存。读写都不抛异常，最坏情况是没命中。"""

    def __init__(
        self,
        data_dir: str | Path,
        ttl_seconds: float = 30 * 86400,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._path = Path(data_dir) / STORE_NAME
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._store: dict = load_store(self._path)

    @property
    def size(self) -> int:
        return len(self._store)

    def get(self, key: str, now: float | None = None) -> CacheHit | None:
        return cache_get(self._store, key, now, self._ttl)

    def put(self, key: str, payload: dict, now: float | None = None) -> None:
        self._store = cache_put(self._store, key, payload, now, self._max)
        save_store(self._path, self._store)
