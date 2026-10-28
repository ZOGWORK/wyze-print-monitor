"""Scheduled prints: press Studio's Print at the planned time.

When a job comes due, eufyMake Studio is driven exactly as you would:

1. switch to its Slice tab (if Print isn't already on screen);
2. press Print — or Slice first, when the plate still needs slicing, and
   then the Print that replaces it;
3. in "Choose a Printer", click the job's printer row and check (from
   the dialog's own pixels) that it really is the selected one;
4. press the dialog's Print, and count it as sent once the dialog closes;
5. go back to the Device tab, where the camera panels live.

Every press is posted (never sent), one step per timer tick, so a busy
Studio can't freeze the app. Anything unexpected stops the run without
guessing: a dialog we opened but can't use is cancelled, and you get the
alarm, a notification and Studio brought to the front. A job that comes
due long after its time (app closed, PC asleep) is marked missed rather
than printed by surprise.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from .. import schedule, studio_win32 as win
from ..schedule import FAILED, MISSED, SCHEDULED, SENDING, STARTED, ScheduledPrint
from .alarm import Alarm

TICK_MS = 5000  # how often to look for due jobs
STEP_MS = 500  # pace of a run's steps
TAB_WAIT_S = 8.0  # Slice tab switched, Print not on screen yet
SLICE_WAIT_S = 180.0  # pressed Slice/Print, no "Choose a Printer" yet
DIALOG_READ_S = 5.0  # the dialog is up but not laid out yet
SELECT_SETTLE_S = 0.8  # after clicking a printer row
SELECT_GIVE_UP_S = 4.0
SEND_WAIT_S = 20.0  # pressed the dialog's Print, waiting for it to close
BACK_TO_DEVICE_S = 3.0


@dataclass
class _Run:
    job: ScheduledPrint
    top: int
    phase: str  # "tab" | "print" | "dialog" | "select" | "send" | "back"
    since: float
    pressed: int = 0  # the Slice/Print button pressed last
    dialog: win.ChoosePrinter | None = None
    row_presses: int = 0


def printer_name(row: int) -> str:
    return f"Printer {row}"


class PrintScheduler(QObject):
    changed = Signal()
    notify = Signal(str, str)  # title, message (tray notification)

    def __init__(self, main) -> None:
        super().__init__(main)
        self.main = main
        self.jobs = schedule.from_dicts(main.settings.print_queue)
        now = datetime.now()
        for job in self.jobs:
            if job.state == SENDING:  # the app closed mid-run
                job.finish(FAILED, now, "The app closed while sending it to Studio.")
        self.alarm = Alarm(self)
        self._run: _Run | None = None
        self._step_timer = QTimer(self)
        self._step_timer.setInterval(STEP_MS)
        self._step_timer.timeout.connect(self._step)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------ queue
    @property
    def busy(self) -> bool:
        return self._run is not None

    def upcoming(self) -> list[ScheduledPrint]:
        return schedule.upcoming(self.jobs)

    def history(self) -> list[ScheduledPrint]:
        return schedule.history(self.jobs)

    def find(self, job_id: str) -> ScheduledPrint | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, at: datetime, printer_row: int, label: str = "") -> ScheduledPrint:
        job = schedule.new_job(at, printer_row, label)
        self.jobs.append(job)
        self._save()
        QTimer.singleShot(0, self._tick)  # "now" jobs go right away
        return job

    def reschedule(self, job_id: str, at: datetime, printer_row: int, label: str) -> None:
        job = self.find(job_id)
        if job is None or job.state != SCHEDULED:
            return
        job.at = at.replace(microsecond=0).isoformat()
        job.printer_row = max(1, printer_row)
        job.label = label.strip()
        self._save()
        QTimer.singleShot(0, self._tick)

    def send_now(self, job_id: str) -> None:
        job = self.find(job_id)
        if job is not None and job.state == SCHEDULED:
            self.reschedule(job_id, datetime.now(), job.printer_row, job.label)

    def cancel(self, job_id: str) -> None:
        job = self.find(job_id)
        if job is not None and job.state == SCHEDULED:
            job.finish(schedule.CANCELLED, datetime.now())
            self._save()

    def clear_history(self) -> None:
        self.jobs = [j for j in self.jobs if j.pending]
        self._save()

    def _save(self) -> None:
        self.jobs = schedule.prune(self.jobs)
        self.main.settings.print_queue = schedule.to_dicts(self.jobs)
        self.main.save_settings_soon()
        self.changed.emit()

    # ------------------------------------------------------------------ timing
    def _tick(self) -> None:
        if self._run is not None:
            return
        now = datetime.now()
        start, missed = schedule.due(self.jobs, now)
        for job in missed:
            when = schedule.describe_when(job.when, now)
            job.finish(MISSED, now, f"It was due {when}, but the app wasn't running then (or the PC was asleep).")
            self._tell(f"Scheduled print missed: {self._title(job)}", job.detail, urgent=False)
        if missed:
            self._save()
        if start is not None:
            self._begin(start)

    # ------------------------------------------------------------------ the run
    def _begin(self, job: ScheduledPrint) -> None:
        job.state = SENDING
        self._save()
        top = win.find_studio()
        if not top:
            self._fail(job, 0, "eufyMake Studio isn't open, so there was nothing to print from.")
            return
        if win.find_choose_printer(top):
            self._fail(job, top, "Studio's Choose a Printer window was already open, so nothing was pressed.")
            return
        win.restore(top)
        self._run = _Run(job, top, "tab", time.monotonic())
        button = win.find_print_button(top)
        if button:
            self._run.phase = "print"
        else:
            tab = win.find_tab(top, win.SLICE_TAB_LABEL)
            if tab:
                win.press(tab)
        self._step_timer.start()

    def _step(self) -> None:
        run = self._run
        if run is None:
            self._step_timer.stop()
            return
        if not win.is_window(run.top):
            self._fail(run.job, 0, "eufyMake Studio closed while the print was being sent.")
            return
        elapsed = time.monotonic() - run.since
        getattr(self, f"_step_{run.phase}")(run, elapsed)

    def _next(self, run: _Run, phase: str) -> None:
        run.phase = phase
        run.since = time.monotonic()

    def _step_tab(self, run: _Run, elapsed: float) -> None:
        if win.find_print_button(run.top):
            self._next(run, "print")
        elif elapsed > TAB_WAIT_S:
            self._fail(run.job, run.top, "Couldn't find the Print button on Studio's Slice tab. Is a plate loaded?")

    def _step_print(self, run: _Run, elapsed: float) -> None:
        button = win.find_print_button(run.top)
        if button and win.is_enabled(button):
            win.press(button)
            run.pressed = button
            self._next(run, "dialog")
        elif elapsed > SLICE_WAIT_S:
            self._fail(run.job, run.top, "Studio's Print button stayed greyed out. Is the plate sliced?")

    def _step_dialog(self, run: _Run, elapsed: float) -> None:
        dialog = win.find_choose_printer(run.top)
        if dialog is None:
            # The pressed button was Slice: once slicing is done its pair is
            # replaced by Export / Print, so press that Print.
            button = win.find_print_button(run.top)
            if button and button != run.pressed and win.is_enabled(button):
                win.press(button)
                run.pressed = button
            elif elapsed > SLICE_WAIT_S:
                self._fail(run.job, run.top, "Studio didn't open its Choose a Printer window. Is the plate sliced?")
            return
        parts = win.read_choose_printer(dialog)
        if parts is None:
            if elapsed > SLICE_WAIT_S + DIALOG_READ_S:
                self._fail(run.job, run.top, "Studio's Choose a Printer window didn't look as expected, so nothing was pressed in it.")
            return
        index = run.job.printer_row - 1
        if index >= len(parts.rows):
            win.press(parts.cancel)
            self._fail(
                run.job, run.top,
                f"Studio lists {len(parts.rows)} printer(s), so there's no {printer_name(run.job.printer_row)}.",
            )
            return
        run.dialog = parts
        win.press(parts.rows[index])
        run.row_presses = 1
        self._next(run, "select")

    def _step_select(self, run: _Run, elapsed: float) -> None:
        parts = run.dialog
        if not win.is_window(parts.dialog):
            self._fail(run.job, run.top, "Studio's Choose a Printer window closed before the print was sent.")
            return
        if elapsed < SELECT_SETTLE_S:
            return
        index = run.job.printer_row - 1
        selected = win.selected_rows(parts.rows)
        if selected is not None and selected[index] and sum(selected) == 1:
            win.press(parts.print)
            self._next(run, "send")
        elif elapsed > SELECT_GIVE_UP_S:
            if run.row_presses < 2:
                win.press(parts.rows[index])
                run.row_presses += 1
                self._next(run, "select")
                return
            win.press(parts.cancel)
            self._fail(
                run.job, run.top,
                f"{printer_name(run.job.printer_row)} couldn't be selected in Studio. "
                "It may be disconnected or busy.",
            )

    def _step_send(self, run: _Run, elapsed: float) -> None:
        if not win.is_window(run.dialog.dialog) or not win.is_visible(run.dialog.dialog):
            job = run.job
            job.finish(STARTED, datetime.now(), f"Sent to {printer_name(job.printer_row)}.")
            self._save()
            self._tell(f"Scheduled print sent: {self._title(job)}", f"Studio sent it to {printer_name(job.printer_row)}.", urgent=False)
            self._next(run, "back")
        elif elapsed > SEND_WAIT_S:
            self._fail(run.job, run.top, "Studio didn't send the print after Print was pressed. Its window is still open: check it.")

    def _step_back(self, run: _Run, elapsed: float) -> None:
        if elapsed >= BACK_TO_DEVICE_S:
            tab = win.find_tab(run.top, win.DEVICE_TAB_LABEL)
            if tab and not win.find_choose_printer(run.top):
                win.press(tab)  # the camera panels and print progress live there
            self._end()

    def _end(self) -> None:
        self._run = None
        self._step_timer.stop()
        QTimer.singleShot(0, self._tick)  # another job may be due

    def _fail(self, job: ScheduledPrint, top: int, reason: str) -> None:
        job.finish(FAILED, datetime.now(), reason)
        self._end()
        self._save()
        if top:
            win.bring_to_front(top)
        self._tell(f"Scheduled print didn't start: {self._title(job)}", reason, urgent=True)

    # ------------------------------------------------------------------ telling
    @staticmethod
    def _title(job: ScheduledPrint) -> str:
        return job.label or printer_name(job.printer_row)

    def _tell(self, title: str, message: str, urgent: bool) -> None:
        self.notify.emit(title, message)
        self.main._status(f"{title}. {message}", 15000)
        self.alarm.play(self.main.settings.ai_sound, urgent)
