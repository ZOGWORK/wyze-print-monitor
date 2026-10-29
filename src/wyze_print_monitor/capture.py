"""RTSP stream reading, clip recording, and snapshot capture.

:class:`StreamReader` runs on its own thread, reconnects with backoff when
the camera drops, and does all per-frame work there (quality, adjustments,
recording, display scaling). :class:`ClipRecorder` writes frames to an MP4.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from dataclasses import replace

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from .adjust import IDENTITY, AutoLight, ImageAdjustments, apply_adjustments, fit_frame, zoom_crop
from .quality import (
    AUTO,
    FULL_HD,
    LOW,
    TARGET_HEIGHT,
    AutoQuality,
    normalize,
    scale_to_height,
    substream_url,
)

# OpenCV splits each resize / colour pass over every core by default. For
# one video frame the hand-off costs more than the work: its 16 workers
# took a whole core at 20 fps where one thread needs a quarter.
cv2.setNumThreads(1)

# Every blocking FFmpeg call is bounded so stop() can always join the thread.
OPEN_TIMEOUT_MS = 5000
READ_TIMEOUT_MS = 5000
FFMPEG_OPTIONS_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
RTSP_OVER_TCP = "rtsp_transport;tcp"
# Options the user set themselves (captured once, before any reader thread
# touches the variable): we add to them and always restore them.
_USER_FFMPEG_OPTIONS = os.environ.get(FFMPEG_OPTIONS_ENV)


def transport_attempts(user_options: str | None) -> list[str | None]:
    """FFmpeg option strings to try, in order (``None`` = no options).

    TCP first, then the user's own options / FFmpeg's default. A user who
    already picked an ``rtsp_transport`` keeps exactly their setting.
    """
    if user_options and "rtsp_transport" in user_options:
        return [user_options]
    tcp = f"{user_options}|{RTSP_OVER_TCP}" if user_options else RTSP_OVER_TCP
    return [tcp, user_options or None]


def _set_ffmpeg_options(options: str | None) -> None:
    if options:
        os.environ[FFMPEG_OPTIONS_ENV] = options
    else:
        os.environ.pop(FFMPEG_OPTIONS_ENV, None)


class StreamReader(QThread):
    """Reads an RTSP stream on its own thread and does all per-frame work there.

    For every decoded frame, off the GUI thread: quality scaling, picture
    adjustments, recording / timelapse capture, and a display image already
    scaled to the size on screen. The GUI then only hands that image to the
    views. Frames are delivered through a one-slot mailbox: if the GUI is
    busy, older frames are replaced rather than queued, so the picture never
    falls behind the camera (a queue of stale frames is what added delay).
    """

    frame_ready = Signal()  # a new frame is waiting in the mailbox: take_latest()
    state_changed = Signal(str, str)  # state, detail message
    # states: "connecting" | "live" | "reconnecting" | "stopped" | "error"
    recording_failed = Signal(object, str)  # (the recorder that failed, message)
    quality_changed = Signal(str)  # human label of what's being shown now

    def __init__(self, url: str, parent=None, quality: str = AUTO) -> None:
        super().__init__(parent)
        self._url = url
        self._substream = substream_url(url)
        self._stop_requested = False
        self.measured_fps = 0.0
        self.frame_size: tuple[int, int] = (0, 0)
        # Picture adjustments, applied on this thread to every frame (so
        # view, snapshots, clips and timelapses all match). Immutable, so the
        # GUI thread can swap it in with one assignment.
        self.adjustments: ImageAdjustments = IDENTITY
        self._auto_light = AutoLight()
        self._auto_light_idle = True
        # The newest frame before hand-set adjustments (no zoom crop), for
        # the print watcher to sample now and then. One reference swap.
        self.last_frame: np.ndarray | None = None
        # Widest view on screen, in device pixels (0 = full size): display
        # images are scaled to this here instead of while painting.
        # Width the GUI shows the video at, in device pixels: 0 = full size,
        # -1 = not on screen at all (no display image is made).
        self.display_width = 0
        self.quality = normalize(quality)
        self._auto = AutoQuality(self._substream is not None)
        self.active_quality = ""
        # Capture sinks, written on this thread; the GUI swaps them under
        # the lock so a frame is never written to a recorder being closed.
        self._sinks_lock = threading.Lock()
        self._recorder: ClipRecorder | None = None
        self._timelapse = None  # TimelapseCollector
        self._mailbox_lock = threading.Lock()
        # (frame before the picture adjustments, display image or None)
        self._latest: tuple[np.ndarray, QImage | None] | None = None
        self._pending = False
        self._taken = 0  # frames the GUI actually picked up (shown)
        self._reconnect = False

    # ------------------------------------------------------------------ GUI API
    def set_quality(self, quality: str) -> None:
        quality = normalize(quality)
        if quality != self.quality:
            self.quality = quality
            self._reconnect = True  # the source may change: reopen

    def set_sinks(self, recorder, timelapse) -> None:
        """Attach / detach the clip recorder and timelapse collector."""
        with self._sinks_lock:
            self._recorder = recorder
            self._timelapse = timelapse

    def take_latest(self) -> tuple[np.ndarray, QImage] | None:
        """The newest (frame, display image), or None; empties the mailbox."""
        with self._mailbox_lock:
            item, self._latest, self._pending = self._latest, None, False
            if item is not None:
                self._taken += 1
        return item

    def request_stop(self) -> None:
        self._stop_requested = True

    def stop(self, timeout_ms: int = 12000) -> bool:
        """Ask the thread to exit and wait for it. Returns True once it has.

        Reads are bounded by CAP_PROP_READ_TIMEOUT_MSEC, so 12s is normally
        ample — but a backend that ignores the timeout can overrun it, so the
        caller must check the result before letting the thread be destroyed.
        """
        self.request_stop()
        return self.wait(timeout_ms)

    # ------------------------------------------------------------------ thread
    def _effective_quality(self) -> str:
        """The level actually in use: the setting, or Auto's current step."""
        return self._auto.level if self.quality == AUTO else self.quality

    def _source(self) -> tuple[str, str]:
        """(url to open, label) for the current quality."""
        level = self._effective_quality()
        url = self._substream if level == LOW and self._substream else self._url
        return url, (f"Auto · {level}" if self.quality == AUTO else level)

    def run(self) -> None:
        if not self._url:
            self.state_changed.emit("error", "No RTSP URL configured.")
            return
        backoff = 1.0
        self.state_changed.emit("connecting", "Opening RTSP stream…")
        while not self._stop_requested:
            url, label = self._source()
            self._reconnect = False
            cap = self._open(url)
            # Keep the decode buffer tiny so the view stays close to live.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                cap.release()
                if url != self._url and not self._stop_requested:
                    # The low stream is unavailable: use the main one (scaled).
                    self._auto.drop_substream()
                    self._substream = None
                    continue
                if self.quality == AUTO and self._auto.probe_failed(time.monotonic()):
                    # Stepping up failed: go back to the stream that worked.
                    self.state_changed.emit("live", "Couldn't switch up; staying on the lower quality.")
                    continue
                self.state_changed.emit(
                    "reconnecting", f"Cannot open stream, retrying in {backoff:.0f}s…"
                )
                self._sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if label != self.active_quality:
                self.active_quality = label
                self.quality_changed.emit(label)
            self.state_changed.emit("live", "Streaming")
            backoff = 1.0
            self._read_loop(cap, url)
            cap.release()
            if not self._stop_requested and not self._reconnect:
                self.state_changed.emit("reconnecting", "Stream dropped, reconnecting…")
                self._sleep(1.0)
        self.state_changed.emit("stopped", "Stream stopped.")

    def _read_loop(self, cap, url: str) -> None:
        frames = 0
        taken_at_start = self._taken
        t_start = time.monotonic()
        while not self._stop_requested and not self._reconnect:
            ok, frame = cap.read()
            if not ok:
                return  # stream dropped -> reconnect
            frames += 1
            now = time.monotonic()
            if now - t_start >= 2.0:
                elapsed = now - t_start
                self.measured_fps = frames / elapsed
                shown_fps = (self._taken - taken_at_start) / elapsed
                frames, taken_at_start, t_start = 0, self._taken, now
                if self.quality == AUTO:
                    # Judge by what reaches the screen too: a busy CPU slows
                    # the display long before it slows the network.
                    self._apply_auto(self._auto.update(min(self.measured_fps, shown_fps), now), url)
            self._handle(frame)

    def _apply_auto(self, level: str | None, current_url: str) -> None:
        if level is None:
            return
        going_down = level != FULL_HD and (level == LOW or self.active_quality.endswith(FULL_HD))
        self.state_changed.emit(
            "live",
            f"Video was struggling (busy PC or slow network): switched to {level}."
            if going_down
            else f"Trying {level} again…",
        )
        url, label = self._source()
        if url != current_url:
            self._reconnect = True  # different stream: reopen (label follows)
        elif label != self.active_quality:
            self.active_quality = label  # same stream, new scaling: instant
            self.quality_changed.emit(label)

    def _handle(self, frame: np.ndarray) -> None:
        height = TARGET_HEIGHT.get(self._effective_quality())
        if height:
            frame = scale_to_height(frame, height)
        h, w = frame.shape[:2]
        self.frame_size = (w, h)  # what we deliver (after quality scaling)
        adjustments = self.adjustments  # one read: the GUI may swap it
        if adjustments.auto_light:
            frame = self._auto_light.apply(frame)
        elif not self._auto_light_idle:
            self._auto_light.reset()  # turned off: start fresh next time
        self._auto_light_idle = not adjustments.auto_light
        self.last_frame = frame  # for the print watcher (see print_watch)
        identity = adjustments.manual().is_identity()
        # The adjustments and the display image are the expensive part (about
        # a CPU core at 1080p): only work out what something actually uses.
        adjusted = None
        with self._sinks_lock:
            if self._recorder is not None or self._timelapse is not None:
                adjusted = frame if identity else _adjusted(frame, adjustments)
            if self._recorder is not None:
                try:
                    self._recorder.write(adjusted)
                except IOError as exc:
                    failed, self._recorder = self._recorder, None
                    # Name the recorder: the GUI may have swapped it since.
                    self.recording_failed.emit(failed, str(exc))
            if self._timelapse is not None:
                self._timelapse.maybe_capture(adjusted)
        width = self.display_width
        if width < 0:
            image = None  # nothing on screen shows the video
        elif adjusted is not None or identity:
            image = to_display_image(adjusted if adjusted is not None else frame, width)
        else:
            image = to_display_image(display_frame(frame, adjustments, width), width)
        with self._mailbox_lock:
            self._latest = (frame, image)
            notify = not self._pending
            self._pending = True
        if notify:
            self.frame_ready.emit()

    def _open(self, url: str) -> "cv2.VideoCapture":
        """Open the stream, preferring RTSP over TCP.

        FFmpeg defaults to RTP over UDP, which drops packets on Wi-Fi (and is
        often blocked by firewalls): each loss ended the read loop and made
        the picture blink while we reconnected. Interleaved TCP is far
        steadier; fall back to FFmpeg's default for cameras that refuse it.

        The timeouts must be passed at construction — VideoCapture(url)
        opens immediately, so setting them afterwards never bounded the open
        (it waited FFmpeg's 30 s default instead).
        """
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS,
        ]
        cap = None
        try:
            for options in transport_attempts(_USER_FFMPEG_OPTIONS):
                # OpenCV's FFmpeg backend only takes demuxer options from this
                # environment variable, read at open time.
                _set_ffmpeg_options(options)
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
                if cap.isOpened() or self._stop_requested:
                    return cap
                cap.release()
            return cap
        finally:
            _set_ffmpeg_options(_USER_FFMPEG_OPTIONS)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_requested and time.monotonic() < deadline:
            time.sleep(0.1)


def _adjusted(frame: np.ndarray, adjustments) -> np.ndarray:
    try:
        return apply_adjustments(frame, adjustments)
    except cv2.error:
        return frame  # never let a bad setting stop the stream


def display_frame(frame: np.ndarray, adjustments, width: int) -> np.ndarray:
    """``frame`` as ``apply_adjustments`` shows it, made straight at the
    ``width`` it's displayed at (0 = full size): crop to the zoom, one resize
    to the shown size, then the colour work on those few pixels. The
    full-size route resized the crop up to 1080p and back down again."""
    adj = adjustments.manual()
    crop = zoom_crop(frame, adj.zoom, adj.pan_x, adj.pan_y) if adj.zoom > 1.0 else frame
    ch, cw = crop.shape[:2]
    shown_w = ch if adj.rotation in (90, 270) else cw  # quarter turns swap the axes
    if 0 < width < shown_w:
        scale = width / shown_w
        size = (max(int(round(cw * scale)), 1), max(int(round(ch * scale)), 1))
        # Linear is 4x faster than area and as good down to half size.
        crop = cv2.resize(crop, size, interpolation=cv2.INTER_LINEAR if scale >= 0.5 else cv2.INTER_AREA)
    elif adj.zoom > 1.0:
        crop = cv2.resize(crop, frame.shape[1::-1], interpolation=cv2.INTER_LINEAR)  # as before
    return _adjusted(crop, replace(adj, zoom=1.0, pan_x=0.0, pan_y=0.0))


def to_display_image(frame: np.ndarray, width: int) -> QImage:
    """An RGB QImage of ``frame`` no wider than ``width`` (0 = full size).

    Shrinking here, on the stream thread, means the GUI never converts or
    smooth-scales a full 1080p frame while painting.
    """
    h, w = frame.shape[:2]
    if 0 < width < w:
        frame = cv2.resize(frame, (width, max(int(round(h * width / w)), 1)), interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]
    # 32-bit, the format Qt paints to the screen with: an RGB888 image was
    # converted again on every paint, on a pool of threads (most of a core).
    # BGRA bytes are exactly Format_RGB32 on little-endian machines.
    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    return QImage(bgra.data, w, h, 4 * w, QImage.Format_RGB32).copy()


class ClipRecorder:
    """Writes BGR frames to an MP4 file. Not thread-safe by itself."""

    def __init__(self, output_path: str, fps: float = 20.0) -> None:
        self.output_path = output_path
        self._writer: cv2.VideoWriter | None = None
        self._fps = fps
        self._size: tuple[int, int] | None = None
        self.frames_written = 0

    def write(self, frame: np.ndarray) -> None:
        if self._writer is None:
            h, w = frame.shape[:2]
            os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
            self._writer = cv2.VideoWriter(
                self.output_path,
                cv2.VideoWriter_fourcc(*"mp4v"),
                self._fps,
                (w, h),
            )
            if not self._writer.isOpened():
                self._writer.release()
                self._writer = None
                raise IOError(
                    f"Could not open video writer for {self.output_path} "
                    "(folder unwritable or mp4v encoder unavailable)."
                )
            self._size = (w, h)
        # If the frame shape changed mid-recording (rotated 90° in the
        # picture settings, RTSP URL switched), letterbox it into the clip's
        # fixed size instead of dropping or stretching it.
        assert self._size is not None
        frame = fit_frame(frame, self._size)
        self._writer.write(frame)
        self.frames_written += 1

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None


def snapshot_path(snapshot_dir: str, prefix: str = "snapshot") -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    os.makedirs(snapshot_dir, exist_ok=True)
    return os.path.join(snapshot_dir, f"{prefix}_{stamp}.png")
