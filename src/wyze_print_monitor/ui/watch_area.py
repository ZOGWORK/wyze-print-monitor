"""Marking the print bed on the camera picture: Print watch looks only there.

The model sometimes "sees" a failure in a cable, a shelf or a toy at the
edge of the picture. Drag a box around the bed and anything outside it is
ignored.
"""
from __future__ import annotations

import cv2
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget

from ..failure_detect import Area, clean_area
from .styles import GREEN


class AreaPicker(QWidget):
    """The frame, with a box you drag out (kept as 0..1 fractions)."""

    changed = Signal()

    def __init__(self, image: QImage, area: Area | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = image
        self.area = area
        self._drag: QPointF | None = None
        self.setMinimumSize(480, 270)
        self.setCursor(Qt.CrossCursor)

    def _image_rect(self) -> QRectF:
        size = self._image.size().scaled(self.size(), Qt.KeepAspectRatio)
        x = (self.width() - size.width()) / 2
        y = (self.height() - size.height()) / 2
        return QRectF(x, y, size.width(), size.height())

    def _fraction(self, pos: QPointF) -> tuple[float, float]:
        r = self._image_rect()
        return (
            min(max((pos.x() - r.x()) / r.width(), 0.0), 1.0),
            min(max((pos.y() - r.y()) / r.height(), 0.0), 1.0),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self._drag = event.position()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag is not None:
            (x1, y1), (x2, y2) = self._fraction(self._drag), self._fraction(event.position())
            self.area = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag is not None and event.button() == Qt.LeftButton:
            self._drag = None
            self.area = clean_area(self.area) if self.area else None  # a click alone: no area
            self.update()
            self.changed.emit()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        r = self._image_rect()
        p.drawImage(r, self._image)
        if not self.area:
            return
        x1, y1, x2, y2 = self.area
        box = QRectF(r.x() + x1 * r.width(), r.y() + y1 * r.height(), (x2 - x1) * r.width(), (y2 - y1) * r.height())
        # Dim what's ignored, outline what's watched.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawRect(QRectF(r.left(), r.top(), r.width(), box.top() - r.top()))
        p.drawRect(QRectF(r.left(), box.bottom(), r.width(), r.bottom() - box.bottom()))
        p.drawRect(QRectF(r.left(), box.top(), box.left() - r.left(), box.height()))
        p.drawRect(QRectF(box.right(), box.top(), r.right() - box.right(), box.height()))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(GREEN), 2))
        p.drawRect(box)


class WatchAreaDialog(QDialog):
    """Pick the watch area on ``frame`` (BGR). ``area`` is the result."""

    def __init__(self, frame, area: Area | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Watch area")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        column = QVBoxLayout(self)
        hint = QLabel(
            "Drag a box around the print bed. Print watch ignores anything outside it, "
            "like cables, shelves or the floor."
        )
        hint.setWordWrap(True)
        column.addWidget(hint)
        self.picker = AreaPicker(image, area, self)
        column.addWidget(self.picker, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.whole_btn = QPushButton("Whole picture")
        self.whole_btn.setToolTip("Watch everything the camera sees")
        self.whole_btn.clicked.connect(self._whole)
        buttons.addButton(self.whole_btn, QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)
        self.resize(760, 520)

    @property
    def area(self) -> Area | None:
        return self.picker.area

    def _whole(self) -> None:
        self.picker.area = None
        self.picker.update()
