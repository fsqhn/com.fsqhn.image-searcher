"""多 key 轮询与冷却。

每个需要 key 的引擎都配一个 KeyPool：轮询取用，某个 key 遇到 401/403/429
或超时就进入冷却，自动换下一个；全部冷却时该引擎本次跳过，不影响其他引擎。
"""

from __future__ import annotations

import threading
import time


class KeyPool:
    """线程安全的 key 轮询池。"""

    def __init__(self, keys: list[str] | None = None, cooldown_seconds: float = 300.0) -> None:
        cleaned = [str(key).strip() for key in (keys or []) if str(key).strip()]
        self._keys = cleaned
        self._cooldown = float(cooldown_seconds)
        self._lock = threading.Lock()
        self._cursor = 0
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    def acquire(self) -> str | None:
        """取一个当前可用的 key；全部冷却时返回 None。"""
        with self._lock:
            if not self._keys:
                return None
            now = time.time()
            total = len(self._keys)
            for offset in range(total):
                index = (self._cursor + offset) % total
                key = self._keys[index]
                if self._cooldown_until.get(key, 0.0) <= now:
                    self._cursor = (index + 1) % total
                    return key
            return None

    def mark_failure(self, key: str, cooldown_seconds: float | None = None) -> None:
        """标记失败并进入冷却，默认 300 秒。"""
        if not key:
            return
        duration = self._cooldown if cooldown_seconds is None else float(cooldown_seconds)
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1
            self._cooldown_until[key] = time.time() + duration

    def mark_success(self, key: str) -> None:
        """成功一次就解除冷却并清零失败计数。"""
        if not key:
            return
        with self._lock:
            self._cooldown_until.pop(key, None)
            self._failures.pop(key, None)

    def available_count(self) -> int:
        with self._lock:
            now = time.time()
            return sum(1 for key in self._keys if self._cooldown_until.get(key, 0.0) <= now)

    def cooldown_remaining(self, key: str) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until.get(key, 0.0) - time.time())
