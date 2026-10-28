"""Reusable widgets for the application."""

from __future__ import annotations

import os
from typing import Iterable

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import ACCENT, BORDER, MUTED, PANEL


class ImagePreview(QGraphicsView):
    """A pannable, zoomable image preview with an empty-state message."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)

        self._placeholder = QGraphicsSimpleTextItem("No image selected")
        font = QFont("Segoe UI", 11)
        self._placeholder.setFont(font)
        self._placeholder.setBrush(QBrush(QColor(MUTED)))
        self._scene.addItem(self._placeholder)

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#eef1f5")))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSceneRect(QRectF(0, 0, 640, 420))
        self.setMinimumSize(260, 200)
        self._auto_fit = True
        self._empty_text = "No image selected"

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self._placeholder.setText(text)
        self._center_placeholder()

    def has_image(self) -> bool:
        return not self._item.pixmap().isNull()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._item.setPixmap(pixmap)
        empty = pixmap.isNull()
        self._placeholder.setVisible(empty)
        if empty:
            self._center_placeholder()
            return
        self._scene.setSceneRect(self._item.boundingRect())
        self.setSceneRect(self._item.boundingRect())
        self._auto_fit = True
        QTimer.singleShot(0, self.fit_image)

    def set_image_bytes(self, data: bytes) -> bool:
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            self.clear()
            return False
        self.set_pixmap(pixmap)
        return True

    def set_image_path(self, path: str) -> bool:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.clear()
            return False
        self.set_pixmap(pixmap)
        return True

    def clear(self) -> None:
        self._item.setPixmap(QPixmap())
        self._placeholder.setVisible(True)
        self._scene.setSceneRect(QRectF(0, 0, 640, 420))
        self.setSceneRect(QRectF(0, 0, 640, 420))
        self._center_placeholder()

    def _center_placeholder(self) -> None:
        rect = self._scene.sceneRect()
        bounds = self._placeholder.boundingRect()
        self._placeholder.setPos(
            rect.center().x() - bounds.width() / 2,
            rect.center().y() - bounds.height() / 2,
        )

    def fit_image(self) -> None:
        if self._item.pixmap().isNull():
            return
        self._auto_fit = True
        self.resetTransform()
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_in(self) -> None:
        self._auto_fit = False
        self.scale(1.25, 1.25)

    def zoom_out(self) -> None:
        self._auto_fit = False
        self.scale(0.8, 0.8)

    def actual_size(self) -> None:
        self._auto_fit = False
        self.resetTransform()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.has_image():
            return
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self._auto_fit = False
        self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._auto_fit and self.has_image():
            self.fit_image()
        else:
            self._center_placeholder()


class ThumbnailStrip(QListWidget):
    """Horizontal filmstrip of recovered images."""

    image_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(104, 72))
        self.setGridSize(QSize(128, 104))
        self.setSpacing(3)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(112)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.itemSelectionChanged.connect(self._selection_changed)

    def _selection_changed(self) -> None:
        item = self.currentItem()
        if item is not None:
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                self.image_selected.emit(path)

    def add_image(self, path: str, label: str | None = None) -> QListWidgetItem:
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(
                104,
                72,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        item = QListWidgetItem(QIcon(pixmap), label or os.path.basename(path))
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.addItem(item)
        return item

    def remove_path(self, path: str) -> None:
        for index in range(self.count() - 1, -1, -1):
            item = self.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.takeItem(index)

    def select_path(self, path: str) -> None:
        for index in range(self.count()):
            item = self.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.setCurrentItem(item)
                return


class StepHeader(QWidget):
    """Little numbered chips showing the workflow position."""

    def __init__(self, labels: Iterable[str], parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._labels: list[QLabel] = []
        for index, label in enumerate(labels, 1):
            chip = QLabel(f"{index}   {label}")
            self._labels.append(chip)
            layout.addWidget(chip)
        self.set_step(0)

    def set_step(self, current: int, completed: int | None = None) -> None:
        if completed is None:
            completed = current
        for index, chip in enumerate(self._labels):
            if index == current:
                name = "StepChipActive"
            elif index < completed:
                name = "StepChipDone"
            else:
                name = "StepChip"
            chip.setObjectName(name)
            chip.style().unpolish(chip)
            chip.style().polish(chip)


class HeaderBar(QFrame):
    """Application header with brand mark and step indicator."""

    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("HeaderBar")
        self.setFixedHeight(76)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 10, 22, 10)
        layout.setSpacing(13)

        logo = QLabel("JM")
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(46, 46)
        layout.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        name = QLabel(title)
        name.setStyleSheet("font-size: 14.5pt; font-weight: 700;")
        tagline = QLabel(subtitle)
        tagline.setObjectName("Hint")
        titles.addWidget(name)
        titles.addWidget(tagline)
        layout.addLayout(titles)
        layout.addStretch(1)

        self.steps = StepHeader(["Setup", "Results", "Post-process"])
        layout.addWidget(self.steps, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_step(self, current: int, completed: int | None = None) -> None:
        self.steps.set_step(current, completed)


class Card(QFrame):
    """A white rounded panel."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(
            f"QFrame#Card {{ background: {PANEL}; border: 1px solid {BORDER};"
            " border-radius: 10px; }}"
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)


def section_title(text: str, subtitle: str | None = None) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    title = QLabel(text)
    title.setObjectName("SectionTitle")
    layout.addWidget(title)
    if subtitle:
        hint = QLabel(subtitle)
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
    return widget


class StatTile(QFrame):
    """Small summary tile used on the results page."""

    def __init__(self, caption: str, value: str = "0", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(
            f"QFrame#Card {{ background: {PANEL}; border: 1px solid {BORDER};"
            " border-radius: 9px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(0)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("BigNumber")
        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("Hint")
        layout.addWidget(self.value_label)
        layout.addWidget(self.caption_label)

    def set_value(self, value: str | int) -> None:
        self.value_label.setText(str(value))


def app_icon() -> QIcon:
    """An original, simple brand mark drawn at runtime (no copied artwork)."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(ACCENT)))
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QColor("white"))
    font = QFont("Segoe UI", 22, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(
        QRectF(2, 2, 60, 60),
        Qt.AlignmentFlag.AlignCenter,
        "JM",
    )
    painter.end()
    return QIcon(pixmap)
