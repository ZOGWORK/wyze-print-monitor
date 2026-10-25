"""The print watcher's brain on the GUI side: when to watch, alert, pause.

Rules (the user's):

* Alert whenever something *might* be wrong — the model's warning level, or
  an unusual sound. Alerts show on the video, in a Windows notification,
  with a beep and a saved snapshot.
* Pause through Studio's Pause button only when the model is very sure
  (its failure level, held for several checks — see failure_detect), and
  only if "auto-pause" is on.
* Never stop a print on its own: Stop is a button on the alert, and it asks
  first.

Pressing anything in Studio is guarded twice over:

* **Right printer.** Studio's Device tab shows the controls of whichever
  printer is selected in its list; we only use them while that is the
  camera's printer (Settings → "Printer with camera"), and re-check right
  before every press.
* **Confirmed.** A press is only a request. A pause counts once Studio shows
  Resume; until then it's "pausing", it's retried once, and if Studio still
  doesn't pause we say so loudly instead of pretending.

Print state (running / paused / finished) is only read while we can see the
camera printer's controls. Switching Studio to another tab or printer, or
closing it, doesn't end the print as far as we're concerned. Until Studio
has shown a print there's no way to know one is running, so we don't watch
(an empty bed with a cable in view isn't a failed print), unless "watch
without Studio" is on: then we watch whenever the stream is live (alerts
only: there's nothing to pause through).

Only what the model sees inside the camera's watch area (the print bed,
if the user marked it) counts.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field

import cv2
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from .. import failure_model, studio_win32
from ..config import redact_credentials_in_url, save_settings
from ..failure_detect import (
    CHECK_INTERVALS_S,
    FAILURE,
    PAUSE_AFTER_S,
    SENSITIVITY,
    WARNING,
    Area,
    Baseline,
    FailureScorer,
    clean_area,
    draw_detections,
)
from ..print_watch import SoundWorker, VisionResult, VisionWorker
from ..sound_watch import describe as describe_sound
from .alarm import Alarm

STUDIO_POLL_MS = 3000
# Studio's card shows "N/A" both briefly (reconnecting, starting a print)
# and once a print is over; only a lasting "N/A" means the print ended.
IDLE_AFTER_UNREADABLE_S = 60.0
PAUSE_POLL_MS = 1000  # while waiting for Studio to confirm a pause
PAUSE_RETRY_S = 6.0  # no Resume button yet: press Pause once more
PAUSE_GIVE_UP_S = 15.0  # still running: tell the user to pause it
WARNING_REPEAT_S = 15 * 60  # don't nag: one warning alert per this long
LOOKS_FINE_SNOOZE_S = 30 * 60
BASELINE_SAVE_EVERY = 30  # checks


def camera_key(url: str) -> str:
    """A stable id for a camera stream that doesn't contain its password."""
    if not url:
        return ""
    return hashlib.sha256(redact_credentials_in_url(url).encode("utf-8")).hexdigest()[:16]


def _watch_areas(settings) -> dict:
    """settings.ai_watch_area: {camera_key: [x1, y1, x2, y2]} (per camera)."""
    data = settings.ai_watch_area
    return data if isinstance(data, dict) else {}


def _baselines(settings) -> dict:
    """settings.ai_baseline: {camera_key: Baseline.to_dict()} (per camera)."""
    data = settings.ai_baseline
    return data if isinstance(data, dict) else {}


@dataclass
class Alert:
    # "warning" | "failure" | "pausing" | "paused" | "pause_failed" | "sound"
    severity: str
    title: str
    message: str
    snapshot: str = ""
    can_pause: bool = False
    can_resume: bool = False
    can_stop: bool = False
    at: float = 0.0


@dataclass
class StudioView:
    """What we can see of Studio right now."""

    running: bool = False  # Studio is open
    ours: bool = False  # its Device tab shows the camera's printer
    controls: dict = field(default_factory=dict)  # {"pause"|"resume"|"stop": hwnd}, only when ours
    # Studio's print status when ours: "running" | "paused" | "idle" |
    # "blank" (its "N/A" card) | None (unknown text, or no card to read)
    status: str | None = None


class StudioObserver:
    """Reads Studio's print controls for the camera's printer only.

    The selected printer is read from the screen (the row's color), which
    is impossible while another window covers Studio's list: then the last
    confirmed selection stands — the selection can only change by clicking
    in Studio, which uncovers it.
    """

    def __init__(self) -> None:
        self._selected_ok: bool | None = None

    def observe(self, configured_row: int) -> StudioView:
        top = studio_win32.find_studio()
        if not top:
            self._selected_ok = None
            return StudioView()
        layout = studio_win32.locate_layout(top)
        if layout is None:  # Device tab not showing (or Studio still starting)
            return StudioView(running=True)
        colors = [studio_win32.row_color(row) for row in layout.printer_rows]
        selected = studio_win32.selected_row_index(colors)
        self._selected_ok = studio_win32.camera_printer_selected(selected, configured_row, self._selected_ok)
        if not studio_win32.may_control_print(self._selected_ok, configured_row, len(layout.printer_rows)):
            return StudioView(running=True)
        status, controls = studio_win32.read_print_state(layout.host)
        return StudioView(True, True, controls, status)


@dataclass
class _PendingPause:
    started: float  # moved forward while Studio's controls can't be seen
    last_press: float
    last_tick: float
    presses: int
    auto: bool  # the AI asked (vs. the user's Pause button)
    snapshot: str = ""


class PrintWatch(QObject):
    alert_changed = Signal(object)  # Alert or None
    popup_requested = Signal(object)  # Alert: show the pop-up card (setting on)
    notify = Signal(str, str)  # title, message (tray notification)
    status_changed = Signal()  # status_text() / risk changed
    download_progress = Signal(int, int)
    download_finished = Signal(bool, str)  # ok, message

    def __init__(self, main) -> None:
        super().__init__(main)
        self.main = main
        self.alert: Alert | None = None
        self.risk = 0.0
        self.last_check = 0.0
        self._vision: VisionWorker | None = None
        self._retired: list = []  # stopped workers still finishing
        self._sound: SoundWorker | None = None
        self._sound_status = ""
        self._url = ""
        self._camera = ""
        self._scorer = FailureScorer()
        self._observer = StudioObserver()
        self._view = StudioView()
        self._printing: bool | None = None  # None: unknown (no Studio)
        self._print_paused = False
        self._unreadable_since: float | None = None
        self._pause: _PendingPause | None = None
        self._unconfirmed_pause: _PendingPause | None = None  # gave up waiting on this one
        self._paused_by_us = False
        self._last_warning = -1e9
        self._snooze_until = 0.0
        self._checks = 0
        self._save_baseline_soon = False
        self._error = ""
        self._download: threading.Thread | None = None
        self._download_cancel = False
        self.alarm = Alarm(self)
        self._poll = QTimer(self)
        self._poll.setInterval(STUDIO_POLL_MS)
        self._poll.timeout.connect(self._poll_studio)
        # The model can arrive after Settings closed: start watching then.
        self.download_finished.connect(self._on_download_finished)

    # ------------------------------------------------------------------ setup
    @property
    def settings(self):
        return self.main.settings

    def model_ready(self) -> bool:
        return failure_model.is_installed()

    def enabled(self) -> bool:
        return bool(self.settings.ai_watch) and self.model_ready()

    def apply_settings(self) -> None:
        """(Re)start or stop the workers to match the settings."""
        self._restart_workers()

    def set_stream(self, url: str | None) -> None:
        """The video stream started (url) or stopped (None)."""
        url = url or ""
        if url == self._url:
            return
        self._url = url
        self._switch_camera(camera_key(url))
        self._restart_workers()

    def _switch_camera(self, key: str) -> None:
        """Another camera (or none): nothing learned or decided about the
        previous one applies — evidence, alerts, cooldowns, requests."""
        if key == self._camera:
            return
        self._stop_workers()  # saves the old camera's baseline
        self._camera = key
        stored = _baselines(self.settings).get(key) if key else None
        self._scorer = FailureScorer(Baseline.from_dict(stored or {}))
        self._pause = self._unconfirmed_pause = None
        self._poll.setInterval(STUDIO_POLL_MS)
        self._paused_by_us = False
        self._snooze_until = 0.0
        self._last_warning = -1e9
        self.risk = 0.0
        self.last_check = 0.0
        self._set_alert(None)

    def _restart_workers(self) -> None:
        if self._pause is not None and self._pause.auto and not self._auto_pause_allowed():
            self._cancel_pause()  # watching or auto-pause was just turned off
        self._stop_workers()
        self._scorer.sensitivity = SENSITIVITY.get(self.settings.ai_sensitivity, 1.0)
        interval = self.check_interval()
        self._scorer.pause_after_s = self.pause_after()
        self._scorer.interval_s = interval
        if not self._url:
            self._poll.stop()
            self.status_changed.emit()
            return
        self._error = ""
        if self.enabled():
            self._vision = VisionWorker(
                failure_model.model_path(),
                self._latest_frame,
                self._scorer,
                interval_s=interval,
                area=self.watch_area(),
                parent=self,
            )
            self._vision.checked.connect(self._on_checked)
            self._vision.failed.connect(self._on_failed)
            self._vision.start(VisionWorker.LowPriority)
        if self.settings.ai_watch and self.settings.sound_alerts:
            self._sound = SoundWorker(self._url, parent=self)
            self._sound.unusual.connect(self._on_sound)
            self._sound.status.connect(self._on_sound_status)
            self._sound.start(SoundWorker.LowPriority)
        if self._vision or self._sound:
            self._poll.start()
            self._poll_studio()
        self.status_changed.emit()

    def _stop_workers(self) -> None:
        for worker in (self._vision, self._sound):
            if worker is not None:
                for signal in (getattr(worker, n, None) for n in ("checked", "failed", "unusual", "status")):
                    if signal is not None:
                        try:
                            signal.disconnect()
                        except (RuntimeError, TypeError):
                            pass
                worker.request_stop()
                if worker.isRunning():
                    # Never let a running QThread be destroyed: keep it until done.
                    self._retired.append(worker)
                    worker.finished.connect(lambda w=worker: self._retired.remove(w) if w in self._retired else None)
        self._vision = self._sound = None
        self._sound_status = ""
        self._save_baseline()

    def shutdown(self) -> None:
        self.alarm.stop()
        self._poll.stop()
        self._download_cancel = True
        self._stop_workers()
        for worker in list(self._retired):
            worker.wait(12000)  # each network wait is bounded by its timeout
        self._retired.clear()

    def _latest_frame(self):
        reader = self.main._reader
        return reader.last_frame if reader is not None else None

    def _save_baseline(self) -> None:
        if not self._camera:
            return
        data = self._scorer.baseline.to_dict()
        baselines = dict(_baselines(self.settings))
        if baselines.get(self._camera) != data:
            baselines[self._camera] = data
            self.settings.ai_baseline = baselines
            try:
                save_settings(self.settings)
            except OSError:
                pass

    def watch_area(self) -> Area | None:
        """Where the print is in this camera's picture (None: everywhere)."""
        return clean_area(_watch_areas(self.settings).get(self._camera)) if self._camera else None

    def has_camera(self) -> bool:
        return bool(self._camera)

    def latest_frame(self):
        return self._latest_frame()

    def set_watch_area(self, area: Area | None) -> None:
        """Watch only ``area`` from now on. What was normal for the whole
        picture (and "Looks fine" taught about it) doesn't apply to part of
        it, so this camera's normal starts over."""
        if not self._camera:
            return
        area = clean_area(area) if area is not None else None
        if area == self.watch_area():
            return
        areas = dict(_watch_areas(self.settings))
        if area is None:
            areas.pop(self._camera, None)
        else:
            areas[self._camera] = list(area)
        self.settings.ai_watch_area = areas
        self._stop_workers()  # saves the old baseline
        # A new scorer: a retired worker may still be using the old one.
        self._scorer = FailureScorer()
        self.risk = 0.0
        self._save_baseline()
        self._restart_workers()

    def check_interval(self) -> int:
        value = self.settings.ai_check_interval_s
        return value if value in CHECK_INTERVALS_S else CHECK_INTERVALS_S[0]

    def pause_after(self) -> int:
        value = self.settings.ai_pause_after_s
        return value if value in PAUSE_AFTER_S else PAUSE_AFTER_S[0]

    def silence(self) -> None:
        self.alarm.stop()

    def test_alert(self) -> bool:
        """Show / play exactly what a failure alert does, without a failure.
        Refused (False) while a real alert is showing: a test must never
        replace it."""
        if self.alert is not None and self.alert.severity != "test":
            return False
        self._raise(
            "test",
            "Test alert",
            "This is how Print watch tells you a print looks like it failed. Click Dismiss to stop it.",
            None,
            urgent=True,
        )
        return True

    def _reset_session(self) -> None:
        """Fresh short-term history for the model and the sound check. Both
        belong to their worker threads while those run, so ask them."""
        if self._vision is not None:
            self._vision.reset_requested = True
        else:
            self._scorer.reset_session()
        if self._sound is not None:
            self._sound.reset_requested = True

    # ------------------------------------------------------------------ Studio
    def _observe(self) -> StudioView:
        self._view = self._observer.observe(self.settings.dock_printer_row)
        return self._view

    def _poll_studio(self) -> None:
        view = self._observe()
        if not view.running:
            # Studio closed: a print it showed carries on without it, so keep
            # what we knew (nothing, if it never showed one). But "idle" is
            # stale once Studio's gone: with "watch without Studio" on, a
            # print started some other way must be watched.
            if self._printing is False and self.settings.ai_watch_without_studio:
                self._set_print_state(None, False)
        elif view.ours and view.status in ("running", "paused", "idle"):
            # The status label, not the buttons: while heating there's no
            # Pause button yet, but the print is under way.
            self._unreadable_since = None
            self._set_print_state(view.status != "idle", view.status == "paused")
        elif view.ours and view.status == "blank":
            # The camera printer's card says "N/A": a blink, or the print's
            # over. Only a lasting one ends it.
            now = time.monotonic()
            if self._unreadable_since is None:
                self._unreadable_since = now
            elif now - self._unreadable_since >= IDLE_AFTER_UNREADABLE_S:
                self._set_print_state(False, False)
        # else: Studio is open but not showing the camera printer, or shows
        # a status we don't know: nothing new is known — keep what we had.
        if not (view.ours and view.status == "blank"):
            self._unreadable_since = None
        self._advance_pause()
        idle = self._printing is False or self._print_paused or not self._watching_unknown()
        for worker in (self._vision, self._sound):
            if worker is not None:
                worker.paused = idle
        self._refresh_alert_actions()
        self.status_changed.emit()

    def _watching_unknown(self) -> bool:
        """Whether to watch now, as far as an unknown print state goes."""
        return self._printing is not None or bool(self.settings.ai_watch_without_studio)

    def _set_print_state(self, printing: bool | None, paused: bool) -> None:
        was_printing, was_paused = self._printing, self._print_paused
        paused = bool(printing) and paused
        if paused and not was_paused and self._unconfirmed_pause is not None:
            # We'd given up waiting, but the pause did happen after all.
            request, self._unconfirmed_pause = self._unconfirmed_pause, None
            self._paused_by_us = True
            self._raise_paused(request)
        if printing and not was_printing:
            # A print started (or Studio appeared mid-print): fresh session.
            self._reset_session()
            self._paused_by_us = False
            self._snooze_until = 0.0
            self._last_warning = -1e9
        elif was_printing and printing is False:
            # The print ended (finished or stopped): its alerts are moot.
            self._pause = self._unconfirmed_pause = None
            self._paused_by_us = False
            self._set_alert(None)
        if was_paused and not paused and printing:
            # Resumed (here or in Studio): the AI watches afresh, so it
            # doesn't re-pause on the old evidence. Same print, so the sound
            # check keeps what it learned (and any "Looks fine" tolerance).
            self._paused_by_us = False
            self._vision_reset()
            if self.alert is not None and self.alert.severity in ("paused", "failure", "pause_failed"):
                self._set_alert(None)
        self._printing = printing
        self._print_paused = paused

    def _verified_control(self, kind: str) -> int | None:
        """The camera printer's ``kind`` button, re-checked right now."""
        view = self._observe()
        return view.controls.get(kind) if view.ours else None

    def _refresh_alert_actions(self) -> None:
        if self.alert is None:
            return
        c = self._view.controls if self._view.ours else {}
        actions = ("pause" in c and self._pause is None, "resume" in c, "stop" in c)
        if actions != (self.alert.can_pause, self.alert.can_resume, self.alert.can_stop):
            self.alert.can_pause, self.alert.can_resume, self.alert.can_stop = actions
            self.alert_changed.emit(self.alert)

    # ------------------------------------------------------------------ pausing
    def pausing(self) -> bool:
        return self._pause is not None

    def _press_pause(self) -> bool:
        hwnd = self._verified_control("pause")
        return bool(hwnd) and studio_win32.press(hwnd)

    def _auto_pause_allowed(self) -> bool:
        return bool(self.settings.ai_watch and self.settings.ai_auto_pause)

    def _begin_pause(self, snapshot: str, auto: bool) -> bool:
        """Ask Studio to pause; confirmed later by _advance_pause."""
        if not self._press_pause():
            return False
        now = time.monotonic()
        self._pause = _PendingPause(now, now, now, 1, auto, snapshot)
        self._unconfirmed_pause = None
        self._poll.setInterval(PAUSE_POLL_MS)
        return True

    def _cancel_pause(self) -> None:
        if self._pause is None:
            return
        self._pause = None
        self._poll.setInterval(STUDIO_POLL_MS)
        if self.alert is not None and self.alert.severity == "pausing":
            self._set_alert(None)

    def _advance_pause(self) -> None:
        pending = self._pause
        if pending is None:
            return
        now = time.monotonic()
        elapsed, pending.last_tick = now - pending.last_tick, now
        if pending.auto and not self._auto_pause_allowed():
            self._cancel_pause()
            return
        if self._print_paused:
            self._pause = None
            self._poll.setInterval(STUDIO_POLL_MS)
            self._paused_by_us = True
            self._raise_paused(pending)
            return
        if self._printing is False:
            self._cancel_pause()  # the print ended meanwhile
            return
        if not self._view.ours:
            # Can't see the printer's controls (another tab or printer): we
            # can neither confirm nor retry, so this time doesn't count.
            pending.started += elapsed
            pending.last_press += elapsed
            return
        if now - pending.started >= PAUSE_GIVE_UP_S:
            self._pause = None
            self._unconfirmed_pause = pending  # in case Studio pauses later
            self._poll.setInterval(STUDIO_POLL_MS)
            if pending.auto:
                title = "Couldn't pause the print — check it now"
                message = (
                    "The AI is very sure the print failed, but Studio didn't pause when asked. "
                    "Pause or stop it yourself."
                )
            else:
                title = "Studio didn't pause the print"
                message = "Pause it in Studio yourself."
            self._raise("pause_failed", title, message, None, snapshot=pending.snapshot)
            return
        if now - pending.last_press >= PAUSE_RETRY_S and pending.presses < 2 and self._press_pause():
            pending.presses += 1
            pending.last_press = now

    def _raise_paused(self, request: _PendingPause) -> None:
        if request.auto:
            self._raise(
                "paused",
                "Print paused — it looks like it failed",
                "The AI was very sure for over 30 seconds, so it paused the print in Studio. "
                "Check the printer, then Resume or Stop.",
                None,
                snapshot=request.snapshot,
                urgent=True,
            )
        else:
            self._raise(
                "paused",
                "Print paused",
                "Paused from here. Check the printer, then Resume or Stop.",
                None,
                snapshot=request.snapshot,
                quiet=True,
            )

    def pause_print(self) -> bool:
        """The user's Pause button: same verified, confirmed path."""
        self.alarm.stop()
        if self._pause is not None:
            return True
        snapshot = self.alert.snapshot if self.alert else ""
        if not self._begin_pause(snapshot, auto=False):
            return False
        self._raise("pausing", "Pausing the print…", "Waiting for Studio to confirm.", None, snapshot=snapshot, quiet=True)
        return True

    def resume_print(self) -> bool:
        self.alarm.stop()
        hwnd = self._verified_control("resume")
        ok = bool(hwnd) and studio_win32.press(hwnd)
        if ok:
            QTimer.singleShot(1500, self._poll_studio)  # state follows when Studio shows Pause again
        return ok

    def stop_print(self, parent: QWidget | None = None) -> bool:
        """Stop — only after the user confirms (Studio may ask again)."""
        self.alarm.stop()
        if not self._verified_control("stop"):
            return False
        answer = QMessageBox.warning(
            parent,
            "Stop the print?",
            "This stops the print in eufyMake Studio. It can't be resumed afterwards.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return False
        hwnd = self._verified_control("stop")  # re-check: the dialog took time
        ok = bool(hwnd) and studio_win32.press(hwnd)
        if ok:
            QTimer.singleShot(1500, self._poll_studio)
        return ok

    def looks_fine(self) -> None:
        """The user checked: it's fine. Quiet for a while, and learn from it:
        an AI alert raises this camera's normal; a sound alert makes this
        print's sound check more tolerant."""
        severity = self.alert.severity if self.alert is not None else ""
        self._snooze_until = time.monotonic() + LOOKS_FINE_SNOOZE_S
        if severity == "sound":
            if self._sound is not None:
                self._sound.teach_requested = True
        else:
            if self._vision is not None:
                self._vision.teach_requested = True  # applied before the reset
            else:
                self._scorer.teach_normal()
            self._vision_reset()
            self._save_baseline_soon = True
        self._set_alert(None)

    def _vision_reset(self) -> None:
        if self._vision is not None:
            self._vision.reset_requested = True
        else:
            self._scorer.reset_session()

    def dismiss(self) -> None:
        self._set_alert(None)

    # ------------------------------------------------------------------ results
    def _on_checked(self, result: VisionResult) -> None:
        self.risk = result.risk
        self.last_check = time.time()
        self._checks += 1
        if self._checks % BASELINE_SAVE_EVERY == 0 or self._save_baseline_soon:
            self._save_baseline_soon = False
            self._save_baseline()  # incl. right after "Looks fine" taught it
        self.status_changed.emit()
        if self._printing is False or not self._watching_unknown():
            return  # a check that was under way when the print ended
        now = time.monotonic()
        if now < self._snooze_until:
            return  # "Looks fine": quiet, failures included
        if result.level == FAILURE:
            self._on_failure(result)
        elif result.level == WARNING and self.settings.ai_alert_level != "failures":
            if now - self._last_warning >= WARNING_REPEAT_S and (self.alert is None or self.alert.severity == "sound"):
                self._last_warning = now
                self._raise(
                    "warning",
                    "Possible print problem",
                    "The AI sees something that may be a failure (spaghetti, a blob or a loose part). Take a look.",
                    result,
                )

    def _on_failure(self, result: VisionResult) -> None:
        if self._pause is not None or self._paused_by_us or self._print_paused:
            return  # already pausing / paused
        gave_up = self.alert is not None and self.alert.severity == "pause_failed"
        # Every failing check may pause — e.g. once the user brings Studio's
        # Device tab back — except after Studio already ignored us (no loop
        # of presses; the alert says to pause by hand).
        if self._auto_pause_allowed() and self._printing and not gave_up:
            if self._begin_pause("", auto=True):
                snapshot = self._save_snapshot(result, "failure")
                self._pause.snapshot = snapshot
                self._raise(
                    "pausing",
                    "Print failure — pausing the print",
                    "The AI is very sure the print failed. Pausing it in Studio…",
                    None,
                    snapshot=snapshot,
                )
                return
        if self.alert is not None and self.alert.severity in ("failure", "pause_failed"):
            return  # already told the user; don't repeat every 10 s
        if not self.settings.ai_auto_pause:
            why = " (auto-pause is off)"
        elif self._printing is None or not self._view.running:
            why = " (open eufyMake Studio's Device tab to let it pause prints)"
        else:
            why = " (Studio isn't showing this printer's Pause button)"
        self._raise(
            "failure",
            "The print looks like it failed",
            f"The AI is very sure something went wrong{why}. Check the printer now.",
            result,
        )

    def _on_sound(self, event) -> None:
        if time.monotonic() < self._snooze_until or (self.alert is not None and self.alert.severity != "sound"):
            return
        self._raise("sound", "Unusual printer sound", describe_sound(event), None)

    def _on_sound_status(self, status: str) -> None:
        self._sound_status = status
        self.status_changed.emit()

    def _on_failed(self, message: str) -> None:
        self._error = message
        self._vision = None
        self.status_changed.emit()
        self.main.status_message.emit(message)

    # ------------------------------------------------------------------ alerting
    def _raise(
        self,
        severity: str,
        title: str,
        message: str,
        result: VisionResult | None,
        snapshot: str = "",
        urgent: bool | None = None,
        quiet: bool = False,
    ) -> None:
        if not snapshot and severity not in ("pausing", "test"):
            snapshot = self._save_snapshot(result, severity)
        c = self._view.controls if self._view.ours else {}
        alert = Alert(
            severity,
            title,
            message,
            snapshot,
            can_pause="pause" in c and self._pause is None,
            can_resume="resume" in c,
            can_stop="stop" in c,
            at=time.time(),
        )
        self._set_alert(alert)
        if quiet:
            return
        if self.settings.ai_popup:
            self.popup_requested.emit(alert)
        if self.settings.ai_notifications:
            self.notify.emit(title, message)
        if urgent is None:
            urgent = severity in ("failure", "pausing", "pause_failed")
        self.alarm.play(self.settings.ai_sound, urgent)

    def _set_alert(self, alert: Alert | None) -> None:
        # A new or cleared alert ends the old one's alarm (_raise starts the
        # new one's sound right after).
        self.alarm.stop()
        self.alert = alert
        self.alert_changed.emit(alert)

    def _save_snapshot(self, result: VisionResult | None, severity: str) -> str:
        frame = result.frame if result is not None else self._latest_frame()
        if frame is None:
            return ""
        if result is not None:
            frame = draw_detections(frame, result.detections, self.watch_area())
        folder = self.settings.resolved_snapshot_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"print-watch-{severity}-{time.strftime('%Y%m%d-%H%M%S')}.jpg")
            if cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 88]):
                self.main.capture_saved.emit(path)
                return path
        except (OSError, cv2.error):
            pass
        return ""

    # ------------------------------------------------------------------ status
    def status_text(self) -> str:
        text = self._status_base()
        return text + self._sound_note() if text else ""

    def _status_base(self) -> str:
        if not self.settings.ai_watch:
            return ""
        if self._pause is not None:
            return "Print watch: pausing the print…"
        if not self.model_ready():
            return "Print watch: model not downloaded"
        if self._error:
            return "Print watch: stopped (model error)"
        if not self._url:
            return "Print watch: waiting for video"
        if self._printing is False:
            return "Print watch: idle (no print running)"
        if not self._watching_unknown():
            return "Print watch: waiting for a print"
        if self._print_paused:
            return "Print watch: print paused"
        if not self.last_check:
            return "Print watch: starting…"
        return f"Print watch: {_risk_word(self.risk)}"

    def _sound_note(self) -> str:
        if self.settings.sound_alerts and self._sound_status.startswith("unavailable"):
            return " · sound alerts off (" + self._sound_status.split(": ", 1)[-1] + ")"
        return ""

    def sound_status(self) -> str:
        """For Settings: "listening", "unavailable: …" or "" (not running)."""
        return self._sound_status

    # ------------------------------------------------------------------ model download
    def downloading(self) -> bool:
        return self._download is not None and self._download.is_alive()

    def download_model(self) -> None:
        if self.downloading():
            return
        self._download_cancel = False

        def work() -> None:
            try:
                failure_model.download_model(
                    progress=lambda done, total: self.download_progress.emit(done, total),
                    cancelled=lambda: self._download_cancel,
                )
            except failure_model.DownloadCancelled:
                self.download_finished.emit(False, "Download cancelled.")
            except Exception as exc:  # network, disk, checksum
                self.download_finished.emit(False, f"Couldn't download the model: {exc}")
            else:
                self.download_finished.emit(True, "Model downloaded and verified.")

        self._download = threading.Thread(target=work, name="model-download", daemon=True)
        self._download.start()

    def _on_download_finished(self, ok: bool, _message: str) -> None:
        if ok and self.settings.ai_watch:
            self._restart_workers()

    def cancel_download(self) -> None:
        self._download_cancel = True

    def remove_model(self) -> None:
        self._stop_workers()
        failure_model.remove_model()
        self._restart_workers()


def _risk_word(risk: float) -> str:
    if risk >= 2 / 3:
        return "looks like a failure"
    if risk >= 1 / 3:
        return "something may be wrong"
    return "all good"
