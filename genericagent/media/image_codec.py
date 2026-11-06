"""有界图片编码：路径真源 → Claude-style image block（对齐 Codex/CC 限制）。"""

from __future__ import annotations

import base64
import hashlib
import math
import mimetypes
import os
import struct
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal, Optional

# 长边预算：默认 1568（对齐 Claude Code 输入链路 m0396:205-206），
# 可用 GA_IMAGE_MAX_DIMENSION 覆盖；夹紧到 512..2048（2048 留给明确要求更大的 provider）。
MAX_DIMENSION = max(512, min(2048, int(os.environ.get("GA_IMAGE_MAX_DIMENSION", "1568"))))
# 面积上限：比只卡长边更贴近 token 预算（4000x500 长条图长边没超但面积很大）。
MAX_IMAGE_AREA = MAX_DIMENSION * MAX_DIMENSION
# 部分 provider（Grok 等）拒绝宽或高 < 8 的图；发送前统一卡死，避免 400。
MIN_DIMENSION = 8
MAX_BASE64_CHARS = 5_000_000
MAX_RAW_FILE_BYTES = 40 * 1024 * 1024
CACHE_SIZE = 32

ImageSource = Literal["clipboard", "path", "upload", "cli", "fallback_text"]

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class ImageCodecError(Exception):
    pass


@dataclass(frozen=True)
class EncodedImage:
    data_b64: str
    media_type: str
    width: int
    height: int
    source_path: str
    sha1: str
    resized: bool
    orig_width: int = 0
    orig_height: int = 0


_encode_cache: OrderedDict[tuple[str, int, int], EncodedImage] = OrderedDict()


def clear_encode_cache() -> None:
    _encode_cache.clear()


def _cache_get(key: tuple[str, int, int]) -> Optional[EncodedImage]:
    if key not in _encode_cache:
        return None
    _encode_cache.move_to_end(key)
    return _encode_cache[key]


def _cache_put(key: tuple[str, int, int], value: EncodedImage) -> EncodedImage:
    _encode_cache[key] = value
    _encode_cache.move_to_end(key)
    while len(_encode_cache) > CACHE_SIZE:
        _encode_cache.popitem(last=False)
    return value


def _sniff_media_type(data: bytes, path: str) -> str:
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 2 and data[:2] == b"BM":
        return "image/bmp"
    guessed = mimetypes.guess_type(path)[0]
    if guessed and guessed.startswith("image/"):
        return guessed
    return "application/octet-stream"


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return 0, 0


def _jpeg_size(data: bytes) -> tuple[int, int]:
    if not (len(data) >= 3 and data[:3] == b"\xff\xd8\xff"):
        return 0, 0
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9):
            i += 2
            continue
        if i + 4 > len(data):
            break
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2) and seg_len >= 7 and i + 9 < len(data):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(w), int(h)
        i += 2 + seg_len
    return 0, 0


def _try_pillow_resize(data: bytes, media_type: str) -> Optional[tuple[bytes, str, int, int, bool]]:
    try:
        from io import BytesIO
        from PIL import Image  # type: ignore
    except Exception:
        return None

    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception as e:
        raise ImageCodecError(f"decode failed: {e}") from e

    ow, oh = img.size
    w, h = ow, oh
    resized = False
    if w > MAX_DIMENSION or h > MAX_DIMENSION or (w * h) > MAX_IMAGE_AREA:
        scale = min(1.0, MAX_DIMENSION / float(max(w, h)), math.sqrt(MAX_IMAGE_AREA / float(w * h)))
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        img = img.resize((nw, nh))
        w, h = nw, nh
        resized = True

    out_type = media_type
    if media_type == "image/bmp":
        out_type = "image/png"
        resized = True

    buf = BytesIO()
    save_kw: dict[str, Any] = {}
    fmt = "PNG"
    if out_type in ("image/jpeg", "image/jpg"):
        fmt = "JPEG"
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        save_kw["quality"] = 85
        out_type = "image/jpeg"
    elif out_type == "image/webp":
        fmt = "WEBP"
        save_kw["quality"] = 85
    else:
        fmt = "PNG"
        out_type = "image/png"
        if img.mode not in ("RGB", "RGBA", "L", "P"):
            img = img.convert("RGBA")

    # 若仍超 base64 上限，逐步降质/缩小
    for _ in range(6):
        buf.seek(0)
        buf.truncate(0)
        img.save(buf, format=fmt, **save_kw)
        raw = buf.getvalue()
        b64_len = (len(raw) + 2) // 3 * 4
        if b64_len <= MAX_BASE64_CHARS:
            return raw, out_type, w, h, resized, ow, oh
        # shrink further
        nw = max(1, int(w * 0.7))
        nh = max(1, int(h * 0.7))
        img = img.resize((nw, nh))
        w, h = nw, nh
        resized = True
        if fmt == "JPEG":
            save_kw["quality"] = max(40, int(save_kw.get("quality", 85) * 0.8))

    raise ImageCodecError(
        f"image still exceeds base64 limit ({MAX_BASE64_CHARS} chars) after resize"
    )


def encode_image_for_prompt(path: str) -> EncodedImage:
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        raise ImageCodecError(f"not a file: {path}")

    size = os.path.getsize(path)
    if size > MAX_RAW_FILE_BYTES:
        raise ImageCodecError(
            f"file too large: {size} bytes > {MAX_RAW_FILE_BYTES}"
        )

    with open(path, "rb") as f:
        data = f.read()

    sha1 = hashlib.sha1(data).hexdigest()
    cache_key = (sha1, MAX_DIMENSION, MAX_BASE64_CHARS)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    media_type = _sniff_media_type(data, path)
    width, height = 0, 0
    resized = False

    if media_type == "image/png":
        width, height = _png_size(data)
    elif media_type == "image/jpeg":
        width, height = _jpeg_size(data)

    # 原始尺寸：缩放前从文件头读出（0,0 表示未知），供尺寸/坐标说明使用。
    orig_width, orig_height = width, height
    needs_resize = (
        media_type == "image/bmp"
        or (width and height and (width > MAX_DIMENSION or height > MAX_DIMENSION))
        or (width and height and (width * height) > MAX_IMAGE_AREA)
        or (len(base64.b64encode(data)) > MAX_BASE64_CHARS)
    )

    if needs_resize or media_type == "application/octet-stream":
        pillow_out = _try_pillow_resize(data, media_type if media_type.startswith("image/") else "image/png")
        if pillow_out is not None:
            data, media_type, width, height, resized, ow, oh = pillow_out
            if ow > 0 and oh > 0:
                orig_width, orig_height = ow, oh
        elif media_type == "application/octet-stream" or not media_type.startswith("image/"):
            raise ImageCodecError(f"unsupported or undecodable image: {path}")
        elif len(base64.b64encode(data)) > MAX_BASE64_CHARS:
            raise ImageCodecError(
                f"image base64 exceeds {MAX_BASE64_CHARS} chars; install Pillow to auto-resize"
            )
        elif media_type == "image/bmp":
            raise ImageCodecError("BMP requires Pillow to convert to PNG")

    data_b64 = base64.b64encode(data).decode("ascii")
    if len(data_b64) > MAX_BASE64_CHARS:
        raise ImageCodecError(
            f"image base64 length {len(data_b64)} exceeds limit {MAX_BASE64_CHARS}"
        )

    if width <= 0 or height <= 0:
        # 未知尺寸时再试 pillow 读一次；仍未知则按 0 处理并在下方拒绝
        pillow_out = _try_pillow_resize(data, media_type if media_type.startswith("image/") else "image/png")
        if pillow_out is not None:
            data, media_type, width, height, resized, ow, oh = pillow_out
            if ow > 0 and oh > 0:
                orig_width, orig_height = ow, oh
            data_b64 = base64.b64encode(data).decode("ascii")

    if width > 0 and height > 0 and (width < MIN_DIMENSION or height < MIN_DIMENSION):
        raise ImageCodecError(
            f"image too small: {width}x{height} (min {MIN_DIMENSION}px each side)"
        )

    if width <= 0 or height <= 0:
        width, height = max(width, 1), max(height, 1)

    enc = EncodedImage(
        data_b64=data_b64,
        media_type=media_type if media_type.startswith("image/") else "image/png",
        width=width,
        height=height,
        source_path=path,
        sha1=sha1,
        resized=resized,
        orig_width=orig_width or width,
        orig_height=orig_height or height,
    )
    return _cache_put(cache_key, enc)


def build_image_block(encoded: EncodedImage) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": encoded.media_type,
            "data": encoded.data_b64,
        },
    }


def normalize_image_attachments(images: Any) -> list[dict[str, Any]]:
    """list[str|dict|Encoded-ish] → list[{path, placeholder, source}] 绝对路径。"""
    out: list[dict[str, Any]] = []
    if not images:
        return out
    for item in images:
        if item is None:
            continue
        if isinstance(item, str):
            path = os.path.abspath(os.path.expanduser(item))
            out.append({"path": path, "placeholder": "", "source": "path"})
            continue
        if isinstance(item, dict):
            p = item.get("path") or item.get("file") or ""
            if not p:
                continue
            path = os.path.abspath(os.path.expanduser(str(p)))
            out.append({
                "path": path,
                "placeholder": str(item.get("placeholder") or ""),
                "source": str(item.get("source") or "path"),
            })
            continue
        path_attr = getattr(item, "path", None)
        if path_attr:
            out.append({
                "path": os.path.abspath(os.path.expanduser(str(path_attr))),
                "placeholder": str(getattr(item, "placeholder", "") or ""),
                "source": str(getattr(item, "source", "path") or "path"),
            })
    return out


def _notes_enabled() -> bool:
    return str(os.environ.get("GA_IMAGE_NOTES", "") or "").strip().lower() not in {"0", "false", "off", "no"}


def display_note(enc: EncodedImage) -> str:
    """Size/coordinate note (aligned with CC Lee). Only when resized or has source path."""
    if not enc.resized and not enc.source_path:
        return ""
    parts: list[str] = []
    if enc.source_path:
        parts.append(f"source: {enc.source_path}")
    ow = enc.orig_width or enc.width
    oh = enc.orig_height or enc.height
    parts.append(f"original {ow}x{oh}")
    if enc.resized and enc.width > 0 and ow > 0:
        scale = ow / float(enc.width)
        parts.append(f"displayed at {enc.width}x{enc.height}, multiply coordinates by {scale:.2f}")
    return "[image] " + ", ".join(parts)


def build_user_content_with_images(
    text: str,
    images: Any = None,
    *,
    extra_paths: Optional[list[str]] = None,
    failures: Optional[list[dict[str, str]]] = None,
) -> Optional[list[dict[str, Any]]]:
    """拼 Claude-style multimodal user content；无有效图且无失败返回 None。"""
    atts = normalize_image_attachments(images)
    seen: set[str] = set()
    paths: list[str] = []
    missing: list[str] = []
    for a in atts:
        p = a["path"]
        if p in seen:
            continue
        seen.add(p)
        if os.path.isfile(p):
            paths.append(p)
        else:
            # 显式附件缺失也要让用户看见（自动扫描的 extra_paths 已在 _extract 里按 isfile 过滤）
            missing.append(p)
            if failures is not None:
                failures.append({"path": p, "error": "file not found"})
    for p in extra_paths or []:
        ap = os.path.abspath(os.path.expanduser(str(p)))
        if ap not in seen and os.path.isfile(ap):
            seen.add(ap)
            paths.append(ap)

    if not paths and not missing:
        return None

    content: list[dict[str, Any]] = [{"type": "text", "text": text or ""}]
    for p in missing:
        content[0]["text"] += f"\n[图片附件读取失败: {p}: file not found]"

    any_ok = False
    notes: list[str] = []
    notes_on = _notes_enabled()
    for path in paths:
        try:
            enc = encode_image_for_prompt(path)
            content.append(build_image_block(enc))
            any_ok = True
            if notes_on:
                _note = display_note(enc)
                if _note:
                    notes.append(_note)
        except Exception as e:
            content[0]["text"] += f"\n[图片附件读取失败: {path}: {e}]"
            if failures is not None:
                failures.append({"path": path, "error": str(e)})
    if notes:
        content[0]["text"] = (content[0]["text"] or "").rstrip() + chr(10) + chr(10) + chr(10).join(notes)
    return content


