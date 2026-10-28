"""Lossless colour correction for recovered JPEGs.

When the start of a JPEG scan is destroyed, the first DC coefficient of every
component is lost as well.  JPEG stores DC values differentially, so that lost
predictor shifts every following DC value in the scan by a constant amount.  A
constant DC shift is additive in pixel space: it changes brightness and colour
balance but leaves the AC detail untouched.

This module corrects that damage without re-encoding the photograph.  It only
replaces the first DC difference of each component in each restart interval;
all other entropy-coded bits are copied verbatim.  Quantization tables,
Huffman tables and image detail are not changed.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Mapping

import numpy as np
from PIL import Image

from .jpeg import JpegHeader, parse_jpeg, replace_scan
from .scan import (
    BitReader,
    ScanError,
    _decode_symbol,
    _stuff,
    decode_scan,
    split_entropy,
)

# A recovered photograph with a lost DC predictor is strongly clipped.  These
# display-space targets match the unclipped, natural-looking results validated
# on real Canon files.  A target of 110 displayed luma corresponds to roughly
# 104 internal (DC) luma once black clipping is taken into account.
TARGET_LUMA = 110.0
TARGET_CHROMA = 128.0
LUMA_TOLERANCE = 0.8
CHROMA_TOLERANCE = 0.8
MAX_PASSES = 6


class ColorFixError(ValueError):
    """The colour of this JPEG cannot be corrected losslessly."""


@dataclass(frozen=True)
class ColorMeans:
    """Average displayed Y, Cb and Cr values."""

    luma: float
    cb: float
    cr: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.luma, self.cb, self.cr)


@dataclass(frozen=True)
class ComponentInfo:
    cid: int
    role: str
    qdc: int


def _components(header: JpegHeader) -> list[ComponentInfo]:
    """Return component id, colour role and DC quantization value.

    Baseline JPEG components are ordered Y, Cb, Cr.  Component ids are often
    1, 2, 3 but this is not guaranteed, so the scale factors and table mapping
    are taken from the frame header rather than assumed.
    """
    result: list[ComponentInfo] = []
    count = len(header.frame.components)
    for index, component in enumerate(header.frame.components):
        if count == 1 or index == 0:
            role = "Y"
        elif index == 1:
            role = "Cb"
        elif index == 2:
            role = "Cr"
        else:
            role = "A"
        table = header.quant_tables.get(component.tq)
        if not table:
            raise ColorFixError(f"missing quantization table {component.tq}")
        result.append(ComponentInfo(component.cid, role, int(table[0])))
    return result


def measure_colors(data: bytes) -> ColorMeans:
    """Measure the displayed Y/Cb/Cr means of a JPEG.

    The full image is decoded at its native size.  Pillow's JPEG decoder is
    fast, and measuring the complete image avoids the chroma bias introduced by
    draft/downscaled decoding.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            ycc = image.convert("YCbCr")
            array = np.asarray(ycc, dtype=np.uint8)
    except Exception as exc:  # noqa: BLE001 - converted to a domain error
        raise ColorFixError(f"could not decode image for colour measurement: {exc}") from exc
    if array.ndim != 3 or array.shape[2] != 3:
        raise ColorFixError("unexpected image mode while measuring colours")
    means = array.reshape(-1, 3).mean(axis=0, dtype=np.float64)
    return ColorMeans(float(means[0]), float(means[1]), float(means[2]))


def luma_dc_mean(data: bytes) -> float:
    """Return the internal luma mean reconstructed from DC coefficients.

    This is the mean before the decoder clips negative pixel values to black.
    It is useful for diagnostics and tests, but a complete entropy decode is
    relatively slow on a 24-megapixel file.  Automatic adjustment therefore
    uses the faster displayed mean and refines it iteratively.
    """
    header = parse_jpeg(data)
    entropy = split_entropy(data, header.scan_start)
    stats = decode_scan(
        header,
        entropy,
        0,
        0,
        header.frame.mcu_count,
        collect_dc=True,
    )
    if not stats.luma_dc:
        raise ColorFixError("could not decode luma DC coefficients")
    luma = _components(header)[0]
    mean_dc = float(np.mean(np.asarray(stats.luma_dc, dtype=np.float64)))
    return mean_dc * luma.qdc / 8.0 + 128.0


# ---------------------------------------------------------------------------
# Entropy rewriting
# ---------------------------------------------------------------------------


class _BitWriter:
    """MSB-first bit writer with fast byte-range copying."""

    __slots__ = ("out", "_buf", "_nbits")

    def __init__(self) -> None:
        self.out = bytearray()
        self._buf = 0
        self._nbits = 0

    def write_bits(self, value: int, count: int) -> None:
        if count <= 0:
            return
        self._buf = (self._buf << count) | (value & ((1 << count) - 1))
        self._nbits += count
        self._flush()

    def _flush(self) -> None:
        out = self.out
        while self._nbits >= 8:
            self._nbits -= 8
            out.append((self._buf >> self._nbits) & 0xFF)
            self._buf &= (1 << self._nbits) - 1

    def write_source_bits(self, source: bytes, start_bit: int, count: int) -> None:
        """Append ``count`` bits from ``source`` starting at ``start_bit``."""
        if count <= 0:
            return
        pos = start_bit >> 3
        offset = start_bit & 7
        if offset:
            take = min(8 - offset, count)
            byte = source[pos]
            value = (byte >> (8 - offset - take)) & ((1 << take) - 1)
            self.write_bits(value, take)
            pos += 1
            count -= take
        while count >= 8:
            chunk_bytes = min(65536, count // 8)
            chunk = source[pos:pos + chunk_bytes]
            self._append_bytes(chunk)
            pos += chunk_bytes
            count -= chunk_bytes * 8
        if count:
            byte = source[pos]
            value = (byte >> (8 - count)) & ((1 << count) - 1)
            self.write_bits(value, count)

    def _append_bytes(self, chunk: bytes) -> None:
        if not chunk:
            return
        bits = len(chunk) * 8
        value = int.from_bytes(chunk, "big")
        self._buf = (self._buf << bits) | value
        self._nbits += bits
        if self._nbits < 8:
            return
        keep = self._nbits & 7
        full = self._nbits >> 3
        whole = self._buf >> keep
        self.out += whole.to_bytes(full, "big")
        self._buf = self._buf & ((1 << keep) - 1) if keep else 0
        self._nbits = keep

    def finish(self) -> bytes:
        if self._nbits:
            pad = 8 - self._nbits
            self.write_bits((1 << pad) - 1, pad)
        return bytes(self.out)


def _encoder(table) -> dict[int, tuple[int, int]]:
    """Map Huffman symbols to their code and bit length."""
    result: dict[int, tuple[int, int]] = {}
    code = 0
    index = 0
    for length in range(1, 17):
        for _ in range(table.counts[length - 1]):
            if index >= len(table.symbols):
                raise ColorFixError("invalid Huffman table")
            result[table.symbols[index]] = (code, length)
            index += 1
            code += 1
        code <<= 1
    return result


def _amplitude(value: int) -> str:
    size = abs(value).bit_length()
    if size == 0:
        return ""
    if value < 0:
        value += (1 << size) - 1
    return format(value, f"0{size}b")


def _receive_value(reader: BitReader, size: int) -> int:
    if size == 0:
        return 0
    value = reader.read_bits(size)
    if value < (1 << (size - 1)):
        value -= (1 << size) - 1
    return value


def _skip_ac(reader: BitReader, table) -> int:
    k = 1
    while k < 64:
        rs = _decode_symbol(reader, table)
        run = rs >> 4
        size = rs & 0x0F
        if size == 0:
            if run == 15:
                k += 16
                if k > 64:
                    raise ScanError("AC run overflow")
                continue
            break
        k += run
        if k >= 64:
            raise ScanError("AC coefficient index overflow")
        reader.read_bits(size)
        k += 1
    return reader._consumed


def _shift_interval(
    raw: bytes,
    header: JpegHeader,
    shifts: Mapping[int, int],
    encoders: Mapping[int, Mapping[int, tuple[int, int]]] | None = None,
) -> bytes:
    """Rewrite the first DC difference of each component in one interval."""
    reader = BitReader(raw, 0)
    patches: list[tuple[int, int, str]] = []
    shifted: set[int] = set()
    if encoders is None:
        encoders = {}

    try:
        for scan_component in header.scan_components:
            component = header.component(scan_component.cid)
            dc_table = header.table_for(0, scan_component.td)
            ac_table = header.table_for(1, scan_component.ta)
            encoder = encoders.get(scan_component.td)
            if encoder is None:
                encoder = _encoder(dc_table)
                encoders[scan_component.td] = encoder
            for _ in range(component.h * component.v):
                dc_start = reader._consumed
                size = _decode_symbol(reader, dc_table)
                if size > 16:
                    raise ScanError("invalid DC coefficient size")
                difference = _receive_value(reader, size)
                ac_start = reader._consumed
                _skip_ac(reader, ac_table)
                if scan_component.cid in shifted:
                    continue
                shift = int(shifts.get(scan_component.cid, 0))
                if not shift:
                    shifted.add(scan_component.cid)
                    continue
                shifted.add(scan_component.cid)
                new_value = difference + shift
                new_size = abs(new_value).bit_length()
                encoded = encoder.get(new_size)
                if encoded is None:
                    raise ColorFixError(
                        f"DC category {new_size} is not present in Huffman "
                        f"table {scan_component.td}"
                    )
                code, length = encoded
                bits = format(code, f"0{length}b") + _amplitude(new_value)
                patches.append((dc_start, ac_start, bits))
    except (ScanError, IndexError) as exc:
        raise ColorFixError(f"could not parse JPEG scan: {exc}") from exc

    first_mcu_end = reader._consumed
    if not patches:
        return raw

    head_bits = "".join(f"{byte:08b}" for byte in raw[: (first_mcu_end + 7) // 8])
    head_bits = head_bits[:first_mcu_end]
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(patches):
        pieces.append(head_bits[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(head_bits[cursor:first_mcu_end])
    new_head = "".join(pieces)

    writer = _BitWriter()
    if new_head:
        writer.write_bits(int(new_head, 2), len(new_head))
    # The rest of the interval is untouched, including the encoder's original
    # fill bits.  A new short fill is added to keep the stream byte-aligned.
    writer.write_source_bits(raw, first_mcu_end, len(raw) * 8 - first_mcu_end)
    return writer.finish()


def _rewrite_scan(
    header: JpegHeader,
    entropy,
    shifts: Mapping[int, int],
) -> bytes:
    """Return a stuffed scan stream with the requested DC shifts."""
    encoders: dict[int, dict[int, tuple[int, int]]] = {}
    out = bytearray()
    for index, (start, end) in enumerate(entropy.segments):
        raw = entropy.cleaned[start:end]
        if raw:
            out += _stuff(_shift_interval(raw, header, shifts, encoders))
        marker = entropy.markers[index] if index < len(entropy.markers) else 0
        if 0xD0 <= marker <= 0xD7:
            out += bytes((0xFF, marker))
        elif marker == 0xD9:
            break
        elif marker:
            raise ColorFixError(
                f"unexpected marker 0xFF{marker:02X} inside the JPEG scan"
            )
    return bytes(out)


def shift_dc_coefficients(data: bytes, shifts: Mapping[int, int]) -> bytes:
    """Return ``data`` with constant DC offsets applied to components.

    ``shifts`` maps component ids to DC coefficient shifts.  A positive value
    brightens that component.  Only the first DC difference of each component
    in every restart interval is rewritten; all image detail is preserved.
    """
    header = parse_jpeg(data)
    nonzero = {int(cid): int(value) for cid, value in shifts.items() if value}
    if not nonzero:
        return data
    if header.scan_ss != 0 or header.scan_se != 63:
        raise ColorFixError("only interleaved baseline JPEG scans are supported")
    if len(header.scan_components) != len(header.frame.components):
        raise ColorFixError("only interleaved JPEG scans are supported")

    entropy = split_entropy(data, header.scan_start)
    scan = _rewrite_scan(header, entropy, nonzero)
    try:
        result = replace_scan(data, scan)
        with Image.open(io.BytesIO(result)) as image:
            image.load()
    except Exception as exc:  # noqa: BLE001 - validate before returning
        raise ColorFixError(f"adjusted JPEG did not decode: {exc}") from exc
    return result


# ---------------------------------------------------------------------------
# Automatic colour balancing
# ---------------------------------------------------------------------------


def _measured(means: ColorMeans, component: ComponentInfo) -> float:
    if component.role == "Y":
        return means.luma
    if component.role == "Cb":
        return means.cb
    if component.role == "Cr":
        return means.cr
    return means.luma


def _error(
    means: ColorMeans,
    components: list[ComponentInfo],
    targets: Mapping[int, float],
) -> float:
    error = 0.0
    for component in components:
        target = targets.get(component.cid)
        if target is None:
            continue
        weight = 1.0 if component.role == "Y" else 0.6
        error += weight * (_measured(means, component) - target) ** 2
    return error


def auto_adjust_colors(
    data: bytes,
    target_luma: float = TARGET_LUMA,
    target_chroma: float = TARGET_CHROMA,
    max_passes: int = MAX_PASSES,
) -> tuple[bytes, bool]:
    """Balance recovered colours with a lossless DC correction.

    The displayed means are measured at full resolution.  A secant-style
    iteration converges quickly even when the original blacks are clipped,
    because the response to a DC shift is close to linear in the unclipped
    region and only the first predictor is rewritten.
    """
    header = parse_jpeg(data)
    components = [c for c in _components(header) if c.role in ("Y", "Cb", "Cr")]
    if not components:
        return data, False
    if header.scan_ss != 0 or header.scan_se != 63:
        raise ColorFixError("only interleaved baseline JPEG scans are supported")
    if len(header.scan_components) != len(header.frame.components):
        raise ColorFixError("only interleaved JPEG scans are supported")

    # Parsing the markers and splitting the entropy stream are comparatively
    # expensive on a 24-megapixel file, so do them once and reuse them for
    # every trial shift.
    entropy = split_entropy(data, header.scan_start)
    prefix = data[: header.scan_start]
    original = measure_colors(data)
    targets = {
        c.cid: (target_luma if c.role == "Y" else target_chroma)
        for c in components
    }
    total: dict[int, int] = {}
    history: dict[int, list[tuple[int, float]]] = {}
    for component in components:
        measured = _measured(original, component)
        history[component.cid] = [(0, measured)]
        delta = targets[component.cid] - measured
        if abs(delta) < (
            LUMA_TOLERANCE if component.role == "Y" else CHROMA_TOLERANCE
        ):
            total[component.cid] = 0
        else:
            total[component.cid] = round(delta * 8.0 / max(1, component.qdc))

    if not any(total.values()):
        return data, False

    def build(shifts: Mapping[int, int]) -> bytes:
        scan = _rewrite_scan(header, entropy, shifts)
        return prefix + scan + b"\xff\xd9"

    best_data = data
    best_error = _error(original, components, targets)
    means = original
    for _ in range(max(1, max_passes)):
        try:
            candidate = build(total)
            measured = measure_colors(candidate)
        except (ColorFixError, OSError):
            break
        error = _error(measured, components, targets)
        if error < best_error:
            best_data = candidate
            best_error = error
        means = measured
        for component in components:
            history[component.cid].append(
                (total[component.cid], _measured(measured, component))
            )

        changed = False
        for component in components:
            target = targets[component.cid]
            observed = _measured(means, component)
            tolerance = (
                LUMA_TOLERANCE if component.role == "Y" else CHROMA_TOLERANCE
            )
            if abs(observed - target) <= tolerance:
                continue
            points = history[component.cid][-2:]
            if len(points) < 2 or points[0][0] == points[1][0]:
                continue
            (x0, y0), (x1, y1) = points
            slope = (y1 - y0) / (x1 - x0)
            if abs(slope) < 1e-7:
                continue
            proposal = x1 + (target - y1) / slope
            step_limit = max(256.0, abs(x1 - x0) * 2.5)
            proposal = max(x1 - step_limit, min(x1 + step_limit, proposal))
            new_shift = int(round(proposal))
            if new_shift == x1:
                new_shift += 1 if target > observed else -1
            if new_shift != total[component.cid]:
                total[component.cid] = new_shift
                changed = True
        if not changed:
            break

    changed = best_data is not data
    return (best_data, True) if changed else (data, False)
