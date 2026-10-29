"""Picture adjustments applied to the live stream.

The Wyze cloud API exposes almost none of the V3's image controls, so the
adjustments a normal video pipeline offers — brightness, contrast, exposure,
gamma, saturation, color temperature, sharpness, rotate/flip and a digital
zoom with pan — are applied to every decoded frame here. They run on the
stream thread (see :class:`capture.StreamReader`) and affect everything
downstream: the live view, snapshots, recordings and timelapses.

Kept Qt-free so it's unit-testable; the per-channel tone curve is cached,
so the common case (tone tweaks only) costs one ``cv2.LUT`` per frame.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields, replace
from functools import lru_cache

import cv2
import numpy as np

# (min, max) for each numeric setting; values outside are clamped on load.
LIMITS: dict[str, tuple[float, float]] = {
    "brightness": (-100, 100),
    "contrast": (-100, 100),
    "exposure": (-2.0, 2.0),   # EV stops
    "gamma": (0.4, 2.5),       # >1 lifts midtones
    "saturation": (-100, 100),  # -100 = black & white
    "temperature": (-100, 100),  # - cool / + warm
    "sharpness": (0, 100),
    "zoom": (1.0, 4.0),
    "pan_x": (-1.0, 1.0),
    "pan_y": (-1.0, 1.0),
}
ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class ImageAdjustments:
    brightness: int = 0
    contrast: int = 0
    exposure: float = 0.0
    gamma: float = 1.0
    saturation: int = 0
    temperature: int = 0
    sharpness: int = 0
    rotation: int = 0
    flip_h: bool = False
    flip_v: bool = False
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    # Balance the light automatically (see AutoLight); the sliders apply on top.
    auto_light: bool = False

    def is_identity(self) -> bool:
        return self == IDENTITY

    def manual(self) -> "ImageAdjustments":
        """Just the hand-set part (what apply_adjustments does)."""
        return replace(self, auto_light=False) if self.auto_light else self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "ImageAdjustments":
        """Build from saved settings: unknown keys ignored, bad values
        dropped, numbers clamped to their range."""
        if not isinstance(data, dict):
            return cls()
        values: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            raw = data[f.name]
            try:
                if f.type in ("bool", bool):
                    values[f.name] = bool(raw)
                elif f.name == "rotation":
                    rot = int(raw) % 360
                    values[f.name] = rot if rot in ROTATIONS else 0
                else:
                    lo, hi = LIMITS[f.name]
                    num = float(raw)
                    if not math.isfinite(num):
                        continue  # NaN/inf would survive min/max: use the default
                    num = min(max(num, lo), hi)
                    values[f.name] = int(round(num)) if f.type in ("int", int) else num
            except (TypeError, ValueError):
                continue
        return cls(**values)


IDENTITY = ImageAdjustments()


@lru_cache(maxsize=32)
def _tone_lut(
    brightness: int, contrast: int, exposure: float, gamma: float, temperature: int
) -> np.ndarray:
    """One 256-entry curve per BGR channel combining every tone setting."""
    x = np.linspace(0.0, 1.0, 256)
    warm = temperature / 100.0
    # BGR channel gains: warm lifts red / lowers blue, cool the reverse.
    gains = (1.0 - 0.25 * warm, 1.0, 1.0 + 0.25 * warm)
    contrast_factor = 2.0 ** (contrast / 50.0)  # -100 → ¼×, +100 → 4×
    channels = []
    for gain in gains:
        y = np.clip(x * (2.0 ** exposure) * gain, 0.0, 1.0)
        y = y ** (1.0 / gamma)
        y = (y - 0.5) * contrast_factor + 0.5
        y = y + brightness / 200.0
        channels.append(np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8))
    return np.stack(channels, axis=-1).reshape(256, 1, 3)


@lru_cache(maxsize=32)
def _saturation_matrix(saturation: int) -> np.ndarray:
    """3×3 BGR matrix blending each pixel with its own luma: one
    ``cv2.transform`` pass instead of a gray conversion plus a blend."""
    s = 1.0 + saturation / 100.0
    luma = np.array([0.114, 0.587, 0.299])  # BGR weights (Rec. 601)
    return (s * np.eye(3) + (1.0 - s) * np.tile(luma, (3, 1))).astype(np.float32)


@lru_cache(maxsize=32)
def _sharpen_kernel(sharpness: int) -> np.ndarray:
    """3×3 unsharp-style kernel (sums to 1, so flat areas are unchanged)."""
    a = sharpness / 100.0 * 0.6
    k = np.full((3, 3), -a / 8.0, np.float32)
    k[1, 1] = 1.0 + a
    return k


def zoom_crop(frame: np.ndarray, zoom: float, pan_x: float, pan_y: float) -> np.ndarray:
    """The region a digital zoom shows (a view, not a copy)."""
    h, w = frame.shape[:2]
    cw, ch = max(int(w / zoom), 1), max(int(h / zoom), 1)
    # pan -1..1 slides the crop window across the full free range.
    x0 = int((w - cw) / 2 * (1 + pan_x))
    y0 = int((h - ch) / 2 * (1 + pan_y))
    return frame[y0 : y0 + ch, x0 : x0 + cw]


def screen_to_source_delta(
    dx: float, dy: float, rotation: int, flip_h: bool, flip_v: bool
) -> tuple[float, float]:
    """Map a movement seen on screen back into the camera's own axes.

    ``apply_adjustments`` crops (zoom/pan) first and flips/rotates after, so
    a drag on the displayed picture has to be un-rotated, then un-flipped,
    before it can change ``pan_x`` / ``pan_y``.
    """
    rotation %= 360
    if rotation == 90:  # display (x', y') = (h - y, x)
        dx, dy = dy, -dx
    elif rotation == 180:
        dx, dy = -dx, -dy
    elif rotation == 270:  # display (x', y') = (y, w - x)
        dx, dy = -dy, dx
    if flip_h:
        dx = -dx
    if flip_v:
        dy = -dy
    return dx, dy


def fit_frame(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Fit ``frame`` into ``size`` (width, height) keeping its proportions,
    padding with black. A video file has one fixed size, so a frame whose
    shape changed mid-capture (rotated 90°, new stream) is letterboxed
    rather than stretched."""
    width, height = size
    h, w = frame.shape[:2]
    if (w, h) == (width, height):
        return frame
    scale = min(width / w, height / h)
    new_w, new_h = max(int(round(w * scale)), 1), max(int(round(h * scale)), 1)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), dtype=frame.dtype)
    x0, y0 = (width - new_w) // 2, (height - new_h) // 2
    canvas[y0 : y0 + new_h, x0 : x0 + new_w] = resized
    return canvas


def apply_adjustments(frame: np.ndarray, adj: ImageAdjustments) -> np.ndarray:
    """Return ``frame`` (BGR uint8) with ``adj`` applied. Never modifies the
    input; returns it unchanged when there's nothing to do."""
    adj = adj.manual()  # auto light is stateful: the stream thread runs AutoLight
    if adj.is_identity():
        return frame
    h, w = frame.shape[:2]
    out = zoom_crop(frame, adj.zoom, adj.pan_x, adj.pan_y) if adj.zoom > 1.0 else frame
    # Color work runs on the (smaller) crop, before scaling back up.
    if (adj.brightness, adj.contrast, adj.exposure, adj.gamma, adj.temperature) != (0, 0, 0.0, 1.0, 0):
        lut = _tone_lut(adj.brightness, adj.contrast, round(adj.exposure, 2), round(adj.gamma, 2), adj.temperature)
        out = cv2.LUT(out, lut)
    if adj.saturation:
        out = cv2.transform(out, _saturation_matrix(adj.saturation))
    if adj.sharpness:
        out = cv2.filter2D(out, -1, _sharpen_kernel(adj.sharpness))
    if out.shape[:2] != (h, w):
        out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    if adj.flip_h and adj.flip_v:
        out = cv2.flip(out, -1)
    elif adj.flip_h:
        out = cv2.flip(out, 1)
    elif adj.flip_v:
        out = cv2.flip(out, 0)
    if adj.rotation == 90:
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
    elif adj.rotation == 180:
        out = cv2.rotate(out, cv2.ROTATE_180)
    elif adj.rotation == 270:
        out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if out is frame:  # flips/zoom of zero effect still must not alias input
        out = frame.copy()
    return out


class AutoLight:
    """Keeps the picture well exposed as the room's light changes.

    Every few frames it measures the brightness range of a small copy of
    the picture, then stretches it (dark and bright ends) and bends the
    midtones toward a comfortable level. Changes are eased in over a couple
    of seconds so the image doesn't "pump", and the stretch is capped so a
    dark, noisy scene isn't amplified into grain. One LUT per frame.
    """

    MEASURE_EVERY = 6
    EASE = 0.15
    TARGET_MID = 0.46
    MAX_STRETCH = 3.0
    GAMMA_RANGE = (0.6, 2.2)

    def __init__(self) -> None:
        self._count = 0
        self._lo, self._hi, self._gamma = 0.0, 255.0, 1.0
        self._fresh = True
        self._lut: np.ndarray | None = None

    def reset(self) -> None:
        self.__init__()

    @staticmethod
    def measure(frame: np.ndarray) -> tuple[float, float, float]:
        """(black point, white point, gamma) that would expose ``frame`` well."""
        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        lo, hi = (float(v) for v in np.percentile(gray, (1.0, 99.5)))
        span = max(hi - lo, 255.0 / AutoLight.MAX_STRETCH)
        lo = min(lo, 255.0 - span)
        hi = lo + span
        mid = float(np.clip((np.median(gray) - lo) / span, 0.02, 0.98))
        # y = x ** (1/gamma); choose gamma so the median lands on TARGET_MID.
        gamma = math.log(mid) / math.log(AutoLight.TARGET_MID)
        gamma = float(np.clip(gamma, *AutoLight.GAMMA_RANGE))
        return lo, hi, gamma

    def apply(self, frame: np.ndarray) -> np.ndarray:
        if self._count % self.MEASURE_EVERY == 0:
            lo, hi, gamma = self.measure(frame)
            ease = 1.0 if self._fresh else self.EASE
            self._fresh = False
            self._lo += (lo - self._lo) * ease
            self._hi += (hi - self._hi) * ease
            self._gamma += (gamma - self._gamma) * ease
            x = np.arange(256, dtype=np.float32)
            y = np.clip((x - self._lo) / max(self._hi - self._lo, 1.0), 0.0, 1.0) ** (1.0 / self._gamma)
            self._lut = np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8)
        self._count += 1
        return cv2.LUT(frame, self._lut)
