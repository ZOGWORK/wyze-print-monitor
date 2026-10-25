"""Persistent settings and secret storage.

Non-secret settings live in a small JSON file under the per-user config dir.
Secrets (Wyze password, API key id/secret) live in the OS credential store
via ``keyring`` and are never written to disk by this module.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

APP_NAME = "wyze-print-monitor"
KEYRING_SERVICE = "wyze-print-monitor"

# Secret names stored in the OS keyring under KEYRING_SERVICE.
SECRET_PASSWORD = "wyze_password"
SECRET_KEY_ID = "wyze_key_id"
SECRET_API_KEY = "wyze_api_key"
# The full RTSP URL (it embeds the camera username/password) is a secret too:
# settings.json only ever stores a redacted copy.
SECRET_RTSP_URL = "rtsp_url"


def clean_api_credential(value: str | None) -> str:
    """Normalize a pasted Wyze API key ID / API key.

    Both are sent as HTTP headers, and copying them from the Wyze developer
    console easily picks up a trailing newline, tab or invisible character —
    which the HTTP client rejects ("Forbidden control character detected in
    headers"). Real keys never contain whitespace or control characters, so
    drop them all.
    """
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isprintable() and not ch.isspace())


def redact_credentials_in_url(url: str) -> str:
    """Return *url* with any embedded userinfo replaced by ``***``.

    ``rtsp://user:pass@192.168.1.10/live`` becomes ``rtsp://***@192.168.1.10/live``.
    """
    if "@" not in url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if not parts.username and not parts.password:
            return url
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit(parts._replace(netloc=f"***@{host}"))
    except Exception:
        return url


def effective_rtsp_url(settings: "Settings") -> str:
    """The real RTSP URL: keyring secret wins, settings file is the fallback."""
    return get_secret(SECRET_RTSP_URL) or settings.rtsp_url


def config_dir() -> str:
    """Per-user config directory for this app."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".config", APP_NAME)


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def default_media_dirs() -> tuple[str, str]:
    """Default snapshot / video folders (Pictures/Videos under home)."""
    home = os.path.expanduser("~")
    return (
        os.path.join(home, "Pictures", APP_NAME),
        os.path.join(home, "Videos", APP_NAME),
    )


@dataclass
class Settings:
    rtsp_url: str = ""
    camera_mac: str = ""
    camera_nickname: str = ""
    wyze_email: str = ""
    timelapse_interval_sec: int = 10
    snapshot_dir: str = ""
    video_dir: str = ""
    always_on_top: bool = True
    # Embed the live view + camera controls inside eufyMake Studio's Device tab
    # (Windows only). dock_printer_row is the 1-based row in Studio's
    # "My Printer" list that has this camera; 0 = show for any printer.
    dock_in_studio: bool = True
    dock_printer_row: int = 1
    # Picture adjustments (adjust.ImageAdjustments.to_dict()); {} = none.
    image_adjustments: dict = field(default_factory=dict)
    # Look for new releases on GitHub at startup and every few hours.
    check_updates: bool = True
    # Video views: fill the space (crop) instead of fitting the whole picture.
    video_fill: bool = False
    # Stream quality: auto | 1080p | 720p | 360p (see quality.py).
    stream_quality: str = "auto"
    # Print watch (needs the separately downloaded model, see failure_model).
    ai_watch: bool = False
    # Pause through Studio when the model is very sure; stopping is always
    # left to the user.
    ai_auto_pause: bool = True
    ai_sensitivity: str = "normal"  # low | normal | high
    sound_alerts: bool = True
    # What alerts: "all" (possible problems too) | "failures" (likely failures only)
    ai_alert_level: str = "all"
    # Alert sound: "alarm" (loops for failures until you respond) | "beep" | "off"
    ai_sound: str = "alarm"
    ai_notifications: bool = True  # Windows notifications
    ai_popup: bool = True  # pop-up alert card on top of other windows
    ai_pause_after_s: int = 30  # a failure must hold this long before pausing
    ai_check_interval_s: int = 10  # seconds between AI checks
    # Without Studio there's no telling whether a print is running: watch
    # anyway (alerts only) instead of waiting for Studio to show one.
    ai_watch_without_studio: bool = False
    # Where the print is, per camera: {camera_key: [x1, y1, x2, y2]} as 0..1
    # fractions of the picture. Anything the model sees outside it is ignored.
    ai_watch_area: dict = field(default_factory=dict)
    # This camera's long-term "normal" for the model (failure_detect.Baseline).
    ai_baseline: dict = field(default_factory=dict)

    def resolved_snapshot_dir(self) -> str:
        if self.snapshot_dir:
            return self.snapshot_dir
        return default_media_dirs()[0]

    def resolved_video_dir(self) -> str:
        if self.video_dir:
            return self.video_dir
        return default_media_dirs()[1]


def load_settings(path: str | None = None) -> Settings:
    """Load settings from JSON; unknown keys are ignored, missing file -> defaults."""
    path = path or settings_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    valid = {f.name for f in Settings.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return Settings(**{k: v for k, v in data.items() if k in valid})


def save_settings(settings: Settings, path: str | None = None) -> None:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = asdict(settings)
    # Never persist camera credentials to disk: the settings file keeps only a
    # redacted copy, the real URL lives in the OS keyring (see SECRET_RTSP_URL).
    data["rtsp_url"] = redact_credentials_in_url(data.get("rtsp_url", ""))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _keyring_exc():
    import keyring.errors

    return (keyring.errors.NoKeyringError, keyring.errors.InitError)


# In-memory fallback for machines with no keyring backend: secrets saved here
# work for the rest of the session but don't survive a restart.
_session_secrets: dict[str, str] = {}


def save_secret(name: str, value: str) -> bool:
    """Best-effort: returns False on machines with no keyring backend, in
    which case the secret is kept in memory for this session only."""
    import keyring

    try:
        keyring.set_password(KEYRING_SERVICE, name, value)
    except _keyring_exc():
        _session_secrets[name] = value
        return False
    _session_secrets.pop(name, None)
    return True


def get_secret(name: str) -> str | None:
    import keyring

    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except _keyring_exc():
        return _session_secrets.get(name)


def delete_secret(name: str) -> None:
    _session_secrets.pop(name, None)
    import keyring

    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception:
        pass
