"""Sample-free JPEG recovery.

Ransomware often destroys the first few kilobytes of a JPEG instead of the
whole file.  The header, quantisation tables and Huffman tables are lost with
that prefix, while most of the entropy-coded scan survives.

This module reconstructs a usable baseline JPEG without a donor photo:

* use the standard Annex K Huffman tables used by virtually every JPEG
  encoder (and all libjpeg/Pillow output);
* test the common component-sampling layouts;
* find an MCU boundary after the damaged prefix;
* infer the MCU row width from the vertical correlation of luma DC values;
* align the surviving stream to a row boundary and rebuild the JPEG.

The exact quantisation tables cannot be recovered from the scan.  A quality
estimate based on the compressed bit rate picks a standard table, and the
caller can override it.  Camera-optimised Huffman tables are not supported
without a sample; the caller should fall back to the sample-based engine in
that case.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .jpeg import (
    ZIGZAG,
    Component,
    Frame,
    HuffmanTable,
    JpegHeader,
    ScanComponent,
    build_jpeg,
)
from .scan import (
    EntropySegments,
    ScanSlice,
    ScanStats,
    build_entropy,
    decode_scan,
    split_entropy,
)

SEARCH_BYTES = 4096
PROBE_MCUS = 48
MIN_MCUS = 64
MIN_ROWS = 5
MIN_MCU_COLS = 8
MAX_MCU_COLS = 1024
MAX_RECOVERED_MCUS = 8_000_000
# Wrong sampling layouts can still decode the same entropy stream.  They
# almost always produce an extremely tall/narrow or wide/flat image, so reject
# those geometries before comparing confidence scores.
MIN_ASPECT = 0.10
MAX_ASPECT = 10.0
MIN_DIMENSION = 64


class SamplelessError(ValueError):
    """Sample-free reconstruction was not possible."""


# --------------------------------------------------------------------------
# Standard JPEG tables (ITU T.81, Annex K)
# --------------------------------------------------------------------------

_DC_LUMA_COUNTS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_DC_LUMA_SYMBOLS = bytes(range(12))
_DC_CHROMA_COUNTS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_DC_CHROMA_SYMBOLS = bytes(range(12))

_AC_LUMA_COUNTS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_AC_LUMA_SYMBOLS = bytes(
    (
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12,
        0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
        0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
        0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16,
        0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
        0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
        0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
        0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
        0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98,
        0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
        0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
        0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4,
        0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA,
        0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
        0xF9, 0xFA,
    )
)
_AC_CHROMA_COUNTS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_AC_CHROMA_SYMBOLS = bytes(
    (
        0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21,
        0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
        0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
        0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0,
        0x15, 0x62, 0x72, 0xD1, 0x0A, 0x16, 0x24, 0x34,
        0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
        0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38,
        0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
        0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
        0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
        0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
        0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
        0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96,
        0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5,
        0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
        0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
        0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2,
        0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
        0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9,
        0xEA, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
        0xF9, 0xFA,
    )
)

# Natural (row-major) Annex K quantisation tables.  JPEG headers store these
# in zig-zag order, which is applied by _quant_tables().
_QUANT_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)
_QUANT_CHROMA = (
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)


def _huffman_tables() -> dict[tuple[int, int], HuffmanTable]:
    specs = (
        (0, 0, _DC_LUMA_COUNTS, _DC_LUMA_SYMBOLS),
        (0, 1, _DC_CHROMA_COUNTS, _DC_CHROMA_SYMBOLS),
        (1, 0, _AC_LUMA_COUNTS, _AC_LUMA_SYMBOLS),
        (1, 1, _AC_CHROMA_COUNTS, _AC_CHROMA_SYMBOLS),
    )
    tables: dict[tuple[int, int], HuffmanTable] = {}
    for table_class, table_id, counts, symbols in specs:
        table = HuffmanTable(table_class, table_id, tuple(counts), symbols)
        table.build_lut()
        tables[(table_class, table_id)] = table
    return tables


_HUFFMAN_TABLES = _huffman_tables()


def _quant_tables(quality: int) -> dict[int, tuple[int, ...]]:
    quality = max(1, min(100, int(quality)))
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    result: dict[int, tuple[int, ...]] = {}
    for table_id, natural in ((0, _QUANT_LUMA), (1, _QUANT_CHROMA)):
        scaled = [
            max(1, min(65535, (value * scale + 50) // 100)) for value in natural
        ]
        result[table_id] = tuple(scaled[ZIGZAG[i]] for i in range(64))
    return result


def standard_quant_tables(quality: int) -> dict[int, tuple[int, ...]]:
    """Return standard IJG quantization tables for a quality level.

    The tables are in JPEG zig-zag order and use table ids 0 (luma) and
    1 (chroma), matching the tables used by the sample-free recovery path.
    The post-process page also uses these tables for its quality presets.
    """
    return _quant_tables(quality)


# --------------------------------------------------------------------------
# Sampling layouts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingLayout:
    name: str
    components: tuple[Component, ...]
    scan_components: tuple[ScanComponent, ...]
    preference: int

    @property
    def mcu_width(self) -> int:
        return 8 * max(c.h for c in self.components)

    @property
    def mcu_height(self) -> int:
        return 8 * max(c.v for c in self.components)


def _layout(
    name: str,
    y: tuple[int, int],
    chroma: tuple[int, int] | None,
    preference: int,
) -> SamplingLayout:
    if chroma is None:
        components = (Component(1, y[0], y[1], 0),)
        scan = (ScanComponent(1, 0, 0),)
    else:
        components = (
            Component(1, y[0], y[1], 0),
            Component(2, chroma[0], chroma[1], 1),
            Component(3, chroma[0], chroma[1], 1),
        )
        scan = (
            ScanComponent(1, 0, 0),
            ScanComponent(2, 1, 1),
            ScanComponent(3, 1, 1),
        )
    return SamplingLayout(name, components, scan, preference)


LAYOUTS = (
    _layout("4:2:0", (2, 2), (1, 1), 0),
    _layout("4:2:2", (2, 1), (1, 1), 1),
    _layout("4:4:4", (1, 1), (1, 1), 2),
    _layout("grayscale", (1, 1), None, 3),
    _layout("4:1:1", (4, 1), (1, 1), 4),
    _layout("4:4:0", (1, 2), (1, 1), 5),
)


def _header(
    layout: SamplingLayout,
    quality: int,
    width: int,
    height: int,
    restart_interval: int = 0,
) -> JpegHeader:
    frame = Frame(0xC0, 8, max(1, width), max(1, height), layout.components)
    return JpegHeader(
        quant_tables=_quant_tables(quality),
        huffman_tables=_HUFFMAN_TABLES,
        frame=frame,
        restart_interval=restart_interval,
        scan_components=layout.scan_components,
        scan_ss=0,
        scan_se=63,
        scan_ah=0,
        scan_al=0,
        scan_start=0,
        segments=(),
    )


def estimate_quality(data_size: int, prefix: int, width: int, height: int) -> int:
    """Guess a standard quality level from the surviving compressed bit rate.

    This cannot be exact: quantisation tables are destroyed with the header.
    The estimate only chooses the closest standard table, and the user can
    override it in the interface.

    The scale tops out at 100 (near-lossless) rather than 95, because 95
    still introduces visible blocking on detailed photographs and the whole
    point of the sample-free path is to give the best usable result when no
    donor photo is available.
    """
    area = max(1, width * height)
    bpp = max(0, data_size - prefix) * 8.0 / area
    if bpp >= 0.72:
        return 100
    if bpp >= 0.48:
        return 95
    if bpp >= 0.36:
        return 90
    if bpp >= 0.30:
        return 87
    if bpp >= 0.25:
        return 82
    if bpp >= 0.20:
        return 77
    if bpp >= 0.15:
        return 72
    if bpp >= 0.10:
        return 65
    return 55


# --------------------------------------------------------------------------
# Scan discovery and geometry inference
# --------------------------------------------------------------------------


def _plausible_geometry(layout: SamplingLayout, cols: int, rows: int) -> bool:
    """Reject geometries that cannot be a normal photograph.

    A wrong layout often decodes the same scan as a very narrow or very flat
    image with a surprisingly high DC correlation.  Those false positives are
    cheap to spot from the resulting dimensions.
    """
    if cols <= 0 or rows <= 0:
        return False
    width = cols * layout.mcu_width
    height = rows * layout.mcu_height
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        return False
    aspect = width / height
    return MIN_ASPECT <= aspect <= MAX_ASPECT


def _find_scan(
    prefix: int,
    entropy: EntropySegments,
    search_end: int,
) -> tuple[int, float, SamplingLayout, ScanSlice] | None:
    """Find a decodable scan and the most likely sampling layout.

    Layouts are tested together at each bit alignment.  This matters for
    performance: trying one layout over the whole search window and only then
    moving to the next would scan thousands of irrelevant bytes whenever the
    file is 4:2:2 or 4:4:4.
    """
    first_end = entropy.segments[0][1] if entropy.segments else 0
    if first_end <= 0:
        return None
    headers = {layout.name: _header(layout, 50, 1, 1) for layout in LAYOUTS}
    best: tuple[float, int, float, SamplingLayout, ScanSlice] | None = None
    full_decodes = 0
    for raw_index in range(prefix, search_end):
        clean_idx = entropy.raw_to_clean.get(raw_index)
        if clean_idx is None or clean_idx >= first_end:
            continue
        for bit in range(8):
            if clean_idx * 8 + bit >= first_end * 8:
                break
            at_offset: tuple[float, int, float, SamplingLayout, ScanSlice] | None = None
            for layout in LAYOUTS:
                header = headers[layout.name]
                probe = decode_scan(
                    header, entropy, clean_idx, bit, PROBE_MCUS, collect_dc=False
                )
                promising = probe.eoi or (
                    probe.mcus >= PROBE_MCUS
                    and probe.reason == "too many MCUs for the supplied header"
                )
                if not promising:
                    continue
                full_decodes += 1
                if full_decodes > 96:
                    if best is not None:
                        return best[1:]
                    return None
                stats = decode_scan(
                    header,
                    entropy,
                    clean_idx,
                    bit,
                    min(MAX_RECOVERED_MCUS, len(entropy.cleaned) + 1024),
                    collect_dc=True,
                )
                if not stats.eoi or stats.mcus < MIN_MCUS:
                    continue
                cols, corr, _ = infer_mcu_cols(stats.luma_dc)
                if cols == 0:
                    continue
                rows = (stats.mcus - stats.mcus % cols) // cols
                if not _plausible_geometry(layout, cols, rows):
                    continue
                score = corr - layout.preference * 0.001
                candidate = (
                    score,
                    cols,
                    corr,
                    layout,
                    ScanSlice(entropy, 0, clean_idx, bit, stats),
                )
                if best is None or score > best[0]:
                    best = candidate
                if at_offset is None or score > at_offset[0]:
                    at_offset = candidate
            # A strong common-layout match is conclusive; stop scanning the
            # encrypted prefix for false positives.
            if at_offset is not None and at_offset[2] >= 0.55:
                return at_offset[1:]
    if best is None:
        return None
    return best[1:]


def _lag_score(values: np.ndarray, lag: int) -> tuple[float, float]:
    a = values[lag:]
    b = values[:-lag]
    if a.size < 32:
        return -1.0, float("inf")
    a_std = float(a.std())
    b_std = float(b.std())
    if a_std == 0.0 or b_std == 0.0:
        return -1.0, float("inf")
    corr = float(np.corrcoef(a, b)[0, 1])
    mad = float(np.abs(a - b).mean())
    return corr, mad


def infer_mcu_cols(
    dc_values: list[int] | np.ndarray,
) -> tuple[int, float, list[tuple[int, float, float]]]:
    """Infer the number of MCUs per row from vertical DC correlation.

    For the true row width, MCUs separated by ``cols`` positions are vertically
    adjacent.  Natural photographs correlate strongly at that lag, while
    neighbouring lags (and integer multiples) are markedly weaker.
    """
    values = np.asarray(dc_values, dtype=np.float64)
    count = int(values.size)
    best_cols = 0
    best_corr = -1.0
    scored: list[tuple[int, float, float]] = []
    if count < 128:
        return 0, best_corr, scored
    max_cols = min(MAX_MCU_COLS, count // MIN_ROWS)
    if max_cols < MIN_MCU_COLS:
        return 0, best_corr, scored
    for cols in range(MIN_MCU_COLS, max_cols + 1):
        corr, mad = _lag_score(values, cols)
        if corr <= -1.0:
            continue
        scored.append((cols, corr, mad))
        if corr > best_corr:
            best_corr = corr
            best_cols = cols
    return best_cols, best_corr, scored


def _aligned_slice(
    header: JpegHeader, entropy: EntropySegments, scan: ScanSlice, cols: int
) -> ScanSlice | None:
    """Move the scan to an MCU row boundary.

    The original image contains a whole number of MCU rows.  After an arbitrary
    number of surviving MCUs, ``N % cols`` is therefore the number of MCUs left
    in the current partial row.  Dropping those leaves a row-aligned stream.
    """
    stats = scan.stats
    skip = stats.mcus % cols
    if skip == 0:
        return scan
    if skip >= len(stats.boundaries):
        return None
    absolute = stats.boundaries[skip]
    new_idx = absolute // 8
    new_bit = absolute % 8
    new_stats = decode_scan(
        header,
        entropy,
        new_idx,
        new_bit,
        min(MAX_RECOVERED_MCUS, len(entropy.cleaned) + 1024),
        collect_dc=True,
    )
    if (
        not new_stats.eoi
        or new_stats.mcus < cols * MIN_ROWS
        or new_stats.mcus % cols
    ):
        return None
    first_segment = 0
    for index, (start, end) in enumerate(entropy.segments):
        if start * 8 <= absolute < end * 8 or (
            index == len(entropy.segments) - 1 and absolute >= start * 8
        ):
            first_segment = index
            break
    return ScanSlice(entropy, first_segment, new_idx, new_bit, new_stats)


def _restart_interval(stats: ScanStats) -> int:
    counts = [c for c in stats.interval_counts if c > 0]
    if len(counts) <= 1:
        return 0
    if all(c == counts[0] for c in counts[:-1]) and counts[-1] <= counts[0]:
        return counts[0]
    return 0


@dataclass
class SamplelessResult:
    data: bytes
    width: int
    height: int
    mcu_cols: int
    mcu_rows: int
    prefix: int
    quality: int
    sampling: str
    confidence: float
    skipped_rows: int


def recover_without_sample(
    data: bytes,
    prefix: int,
    quality: int | None = None,
    search_bytes: int = SEARCH_BYTES,
) -> SamplelessResult:
    """Recover one JPEG scan without a donor header.

    Raises :class:`SamplelessError` with a user-facing message when the stream
    cannot be reconstructed confidently.
    """
    if prefix < 0 or prefix >= len(data):
        raise SamplelessError("encrypted part size is outside the file")

    search_end = min(len(data), prefix + max(1024, search_bytes))
    entropy = split_entropy(data, prefix, map_until=search_end)
    found = _find_scan(prefix, entropy, search_end)
    if found is None:
        raise SamplelessError(
            "could not find a decodable scan after the encrypted prefix. "
            "The file may use custom Huffman tables; add a readable photo "
            "from the same camera."
        )
    cols, corr, layout, scan = found
    if corr < 0.30:
        raise SamplelessError(
            "the surviving scan does not show a reliable row width; add a "
            "readable photo from the same camera"
        )

    header = _header(layout, 50, 1, 1)
    aligned = _aligned_slice(header, scan.entropy, scan, cols)
    if aligned is None:
        raise SamplelessError(
            "the surviving scan could not be aligned to a whole MCU row; "
            "add a readable photo from the same camera"
        )
    rows = aligned.stats.mcus // cols
    if rows < MIN_ROWS:
        raise SamplelessError("too few MCU rows survived for a usable image")

    expected_width = cols * layout.mcu_width
    expected_height = rows * layout.mcu_height
    chosen_quality = (
        max(1, min(100, int(quality)))
        if quality is not None
        else estimate_quality(len(data), prefix, expected_width, expected_height)
    )

    restart = _restart_interval(aligned.stats)
    if aligned.stats.interval_counts and len(aligned.stats.interval_counts) > 1:
        if restart == 0 or aligned.stats.interval_counts[0] != restart:
            raise SamplelessError(
                "this file uses restart markers that do not line up with a "
                "whole MCU row; add a readable photo from the same camera"
            )

    header = _header(
        layout, chosen_quality, expected_width, expected_height, restart
    )
    try:
        entropy = build_entropy(aligned)
        output = build_jpeg(header, entropy, expected_height)
        with Image.open(io.BytesIO(output)) as image:
            image.load()
            if image.width != expected_width or image.height != expected_height:
                raise SamplelessError(
                    "rebuilt JPEG has unexpected dimensions"
                )
    except SamplelessError:
        raise
    except Exception as exc:  # noqa: BLE001 - converted to a user message
        raise SamplelessError(f"could not rebuild the JPEG: {exc}") from exc

    # The exact number of destroyed rows is unknowable, but the compressed
    # bit rate gives a useful estimate for the results table.
    tail = max(1, len(data) - prefix)
    avg_bytes_per_mcu = tail / max(1, scan.stats.mcus)
    lost_mcus = prefix / max(0.25, avg_bytes_per_mcu)
    skipped_rows = max(0, int(round(lost_mcus / cols)))

    return SamplelessResult(
        data=output,
        width=expected_width,
        height=expected_height,
        mcu_cols=cols,
        mcu_rows=rows,
        prefix=prefix,
        quality=chosen_quality,
        sampling=layout.name,
        confidence=corr,
        skipped_rows=skipped_rows,
    )
