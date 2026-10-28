"""Scheduling prints from eufyMake Studio, and the print queue.

* A small ▴ sits on the right end of Studio's Slice-tab **Print** button
  (a split button): it opens a drop-up with "Schedule print…" and the
  prints already scheduled.
* The **print queue** fills the empty lower part of Studio's "My Printer"
  column on the Device tab: what's coming up (with Print now / Edit /
  Cancel) and what happened to recent ones.

Both are native Qt windows parented into Studio's own window tree, like
the camera panels (see :mod:`.studio_dock`), so they move and hide with
Studio by themselves. The same queue is also a window of its own (main
window → Print, or the tray), which works without docking.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QCursor, QWindow
from PySide6.QtWidgets import (
    QApplication,
    QDateTimeEdit,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import schedule, studio_win32 as win
from ..schedule import FAILED, MISSED, SENDING, STATE_LABELS, ScheduledPrint
from .print_schedule import PrintScheduler, printer_name
from .styles import AMBER, CARD, DIVIDER, GREEN, RED, TEXT_DIM
from .views import VIEWS_QSS

TICK_MS = 300
FIND_EVERY_S = 1.0  # re-find Studio's Print button / printer column
REFRESH_MS = 30_000  # "in 5 min" texts
QUEUE_MIN_H = 140  # less room than this in the column: don't show the queue

# Colors sampled from Studio's own "Choose a Printer" dialog, so ours look
# like part of Studio rather than another app.
DIALOG_BG = "#333438"
DIALOG_LINE = "#545863"
ROW_SELECTED = "#3a4c3e"
BUTTON_GREY = "#595a5e"
FIELD_BG = "#2a2b2e"

QUEUE_QSS = f"""
QFrame#job {{ background: #313235; border-radius: 6px; }}
QFrame#job[state="sending"] {{ background: {ROW_SELECTED}; }}
QLabel#jobTime {{ font-size: 14px; color: #ffffff; }}
QLabel#jobInfo {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#state {{ font-size: 12px; color: {TEXT_DIM}; }}
QLabel#state[state="started"] {{ color: {GREEN}; }}
QLabel#state[state="failed"] {{ color: {RED}; }}
QLabel#state[state="missed"] {{ color: {AMBER}; }}
QLabel#state[state="sending"] {{ color: {GREEN}; }}
QToolButton#mini {{ border: none; border-radius: 4px; padding: 2px 6px; font-size: 12px; color: {TEXT_DIM}; }}
QToolButton#mini:hover {{ background: #3d4044; color: #ffffff; }}
QMenu {{ background: {DIALOG_BG}; border: 1px solid {DIALOG_LINE}; padding: 4px 0; }}
QMenu::item {{ padding: 7px 22px; }}
QMenu::item:selected {{ background: {ROW_SELECTED}; }}
QMenu::item:disabled {{ color: #7d8086; }}
QMenu::separator {{ height: 1px; background: {DIALOG_LINE}; margin: 4px 0; }}
"""

DIALOG_QSS = f"""
QWidget#dialog {{ background: {DIALOG_BG}; border: 1px solid #44464b; }}
QLabel#dialogTitle {{ font-size: 16px; color: #ffffff; }}
QFrame#line {{ background: {DIALOG_LINE}; }}
QLabel#caption {{ color: {TEXT_DIM}; font-size: 13px; }}
QLabel#note {{ color: {TEXT_DIM}; font-size: 12px; }}
QToolButton#close {{ border: none; color: {TEXT_DIM}; font-size: 15px; padding: 2px 6px; }}
QToolButton#close:hover {{ color: #ffffff; }}
QDateTimeEdit, QLineEdit {{
    background: {FIELD_BG}; border: 1px solid #4a4c51; border-radius: 4px;
    padding: 6px 8px; color: #ffffff; font-size: 14px; selection-background-color: {ROW_SELECTED};
}}
QDateTimeEdit:focus, QLineEdit:focus {{ border-color: {GREEN}; }}
QDateTimeEdit::drop-down {{ border: none; width: 24px; }}
QCalendarWidget QWidget {{ background: {DIALOG_BG}; color: #ffffff; }}
QCalendarWidget QAbstractItemView:enabled {{ selection-background-color: {GREEN}; selection-color: #ffffff; }}
QCalendarWidget QToolButton {{ color: #ffffff; padding: 4px; }}
QPushButton#chip {{
    background: #45464a; color: #e6e7e9; border-radius: 12px; padding: 3px 12px;
    min-height: 18px; font-size: 12px;
}}
QPushButton#chip:hover {{ background: #505156; }}
QPushButton#printerRow {{
    background: transparent; border: none; border-radius: 0; text-align: left; padding: 10px 24px;
    font-size: 14px; min-height: 40px;
}}
QPushButton#printerRow:hover {{ background: #3a3b3f; }}
QPushButton#printerRow:checked {{ background: {ROW_SELECTED}; }}
QPushButton#cancel, QPushButton#confirm {{
    border-radius: 6px; min-height: 38px; font-size: 15px; color: #ffffff; padding: 0 12px;
}}
QPushButton#cancel {{ background: {BUTTON_GREY}; }}
QPushButton#cancel:hover {{ background: #66676b; }}
QPushButton#confirm {{ background: {GREEN}; }}
QPushButton#confirm:hover {{ background: #74dc73; }}
"""


def studio_printer_count() -> int:
    """Rows in Studio's "My Printer" list (at least 1; 0 when unknown)."""
    top = win.find_studio()
    layout = win.locate_layout(top) if top else None
    return len(layout.printer_rows) if layout else 0


def studio_project() -> str:
    top = win.find_studio()
    return win.project_name(win.title(top)) if top else ""


# ---------------------------------------------------------------------- dialog
class ScheduleDialog(QDialog):
    """Pick when (and on which printer) Studio's sliced plate prints.

    Laid out like Studio's own "Choose a Printer": a centered title with ✕,
    the printers as a list (selected row tinted green with a check), and
    wide Cancel / confirm buttons at the bottom.
    """

    def __init__(self, main, scheduler: PrintScheduler, job: ScheduledPrint | None = None, parent=None) -> None:
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.main = main
        self.scheduler = scheduler
        self.job = job
        self._drag: QPoint | None = None
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("dialog")
        self.setStyleSheet(VIEWS_QSS + QUEUE_QSS + DIALOG_QSS)
        heading = "Reschedule Print" if job else "Schedule Print"
        self.setWindowTitle(heading)
        self.setFixedWidth(480)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 22)
        column.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(52, 12, 12, 12)
        title = QLabel(heading)
        title.setObjectName("dialogTitle")
        title.setAlignment(Qt.AlignCenter)
        head.addWidget(title, 1)
        close = QToolButton()
        close.setObjectName("close")
        close.setText("✕")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.reject)
        head.addWidget(close)
        column.addLayout(head)
        line = QFrame()
        line.setObjectName("line")
        line.setFixedHeight(1)
        column.addWidget(line)

        body = QVBoxLayout()
        body.setContentsMargins(24, 16, 24, 8)
        body.setSpacing(8)
        column.addLayout(body)
        body.addWidget(self._caption("Start time"))
        now = datetime.now()
        self.when = QDateTimeEdit()
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("ddd, MMM d, yyyy    h:mm AP")
        self.when.setMinimumDateTime(QDateTime(now.replace(second=0, microsecond=0)))
        self.when.setDateTime(QDateTime(job.when if job else schedule.default_time(now)))
        body.addWidget(self.when)
        quick = QHBoxLayout()
        quick.setSpacing(6)
        for text, pick in (
            ("In 1 hour", lambda n: n + timedelta(hours=1)),
            ("Tonight 10 PM", lambda n: n.replace(hour=22, minute=0) + timedelta(days=1 if n.hour >= 22 else 0)),
            ("Tomorrow 8 AM", lambda n: n.replace(hour=8, minute=0) + timedelta(days=1)),
        ):
            button = QPushButton(text)
            button.setObjectName("chip")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(
                lambda _=False, pick=pick: self.when.setDateTime(
                    QDateTime(pick(datetime.now().replace(second=0, microsecond=0)))
                )
            )
            quick.addWidget(button)
        quick.addStretch(1)
        body.addLayout(quick)
        body.addSpacing(6)
        body.addWidget(self._caption("Printer"))

        # Full-width rows like Studio's printer list.
        rows = max(studio_printer_count(), job.printer_row if job else 1, 1)
        camera_row = main.settings.dock_printer_row
        default = job.printer_row if job else (camera_row if 0 < camera_row <= rows else 1)
        self.row = default
        self._rows: list[tuple[QPushButton, str, str]] = []
        for row in range(1, rows + 1):
            button = QPushButton()
            button.setObjectName("printerRow")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            detail = f"Row {row} in My Printer" + (" · has the camera" if row == camera_row else "")
            button.toggled.connect(lambda on, row=row: self._pick_row(row) if on else None)
            column.addWidget(button)
            self._rows.append((button, printer_name(row), detail))
        self._rows[default - 1][0].setChecked(True)
        self._pick_row(default)

        rest = QVBoxLayout()
        rest.setContentsMargins(24, 14, 24, 0)
        rest.setSpacing(8)
        rest.addWidget(self._caption("Name (optional)"))
        self.label = QLineEdit(job.label if job else studio_project())
        self.label.setPlaceholderText("Shown in the print queue")
        rest.addWidget(self.label)
        note = QLabel(
            "At this time the app presses Print in Studio for you, sending whatever is "
            "sliced on its Slice tab. Leave Studio open and the PC awake."
        )
        note.setObjectName("note")
        note.setWordWrap(True)
        rest.addSpacing(4)
        rest.addWidget(note)
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self.error.setWordWrap(True)
        self.error.hide()
        rest.addWidget(self.error)
        column.addLayout(rest)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(24, 22, 24, 0)
        buttons.setSpacing(12)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("cancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Save" if job else "Schedule")
        ok.setObjectName("confirm")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(self._save)
        buttons.addWidget(cancel, 1)
        buttons.addWidget(ok, 1)
        column.addLayout(buttons)

    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("caption")
        return label

    def _pick_row(self, row: int) -> None:
        self.row = row
        for i, (button, name, detail) in enumerate(self._rows, start=1):
            chosen = i == row
            button.setText(f"{name}{'   ✓' if chosen else ''}\n{detail}")
            button.setStyleSheet(f"QPushButton#printerRow {{ color: {'#ffffff' if chosen else '#a9acb1'}; }}")

    # Frameless like Studio's dialog: drag it by its title row.
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton and event.position().y() < 50:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._drag = None
        super().mouseReleaseEvent(event)

    def _save(self) -> None:
        at = self.when.dateTime().toPython().replace(second=0, microsecond=0)
        row = self.row
        if at < datetime.now() - timedelta(minutes=1):
            self._show_error("That time has already passed.")
            return
        other = schedule.clashes(self.scheduler.jobs, at, row, ignore=self.job.id if self.job else "")
        if other is not None:
            self._show_error(f"{printer_name(row)} already has a print scheduled then.")
            return
        if self.job:
            self.scheduler.reschedule(self.job.id, at, row, self.label.text())
        else:
            self.scheduler.add(at, row, self.label.text())
        when = schedule.describe_when(at, datetime.now())
        self.main._status(f"Print scheduled for {when} on {printer_name(row)}.", 8000)
        self.accept()

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


def open_schedule_dialog(main, scheduler: PrintScheduler, job: ScheduledPrint | None = None) -> None:
    dialog = ScheduleDialog(main, scheduler, job)
    dialog.setAttribute(Qt.WA_DeleteOnClose, True)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


# ---------------------------------------------------------------------- queue
class QueueView(QWidget):
    """Upcoming scheduled prints and recent results."""

    def __init__(self, main, scheduler: PrintScheduler, parent=None) -> None:
        super().__init__(parent)
        self.main = main
        self.scheduler = scheduler
        self.setStyleSheet(VIEWS_QSS + QUEUE_QSS)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("Print queue")
        title.setStyleSheet("font-size: 16px; color: #ffffff;")
        head.addWidget(title, 1)
        add = QToolButton()
        add.setObjectName("flat")
        add.setText("+ Schedule")
        add.setStyleSheet(f"color: {GREEN};")
        add.setCursor(Qt.PointingHandCursor)
        add.setToolTip("Schedule what's sliced in Studio to print later")
        add.clicked.connect(lambda: open_schedule_dialog(main, scheduler))
        head.addWidget(add)
        column.addLayout(head)
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        column.addWidget(line)

        self._list = QWidget()
        self._rows = QVBoxLayout(self._list)
        self._rows.setContentsMargins(0, 0, 4, 0)
        self._rows.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self._list)
        column.addWidget(scroll, 1)

        scheduler.changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    def refresh(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        now = datetime.now()
        upcoming = self.scheduler.upcoming()
        if not upcoming:
            empty = QLabel("Nothing scheduled. Slice a plate, then use ▴ on Studio's Print button or + Schedule.")
            empty.setObjectName("dim")
            empty.setWordWrap(True)
            self._rows.addWidget(empty)
        for job in upcoming:
            self._rows.addWidget(self._job_card(job, now))
        recent = self.scheduler.history()
        if recent:
            head = QHBoxLayout()
            caption = QLabel("RECENT")
            caption.setObjectName("section")
            head.addWidget(caption, 1)
            clear = QToolButton()
            clear.setObjectName("mini")
            clear.setText("Clear")
            clear.clicked.connect(self.scheduler.clear_history)
            head.addWidget(clear)
            holder = QWidget()
            holder.setLayout(head)
            head.setContentsMargins(0, 8, 0, 0)
            self._rows.addWidget(holder)
            for job in recent:
                self._rows.addWidget(self._history_row(job, now))
        self._rows.addStretch(1)

    def _job_card(self, job: ScheduledPrint, now: datetime) -> QWidget:
        card = QFrame()
        card.setObjectName("job")
        card.setProperty("state", job.state)
        column = QVBoxLayout(card)
        column.setContentsMargins(10, 8, 6, 8)
        column.setSpacing(2)
        top = QHBoxLayout()
        when = QLabel(schedule.describe_when(job.when, now))
        when.setObjectName("jobTime")
        top.addWidget(when, 1)
        state = QLabel("Sending…" if job.state == SENDING else schedule.describe_in(job.when, now))
        state.setObjectName("state")
        state.setProperty("state", job.state)
        top.addWidget(state)
        column.addLayout(top)
        info = QLabel(" · ".join(filter(None, [printer_name(job.printer_row), job.label])))
        info.setObjectName("jobInfo")
        column.addWidget(info)
        if job.state != SENDING:
            actions = QHBoxLayout()
            actions.setSpacing(2)
            actions.addStretch(1)
            for text, tip, slot in (
                ("Print now", "Send it to Studio now", lambda: self.scheduler.send_now(job.id)),
                ("Edit", "Change the time or printer", lambda: open_schedule_dialog(self.main, self.scheduler, job)),
                ("Cancel", "Remove it from the queue", lambda: self.scheduler.cancel(job.id)),
            ):
                button = QToolButton()
                button.setObjectName("mini")
                button.setText(text)
                button.setToolTip(tip)
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(slot)
                actions.addWidget(button)
            column.addLayout(actions)
        return card

    def _history_row(self, job: ScheduledPrint, now: datetime) -> QWidget:
        row = QWidget()
        column = QVBoxLayout(row)
        column.setContentsMargins(2, 2, 2, 4)
        column.setSpacing(1)
        top = QHBoxLayout()
        name = QLabel(" · ".join(filter(None, [schedule.describe_when(job.when, now), job.label or printer_name(job.printer_row)])))
        name.setStyleSheet("font-size: 12px;")
        top.addWidget(name, 1)
        state = QLabel(STATE_LABELS.get(job.state, job.state))
        state.setObjectName("state")
        state.setProperty("state", job.state)
        top.addWidget(state)
        column.addLayout(top)
        if job.detail and job.state in (FAILED, MISSED):
            detail = QLabel(job.detail)
            detail.setObjectName("jobInfo")
            detail.setWordWrap(True)
            column.addWidget(detail)
        return row


class QueueWindow(QDialog):
    """The queue as a window of its own (works without Studio docking)."""

    def __init__(self, main, scheduler: PrintScheduler) -> None:
        super().__init__(None)
        self.setObjectName("surface")
        self.setStyleSheet(VIEWS_QSS)
        self.setWindowTitle("Print queue")
        self.resize(380, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(QueueView(main, scheduler))


# ---------------------------------------------------------------------- in Studio
class _Embedded(QWidget):
    """A native top-level that gets parented into a Studio window."""

    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.foreign: QWindow | None = None
        self.parent_hwnd = 0

    def embed(self, parent_hwnd: int, rect: win.Rect) -> None:
        self.parent_hwnd = parent_hwnd
        self.foreign = QWindow.fromWinId(parent_hwnd)
        self.winId()  # create the native window before reparenting it
        self.windowHandle().setParent(self.foreign)
        scale = win.dpi_scale(parent_hwnd)
        x, y, w, h = rect
        self.setGeometry(round(x / scale), round(y / scale), round(w / scale), round(h / scale))
        self.show()
        win.place(int(self.winId()), rect, raise_to_top=True)

    def release(self) -> None:
        if win.is_window(int(self.winId())):
            self.hide()
            handle = self.windowHandle()
            if handle is not None:
                handle.setParent(None)
        self.deleteLater()
        if self.foreign is not None:
            self.foreign.deleteLater()


class _ArrowButton(_Embedded):
    """The ▴ half of the split Print button."""

    def __init__(self, on_click) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Schedule this print")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.button = QToolButton()
        self.button.setText("▴")
        self.button.clicked.connect(on_click)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.button)
        self.setStyleSheet(
            f"QWidget {{ background: {GREEN}; }}"
            "QToolButton { border: none; border-left: 1px solid rgba(0, 0, 0, 45);"
            " border-top-right-radius: 4px; border-bottom-right-radius: 4px;"
            " color: #ffffff; font-size: 13px; background: transparent; }"
            "QToolButton:hover { background: rgba(255, 255, 255, 50); }"
        )

    def set_count(self, scheduled: int) -> None:
        self.setToolTip(
            f"Schedule this print ({scheduled} scheduled)" if scheduled else "Schedule this print"
        )


class _QueuePanel(_Embedded):
    def __init__(self, main, scheduler: PrintScheduler) -> None:
        super().__init__()
        self.setObjectName("surface")
        self.setStyleSheet(VIEWS_QSS + f"QWidget#surface {{ background: {CARD}; border-top: 1px solid {DIVIDER}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 8, 10)
        layout.addWidget(QueueView(main, scheduler))


def queue_rect(sidebar: win.Rect, rows: list[win.Rect]) -> win.Rect | None:
    """Where the queue goes in the "My Printer" column (column-relative):
    its lower part, below the printer rows; None when there's no room."""
    _x, _y, w, h = sidebar
    rows_bottom = max((r[1] + r[3] for r in rows), default=60)
    top = max(rows_bottom + 16, int(h * 0.4))
    height = h - top - 6
    if height < QUEUE_MIN_H:
        return None
    return (6, top, w - 12, height)


class StudioScheduleDock(QObject):
    """Keeps the ▴ on Studio's Print button and the queue in its printer column."""

    def __init__(self, main, scheduler: PrintScheduler) -> None:
        super().__init__(main)
        self.main = main
        self.scheduler = scheduler
        self.arrow: _ArrowButton | None = None
        self.queue: _QueuePanel | None = None
        self._top = 0
        self._print_button = 0
        self._sidebar = 0
        self._rows: list[int] = []
        self._last_find = 0.0
        self._menu: QMenu | None = None
        self._arrow_rect: win.Rect | None = None
        scheduler.changed.connect(self._sync_badge)
        QApplication.instance().aboutToQuit.connect(self.detach)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        if sys.platform == "win32":
            self._timer.start(TICK_MS)

    def _tick(self) -> None:
        if not self.main.settings.dock_in_studio:
            self.detach()
            return
        now = time.monotonic()
        if not win.is_window(self._top):
            self.detach()
            self._top = 0
            if now - self._last_find < FIND_EVERY_S * 1.5:
                return
            self._last_find = now
            self._top = win.find_studio() or 0
            if not self._top:
                return
        if now - self._last_find >= FIND_EVERY_S:
            self._last_find = now
            self._find_parts()
        self._place_arrow()
        self._place_queue()

    def _find_parts(self) -> None:
        button = win.find_print_button(self._top)
        if button:
            self._print_button = button
        layout = win.locate_layout(self._top)
        if layout is not None and layout.sidebar:
            self._sidebar = layout.sidebar
            self._rows = layout.printer_rows

    # ------------------------------------------------------------------ ▴
    def _place_arrow(self) -> None:
        button = self._print_button
        if not win.is_window(button):
            self._drop_arrow()
            return
        bar = win.parent(button)
        if self.arrow is not None and (self.arrow.parent_hwnd != bar or not win.is_window(int(self.arrow.winId()))):
            self._drop_arrow()
        x, y, w, h = win.rect_in_parent(button, bar)
        rect = (x + w - h, y, h, h)
        if self.arrow is None:
            win.ensure_clips_children(bar)
            self.arrow = _ArrowButton(self._open_menu)
            self.arrow.embed(bar, rect)
            self._arrow_rect = rect
            self._sync_badge()
        # Print must not paint over the arrow sitting on top of it.
        win.ensure_clips_siblings(button)
        hwnd = int(self.arrow.winId())
        needs_raise = not win.is_above(hwnd, button)
        if needs_raise or rect != self._arrow_rect:
            win.place(hwnd, rect, raise_to_top=needs_raise)
            self._arrow_rect = rect

    def _drop_arrow(self) -> None:
        if self.arrow is not None:
            self.arrow.release()
            self.arrow = None

    def _sync_badge(self) -> None:
        if self.arrow is not None:
            self.arrow.set_count(len(self.scheduler.upcoming()))

    def _open_menu(self) -> None:
        menu = QMenu()
        menu.setStyleSheet(VIEWS_QSS + QUEUE_QSS)
        menu.addAction("Schedule this print…", lambda: open_schedule_dialog(self.main, self.scheduler))
        upcoming = self.scheduler.upcoming()
        if upcoming:
            menu.addSection("Scheduled")
            now = datetime.now()
            for job in upcoming[:6]:
                text = " · ".join(
                    filter(None, [schedule.describe_when(job.when, now), printer_name(job.printer_row), job.label])
                )
                action = menu.addAction(text, lambda job=job: open_schedule_dialog(self.main, self.scheduler, job))
                action.setEnabled(job.state != SENDING)
        menu.addSeparator()
        menu.addAction("Open print queue", self.main.show_print_queue)
        self._menu = menu
        # Drop *up* from the arrow: find the arrow's top-right in logical
        # coordinates from the click position (the arrow lives inside a
        # foreign window, so Qt's own mapToGlobal can't be trusted).
        hint = menu.sizeHint()
        cursor = QCursor.pos()
        hwnd = int(self.arrow.winId()) if self.arrow is not None else 0
        if hwnd:
            ax, ay, aw, _ah = win.window_rect(hwnd)
            px, py = win.cursor_pos()
            scale = win.dpi_scale(hwnd)
            right = cursor.x() + round((ax + aw - px) / scale)
            top = cursor.y() - round((py - ay) / scale)
            menu.popup(QPoint(right - hint.width(), top - hint.height() - 2))
        else:
            menu.popup(cursor - QPoint(hint.width(), hint.height()))

    # ------------------------------------------------------------------ queue
    def _place_queue(self) -> None:
        sidebar = self._sidebar
        if not win.is_window(sidebar):
            self._drop_queue()
            return
        if self.queue is not None and (self.queue.parent_hwnd != sidebar or not win.is_window(int(self.queue.winId()))):
            self._drop_queue()
        rows = [win.rect_in_parent(r, sidebar) for r in self._rows if win.is_window(r)]
        _sx, _sy, sw, sh = win.window_rect(sidebar)
        rect = queue_rect((0, 0, sw, sh), rows)
        if rect is None:
            if self.queue is not None:
                self.queue.hide()
            return
        if self.queue is None:
            win.ensure_clips_children(sidebar)
            self.queue = _QueuePanel(self.main, self.scheduler)
            self.queue.embed(sidebar, rect)
            return
        if not self.queue.isVisible():
            self.queue.show()
        hwnd = int(self.queue.winId())
        if win.rect_in_parent(hwnd, sidebar) != rect:
            win.place(hwnd, rect, raise_to_top=True)

    def _drop_queue(self) -> None:
        if self.queue is not None:
            self.queue.release()
            self.queue = None

    def detach(self) -> None:
        self._drop_arrow()
        self._drop_queue()
