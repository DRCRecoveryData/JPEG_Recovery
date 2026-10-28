"""Application theme and palette."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication, QProxyStyle, QStyle

ACCENT = "#1f6feb"
ACCENT_DARK = "#1a5fd0"
ACCENT_SOFT = "#e8f1ff"
TEXT = "#17202b"
MUTED = "#677386"
BG = "#f3f5f8"
PANEL = "#ffffff"
BORDER = "#d9dfe8"
SUCCESS = "#1a7f4b"
DANGER = "#c0392b"
WARNING = "#b7791f"


class AppStyle(QProxyStyle):
    """Fusion style with crisp, high-contrast checkbox indicators.

    The native Fusion indicator is almost invisible on the light cards with
    this palette, especially when unchecked.  Drawing it explicitly also keeps
    the appearance identical at every display scale.
    """

    def __init__(self) -> None:
        super().__init__("Fusion")

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric in (
            QStyle.PixelMetric.PM_IndicatorWidth,
            QStyle.PixelMetric.PM_IndicatorHeight,
        ):
            return 16
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            self._draw_indicator(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    @staticmethod
    def _draw_indicator(option, painter: QPainter) -> None:
        state = option.state
        checked = bool(state & QStyle.StateFlag.State_On)
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)

        if checked:
            fill = QColor(ACCENT if enabled else "#a9c4f2")
            border = QColor(ACCENT_DARK if hovered and enabled else fill)
        else:
            fill = QColor(PANEL if enabled else "#f1f3f6")
            if not enabled:
                border = QColor(BORDER)
            elif hovered:
                border = QColor("#8fa2ba")
            else:
                border = QColor("#b7c2d0")

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(0.75, 0.75, -0.75, -0.75)
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3.0, 3.0)

        if checked:
            pen = QPen(QColor("#ffffff"), 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            width = rect.width()
            height = rect.height()
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(
                            rect.left() + width * 0.23,
                            rect.top() + height * 0.52,
                        ),
                        QPointF(
                            rect.left() + width * 0.42,
                            rect.top() + height * 0.71,
                        ),
                        QPointF(
                            rect.left() + width * 0.78,
                            rect.top() + height * 0.30,
                        ),
                    ]
                )
            )
        painter.restore()


STYLE = f"""
* {{
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 9.5pt;
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background: {BG};
}}
QWidget#Page {{
    background: {BG};
}}
QFrame#HeaderBar {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
QFrame#Card, QGroupBox {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QGroupBox {{
    margin-top: 16px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 5px;
    color: {TEXT};
    background: {PANEL};
}}
QLabel#Title {{
    font-size: 21pt;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#Subtitle {{
    color: {MUTED};
    font-size: 9.5pt;
}}
QLabel#PageTitle {{
    font-size: 16pt;
    font-weight: 700;
}}
QLabel#SectionTitle {{
    font-size: 11pt;
    font-weight: 600;
}}
QLabel#Muted, QLabel#Hint {{
    color: {MUTED};
}}
QLabel#Hint {{
    font-size: 8.8pt;
}}
QLabel#BigNumber {{
    font-size: 18pt;
    font-weight: 700;
    color: {ACCENT};
}}
QLabel#Logo {{
    background: {ACCENT};
    color: white;
    border-radius: 10px;
    font-size: 17pt;
    font-weight: 800;
}}
QLabel#StepChip {{
    background: #eef1f5;
    color: {MUTED};
    border-radius: 11px;
    padding: 5px 12px;
    font-weight: 600;
}}
QLabel#StepChipActive {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    border: 1px solid #bfd7ff;
    border-radius: 11px;
    padding: 4px 11px;
    font-weight: 700;
}}
QLabel#StepChipDone {{
    background: #e9f7ef;
    color: {SUCCESS};
    border-radius: 11px;
    padding: 5px 12px;
    font-weight: 600;
}}
QPushButton {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 13px;
    min-height: 16px;
}}
QPushButton:hover {{
    border-color: #b7c3d3;
    background: #f8fafc;
}}
QPushButton:pressed {{
    background: #eef2f7;
}}
QPushButton:disabled {{
    color: #9aa5b4;
    background: #f1f3f6;
    border-color: #e3e7ed;
}}
QPushButton#Primary {{
    background: {ACCENT};
    color: white;
    border: 1px solid {ACCENT};
    font-weight: 600;
    padding: 9px 22px;
}}
QPushButton#Primary:hover {{
    background: {ACCENT_DARK};
    border-color: {ACCENT_DARK};
}}
QPushButton#Primary:disabled {{
    background: #9fc0f5;
    border-color: #9fc0f5;
    color: #f7faff;
}}
QPushButton#Danger {{
    color: {DANGER};
    border-color: #e5beb9;
    background: #fffafa;
}}
QPushButton#Danger:hover {{
    background: #fdf0ee;
    border-color: #d99a92;
}}
QPushButton#Link {{
    border: none;
    background: transparent;
    color: {ACCENT};
    padding: 2px 4px;
    text-decoration: underline;
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #cfe2ff;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled {{
    background: #f1f3f6;
    color: #8b96a6;
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
    outline: none;
}}
QCheckBox {{
    spacing: 7px;
}}
QListWidget {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 3px;
    outline: none;
}}
QListWidget::item {{
    padding: 4px;
    border-radius: 5px;
}}
QListWidget::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
}}
QTableWidget {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 7px;
    gridline-color: #edf0f4;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
    outline: none;
}}
QTableWidget::item {{
    padding: 5px;
}}
QHeaderView::section {{
    background: #f7f9fc;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px;
    font-weight: 600;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 7px;
    background: {PANEL};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 7px 16px;
    border: 1px solid transparent;
    color: {MUTED};
}}
QTabBar::tab:selected {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-bottom-color: {PANEL};
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    color: {TEXT};
    font-weight: 600;
}}
QPlainTextEdit {{
    background: #0f1720;
    color: #d7e0ea;
    border: 1px solid #1c2733;
    border-radius: 7px;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 8.8pt;
    padding: 5px;
    selection-background-color: #2b4d77;
}}
QProgressBar {{
    background: #e8ecf2;
    border: none;
    border-radius: 4px;
    height: 7px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 4px;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #c6cfdb;
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: #aeb9c8;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #c6cfdb;
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QSplitter::handle {{
    background: transparent;
    width: 8px;
    height: 8px;
}}
QStatusBar {{
    background: {PANEL};
    border-top: 1px solid {BORDER};
    color: {MUTED};
}}
QToolTip {{
    background: #1e2a38;
    color: #eef3f9;
    border: none;
    padding: 5px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle(AppStyle())
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fc"))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#98a3b2"))
    app.setPalette(palette)
    font = QFont("Segoe UI", 9)
    app.setFont(font)
    app.setStyleSheet(STYLE)
