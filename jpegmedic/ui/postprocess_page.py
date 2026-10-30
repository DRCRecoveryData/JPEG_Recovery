"""Post-process page: adjust quantization tables and preview the result."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from PyQt6.QtCore import QEventLoop, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.colorfix import auto_adjust_colors
from ..core.jpeg import (
    ZIGZAG,
    JpegError,
    JpegHeader,
    parse_jpeg,
    quant_tables_used_by_components,
    replace_quant_tables,
)
from ..core.recovery import RecoveredImage
from ..core.sampleless import standard_quant_tables
from .theme import DANGER, SUCCESS
from .widgets import Card, ImagePreview, ThumbnailStrip, section_title

# Zig-zag position for every natural 8×8 index.
ZIGZAG_POSITION = [0] * 64
for _position, _natural in enumerate(ZIGZAG):
    ZIGZAG_POSITION[_natural] = _position


class PostProcessPage(QWidget):
    """Edit the DQT values of recovered files in place."""

    back_requested = pyqtSignal()
    done_requested = pyqtSignal()
    image_saved = pyqtSignal(str)
    image_deleted = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._images: list[RecoveredImage] = []
        self._current: RecoveredImage | None = None
        self._saved_data: bytes = b""
        self._saved_tables: dict[int, list[int]] = {}
        self._tables: dict[int, list[int]] = {}
        self._header: JpegHeader | None = None
        self._dirty = False
        self._loading = False
        self._output_folder = ""
        self._spins: dict[tuple[int, int], QSpinBox] = {}
        self._sliders: dict[int, tuple[QSlider, QLabel]] = {}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(140)
        self._preview_timer.timeout.connect(self._render_preview)
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Post-process recovered images")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Fine-tune brightness and contrast without re-encoding the JPEG."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        heading.addLayout(title_box, 1)
        heading.addStretch(1)
        self.open_folder_button = QPushButton("Open output folder")
        self.open_folder_button.clicked.connect(self._open_output_folder)
        heading.addWidget(self.open_folder_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        preview_card = Card()
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(14, 12, 14, 13)
        preview_layout.setSpacing(8)
        preview_layout.addWidget(
            section_title(
                "Preview",
                "Changes are previewed from the saved JPEG data; press Save to "
                "write them to the file.",
            )
        )
        self.preview = ImagePreview()
        self.preview.set_empty_text("No recovered image to edit")
        self.preview.setMinimumHeight(280)
        preview_layout.addWidget(self.preview, 1)

        self.thumbnails = ThumbnailStrip()
        self.thumbnails.image_selected.connect(self._thumbnail_selected)
        preview_layout.addWidget(self.thumbnails)

        self.details = QLabel("")
        self.details.setObjectName("Hint")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        preview_layout.addWidget(self.details)

        navigation = QHBoxLayout()
        navigation.setSpacing(6)
        self.previous_button = QPushButton("← Previous")
        self.previous_button.clicked.connect(lambda: self._step(-1))
        self.next_button = QPushButton("Next →")
        self.next_button.clicked.connect(lambda: self._step(1))
        self.dirty_label = QLabel("Up to date")
        self.dirty_label.setObjectName("Hint")
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        navigation.addStretch(1)
        navigation.addWidget(self.dirty_label)
        preview_layout.addLayout(navigation)
        splitter.addWidget(preview_card)

        editor_card = Card()
        editor_layout = QVBoxLayout(editor_card)
        editor_layout.setContentsMargins(14, 12, 14, 13)
        editor_layout.setSpacing(8)
        editor_layout.addWidget(
            section_title(
                "Colour & quantization",
                "Auto-adjust repairs the lost DC predictor without touching "
                "image detail.  Quality presets replace both tables with "
                "standard IJG tables.",
            )
        )

        color_row = QHBoxLayout()
        color_row.setSpacing(7)
        self.auto_colors_button = QPushButton("Auto-adjust colors")
        self.auto_colors_button.setToolTip(
            "Correct the brightness and colour cast caused by the encrypted "
            "first part of the JPEG.  Only DC coefficients are rewritten; the "
            "photo's detail and quantization tables stay untouched."
        )
        self.auto_colors_button.clicked.connect(self._auto_adjust_colors)
        color_note = QLabel("Fixes clipping and casts from the lost DC predictor.")
        color_note.setObjectName("Hint")
        color_note.setWordWrap(True)
        color_row.addWidget(self.auto_colors_button)
        color_row.addWidget(color_note, 1)
        editor_layout.addLayout(color_row)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(7)
        quality_label = QLabel("Standard quality")
        quality_label.setObjectName("Hint")
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Keep current", None)
        for quality in (60, 70, 75, 80, 85, 90, 95, 100):
            self.quality_combo.addItem(f"q{quality}", quality)
        self.quality_combo.setToolTip(
            "Replace the quantization tables with standard IJG tables.  This "
            "changes brightness, contrast and colour rendition immediately; "
            "the compressed image data is not re-encoded.  q100 is near-lossless "
            "and best for detailed photos (larger file)."
        )
        self.apply_quality_button = QPushButton("Apply")
        self.apply_quality_button.clicked.connect(self._apply_quality_preset)
        quality_row.addWidget(quality_label)
        quality_row.addWidget(self.quality_combo, 1)
        quality_row.addWidget(self.apply_quality_button)
        editor_layout.addLayout(quality_row)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        editor_layout.addWidget(self.tabs, 1)
        self.editor_hint = QLabel(
            "Tip: the scale slider adjusts every value in the active table.  "
            "Use Reset to return to the baseline used by the sliders."
        )
        self.editor_hint.setObjectName("Hint")
        self.editor_hint.setWordWrap(True)
        editor_layout.addWidget(self.editor_hint)
        splitter.addWidget(editor_card)
        splitter.setSizes([700, 480])

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.back_button = QPushButton("← Back to results")
        self.back_button.clicked.connect(self._try_back)
        bottom.addWidget(self.back_button)
        bottom.addStretch(1)

        self.delete_button = QPushButton("Delete image")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete_current)
        bottom.addWidget(self.delete_button)

        self.copy_button = QPushButton("Save a copy…")
        self.copy_button.clicked.connect(self._save_copy)
        bottom.addWidget(self.copy_button)

        self.save_button = QPushButton("Save changes")
        self.save_button.clicked.connect(self._save)
        bottom.addWidget(self.save_button)

        self.done_button = QPushButton("Done")
        self.done_button.setObjectName("Primary")
        self.done_button.clicked.connect(self._try_done)
        bottom.addWidget(self.done_button)
        layout.addLayout(bottom)

    # ------------------------------------------------------------- image state
    def set_images(self, images: list[RecoveredImage], output_folder: str = "") -> None:
        self._images = list(images)
        self._output_folder = output_folder
        self.open_folder_button.setEnabled(os.path.isdir(output_folder))
        self.thumbnails.clear()
        for index, image in enumerate(self._images, 1):
            item = self.thumbnails.add_image(
                image.path, f"{index}. {os.path.basename(image.path)}"
            )
            item.setToolTip(image.path)
        self._dirty = False
        preferred = self._current.path if self._current is not None else None
        self._current = None
        targets = [i for i in self._images if i.path == preferred]
        if targets:
            self._load_image(targets[0])
        elif self._images:
            self._load_image(self._images[0])
        else:
            self._clear_view()

    def _clear_view(self) -> None:
        self.preview.clear()
        self.preview.set_empty_text("No recovered image to edit")
        self.details.setText("")
        self.tabs.clear()
        self.spins_clear()
        self._current = None
        self._header = None
        self._saved_data = b""
        self._saved_tables.clear()
        self._tables.clear()
        self._reset_quality_combo()
        self._set_dirty(False)
        self._update_buttons()
        self.dirty_label.setText("No image")

    def spins_clear(self) -> None:
        self._spins.clear()

    def _reset_quality_combo(self) -> None:
        if self.quality_combo.count():
            was_blocked = self.quality_combo.blockSignals(True)
            self.quality_combo.setCurrentIndex(0)
            self.quality_combo.blockSignals(was_blocked)

    def _update_buttons(self) -> None:
        has = self._current is not None
        self.save_button.setEnabled(has and self._dirty)
        self.copy_button.setEnabled(has)
        self.delete_button.setEnabled(has)
        self.auto_colors_button.setEnabled(has)
        self.apply_quality_button.setEnabled(has)
        self.quality_combo.setEnabled(has)
        self.previous_button.setEnabled(has and len(self._images) > 1)
        self.next_button.setEnabled(has and len(self._images) > 1)
        self.done_button.setEnabled(True)

    def _load_image(self, image: RecoveredImage) -> None:
        try:
            data = Path(image.path).read_bytes()
            header = parse_jpeg(data)
        except (OSError, JpegError) as exc:
            QMessageBox.warning(
                self,
                "Cannot open recovered image",
                f"{image.path}\n\n{exc}",
            )
            self._remove_from_list(image.path)
            self._clear_view()
            return
        self._current = image
        self._saved_data = data
        self._header = header
        self._tables = {k: list(v) for k, v in header.quant_tables.items()}
        self._saved_tables = copy.deepcopy(self._tables)
        self._reset_quality_combo()
        self._set_dirty(False)
        self._build_tabs()
        self.tabs.setEnabled(True)
        self.preview.set_empty_text("No recovered image to edit")
        self.preview.set_image_path(image.path)
        self._update_details()
        self._update_buttons()
        self.thumbnails.select_path(image.path)

    def _build_tabs(self) -> None:
        self.tabs.clear()
        self._spins.clear()
        self._sliders.clear()
        if self._header is None:
            return
        roles = quant_tables_used_by_components(self._header)
        role_names: dict[str, list[str]] = {}
        for role, ids in roles.items():
            for table_id in ids:
                role_names.setdefault(table_id, []).append(role)

        for table_id in sorted(self._tables):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(10, 10, 10, 8)
            page_layout.setSpacing(8)

            controls = QHBoxLayout()
            controls.setSpacing(7)
            scale_label = QLabel("Scale")
            scale_label.setObjectName("Hint")
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(20, 300)
            slider.setValue(100)
            slider.setMinimumWidth(120)
            value_label = QLabel("100%")
            value_label.setObjectName("Hint")
            value_label.setFixedWidth(38)
            reset = QPushButton("Reset")
            reset.setToolTip("Restore the values saved in the file")
            controls.addWidget(scale_label)
            controls.addWidget(slider, 1)
            controls.addWidget(value_label)
            controls.addWidget(reset)
            page_layout.addLayout(controls)

            grid = QGridLayout()
            grid.setContentsMargins(0, 2, 0, 0)
            grid.setSpacing(3)
            for natural in range(64):
                spin = QSpinBox()
                spin.setRange(1, 255)
                spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
                spin.setFixedSize(43, 27)
                spin.setStyleSheet(
                    "QSpinBox { padding: 1px; border-radius: 4px; }"
                )
                position = ZIGZAG_POSITION[natural]
                spin.setValue(self._tables[table_id][position])
                spin.valueChanged.connect(
                    lambda value, tid=table_id, pos=position: self._cell_changed(
                        tid, pos, value
                    )
                )
                self._spins[(table_id, natural)] = spin
                grid.addWidget(spin, natural // 8, natural % 8)
            page_layout.addLayout(grid)
            page_layout.addStretch(1)

            role_text = self._role_text(role_names.get(table_id, []), table_id)
            note = QLabel(
                f"{role_text} · cells are shown in their usual 8×8 spatial "
                "layout.  Only the JPEG header is rewritten; the compressed "
                "coefficients stay untouched."
            )
            note.setObjectName("Hint")
            note.setWordWrap(True)
            page_layout.addWidget(note)
            self._sliders[table_id] = (slider, value_label)

            slider.valueChanged.connect(
                lambda value, tid=table_id, label=value_label: self._scale_table(
                    tid, value, label
                )
            )
            reset.clicked.connect(
                lambda _=False, tid=table_id, s=slider, lbl=value_label: self._reset_table(
                    tid, s, lbl
                )
            )
            self.tabs.addTab(page, role_text)

    @staticmethod
    def _role_text(roles: list[str], table_id: int) -> str:
        names = {"Y": "Luma", "Cb": "Blue chroma", "Cr": "Red chroma", "A": "Alpha"}
        if "Y" in roles and ("Cb" in roles or "Cr" in roles):
            label = "All colours"
        elif "Y" in roles:
            label = "Luma"
        elif "Cb" in roles and "Cr" in roles:
            label = "Chrominance"
        elif roles:
            label = names.get(roles[0], "Table")
        else:
            label = "Table"
        return f"{label} · DQT {table_id}"

    def _cell_changed(self, table_id: int, position: int, value: int) -> None:
        if self._loading or table_id not in self._tables:
            return
        self._tables[table_id][position] = value
        self._set_dirty(True)
        self._schedule_preview()

    def _scale_table(self, table_id: int, percent: int, label: QLabel) -> None:
        if self._loading:
            return
        label.setText(f"{percent}%")
        reference = self._saved_tables.get(table_id)
        if reference is None:
            return
        factor = percent / 100.0
        scaled = [max(1, min(255, round(v * factor))) for v in reference]
        self._loading = True
        try:
            self._tables[table_id] = scaled
            for natural in range(64):
                spin = self._spins.get((table_id, natural))
                if spin is not None:
                    spin.setValue(scaled[ZIGZAG_POSITION[natural]])
        finally:
            self._loading = False
        self._set_dirty(True)
        self._schedule_preview()

    def _reset_table(self, table_id: int, slider: QSlider, label: QLabel) -> None:
        if table_id not in self._saved_tables:
            return
        values = list(self._saved_tables[table_id])
        self._tables[table_id] = values
        was_blocked = slider.blockSignals(True)
        slider.setValue(100)
        slider.blockSignals(was_blocked)
        label.setText("100%")
        self._loading = True
        try:
            for natural in range(64):
                spin = self._spins.get((table_id, natural))
                if spin is not None:
                    spin.setValue(values[ZIGZAG_POSITION[natural]])
        finally:
            self._loading = False
        self._set_dirty(True)
        self._schedule_preview()

    def _reset_sliders(self) -> None:
        for slider, label in self._sliders.values():
            was_blocked = slider.blockSignals(True)
            slider.setValue(100)
            slider.blockSignals(was_blocked)
            label.setText("100%")

    def _apply_quality_preset(self) -> None:
        if self._current is None or self._header is None:
            return
        quality = self.quality_combo.currentData()
        if quality is None:
            return
        standard = standard_quant_tables(int(quality))
        roles = quant_tables_used_by_components(self._header)
        assigned: dict[int, tuple[int, ...]] = {}
        for role in ("Y", "Cb", "Cr", "A"):
            source = standard[0] if role == "Y" else standard[1]
            for table_id in roles.get(role, []):
                assigned.setdefault(table_id, source)
        if not assigned:
            QMessageBox.information(
                self,
                "Cannot apply quality",
                "This image does not expose a usable quantization table mapping.",
            )
            return
        self._tables = copy.deepcopy(self._tables)
        for table_id, values in assigned.items():
            self._tables[table_id] = list(values)
        # The sliders scale relative to the table currently shown.  Make the
        # preset that baseline so moving a slider does not undo the preset.
        self._saved_tables = copy.deepcopy(self._tables)
        self._loading = True
        try:
            for table_id, values in self._tables.items():
                for natural in range(64):
                    spin = self._spins.get((table_id, natural))
                    if spin is not None:
                        spin.setValue(values[ZIGZAG_POSITION[natural]])
        finally:
            self._loading = False
        self._reset_sliders()
        self._set_dirty(True)
        self.dirty_label.setText(f"Standard q{quality} tables · unsaved")
        self._schedule_preview()

    def _auto_adjust_colors(self) -> None:
        if self._current is None or not self._saved_data:
            return
        self.auto_colors_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.dirty_label.setText("Analysing colours…")
        self.dirty_label.setStyleSheet("")
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        )
        error: Exception | None = None
        adjusted = b""
        changed = False
        try:
            payload = replace_quant_tables(
                self._saved_data,
                {k: tuple(v) for k, v in self._tables.items()},
            )
            adjusted, changed = auto_adjust_colors(payload)
        except (JpegError, OSError, ValueError) as exc:
            error = exc
        finally:
            QApplication.restoreOverrideCursor()
            self.auto_colors_button.setEnabled(self._current is not None)
        if error is not None:
            self._set_dirty(self._dirty)
            QMessageBox.warning(
                self,
                "Cannot auto-adjust colours",
                f"{self._current.path}\n\n{error}",
            )
            return
        if not changed:
            self._render_preview()
            if self._dirty:
                self.dirty_label.setText(
                    "No colour correction needed · unsaved changes"
                )
            else:
                self.dirty_label.setText("Colours already balanced")
            self.dirty_label.setStyleSheet(f"color: {SUCCESS}; font-weight: 600;")
            return
        try:
            header = parse_jpeg(adjusted)
        except JpegError as exc:
            QMessageBox.warning(
                self, "Cannot auto-adjust colours", str(exc)
            )
            return
        self._saved_data = adjusted
        self._header = header
        self._tables = {k: list(v) for k, v in header.quant_tables.items()}
        self._saved_tables = copy.deepcopy(self._tables)
        self._reset_quality_combo()
        self._build_tabs()
        self._current.adjusted = True
        self._set_dirty(True)
        self._update_details()
        self._render_preview()
        self.dirty_label.setText("Colours adjusted · unsaved")

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.save_button.setEnabled(self._current is not None and dirty)
        if self._current is None:
            self.dirty_label.setText("No image")
            self.dirty_label.setStyleSheet("")
            return
        if dirty:
            self.dirty_label.setText("Unsaved changes")
            self.dirty_label.setStyleSheet(f"color: {DANGER}; font-weight: 600;")
        else:
            self.dirty_label.setText("Up to date")
            self.dirty_label.setStyleSheet(f"color: {SUCCESS}; font-weight: 600;")

    def _update_details(self) -> None:
        if self._current is None:
            self.details.setText("")
            return
        image = self._current
        if image.sampleless:
            header = f"header: inferred {image.sampling} · q{image.quality}"
            removed = (
                f"About {image.skipped_rows} MCU row"
                f"{'s' if image.skipped_rows != 1 else ''} removed from the top"
            )
        else:
            header = f"sample: {image.sample}"
            removed = (
                f"Removed {image.skipped_rows} MCU row"
                f"{'s' if image.skipped_rows != 1 else ''} from the top"
            )
        tables_quality = self._standard_tables_quality()
        if tables_quality is not None and tables_quality != image.quality:
            header += f" · tables: standard q{tables_quality}"
        parts = [
            f"<b>{os.path.basename(image.path)}</b>",
            f"{image.width} × {image.height} px · {header} · "
            f"encrypted prefix: {image.prefix} bytes",
            removed + (" · colours auto-adjusted" if image.adjusted else ""),
            image.path,
        ]
        self.details.setText("<br>".join(parts))
        self.details.setToolTip(image.path)

    def _standard_tables_quality(self) -> int | None:
        """Return the standard quality matching the current tables, if any."""
        if self._header is None or not self._tables:
            return None
        roles = quant_tables_used_by_components(self._header)
        if not roles:
            return None
        expected_by_role: dict[str, list[int]] = {}
        for role, table_ids in roles.items():
            if not table_ids:
                continue
            role_tables = [
                list(self._tables.get(table_id, ())) for table_id in table_ids
            ]
            if any(len(values) != 64 for values in role_tables):
                return None
            expected_by_role[role] = role_tables[0]
            if any(values != role_tables[0] for values in role_tables[1:]):
                return None
        if not expected_by_role:
            return None
        for quality in range(1, 101):
            standard = standard_quant_tables(quality)
            if all(
                values == list(standard[0] if role == "Y" else standard[1])
                for role, values in expected_by_role.items()
            ):
                return quality
        return None

    # --------------------------------------------------------------- preview
    def _schedule_preview(self) -> None:
        self._preview_timer.start()

    def _render_preview(self) -> None:
        if self._current is None or not self._saved_data:
            return
        try:
            payload = replace_quant_tables(
                self._saved_data,
                {k: tuple(v) for k, v in self._tables.items()},
            )
        except Exception as exc:  # noqa: BLE001
            self.dirty_label.setText(f"Preview failed: {exc}")
            self.dirty_label.setStyleSheet(f"color: {DANGER};")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(payload):
            return
        self.preview.set_pixmap(pixmap)
        self._update_details()

    # -------------------------------------------------------------- commands
    def _thumbnail_selected(self, path: str) -> None:
        if self._current is not None and os.path.normcase(path) == os.path.normcase(
            self._current.path
        ):
            return
        if not self._confirm_discard():
            self.thumbnails.select_path(self._current.path)
            return
        image = next(
            (
                i
                for i in self._images
                if os.path.normcase(i.path) == os.path.normcase(path)
            ),
            None,
        )
        if image is not None:
            self._load_image(image)

    def _step(self, delta: int) -> None:
        if not self._images or self._current is None:
            return
        paths = [os.path.normcase(i.path) for i in self._images]
        try:
            index = paths.index(os.path.normcase(self._current.path))
        except ValueError:
            index = 0
        new_index = (index + delta) % len(self._images)
        target = self._images[new_index]
        if not self._confirm_discard():
            return
        self._load_image(target)

    def _confirm_discard(self) -> bool:
        if not self._dirty or self._current is None:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"Do you want to save the changes to "
            f"{os.path.basename(self._current.path)}?"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        choice = box.exec()
        if choice == QMessageBox.StandardButton.Save:
            return self._save()
        if choice == QMessageBox.StandardButton.Discard:
            self._set_dirty(False)
            return True
        return False

    def _save(self) -> bool:
        if self._current is None:
            return False
        try:
            payload = replace_quant_tables(
                self._saved_data,
                {k: tuple(v) for k, v in self._tables.items()},
            )
            Path(self._current.path).write_bytes(payload)
        except (OSError, JpegError, ValueError) as exc:
            QMessageBox.warning(
                self, "Cannot save changes", f"{self._current.path}\n\n{exc}"
            )
            return False
        self._saved_data = payload
        self._saved_tables = copy.deepcopy(self._tables)
        self._reset_sliders()
        try:
            self._header = parse_jpeg(payload)
        except JpegError:
            pass
        self._set_dirty(False)
        self.image_saved.emit(self._current.path)
        self._update_details()
        self._render_preview()
        return True

    def _save_copy(self) -> None:
        if self._current is None:
            return
        root, ext = os.path.splitext(self._current.path)
        suggested = f"{root}_edited{ext or '.jpg'}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save a copy", suggested, "JPEG images (*.jpg *.jpeg)"
        )
        if not path:
            return
        try:
            payload = replace_quant_tables(
                self._saved_data,
                {k: tuple(v) for k, v in self._tables.items()},
            )
            Path(path).write_bytes(payload)
        except (OSError, JpegError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot save a copy", str(exc))
            return
        QMessageBox.information(
            self, "Copy saved", f"A copy was written to:\n{path}"
        )

    def _delete_current(self) -> None:
        if self._current is None:
            return
        path = self._current.path
        answer = QMessageBox.question(
            self,
            "Delete recovered image?",
            "This removes the recovered file from disk:\n"
            f"{path}\n\nThe encrypted original is not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(path)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot delete file", str(exc))
            return
        self._remove_from_list(path)
        self.image_deleted.emit(path)
        self._set_dirty(False)
        if self._images:
            self._present_image(self._images[0])
        else:
            self._clear_view()

    def _remove_from_list(self, path: str) -> None:
        target = os.path.normcase(path)
        self._images = [
            image for image in self._images if os.path.normcase(image.path) != target
        ]
        self.thumbnails.remove_path(path)
        if self._current is not None and os.path.normcase(
            self._current.path
        ) == target:
            self._current = None

    def _present_image(self, image: RecoveredImage) -> None:
        self._load_image(image)
        self.thumbnails.select_path(image.path)

    def can_leave(self) -> bool:
        """Ask about unsaved edits before another page takes over."""
        return self._confirm_discard()

    def _try_back(self) -> None:
        if self._confirm_discard():
            self.back_requested.emit()

    def _try_done(self) -> None:
        if self._confirm_discard():
            self.done_requested.emit()

    def _open_output_folder(self) -> None:
        if os.path.isdir(self._output_folder):
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(self._output_folder))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.open_folder_button.setEnabled(os.path.isdir(self._output_folder))
