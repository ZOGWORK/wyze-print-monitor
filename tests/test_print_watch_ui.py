"""The print watcher's rules, against a fake Studio. Needs PySide6.

Alert freely; pause only when sure, only the camera's printer, and only
count it once Studio confirms; never stop without asking.
"""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject, Signal  # noqa: E402

from wyze_print_monitor.config import Settings  # noqa: E402
from wyze_print_monitor.failure_detect import FAILURE, WARNING  # noqa: E402
from wyze_print_monitor.print_watch import VisionResult  # noqa: E402
from wyze_print_monitor.ui import print_watch_ui as pw  # noqa: E402

PAUSE, STOP, RESUME = 101, 102, 103
PRINTING = {"pause": PAUSE, "stop": STOP}
PAUSED = {"resume": RESUME, "stop": STOP}


class FakeMain(QObject):
    status_message = Signal(str)
    capture_saved = Signal(str)

    def __init__(self, tmp_path):
        super().__init__()
        self.settings = Settings(snapshot_dir=str(tmp_path), ai_watch=True)
        self._reader = None


class FakeStudio:
    """What the observer sees; obeys Pause clicks unless told not to."""

    def __init__(self):
        self.running, self.ours, self.controls = True, True, dict(PRINTING)
        self.obeys = True
        self.pressed = []
        self.status = ""  # "" = follow the buttons; else a status kind or None (unreadable)

    def current_status(self):
        if self.status != "":
            return self.status
        if "resume" in self.controls:
            return "paused"
        return "running" if "pause" in self.controls else "idle"

    def view(self, _row):
        ours = self.running and self.ours
        return pw.StudioView(self.running, ours, dict(self.controls) if ours else {}, self.current_status() if ours else None)

    def press(self, hwnd):
        self.pressed.append(hwnd)
        if self.obeys and hwnd == PAUSE:
            self.controls = dict(PAUSED)
        elif self.obeys and hwnd == RESUME:
            self.controls = dict(PRINTING)
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    studio = FakeStudio()
    clock = Clock()
    monkeypatch.setattr(pw, "save_settings", lambda s: None)
    sounds = []
    monkeypatch.setattr(pw.Alarm, "play", lambda self, sound, urgent: sounds.append((sound, urgent)))
    monkeypatch.setattr(pw.StudioObserver, "observe", lambda self, row: studio.view(row))
    monkeypatch.setattr(pw.studio_win32, "press", studio.press)
    monkeypatch.setattr(pw.time, "monotonic", clock)
    main = FakeMain(tmp_path)
    w = pw.PrintWatch(main)
    notes = []
    w.notify.connect(lambda title, msg: notes.append(title))
    w._poll_studio()  # Studio shows the camera printer printing
    yield w, main, studio, clock, notes
    w.shutdown()
    app.processEvents()


def _result(level):
    return VisionResult(level, 0.9 if level == FAILURE else 0.5, [], np.zeros((90, 160, 3), np.uint8))


def test_warning_alerts_but_never_presses_anything(env):
    w, _main, studio, _clock, notes = env
    w._on_checked(_result(WARNING))
    assert w.alert.severity == "warning" and notes == ["Possible print problem"]
    assert os.path.exists(w.alert.snapshot)
    assert studio.pressed == []


def test_sure_failure_pauses_and_waits_for_studio_to_confirm(env):
    w, _main, studio, _clock, _n = env
    studio.obeys = False  # Studio hasn't reacted yet
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [PAUSE]
    assert w.alert.severity == "pausing" and w.pausing()
    assert "pausing" in w.status_text()
    studio.controls = dict(PAUSED)  # now Studio shows Resume
    w._poll_studio()
    assert w.alert.severity == "paused" and not w.pausing()
    assert w.alert.can_resume and w.alert.can_stop
    w._on_checked(_result(FAILURE))  # still failing: nothing more to do
    assert studio.pressed == [PAUSE]


def test_pause_is_retried_once_then_reported_if_studio_ignores_it(env):
    w, _main, studio, clock, notes = env
    studio.obeys = False
    w._on_checked(_result(FAILURE))
    clock.now += pw.PAUSE_RETRY_S
    w._poll_studio()
    assert studio.pressed == [PAUSE, PAUSE]
    clock.now += pw.PAUSE_RETRY_S
    w._poll_studio()
    assert studio.pressed == [PAUSE, PAUSE]  # only one retry
    clock.now += pw.PAUSE_GIVE_UP_S
    w._poll_studio()
    assert w.alert.severity == "pause_failed" and not w.pausing()
    assert notes[-1].startswith("Couldn't pause")
    assert w.alert.can_pause  # the user can still try
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [PAUSE, PAUSE]  # no silent loop of presses


def test_never_presses_another_printers_pause(env):
    w, _main, studio, _clock, _n = env
    studio.ours = False  # the user selected another (running) printer in Studio
    w._on_checked(_result(FAILURE))
    assert studio.pressed == []
    assert w.alert.severity == "failure" and "isn't showing" in w.alert.message
    assert not w.alert.can_pause and not w.alert.can_stop


def test_user_buttons_are_also_verified(env):
    w, _main, studio, _clock, _n = env
    studio.ours = False
    assert w.pause_print() is False and w.resume_print() is False and w.stop_print() is False
    assert studio.pressed == []


def test_switching_studio_tabs_doesnt_end_the_print(env):
    w, _main, studio, _clock, _n = env
    for _ in range(40):
        w._scorer.update([])
    w._on_checked(_result(WARNING))
    studio.ours = False  # Studio on its Slice tab (or another printer)
    w._poll_studio()
    studio.ours = True
    w._poll_studio()
    assert w._printing is True
    assert w._scorer.frames == 40  # same print, history kept
    assert w.alert is not None and w.alert.severity == "warning"


def test_print_end_clears_alerts_and_a_new_print_starts_fresh(env):
    w, _main, studio, _clock, notes = env
    w._on_checked(_result(WARNING))
    studio.controls = {}  # print finished
    w._poll_studio()
    assert w._printing is False and w.alert is None
    studio.controls = dict(PRINTING)  # next print, within 15 minutes
    w._poll_studio()
    w._on_checked(_result(WARNING))
    assert notes.count("Possible print problem") == 2  # no leftover cooldown


def test_looks_fine_quiets_failures_too(env):
    w, _main, studio, clock, notes = env
    w._on_checked(_result(WARNING))
    w.looks_fine()
    clock.now += 60
    w._on_checked(_result(FAILURE))
    assert w.alert is None and studio.pressed == [] and len(notes) == 1
    clock.now += pw.LOOKS_FINE_SNOOZE_S
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [PAUSE]


def test_auto_pause_off_only_alerts(env):
    w, main, studio, _clock, _n = env
    main.settings.ai_auto_pause = False
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [] and w.alert.severity == "failure"
    assert "auto-pause is off" in w.alert.message


def test_without_studio_failure_alerts_and_explains(env):
    w, _main, studio, _clock, _n = env
    studio.running = False
    w._poll_studio()
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [] and w.alert.severity == "failure"
    assert "Device tab" in w.alert.message


def test_resume_clears_the_pause_and_watches_afresh(env):
    w, _main, studio, _clock, _n = env
    w._on_checked(_result(FAILURE))
    w._poll_studio()
    assert w.alert.severity == "paused"
    assert w.resume_print() is True and studio.pressed == [PAUSE, RESUME]
    w._poll_studio()
    assert w.alert is None and not w._print_paused and not w._paused_by_us


def test_stop_asks_first_and_rechecks(env, monkeypatch):
    w, _main, studio, _clock, _n = env
    answers = iter([QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.Yes])
    monkeypatch.setattr(pw.QMessageBox, "warning", lambda *a, **k: next(answers))
    assert w.stop_print() is False and studio.pressed == []
    assert w.stop_print() is True and studio.pressed == [STOP]

    def switch_printer_while_asking(*_a, **_k):
        studio.ours = False
        return QtWidgets.QMessageBox.Yes

    monkeypatch.setattr(pw.QMessageBox, "warning", switch_printer_while_asking)
    assert w.stop_print() is False and studio.pressed == [STOP]


def test_sound_reset_is_handed_to_the_worker(env):
    w, _main, studio, _clock, _n = env

    class FakeSound:
        reset_requested = False
        paused = False

    w._sound = FakeSound()
    studio.controls = {}
    w._poll_studio()
    studio.controls = dict(PRINTING)
    w._poll_studio()  # a new print
    assert w._sound.reset_requested is True
    w._sound = None


def test_turning_auto_pause_off_cancels_a_pending_auto_pause(env):
    w, main, studio, clock, _n = env
    studio.obeys = False
    w._on_checked(_result(FAILURE))
    assert w.pausing()
    main.settings.ai_auto_pause = False
    w.apply_settings()
    assert not w.pausing() and (w.alert is None or w.alert.severity != "pausing")
    clock.now += pw.PAUSE_RETRY_S
    w._poll_studio()
    assert studio.pressed == [PAUSE]  # no retry after the switch went off


def test_hidden_controls_dont_count_towards_giving_up(env):
    w, _main, studio, clock, _n = env
    studio.obeys = False
    w._on_checked(_result(FAILURE))
    studio.ours = False  # user flips to another Studio tab
    for _ in range(6):
        clock.now += 10
        w._poll_studio()
    assert w.pausing()  # still waiting, not "couldn't pause"
    studio.ours = True
    studio.controls = dict(PAUSED)  # it did pause
    w._poll_studio()
    assert w.alert.severity == "paused"


def test_a_late_pause_corrects_the_couldnt_pause_alert(env):
    w, _main, studio, clock, _n = env
    studio.obeys = False
    w._on_checked(_result(FAILURE))
    clock.now += pw.PAUSE_GIVE_UP_S + 1
    w._poll_studio()
    assert w.alert.severity == "pause_failed"
    studio.controls = dict(PAUSED)
    w._poll_studio()
    assert w.alert.severity == "paused"


def test_a_manual_pause_doesnt_blame_the_ai(env):
    w, _main, studio, clock, notes = env
    w._on_sound(type("E", (), {"kind": "bang", "louder_db": 30.0})())
    assert w.alert.severity == "sound"
    assert w.pause_print() is True
    w._poll_studio()
    assert w.alert.title == "Print paused" and "AI" not in w.alert.message
    studio.controls = dict(PRINTING)
    w._poll_studio()
    studio.obeys = False
    assert w.pause_print() is True
    clock.now += pw.PAUSE_GIVE_UP_S + 1
    w._poll_studio()
    assert w.alert.title == "Studio didn't pause the print" and "AI" not in w.alert.message


def test_another_camera_starts_from_nothing(env):
    w, main, _studio, _clock, _n = env
    main.settings.sound_alerts = False  # no real audio connection in tests
    w.set_stream("rtsp://u:p@cam-one/stream0")
    for _ in range(40):
        w._scorer.update([])
    w._on_checked(_result(WARNING))
    w.set_stream("rtsp://u:p@cam-two/stream0")
    assert w.alert is None and w._scorer.frames == 0 and w._scorer.baseline.lifetime_frames == 0
    assert pw.camera_key("rtsp://u:p@cam-one/stream0") in main.settings.ai_baseline
    w.set_stream("rtsp://u:p@cam-one/stream0")  # back: its own baseline returns
    assert w._scorer.baseline.lifetime_frames == 40


def test_camera_key_has_no_password():
    a = pw.camera_key("rtsps://user:secret@192.168.1.5:322/stream0")
    assert a == pw.camera_key("rtsps://user:other@192.168.1.5:322/stream0")
    assert "secret" not in a and a != pw.camera_key("rtsps://user:secret@192.168.1.6:322/stream0")


def test_auto_pause_tries_again_once_the_controls_are_back(env):
    w, _main, studio, _clock, notes = env
    studio.ours = False
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [] and w.alert.severity == "failure"
    studio.ours = True
    w._poll_studio()
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [PAUSE] and w.alert.severity in ("pausing", "paused")
    w._on_checked(_result(FAILURE))
    assert studio.pressed == [PAUSE]


def test_failure_alert_isnt_repeated_every_check(env):
    w, main, _studio, _clock, notes = env
    main.settings.ai_auto_pause = False
    w._on_checked(_result(FAILURE))
    w._on_checked(_result(FAILURE))
    assert notes == ["The print looks like it failed"]


def test_finished_download_starts_watching(env, monkeypatch):
    w, main, _studio, _clock, _n = env
    restarts = []
    monkeypatch.setattr(w, "_restart_workers", lambda: restarts.append(1))
    w.download_finished.emit(True, "ok")
    assert restarts == [1]
    main.settings.ai_watch = False
    w.download_finished.emit(True, "ok")
    w.download_finished.emit(False, "failed")
    assert restarts == [1]


def test_sound_trouble_is_shown(env):
    w, _main, _studio, _clock, _n = env
    w._url = "rtsp://cam"
    w.last_check = 1.0
    w._sound_status = "unavailable: the camera stream has no audio"
    assert "sound alerts off (the camera stream has no audio)" in w.status_text()
    w._sound_status = "listening"
    assert "sound" not in w.status_text()


def test_only_failures_setting_skips_warnings(env):
    w, main, _studio, _clock, notes = env
    main.settings.ai_alert_level = "failures"
    w._on_checked(_result(WARNING))
    assert w.alert is None and notes == []
    w._on_checked(_result(FAILURE))
    assert w.alert is not None


def test_notifications_can_be_turned_off(env):
    w, main, _studio, _clock, notes = env
    main.settings.ai_notifications = False
    w._on_checked(_result(WARNING))
    assert w.alert.severity == "warning" and notes == []


def test_sound_choice_and_urgency(env, monkeypatch):
    w, main, studio, _clock, _n = env
    played = []
    monkeypatch.setattr(w.alarm, "play", lambda sound, urgent: played.append((sound, urgent)))
    w._on_checked(_result(WARNING))
    main.settings.ai_sound = "beep"
    w._on_checked(_result(FAILURE))  # pausing: urgent
    w._poll_studio()  # confirmed paused: stays urgent
    assert played == [("alarm", False), ("beep", True), ("beep", True)]


def test_test_alert_plays_and_dismisses(env, monkeypatch):
    w, _main, studio, _clock, notes = env
    played = []
    monkeypatch.setattr(w.alarm, "play", lambda sound, urgent: played.append(urgent))
    w.test_alert()
    assert w.alert.severity == "test" and played == [True] and notes == ["Test alert"]
    assert w.alert.snapshot == "" and studio.pressed == []
    w.dismiss()
    assert w.alert is None


def test_settings_feed_the_scorer_and_worker(env):
    w, main, _studio, _clock, _n = env
    main.settings.ai_pause_after_s = 60
    main.settings.ai_check_interval_s = 20
    w.apply_settings()
    assert w._scorer.pause_after_s == 60
    assert w._scorer.interval_s == 20
    main.settings.ai_check_interval_s = 7  # not an offered value
    assert w.check_interval() == 10


def test_heating_counts_as_printing_without_a_pause_button(env):
    w, _main, studio, _clock, _n = env
    studio.controls = {"stop": STOP}
    studio.status = "running"  # "Heating": only Stop is shown
    w._poll_studio()
    assert w._printing is True and not w._print_paused


def test_a_brief_na_keeps_the_print_but_a_lasting_one_ends_it(env):
    w, _main, studio, clock, _n = env
    studio.controls = {}
    studio.status = "blank"  # Studio's card shows "N/A"
    w._poll_studio()
    clock.now += 20
    w._poll_studio()
    assert w._printing is True  # a blink (e.g. starting the next print)
    studio.controls, studio.status = dict(PRINTING), ""
    w._poll_studio()
    studio.controls, studio.status = {}, "blank"
    w._poll_studio()
    clock.now += 30
    w._poll_studio()
    assert w._printing is True  # the timer restarted after the status came back
    clock.now += pw.IDLE_AFTER_UNREADABLE_S
    w._poll_studio()
    assert w._printing is False  # the print is over


def test_an_unknown_status_never_ends_the_print(env):
    w, _main, studio, clock, _n = env
    studio.controls = {"stop": STOP}
    studio.status = None  # text we don't recognise, or no card to read
    for _ in range(5):
        clock.now += pw.IDLE_AFTER_UNREADABLE_S
        w._poll_studio()
    assert w._printing is True


def test_a_test_alert_never_replaces_a_real_one(env, monkeypatch):
    w, _main, studio, _clock, _n = env
    w._on_checked(_result(FAILURE))
    w._poll_studio()
    assert w.alert.severity == "paused"
    assert w.test_alert() is False
    assert w.alert.severity == "paused"


def test_looks_fine_on_an_ai_alert_teaches_the_model_side(env):
    w, _main, _studio, _clock, _n = env

    class FakeVision:
        teach_requested = reset_requested = paused = False

    w._vision = FakeVision()
    w._on_checked(_result(WARNING))
    w.looks_fine()
    assert w._vision.teach_requested and w._vision.reset_requested
    assert w._save_baseline_soon
    w._vision = None


def test_looks_fine_on_a_sound_alert_teaches_the_sound_side_only(env):
    w, _main, _studio, _clock, _n = env

    class FakeWorker:
        teach_requested = reset_requested = paused = False

    w._vision, w._sound = FakeWorker(), FakeWorker()
    w._on_sound(type("E", (), {"kind": "unusual", "louder_db": 12.0})())
    w.looks_fine()
    assert w._sound.teach_requested and not w._sound.reset_requested
    assert not w._vision.teach_requested and not w._vision.reset_requested
    w._vision = w._sound = None


def test_resuming_keeps_the_sound_checks_learning(env):
    w, _main, studio, _clock, _n = env

    class FakeWorker:
        teach_requested = reset_requested = paused = False

    w._vision, w._sound = FakeWorker(), FakeWorker()
    studio.controls = dict(PAUSED)
    w._poll_studio()
    studio.controls = dict(PRINTING)  # resumed
    w._poll_studio()
    assert w._vision.reset_requested and not w._sound.reset_requested
    w._vision = w._sound = None


def test_without_studio_it_waits_for_a_print(env):
    _w, main, studio, _clock, notes = env
    studio.running = False  # Studio was never open: no telling if anything prints
    w = pw.PrintWatch(main)
    w._poll_studio()
    w.model_ready = lambda: True  # no model download in tests
    w._url, w.last_check = "rtsp://cam/stream0", 1.0  # video on (no real workers)
    assert w.status_text().startswith("Print watch: waiting for a print")
    w._on_checked(_result(WARNING))
    w._on_checked(_result(FAILURE))
    assert w.alert is None and studio.pressed == []
    main.settings.ai_watch_without_studio = True  # the user's choice: watch anyway
    w._on_checked(_result(WARNING))
    assert w.alert.severity == "warning"
    w.shutdown()


def test_closing_studio_mid_print_keeps_watching(env):
    w, _main, studio, _clock, _n = env
    studio.running = False
    w._poll_studio()
    assert w._printing is True
    w._on_checked(_result(WARNING))
    assert w.alert.severity == "warning"


def test_watch_area_is_per_camera_and_starts_the_normal_over(env):
    w, main, _studio, _clock, _n = env
    main.settings.sound_alerts = False
    w.set_stream("rtsp://u:p@cam-one/stream0")
    for _ in range(40):
        w._scorer.update([])
    w.set_watch_area((0.2, 0.1, 0.8, 0.9))
    assert w.watch_area() == (0.2, 0.1, 0.8, 0.9)
    assert w._scorer.baseline.lifetime_frames == 0
    w.set_stream("rtsp://u:p@cam-two/stream0")
    assert w.watch_area() is None
    w.set_stream("rtsp://u:p@cam-one/stream0")
    assert w.watch_area() == (0.2, 0.1, 0.8, 0.9)
    w.set_watch_area(None)
    assert w.watch_area() is None


def test_watch_without_studio_overrides_a_stale_idle(env):
    w, main, studio, _clock, _n = env
    studio.controls = {}  # Studio last showed the printer idle
    w._poll_studio()
    assert w._printing is False
    studio.running = False  # then closed; a print starts from elsewhere
    w._poll_studio()
    w._on_checked(_result(WARNING))
    assert w.alert is None  # off: idle stands
    main.settings.ai_watch_without_studio = True
    w._poll_studio()
    w._on_checked(_result(WARNING))
    assert w.alert is not None and w.alert.severity == "warning"
