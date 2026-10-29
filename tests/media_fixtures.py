"""Small real pictures and a structurally valid video, made on the spot, so tests can prove that the file checks accept the good and refuse
the damaged. (Third-party creative is never copied into the repository.)"""
import hashlib
import io
import struct

from PIL import Image, ImageDraw


def _colours(seed):
    h = hashlib.sha256(str(seed).encode()).digest()
    return [(h[i], h[i + 1], h[i + 2]) for i in range(0, 12, 3)]


def picture(seed="a", size=(640, 960), fmt="JPEG"):
    """A real, non-blank picture that differs for each seed."""
    a, b, c, d = _colours(seed)
    im = Image.new("RGB", size, a)
    draw = ImageDraw.Draw(im)
    w, h = size
    draw.rectangle([0, 0, w // 2, h // 2], fill=b)
    draw.rectangle([w // 3, h // 3, w, h], fill=c)
    draw.ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=d)
    for n in range(0, w, 17):
        draw.line([n, 0, w - n, h], fill=(255 - a[0], 255 - a[1], 255 - a[2]))
    out = io.BytesIO()
    im.save(out, fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return out.getvalue()


def jpeg_for(seed="a", size=(640, 960)):
    return picture(seed, size, "JPEG")


def png_for(seed="a", size=(640, 960)):
    return picture(seed, size, "PNG")


def blank_jpeg(size=(640, 960)):
    out = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(out, "JPEG")
    return out.getvalue()


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def tiny_mp4(seconds=2, width=720, height=1280, padding=4000, moov_first=True):
    """A video whose structure is whole (header with a duration and a picture track, data) though it holds no real pictures."""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    mvhd = _box(b"mvhd", bytes(4) + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + bytes(80))
    tkhd = _box(b"tkhd", bytes(76) + struct.pack(">II", width << 16, height << 16) + bytes(8))
    mdia = _box(b"mdia", _box(b"hdlr", bytes(8) + b"vide" + bytes(12) + b"\0"))
    moov = _box(b"moov", mvhd + _box(b"trak", tkhd + mdia))
    mdat = _box(b"mdat", bytes(range(256)) * (padding // 256 + 1))
    return ftyp + moov + mdat if moov_first else ftyp + mdat + moov


def cut(data, share):
    return data[:max(1, int(len(data) * share))]
