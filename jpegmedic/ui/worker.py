"""Background recovery worker."""

from __future__ import annotations

import os
import time
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.jpeg import JpegHeader
from ..core.recovery import FileResult, RecoveryConfig, recover_file


class RecoveryWorker(QThread):
    """Run a batch of files without blocking the interface."""

    progress = pyqtSignal(int, int, str)
    file_finished = pyqtSignal(object, float)
    log = pyqtSignal(str)
    completed = pyqtSignal(int, int, bool)  # recovered, failed, cancelled

    def __init__(
        self,
        config: RecoveryConfig,
        headers: list[tuple[str, JpegHeader]],
        files: list[str],
        parent=None,
    ):
        super().__init__(parent)
        self.config = config
        self.headers = headers
        self.files = list(files)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D401 - QThread entry point
        recovered = 0
        failed = 0
        cancelled = False
        total = len(self.files)
        self.log.emit(
            f"Starting recovery of {total} file{'s' if total != 1 else ''}…"
        )
        for index, path in enumerate(self.files, 1):
            if self._cancel:
                cancelled = True
                self.log.emit("Recovery cancelled by the user.")
                break
            name = os.path.basename(path)
            self.progress.emit(index - 1, total, name)
            started = time.perf_counter()
            try:
                result = recover_file(path, self.config, self.headers)
            except Exception as exc:  # noqa: BLE001 - keep the batch alive
                result = FileResult(
                    path, False, f"unexpected error: {exc}"
                )
                self.log.emit(traceback.format_exc(limit=3).strip())
            elapsed = time.perf_counter() - started
            if result.ok:
                recovered += 1
                self.log.emit(
                    f"[OK]   {name} → {len(result.images)} image"
                    f"{'s' if len(result.images) != 1 else ''} "
                    f"({elapsed:.2f}s)"
                )
                for image in result.images:
                    header = image.sample
                    if image.sampleless:
                        header = (
                            f"inferred {image.sampling} · q{image.quality}"
                        )
                    self.log.emit(
                        f"       {os.path.basename(image.path)}  "
                        f"{image.width}×{image.height}  "
                        f"header: {header}  prefix: {image.prefix}"
                    )
            else:
                failed += 1
                self.log.emit(f"[FAIL] {name} — {result.message}")
            self.file_finished.emit(result, elapsed)
        self.completed.emit(recovered, failed, cancelled)
