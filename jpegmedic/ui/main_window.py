"""Main window and workflow wiring."""

from __future__ import annotations

import os

from PyQt6.QtCore import QSettings, QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..core.recovery import find_candidates, headers_from_samples
from ..core.samples import sample_base_extensions
from .postprocess_page import PostProcessPage
from .results_page import ResultsPage
from .setup_page import SetupPage
from .theme import MUTED
from .widgets import HeaderBar, app_icon
from .worker import RecoveryWorker


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} — JPEG Recovery")
        self.setWindowIcon(app_icon())
        self.resize(1180, 800)
        self.setMinimumSize(980, 680)

        self._worker: RecoveryWorker | None = None
        self._config = None
        self._files: list[str] = []

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(central)

        self.header = HeaderBar(
            "JPEG Recovery",
            "Anti-Ransomware Edition · recover JPEGs after a ransomware attack",
        )
        layout.addWidget(self.header)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.setup_page = SetupPage()
        self.results_page = ResultsPage()
        self.postprocess_page = PostProcessPage()
        self.stack.addWidget(self.setup_page)
        self.stack.addWidget(self.results_page)
        self.stack.addWidget(self.postprocess_page)

        self._build_status_bar()
        self._connect_pages()
        self._restore_window()
        self._show_page(0, 0, 0)

    # ------------------------------------------------------------------ setup
    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)
        bar.showMessage(f"Ready · version {__version__}")
        about = QPushButton("About")
        about.setObjectName("Link")
        about.setCursor(Qt.CursorShape.PointingHandCursor)
        about.clicked.connect(self._about)
        bar.addPermanentWidget(about)

    def _connect_pages(self) -> None:
        self.setup_page.start_requested.connect(self._start_recovery)
        self.results_page.back_requested.connect(self._results_back)
        self.results_page.continue_requested.connect(self._results_continue)
        self.results_page.cancel_requested.connect(self._cancel_recovery)
        self.results_page.open_output_requested.connect(self._open_output)
        self.postprocess_page.back_requested.connect(self._postprocess_back)
        self.postprocess_page.done_requested.connect(self._postprocess_done)
        self.postprocess_page.image_saved.connect(self._image_saved)
        self.postprocess_page.image_deleted.connect(self._image_deleted)

    def _show_page(self, index: int, step: int, completed: int) -> None:
        self.stack.setCurrentIndex(index)
        self.header.set_step(step, completed)

    # --------------------------------------------------------------- recovery
    def _start_recovery(self) -> None:
        page = self.setup_page
        problem = page.validate()
        if problem:
            QMessageBox.warning(self, "Cannot start recovery", problem)
            return

        # If the damaged folder also contains readable JPEGs, use them as
        # implicit samples.  If there are none, the sample-free engine takes
        # over automatically.
        if not page.samples and page.auto_mode.isChecked():
            added = page.add_implicit_samples()
            if added:
                self.statusBar().showMessage(
                    f"Using {added} readable JPEG(s) from the source folder as samples."
                )
        page.save_settings()
        config = page.config()
        sample_headers = headers_from_samples(config.samples)
        files = find_candidates(
            config.encrypted_folder,
            config.extension,
            sample_base_extensions(config.samples),
            config.filenames_encrypted,
        )
        if not files:
            QMessageBox.information(
                self,
                "No matching files",
                "No files in the source folder match the current extension "
                "and naming settings.  Check the file extension and the "
                "\"file names are encrypted\" option.",
            )
            return
        if not sample_headers and not config.allow_sampleless:
            QMessageBox.information(
                self,
                "Sample required",
                "No readable sample was found.  Add a sample photo, place "
                "readable JPEGs in the source folder, or enable \"Recover "
                "without a sample\".",
            )
            return

        self._config = config
        self._files = files
        self.results_page.set_output_folder(config.output_folder)
        self.results_page.start_run(len(files))
        self._show_page(1, 1, 1)
        self.setup_page.set_busy(True)
        mode = "sample-free" if not sample_headers else "sample-based"
        self.statusBar().showMessage(
            f"Recovering {len(files)} file(s) ({mode})…"
        )

        worker = RecoveryWorker(config, sample_headers, files, self)
        worker.progress.connect(self.results_page.set_current_file)
        worker.file_finished.connect(self.results_page.handle_result)
        worker.log.connect(self.results_page.append_log)
        worker.completed.connect(self._recovery_completed)
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        worker.start()

    def _recovery_completed(
        self, recovered: int, failed: int, cancelled: bool
    ) -> None:
        self.setup_page.set_busy(False)
        self.results_page.finish_run(recovered, failed, cancelled)
        if cancelled:
            self.statusBar().showMessage("Recovery cancelled.")
            return
        image_count = len(self.results_page.images)
        self.statusBar().showMessage(
            f"Recovery finished: {image_count} image(s) from "
            f"{recovered} file(s), {failed} failed."
        )
        if (
            image_count
            and self.setup_page.open_when_done.isChecked()
            and self._config is not None
            and os.path.isdir(self._config.output_folder)
        ):
            folder = self._config.output_folder
            QTimer.singleShot(
                250,
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)),
            )

    def _worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()

    def _cancel_recovery(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.statusBar().showMessage("Cancelling after the current file…")

    def _results_back(self) -> None:
        if self._worker is not None:
            return
        self.setup_page.set_busy(False)
        self.setup_page.show_summary()
        self._show_page(0, 0, 0)
        self.statusBar().showMessage("Ready.")

    def _results_continue(self) -> None:
        images = self.results_page.images
        if not images:
            QMessageBox.information(
                self, "Nothing to post-process", "No recovered images were produced."
            )
            return
        output = self._config.output_folder if self._config else ""
        self.postprocess_page.set_images(images, output)
        self._show_page(2, 2, 2)
        self.statusBar().showMessage(
            f"Editing {len(images)} recovered image(s)."
        )

    # ----------------------------------------------------------- post-process
    def _postprocess_back(self) -> None:
        self._show_page(1, 1, 1)
        self.statusBar().showMessage("Recovery results.")

    def _postprocess_done(self) -> None:
        self._show_page(0, 0, 3)
        self.setup_page.show_summary()
        output = self._config.output_folder if self._config else ""
        self.statusBar().showMessage(f"Finished. Recovered files are in {output}")

    def _image_saved(self, path: str) -> None:
        self.results_page.refresh_image(path)
        self.statusBar().showMessage(f"Saved changes to {os.path.basename(path)}")

    def _image_deleted(self, path: str) -> None:
        self.results_page.remove_image(path)
        self.statusBar().showMessage(f"Deleted {os.path.basename(path)}")

    def _open_output(self) -> None:
        folder = (
            self._config.output_folder
            if self._config is not None
            else self.setup_page.config().output_folder
        )
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            QMessageBox.information(
                self, "Output folder", "The output folder does not exist yet."
            )

    # ---------------------------------------------------------------- closing
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.stack.currentWidget() is self.postprocess_page:
            if not self.postprocess_page.can_leave():
                event.ignore()
                return
        if self._worker is not None and self._worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Recovery in progress",
                "A recovery is still running. Cancel it and close the application?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.cancel()
            if not self._worker.wait(15000):
                QMessageBox.information(
                    self,
                    "Still busy",
                    "The current file could not be interrupted yet. Close the "
                    "window again when it has finished.",
                )
                event.ignore()
                return
        self.setup_page.save_settings()
        QSettings().setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)

    def _restore_window(self) -> None:
        geometry = QSettings().value("window/geometry")
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except TypeError:
                pass

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {__app_name__}",
            f"<h3>{__app_name__} {__version__}</h3>"
            "<p>An independent, open reimplementation of a JPEG anti-ransomware "
            "recovery workflow.</p>"
            "<p>It rebuilds JPEG files from the surviving compressed data.  A "
            "sample from the same camera gives exact headers; without one, the "
            "sampling layout and row width are inferred from the scan using "
            "standard JPEG tables.  The first encrypted MCU rows are lost; the "
            "rest of the image is preserved without re-encoding.</p>"
            "<p>Auto-adjust colors repairs the lost DC predictor with a "
            "lossless DC shift, and the Post-process page offers standard "
            "quality presets for the surviving coefficients.</p>"
            "<p>This project is not affiliated with, endorsed by, or licensed "
            "from the original product.</p>"
            f"<p style='color:{MUTED}'>PyQt6 · Python</p>",
        )


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication(argv or [])
    app.setOrganizationName("JPEGRecovery")
    app.setApplicationName("JPEG Recovery")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    return app
