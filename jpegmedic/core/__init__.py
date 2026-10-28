"""Recovery engine (no Qt dependencies)."""

from .jpeg import JpegError, JpegHeader, parse_jpeg
from .recovery import (
    FileResult,
    RecoveryConfig,
    RecoveredImage,
    find_candidates,
    guess_extension,
    recover_file,
)
from .samples import SampleInfo, load_sample

__all__ = [
    "FileResult",
    "JpegError",
    "JpegHeader",
    "RecoveredImage",
    "RecoveryConfig",
    "SampleInfo",
    "find_candidates",
    "guess_extension",
    "load_sample",
    "parse_jpeg",
    "recover_file",
]
