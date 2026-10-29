"""The app's two visible surfaces, shared by the full window and the panels
docked inside eufyMake Studio:

* :class:`VideoView` — the live picture, edge to edge. Status chips float
  over the top of it, a floating toolbar over the bottom (fading out while
  you're not using it). Scroll to zoom, drag to pan, double-click for
  fullscreen, Fit/Fill to letterbox or fill the space.
* :class:`CameraPanel` — the camera: header, connection banner, big toggle
  tiles (siren, spotlight, motion, notifications), power, recent captures,
  quick picture controls and device details.

These are views only. ``ui.binder.PanelBinder`` connects them to the
MainWindow, which owns all behavior.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..captures import Capture, describe_age, recent_captures, thumbnail
from .icons import (
    ICON_ADJUST,
    ICON_BELL,
    ICON_WATCH,
    ICON_CAMERA,
    ICON_CLOSE,
    ICON_FIT,
    ICON_FOLDER,
    ICON_FULLSCREEN,
    ICON_LIGHT,
    ICON_MOTION,
    ICON_PHOTO,
    ICON_POWER,
    ICON_RECORD,
    ICON_RESET,
    ICON_SETTINGS,
    ICON_SIREN,
    ICON_TIMER,
    ICON_VIDEO,
    ICON_WINDOW,
    glyph_icon,
    icon_font_family,
    tool_button,
)
from .styles import (
    DOT_AMBER,
    DOT_GREEN,
    DOT_GREY,
    DOT_RED,
    PILL_BG,
    PILL_BG_HOVER,
    PILL_TEXT,
    APP_BG,
    CARD,
    DIVIDER,
    GREEN,
    GREEN_BTN,
    GREEN_BTN_HOVER,
    RED,
    SELECTED,
    SLIDER_QSS,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
)

TOOLBAR_IDLE_MS = 2500
SNAP_PX = 2  # image within this many device px of its drawn size: drawn 1:1
ZOOM_STEP = 1.15  # per wheel notch

# Sheet for every surface. The catch-all QWidget rule comes first; equal
# specificity resolves by order, so each specific rule below wins over it.
VIEWS_QSS = f"""
QWidget {{
    background: transparent; color: {TEXT};
    font-family: "Segoe UI", sans-serif; font-size: 13px;
}}
QWidget#surface {{ background: {CARD}; }}
QLabel#chip, QLabel#overlayInfo {{
    background: {PILL_BG}; color: {PILL_TEXT}; border-radius: 11px;
    padding: 0 10px; min-height: 22px; max-height: 22px; font-size: 11px; font-weight: 600;
}}
QLabel#overlayInfo {{ font-weight: 400; color: #c9cbce; }}
QPushButton#zoomChip {{
    background: {PILL_BG}; color: {PILL_TEXT}; border-radius: 11px;
    padding: 0 10px; min-height: 22px; max-height: 22px; font-size: 11px; font-weight: 600;
}}
QPushButton#zoomChip:hover {{ background: {PILL_BG_HOVER}; }}
QFrame#pill {{ background: rgba(24, 25, 27, 225); border: 1px solid rgba(255, 255, 255, 30); border-radius: 12px; }}
QFrame#pill QToolButton {{ border: none; border-radius: 8px; padding: 6px 9px; color: {TEXT}; }}
QFrame#pill QToolButton:hover {{ background: rgba(255, 255, 255, 28); }}
QFrame#pill QToolButton:checked {{ background: {SELECTED}; color: {GREEN}; }}
QFrame#pill QToolButton:disabled {{ color: {TEXT_FAINT}; }}
QFrame#pill QToolButton#quality {{
    font-size: 11px; font-weight: 600; padding: 4px 8px; border: 1px solid rgba(255, 255, 255, 45);
}}
QFrame#pill QToolButton#quality::menu-indicator {{ image: none; width: 0; }}
QFrame#pillSep {{ background: rgba(255, 255, 255, 40); }}
QFrame#alert {{ background: rgba(58, 51, 36, 240); border: 1px solid #8a6d2f; border-radius: 10px; }}
QFrame#alert[severity="failure"], QFrame#alert[severity="paused"],
QFrame#alert[severity="pausing"], QFrame#alert[severity="pause_failed"] {{
    background: rgba(70, 30, 30, 242); border-color: {RED};
}}
QLabel#alertTitle {{ font-size: 14px; font-weight: 600; color: #ffffff; }}
QLabel#alertText {{ font-size: 12px; color: #e9dcc3; }}
QPushButton#danger {{ background: #6b2a2a; color: #ffd6d6; }}
QPushButton#danger:hover {{ background: #803232; }}
QLabel#toast {{
    background: rgba(24, 25, 27, 225); color: {TEXT}; border-radius: 8px;
    padding: 6px 12px; font-size: 12px;
}}

QLabel#title {{ font-size: 16px; font-weight: 600; }}
QLabel#dim {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#section {{ color: {TEXT_DIM}; font-size: 11px; font-weight: 600; letter-spacing: 0.6px; }}
QLabel#dot {{ font-size: 12px; }}
QFrame#divider {{ background: {DIVIDER}; }}
QFrame#banner {{ background: #3a3324; border-radius: 8px; }}
QLabel#bannerText {{ color: #f0d9a8; font-size: 12px; }}

QToolButton#tile {{
    background: #303235; border: 1px solid #3a3c40; border-radius: 10px;
    padding: 10px 12px; text-align: left; font-size: 13px;
}}
QToolButton#tile:hover {{ background: #36383c; }}
QToolButton#tile:checked {{ background: #263a2a; border-color: {GREEN}; color: #c9f2c8; }}
QToolButton#tileDanger:checked {{ background: #3d2424; border-color: {RED}; color: #ffd0d0; }}
QToolButton#tile:disabled, QToolButton#tileDanger:disabled {{ color: {TEXT_FAINT}; background: #2b2c2f; border-color: #333539; }}
QToolButton#tileDanger {{
    background: #303235; border: 1px solid #3a3c40; border-radius: 10px;
    padding: 10px 12px; font-size: 13px;
}}
QToolButton#tileDanger:hover {{ background: #36383c; }}

QToolButton#flat {{ border: none; border-radius: 6px; padding: 4px 6px; }}
QToolButton#flat:hover {{ background: #36383c; }}
QToolButton#capture {{
    border: 1px solid #3a3c40; border-radius: 8px; padding: 4px; background: #232427;
    font-size: 11px; color: {TEXT_DIM};
}}
QToolButton#capture:hover {{ border-color: {GREEN}; color: {TEXT}; }}

QPushButton {{
    background: {GREEN_BTN}; color: {GREEN}; border: none; border-radius: 6px;
    padding: 7px 12px; min-height: 18px;
}}
QPushButton:hover {{ background: {GREEN_BTN_HOVER}; }}
QPushButton:disabled {{ background: #2f3033; color: {TEXT_FAINT}; }}
QPushButton#accent {{ background: {GREEN}; color: #10240f; font-weight: 600; }}
QPushButton#accent:hover {{ background: #74dc73; }}
QPushButton#plain {{ background: #34363a; color: {TEXT}; }}
QPushButton#plain:hover {{ background: #3d4044; }}
QPushButton#plain:disabled {{ background: #2f3033; color: {TEXT_FAINT}; }}
QPushButton#link {{ background: transparent; color: {GREEN}; padding: 0; min-height: 0; }}
QPushButton#link:hover {{ color: #8be68a; }}

QComboBox {{ background: {APP_BG}; border: 1px solid {DIVIDER}; border-radius: 6px; padding: 4px 8px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {DIVIDER}; selection-background-color: {SELECTED}; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #4a4d52; border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QMenu {{ background: {CARD}; color: {TEXT}; border: 1px solid {DIVIDER}; }}
QMenu::item {{ padding: 5px 20px; }}
QMenu::item:selected {{ background: {SELECTED}; }}
""" + SLIDER_QSS

CHIP_STYLES = {  # stream state -> (text, status dot)
    "live": ("LIVE", DOT_GREEN),
    "connecting": ("Connecting…", DOT_AMBER),
    "reconnecting": ("Reconnecting…", DOT_AMBER),
    "error": ("Error", DOT_RED),
    "idle": ("Idle", DOT_GREY),
    "stopped": ("Idle", DOT_GREY),
}


def set_chip(label: QLabel, text: str, dot: str = "") -> None:
    """Show an overlay pill (hidden when ``text`` is empty), with an
    optional colored status dot. The pill's look comes from the shared
    stylesheet; only the text changes, and only when it differs (this runs
    on a timer)."""
    if not text:
        if not label.isHidden():
            label.hide()
        return
    shown = f'<span style="color:{dot}">\u25cf</span>&nbsp; {text}' if dot else text
    if label.text() != shown:
        label.setText(shown)
    if label.isHidden():
        label.show()


def _chip() -> QLabel:
    label = QLabel()
    label.setObjectName("chip")
    label.hide()
    return label


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    return label


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


class _Pill(QFrame):
    """Rounded floating toolbar."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("pill")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(6, 4, 6, 4)
        self.row.setSpacing(2)

    def separator(self) -> None:
        sep = QFrame()
        sep.setObjectName("pillSep")
        sep.setFixedSize(1, 20)
        self.row.addSpacing(4)
        self.row.addWidget(sep)
        self.row.addSpacing(4)


class AlertBar(QFrame):
    """A print-watch alert with what to do: across the top of the video, or
    ``compact`` (text above, buttons below) in the pop-up alert."""

    def __init__(self, parent: QWidget | None = None, compact: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("alert")
        self.setAttribute(Qt.WA_StyledBackground, True)
        if compact:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(12, 10, 12, 12)
            outer.setSpacing(8)
            row = QHBoxLayout()
            row.setSpacing(6)
        else:
            row = QHBoxLayout(self)
            row.setContentsMargins(12, 10, 10, 10)
            row.setSpacing(10)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("alertTitle")
        self.text = QLabel()
        self.text.setObjectName("alertText")
        self.text.setWordWrap(True)
        texts.addWidget(self.title)
        texts.addWidget(self.text)
        if compact:
            outer.addLayout(texts)
            outer.addLayout(row)
            row.addStretch(1)
        else:
            row.addLayout(texts, 1)
        self.silence_btn = QPushButton("\U0001F507 Silence")
        self.silence_btn.setObjectName("plain")
        self.silence_btn.setToolTip("Stop the alarm sound (the alert stays)")
        self.silence_btn.hide()
        self.view_btn = QPushButton("View snapshot")
        self.view_btn.setObjectName("plain")
        self.fine_btn = QPushButton("Looks fine")
        self.fine_btn.setObjectName("plain")
        self.fine_btn.setToolTip("Dismiss, and stay quiet for 30 minutes")
        self.pause_btn = QPushButton("Pause print")
        self.pause_btn.setObjectName("accent")
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setObjectName("accent")
        self.stop_btn = QPushButton("Stop print…")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setToolTip("Asks before stopping")
        buttons = (self.silence_btn, self.view_btn, self.fine_btn, self.pause_btn, self.resume_btn, self.stop_btn)
        for button in buttons:
            button.setCursor(Qt.PointingHandCursor)
        if compact:
            # A narrow card: print actions on one row, the rest on another.
            second = QHBoxLayout()
            second.setSpacing(6)
            for button in (self.pause_btn, self.resume_btn, self.stop_btn):
                row.addWidget(button)
            for button in (self.silence_btn, self.view_btn, self.fine_btn):
                second.addWidget(button)
            second.addStretch(1)
            outer.addLayout(second)
        else:
            for button in buttons:
                row.addWidget(button, 0, Qt.AlignVCenter)
        self.hide()

    def show_alert(self, alert) -> None:
        if alert is None:
            self.hide()
            return
        if self.property("severity") != alert.severity:
            self.setProperty("severity", alert.severity)
            self.style().unpolish(self)
            self.style().polish(self)
        icon = "🔊" if alert.severity == "sound" else "⚠"
        self.title.setText(f"{icon}  {alert.title}")
        self.text.setText(alert.message)
        self.view_btn.setVisible(bool(alert.snapshot))
        self.pause_btn.setVisible(alert.can_pause and alert.severity not in ("paused", "pausing"))
        self.resume_btn.setVisible(alert.can_resume)
        self.stop_btn.setVisible(alert.can_stop)
        self.fine_btn.setText("Looks fine" if alert.severity in ("warning", "failure", "sound") else "Dismiss")
        self.show()
        self.raise_()


class VideoView(QWidget):
    """The live picture with overlaid status and controls."""

    fullscreen_requested = Signal()
    zoom_by = Signal(float)       # multiply the current digital zoom
    pan_by = Signal(float, float)  # change in pan, in adjust.pan units (-1..1)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setMinimumSize(240, 135)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._image: QImage | None = None
        self._placeholder = "No signal"
        self._fill = False
        self._zoom = 1.0
        self._drag_from: QPoint | None = None
        self.live = False

        # --- top overlay: status chips
        self.top = QWidget(self)
        top = QHBoxLayout(self.top)
        top.setContentsMargins(12, 10, 12, 0)
        top.setSpacing(6)
        self.state_chip = _chip()
        self.info = QLabel()
        self.info.setObjectName("overlayInfo")
        self.rec_chip = _chip()
        self.timelapse_chip = _chip()
        self.watch_chip = _chip()  # print watch status
        self.zoom_chip = QPushButton()
        self.zoom_chip.setObjectName("zoomChip")
        self.zoom_chip.setToolTip("Digital zoom — click to reset (scroll to zoom, drag to pan)")
        self.zoom_chip.setCursor(Qt.PointingHandCursor)
        self.zoom_chip.hide()
        for widget in (self.state_chip, self.info):
            top.addWidget(widget)
        top.addStretch(1)
        for widget in (self.zoom_chip, self.watch_chip, self.rec_chip, self.timelapse_chip):
            top.addWidget(widget)

        # --- bottom overlay: floating toolbar + status toast
        self.pill = _Pill(self)
        self.snapshot_btn = tool_button(ICON_CAMERA, "", "Snapshot — save a PNG of the current frame")
        self.record_btn = tool_button(ICON_RECORD, "", "Record an MP4 clip", checkable=True)
        self.timelapse_btn = tool_button(ICON_TIMER, "", "Print timelapse", checkable=True)
        self.adjust_btn = tool_button(ICON_ADJUST, "", "Picture: brightness, exposure, rotate, zoom…")
        self.fit_btn = tool_button(ICON_FIT, "", "Fill the space (crop) / fit the whole picture", checkable=True)
        self.quality_btn = QToolButton()
        self.quality_btn.setObjectName("quality")
        self.quality_btn.setText("Auto")
        self.quality_btn.setToolTip("Stream quality")
        self.quality_btn.setPopupMode(QToolButton.InstantPopup)
        self.quality_btn.setCursor(Qt.PointingHandCursor)
        self.folder_btn = tool_button(ICON_FOLDER, "", "Open the snapshot / video folder")
        self.folder_btn.setPopupMode(QToolButton.InstantPopup)
        self.fullscreen_btn = tool_button(ICON_FULLSCREEN, "", "Fullscreen (Esc to exit)")
        self.window_btn = tool_button(ICON_WINDOW, "", "Open the full window")
        self.settings_btn = tool_button(ICON_SETTINGS, "", "Settings")
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("accent")
        self.update_btn.hide()
        for widget in (self.snapshot_btn, self.record_btn, self.timelapse_btn):
            self.pill.row.addWidget(widget)
        self.pill.separator()
        for widget in (self.adjust_btn, self.fit_btn, self.quality_btn, self.folder_btn):
            self.pill.row.addWidget(widget)
        self.pill.separator()
        for widget in (self.fullscreen_btn, self.window_btn, self.settings_btn, self.update_btn):
            self.pill.row.addWidget(widget)
        for button in self.pill.findChildren(QToolButton):
            button.setIconSize(QSize(18, 18))

        self.alert = AlertBar(self)

        self.toast = QLabel(self)
        self.toast.setObjectName("toast")
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)

        # Fade the toolbar out while nobody is using it (only once live).
        self._pill_fx = QGraphicsOpacityEffect(self.pill)
        self._pill_fx.setOpacity(1.0)
        self.pill.setGraphicsEffect(self._pill_fx)
        self._pill_anim = QPropertyAnimation(self._pill_fx, b"opacity", self)
        self._pill_anim.setDuration(220)
        self._pill_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(TOOLBAR_IDLE_MS)
        self._idle.timeout.connect(self._maybe_hide_toolbar)

    # ------------------------------------------------------------------ content
    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def has_image(self) -> bool:
        return self._image is not None

    def show_placeholder(self, text: str) -> None:
        if self._image is not None or self._placeholder != text:
            self._image = None
            self._placeholder = text
            self.update()

    def set_fill(self, fill: bool) -> None:
        if self._fill != fill:
            self._fill = fill
            self.update()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom
        if zoom > 1.0:
            self.zoom_chip.setText(f"{zoom:.1f}×  ✕")
            self.zoom_chip.show()
        else:
            self.zoom_chip.hide()
        self.setCursor(Qt.OpenHandCursor if zoom > 1.0 else Qt.ArrowCursor)

    def set_live(self, live: bool) -> None:
        if live == self.live:
            return
        self.live = live
        if live:
            self._idle.start()
        else:
            self._show_toolbar()

    def show_alert(self, alert) -> None:
        self.alert.show_alert(alert)
        self._place_overlays()

    def show_toast(self, message: str, timeout_ms: int = 5000) -> None:
        if not message:
            return
        self.toast.setText(message)
        self.toast.adjustSize()
        self._place_overlays()
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(timeout_ms or 8000)

    # ------------------------------------------------------------------ geometry
    def _image_rect(self) -> QRectF:
        """Where the picture is drawn: fitted (letterboxed) or filled (cropped)."""
        if self._image is None or self._image.isNull():
            return QRectF(self.rect())
        iw, ih = self._image.width(), self._image.height()
        w, h = self.width(), self.height()
        scale = max(w / iw, h / ih) if self._fill else min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        return QRectF((w - dw) / 2, (h - dh) / 2, dw, dh)

    def _place_overlays(self) -> None:
        self.top.setGeometry(0, 0, self.width(), 44)
        if not self.alert.isHidden():
            aw = min(self.width() - 24, 920)
            ah = self.alert.heightForWidth(aw) if self.alert.hasHeightForWidth() else self.alert.sizeHint().height()
            self.alert.setGeometry((self.width() - aw) // 2, 48, aw, max(ah, self.alert.sizeHint().height()))
        self.pill.adjustSize()
        pw, ph = self.pill.sizeHint().width(), self.pill.sizeHint().height()
        pw = min(pw, self.width() - 16)
        self.pill.setGeometry((self.width() - pw) // 2, self.height() - ph - 12, pw, ph)
        if self.toast.isVisible() or self.toast.text():
            tw = min(self.toast.sizeHint().width(), self.width() - 24)
            self.toast.setGeometry(12, self.pill.y() - self.toast.sizeHint().height() - 8, tw, self.toast.sizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_overlays()

    # ------------------------------------------------------------------ painting
    def _draw_image(self, painter: QPainter) -> None:
        rect = self._image_rect()
        dpr = self.devicePixelRatioF()
        iw, ih = self._image.width(), self._image.height()
        if abs(rect.width() * dpr - iw) <= SNAP_PX and abs(rect.height() * dpr - ih) <= SNAP_PX:
            # The stream thread already made the image the drawn size: draw it
            # 1:1 on whole device pixels. Letting Qt "scale" it by a pixel or
            # two at 125% display scaling cost a full smooth resample on every
            # frame (most of a CPU core).
            image = QImage(self._image)  # shares the pixels
            image.setDevicePixelRatio(dpr)
            x = round((self.width() * dpr - iw) / 2) / dpr
            y = round((self.height() * dpr - ih) / 2) / dpr
            painter.drawImage(QPointF(x, y), image)
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(rect, self._image)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image is not None and not self._image.isNull():
            self._draw_image(painter)
            # Soft shades so the chips and toolbar stay readable on bright video.
            top = QLinearGradient(0, 0, 0, 70)
            top.setColorAt(0, QColor(0, 0, 0, 140))
            top.setColorAt(1, QColor(0, 0, 0, 0))
            painter.fillRect(0, 0, self.width(), 70, top)
            bottom = QLinearGradient(0, self.height() - 90, 0, self.height())
            bottom.setColorAt(0, QColor(0, 0, 0, 0))
            bottom.setColorAt(1, QColor(0, 0, 0, 120))
            painter.fillRect(0, self.height() - 90, self.width(), 90, bottom)
        else:
            family = icon_font_family()
            if family:
                icon_font = QFont(family)
                icon_font.setPixelSize(40)
                painter.setFont(icon_font)
                painter.setPen(QColor("#4a4d52"))
                painter.drawText(self.rect().adjusted(0, -50, 0, -50), Qt.AlignCenter, ICON_VIDEO)
            text_font = QFont("Segoe UI")
            text_font.setPixelSize(14)
            painter.setFont(text_font)
            painter.setPen(QColor(TEXT_DIM))
            painter.drawText(self.rect().adjusted(24, 20, -24, 20), Qt.AlignCenter | Qt.TextWordWrap, self._placeholder)
        painter.end()

    # ------------------------------------------------------------------ toolbar fade
    def _show_toolbar(self) -> None:
        if self._pill_fx.opacity() < 1.0 and self._pill_anim.endValue() != 1.0:
            self._pill_anim.stop()
            self._pill_anim.setStartValue(self._pill_fx.opacity())
            self._pill_anim.setEndValue(1.0)
            self._pill_anim.start()
        self.pill.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        if self.live:
            self._idle.start()

    def _maybe_hide_toolbar(self) -> None:
        under_mouse = self.pill.underMouse() or any(
            b.isDown() or (b.menu() is not None and b.menu().isVisible())
            for b in self.pill.findChildren(QToolButton)
        )
        if not self.live or under_mouse:
            self._idle.start()
            return
        self._pill_anim.stop()
        self._pill_anim.setStartValue(self._pill_fx.opacity())
        self._pill_anim.setEndValue(0.0)
        self._pill_anim.start()
        self.pill.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------------ interaction
    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._show_toolbar()
        super().enterEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._show_toolbar()
        if self._drag_from is not None and self._zoom > 1.0:
            pos = event.position().toPoint()
            delta = pos - self._drag_from
            self._drag_from = pos
            rect = self._image_rect()
            # A full drag across the picture moves through 2/(zoom-1) of the
            # pan range (see adjust.zoom_crop): dragging right shows more left.
            if rect.width() > 0 and rect.height() > 0:
                self.pan_by.emit(
                    -delta.x() * 2.0 / (rect.width() * (self._zoom - 1.0)),
                    -delta.y() * 2.0 / (rect.height() * (self._zoom - 1.0)),
                )
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton and self._zoom > 1.0:
            self._drag_from = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.OpenHandCursor if self._zoom > 1.0 else Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.fullscreen_requested.emit()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        notches = event.angleDelta().y() / 120.0
        if notches:
            self.zoom_by.emit(ZOOM_STEP ** notches)
            event.accept()
            return
        super().wheelEvent(event)


class ToggleTile(QToolButton):
    """A large on/off tile: icon, title and current state."""

    def __init__(self, glyph: str, title: str, danger: bool = False) -> None:
        super().__init__()
        self.setObjectName("tileDanger" if danger else "tile")
        self.setCheckable(True)
        self.title = title
        self._glyph = glyph
        self._danger = danger
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(22, 22))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(58)
        self.setCursor(Qt.PointingHandCursor)
        self._state = ""
        self.set_state("Off")

    def set_state(self, state: str) -> None:
        """Update the second line ("On", "Off", "…", "Unknown")."""
        if state == self._state:
            return
        self._state = state
        on = self.isChecked()
        color = (RED if self._danger else GREEN) if on else TEXT_DIM
        self.setIcon(glyph_icon(self._glyph, color, 22))
        self.setText(f"{self.title}\n{state}")

    def nextCheckState(self) -> None:  # noqa: N802 - Qt override
        # The binder decides the real state (a failed command rolls back).
        pass


class CapturesGrid(QWidget):
    """Thumbnails of the latest snapshots / clips / timelapses."""

    _thumb_ready = Signal(str, float, object)  # path, mtime, QImage | None
    _scan_ready = Signal(object)  # list[Capture]

    COLUMNS = 3

    def __init__(self) -> None:
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(6)
        for column in range(self.COLUMNS):
            self._grid.setColumnStretch(column, 1)  # one item = one third, not the row
        self._buttons: list[QToolButton] = []
        self._items: list[Capture] = []
        self._cache: dict[tuple[str, float], QPixmap] = {}
        self._pending: set[tuple[str, float]] = set()
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._thumb_ready.connect(self._on_thumb)
        self._scan_ready.connect(self.set_items)
        self._scanning = False
        self.empty = QLabel("No captures yet. Take a snapshot or record a clip.")
        self.empty.setObjectName("dim")
        self.empty.setWordWrap(True)
        self._grid.addWidget(self.empty, 0, 0, 1, self.COLUMNS)

    def request_scan(self, snapshot_dir: str, video_dir: str) -> None:
        """Rescan the capture folders on the worker thread, then show them."""
        if self._scanning:
            return
        self._scanning = True

        def work() -> None:
            try:
                items = recent_captures(snapshot_dir, video_dir)
            except Exception:  # noqa: BLE001 - a bad folder just shows nothing new
                items = None
            try:
                self._scan_ready.emit(items)
            except RuntimeError:
                pass  # view already gone

        self._pool.submit(work)

    def set_items(self, items: list[Capture] | None) -> None:
        self._scanning = False
        if items is None:
            return
        if [(c.path, c.mtime) for c in items] == [(c.path, c.mtime) for c in self._items]:
            self._refresh_captions()
            return
        self._items = items
        # Keep thumbnails only for what's on screen: the cache can't grow
        # over a long session, and stale versions of a file are dropped.
        current = {(c.path, c.mtime) for c in items}
        self._cache = {key: pixmap for key, pixmap in self._cache.items() if key in current}
        for button in self._buttons:
            self._grid.removeWidget(button)
            button.deleteLater()
        self._buttons = []
        self.empty.setVisible(not items)
        for i, item in enumerate(items):
            button = QToolButton()
            button.setObjectName("capture")
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(104, 58))
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(os.path.basename(item.path))
            button.clicked.connect(lambda _=False, p=item.path: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
            key = (item.path, item.mtime)
            if key in self._cache:
                button.setIcon(self._cache[key])
            else:
                button.setIcon(glyph_icon(ICON_PHOTO if item.kind == "snapshot" else ICON_VIDEO, TEXT_FAINT, 32))
                self._request(item)
            self._grid.addWidget(button, 1 + i // self.COLUMNS, i % self.COLUMNS)
            self._buttons.append(button)
        self._refresh_captions()

    def refresh_ages(self) -> None:
        """Update the "5 min ago" captions without rescanning."""
        self._refresh_captions()

    def _refresh_captions(self) -> None:
        now = time.time()
        for button, item in zip(self._buttons, self._items):
            text = f"{item.label} · {describe_age(now - item.mtime)}"
            if button.text() != text:
                button.setText(text)

    def _request(self, item: Capture) -> None:
        key = (item.path, item.mtime)
        if key in self._pending:
            return
        self._pending.add(key)

        def work() -> None:
            frame = thumbnail(item.path, width=208)
            image = None
            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = rgb.shape[:2]
                image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
            try:
                self._thumb_ready.emit(item.path, item.mtime, image)
            except RuntimeError:
                pass  # view already gone

        self._pool.submit(work)

    def _on_thumb(self, path: str, mtime: float, image) -> None:
        key = (path, mtime)
        self._pending.discard(key)
        if image is None or key not in {(c.path, c.mtime) for c in self._items}:
            return  # unreadable, or no longer shown
        pixmap = QPixmap.fromImage(image)
        self._cache[key] = pixmap
        for button, item in zip(self._buttons, self._items):
            if (item.path, item.mtime) == key:
                button.setIcon(pixmap)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class CameraPanel(QWidget):
    """Everything about the camera, laid out to fill a tall side column.

    Two pages: the camera controls, and the picture adjustments. Adjusting
    happens here — beside the video, never on top of it — so you see the
    effect live; "Done" flips back.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pages.addWidget(scroll)
        self._build_adjust_page()
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        # --- header: camera name (or picker), status, refresh, settings
        header = QHBoxLayout()
        header.setSpacing(6)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.name_label = QLabel("Wyze camera")
        self.name_label.setObjectName("title")
        self.camera_combo = QComboBox()
        self.camera_combo.hide()
        self.status_label = QLabel()
        self.status_label.setObjectName("dot")
        names.addWidget(self.name_label)
        names.addWidget(self.camera_combo)
        names.addWidget(self.status_label)
        header.addLayout(names, 1)
        self.refresh_btn = tool_button(ICON_RESET, "", "Refresh the camera list")
        self.refresh_btn.setObjectName("flat")
        self.settings_btn = tool_button(ICON_SETTINGS, "", "Settings")
        self.settings_btn.setObjectName("flat")
        header.addWidget(self.refresh_btn, 0, Qt.AlignTop)
        header.addWidget(self.settings_btn, 0, Qt.AlignTop)
        layout.addLayout(header)

        # --- connection banner (only when something needs attention)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        banner_row = QHBoxLayout(self.banner)
        banner_row.setContentsMargins(12, 8, 8, 8)
        self.banner_text = QLabel()
        self.banner_text.setObjectName("bannerText")
        self.banner_text.setWordWrap(True)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("accent")
        banner_row.addWidget(self.banner_text, 1)
        banner_row.addWidget(self.connect_btn, 0, Qt.AlignVCenter)
        self.banner.hide()
        layout.addWidget(self.banner)

        # --- big toggles
        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.siren_tile = ToggleTile(ICON_SIREN, "Siren", danger=True)
        self.spotlight_tile = ToggleTile(ICON_LIGHT, "Spotlight")
        self.motion_tile = ToggleTile(ICON_MOTION, "Motion")
        self.notify_tile = ToggleTile(ICON_BELL, "Alerts")
        for i, tile in enumerate((self.siren_tile, self.spotlight_tile, self.motion_tile, self.notify_tile)):
            tiles.addWidget(tile, i // 2, i % 2)
        # Print watch is the app's own switch (not a camera setting): full width.
        self.watch_tile = ToggleTile(ICON_WATCH, "Print watch (AI)")
        self.watch_tile.setToolTip("Watch prints for failures. Settings > Print watch for options.")
        tiles.addWidget(self.watch_tile, 2, 0, 1, 2)
        layout.addLayout(tiles)

        # --- power
        power = QHBoxLayout()
        power.setSpacing(8)
        self.power_on_btn = QPushButton(glyph_icon(ICON_POWER, GREEN, 16), " On")
        self.power_off_btn = QPushButton(glyph_icon(ICON_POWER, TEXT_DIM, 16), " Off")
        self.restart_btn = QPushButton(glyph_icon(ICON_RESET, TEXT, 16), " Restart")
        self.power_off_btn.setObjectName("plain")
        self.restart_btn.setObjectName("plain")
        for button in (self.power_on_btn, self.power_off_btn, self.restart_btn):
            power.addWidget(button)
        layout.addLayout(power)

        layout.addWidget(_divider())

        # --- recent captures
        captures_head = QHBoxLayout()
        captures_head.addWidget(_section("RECENT CAPTURES"))
        captures_head.addStretch(1)
        self.open_folder_btn = QPushButton("Open folder")
        self.open_folder_btn.setObjectName("link")
        self.open_folder_btn.setCursor(Qt.PointingHandCursor)
        captures_head.addWidget(self.open_folder_btn)
        layout.addLayout(captures_head)
        self.captures = CapturesGrid()
        layout.addWidget(self.captures)

        layout.addWidget(_divider())

        # --- quick picture controls
        picture_head = QHBoxLayout()
        picture_head.addWidget(_section("PICTURE"))
        picture_head.addStretch(1)
        self.more_picture_btn = QPushButton("All adjustments…")
        self.more_picture_btn.setObjectName("link")
        self.more_picture_btn.setCursor(Qt.PointingHandCursor)
        picture_head.addWidget(self.more_picture_btn)
        layout.addLayout(picture_head)
        quick = QGridLayout()
        quick.setHorizontalSpacing(10)
        quick.setVerticalSpacing(4)
        quick.setColumnStretch(1, 1)
        self.quick_sliders: dict[str, tuple[QSlider, QLabel]] = {}
        for row, (name, text, lo, hi) in enumerate(
            (("brightness", "Brightness", -100, 100), ("exposure", "Exposure", -20, 20), ("zoom", "Zoom", 100, 400))
        ):
            label = QLabel(text)
            label.setFixedWidth(78)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(lo, hi)
            value = QLabel()
            value.setObjectName("dim")
            value.setMinimumWidth(54)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            quick.addWidget(label, row, 0)
            quick.addWidget(slider, row, 1)
            quick.addWidget(value, row, 2)
            self.quick_sliders[name] = (slider, value)
        layout.addLayout(quick)

        layout.addStretch(1)
        layout.addWidget(_divider())
        self.details_label = QLabel()
        self.details_label.setObjectName("dim")
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details_label)

    # ------------------------------------------------------------------ pages
    def _build_adjust_page(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(14, 12, 10, 6)
        title = QLabel("Picture")
        title.setObjectName("title")
        hint = QLabel("Changes show live on the video.")
        hint.setObjectName("dim")
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(title)
        titles.addWidget(hint)
        head.addLayout(titles, 1)
        self.adjust_done_btn = QPushButton("Done")
        self.adjust_done_btn.setObjectName("accent")
        self.adjust_done_btn.setCursor(Qt.PointingHandCursor)
        head.addWidget(self.adjust_done_btn, 0, Qt.AlignTop)
        column.addLayout(head)
        column.addWidget(_divider())
        self._adjust_scroll = QScrollArea()
        self._adjust_scroll.setWidgetResizable(True)
        self._adjust_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        column.addWidget(self._adjust_scroll, 1)
        self.pages.addWidget(page)
        self.adjust_done_btn.clicked.connect(self.show_controls)

    def set_adjust_widget(self, widget: QWidget) -> None:
        """Install the adjustment controls (built by the binder: they need
        the main window)."""
        self._adjust_scroll.setWidget(widget)

    def show_adjust(self) -> None:
        self.pages.setCurrentIndex(1)

    def show_controls(self) -> None:
        self.pages.setCurrentIndex(0)

    def adjusting(self) -> bool:
        return self.pages.currentIndex() == 1
