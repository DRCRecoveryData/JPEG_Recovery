"""Results page: batch progress, log and recovered-image preview."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.recovery import FileResult, RecoveredImage
from .theme import DANGER, MUTED, SUCCESS
from .widgets import Card, ImagePreview, StatTile, ThumbnailStrip, section_title


class ResultsPage(QWidget):
    """Shows what the recovery engine found, file by file."""

    back_requested = pyqtSignal()
    continue_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    open_output_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._results: list[FileResult] = []
        self._row_results: dict[int, FileResult] = {}
        self._images: list[RecoveredImage] = []
        self._running = False
        self._output_folder = ""
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Recovery results")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Recovered files are valid JPEGs. Select one to preview it, then "
            "continue to fine-tune its quality."
        )
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        heading.addLayout(title_box)
        heading.addStretch(1)

        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.clicked.connect(self.open_output_requested.emit)
        self.open_output_button.setEnabled(False)
        heading.addWidget(self.open_output_button, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(heading)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_scanned = StatTile("Files scanned", "0")
        self.tile_recovered = StatTile("Files recovered", "0")
        self.tile_failed = StatTile("Failed", "0")
        self.tile_images = StatTile("Images written", "0")
        for tile in (
            self.tile_scanned,
            self.tile_recovered,
            self.tile_failed,
            self.tile_images,
        ):
            tiles.addWidget(tile)
        layout.addLayout(tiles)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        left = QSplitter(Qt.Orientation.Vertical)
        left.setChildrenCollapsible(False)
        splitter.addWidget(left)

        table_card = Card()
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(14, 12, 14, 13)
        table_layout.setSpacing(8)
        table_layout.addWidget(
            section_title("Processed files", "Double-click a recovered file to open it.")
        )
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["File", "Status", "Dimensions", "Header", "Prefix", "Removed rows"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(self._open_row)
        table_layout.addWidget(self.table, 1)
        left.addWidget(table_card)

        log_card = Card()
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 13)
        log_layout.setSpacing(8)
        log_header = QHBoxLayout()
        log_header.addWidget(section_title("Log"))
        log_header.addStretch(1)
        copy_button = QPushButton("Copy log")
        copy_button.clicked.connect(self._copy_log)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(lambda: self.log_view.clear())
        log_header.addWidget(copy_button)
        log_header.addWidget(clear_button)
        log_layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self.log_view.setMinimumHeight(90)
        log_layout.addWidget(self.log_view, 1)
        left.addWidget(log_card)
        left.setSizes([440, 170])

        preview_card = Card()
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(14, 12, 14, 13)
        preview_layout.setSpacing(8)
        preview_layout.addWidget(
            section_title("Preview", "The top rows are missing from every recovered image.")
        )
        self.preview = ImagePreview()
        self.preview.set_empty_text("Select a recovered file to see its preview")
        self.preview.setMinimumHeight(260)
        preview_layout.addWidget(self.preview, 1)

        self.thumbnails = ThumbnailStrip()
        self.thumbnails.image_selected.connect(self._show_path)
        self.thumbnails.hide()
        preview_layout.addWidget(self.thumbnails)

        self.details = QLabel("")
        self.details.setObjectName("Hint")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        preview_layout.addWidget(self.details)

        open_row = QHBoxLayout()
        open_row.addStretch(1)
        self.open_button = QPushButton("Open image")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_current)
        open_row.addWidget(self.open_button)
        preview_layout.addLayout(open_row)
        splitter.addWidget(preview_card)
        splitter.setSizes([740, 430])

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.back_button = QPushButton("← Back to setup")
        self.back_button.clicked.connect(self.back_requested.emit)
        bottom.addWidget(self.back_button)

        status_box = QVBoxLayout()
        status_box.setSpacing(4)
        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("Hint")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(7)
        self.progress.setVisible(False)
        status_box.addWidget(self.status_label)
        status_box.addWidget(self.progress)
        bottom.addLayout(status_box, 1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel)
        bottom.addWidget(self.cancel_button)

        self.continue_button = QPushButton("Continue to post-process →")
        self.continue_button.setObjectName("Primary")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.continue_requested.emit)
        bottom.addWidget(self.continue_button)
        layout.addLayout(bottom)

    # ------------------------------------------------------------- lifecycle
    def start_run(self, total: int) -> None:
        self._results.clear()
        self._row_results.clear()
        self._images.clear()
        self.table.setRowCount(0)
        self.log_view.clear()
        self.thumbnails.clear()
        self.thumbnails.hide()
        self.preview.clear()
        self.details.clear()
        self.open_button.setEnabled(False)
        self.open_output_button.setEnabled(False)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status_label.setText(
            f"Recovering 0 of {total} file{'s' if total != 1 else ''}…"
        )
        self.tile_scanned.set_value("0")
        self.tile_recovered.set_value("0")
        self.tile_failed.set_value("0")
        self.tile_images.set_value("0")
        self._running = True
        self.back_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.continue_button.setEnabled(False)
        self.setCursor(Qt.CursorShape.BusyCursor)

    def handle_result(self, result: FileResult, elapsed: float) -> None:
        self._results.append(result)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_results[row] = result

        name_item = QTableWidgetItem(result.source_name)
        name_item.setToolTip(result.source)
        self.table.setItem(row, 0, name_item)

        status_text = "Recovered" if result.ok else "Failed"
        status_item = QTableWidgetItem(status_text)
        status_item.setForeground(
            QColor(SUCCESS if result.ok else DANGER)
        )
        status_item.setToolTip(result.message)
        font = status_item.font()
        font.setBold(True)
        status_item.setFont(font)
        self.table.setItem(row, 1, status_item)

        if result.images:
            sizes = sorted({(i.width, i.height) for i in result.images})
            dims = ", ".join(f"{w}×{h}" for w, h in sizes)
            sample = result.images[0].sample
            if result.images[0].sampleless:
                sample = "Inferred"
            prefix = str(result.images[0].prefix)
            skipped = ", ".join(
                f"~{i.skipped_rows}" if i.estimated_rows else str(i.skipped_rows)
                for i in result.images
            )
        else:
            dims = "—"
            sample = "—"
            prefix = "—"
            skipped = "—"
        for column, text in enumerate(
            (dims, sample, prefix, skipped), start=2
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, column, item)

        self._update_stats()
        if len(self._results) == 1 and result.images:
            self.table.selectRow(0)

    def finish_run(self, recovered: int, failed: int, cancelled: bool) -> None:
        self._running = False
        self.cancel_button.setEnabled(False)
        self.cancel_button.setVisible(False)
        self.back_button.setEnabled(True)
        self.unsetCursor()
        total = len(self._results)
        self.progress.setVisible(False)
        if total:
            self.tile_scanned.set_value(total)
        self._update_stats()
        image_count = len(self._images)
        if cancelled:
            self.status_label.setText(
                f"Cancelled after {total} file{'s' if total != 1 else ''}."
            )
        elif image_count:
            self.status_label.setText(
                f"Recovered {image_count} image"
                f"{'s' if image_count != 1 else ''} from {recovered} "
                f"file{'s' if recovered != 1 else ''}."
            )
        else:
            self.status_label.setText(
                "No readable images were produced. Check the log and the "
                "encrypted prefix or sample settings."
            )
        self.continue_button.setEnabled(bool(image_count))
        self.open_output_button.setEnabled(os.path.isdir(self._output_folder))
        if image_count:
            self.table.selectRow(0)

    def set_output_folder(self, path: str) -> None:
        self._output_folder = path
        self.open_output_button.setEnabled(False)

    def set_current_file(self, index: int, total: int, name: str) -> None:
        if self._running:
            self.progress.setValue(max(0, index))
            self.status_label.setText(
                f"Recovering {min(index + 1, total)} of {total}: {name}"
            )

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # ------------------------------------------------------------------ data
    @property
    def images(self) -> list[RecoveredImage]:
        return list(self._images)

    @property
    def results(self) -> list[FileResult]:
        return list(self._results)

    def _update_stats(self) -> None:
        self._images = [
            image for result in self._results for image in result.images
        ]
        recovered = sum(1 for result in self._results if result.ok)
        failed = sum(1 for result in self._results if not result.ok)
        self.tile_scanned.set_value(len(self._results))
        self.tile_recovered.set_value(recovered)
        self.tile_failed.set_value(failed)
        self.tile_images.set_value(len(self._images))
        self.continue_button.setEnabled(bool(self._images) and not self._running)

    # -------------------------------------------------------------- selection
    def _selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return
        result = self._row_results.get(rows[0].row())
        if result is None:
            return
        self.thumbnails.clear()
        if result.images:
            self.thumbnails.show()
            for index, image in enumerate(result.images, 1):
                label = os.path.basename(image.path)
                if len(result.images) > 1:
                    label = f"{index}. {label}"
                item = self.thumbnails.add_image(image.path, label)
                item.setToolTip(image.path)
            self.thumbnails.setCurrentRow(0)
            self._show_image(result.images[0])
        else:
            self.thumbnails.hide()
            self.preview.clear()
            self.preview.set_empty_text("No readable image")
            self.details.setText(result.message)
            self.open_button.setEnabled(False)

    def _show_image(self, image: RecoveredImage) -> None:
        self.preview.set_image_path(image.path)
        if image.sampleless:
            header = f"inferred {image.sampling} · q{image.quality}"
            removed = (
                f"About {image.skipped_rows} MCU row"
                f"{'s' if image.skipped_rows != 1 else ''} removed (estimated)"
            )
        else:
            header = f"sample: {image.sample}"
            removed = (
                f"First {image.skipped_rows} MCU row"
                f"{'s' if image.skipped_rows != 1 else ''} removed"
            )
        lines = [
            f"<b>{os.path.basename(image.path)}</b>",
            f"{image.width} × {image.height} px · {header} · "
            f"encrypted prefix: {image.prefix} bytes",
            removed + (" · colours auto-adjusted" if image.adjusted else ""),
        ]
        self.details.setText("<br>".join(lines))
        self.details.setToolTip(image.path)
        self.open_button.setEnabled(True)

    def _show_path(self, path: str) -> None:
        for image in self._images:
            if os.path.normcase(image.path) == os.path.normcase(path):
                self._show_image(image)
                return

    def _current_image(self) -> RecoveredImage | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        result = self._row_results.get(rows[0].row())
        if not result or not result.images:
            return None
        current = self.thumbnails.currentItem()
        if current is not None:
            path = current.data(Qt.ItemDataRole.UserRole)
            for image in result.images:
                if os.path.normcase(image.path) == os.path.normcase(path):
                    return image
        return result.images[0]

    def _open_current(self) -> None:
        image = self._current_image()
        if image:
            QDesktopServices.openUrl(QUrl.fromLocalFile(image.path))

    def _open_row(self, row: int, _column: int) -> None:
        result = self._row_results.get(row)
        if result and result.images:
            QDesktopServices.openUrl(QUrl.fromLocalFile(result.images[0].path))

    def remove_image(self, path: str) -> None:
        """Reflect a file deleted on the post-process page."""
        target = os.path.normcase(path)
        for result in self._results:
            before = len(result.images)
            result.images = [
                image
                for image in result.images
                if os.path.normcase(image.path) != target
            ]
            if len(result.images) != before:
                if not result.images:
                    result.ok = False
                    result.message = "Recovered file was deleted"
                self._refresh_result_row(result)
        self._update_stats()
        self.thumbnails.remove_path(path)
        if self.thumbnails.count() == 0 and self.table.currentRow() >= 0:
            result = self._row_results.get(self.table.currentRow())
            if result and not result.images:
                self.preview.clear()
                self.preview.set_empty_text("No readable image")
                self.details.setText(result.message)
                self.open_button.setEnabled(False)
        self.continue_button.setEnabled(bool(self._images) and not self._running)

    def refresh_image(self, path: str) -> None:
        """Reload a preview after the post-process page saved changes."""
        target = os.path.normcase(path)
        for index in range(self.thumbnails.count()):
            item = self.thumbnails.item(index)
            if os.path.normcase(item.data(Qt.ItemDataRole.UserRole)) == target:
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    from PyQt6.QtGui import QIcon

                    item.setIcon(
                        QIcon(
                            pixmap.scaled(
                                104,
                                72,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        )
                    )
                break
        if self._current_image_path() == target:
            for image in self._images:
                if os.path.normcase(image.path) == target:
                    self.preview.set_image_path(path)
                    self._show_image(image)
                    break

    def _current_image_path(self) -> str:
        current = self.thumbnails.currentItem()
        if current is None:
            return ""
        return os.path.normcase(current.data(Qt.ItemDataRole.UserRole) or "")

    def _refresh_result_row(self, result: FileResult) -> None:
        row = None
        for index, candidate in self._row_results.items():
            if candidate is result:
                row = index
                break
        if row is None:
            return
        if result.images:
            sizes = sorted({(i.width, i.height) for i in result.images})
            dims = ", ".join(f"{w}×{h}" for w, h in sizes)
            sample = result.images[0].sample
            if result.images[0].sampleless:
                sample = "Inferred"
            prefix = str(result.images[0].prefix)
            skipped = ", ".join(
                f"~{i.skipped_rows}" if i.estimated_rows else str(i.skipped_rows)
                for i in result.images
            )
            status_text = "Recovered"
            color = SUCCESS
        else:
            dims = "—"
            sample = "—"
            prefix = "—"
            skipped = "—"
            status_text = "Deleted"
            color = MUTED
        self.table.item(row, 1).setText(status_text)
        self.table.item(row, 1).setForeground(QColor(color))
        for column, text in enumerate((dims, sample, prefix, skipped), start=2):
            item = self.table.item(row, column)
            if item is not None:
                item.setText(text)

    def _copy_log(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.log_view.toPlainText())

    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling after the current file…")
        self.cancel_requested.emit()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.open_output_button.setEnabled(os.path.isdir(self._output_folder))
