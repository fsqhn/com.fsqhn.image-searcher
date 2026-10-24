"""HTTP 会话与图片下载。

SerpAPI 和 twimg 在国内直连不通，需要走本地代理；引擎与图片下载共用
``[plugin] proxy`` 这一个代理设置，留空即直连。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

MAX_IMAGE_BYTES = 12 * 1024 * 1024

# 这些图床在国内直连比走代理更快也更稳：实测 avatars.mds.yandex.net 直连约 1 秒，
# 走代理时常 15 秒以上甚至 SSL 报错，验证阶段会因为超时把结果全标成「未验证」。
_DIRECT_FIRST_SUFFIXES = ("yandex.net", "yandex.ru", "yandex.com", "yandex.com.tr")

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}


class HttpError(RuntimeError):
    """网络请求失败。"""


def is_public_url(url: str) -> bool:
    """只放行指向公网地址的 http(s) 链接。

    图片 URL 有两个来源：聊天消息里的图片段和搜索引擎返回的候选图。前者
    可以由群成员构造，所以不能直接拿去请求——否则一条伪造的图片段就能让
    进程带着用户配的代理去访问本机、内网或云厂商的元数据接口。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return False

    addresses: list[ipaddress._BaseAddress] = []
    try:
        addresses.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError):
            return False
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        return False
    return all(address.is_global for address in addresses)



def make_session(proxy: str = "", *, headers: dict[str, str] | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if headers:
        session.headers.update(headers)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def download_routes(url: str, proxy: str = "") -> list[str]:
    """按主机决定先走哪条线路，返回要去重尝试的顺序（"" 表示直连）。"""
    host = (urlparse(url).hostname or "").lower()
    direct_first = any(host == suffix or host.endswith("." + suffix) for suffix in _DIRECT_FIRST_SUFFIXES)
    routes = ["", proxy] if direct_first else [proxy, ""]
    ordered: list[str] = []
    for route in routes:
        if route not in ordered:
            ordered.append(route)
    return ordered


# 这些错误是瞬时的（TLS 被中途掐断、连接重置、超时），换线路或重试一次通常就能过。
_RETRY_HINTS = (
    "sslerror",
    "ssleoferror",
    "eof",
    "timeout",
    "timed out",
    "connection",
    "reset",
    "max retries",
    "http 5",
)


def is_transient_error(error: BaseException) -> bool:
    """判断下载失败是否值得重试：对端明确拒绝（4xx）或内容不合规就不重试。"""
    text = str(error).lower()
    return any(hint in text for hint in _RETRY_HINTS)


def download_image_multi(url: str, proxy: str = "", timeout: float = 15.0, attempts: int = 3) -> bytes:
    """下载图片，失败时换线路并重试。

    Yandex 图床直连优先（代理反而更慢），其他站点先走代理。TLS 被中途掐断这类
    瞬时错误很常见，单次尝试经常白给，所以按线路轮换重试几次；对端明确拒绝的
    （403/404、地址不合规）不浪费次数。
    """
    routes = download_routes(url, proxy)
    last_error: HttpError | None = None
    total = max(1, attempts)
    for index in range(total):
        route = routes[index % len(routes)]
        try:
            return download_image(url, route, timeout)
        except HttpError as exc:
            last_error = exc
            if not is_transient_error(exc):
                break
            if index + 1 < total:
                time.sleep(0.3)
    raise last_error or HttpError("图片下载失败")


def download_image(url: str, proxy: str = "", timeout: float = 15.0) -> bytes:
    """下载候选图，返回原始字节；超过大小上限直接放弃。"""
    if not url:
        raise HttpError("图片地址为空")
    if not is_public_url(url):
        raise HttpError("图片地址不是公网 http(s)，已拒绝")
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
