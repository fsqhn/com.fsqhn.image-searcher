"""图片指纹（dHash）与上传前压缩。

走指纹而不是视觉模型，是因为这里要回答的问题只有「搜出来的这张图是不是
用户发的同一张图」，缩放、二次压缩、换格式都不应改变结论。
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

_HASH_SIDE = 8
_TARGET_UPLOAD_BYTES = 480 * 1024
_MIN_SIDE = 320
# 12MB 的 JPEG 可以解出上亿像素，解码前先按尺寸拦一次，别把内存顶爆。
# 不要改成 Image.MAX_IMAGE_PIXELS：那是 PIL 的模块级全局，会连累同一 runner 进程里
# 的其他插件；下面每个入口都会在解码像素前用 _too_large() 自己拦一遍。
MAX_PIXELS = 40_000_000


def _too_large(image: Image.Image) -> bool:
    return image.width * image.height > MAX_PIXELS


def dhash(raw: bytes, side: int = _HASH_SIDE) -> int | None:
    """计算 64 位 dHash；无法解码时返回 None。"""
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if _too_large(image):
                return None
            image = ImageOps.exif_transpose(image)
            gray = image.convert("L").resize((side + 1, side), Image.Resampling.LANCZOS)
            pixels = gray.tobytes()
    except Exception:
        return None

    bits = 0
    for row in range(side):
        offset = row * (side + 1)
        for col in range(side):
            bits = (bits << 1) | (1 if pixels[offset + col] > pixels[offset + col + 1] else 0)
    return bits


def hamming(left: int, right: int) -> int:
    """两个指纹的汉明距离，0 表示完全一致。"""
    return (left ^ right).bit_count()


def prepare_upload(raw: bytes, limit: int = _TARGET_UPLOAD_BYTES) -> tuple[bytes, str]:
    """把图片压到上传接口能接受的大小，返回 (字节, mime)。

    SerpAPI 的 Image API 上限 500KB；Yandex 与百度虽然更宽松，但统一压一次
    可以减少传输时间，也避免长图把整次搜索拖慢。
    """
    if not raw:
        return raw, "image/jpeg"
    # 小体积不等于小像素，纯色图能压到几百 KB 却有上亿像素，先读头部拦一次。
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if _too_large(probe):
                raise ValueError("图片像素过大")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - 解不开的图交给下面的分支按原逻辑处理
        pass
    if len(raw) <= limit and _sniff_mime(raw) in {"image/jpeg", "image/png", "image/webp"}:
        return raw, _sniff_mime(raw)

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if _too_large(image):
                raise ValueError("图片像素过大")
            image = ImageOps.exif_transpose(image)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            elif image.mode == "L":
                image = image.convert("RGB")
            width, height = image.size
            scale = 1.0
            if max(width, height) > 2048:
                scale = 2048 / max(width, height)
            for _ in range(8):
                target = image
                if scale < 1.0:
                    target = image.resize(
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                buffer = io.BytesIO()
                target.save(buffer, format="JPEG", quality=88, optimize=True)
                payload = buffer.getvalue()
                if len(payload) <= limit:
                    return payload, "image/jpeg"
                quality = 82
                while quality >= 55:
                    buffer = io.BytesIO()
                    target.save(buffer, format="JPEG", quality=quality, optimize=True)
                    payload = buffer.getvalue()
                    if len(payload) <= limit:
                        return payload, "image/jpeg"
                    quality -= 8
                scale *= 0.8
                if max(width * scale, height * scale) < _MIN_SIDE:
                    break
    except Exception:
        pass

    return raw, _sniff_mime(raw)


def _sniff_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"
