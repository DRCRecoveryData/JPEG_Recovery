"""Sample discovery: plain JPEGs and JPEG previews embedded in RAW files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .jpeg import JpegError, JpegHeader, parse_jpeg

RAW_EXTENSIONS = {
    "3fr", "arw", "cr2", "cr3", "crw", "dng", "nef", "nrw", "orf", "pef",
    "ptx", "raf", "raw", "rw2", "rwl", "sr2", "srf", "x3f",
}
JPEG_EXTENSIONS = {"jpg", "jpeg", "jpe", "jfif"}


@dataclass
class SampleInfo:
    path: str
    header: JpegHeader
    jpeg_bytes: bytes
    from_raw: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def width(self) -> int:
        return self.header.width

    @property
    def height(self) -> int:
        return self.header.height

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class EmbeddedJpeg:
    offset: int
    data: bytes
    header: JpegHeader

    @property
    def area(self) -> int:
        return self.header.width * self.header.height


def iter_embedded_jpegs(data: bytes, max_candidates: int = 256):
    """Yield JPEG streams found by SOI/EOI signatures inside a container."""
    pos = 0
    found = 0
    size = len(data)
    while pos < size and found < max_candidates:
        start = data.find(b"\xff\xd8", pos)
        if start < 0:
            return
        end = data.find(b"\xff\xd9", start + 2)
        if end < 0:
            return
        chunk = data[start:end + 2]
        try:
            header = parse_jpeg(chunk)
        except JpegError:
            pos = start + 2
            continue
        if header.frame.width >= 16 and header.frame.height >= 16:
            yield EmbeddedJpeg(start, chunk, header)
            found += 1
        pos = end + 2


def load_sample(path: str) -> SampleInfo:
    data = Path(path).read_bytes()
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in JPEG_EXTENSIONS or data[:2] == b"\xff\xd8":
        try:
            return SampleInfo(path, parse_jpeg(data), data, False)
        except JpegError:
            if ext not in RAW_EXTENSIONS:
                raise

    candidates = list(iter_embedded_jpegs(data))
    if not candidates:
        raise JpegError(
            f"no usable baseline JPEG preview found in {os.path.basename(path)}"
        )
    # Prefer a large, camera-sized preview.  Tiny EXIF thumbnails are only
    # useful when nothing else survives.
    best = max(candidates, key=lambda c: (c.area, len(c.data)))
    return SampleInfo(path, best.header, best.data, True)


def sample_base_extensions(samples: list[SampleInfo]) -> set[str]:
    if not samples:
        return set(JPEG_EXTENSIONS)
    exts: set[str] = set()
    for sample in samples:
        ext = Path(sample.path).suffix.lower().lstrip(".")
        if sample.from_raw or ext in RAW_EXTENSIONS:
            exts.add(ext)
    return exts or set(JPEG_EXTENSIONS)


def dedupe_samples(samples: list[SampleInfo]) -> list[SampleInfo]:
    seen: set[tuple] = set()
    result = []
    for sample in samples:
        key = (
            sample.header.frame.width,
            sample.header.frame.height,
            tuple(sorted(sample.header.quant_tables.items())),
            tuple((c.cid, c.h, c.v, c.tq) for c in sample.header.frame.components),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(sample)
    return result
