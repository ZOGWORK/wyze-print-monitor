"""Application entry point."""
from __future__ import annotations

import getpass
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .ui.main_window import MainWindow
from .config import config_dir
from .ui.styles import DARK_QSS, checkmark_qss


ICON_PATH = Path(__file__).parent / "assets" / "icon.png"
APP_USER_MODEL_ID = "ControlPointFTC.WyzePrintMonitor"
INSTALLER_MUTEX = "WyzePrintMonitorRunning"  # must match AppMutex in installer/*.iss


def app_icon() -> QIcon:
    """The app logo, or a green camera glyph if the asset is missing."""
    if ICON_PATH.is_file():
        icon = QIcon(str(ICON_PATH))
        if not icon.isNull():
            return icon
    from .ui.icons import ICON_CAMERA, glyph_icon

    return glyph_icon(ICON_CAMERA, "#62d361", 32)


LOG_NAME = "errors.log"
LOG_MAX_BYTES = 512 * 1024


def _log_errors() -> None:
    """The installed app has no console, so errors would vanish: keep them
    (and native crashes) in errors.log in the config folder."""
    try:
        folder = config_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, LOG_NAME)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".old")
        log = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - open for the app's lifetime
    except OSError:
        return
    import faulthandler
    import traceback

    from . import __version__

    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} started {__version__}\n")
    faulthandler.enable(log)

    def hook(kind, value, tb) -> None:
        log.write(f"{time.strftime('%H:%M:%S')} ")
        log.write("".join(traceback.format_exception(kind, value, tb)))

    sys.excepthook = hook  # Qt slots report their exceptions through this


def _set_windows_app_id() -> None:
    """Give the process its own taskbar identity, so Windows shows our icon
    (and groups our windows) instead of python.exe's."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _instance_name() -> str:
    return f"WyzePrintMonitor-{getpass.getuser()}"


def _claim_single_instance() -> QLockFile | None:
    """Atomically claim "the running copy". Returns the held lock, or None
    if another copy holds it. Must happen before any window or camera work,
    so two near-simultaneous launches can't both get through. A lock left
    by a crashed copy is detected as stale and taken over."""
    path = os.path.join(QStandardPaths.writableLocation(QStandardPaths.TempLocation), _instance_name() + ".lock")
    lock = QLockFile(path)
    lock.setStaleLockTime(0)  # stale = its owner process is gone
    return lock if lock.tryLock(0) else None


def _signal_running_instance(attempts: int = 20) -> bool:
    """Ask the running copy to come forward. It may still be starting up
    (lock taken, not listening yet), so retry for a few seconds."""
    for _ in range(attempts):
        socket = QLocalSocket()
        socket.connectToServer(_instance_name())
        if socket.waitForConnected(300):
            socket.write(b"show")
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return True
        time.sleep(0.2)
    return False


def _listen_for_other_launches(app: QApplication, window: MainWindow) -> QLocalServer:
    """Opening the app again (Start menu, desktop icon) brings this one up
    instead of starting a second copy that would fight over the camera."""
    server = QLocalServer(app)

    def on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.disconnected.connect(connection.deleteLater)
        window.show_window()

    server.newConnection.connect(on_connection)
    if not server.listen(_instance_name()):
        # We hold the instance lock, so any existing endpoint is a leftover
        # from a crashed copy: clear it and try once more.
        QLocalServer.removeServer(_instance_name())
        if not server.listen(_instance_name()):
            print(f"Single-instance listener unavailable: {server.errorString()}", file=sys.stderr)
    return server


def _hold_installer_mutex():
    """A named mutex the installer's AppMutex checks, so setup/uninstall
    knows the app is running and closes it first."""
    try:
        import ctypes

        return ctypes.windll.kernel32.CreateMutexW(None, False, INSTALLER_MUTEX)
    except (AttributeError, OSError):
        return None


def _make_tray(app: QApplication, window: MainWindow) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(app.windowIcon(), app)
    tray.setToolTip("Wyze Print Monitor")
    menu = QMenu()
    menu.addAction("Open Wyze Print Monitor", window.show_window)
    menu.addAction("Schedule print…", window.schedule_print)
    menu.addAction("Print queue", window.show_print_queue)
    menu.addAction("Settings…", window._open_settings)
    menu.addAction("Check for updates…", lambda: window.update_manager.check(silent=False))
    menu.addSeparator()
    menu.addAction("Exit", window.quit_app)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: window.show_window()
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick)
        else None
    )
    tray._menu = menu  # keep the menu alive with the tray
    tray.show()
    window.update_manager.available.connect(
        lambda info: tray.showMessage(
            "Update available",
            f"Wyze Print Monitor {info.version} is ready to install. Click Update in the app.",
            QSystemTrayIcon.Information,
            8000,
        )
    )
    # Clicking a notification does what it was about: install an update, or
    # bring up the window to look at a print-watch alert.
    last = {"kind": ""}
    window.update_manager.available.connect(lambda _info: last.update(kind="update"))

    def print_alert(title: str, message: str) -> None:
        last["kind"] = "alert"
        tray.showMessage(title, message, QSystemTrayIcon.Warning, 15000)

    window.print_watch.notify.connect(print_alert)

    def schedule_note(title: str, message: str) -> None:
        last["kind"] = "schedule"
        tray.showMessage(title, message, QSystemTrayIcon.Information, 15000)

    window.print_scheduler.notify.connect(schedule_note)

    def clicked() -> None:
        if last["kind"] == "update":
            window.update_manager.install()
        elif last["kind"] == "schedule":
            window.show_print_queue()
        else:
            window.show_window()

    tray.messageClicked.connect(clicked)
    return tray


def self_test(report_path: str) -> int:
    """Check a packaged build can do its job, without opening a window.

    ``WyzePrintMonitor.exe --self-test report.txt`` writes what it found and
    exits 0 only if video decoding, the OS credential store, Qt networking,
    the Wyze client, the print watch's AI/audio runtimes and the app icon
    are all usable. Run by the release
    workflow on every build.
    """
    from . import __version__

    lines = [f"Wyze Print Monitor {__version__}", f"frozen: {bool(getattr(sys, 'frozen', False))}"]
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            result, good = fn()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            result, good = f"{type(exc).__name__}: {exc}", False
        lines.append(f"{'OK  ' if good else 'FAIL'} {label}: {result}")
        ok = ok and good

    def opencv():
        import cv2

        has_ffmpeg = cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG)
        return f"{cv2.__version__}, FFmpeg backend {'present' if has_ffmpeg else 'MISSING'}", has_ffmpeg

    def credentials():
        import keyring

        backend = keyring.get_keyring()
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        return name, "fail" not in name and "null" not in name.lower()

    def qt():
        import PySide6
        from PySide6.QtNetwork import QLocalServer  # noqa: F401

        return PySide6.__version__, True

    def wyze():
        from wyzeapy.wyze_auth_lib import get_ssl_context

        from .wyze_control import WyzeController  # noqa: F401

        # Builds the exact TLS context login uses: needs certifi's bundle and
        # wyzeapy's own wyze_api_ca.pem to be packaged.
        get_ssl_context()
        return "client + Wyze TLS certificates OK", True

    def ai_runtime():
        import onnxruntime as ort

        providers = ort.get_available_providers()  # loads the native runtime
        return f"onnxruntime {ort.__version__}, {', '.join(providers)}", "CPUExecutionProvider" in providers

    def audio():
        import av

        return f"PyAV {av.__version__}", True

    check("OpenCV", opencv)
    check("AI runtime (print watch)", ai_runtime)
    check("Audio (sound alerts)", audio)
    check("Credential store", credentials)
    check("Qt", qt)
    check("Wyze client", wyze)
    check("App icon", lambda: (str(ICON_PATH), ICON_PATH.is_file()))
    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        report = sys.argv[index + 1] if len(sys.argv) > index + 1 else "self-test.txt"
        return self_test(report)
    _log_errors()
    if sys.platform == "win32":
        _set_windows_app_id()  # must happen before any window exists
    app = QApplication(sys.argv)
    app.setApplicationName("Wyze Print Monitor")
    app.setOrganizationName("Control-Point-FTC")
    app.setWindowIcon(app_icon())
    app.setStyleSheet(DARK_QSS + checkmark_qss(os.path.join(config_dir(), "ui")))
    instance_lock = _claim_single_instance()
    if instance_lock is None:
        _signal_running_instance()
        return 0
    mutex = _hold_installer_mutex() if sys.platform == "win32" else None  # noqa: F841 - held for the app's lifetime
    window = MainWindow()
    server = _listen_for_other_launches(app, window)  # noqa: F841 - kept alive by app parent

    dock = None
    if sys.platform == "win32":
        from .ui.studio_dock import StudioDock

        # The panels live inside eufyMake Studio's Device tab; this window and
        # the tray icon are the way back to the full app.
        from .ui.schedule_ui import StudioScheduleDock

        dock = StudioDock(window)
        # The ▴ on Studio's Print button and the print queue in its printer list.
        window._schedule_dock = StudioScheduleDock(window, window.print_scheduler)
        app.setQuitOnLastWindowClosed(False)
        window._tray = _make_tray(app, window)

    if dock is not None and window.settings.dock_in_studio and dock.studio_running():
        window._status("Showing the camera inside eufyMake Studio's Device tab.", 5000)
        if window._tray is not None:
            # No window opens in this case, so say the app did start.
            window._tray.showMessage(
                "Wyze Print Monitor is running",
                "It's inside eufyMake Studio: the camera on the Device tab, and ▴ on Print to "
                "schedule. Click here (or the tray icon) for the full window.",
                QSystemTrayIcon.Information,
                8000,
            )
    else:
        window.show()
    code = app.exec()
    instance_lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
