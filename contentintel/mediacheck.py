"""Is this file really a complete, usable picture or video?

A saved file is only useful to a creative team if it opens, is whole, and shows the content. This module looks at the bytes themselves, not at
what a server claimed: the real type is read from the file's own first bytes, a picture must decode fully, a video's structure must be whole
(its boxes add up to its length, it has a header with a duration and a video track), and a file that is blank, tiny or cut short is refused
with a plain reason. It needs no tool beyond Pillow for pictures (without Pillow a picture is checked by its header and end marker only, and
says so); ffmpeg is used only to take a still from a video, and only if it is there.
"""
import io
import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field

try:                                                  # Pillow checks that a picture really decodes
    from PIL import Image, ImageStat
except ImportError:                                   # pragma: no cover - exercised by a test that hides it
    Image = ImageStat = None

MIN_SIDE = 200                  # a picture whose shorter side is under this cannot be judged by eye
LOW_RES_SIDE = 600              # under this it opens but is poor for review (shown in the preview label)
MIN_BYTES = 1024
BLANK_STDDEV = 2.0              # a picture whose brightness hardly varies is blank or one colour
MIN_SECONDS = 1.0


@dataclass
class MediaCheck:
    kind: str = ""                  # image | video | ""
    mime: str = ""
    ext: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    bytes: int = 0
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems

    @property
    def low_resolution(self):
        return self.kind == "image" and 0 < min(self.width, self.height) < LOW_RES_SIDE

    def describe(self):
        if self.kind == "video":
            return f"video {self.duration:.0f}s, {self.width}x{self.height}"
        if self.kind == "image":
            return f"image {self.width}x{self.height}"
        return "unknown file"


def sniff(data):
    """(kind, mime, extension) from the file's own first bytes; ('', '', '') if it is not a picture or video we know."""
    head = bytes(data[:16])
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp", "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif", "gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return ("video", "video/quicktime", "mov") if brand == b"qt  " else ("video", "video/mp4", "mp4")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm", "webm"
    return "", "", ""


# ---- pictures -------------------------------------------------------------------------------------------------------------------------------

def _jpeg_complete(data):
    return bytes(data[-2:]) == b"\xff\xd9" or b"\xff\xd9" in bytes(data[-64:])


def _check_image(data, result):
    if Image is None:
        result.notes.append("the picture was checked by its header only (Pillow is not installed)")
        if result.ext == "jpg" and not _jpeg_complete(data):
            result.problems.append("the picture is cut short (it has no end marker)")
        if result.ext == "png" and not bytes(data[-12:]).endswith(b"IEND\xaeB`\x82"):
            result.problems.append("the picture is cut short (it has no end marker)")
        return
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        im = Image.open(io.BytesIO(data))
        im.load()                                       # a truncated picture fails here
    except Exception as exc:
        text = str(exc).lower()
        why = "is cut short" if "truncat" in text or "premature" in text or "incomplete" in text else "does not open"
        result.problems.append(f"the picture {why} ({exc.__class__.__name__})")
        return
    result.width, result.height = im.size
    if min(im.size) < MIN_SIDE:
        result.problems.append(f"the picture is too small to judge ({im.size[0]}x{im.size[1]})")
        return
    grey = im.convert("L")
    if ImageStat.Stat(grey).stddev[0] < BLANK_STDDEV:
        result.problems.append("the picture is blank or a single colour")


# ---- videos (MP4 and QuickTime structure) ---------------------------------------------------------------------------------------------------

def _boxes(data, start, end):
    """(type, payload_start, box_end) for each box between start and end; stops at a damaged box."""
    pos, out = start, []
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                break
            size, header = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            out.append((kind.decode("latin1"), pos + header, -1))             # a box that claims more bytes than there are
            break
        out.append((kind.decode("latin1"), pos + header, pos + size))
        pos += size
    return out


def _check_video(data, result):
    size = len(data)
    top = _boxes(data, 0, size)
    kinds = [k for k, _, _ in top]
    if "moov" not in kinds:
        result.problems.append("the video is cut short (it has no header: the download stopped before the end)")
        return
    if any(e == -1 for _, _, e in top) or (top and top[-1][2] != size):
        result.problems.append("the video is cut short (its parts do not add up to its length)")
        return
    if "mdat" not in kinds:
        result.problems.append("the video has no picture data")
        return
    moov = next((p, e) for k, p, e in top if k == "moov")
    tracks = 0
    for kind, p, e in _boxes(data, moov[0], moov[1]):
        if kind == "mvhd" and e > p + 20:
            version = data[p]
            if version == 1:
                scale, dur = struct.unpack(">IQ", data[p + 20:p + 32])
            else:
                scale, dur = struct.unpack(">II", data[p + 12:p + 20])
            result.duration = dur / scale if scale else 0.0
        if kind == "trak":
            for k2, p2, e2 in _boxes(data, p, e):
                if k2 == "tkhd" and not result.width:
                    version = data[p2]
                    at = p2 + (88 if version == 1 else 76)
                    if e2 >= at + 8:
                        w, h = struct.unpack(">II", data[at:at + 8])
                        if (w >> 16) and (h >> 16):
                            result.width, result.height = w >> 16, h >> 16
                if k2 == "mdia":
                    for k3, p3, e3 in _boxes(data, p2, e2):
                        if k3 == "hdlr" and data[p3 + 8:p3 + 12] == b"vide":
                            tracks += 1
    if not tracks:
        result.problems.append("the video has no picture track")
    if result.duration < MIN_SECONDS:
        result.problems.append(f"the video is shorter than a second ({result.duration:.1f}s)")


# ---- the whole check --------------------------------------------------------------------------------------------------------------------------------

def check_media(data, declared_length=None):
    """Judge a downloaded file from its bytes. `declared_length` is what the server said it would send, if it said."""
    kind, mime, ext = sniff(data)
    result = MediaCheck(kind=kind, mime=mime, ext=ext, bytes=len(data))
    if declared_length is not None and declared_length != len(data):
        result.problems.append(f"the file is cut short ({len(data)} of {declared_length} bytes arrived)")
    if not data:
        result.problems.append("the file is empty")
        return result
    if not kind:
        result.problems.append("the file is not a picture or video we can read")
        return result
    if len(data) < MIN_BYTES:
        result.problems.append(f"the file is too small to be real ({len(data)} bytes)")
        return result
    if kind == "image":
        _check_image(data, result)
    elif result.ext in ("mp4", "mov"):
        _check_video(data, result)
    else:
        result.notes.append("a WebM video is not checked in detail")
    return result


def extension_matches(name, check):
    """Does a file name end the way its real type says? (jpeg and jpg are the same)"""
    tail = os.path.splitext(str(name))[1].lower().lstrip(".")
    return tail == check.ext or (tail == "jpeg" and check.ext == "jpg")


def corrected_name(name, check):
    stem = os.path.splitext(str(name))[0]
    return f"{stem}.{check.ext}"


def extract_frame(data, ffmpeg="ffmpeg", runner=subprocess.run, at_seconds=1.0):
    """One still picture (JPEG bytes) from a video, or None if ffmpeg is not there or cannot read it."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, "in.mp4"), os.path.join(tmp, "frame.jpg")
        with open(src, "wb") as handle:
            handle.write(data)
        try:
            runner([ffmpeg, "-y", "-loglevel", "error", "-ss", str(at_seconds), "-i", src, "-frames:v", "1", "-vf", "scale='min(900,iw)':-2", out],
                   capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if not os.path.exists(out):                      # a clip shorter than the offset: take the first frame
            try:
                runner([ffmpeg, "-y", "-loglevel", "error", "-i", src, "-frames:v", "1", "-vf", "scale='min(900,iw)':-2", out],
                       capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                return None
        if not os.path.exists(out):
            return None
        with open(out, "rb") as handle:
            return handle.read()
