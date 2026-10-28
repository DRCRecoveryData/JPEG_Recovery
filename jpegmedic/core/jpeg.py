"""Low-level JPEG marker parsing and header construction.

Only the parts needed by the recovery engine are implemented: baseline /
extended-sequential Huffman JPEGs.  Progressive JPEGs are detected and
rejected with a clear error because their coefficient scans cannot be dropped
from an arbitrary MCU boundary the same way a sequential scan can.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable


class JpegError(ValueError):
    """Raised when a file is not a supported JPEG."""


SOI = 0xD8
EOI = 0xD9
SOS = 0xDA
DQT = 0xDB
DHT = 0xC4
DRI = 0xDD
APP0 = 0xE0
APP1 = 0xE1

SOF_MARKERS = {
    0xC0: "baseline",
    0xC1: "extended sequential",
    0xC2: "progressive",
    0xC3: "lossless",
    0xC5: "differential sequential",
    0xC6: "differential progressive",
    0xC7: "differential lossless",
    0xC9: "extended sequential, arithmetic",
    0xCA: "progressive, arithmetic",
    0xCB: "lossless, arithmetic",
    0xCD: "differential sequential, arithmetic",
    0xCE: "differential progressive, arithmetic",
    0xCF: "differential lossless, arithmetic",
}

# Zig-zag order: position in the entropy-coded / DQT stream -> natural index.
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)


@dataclass(frozen=True)
class Component:
    cid: int
    h: int
    v: int
    tq: int


@dataclass
class Frame:
    marker: int
    precision: int
    width: int
    height: int
    components: tuple[Component, ...]

    @property
    def kind(self) -> str:
        return SOF_MARKERS.get(self.marker, "unknown")

    @property
    def progressive(self) -> bool:
        return self.marker in (0xC2, 0xC6, 0xCA, 0xCE)

    @property
    def hmax(self) -> int:
        return max(c.h for c in self.components)

    @property
    def vmax(self) -> int:
        return max(c.v for c in self.components)

    @property
    def mcu_width(self) -> int:
        return 8 * self.hmax

    @property
    def mcu_height(self) -> int:
        return 8 * self.vmax

    @property
    def mcu_cols(self) -> int:
        return (self.width + self.mcu_width - 1) // self.mcu_width

    @property
    def mcu_rows(self) -> int:
        return (self.height + self.mcu_height - 1) // self.mcu_height

    @property
    def mcu_count(self) -> int:
        return self.mcu_cols * self.mcu_rows


@dataclass
class HuffmanTable:
    table_class: int  # 0 = DC, 1 = AC
    table_id: int
    counts: tuple[int, ...]  # 16 entries
    symbols: bytes
    lut: list[int] = field(default_factory=list, repr=False)

    def build_lut(self) -> None:
        """Build a 16-bit look-up table.

        Each entry is ``(symbol << 5) | code_length``.  Zero means an invalid
        code.  A 16-bit peek is enough because JPEG Huffman codes are at most
        16 bits long.
        """
        if len(self.counts) != 16:
            raise JpegError("invalid Huffman table: expected 16 code counts")
        if sum(self.counts) != len(self.symbols):
            raise JpegError("invalid Huffman table: symbol count mismatch")
        lut = [0] * 65536
        code = 0
        k = 0
        for length in range(1, 17):
            for _ in range(self.counts[length - 1]):
                if code >= (1 << length):
                    raise JpegError("invalid Huffman table: code overflow")
                symbol = self.symbols[k]
                k += 1
                shift = 16 - length
                entry = (symbol << 5) | length
                base = code << shift
                for i in range(1 << shift):
                    lut[base + i] = entry
                code += 1
            code <<= 1
        self.lut = lut

    def to_segment(self) -> bytes:
        payload = bytes([(self.table_class << 4) | self.table_id])
        payload += bytes(self.counts) + self.symbols
        return b"\xff\xc4" + struct.pack(">H", len(payload) + 2) + payload


@dataclass(frozen=True)
class ScanComponent:
    cid: int
    td: int
    ta: int


@dataclass(frozen=True)
class Segment:
    marker: int
    offset: int
    raw: bytes


@dataclass
class JpegHeader:
    quant_tables: dict[int, tuple[int, ...]]  # values in zig-zag order
    huffman_tables: dict[tuple[int, int], HuffmanTable]
    frame: Frame
    restart_interval: int
    scan_components: tuple[ScanComponent, ...]
    scan_ss: int
    scan_se: int
    scan_ah: int
    scan_al: int
    scan_start: int
    segments: tuple[Segment, ...]
    data: bytes = field(repr=False, default=b"")

    @property
    def width(self) -> int:
        return self.frame.width

    @property
    def height(self) -> int:
        return self.frame.height

    def component(self, cid: int) -> Component:
        for c in self.frame.components:
            if c.cid == cid:
                return c
        raise JpegError(f"scan references unknown component {cid}")

    def table_for(self, table_class: int, table_id: int) -> HuffmanTable:
        try:
            return self.huffman_tables[(table_class, table_id)]
        except KeyError as exc:
            kind = "DC" if table_class == 0 else "AC"
            raise JpegError(f"missing {kind} Huffman table {table_id}") from exc

    def quant_signature(self) -> tuple:
        return tuple((k, self.quant_tables[k]) for k in sorted(self.quant_tables))


def _u16(data: bytes, pos: int) -> int:
    if pos + 2 > len(data):
        raise JpegError("unexpected end of file")
    return (data[pos] << 8) | data[pos + 1]


def _parse_dqt(payload: bytes, tables: dict[int, tuple[int, ...]]) -> None:
    pos = 0
    while pos < len(payload):
        info = payload[pos]
        pos += 1
        precision = info >> 4
        table_id = info & 0x0F
        if precision == 0:
            need = 64
            if pos + need > len(payload):
                raise JpegError("truncated DQT segment")
            values = tuple(payload[pos:pos + need])
            pos += need
        elif precision == 1:
            need = 128
            if pos + need > len(payload):
                raise JpegError("truncated 16-bit DQT segment")
            values = tuple(
                (payload[pos + i] << 8) | payload[pos + i + 1]
                for i in range(0, need, 2)
            )
            pos += need
        else:
            raise JpegError(f"invalid DQT precision {precision}")
        tables[table_id] = values


def _parse_dht(payload: bytes, tables: dict[tuple[int, int], HuffmanTable]) -> None:
    pos = 0
    while pos < len(payload):
        if pos + 17 > len(payload):
            raise JpegError("truncated DHT segment")
        info = payload[pos]
        pos += 1
        table_class = info >> 4
        table_id = info & 0x0F
        if table_class not in (0, 1):
            raise JpegError(f"invalid Huffman table class {table_class}")
        counts = tuple(payload[pos:pos + 16])
        pos += 16
        n = sum(counts)
        if pos + n > len(payload):
            raise JpegError("truncated Huffman symbols")
        symbols = payload[pos:pos + n]
        pos += n
        table = HuffmanTable(table_class, table_id, counts, symbols)
        table.build_lut()
        tables[(table_class, table_id)] = table


def _parse_sof(marker: int, payload: bytes) -> Frame:
    if len(payload) < 6:
        raise JpegError("truncated SOF segment")
    precision = payload[0]
    height = (payload[1] << 8) | payload[2]
    width = (payload[3] << 8) | payload[4]
    ncomp = payload[5]
    if ncomp < 1 or len(payload) < 6 + 3 * ncomp:
        raise JpegError("invalid SOF component table")
    components = []
    pos = 6
    for _ in range(ncomp):
        cid = payload[pos]
        hv = payload[pos + 1]
        tq = payload[pos + 2]
        h = hv >> 4
        v = hv & 0x0F
        if not (1 <= h <= 4 and 1 <= v <= 4):
            raise JpegError("invalid sampling factor")
        components.append(Component(cid, h, v, tq))
        pos += 3
    if width < 1 or height < 1:
        raise JpegError("invalid image dimensions")
    return Frame(marker, precision, width, height, tuple(components))


def _parse_sos(payload: bytes) -> tuple[tuple[ScanComponent, ...], int, int, int, int]:
    if len(payload) < 1:
        raise JpegError("truncated SOS segment")
    ns = payload[0]
    if ns < 1 or len(payload) < 1 + 2 * ns + 3:
        raise JpegError("invalid SOS component table")
    comps = []
    pos = 1
    for _ in range(ns):
        cid = payload[pos]
        tables = payload[pos + 1]
        comps.append(ScanComponent(cid, tables >> 4, tables & 0x0F))
        pos += 2
    ss = payload[pos]
    se = payload[pos + 1]
    ahal = payload[pos + 2]
    return tuple(comps), ss, se, ahal >> 4, ahal & 0x0F


def parse_jpeg(data: bytes) -> JpegHeader:
    """Parse a JPEG up to (and including) its first SOS header."""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise JpegError("not a JPEG file (missing SOI marker)")

    pos = 2
    quant_tables: dict[int, tuple[int, ...]] = {}
    huffman_tables: dict[tuple[int, int], HuffmanTable] = {}
    frame: Frame | None = None
    restart_interval = 0
    segments: list[Segment] = []
    scan_start = None
    scan_components: tuple[ScanComponent, ...] = ()
    scan_ss = scan_se = scan_ah = scan_al = 0

    while pos < len(data):
        if data[pos] != 0xFF:
            raise JpegError(f"expected marker at offset {pos}")
        start = pos
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            raise JpegError("truncated marker")
        marker = data[pos]
        pos += 1

        if marker == 0xD8:  # stray SOI
            continue
        if marker == 0xD9:
            raise JpegError("JPEG has no image scan")
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            # Stand-alone marker outside entropy data; copy it through.
            segments.append(Segment(marker, start, data[start:pos]))
            continue

        length = _u16(data, pos)
        if length < 2 or pos + length > len(data):
            raise JpegError(f"invalid segment length at offset {start}")
        payload = data[pos + 2:pos + length]
        pos += length
        segments.append(Segment(marker, start, data[start:pos]))

        if marker == DQT:
            _parse_dqt(payload, quant_tables)
        elif marker == DHT:
            _parse_dht(payload, huffman_tables)
        elif marker in SOF_MARKERS:
            if frame is not None:
                raise JpegError("multiple SOF segments")
            frame = _parse_sof(marker, payload)
        elif marker == DRI:
            if len(payload) < 2:
                raise JpegError("invalid DRI segment")
            restart_interval = (payload[0] << 8) | payload[1]
        elif marker == SOS:
            scan_components, scan_ss, scan_se, scan_ah, scan_al = _parse_sos(payload)
            scan_start = pos
            break

    if frame is None:
        raise JpegError("JPEG has no frame header")
    if scan_start is None:
        raise JpegError("JPEG has no SOS scan header")
    if not quant_tables:
        raise JpegError("JPEG has no quantization tables")
    if not huffman_tables:
        raise JpegError("JPEG has no Huffman tables")
    if frame.progressive:
        raise JpegError(
            "progressive JPEGs are not supported by this recovery engine; "
            "use a baseline sample"
        )
    if frame.marker not in (0xC0, 0xC1):
        raise JpegError(f"unsupported JPEG process: {frame.kind}")
    if frame.precision != 8:
        raise JpegError(
            f"unsupported {frame.precision}-bit JPEG precision; only 8-bit "
            "baseline and extended-sequential samples are supported"
        )

    # Verify that every scan component exists.
    for sc in scan_components:
        frame_component = frame.components
        if not any(c.cid == sc.cid for c in frame_component):
            raise JpegError(f"SOS references missing component {sc.cid}")

    return JpegHeader(
        quant_tables=quant_tables,
        huffman_tables=huffman_tables,
        frame=frame,
        restart_interval=restart_interval,
        scan_components=scan_components,
        scan_ss=scan_ss,
        scan_se=scan_se,
        scan_ah=scan_ah,
        scan_al=scan_al,
        scan_start=scan_start,
        segments=tuple(segments),
        data=data,
    )


def _dqt_segment(table_id: int, values: Iterable[int]) -> bytes:
    values = tuple(values)
    if len(values) != 64:
        raise JpegError("a quantization table must have 64 values")
    if any(not (1 <= v <= 65535) for v in values):
        raise JpegError("quantization values must be in 1..65535")
    if max(values) <= 255:
        payload = bytes([table_id & 0x0F]) + bytes(values)
    else:
        payload = bytes([0x10 | (table_id & 0x0F)])
        payload += b"".join(struct.pack(">H", v) for v in values)
    return b"\xff\xdb" + struct.pack(">H", len(payload) + 2) + payload


def _sof_segment(frame: Frame) -> bytes:
    payload = bytes([frame.precision])
    payload += struct.pack(">HH", frame.height, frame.width)
    payload += bytes([len(frame.components)])
    for c in frame.components:
        payload += bytes([c.cid, (c.h << 4) | c.v, c.tq])
    return bytes([0xFF, frame.marker]) + struct.pack(">H", len(payload) + 2) + payload


def _sos_segment(header: JpegHeader) -> bytes:
    payload = bytes([len(header.scan_components)])
    for sc in header.scan_components:
        payload += bytes([sc.cid, (sc.td << 4) | sc.ta])
    payload += bytes([header.scan_ss, header.scan_se, (header.scan_ah << 4) | header.scan_al])
    return b"\xff\xda" + struct.pack(">H", len(payload) + 2) + payload


def build_jpeg(header: JpegHeader, entropy: bytes, height: int | None = None) -> bytes:
    """Build a fresh JPEG header around an already encoded entropy stream."""
    frame = Frame(
        marker=header.frame.marker,
        precision=header.frame.precision,
        width=header.frame.width,
        height=header.frame.height if height is None else height,
        components=header.frame.components,
    )
    out = bytearray(b"\xff\xd8")
    # Minimal JFIF marker keeps the output friendly to older viewers.
    out += b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    for table_id in sorted(header.quant_tables):
        out += _dqt_segment(table_id, header.quant_tables[table_id])
    out += _sof_segment(frame)
    for key in sorted(header.huffman_tables):
        out += header.huffman_tables[key].to_segment()
    if header.restart_interval:
        out += b"\xff\xdd\x00\x04" + struct.pack(">H", header.restart_interval)
    out += _sos_segment(header)
    out += entropy
    out += b"\xff\xd9"
    return bytes(out)


def replace_quant_tables(data: bytes, tables: dict[int, tuple[int, ...]]) -> bytes:
    """Return ``data`` with its DQT segments replaced.

    All other segments, including the scan data, stay byte-identical.  This is
    what the post-processing colour sliders use.
    """
    header = parse_jpeg(data)
    out = bytearray()
    for seg in header.segments:
        if seg.marker == DQT:
            continue
        if seg.marker == SOS:
            break
        out += seg.raw
    if not out.startswith(b"\xff\xd8"):
        out = bytearray(b"\xff\xd8") + out
    for table_id in sorted(tables):
        out += _dqt_segment(table_id, tables[table_id])
    out += _sos_segment(header)
    out += data[header.scan_start:]
    return bytes(out)


def replace_scan(
    data: bytes, entropy: bytes, append_eoi: bool = True
) -> bytes:
    """Return ``data`` with its entropy-coded scan data replaced.

    The marker segments through SOS are kept byte-for-byte, so dimensions,
    sampling factors, quantization tables and Huffman tables are unchanged.
    ``entropy`` may include restart markers.  By default an EOI marker is
    appended when the supplied stream does not already end with one.
    """
    header = parse_jpeg(data)
    scan = bytes(entropy)
    if append_eoi and not scan.endswith(b"\xff\xd9"):
        scan += b"\xff\xd9"
    return data[: header.scan_start] + scan


def quant_tables_used_by_components(header: JpegHeader) -> dict[str, list[int]]:
    """Map colour roles to DQT table ids, e.g. {'Y': [0], 'Cb': [1]}."""
    result: dict[str, list[int]] = {"Y": [], "Cb": [], "Cr": [], "A": []}
    n = len(header.frame.components)
    for index, comp in enumerate(header.frame.components):
        if n == 1:
            role = "Y"
        elif index == 0:
            role = "Y"
        elif index == 1:
            role = "Cb"
        elif index == 2:
            role = "Cr"
        else:
            role = "A"
        if comp.tq not in result[role]:
            result[role].append(comp.tq)
    return result
