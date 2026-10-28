"""Huffman scan handling for baseline JPEGs.

The recovery engine never has to run an IDCT: it validates and counts the
entropy-coded MCUs, then rewrites the byte alignment so the surviving scan can
be wrapped in a fresh JPEG header.  Keeping this module free of Qt makes it
easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .jpeg import HuffmanTable, JpegHeader


class ScanError(ValueError):
    """The entropy stream could not be decoded at this alignment."""


class BitReader:
    """MSB-first bit reader with a 16-bit look-ahead buffer."""

    __slots__ = ("_data", "_length", "_pos", "_buf", "_nbits", "_consumed", "_total")

    def __init__(self, data, start_bit: int = 0):
        self._data = data
        self._length = len(data)
        self._pos = start_bit >> 3
        self._buf = 0
        self._nbits = 0
        self._consumed = 0
        self._total = self._length * 8 - start_bit

        skip = start_bit & 7
        if skip:
            if self._pos >= self._length:
                raise ScanError("start offset beyond entropy data")
            self._buf = data[self._pos] & ((1 << (8 - skip)) - 1)
            self._nbits = 8 - skip
            self._pos += 1

    def _fill(self) -> None:
        while self._nbits <= 24 and self._pos < self._length:
            self._buf = (self._buf << 8) | self._data[self._pos]
            self._nbits += 8
            self._pos += 1

    def remaining(self) -> int:
        return self._total - self._consumed

    def peek16(self) -> int:
        if self._nbits < 16:
            self._fill()
        if self._nbits > 16:
            return (self._buf >> (self._nbits - 16)) & 0xFFFF
        pad = 16 - self._nbits
        return ((self._buf << pad) | ((1 << pad) - 1)) & 0xFFFF

    def skip(self, n: int) -> None:
        if n < 0:
            raise ScanError("invalid bit count")
        if n > self._nbits:
            self._fill()
        if n > self._nbits:
            raise ScanError("bit buffer underrun")
        self._nbits -= n
        self._buf &= (1 << self._nbits) - 1
        self._consumed += n

    def read_bits(self, n: int) -> int:
        if n == 0:
            return 0
        if n > self.remaining():
            raise ScanError("unexpected end of entropy data")
        if n > self._nbits:
            self._fill()
        if n > self._nbits:
            raise ScanError("unexpected end of entropy data")
        shift = self._nbits - n
        value = (self._buf >> shift) & ((1 << n) - 1)
        self._nbits -= n
        self._buf &= (1 << self._nbits) - 1
        self._consumed += n
        return value

    def trailing_bits(self) -> tuple[int, int]:
        """Return (count, value) of the unconsumed bits."""
        n = self.remaining()
        if n <= 0:
            return 0, 0
        if self._nbits < n:
            self._fill()
        if self._nbits < n:
            n = self._nbits
        return n, self._buf & ((1 << n) - 1)


@dataclass
class EntropySegments:
    cleaned: bytes = field(repr=False)
    segments: list[tuple[int, int]] = field(repr=False)
    markers: list[int] = field(repr=False)
    eoi_found: bool = field(repr=False)
    raw_to_clean: dict[int, int] = field(repr=False)


def split_entropy(
    data: bytes, start_byte: int, map_until: int | None = None
) -> EntropySegments:
    """Split scan data at restart markers and remove byte stuffing.

    ``cleaned`` is the concatenation of every interval with 0xFF00 replaced by
    0xFF.  Restart/EOI markers are removed and recorded in ``markers``.
    """
    cleaned = bytearray()
    segments: list[tuple[int, int]] = []
    markers: list[int] = []
    raw_to_clean: dict[int, int] = {}
    seg_start = 0
    i = start_byte
    n = len(data)
    eoi_found = False
    stop_at = n if map_until is None else min(n, map_until)

    while i < n:
        if i < stop_at:
            raw_to_clean.setdefault(i, len(cleaned))
        byte = data[i]
        if byte == 0xFF:
            if i + 1 >= n:
                cleaned.append(0xFF)
                i += 1
                continue
            nxt = data[i + 1]
            if nxt == 0x00:
                cleaned.append(0xFF)
                i += 2
                continue
            if 0xD0 <= nxt <= 0xD7:
                segments.append((seg_start, len(cleaned)))
                markers.append(nxt)
                seg_start = len(cleaned)
                i += 2
                continue
            if nxt == 0xD9:
                segments.append((seg_start, len(cleaned)))
                markers.append(nxt)
                eoi_found = True
                i += 2
                break
            if nxt == 0xFF:
                # Fill byte before a marker.
                i += 1
                continue
            # Some other marker, e.g. an APP segment after the scan.  Stop.
            segments.append((seg_start, len(cleaned)))
            markers.append(nxt)
            i += 2
            break
        cleaned.append(byte)
        i += 1

    if not segments or segments[-1][1] != len(cleaned):
        segments.append((seg_start, len(cleaned)))
        if len(markers) < len(segments):
            markers.append(0)

    return EntropySegments(bytes(cleaned), segments, markers, eoi_found, raw_to_clean)


@dataclass
class ScanStats:
    mcus: int
    blocks: int
    eoi: bool
    reason: str
    consumed_bits: int
    interval_counts: list[int]
    boundaries: list[int] = field(default_factory=list, repr=False)
    # First-block DC value of the leading scan component for each MCU.  Only
    # populated when ``decode_scan(..., collect_dc=True)`` is used; the
    # sample-free engine needs these values to infer the JPEG row width.
    luma_dc: list[int] = field(default_factory=list, repr=False)


def _decode_symbol(reader: BitReader, table: HuffmanTable) -> int:
    entry = table.lut[reader.peek16()]
    if entry == 0:
        raise ScanError("invalid Huffman code")
    length = entry & 31
    if length > reader.remaining():
        raise ScanError("Huffman code crosses end of entropy data")
    reader.skip(length)
    return entry >> 5


def _receive_extend(reader: BitReader, n: int) -> int:
    if n == 0:
        return 0
    value = reader.read_bits(n)
    if value < (1 << (n - 1)):
        value -= (1 << n) - 1
    return value


def _decode_block(
    reader: BitReader, dc: HuffmanTable, ac: HuffmanTable, predictor: int
) -> int:
    size = _decode_symbol(reader, dc)
    if size > 16:
        raise ScanError("invalid DC coefficient size")
    value = predictor + _receive_extend(reader, size)
    k = 1
    while k < 64:
        rs = _decode_symbol(reader, ac)
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
        _receive_extend(reader, size)
        k += 1
    return value


def decode_scan(
    header: JpegHeader,
    entropy: EntropySegments,
    start_clean_idx: int,
    start_bit: int = 0,
    max_mcus: int | None = None,
    collect_dc: bool = False,
) -> ScanStats:
    """Decode/count MCUs from a cleaned entropy stream.

    The first segment may start at ``start_bit``; all later intervals start at
    a byte boundary, as required by the JPEG restart marker format.  When
    ``collect_dc`` is true, the first-block DC value of the leading scan
    component is recorded for every MCU.
    """
    frame = header.frame
    if len(header.scan_components) != len(frame.components):
        raise ScanError("only interleaved baseline scans are supported")

    dc_pred = {c.cid: 0 for c in frame.components}
    mcus = 0
    blocks = 0
    interval_counts: list[int] = []
    boundaries: list[int] = []
    luma_dc: list[int] = [] if collect_dc else []
    consumed = 0
    eoi = False
    reason = "end of data"

    seg_index = 0
    for seg_index, (seg_start, seg_end) in enumerate(entropy.segments):
        if seg_end < start_clean_idx:
            continue
        if seg_index > 0:
            start_bit = 0
        local_start = max(seg_start, start_clean_idx) if seg_index == 0 else seg_start
        if local_start >= seg_end:
            continue
        view = memoryview(entropy.cleaned)[local_start:seg_end]
        reader = BitReader(view, start_bit)
        baseline_consumed = reader._consumed
        interval_mcus = 0

        try:
            while True:
                remaining = reader.remaining()
                if remaining <= 7:
                    break
                if header.restart_interval and interval_mcus >= header.restart_interval:
                    break
                boundaries.append(local_start * 8 + start_bit + reader._consumed)
                first_dc: int | None = None
                for scan_index, sc in enumerate(header.scan_components):
                    comp = header.component(sc.cid)
                    dc_table = header.table_for(0, sc.td)
                    ac_table = header.table_for(1, sc.ta)
                    predictor = dc_pred[sc.cid]
                    for _ in range(comp.v):
                        for _ in range(comp.h):
                            predictor = _decode_block(
                                reader, dc_table, ac_table, predictor
                            )
                            if collect_dc and scan_index == 0 and first_dc is None:
                                first_dc = predictor
                            blocks += 1
                    dc_pred[sc.cid] = predictor
                if collect_dc:
                    luma_dc.append(0 if first_dc is None else first_dc)
                mcus += 1
                interval_mcus += 1
                if max_mcus is not None and mcus > max_mcus:
                    raise ScanError("too many MCUs for the supplied header")
        except ScanError as exc:
            return ScanStats(
                mcus, blocks, False, str(exc), consumed, interval_counts, boundaries,
                luma_dc,
            )
        finally:
            consumed += reader._consumed - baseline_consumed

        interval_counts.append(interval_mcus)
        marker = entropy.markers[seg_index] if seg_index < len(entropy.markers) else 0
        is_last = seg_index == len(entropy.segments) - 1

        if marker == 0xD9:
            eoi = True
            reason = "EOI"
            break
        if 0xD0 <= marker <= 0xD7:
            # Restart marker: the interval must end on a byte boundary and,
            # when DRI is known, contain exactly DRI MCUs.
            if header.restart_interval and interval_mcus != header.restart_interval:
                return ScanStats(
                    mcus, blocks, False,
                    "restart interval MCU count mismatch",
                    consumed, interval_counts, boundaries, luma_dc,
                )
            for cid in dc_pred:
                dc_pred[cid] = 0
            continue
        if is_last:
            reason = "no EOI marker"
            break
        reason = f"unexpected marker 0xFF{marker:02X}"
        break

    return ScanStats(
        mcus, blocks, eoi, reason, consumed, interval_counts, boundaries, luma_dc
    )


@dataclass
class ScanSlice:
    entropy: EntropySegments = field(repr=False)
    first_segment: int
    start_clean_idx: int
    start_bit: int
    stats: ScanStats

    @property
    def mcus(self) -> int:
        return self.stats.mcus


def _looks_like_padding(reader: BitReader) -> bool:
    n, value = reader.trailing_bits()
    if n > 7:
        return False
    if n == 0:
        return True
    return value in (0, (1 << n) - 1)


def _row_aligned(
    header: JpegHeader,
    entropy: EntropySegments,
    clean_idx: int,
    bit: int,
    stats: ScanStats,
    max_mcus: int,
) -> ScanSlice | None:
    """Advance a valid MCU boundary to the start of the next MCU row."""
    cols = header.frame.mcu_cols
    if stats.mcus <= 0 or stats.mcus > max_mcus or not stats.eoi:
        return None
    if stats.mcus % cols == 0:
        return ScanSlice(entropy, 0, clean_idx, bit, stats)
    skip = stats.mcus % cols
    if skip >= len(stats.boundaries):
        return None
    absolute = stats.boundaries[skip]
    new_idx = absolute // 8
    new_bit = absolute % 8
    new_stats = decode_scan(header, entropy, new_idx, new_bit, max_mcus)
    if (
        not new_stats.eoi
        or new_stats.mcus <= 0
        or new_stats.mcus > max_mcus
        or new_stats.mcus % cols != 0
    ):
        return None
    first_segment = 0
    for index, (start, end) in enumerate(entropy.segments):
        if start * 8 <= absolute < end * 8 or (index == len(entropy.segments) - 1 and absolute >= start * 8):
            first_segment = index
            break
    return ScanSlice(entropy, first_segment, new_idx, new_bit, new_stats)


def find_scan_start(
    header: JpegHeader,
    data: bytes,
    prefix_size: int,
    search_bytes: int = 16384,
    max_mcus: int | None = None,
) -> ScanSlice | None:
    """Find the first MCU boundary after the encrypted prefix.

    The caller supplies a header whose dimensions/sampling are believed to
    match the damaged file.  Counts are used to validate candidate bit
    offsets; a wrong offset nearly always raises inside a few MCUs.
    """
    if prefix_size < 0 or prefix_size >= len(data):
        return None
    cols = header.frame.mcu_cols
    total = header.frame.mcu_count
    if max_mcus is None:
        max_mcus = total

    # Restart markers give us a free, byte-aligned synchronization point.
    if header.restart_interval:
        limit = min(len(data) - 1, prefix_size + search_bytes)
        index = prefix_size
        while index < limit:
            marker_at = data.find(b"\xff", index, limit)
            if marker_at < 0 or marker_at + 1 >= len(data):
                break
            if not 0xD0 <= data[marker_at + 1] <= 0xD7:
                index = marker_at + 1
                continue
            start = marker_at + 2
            entropy = split_entropy(data, start)
            stats = decode_scan(header, entropy, 0, 0, max_mcus)
            if (
                stats.eoi
                and 0 < stats.mcus <= max_mcus
                and stats.mcus % cols == 0
            ):
                return ScanSlice(entropy, 0, 0, 0, stats)
            # Starting inside a restart interval would leave a partial
            # interval at the front of the rebuilt stream, so advance to the
            # next marker and try again instead of shifting bits.
            index = marker_at + 2
        return None

    start_byte = prefix_size
    search_end = min(len(data), prefix_size + search_bytes)
    entropy = split_entropy(data, start_byte, map_until=search_end)

    # Only the first interval is interesting for a non-restart stream, but we
    # still handle a rare embedded restart marker by decoding all intervals.
    first_end = entropy.segments[0][1] if entropy.segments else 0
    probe_limit = 48
    for raw_index in range(start_byte, search_end):
        clean_idx = entropy.raw_to_clean.get(raw_index)
        if clean_idx is None or clean_idx >= first_end:
            continue
        for bit in range(8):
            if clean_idx * 8 + bit >= first_end * 8:
                break
            # Decode a short prefix first.  A wrong bit alignment practically
            # always fails within a few MCUs; only promising candidates pay the
            # cost of a full scan.
            probe = decode_scan(header, entropy, clean_idx, bit, probe_limit)
            promising = probe.eoi or (
                probe.mcus >= probe_limit
                and probe.reason == "too many MCUs for the supplied header"
            )
            if not promising:
                continue
            stats = probe if probe.eoi else decode_scan(
                header, entropy, clean_idx, bit, max_mcus
            )
            if (
                stats.eoi
                and stats.mcus > 0
                and stats.mcus <= max_mcus
            ):
                aligned = _row_aligned(
                    header, entropy, clean_idx, bit, stats, max_mcus
                )
                if aligned is not None and aligned.stats.mcus >= cols:
                    return aligned
    return None


def shift_bits(data: bytes, bit_offset: int) -> bytes:
    """Drop ``bit_offset`` leading bits and repack starting at the MSB."""
    if bit_offset == 0:
        return bytes(data)
    if not 1 <= bit_offset <= 7:
        raise ValueError("bit_offset must be in 0..7")
    if not data:
        return b""
    out = bytearray()
    buf = data[0] & ((1 << (8 - bit_offset)) - 1)
    nbits = 8 - bit_offset
    for byte in data[1:]:
        buf = (buf << 8) | byte
        nbits += 8
        while nbits >= 8:
            nbits -= 8
            out.append((buf >> nbits) & 0xFF)
            buf &= (1 << nbits) - 1
    # Pad the final partial byte with 1s, as JPEG encoders do.
    if nbits:
        pad = 8 - nbits
        out.append((buf << pad) | ((1 << pad) - 1))
    return bytes(out)


def _stuff(segment: bytes) -> bytes:
    return segment.replace(b"\xff", b"\xff\x00")


def build_entropy(slice_: ScanSlice) -> bytes:
    """Return a byte-aligned entropy stream starting at the found MCU."""
    entropy = slice_.entropy
    out = bytearray()
    first = True
    for index in range(slice_.first_segment, len(entropy.segments)):
        start, end = entropy.segments[index]
        if first:
            start = max(start, slice_.start_clean_idx)
            chunk = entropy.cleaned[start:end]
            if slice_.start_bit:
                chunk = shift_bits(chunk, slice_.start_bit)
            first = False
        else:
            chunk = entropy.cleaned[start:end]
        if chunk:
            out += _stuff(chunk)
        marker = entropy.markers[index] if index < len(entropy.markers) else 0
        if 0xD0 <= marker <= 0xD7:
            out += bytes((0xFF, marker))
        elif marker == 0xD9:
            break
    return bytes(out)
