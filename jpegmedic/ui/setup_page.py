"""Setup page: choose encrypted files, samples and output options."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QSize, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.recovery import (
    DEFAULT_PREFIX,
    RecoveryConfig,
    find_candidates,
    guess_extension,
    prefix_candidates,
    recover_bytes,
)
from ..core.samples import (
    JPEG_EXTENSIONS,
    RAW_EXTENSIONS,
    SampleInfo,
    dedupe_samples,
    load_sample,
    sample_base_extensions,
)
from .widgets import Card, section_title


class SampleList(QListWidget):
    """Sample list with a compact initial height.

    The default QListWidget size hint is tall enough to push the setup page
    below an 800-pixel window.  The list still expands when the window grows.
    """

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(hint.width(), 110)


class SetupPage(QWidget):
    """The first workflow page.

    The page owns only the form state.  Running the recovery is the main
    window's job, which keeps the widget usable and testable in isolation.
    """

    start_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._samples: list[SampleInfo] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("Page")
        scroll.setWidget(content)
        page = QVBoxLayout(content)
        page.setContentsMargins(28, 18, 28, 18)
        page.setSpacing(12)

        heading = QVBoxLayout()
        heading.setSpacing(3)
        title = QLabel("Recover encrypted JPEG photos")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Point the application at the encrypted files and choose where to save "
            "the recovered images.  Samples are optional: without one, the "
            "geometry is inferred from the surviving scan."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        page.addLayout(heading)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        page.addLayout(columns, 1)

        left = QVBoxLayout()
        left.setSpacing(16)
        right = QVBoxLayout()
        right.setSpacing(16)
        columns.addLayout(left, 3)
        columns.addLayout(right, 2)

        left.addWidget(self._build_source_card())
        left.addWidget(self._build_samples_card(), 1)

        right.addWidget(self._build_output_card())
        right.addWidget(self._build_options_card())
        right.addWidget(self._build_start_card())
        right.addStretch(1)

        footer = QLabel(
            "Tip: recovered files are written next to your originals, never "
            "over them.  Keep a backup of the encrypted set until you are happy "
            "with the results."
        )
        footer.setObjectName("Hint")
        footer.setWordWrap(True)
        page.addWidget(footer)

        self._load_settings()

    # ------------------------------------------------------------------ UI
    def _build_source_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(12)
        layout.addWidget(
            section_title(
                "1. Encrypted files",
                "Select the folder containing the files damaged by ransomware.",
            )
        )

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        label = QLabel("Folder")
        label.setMinimumWidth(108)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(r"C:\Photos\Encrypted")
        self.source_edit.textChanged.connect(self._source_changed)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_source)
        grid.addWidget(label, 0, 0)
        grid.addWidget(self.source_edit, 0, 1)
        grid.addWidget(browse, 0, 2)

        label = QLabel("File extension")
        self.extension_combo = QComboBox()
        self.extension_combo.setEditable(True)
        self.extension_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.extension_combo.addItem("")
        for ext in (".abc", ".enc", ".encrypted", ".lock", ".crypt", ".ransom", ".aes"):
            self.extension_combo.addItem(ext)
        self.extension_combo.setCurrentText(".abc")
        self.extension_combo.setToolTip(
            "Extension added by the ransomware. Leave empty if no extension "
            "was appended."
        )
        auto_ext = QPushButton("Auto")
        auto_ext.setToolTip("Detect the most common encrypted-file extension")
        auto_ext.clicked.connect(self._detect_extension)
        grid.addWidget(label, 1, 0)
        grid.addWidget(self.extension_combo, 1, 1)
        grid.addWidget(auto_ext, 1, 2)

        label = QLabel("Encrypted prefix")
        self.prefix_combo = QComboBox()
        self.prefix_combo.setEditable(True)
        self.prefix_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.prefix_combo.addItem("Auto")
        for value in (DEFAULT_PREFIX, 65536, 131072, 262144, 524288):
            self.prefix_combo.addItem(str(value))
        self.prefix_combo.setCurrentText(str(DEFAULT_PREFIX))
        self.prefix_combo.setToolTip(
            "Number of bytes encrypted at the start of each file. Choose Auto "
            "to try common values, or detect it with a sample loaded."
        )
        auto_prefix = QPushButton("Detect…")
        auto_prefix.setToolTip("Try common prefix sizes against the loaded samples")
        auto_prefix.clicked.connect(self._detect_prefix)
        grid.addWidget(label, 2, 0)
        grid.addWidget(self.prefix_combo, 2, 1)
        grid.addWidget(auto_prefix, 2, 2)

        self.encrypted_names = QCheckBox(
            "File names are encrypted (files are named like 8f3c….abc)"
        )
        self.encrypted_names.toggled.connect(self._filenames_toggled)
        grid.addWidget(self.encrypted_names, 3, 1, 1, 2)

        layout.addLayout(grid)

        self.source_hint = QLabel("")
        self.source_hint.setObjectName("Hint")
        self.source_hint.setWordWrap(True)
        layout.addWidget(self.source_hint)
        return card

    def _build_samples_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(
            section_title(
                "2. Sample photos (optional)",
                "A readable photo from the same camera gives the most accurate "
                "colour and dimensions.  Without one, the application infers both "
                "from the surviving scan.",
            )
        )

        self.sample_list = SampleList()
        self.sample_list.setMinimumHeight(110)
        self.sample_list.setIconSize(QSize(56, 42))
        self.sample_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.sample_list.setAlternatingRowColors(False)
        self.sample_list.setToolTip("Double-click to open a sample")
        self.sample_list.itemDoubleClicked.connect(self._open_sample)
        layout.addWidget(self.sample_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        add_files = QPushButton("Add files…")
        add_files.clicked.connect(self._add_sample_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self._add_sample_folder)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_samples)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_samples)
        buttons.addWidget(add_files)
        buttons.addWidget(add_folder)
        buttons.addWidget(remove)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.sample_hint = QLabel("No samples loaded.")
        self.sample_hint.setObjectName("Hint")
        self.sample_hint.setWordWrap(True)
        layout.addWidget(self.sample_hint)
        return card

    def _build_output_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(section_title("3. Output"))

        row = QHBoxLayout()
        row.setSpacing(7)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Folder for recovered JPEGs")
        echo = QPushButton("…")
        echo.setFixedWidth(36)
        echo.clicked.connect(self._browse_output)
        row.addWidget(self.output_edit, 1)
        row.addWidget(echo)
        layout.addLayout(row)

        self.separate_folder = QCheckBox("Create a subfolder for each image")
        self.separate_folder.setToolTip(
            "Useful when one encrypted file can produce several variants."
        )
        layout.addWidget(self.separate_folder)

        self.open_when_done = QCheckBox("Open the output folder when finished")
        self.open_when_done.setChecked(True)
        layout.addWidget(self.open_when_done)
        return card

    def _build_options_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(9)
        layout.addWidget(section_title("Recovery options"))

        self.auto_mode = QCheckBox("Find readable JPEGs in the source folder")
        self.auto_mode.setChecked(True)
        self.auto_mode.setToolTip(
            "When no sample is added manually, use readable JPEGs from the "
            "encrypted folder as samples.  If there are none, recovery falls "
            "back to sample-free inference."
        )
        layout.addWidget(self.auto_mode)

        self.sampleless = QCheckBox("Recover without a sample (best effort)")
        self.sampleless.setChecked(True)
        self.sampleless.setToolTip(
            "Infer the image width, sampling layout and repair tables from the "
            "surviving JPEG scan when no donor photo is available."
        )
        layout.addWidget(self.sampleless)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(7)
        quality_label = QLabel("Sample-free JPEG quality")
        quality_label.setObjectName("Hint")
        self.sampleless_quality = QComboBox()
        self.sampleless_quality.addItem("Auto (estimate)")
        for value in (60, 70, 75, 80, 85, 90, 95, 100):
            self.sampleless_quality.addItem(str(value))
        self.sampleless_quality.setCurrentText("100")
        self.sampleless_quality.setToolTip(
            "Quantisation tables are stored in the destroyed header.  The "
            "default is 100 (near-lossless, least visible blocking on detailed "
            "photos, larger file).  Auto estimates the quality from the "
            "compressed bit rate; choose a fixed standard quality if the "
            "colours look wrong."
        )
        quality_row.addWidget(quality_label, 1)
        quality_row.addWidget(self.sampleless_quality)
        layout.addLayout(quality_row)
        self.sampleless.toggled.connect(self.sampleless_quality.setEnabled)
        self.sampleless.toggled.connect(self._refresh_summary)
        self.sampleless.toggled.connect(self._refresh_sample_hint)
        self.auto_mode.toggled.connect(self._refresh_summary)

        self.auto_adjust = QCheckBox("Auto-adjust colors")
        self.auto_adjust.setToolTip(
            "Repair the lost DC predictor behind brightness and colour casts. "
            "Only DC coefficients are rewritten, so image detail is preserved."
        )
        layout.addWidget(self.auto_adjust)

        cmd_label = QLabel("Post-process command")
        cmd_label.setObjectName("Hint")
        layout.addWidget(cmd_label)
        row = QHBoxLayout()
        row.setSpacing(7)
        self.post_edit = QLineEdit()
        self.post_edit.setPlaceholderText(r'"C:\Tools\editor.exe" "%1"')
        self.post_edit.setToolTip(
            "Optional program run for every recovered file. %1 is replaced "
            "with the file path; otherwise the path is appended."
        )
        browse = QPushButton("…")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._browse_post)
        row.addWidget(self.post_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.wait_process = QCheckBox("Wait for the command to finish")
        self.wait_process.setChecked(True)
        layout.addWidget(self.wait_process)
        return card

    def _build_start_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        self.start_button = QPushButton("Recover files")
        self.start_button.setObjectName("Primary")
        self.start_button.setMinimumHeight(38)
        self.start_button.clicked.connect(self._try_start)
        layout.addWidget(self.start_button)
        self.summary_label = QLabel("Waiting for a source folder.")
        self.summary_label.setObjectName("Hint")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return card

    # ------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        settings = QSettings()
        self.source_edit.setText(settings.value("setup/source", "", str))
        self.source_edit.setCursorPosition(0)
        self.output_edit.setText(settings.value("setup/output", "", str))
        self.output_edit.setCursorPosition(0)
        self.extension_combo.setCurrentText(
            settings.value("setup/extension", ".abc", str)
        )
        self.prefix_combo.setCurrentText(
            settings.value("setup/prefix", str(DEFAULT_PREFIX), str)
        )
        self.encrypted_names.setChecked(
            settings.value("setup/encrypted_names", False, bool)
        )
        self.separate_folder.setChecked(
            settings.value("setup/separate", False, bool)
        )
        self.open_when_done.setChecked(
            settings.value("setup/open_when_done", True, bool)
        )
        self.auto_mode.setChecked(settings.value("setup/auto_mode", True, bool))
        self.sampleless.setChecked(
            settings.value("setup/sampleless", True, bool)
        )
        self.sampleless_quality.setCurrentText(
            settings.value(
                "setup/sampleless_quality", "100", str
            )
        )
        self.auto_adjust.setChecked(
            settings.value("setup/auto_adjust", False, bool)
        )
        self.post_edit.setText(settings.value("setup/post_command", "", str))
        self.wait_process.setChecked(
            settings.value("setup/wait_process", True, bool)
        )
        # Sample paths are intentionally not restored: removable drives and
        # renamed files should not produce errors at startup.

    def save_settings(self) -> None:
        settings = QSettings()
        settings.setValue("setup/source", self.source_edit.text().strip())
        settings.setValue("setup/output", self.output_edit.text().strip())
        settings.setValue("setup/extension", self.extension_combo.currentText().strip())
        settings.setValue("setup/prefix", self.prefix_combo.currentText().strip())
        settings.setValue("setup/encrypted_names", self.encrypted_names.isChecked())
        settings.setValue("setup/separate", self.separate_folder.isChecked())
        settings.setValue("setup/open_when_done", self.open_when_done.isChecked())
        settings.setValue("setup/auto_mode", self.auto_mode.isChecked())
        settings.setValue("setup/sampleless", self.sampleless.isChecked())
        settings.setValue(
            "setup/sampleless_quality", self.sampleless_quality.currentText()
        )
        settings.setValue("setup/auto_adjust", self.auto_adjust.isChecked())
        settings.setValue("setup/post_command", self.post_edit.text().strip())
        settings.setValue("setup/wait_process", self.wait_process.isChecked())

    # --------------------------------------------------------------- samples
    @property
    def samples(self) -> list[SampleInfo]:
        return list(self._samples)

    def add_samples(self, paths: list[str]) -> None:
        errors: list[str] = []
        added = 0
        for path in paths:
            try:
                sample = load_sample(path)
            except Exception as exc:  # noqa: BLE001 - displayed to the user
                errors.append(f"{os.path.basename(path)}: {exc}")
                continue
            key = (
                sample.header.frame.width,
                sample.header.frame.height,
                sample.header.quant_signature(),
            )
            if any(
                (
                    s.header.frame.width,
                    s.header.frame.height,
                    s.header.quant_signature(),
                )
                == key
                for s in self._samples
            ):
                continue
            self._samples.append(sample)
            self._append_sample_item(sample)
            added += 1
        self._samples = dedupe_samples(self._samples)
        self._refresh_sample_hint()
        if errors:
            QMessageBox.warning(
                self,
                "Some samples could not be used",
                "\n".join(errors[:8])
                + ("\n…" if len(errors) > 8 else ""),
            )
        if added and not self.output_edit.text().strip():
            source = self.source_edit.text().strip()
            if source:
                self.output_edit.setText(os.path.join(source, "recovered"))
                self.output_edit.setCursorPosition(0)

    def _append_sample_item(self, sample: SampleInfo) -> None:
        icon = QIcon()
        pixmap = QPixmap(sample.path)
        if not pixmap.isNull():
            icon = QIcon(
                pixmap.scaled(
                    56,
                    42,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        suffix = " (RAW preview)" if sample.from_raw else ""
        item = QListWidgetItem(
            icon,
            f"{sample.name}{suffix}\n{sample.width} × {sample.height}",
        )
        item.setData(Qt.ItemDataRole.UserRole, sample.path)
        item.setToolTip(sample.path)
        self.sample_list.addItem(item)

    def _refresh_sample_hint(self) -> None:
        count = len(self._samples)
        if not count:
            if self.sampleless.isChecked():
                self.sample_hint.setText(
                    "No samples loaded · sample-free recovery will infer the "
                    "geometry and standard tables from the surviving scan."
                )
            else:
                self.sample_hint.setText(
                    "No samples loaded. Add a readable photo from the same "
                    "camera; readable JPEGs in the source folder can also be used."
                )
            self._refresh_summary()
            return
        sizes = {(s.width, s.height) for s in self._samples}
        if len(sizes) == 1:
            width, height = next(iter(sizes))
            size_text = f"{width} × {height}"
        else:
            size_text = f"{len(sizes)} different sizes"
        self.sample_hint.setText(
            f"{count} sample{'s' if count != 1 else ''} loaded · {size_text}."
        )
        self._refresh_summary()

    def _add_sample_files(self) -> None:
        patterns = " ".join(
            f"*.{ext}" for ext in sorted(JPEG_EXTENSIONS | RAW_EXTENSIONS)
        )
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add sample photos",
            self.source_edit.text().strip() or str(Path.home()),
            f"Photos ({patterns});;All files (*.*)",
        )
        if paths:
            self.add_samples(paths)

    def _add_sample_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Add samples from a folder", str(Path.home())
        )
        if not folder:
            return
        paths = []
        try:
            for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
                if not entry.is_file():
                    continue
                ext = Path(entry.name).suffix.lower().lstrip(".")
                if ext in JPEG_EXTENSIONS or ext in RAW_EXTENSIONS:
                    paths.append(entry.path)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot read folder", str(exc))
            return
        if not paths:
            QMessageBox.information(
                self, "No photos found", "That folder has no JPEG or RAW files."
            )
            return
        self.add_samples(paths)

    def _remove_samples(self) -> None:
        for item in self.sample_list.selectedItems():
            row = self.sample_list.row(item)
            self.sample_list.takeItem(row)
            if 0 <= row < len(self._samples):
                del self._samples[row]
        self._refresh_sample_hint()

    def _clear_samples(self) -> None:
        self._samples.clear()
        self.sample_list.clear()
        self._refresh_sample_hint()

    def _open_sample(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl

            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # -------------------------------------------------------------- browsing
    def _browse_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the folder with encrypted files", self.source_edit.text()
        )
        if folder:
            self.source_edit.setText(folder)
            self.source_edit.setCursorPosition(0)
            if not self.output_edit.text().strip():
                self.output_edit.setText(os.path.join(folder, "recovered"))
                self.output_edit.setCursorPosition(0)

    def _browse_output(self) -> None:
        start = self.output_edit.text().strip() or self.source_edit.text().strip()
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the output folder", start or str(Path.home())
        )
        if folder:
            self.output_edit.setText(folder)
            self.output_edit.setCursorPosition(0)

    def _browse_post(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a post-processing program",
            "",
            "Programs (*.exe *.bat *.cmd);;All files (*.*)",
        )
        if path:
            self.post_edit.setText(f'"{path}" "%1"')

    def _source_changed(self) -> None:
        source = self.source_edit.text().strip()
        if source and not self.output_edit.text().strip():
            self.output_edit.setText(os.path.join(source, "recovered"))
        if not source:
            self.source_hint.setText("")
            return
        try:
            entries = [e for e in os.scandir(source) if e.is_file()]
        except OSError as exc:
            self.source_hint.setText(f"Cannot read folder: {exc}")
            return
        if not entries:
            self.source_hint.setText("This folder is empty.")
            return
        ext = self.extension_combo.currentText().strip().lstrip(".")
        matching = 0
        if ext:
            matching = sum(1 for e in entries if e.name.lower().endswith("." + ext.lower()))
        else:
            matching = len(entries)
        self.source_hint.setText(
            f"{len(entries)} file{'s' if len(entries) != 1 else ''} in folder"
            + (f" · {matching} match .{ext}" if ext else "")
        )
        self._refresh_summary()

    def _filenames_toggled(self, _checked: bool) -> None:
        self._refresh_summary()

    def _detect_extension(self) -> None:
        source = self.source_edit.text().strip()
        if not source or not os.path.isdir(source):
            QMessageBox.information(self, "Choose a folder first", "")
            return
        ext = guess_extension(
            source,
            sample_base_extensions(self._samples),
            self.encrypted_names.isChecked(),
        )
        if ext:
            self.extension_combo.setCurrentText("." + ext.lstrip("."))
        else:
            QMessageBox.information(
                self,
                "Extension not found",
                "Could not work out the encrypted-file extension. Enter it "
                "manually or leave the field empty.",
            )

    def _detect_prefix(self) -> None:
        source = self.source_edit.text().strip()
        if not source or not os.path.isdir(source):
            QMessageBox.information(self, "Choose a folder first", "")
            return
        if not self._samples:
            QMessageBox.information(
                self,
                "Prefix detection needs a sample",
                "Leave the encrypted prefix on Auto; sample-free recovery will "
                "try the common sizes for you.  Adding a readable photo also "
                "enables Detect….",
            )
            return
        ext = self.extension_combo.currentText().strip()
        files = find_candidates(
            source, ext, sample_base_extensions(self._samples),
            self.encrypted_names.isChecked(),
        )
        if not files:
            QMessageBox.information(
                self, "No encrypted files", "No files match the current settings."
            )
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        found: int | None = None
        try:
            for path in files[:3]:
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    continue
                # Candidates are ascending: the first success is the smallest
                # working prefix and therefore keeps the most rows.
                for prefix in prefix_candidates(data, None):
                    for sample in self._samples:
                        attempt = recover_bytes(data, sample.header, prefix)
                        if attempt.ok:
                            found = prefix
                            break
                    if found is not None:
                        break
                if found is not None:
                    break
        finally:
            QApplication.restoreOverrideCursor()
        if found is None:
            QMessageBox.information(
                self,
                "Prefix not detected",
                "None of the common prefix sizes produced a readable file. "
                "Enter the size manually if you know it.",
            )
            return
        self.prefix_combo.setCurrentText(str(found))

    # ------------------------------------------------------------- validation
    def _refresh_summary(self) -> None:
        source = self.source_edit.text().strip()
        if not source:
            self.summary_label.setText("Waiting for a source folder.")
            return
        try:
            count = sum(
                1
                for entry in os.scandir(source)
                if entry.is_file()
            )
        except OSError:
            count = 0
        parts = [f"{count} file{'s' if count != 1 else ''} in the source folder"]
        if self._samples:
            parts.append(f"{len(self._samples)} sample(s)")
        elif self.auto_mode.isChecked() or self.sampleless.isChecked():
            parts.append("sample-free ready")
        else:
            parts.append("no samples yet")
        self.summary_label.setText(" · ".join(parts) + ".")

    def config(self) -> RecoveryConfig:
        text = self.prefix_combo.currentText().strip()
        prefix: int | None
        if not text or text.lower().startswith("auto"):
            prefix = None
        else:
            try:
                prefix = int(text.replace(",", "").replace(" ", ""))
            except ValueError:
                prefix = DEFAULT_PREFIX
        output = self.output_edit.text().strip()
        if not output:
            source = self.source_edit.text().strip()
            output = os.path.join(source, "recovered") if source else ""
        quality_text = self.sampleless_quality.currentText().strip()
        try:
            quality = int(quality_text)
        except ValueError:
            quality = None
        return RecoveryConfig(
            encrypted_folder=self.source_edit.text().strip(),
            output_folder=output,
            extension=self.extension_combo.currentText().strip(),
            prefix_size=prefix,
            filenames_encrypted=self.encrypted_names.isChecked(),
            separate_subfolders=self.separate_folder.isChecked(),
            auto_adjust=self.auto_adjust.isChecked(),
            post_command=self.post_edit.text().strip(),
            wait_for_process=self.wait_process.isChecked(),
            auto_mode=self.auto_mode.isChecked(),
            samples=self.samples,
            allow_sampleless=self.sampleless.isChecked(),
            sampleless_quality=quality,
        )

    def validate(self) -> str | None:
        """Return a human-readable problem, or ``None`` when ready."""
        source = self.source_edit.text().strip()
        if not source:
            return "Choose the folder containing the encrypted files."
        if not os.path.isdir(source):
            return "The source folder does not exist."
        try:
            if not any(e.is_file() for e in os.scandir(source)):
                return "The source folder contains no files."
        except OSError as exc:
            return f"Cannot read the source folder: {exc}"
        output = self.output_edit.text().strip() or os.path.join(source, "recovered")
        try:
            os.makedirs(output, exist_ok=True)
        except OSError as exc:
            return f"Cannot create the output folder: {exc}"
        prefix_text = self.prefix_combo.currentText().strip()
        if prefix_text and not prefix_text.lower().startswith("auto"):
            try:
                prefix = int(prefix_text.replace(",", "").replace(" ", ""))
            except ValueError:
                return "The encrypted prefix size must be a number or Auto."
            if prefix <= 0:
                return "The encrypted prefix size must be greater than zero."
        if (
            self.encrypted_names.isChecked()
            and not self.extension_combo.currentText().strip()
        ):
            return (
                "Enter the encrypted file extension, or turn off "
                "\"File names are encrypted\"."
            )
        if (
            not self._samples
            and not self.auto_mode.isChecked()
            and not self.sampleless.isChecked()
        ):
            return (
                "Add a sample photo from the same camera, or enable "
                "\"Recover without a sample\"."
            )
        return None

    def _try_start(self) -> None:
        problem = self.validate()
        if problem:
            QMessageBox.warning(self, "Cannot start recovery", problem)
            return
        self.start_requested.emit()

    def set_busy(self, busy: bool) -> None:
        self.start_button.setEnabled(not busy)
        self.start_button.setText("Recovering…" if busy else "Recover files")

    def show_summary(self) -> None:
        self._refresh_summary()

    def add_implicit_samples(self) -> int:
        """Load readable JPEGs from the source folder, if any.

        Returns the number of new samples.  This mirrors the original
        application's convenience mode and avoids asking the user to copy
        photos out of the damaged folder.
        """
        source = self.source_edit.text().strip()
        if not source or not os.path.isdir(source):
            return 0
        existing = {
            os.path.normcase(s.path) for s in self._samples
        }
        ext = self.extension_combo.currentText().strip().lstrip(".")
        # A JPEG next to "photo.jpg.<ext>" may be a previously recovered
        # (cropped) output, not a good donor.  Skip those.
        encrypted_names: set[str] = set()
        paths = []
        try:
            entries = list(os.scandir(source))
            if ext:
                encrypted_names = {
                    entry.name.lower()
                    for entry in entries
                    if entry.is_file()
                    and entry.name.lower().endswith("." + ext.lower())
                }
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                if not entry.is_file():
                    continue
                if os.path.normcase(entry.path) in existing:
                    continue
                if Path(entry.name).suffix.lower().lstrip(".") not in JPEG_EXTENSIONS:
                    continue
                if ext and (entry.name + "." + ext).lower() in encrypted_names:
                    continue
                paths.append(entry.path)
        except OSError:
            return 0
        before = len(self._samples)
        if paths:
            self.add_samples(paths[:8])
        return len(self._samples) - before
