"""Batch recovery orchestration for the JPEG Recovery application."""

from __future__ import annotations

import io
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image

from .jpeg import (
    JpegHeader,
    build_jpeg,
    parse_jpeg,
)
from .samples import JPEG_EXTENSIONS, SampleInfo
from .sampleless import SamplelessError, recover_without_sample
from .scan import build_entropy, find_scan_start
from .colorfix import auto_adjust_colors

DEFAULT_PREFIX = 153605
AUTO_PREFIXES = (
    DEFAULT_PREFIX,
    65536,
    131072,
    262144,
    524288,
    1048576,
    32768,
    16384,
    8192,
    4096,
)


class RecoveryError(ValueError):
    pass


@dataclass
class RecoveredImage:
    path: str
    width: int
    height: int
    sample: str
    prefix: int
    skipped_rows: int
    adjusted: bool = False
    sampleless: bool = False
    quality: int = 0
    sampling: str = ""
    estimated_rows: bool = False


@dataclass
class FileResult:
    source: str
    ok: bool
    message: str
    images: list[RecoveredImage] = field(default_factory=list)

    @property
    def source_name(self) -> str:
        return os.path.basename(self.source)


@dataclass
class RecoveryConfig:
    encrypted_folder: str
    output_folder: str
    extension: str = ""
    prefix_size: int | None = None  # None == Auto
    filenames_encrypted: bool = False
    separate_subfolders: bool = False
    auto_adjust: bool = False
    post_command: str = ""
    wait_for_process: bool = True
    auto_mode: bool = False
    samples: list[SampleInfo] = field(default_factory=list)
    allow_sampleless: bool = True
    sampleless_quality: int | None = None


@dataclass
class Attempt:
    ok: bool
    message: str
    data: bytes | None = None
    width: int = 0
    height: int = 0
    skipped_rows: int = 0
    prefix: int = 0
    adjusted: bool = False
    sampleless: bool = False
    quality: int = 0
    sampling: str = ""


def auto_adjust_quant(header: JpegHeader, data: bytes) -> tuple[bytes, bool]:
    """Apply lossless DC colour correction to a recovered JPEG.

    The name is kept for compatibility with the original recovery pipeline.
    Quantization tables cannot repair an additive DC offset, so this now
    delegates to :func:`jpegmedic.core.colorfix.auto_adjust_colors`.
    """
    del header  # the corrected header is reparsed from the recovered bytes
    try:
        return auto_adjust_colors(data)
    except Exception:
        return data, False


def recover_bytes(data: bytes, header: JpegHeader, prefix: int) -> Attempt:
    """Recover one file using a known-good header."""
    if prefix < 0 or prefix >= len(data):
        return Attempt(False, "encrypted part size is outside the file")
    scan = find_scan_start(
        header, data, prefix, search_bytes=16384, max_mcus=header.frame.mcu_count
    )
    if scan is None:
        return Attempt(
            False,
            "no usable MCU boundary found after the encrypted prefix",
        )
    cols = header.frame.mcu_cols
    if scan.stats.mcus % cols:
        return Attempt(False, "recovered scan is not a whole number of MCU rows")
    recovered_rows = scan.stats.mcus // cols
    total_rows = header.frame.mcu_rows
    skipped = total_rows - recovered_rows
    if recovered_rows <= 0 or skipped < 0:
        return Attempt(False, "recovered scan does not fit the sample dimensions")
    height = header.frame.height - skipped * header.frame.mcu_height
    if height <= 0:
        return Attempt(False, "nothing remains after removing the encrypted part")
    entropy = build_entropy(scan)
    try:
        out = build_jpeg(header, entropy, height)
    except Exception as exc:
        return Attempt(False, f"could not rebuild JPEG: {exc}")

    try:
        with Image.open(io.BytesIO(out)) as im:
            im.load()
            if im.width != header.frame.width:
                raise RecoveryError("decoded width does not match the sample")
    except Exception as exc:
        return Attempt(False, f"recovered JPEG did not decode: {exc}")

    adjusted = False
    # Colour correction is applied by the caller, which owns the config.
    return Attempt(True, "OK", out, header.frame.width, height, skipped, prefix, adjusted)


def prefix_candidates(data: bytes, prefix_size: int | None) -> list[int]:
    if prefix_size is not None:
        return [prefix_size]
    # Test small prefixes first.  A prefix smaller than the real one starts in
    # encrypted bytes and cannot decode; the first prefix that does decode is
    # therefore the closest candidate above the damage and loses the fewest
    # MCU rows.
    return sorted({value for value in AUTO_PREFIXES if 0 < value < len(data)})


def recover_with_samples(
    data: bytes, headers: Iterable[tuple[str, JpegHeader]], prefix_size: int | None
) -> list[tuple[Attempt, str, int]]:
    """Try each sample header, returning (attempt, sample name, prefix).

    Automatic candidates are tested from smallest to largest.  The first valid
    decode is the one that lost the fewest rows, so it is also the best result.
    """
    results = []
    for sample_name, header in headers:
        last_error = ""
        for prefix in prefix_candidates(data, prefix_size):
            attempt = recover_bytes(data, header, prefix)
            if attempt.ok:
                results.append((attempt, sample_name, prefix))
                break
            last_error = attempt.message
        else:
            results.append(
                (
                    Attempt(False, last_error or "recovery failed"),
                    sample_name,
                    prefix_size or DEFAULT_PREFIX,
                )
            )
    return results


def recover_without_samples(
    data: bytes, prefix_size: int | None, quality: int | None = None
) -> tuple[Attempt, int]:
    """Try the sample-free engine at each prefix candidate.

    Returns ``(attempt, tested_prefix)``.  Automatic candidates are tested in
    ascending order, matching the sample-based path.
    """
    last_error = ""
    last_prefix = prefix_size or DEFAULT_PREFIX
    for prefix in prefix_candidates(data, prefix_size):
        last_prefix = prefix
        try:
            result = recover_without_sample(data, prefix, quality=quality)
        except SamplelessError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            last_error = f"sample-free recovery failed: {exc}"
            continue
        return (
            Attempt(
                True,
                "OK",
                result.data,
                result.width,
                result.height,
                result.skipped_rows,
                result.prefix,
                False,
                True,
                result.quality,
                result.sampling,
            ),
            result.prefix,
        )
    return Attempt(False, last_error or "sample-free recovery failed"), last_prefix


def _strip_ext(name: str, ext: str) -> str:
    if ext:
        suffix = "." + ext.lstrip(".")
        if name.lower().endswith(suffix.lower()):
            return name[: -len(suffix)]
    return name


def output_stem(source_name: str, ext: str) -> str:
    stem = _strip_ext(source_name, ext)
    lower = stem.lower()
    if not lower.endswith((".jpg", ".jpeg", ".jpe")):
        stem += ".jpg"
    return stem


def _variant_name(stem: str, index: int, count: int) -> str:
    if count <= 1:
        return stem
    root, ext = os.path.splitext(stem)
    return f"{root}_{index}{ext}"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while True:
        candidate = f"{root}_{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def run_post_command(command: str, path: str, wait: bool) -> str:
    command = command.strip()
    if not command:
        return ""
    quoted = f'"{path}"'
    cmdline = command.replace("%1", quoted) if "%1" in command else f"{command} {quoted}"
    try:
        proc = subprocess.Popen(cmdline, shell=True)
        if wait:
            code = proc.wait()
            if code:
                return f"post-process exited with code {code}"
        return ""
    except Exception as exc:
        return f"post-process failed: {exc}"


def recover_file(
    source: str,
    config: RecoveryConfig,
    headers: list[tuple[str, JpegHeader]],
) -> FileResult:
    try:
        data = Path(source).read_bytes()
    except OSError as exc:
        return FileResult(source, False, f"cannot read file: {exc}")

    attempts: list[tuple[Attempt, str, int]] = []
    errors: set[str] = set()
    if not headers and not config.allow_sampleless:
        return FileResult(
            source,
            False,
            "no usable sample header and sample-free recovery is disabled; "
            "add a sample photo from the same camera",
        )
    if headers:
        attempts = recover_with_samples(data, headers, config.prefix_size)
        errors.update(a[0].message for a in attempts if a[0].message)

    successes: list[tuple[Attempt, str, int]] = [
        a for a in attempts if a[0].ok and a[0].data is not None
    ]

    # A sample is optional.  When none was supplied, or every sample header was
    # unusable, infer the geometry and standard tables from the surviving scan.
    if not successes and config.allow_sampleless:
        attempt, prefix = recover_without_samples(
            data, config.prefix_size, config.sampleless_quality
        )
        label = "Inferred"
        if attempt.ok:
            successes = [(attempt, label, prefix)]
        else:
            errors.add(attempt.message)

    if not successes:
        message = "; ".join(sorted(e for e in errors if e)) or "recovery failed"
        return FileResult(source, False, message)

    source_name = os.path.basename(source)
    stem = output_stem(source_name, config.extension)
    dest_root = config.output_folder
    if config.separate_subfolders:
        dest_root = os.path.join(dest_root, os.path.splitext(stem)[0])
    try:
        os.makedirs(dest_root, exist_ok=True)
    except OSError as exc:
        return FileResult(source, False, f"cannot create output folder: {exc}")

    images: list[RecoveredImage] = []
    post_notes: list[str] = []
    for i, (attempt, sample_name, prefix) in enumerate(successes, 1):
        assert attempt.data is not None
        payload = attempt.data
        adjusted = False
        if config.auto_adjust:
            # Rebuild the header object used to make this output so the luma
            # tables can be scaled.  Parsing our own output is cheap.
            try:
                out_header = parse_jpeg(payload)
                payload, adjusted = auto_adjust_quant(out_header, payload)
            except Exception:
                adjusted = False

        name = _variant_name(stem, i, len(successes))
        path = _unique_path(os.path.join(dest_root, name))
        try:
            Path(path).write_bytes(payload)
        except OSError as exc:
            return FileResult(source, False, f"cannot write output: {exc}")
        images.append(
            RecoveredImage(
                path=path,
                width=attempt.width,
                height=attempt.height,
                sample=sample_name,
                prefix=prefix,
                skipped_rows=attempt.skipped_rows,
                adjusted=adjusted,
                sampleless=attempt.sampleless,
                quality=attempt.quality,
                sampling=attempt.sampling,
                estimated_rows=attempt.sampleless,
            )
        )
        if config.post_command:
            note = run_post_command(config.post_command, path, config.wait_for_process)
            if note:
                post_notes.append(note)

    note = f"{len(images)} variant(s)" if len(images) > 1 else "OK"
    if post_notes:
        note += "; " + "; ".join(sorted(set(post_notes)))
    return FileResult(source, True, note, images)


def guess_extension(
    folder: str, base_exts: set[str], filenames_encrypted: bool
) -> str:
    counts: dict[str, int] = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return ""
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if "." not in name:
            continue
        stem, ext = name.rsplit(".", 1)
        if not ext:
            continue
        if filenames_encrypted:
            if ext.lower() not in JPEG_EXTENSIONS and ext.lower() not in base_exts:
                counts[ext] = counts.get(ext, 0) + 1
        else:
            if "." not in stem:
                continue
            base_ext = stem.rsplit(".", 1)[1].lower()
            if base_ext in base_exts:
                counts[ext] = counts.get(ext, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def find_candidates(
    folder: str, extension: str, base_exts: set[str], filenames_encrypted: bool
) -> list[str]:
    ext = extension.lstrip(".").lower()
    result = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return result
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        lower = name.lower()
        if ext:
            if not lower.endswith("." + ext):
                continue
            stem = name[: -(len(ext) + 1)]
        else:
            stem = name
        if not filenames_encrypted:
            if not any(stem.lower().endswith("." + b) for b in base_exts):
                continue
        result.append(entry.path)
    return result


def headers_from_samples(samples: list[SampleInfo]) -> list[tuple[str, JpegHeader]]:
    return [(s.name, s.header) for s in samples]
