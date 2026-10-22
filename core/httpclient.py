"""HTTP 会话与图片下载。

SerpAPI 和 twimg 在国内直连不通，需要走本地代理；引擎与图片下载共用
``[plugin] proxy`` 这一个代理设置，留空即直连。
"""

from __future__ import annotations

from typing import Any

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

MAX_IMAGE_BYTES = 12 * 1024 * 1024


class HttpError(RuntimeError):
    """网络请求失败。"""


def make_session(proxy: str = "", *, headers: dict[str, str] | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if headers:
        session.headers.update(headers)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def download_image(url: str, proxy: str = "", timeout: float = 15.0) -> bytes:
    """下载候选图，返回原始字节；超过大小上限直接放弃。"""
    if not url:
        raise HttpError("图片地址为空")
    session = make_session(proxy, headers={"Referer": ""})
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            if resp.status_code != 200:
                raise HttpError(f"HTTP {resp.status_code}")
            buffer = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) > MAX_IMAGE_BYTES:
                    raise HttpError("图片超过大小上限")
            if not buffer:
                raise HttpError("图片内容为空")
            return bytes(buffer)
    except HttpError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读错误
        raise HttpError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        session.close()


def json_or_raise(resp: requests.Response, engine: str) -> dict[str, Any]:
    """统一的 JSON 解析与错误提示。"""
    if resp.status_code >= 400:
        raise HttpError(f"{engine} 返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HttpError(f"{engine} 返回非 JSON 内容") from exc
    if not isinstance(data, dict):
        raise HttpError(f"{engine} 返回格式异常")
    return data


def is_auth_failure(status_code: int) -> bool:
    """这些状态码说明 key 有问题或额度用完，应当冷却换 key。"""
    return status_code in (401, 402, 403, 429)
