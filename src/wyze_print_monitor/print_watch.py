"""Watching a print for failures: vision (Obico model) and sound workers.

Both run on low-priority threads, separate from the video:

* :class:`VisionWorker` samples the stream's newest frame every
  ``CHECK_INTERVAL_S`` and runs the failure model on it (≈0.5–1 s of CPU on
  two threads, so a few percent on average).
* :class:`SoundWorker` keeps its own light connection to the camera's
  low-res stream, reads only the audio (video packets are skipped, never
  decoded) and feeds :mod:`sound_watch`.

:class:`PrintWatch` decides what to do with their results (see
``ui/print_watch_ui.py`` for what the user sees).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PySide6.QtCore import QThread, Signal

from .failure_detect import (
    CHECK_INTERVAL_S,
    Area,
    Baseline,
    Detection,
    FailureScorer,
    in_area,
    postprocess,
    preprocess,
)
from .quality import substream_url
from .sound_watch import WINDOW_S, SoundAnomaly, band_levels

INFERENCE_THREADS = 2


def load_session(path: str):
    """An onnxruntime session tuned to stay out of the way of the video."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = INFERENCE_THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])


@dataclass
class VisionResult:
    level: str  # failure_detect.OK / WARNING / FAILURE
    risk: float  # 0..1
    detections: list[Detection] = field(default_factory=list)
    frame: np.ndarray | None = None
    seconds: float = 0.0  # inference time


class VisionWorker(QThread):
    checked = Signal(object)  # VisionResult
    failed = Signal(str)  # couldn't load / run the model

    def __init__(
        self,
        model_path: str,
        frame_source: Callable[[], np.ndarray | None],
        scorer: FailureScorer,
        interval_s: float = CHECK_INTERVAL_S,
        area: Area | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._model_path = model_path
        self._area = area  # only detections in here count (None: everywhere)
        self._frame_source = frame_source
        self.scorer = scorer
        self._interval = interval_s
        self._wake = threading.Event()
        self._stop = False
        self.paused = False  # GUI sets this while nothing is printing
        self.reset_requested = False  # GUI: start a fresh session (applied here)
        self.teach_requested = False  # GUI: "Looks fine" — learn this as normal

    def request_stop(self) -> None:
        """Ask the thread to finish (returns at once; see QThread.finished)."""
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        try:
            session = load_session(self._model_path)
        except Exception as exc:  # broken/missing file, no onnxruntime, ...
            self.failed.emit(f"Couldn't load the failure-detection model: {exc}")
            return
        name = session.get_inputs()[0].name
        shape = session.get_inputs()[0].shape
        size = (int(shape[3]), int(shape[2])) if len(shape) == 4 else (416, 416)
        last = None
        while not self._stop:
            self._wake.wait(self._interval)
            if self._stop:
                break
            frame = self._frame_source()
            if self.paused or frame is None or frame is last:
                continue  # nothing new (stream down) or not printing
            last = frame
            started = time.perf_counter()
            try:
                detections = in_area(postprocess(session.run(None, {name: preprocess(frame, size)})), self._area)
            except Exception as exc:
                self.failed.emit(f"Failure detection stopped: {exc}")
                return
            if self.teach_requested:  # before a reset: it needs what was seen
                self.teach_requested = False
                self.scorer.teach_normal()
            if self.reset_requested:
                self.reset_requested = False
                self.scorer.reset_session()
            level = self.scorer.update(detections)
            self.checked.emit(
                VisionResult(level, self.scorer.risk(), detections, frame, time.perf_counter() - started)
            )


def _audio_options() -> dict:
    # tls_verify: the camera's certificate is self-signed (OpenCV's FFmpeg
    # build doesn't verify either).
    return {"rtsp_transport": "tcp", "timeout": "8000000", "tls_verify": "0"}


class SoundWorker(QThread):
    unusual = Signal(object)  # sound_watch.SoundEvent
    status = Signal(str)  # "listening" / "unavailable: ..." for the UI

    RETRY_S = (5, 15, 60, 300)

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self._url = substream_url(url) or url  # the light stream if there is one
        self._stop = False
        self._wake = threading.Event()
        self.paused = False
        self.reset_requested = False  # GUI: forget the learned sound (applied here)
        self.teach_requested = False  # GUI: "Looks fine" on a sound alert
        self.anomaly = SoundAnomaly()

    def request_stop(self) -> None:
        """Ask the thread to finish (returns at once: it may be waiting on
        the network for a few seconds; see QThread.finished)."""
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        try:
            import av  # optional: no PyAV, no sound alerts
        except ImportError:
            self.status.emit("unavailable: audio support isn't installed")
            return
        failures = 0
        while not self._stop:
            try:
                self._listen(av)
                failures = 0
            except Exception as exc:
                self.status.emit("unavailable: " + _plain_error(exc))
                failures += 1
            if self._stop:
                break
            self._wake.wait(self.RETRY_S[min(failures, len(self.RETRY_S) - 1)])

    def _listen(self, av) -> None:
        container = av.open(self._url, options=_audio_options(), timeout=10)
        try:
            if not container.streams.audio:
                raise RuntimeError("the camera stream has no audio")
            audio = container.streams.audio[0]
            rate = audio.codec_context.sample_rate or 8000
            window = int(rate * WINDOW_S)
            buf = np.zeros(0, np.float32)
            self.status.emit("listening")
            self.anomaly.reset()
            for packet in container.demux(audio):  # video packets are dropped unread
                if self._stop:
                    return
                for frame in packet.decode():
                    samples = frame.to_ndarray().astype(np.float32)
                    if samples.ndim > 1:
                        samples = samples.mean(axis=0)
                    buf = np.concatenate([buf, samples / 32768.0])
                while buf.size >= window:
                    chunk, buf = buf[:window], buf[window:]
                    if self.reset_requested:
                        self.reset_requested = False
                        self.anomaly.reset(new_print=True)
                    if self.teach_requested:
                        self.teach_requested = False
                        self.anomaly.teach()
                    if self.paused:
                        continue
                    event = self.anomaly.update(band_levels(chunk, rate), time.monotonic())
                    if event is not None:
                        self.unusual.emit(event)
        finally:
            container.close()


def _plain_error(exc: Exception) -> str:
    """An error message without the stream URL (it embeds the password)."""
    return re.sub(r"rtsps?://[^\s'\"]+", "<camera>", str(exc)) or type(exc).__name__
