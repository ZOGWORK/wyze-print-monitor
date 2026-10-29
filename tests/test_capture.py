"""Tests for capture helpers (Qt stubbed — no GUI needed)."""
import re
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stub_qt(monkeypatch):
    """capture.py subclasses QThread at import time; stub just enough Qt
    to import the pure helpers without a GUI toolkit installed."""
    qtcore = types.ModuleType("PySide6.QtCore")

    class QThread:
        wait_result = True

        def __init__(self, *a, **k): ...

        def wait(self, *a, **k):
            return self.wait_result

    qtcore.QThread = QThread
    qtcore.Signal = lambda *a, **k: None
    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QImage = type("QImage", (), {})
    pyside = types.ModuleType("PySide6")
    pyside.QtCore = qtcore
    pyside.QtGui = qtgui
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)
    monkeypatch.delitem(sys.modules, "wyze_print_monitor.capture", raising=False)


def test_snapshot_paths_are_unique(tmp_path):
    """Two snapshots taken in the same second must not share a path."""
    from wyze_print_monitor.capture import snapshot_path

    first = snapshot_path(str(tmp_path))
    second = snapshot_path(str(tmp_path))
    assert first != second
    assert re.search(r"\d{8}-\d{6}-\d{6}\.png$", first)


def test_stop_reports_a_thread_that_did_not_exit():
    """stop() must tell the caller when the reader is still blocked, so the
    owner never destroys a running QThread."""
    from wyze_print_monitor.capture import StreamReader

    reader = StreamReader("rtsp://example/live")
    reader.wait_result = False
    assert reader.stop(timeout_ms=1) is False
    assert reader._stop_requested is True

    reader.wait_result = True
    assert reader.stop() is True


def test_open_prefers_tcp_with_timeouts_and_falls_back(monkeypatch):
    """RTSP over TCP first (UDP drops frames on Wi-Fi), timeouts passed at
    open time, and FFmpeg's default transport if the camera refuses TCP."""
    import os

    from wyze_print_monitor import capture

    attempts = []

    class FakeCap:
        def __init__(self, url, backend, params):
            attempts.append((os.environ.get(capture.FFMPEG_OPTIONS_ENV), list(params)))
            self._ok = len(attempts) == 2  # TCP refused, default works

        def isOpened(self):
            return self._ok

        def release(self):
            pass

    monkeypatch.setattr(capture.cv2, "VideoCapture", FakeCap)
    monkeypatch.delenv(capture.FFMPEG_OPTIONS_ENV, raising=False)
    monkeypatch.setattr(capture, "_USER_FFMPEG_OPTIONS", None)
    cap = capture.StreamReader("rtsps://cam/stream0")._open("rtsps://cam/stream0")

    assert cap.isOpened()
    assert [opts for opts, _ in attempts] == [capture.RTSP_OVER_TCP, None]
    params = attempts[0][1]
    assert params[params.index(capture.cv2.CAP_PROP_OPEN_TIMEOUT_MSEC) + 1] == capture.OPEN_TIMEOUT_MS
    assert params[params.index(capture.cv2.CAP_PROP_READ_TIMEOUT_MSEC) + 1] == capture.READ_TIMEOUT_MS


def test_transport_attempts_preserve_user_options():
    from wyze_print_monitor.capture import RTSP_OVER_TCP, transport_attempts

    assert transport_attempts(None) == [RTSP_OVER_TCP, None]
    assert transport_attempts("stimeout;5000000") == [
        f"stimeout;5000000|{RTSP_OVER_TCP}",
        "stimeout;5000000",
    ]
    # A transport the user chose wins outright.
    assert transport_attempts("rtsp_transport;udp") == ["rtsp_transport;udp"]


def test_open_restores_user_options(monkeypatch):
    import os

    from wyze_print_monitor import capture

    class FailingCap:
        def __init__(self, *a):
            pass

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(capture.cv2, "VideoCapture", FailingCap)
    monkeypatch.setattr(capture, "_USER_FFMPEG_OPTIONS", "stimeout;5000000")
    monkeypatch.setenv(capture.FFMPEG_OPTIONS_ENV, "stimeout;5000000")
    capture.StreamReader("rtsp://cam/live")._open("rtsp://cam/live")
    assert os.environ[capture.FFMPEG_OPTIONS_ENV] == "stimeout;5000000"


def test_mailbox_keeps_only_the_newest_frame_and_notifies_once(monkeypatch):
    """If the GUI is busy, stale frames are replaced, not queued (queued
    frames are what made the picture lag behind the camera) - while the
    recorder on the stream thread still gets every frame."""
    import numpy as np

    from wyze_print_monitor import capture

    monkeypatch.setattr(capture, "to_display_image", lambda frame, width: ("image", int(frame[0, 0, 0])))
    reader = capture.StreamReader("rtsp://cam/stream0", quality="1080p")
    emitted = []
    reader.frame_ready = type("Sig", (), {"emit": lambda self: emitted.append(1)})()
    written = []
    reader.set_sinks(type("Rec", (), {"write": lambda self, f: written.append(int(f[0, 0, 0]))})(), None)

    for value in (1, 2, 3):
        reader._handle(np.full((4, 4, 3), value, np.uint8))
    assert len(emitted) == 1              # one notification while the GUI is busy
    frame, image = reader.take_latest()
    assert image == ("image", 3)          # only the newest frame is shown
    assert written == [1, 2, 3]           # but every frame was recorded
    assert reader.take_latest() is None

    reader._handle(np.full((4, 4, 3), 4, np.uint8))
    assert len(emitted) == 2              # mailbox emptied -> notify again


def test_hidden_video_skips_the_display_image_and_adjustments(monkeypatch):
    """Nothing on screen and nothing recording: the costly colour work and
    display conversion are skipped (they took about a CPU core at 1080p)."""
    import numpy as np

    from wyze_print_monitor import capture
    from wyze_print_monitor.adjust import ImageAdjustments

    calls = []
    monkeypatch.setattr(capture, "apply_adjustments", lambda f, a: calls.append("adjust") or f)
    monkeypatch.setattr(capture, "to_display_image", lambda f, w: calls.append("image") or "image")
    reader = capture.StreamReader("rtsp://cam/stream0", quality="1080p")
    reader.frame_ready = type("Sig", (), {"emit": lambda self: None})()
    reader.adjustments = ImageAdjustments(brightness=10)
    reader.display_width = -1
    raw = np.full((4, 4, 3), 7, np.uint8)
    reader._handle(raw)
    frame, image = reader.take_latest()
    assert image is None and calls == []
    assert frame is raw  # snapshots apply the adjustments themselves

    written = []
    reader.set_sinks(type("Rec", (), {"write": lambda self, f: written.append(f)})(), None)
    reader._handle(raw)
    assert calls == ["adjust"] and len(written) == 1  # recording still gets adjusted frames


def test_display_frames_are_made_at_the_shown_size():
    """The on-screen picture is cropped and shrunk before the colour work,
    and still looks like the full-size adjusted frame scaled down."""
    import cv2
    import numpy as np

    from wyze_print_monitor.adjust import ImageAdjustments, apply_adjustments
    from wyze_print_monitor.capture import display_frame

    y, x = np.mgrid[0:1080, 0:1920]
    frame = np.dstack([x % 256, y % 256, (x + y) % 256]).astype(np.uint8)
    frame = cv2.GaussianBlur(frame, (0, 0), 3)  # camera-like, no pixel noise
    adj = ImageAdjustments(brightness=10, contrast=5, saturation=10, zoom=1.5, pan_x=0.3)
    small = display_frame(frame, adj, 540)
    assert small.shape[:2] == (304, 540)
    reference = cv2.resize(apply_adjustments(frame, adj), (540, 304), interpolation=cv2.INTER_AREA)
    assert np.abs(small.astype(int) - reference.astype(int)).mean() < 6

    turned = display_frame(frame, ImageAdjustments(rotation=90, brightness=5), 540)
    assert turned.shape[:2] == (960, 540)
    assert display_frame(frame, ImageAdjustments(brightness=5), 0).shape[:2] == (1080, 1920)
    assert display_frame(frame, ImageAdjustments(zoom=2.0), 0).shape[:2] == (1080, 1920)
