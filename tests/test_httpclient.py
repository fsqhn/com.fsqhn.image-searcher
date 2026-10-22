"""远程图片地址过滤：群成员构造的图片段不能把请求导向本机或内网。"""

from __future__ import annotations

import io
import unittest
from unittest import mock

from PIL import Image

from core.hashing import dhash, prepare_upload
from core.httpclient import is_public_url


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


if __name__ == "__main__":
    unittest.main()
