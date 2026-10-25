"""Print-failure detection: model I/O and the decision logic (no Qt).

The model is Obico's open failure-detection network (a YOLO detector with
one class, "failure": spaghetti, blobs, parts knocked loose). It's run on
one frame every ``CHECK_INTERVAL_S`` seconds, and single frames are never
trusted: like Obico, each check's detections are folded into running means
and compared with this printer's own long-term baseline, so a camera that
always sees a little "failure" in some odd corner doesn't cry wolf.

Two bars, as in Obico:

* **warning**: the smoothed score is clearly above normal → alert.
* **failure**: ``ESCALATING_FACTOR`` × higher still → pause, but only after
  it held for ``PAUSE_CONFIRM_CHECKS`` checks in a row (we're stricter than
  Obico here: pausing a good print by mistake is worse than a late pause).

Nothing here ever *stops* a print; that's always the user's call.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

CHECK_INTERVAL_S = 10.0  # Obico's parameters are tuned for one frame / 10 s
DETECTION_THRESH = 0.08  # a box counts towards the score above this
NMS_THRESH = 0.45
BOX_DRAW_THRESH = 0.2  # boxes worth showing the user

# Obico's tuned hyper-parameters (backend/config/settings.py, 1st gen).
EWM_SPAN = 12
ROLLING_WIN_SHORT = 310
ROLLING_WIN_LONG = 7200
THRESHOLD_LOW = 0.38
THRESHOLD_HIGH = 0.78
INIT_SAFE_FRAME_NUM = 30  # first ~5 min of a session (at 10 s checks): never alert
INIT_SAFE_S = INIT_SAFE_FRAME_NUM * CHECK_INTERVAL_S
ROLLING_MEAN_SHORT_MULTIPLE = 3.8
ESCALATING_FACTOR = 1.75

TEACH_STEP = 0.5  # "Looks fine": move the camera's normal halfway to what it saw
TEACH_MAX = 1.0  # ...but never treat more than this much "failure" as normal
PAUSE_AFTER_DEFAULT_S = 30.0  # failure must hold this long (watched time) to pause
PAUSE_CONFIRM_CHECKS = 4  # = 30 s at 10 s checks: failing at 0, 10, 20 and 30 s

OK, WARNING, FAILURE = "ok", "warning", "failure"

SENSITIVITY = {"low": 0.8, "normal": 1.0, "high": 1.2}
CHECK_INTERVALS_S = (10, 20, 30)
PAUSE_AFTER_S = (30, 60, 120)




# ------------------------------------------------------------------ model I/O
def preprocess(frame: np.ndarray, size: tuple[int, int] = (416, 416)) -> np.ndarray:
    """BGR frame -> the network's NCHW float input (RGB, 0..1)."""
    w, h = size
    resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0


@dataclass(frozen=True)
class Detection:
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 as 0..1 fractions


def _nms(boxes: np.ndarray, confs: np.ndarray, thresh: float) -> list[int]:
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    order = confs.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i, rest = order[0], order[1:]
        keep.append(int(i))
        w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        union = areas[i] + areas[rest] - inter
        overlap = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = rest[overlap <= thresh]
    return keep


def postprocess(outputs, conf_thresh: float = DETECTION_THRESH) -> list[Detection]:
    """The network's raw ``[boxes (1,N,1,4), confs (1,N,classes)]`` -> detections."""
    boxes = np.asarray(outputs[0], dtype=np.float32)[0, :, 0, :]
    confs = np.asarray(outputs[1], dtype=np.float32)[0].max(axis=1)
    mask = confs > conf_thresh
    boxes, confs = boxes[mask], confs[mask]
    if not len(confs):
        return []
    keep = _nms(boxes, confs, NMS_THRESH)
    out = []
    for i in keep:
        x1, y1, x2, y2 = (float(np.clip(v, 0.0, 1.0)) for v in boxes[i])
        out.append(Detection(float(confs[i]), (x1, y1, x2, y2)))
    return sorted(out, key=lambda d: d.confidence, reverse=True)


Area = tuple[float, float, float, float]  # x1, y1, x2, y2 as 0..1 fractions
MIN_AREA_SIDE = 0.05


def clean_area(value) -> Area | None:
    """A stored watch area, or None (the whole picture) if it isn't one."""
    try:
        x1, y1, x2, y2 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((min(max(x1, 0.0), 1.0), min(max(x2, 0.0), 1.0)))
    y1, y2 = sorted((min(max(y1, 0.0), 1.0), min(max(y2, 0.0), 1.0)))
    if x2 - x1 < MIN_AREA_SIDE or y2 - y1 < MIN_AREA_SIDE:
        return None
    return (x1, y1, x2, y2)


def in_area(detections: list[Detection], area: Area | None) -> list[Detection]:
    """Only the detections centred inside ``area`` (the print bed): a cable
    or a shelf off to the side is never a failed print."""
    if area is None:
        return detections
    x1, y1, x2, y2 = area
    return [
        d for d in detections
        if x1 <= (d.box[0] + d.box[2]) / 2 <= x2 and y1 <= (d.box[1] + d.box[3]) / 2 <= y2
    ]


# -------------------------------------------------------------- the decision
@dataclass
class Baseline:
    """This printer's long-term normal (persisted between sessions)."""

    rolling_mean_long: float = 0.0
    lifetime_frames: int = 0

    def to_dict(self) -> dict:
        return {"rolling_mean_long": self.rolling_mean_long, "lifetime_frames": self.lifetime_frames}

    @classmethod
    def from_dict(cls, data) -> "Baseline":
        try:
            mean = float(data.get("rolling_mean_long", 0.0))
            frames = int(data.get("lifetime_frames", 0))
        except (AttributeError, TypeError, ValueError):
            return cls()
        if not np.isfinite(mean) or mean < 0 or frames < 0:
            return cls()
        return cls(mean, frames)


def _rolling(p: float, mean: float, count: int, window: int) -> float:
    return mean + (p - mean) / float(window if window <= count else count + 1)


@dataclass
class FailureScorer:
    """Obico's prediction state machine, plus a stricter pause rule."""

    baseline: Baseline = field(default_factory=Baseline)
    sensitivity: float = 1.0
    ewm_mean: float = 0.0
    rolling_mean_short: float = 0.0
    frames: int = 0  # checks this session
    failure_streak: int = 0
    last_p: float = 0.0
    # Pause once failing checks have spanned this much watched time (the
    # user's "pause once sure for"), however often it checks.
    pause_after_s: float = PAUSE_AFTER_DEFAULT_S
    streak_started_s: float = 0.0
    # Seconds between checks (set with the interval setting) and how long
    # this session has been watched: the quiet start of a print is ~5
    # minutes of watching however often it checks, even if that changes.
    interval_s: float = CHECK_INTERVAL_S
    watched_s: float = 0.0

    def teach_normal(self) -> None:
        """The user said an alert was a false alarm ("Looks fine"): what the
        model sees right now is normal for this camera. Raise the long-term
        normal toward it, partly and capped, so the same harmless thing
        alerts less; it drifts back down by itself if the scene changes."""
        b = self.baseline
        if self.ewm_mean > b.rolling_mean_long and b.rolling_mean_long < TEACH_MAX:
            # Only ever raises it (a normal already above the cap stays).
            b.rolling_mean_long += TEACH_STEP * (self.ewm_mean - b.rolling_mean_long)
            b.rolling_mean_long = min(b.rolling_mean_long, TEACH_MAX)
        # Count it as real history, so the next checks don't simply undo it.
        b.lifetime_frames = max(b.lifetime_frames, ROLLING_WIN_SHORT)

    def reset_session(self) -> None:
        """A new print (or watching resumed): forget short-term history."""
        self.ewm_mean = self.rolling_mean_short = self.last_p = 0.0
        self.frames = self.failure_streak = 0
        self.watched_s = 0.0

    def update(self, detections: list[Detection]) -> str:
        """Fold one check in; returns OK, WARNING or FAILURE (= pause now)."""
        p = sum(d.confidence for d in detections)
        alpha = 2 / (EWM_SPAN + 1)
        b = self.baseline
        self.last_p = p
        self.frames += 1
        self.watched_s += self.interval_s
        b.lifetime_frames += 1
        self.ewm_mean = p * alpha + self.ewm_mean * (1 - alpha)
        self.rolling_mean_short = _rolling(p, self.rolling_mean_short, self.frames, ROLLING_WIN_SHORT)
        b.rolling_mean_long = _rolling(p, b.rolling_mean_long, b.lifetime_frames, ROLLING_WIN_LONG)

        if self._failing(ESCALATING_FACTOR):
            if self.failure_streak == 0:
                self.streak_started_s = self.watched_s
            self.failure_streak += 1
            held = self.watched_s - self.streak_started_s
            if self.failure_streak >= 2 and held >= self.pause_after_s - 1e-6:
                return FAILURE
            return WARNING  # very likely, but not sure enough to pause yet
        self.failure_streak = 0
        return WARNING if self._failing(1.0) else OK

    def _failing(self, escalating: float) -> bool:
        if self.watched_s < INIT_SAFE_S - 1e-6:
            return False
        adjusted = (self.ewm_mean - self.baseline.rolling_mean_long) * self.sensitivity / escalating
        if adjusted < THRESHOLD_LOW:
            return False
        if adjusted > THRESHOLD_HIGH:
            return True
        return adjusted > (self.rolling_mean_short - self.baseline.rolling_mean_long) * ROLLING_MEAN_SHORT_MULTIPLE

    def risk(self) -> float:
        """0..1 for a meter: <1/3 fine, 1/3..2/3 warning, >2/3 failure (Obico's scale)."""
        warn = (self.rolling_mean_short - self.baseline.rolling_mean_long) * ROLLING_MEAN_SHORT_MULTIPLE
        warn = min(THRESHOLD_HIGH, max(THRESHOLD_LOW, warn))
        fail = warn * ESCALATING_FACTOR
        p = (self.ewm_mean - self.baseline.rolling_mean_long) * self.sensitivity

        def scale(v, lo, hi, out_lo, out_hi):
            return min(out_hi, max(out_lo, out_lo + (v - lo) * (out_hi - out_lo) / (hi - lo)))

        if p > fail:
            return scale(p, fail, fail * 1.5, 2 / 3, 1.0)
        if p > warn:
            return scale(p, warn, fail, 1 / 3, 2 / 3)
        return scale(p, 0, warn, 0, 1 / 3)


def draw_detections(frame: np.ndarray, detections: list[Detection], area: Area | None = None) -> np.ndarray:
    """A copy of ``frame`` with the worth-showing boxes outlined (for alerts),
    and the watch area, if there is one, in a thin gray line."""
    out = frame.copy()
    h, w = out.shape[:2]
    if area is not None:
        x1, y1, x2, y2 = area
        cv2.rectangle(out, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (180, 180, 180), max(1, w // 800))
    for d in detections:
        if d.confidence < BOX_DRAW_THRESH:
            continue
        x1, y1, x2, y2 = d.box
        p1, p2 = (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h))
        cv2.rectangle(out, p1, p2, (40, 60, 240), max(2, w // 400))
        cv2.putText(out, f"{d.confidence:.0%}", (p1[0] + 4, p1[1] + 22), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (40, 60, 240), 2, cv2.LINE_AA)
    return out
