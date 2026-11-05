"""远程图片地址过滤：群成员构造的图片段不能把请求导向本机或内网。"""

from __future__ import annotations

import io
import unittest
from unittest import mock

from PIL import Image

from core import httpclient
from core.hashing import dhash, prepare_upload
from core.httpclient import HttpError, download_image_multi, download_routes, is_public_url, is_transient_error


class PublicUrlTest(unittest.TestCase):
    def test_loopback_and_private_are_rejected(self) -> None:
        for url in (
            "http://127.0.0.1/a.jpg",
            "http://127.1.2.3:8000/a.jpg",
            "http://localhost/a.jpg",
            "http://foo.localhost/a.jpg",
            "http://10.0.0.8/a.jpg",
            "http://192.168.1.1/a.jpg",
            "http://172.16.3.4/a.jpg",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/a.jpg",
            "http://[fe80::1]/a.jpg",
            "http://metadata.google.internal/computeMetadata/v1/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))

    def test_non_http_scheme_is_rejected(self) -> None:
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/a.jpg", "gopher://1.1.1.1/"):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))

    def test_public_ip_is_allowed(self) -> None:
        self.assertTrue(is_public_url("https://1.1.1.1/a.jpg"))
        self.assertTrue(is_public_url("http://8.8.8.8/a.jpg"))

    def test_empty_and_garbage_are_rejected(self) -> None:
        for url in ("", "not a url", "https://"):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url))


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height), 128).save(buffer, format="PNG")
    return buffer.getvalue()


class PixelGuardTest(unittest.TestCase):
    def test_hash_skips_oversized_image(self) -> None:
        raw = _png(300, 300)
        with mock.patch("core.hashing.MAX_PIXELS", 1000):
            self.assertIsNone(dhash(raw))

    def test_upload_rejects_oversized_image(self) -> None:
        raw = _png(300, 300)
        with mock.patch("core.hashing.MAX_PIXELS", 1000):
            with self.assertRaises(ValueError):
                prepare_upload(raw)


class DownloadRouteTest(unittest.TestCase):
    """Yandex 图床直连更快，代理反而超时，所以线路顺序要按主机分。"""

    def test_yandex_cdn_prefers_direct(self) -> None:
        proxy = "http://127.0.0.1:6789"
        self.assertEqual(
            download_routes("https://avatars.mds.yandex.net/i?id=1", proxy),
            ["", proxy],
        )
        self.assertEqual(
            download_routes("https://avatars.mds.yandex.ru/i?id=1", proxy),
            ["", proxy],
        )

    def test_other_hosts_prefer_proxy(self) -> None:
        proxy = "http://127.0.0.1:6789"
        self.assertEqual(
            download_routes("https://cdn.donmai.us/a.jpg", proxy),
            [proxy, ""],
        )

    def test_without_proxy_only_direct(self) -> None:
        self.assertEqual(download_routes("https://cdn.donmai.us/a.jpg", ""), [""])
        self.assertEqual(download_routes("https://avatars.mds.yandex.net/x", ""), [""])


class DownloadRetryTest(unittest.TestCase):
    """Yandex 图床的 TLS 连接常被中途掐断，单次失败要换线路重试。"""

    def test_transient_error_is_retried_on_other_route(self) -> None:
        proxy = "http://127.0.0.1:6789"
        calls = []

        def fake_download(url, route="", timeout=15.0):
            calls.append(route)
            if len(calls) == 1:
                raise HttpError("SSLError: SSLEOFError(8, 'EOF occurred in violation of protocol')")
            return b"image"

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            data = download_image_multi("https://avatars.mds.yandex.net/i?id=1", proxy, timeout=1)

        self.assertEqual(data, b"image")
        self.assertEqual(calls, ["", proxy])

    def test_403_retries_once_with_referer(self) -> None:
        """403 是图床要来源页，不是线路问题：带 Referer 再试一次。"""
        calls = []

        def fake_download(url, route="", timeout=15.0, referer=""):
            calls.append(referer)
            if not referer:
                raise HttpError("HTTP 403")
            return b"image"

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            data = download_image_multi("https://fapello.com/content/a.jpg", "http://127.0.0.1:6789", timeout=1)

        self.assertEqual(data, b"image")
        self.assertEqual(calls, ["", "https://fapello.com/"])

    def test_403_is_retried_only_once(self) -> None:
        """带 Referer 还是 403 就放弃，不要无休止地打对端。"""
        calls = []

        def fake_download(url, route="", timeout=15.0, referer=""):
            calls.append(referer)
            raise HttpError("HTTP 403")

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            with self.assertRaises(HttpError):
                download_image_multi("https://cdn.donmai.us/a.jpg", "http://127.0.0.1:6789", timeout=1)

        self.assertEqual(len(calls), 2)

    def test_404_is_not_retried(self) -> None:
        calls = []

        def fake_download(url, route="", timeout=15.0, referer=""):
            calls.append(referer)
            raise HttpError("HTTP 404")

        with mock.patch("core.httpclient.download_image", side_effect=fake_download):
            with self.assertRaises(HttpError):
                download_image_multi("https://cdn.donmai.us/a.jpg", "http://127.0.0.1:6789", timeout=1)

        self.assertEqual(len(calls), 1)

    def test_transient_classification(self) -> None:
        self.assertTrue(is_transient_error(HttpError("ReadTimeout")))
        self.assertTrue(is_transient_error(HttpError("HTTP 503")))
        self.assertFalse(is_transient_error(HttpError("图片地址不是公网 http(s)，已拒绝")))
        self.assertFalse(is_transient_error(HttpError("图片超过大小上限")))


if __name__ == "__main__":
    unittest.main()


class RefererTest(unittest.TestCase):
    """403 的图床要靠来源页放行：v2ph、fapello 裸下 403、带 Referer 200。"""

    def test_origin_from_image_url(self) -> None:
        self.assertEqual(
            httpclient.referer_of("https://fapello.com/content/n/o/x/1000/x_0223_300px.jpg"),
            "https://fapello.com/",
        )
        self.assertEqual(httpclient.referer_of("http://cdn.v2ph.com/photos/a.jpg"), "http://cdn.v2ph.com/")

    def test_invalid(self) -> None:
        self.assertEqual(httpclient.referer_of(""), "")
        self.assertEqual(httpclient.referer_of("not-a-url"), "")
