"""Main window: eufyMake Studio Device-tab layout — camera sidebar on the left,
live monitor in the center, Control + Camera panels on the right."""
from __future__ import annotations

import datetime
import os
import time

import cv2
import numpy as np
from PySide6.QtCore import QEvent, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..async_runner import AsyncRunner
from ..adjust import ImageAdjustments, apply_adjustments
from ..capture import ClipRecorder, StreamReader, snapshot_path
from ..quality import display_width_for
from ..quality import normalize as normalize_quality
from ..config import (
    SECRET_API_KEY,
    SECRET_KEY_ID,
    SECRET_PASSWORD,
    Settings,
    clean_api_credential,
    effective_rtsp_url,
    get_secret,
    load_settings,
    save_settings,
)
from ..timelapse import TimelapseCollector, compile_timelapse
from ..wyze_control import (
    LOGIN_RETRY_DELAYS_S,
    CameraInfo,
    CameraState,
    WyzeController,
    describe_login_error,
)
from .. import __version__, failure_model
from .binder import PanelBinder
from .icons import ICON_CAMERA, ICON_FOLDER, ICON_FULLSCREEN, ICON_RECORD, ICON_TIMER, tool_button
from .settings_dialog import SettingsDialog, forget_secrets
from .alert_popup import AlertPopup
from .print_schedule import PrintScheduler
from .print_watch_ui import PrintWatch
from .update_ui import UpdateManager
from .views import VIEWS_QSS, CameraPanel, VideoView

RIGHT_COLUMN_WIDTH = 380
LABEL_REFRESH_S = 0.5


class TimelapseCompileWorker(QThread):
    """Compiles saved JPEG frames into an MP4 off the GUI thread."""

    done = Signal(str, int)  # output path, frame count
    failed = Signal(str)

    def __init__(self, frames_dir: str, output_path: str, parent=None) -> None:
        super().__init__(parent)
        self._frames_dir = frames_dir
        self._output_path = output_path

    def run(self) -> None:
        try:
            count = compile_timelapse(self._frames_dir, self._output_path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            self.failed.emit(f"Could not compile timelapse: {exc}")
        else:
            self.done.emit(self._output_path, count)


class MainWindow(QMainWindow):
    # Every status-bar message, re-broadcast for views outside this window
    # (the panels docked inside eufyMake Studio).
    status_message = Signal(str, int)
    # The picture adjustments changed (from any panel); views re-sync.
    adjustments_changed = Signal(object)
    # A snapshot / clip / timelapse file was written (path).
    capture_saved = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        # VideoViews showing the live picture (this window's and the
        # Studio-docked one); each PanelBinder registers its view here.
        self.video_views: list[VideoView] = []
        # While docked into Studio, closing this window only hides it; the app
        # keeps running until File → Exit (or the tray's Exit).
        self.hide_on_close = False
        self._quitting = False
        self._shut_down = False
        self._fullscreen_from_hidden = False
        self.setWindowTitle("Wyze Print Monitor")
        self.resize(1180, 720)

        self.settings = load_settings()
        if failure_model.consume_enable_marker():
            self.settings.ai_watch = True  # chosen in the installer
            save_settings(self.settings)
        self.runner = AsyncRunner(self)
        self.runner.start()
        self.controller = WyzeController(self.runner)

        self._reader: StreamReader | None = None
        self._retired_readers: list[StreamReader] = []
        self._close_pending = False
        self._last_labels = 0.0
        self.active_quality = ""
        self._recorder: ClipRecorder | None = None
        self._timelapse: TimelapseCollector | None = None
        self._timelapse_write_warned = False
        self._timelapse_frames_dir = ""
        self._compile_worker: TimelapseCompileWorker | None = None
        self._cameras: list[CameraInfo] = []
        self._last_frame: np.ndarray | None = None
        self._rec_start: float | None = None
        self._stream_state = "idle"

        self.adjustments = ImageAdjustments.from_dict(self.settings.image_adjustments)
        # Slider drags fire many changes a second: save once they settle.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(lambda: save_settings(self.settings))

        # Re-login after temporary failures (rate limit, network), see
        # wyze_control.LOGIN_RETRY_DELAYS_S.
        self._login_retries = 0
        self._login_retry_timer = QTimer(self)
        self._login_retry_timer.setSingleShot(True)
        self._login_retry_timer.timeout.connect(lambda: self._on_connect(silent=True))

        self.update_manager = UpdateManager(self)
        self.print_watch = PrintWatch(self)
        self.print_scheduler = PrintScheduler(self)
        self._queue_window = None
        self.alert_popup = AlertPopup(self)
        self.update_manager.available.connect(self._on_update_available)

        self._build_ui()
        self._apply_always_on_top()
        if self.settings.rtsp_url:
            self._start_stream()
        QTimer.singleShot(500, self._auto_connect)
        self.update_manager.start()

    # ------------------------------------------------------------------ UI build
    def _build_ui(self) -> None:
        """The full window: the live picture beside the camera panel.

        The control widgets that own the behavior (buttons, toggles, status
        labels — built by the ``_build_*`` helpers below) live in a hidden
        host. What you see are the shared views from ``ui.views``, bound to
        those controls by a ``PanelBinder``, exactly like the panels docked
        in eufyMake Studio.
        """
        self._build_menus()

        self._controls = QWidget(self)
        self._controls.hide()
        controls = QHBoxLayout(self._controls)
        controls.addWidget(self._build_sidebar())
        controls.addLayout(self._build_center())
        controls.addLayout(self._build_right_column())

        main = QWidget()
        main.setStyleSheet(VIEWS_QSS)
        row = QHBoxLayout(main)
        row.setContentsMargins(10, 10, 10, 10)
        row.setSpacing(10)
        self.view = VideoView()
        video_card = QFrame()
        video_card.setObjectName("surface")
        video_card.setAttribute(Qt.WA_StyledBackground, True)
        QVBoxLayout(video_card).setContentsMargins(0, 0, 0, 0)
        video_card.layout().addWidget(self.view)
        row.addWidget(video_card, 1)
        self.camera_panel = CameraPanel()
        self._side_card = QFrame()
        self._side_card.setObjectName("surface")
        self._side_card.setAttribute(Qt.WA_StyledBackground, True)
        self._side_card.setFixedWidth(RIGHT_COLUMN_WIDTH)
        QVBoxLayout(self._side_card).setContentsMargins(0, 0, 0, 0)
        self._side_card.layout().addWidget(self.camera_panel)
        row.addWidget(self._side_card)
        self.setCentralWidget(main)
        self.binder = PanelBinder(self, self.view, self.camera_panel)
        self.update_btn = QPushButton("Update available")
        self.update_btn.setObjectName("accentButton")
        self.update_btn.clicked.connect(self.update_manager.install)
        self.update_btn.hide()
        self.statusBar().addPermanentWidget(self.update_btn)
        self._status("Ready")
        self._set_controls_enabled(False)

    @staticmethod
    def _card(title: str) -> tuple[QFrame, QHBoxLayout, QVBoxLayout]:
        """A Studio-style card: header bar (title + right-side slot) over a body."""
        card = QFrame()
        card.setObjectName("card")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget()
        header.setObjectName("cardHeader")
        header.setAttribute(Qt.WA_StyledBackground, True)
        header.setFixedHeight(36)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(12, 0, 10, 0)
        header_row.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("cardTitle")
        header_row.addWidget(label)
        header_row.addStretch(1)
        outer.addWidget(header)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)
        return card, header_row, body

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _build_sidebar(self) -> QWidget:
        """Left device list, Studio "My Printer" style. Selects the camera."""
        card, _header, body = self._card("My Camera")
        card.setFixedWidth(220)
        self.camera_list = QListWidget()
        self.camera_list.setObjectName("cameraList")
        self.camera_list.currentRowChanged.connect(self._on_sidebar_camera_selected)
        body.addWidget(self.camera_list)
        return card

    def _tool_button(self, glyph: str, text: str, tip: str, checkable: bool = False) -> QToolButton:
        return tool_button(glyph, text, tip, checkable)

    def _build_center(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(10)

        # Monitor card: status in the header, picture, capture toolbar.
        monitor, header, body = self._card("Monitor")
        self.stream_info_label = QLabel("")
        self.stream_info_label.setObjectName("dim")
        self.status_pill = QLabel("Idle")
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setFixedHeight(20)
        self._set_pill("idle")
        header.addWidget(self.stream_info_label)
        header.addWidget(self.status_pill)

        self.video_label = QLabel("No signal — set your RTSP URL in Settings.")
        self.video_label.setObjectName("videoLabel")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(320, 180)
        # Ignore the pixmap's size hint so a big frame never stretches the
        # layout; frames are scaled to whatever room the label gets.
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        body.addWidget(self.video_label, 1)

        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        toolbar.setFixedHeight(40)
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(8, 4, 8, 4)
        tools.setSpacing(4)
        self.snapshot_btn = self._tool_button(ICON_CAMERA, "Snapshot", "Save a PNG of the current frame")
        self.snapshot_btn.clicked.connect(self._on_snapshot)
        self.record_btn = self._tool_button(ICON_RECORD, "Record", "Record an MP4 clip", checkable=True)
        self.record_btn.clicked.connect(self._on_record_toggled)
        self.timelapse_btn = self._tool_button(ICON_TIMER, "Timelapse", "Capture a print timelapse", checkable=True)
        self.timelapse_btn.clicked.connect(self._on_timelapse_toggled)
        folder_btn = self._tool_button(ICON_FOLDER, "", "Open snapshot / video folder")
        folder_btn.setPopupMode(QToolButton.InstantPopup)
        folders = QMenu(folder_btn)
        folders.addAction("Open snapshot folder", lambda: self._open_folder(self.settings.resolved_snapshot_dir()))
        folders.addAction("Open video folder", lambda: self._open_folder(self.settings.resolved_video_dir()))
        folder_btn.setMenu(folders)
        self.fullscreen_btn = self._tool_button(ICON_FULLSCREEN, "", "Fullscreen (Esc to exit)")
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        for widget in (self.snapshot_btn, self.record_btn, self.timelapse_btn, folder_btn):
            tools.addWidget(widget)
        tools.addStretch(1)
        tools.addWidget(self.fullscreen_btn)
        body.addWidget(toolbar)
        column.addWidget(monitor, 1)

        # Session strip, Studio "Progress" style: what's being captured.
        session, _header, session_body = self._card("Session")
        stats = QHBoxLayout()
        stats.setContentsMargins(12, 10, 12, 12)
        stats.setSpacing(28)
        self.session_stream = QLabel("Idle")
        self.session_rec = QLabel("—")
        self.session_timelapse = QLabel("—")
        for caption, value in (
            ("Stream", self.session_stream),
            ("Recording", self.session_rec),
            ("Timelapse", self.session_timelapse),
        ):
            value.setObjectName("sessionValue")
            cap = QLabel(caption)
            cap.setObjectName("sessionCaption")
            pair = QHBoxLayout()
            pair.setSpacing(8)
            pair.addWidget(cap)
            pair.addWidget(value)
            stats.addLayout(pair)
        stats.addStretch(1)
        session_body.addLayout(stats)
        column.addWidget(session)
        return column

    def _stat_card(self, caption: str) -> tuple[QLabel, QLabel, QWidget]:
        """Big readout, Studio temperature style."""
        box = QWidget()
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(0)
        value = QLabel("—")
        value.setObjectName("statValue")
        value.setAlignment(Qt.AlignCenter)
        cap = QLabel(caption)
        cap.setObjectName("statCaption")
        cap.setAlignment(Qt.AlignCenter)
        vbox.addWidget(value)
        vbox.addWidget(cap)
        return value, cap, box

    def _build_right_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(10)

        control, _header, body = self._card("Control")
        control.setFixedWidth(RIGHT_COLUMN_WIDTH)
        cards = QHBoxLayout()
        cards.setContentsMargins(8, 14, 8, 14)
        self.stat_stream_value, self.stat_stream_cap, stream_card = self._stat_card("State")
        self.stat_video_value, self.stat_video_cap, video_card = self._stat_card("Video")
        cards.addWidget(stream_card)
        cards.addWidget(video_card)
        body.addLayout(cards)
        column.addWidget(control)

        # Camera / Image tabs: the controls get the full height instead of
        # one long column that overflows.
        tabs_card = QFrame()
        tabs_card.setObjectName("card")
        tabs_card.setFixedWidth(RIGHT_COLUMN_WIDTH)
        tabs_layout = QVBoxLayout(tabs_card)
        tabs_layout.setContentsMargins(0, 0, 0, 0)
        self.side_tabs = QTabWidget()
        self.side_tabs.setDocumentMode(True)
        self.side_tabs.addTab(self._scrollable(self._camera_tab()), "Camera")
        tabs_layout.addWidget(self.side_tabs)
        column.addWidget(tabs_card, 1)
        return column

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    def _camera_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(6)

        # Connection (camera selection lives in the left list).
        layout.addWidget(self._section_label("CONNECTION"))
        self.conn_label = QLabel("Not connected to Wyze.")
        self.conn_label.setObjectName("dim")
        self.conn_label.setWordWrap(True)
        layout.addWidget(self.conn_label)
        row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("accentButton")
        self.connect_btn.clicked.connect(self._on_connect)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._on_refresh_cameras)
        row.addWidget(self.connect_btn)
        row.addWidget(self.refresh_btn)
        layout.addLayout(row)

        layout.addSpacing(6)
        layout.addWidget(self._section_label("POWER"))
        row = QHBoxLayout()
        self.power_on_btn = QPushButton("Turn on")
        self.power_off_btn = QPushButton("Turn off")
        self.restart_btn = QPushButton("Restart")
        self.power_on_btn.clicked.connect(lambda: self._cam_action("Power on", self.controller.set_power, True))
        self.power_off_btn.clicked.connect(lambda: self._cam_action("Power off", self.controller.set_power, False))
        self.restart_btn.clicked.connect(self._on_restart)
        for button in (self.power_on_btn, self.power_off_btn, self.restart_btn):
            row.addWidget(button)
        layout.addLayout(row)

        layout.addSpacing(6)
        layout.addWidget(self._section_label("SIREN & SPOTLIGHT"))
        row = QHBoxLayout()
        self.siren_btn = QPushButton("Siren: off")
        self.siren_btn.setObjectName("danger")
        self.siren_btn.setCheckable(True)
        self.siren_btn.clicked.connect(self._on_siren_toggled)
        self.spotlight_btn = QPushButton("Spotlight: off")
        self.spotlight_btn.setCheckable(True)
        self.spotlight_btn.clicked.connect(self._on_spotlight_toggled)
        row.addWidget(self.siren_btn)
        row.addWidget(self.spotlight_btn)
        layout.addLayout(row)

        layout.addSpacing(6)
        layout.addWidget(self._section_label("DETECTION"))
        self.motion_check = QCheckBox("Motion detection")
        self.motion_check.toggled.connect(self._on_motion_changed)
        self.notify_check = QCheckBox("Push notifications")
        self.notify_check.toggled.connect(self._on_notify_changed)
        layout.addWidget(self.motion_check)
        layout.addWidget(self.notify_check)

        layout.addSpacing(6)
        layout.addWidget(self._section_label("DEVICE INFO"))
        info = QFormLayout()
        info.setHorizontalSpacing(16)
        info.setVerticalSpacing(4)
        self.info_model = QLabel("—")
        self.info_status = QLabel("—")
        self.info_firmware = QLabel("—")
        self.info_ip = QLabel("—")
        for caption, value in (
            ("Model", self.info_model),
            ("Status", self.info_status),
            ("Firmware", self.info_firmware),
            ("IP", self.info_ip),
        ):
            cap = QLabel(caption)
            cap.setObjectName("dim")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            info.addRow(cap, value)
        layout.addLayout(info)
        layout.addStretch(1)
        return tab

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        forget_action = QAction("Forget Wyze credentials", self)
        forget_action.triggered.connect(self._forget_credentials)
        file_menu.addAction(forget_action)

        file_menu.addSeparator()
        snaps_action = QAction("Open snapshot folder", self)
        snaps_action.triggered.connect(
            lambda: self._open_folder(self.settings.resolved_snapshot_dir())
        )
        file_menu.addAction(snaps_action)
        vids_action = QAction("Open video folder", self)
        vids_action.triggered.connect(
            lambda: self._open_folder(self.settings.resolved_video_dir())
        )
        file_menu.addAction(vids_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.quit_app)
        file_menu.addAction(exit_action)

        view_menu = self.menuBar().addMenu("View")
        self.on_top_action = QAction("Always on top", self, checkable=True)
        self.on_top_action.setChecked(self.settings.always_on_top)
        self.on_top_action.triggered.connect(self._toggle_always_on_top)
        view_menu.addAction(self.on_top_action)
        self.fullscreen_action = QAction("Fullscreen", self, checkable=True)
        self.fullscreen_action.triggered.connect(self._toggle_fullscreen)
        view_menu.addAction(self.fullscreen_action)

        settings_menu = self.menuBar().addMenu("Settings")
        settings_menu.addAction(settings_action)
        self.print_watch_action = QAction("Print watch (AI)", self, checkable=True)
        self.print_watch_action.setToolTip("Watch prints for failures")
        self.print_watch_action.setChecked(bool(self.settings.ai_watch))
        self.print_watch_action.toggled.connect(self.set_print_watch_enabled)
        settings_menu.addAction(self.print_watch_action)
        watch_settings = QAction("Print watch settings…", self)
        watch_settings.triggered.connect(lambda: self._open_settings(tab="watch"))
        settings_menu.addAction(watch_settings)

        print_menu = self.menuBar().addMenu("Print")
        schedule_action = QAction("Schedule print…", self)
        schedule_action.setToolTip("Print what's sliced in eufyMake Studio at a set time")
        schedule_action.triggered.connect(self.schedule_print)
        print_menu.addAction(schedule_action)
        queue_action = QAction("Print queue", self)
        queue_action.triggered.connect(self.show_print_queue)
        print_menu.addAction(queue_action)

        help_menu = self.menuBar().addMenu("Help")
        about_action = QAction("About Wyze Print Monitor", self)
        about_action.triggered.connect(self._show_about)
        updates_action = QAction("Check for updates…", self)
        updates_action.triggered.connect(lambda: self.update_manager.check(silent=False))
        help_menu.addAction(updates_action)
        help_menu.addAction(about_action)

    def schedule_print(self) -> None:
        from .schedule_ui import open_schedule_dialog

        open_schedule_dialog(self, self.print_scheduler)

    def show_print_queue(self) -> None:
        from .schedule_ui import QueueWindow

        if self._queue_window is None:
            self._queue_window = QueueWindow(self, self.print_scheduler)
        self._queue_window.show()
        self._queue_window.raise_()
        self._queue_window.activateWindow()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Wyze Print Monitor",
            f"Wyze Print Monitor {__version__}\n\nLive RTSP view plus Wyze camera controls "
            "for 3D-print monitoring.\nSecrets live in the OS keyring, never on disk.",
        )

    # ------------------------------------------------------------------ helpers
    def _set_pill(self, state: str) -> None:
        colors = {
            "idle": ("#3d4148", "#d7d9dc"),
            "connecting": ("#8a6d2f", "#ffffff"),
            "live": ("#2f7d3a", "#ffffff"),
            "reconnecting": ("#8a6d2f", "#ffffff"),
            "error": ("#8a2f2f", "#ffffff"),
            "stopped": ("#3d4148", "#d7d9dc"),
        }
        bg, fg = colors.get(state, colors["idle"])
        self.status_pill.setStyleSheet(
            f"QLabel#statusPill {{ background-color: {bg}; color: {fg}; }}"
        )

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.power_on_btn,
            self.power_off_btn,
            self.restart_btn,
            self.siren_btn,
            self.spotlight_btn,
            self.motion_check,
            self.notify_check,
        ):
            widget.setEnabled(enabled)

    def _open_folder(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        # QDesktopServices handles Windows/macOS/Linux without shelling out.
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------ stream
    def _start_stream(self) -> None:
        self._stop_stream()
        self._reader = StreamReader(
            effective_rtsp_url(self.settings), self, quality=self.settings.stream_quality
        )
        self._reader.adjustments = self.adjustments
        self._reader.display_width = self._display_width()
        self._reader.set_sinks(self._recorder, self._timelapse)  # survive a restart
        self._reader.frame_ready.connect(self._on_frame_ready)
        self._reader.state_changed.connect(self._on_stream_state)
        self._reader.recording_failed.connect(self._on_recording_failed)
        self._reader.quality_changed.connect(self._on_quality_changed)
        self._reader.start()
        self.print_watch.set_stream(effective_rtsp_url(self.settings))

    def _stop_stream(self) -> None:
        if self._reader is not None:
            reader = self._reader
            self._reader = None
            reader.set_sinks(None, None)  # the recorder/timelapse outlive this reader
            reader.frame_ready.disconnect(self._on_frame_ready)
            reader.state_changed.disconnect(self._on_stream_state)
            reader.recording_failed.disconnect(self._on_recording_failed)
            reader.quality_changed.disconnect(self._on_quality_changed)
            if not reader.stop():
                # Still blocked inside FFmpeg. Never let a running QThread be
                # destroyed: keep a reference until it actually finishes, and
                # closeEvent joins anything left here before exiting.
                self._retired_readers.append(reader)
                reader.finished.connect(lambda r=reader: self._forget_reader(r))

    def _forget_reader(self, reader: StreamReader) -> None:
        if reader in self._retired_readers:
            self._retired_readers.remove(reader)

    def _on_stream_state(self, state: str, detail: str) -> None:
        labels = {
            "connecting": "Connecting",
            "live": "Live",
            "reconnecting": "Reconnecting",
            "stopped": "Stopped",
            "error": "Error",
        }
        self._stream_state = state
        self.status_pill.setText(labels.get(state, state))
        self._set_pill(state)
        self.session_stream.setText(labels.get(state, state))
        self._update_stat_cards()
        # Frames stop arriving on disconnect/error, so refresh the session
        # panel here too — otherwise it keeps showing a stale active state.
        self._update_session_panel()
        self._status(detail, 5000)

    def _update_stat_cards(self) -> None:
        state_labels = {
            "connecting": "…",
            "live": "LIVE",
            "reconnecting": "…",
            "stopped": "Idle",
            "error": "Error",
            "idle": "Idle",
        }
        self.stat_stream_value.setText(state_labels.get(self._stream_state, "Idle"))
        if self._reader is not None:
            w, h = self._reader.frame_size
            fps = self._reader.measured_fps
            if w and h:
                self.stat_video_value.setText(f"{fps:.0f} fps")
                self.stat_video_cap.setText(f"{w} × {h}")
                return
        self.stat_video_value.setText("—")
        self.stat_video_cap.setText("Video")

    def _update_session_panel(self) -> None:
        if self._recorder is not None and self._rec_start is not None:
            elapsed = int(time.monotonic() - self._rec_start)
            self.session_rec.setText(
                f"● {elapsed // 60:02d}:{elapsed % 60:02d}"
                f" · {self._recorder.frames_written} frames"
            )
        else:
            self.session_rec.setText("—")
        if self._timelapse is not None:
            self.session_timelapse.setText(
                f"{self._timelapse.saved} frames · every {self._timelapse.interval_sec}s"
            )
        else:
            self.session_timelapse.setText("—")

    def _on_frame_ready(self) -> None:
        """A new frame is in the reader's mailbox. Everything heavy (scaling,
        adjustments, recording, conversion) already happened on the stream
        thread; this only hands the ready-made image to the views."""
        reader = self._reader
        item = reader.take_latest() if reader is not None else None
        if item is None:
            return
        frame, image = item
        self._present(frame, image)
        reader.display_width = self._display_width()

    def _present(self, frame: np.ndarray, image: QImage) -> None:
        self._last_frame = frame
        if image is not None:
            for view in self.video_views:
                if view.isVisible():
                    view.set_image(image)
        # Status text doesn't need 20 updates a second.
        now = time.monotonic()
        if now - self._last_labels < LABEL_REFRESH_S:
            return
        self._last_labels = now
        if self._reader is not None:
            w, h = self._reader.frame_size
            if w and h:
                self.stream_info_label.setText(f"{w}×{h}  •  {self._reader.measured_fps:.0f} fps")
        self._update_stat_cards()
        self._update_session_panel()
        collector = self._timelapse
        if collector is not None and collector.write_errors and not self._timelapse_write_warned:
            self._timelapse_write_warned = True
            self._status("Timelapse: couldn't save a frame — check disk space.", 8000)

    def _display_width(self) -> int:
        """Widest visible view in device pixels: frames are scaled to this on
        the stream thread (-1 while nothing is visible: no image is made)."""
        w, h = self._reader.frame_size if self._reader is not None else (0, 0)
        aspect = w / h if w and h else 16 / 9
        fill = bool(self.settings.video_fill)
        widths = [
            display_width_for(
                view.width() * view.devicePixelRatioF(),
                view.height() * view.devicePixelRatioF(),
                aspect,
                fill,
            )
            for view in self.video_views
            if view.isVisible()
        ]
        return max(widths, default=-1)

    def _on_recording_failed(self, failed, error: str) -> None:
        # e.g. folder became unwritable mid-recording: stop cleanly instead of
        # claiming a good clip at the end.
        if failed is not self._recorder:
            # Stale: that recording was already stopped (or replaced) here.
            failed.close()
            return
        self._recorder = None
        self._rec_start = None
        # Detach under the stream's lock before closing: a sink update in
        # between may have re-attached it.
        self._update_sinks()
        failed.close()
        self.record_btn.setChecked(False)
        self.record_btn.setText("Record")
        self._update_session_panel()
        QMessageBox.warning(self, "Recording failed", error)

    def _on_quality_changed(self, label: str) -> None:
        self.active_quality = label

    def set_stream_quality(self, quality: str) -> None:
        """Change the quality setting (from the toolbar or Settings)."""
        quality = normalize_quality(quality)
        if quality == self.settings.stream_quality:
            return
        self.settings.stream_quality = quality
        save_settings(self.settings)
        if self._reader is not None:
            self._reader.set_quality(quality)

    def _update_sinks(self) -> None:
        """Point the stream thread at the current recorder / timelapse."""
        if self._reader is not None:
            self._reader.set_sinks(self._recorder, self._timelapse)

    # ------------------------------------------------------------------ capture actions
    def _on_snapshot(self) -> None:
        if self._last_frame is None:
            QMessageBox.information(self, "Snapshot", "No video frame available yet.")
            return
        path = snapshot_path(self.settings.resolved_snapshot_dir())
        # Frames arrive before the picture adjustments (those are only worked
        # out for what's on screen), so apply them to the full-size snapshot.
        if cv2.imwrite(path, apply_adjustments(self._last_frame, self.adjustments)):
            self._status(f"Snapshot saved: {path}", 5000)
            self.capture_saved.emit(path)
        else:
            QMessageBox.warning(self, "Snapshot", f"Couldn't save snapshot to:\n{path}")

    def _on_record_toggled(self, checked: bool) -> None:
        if checked:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = os.path.join(
                self.settings.resolved_video_dir(), f"clip_{stamp}.mp4"
            )
            # Record at the stream's measured rate so clips play back in real
            # time instead of a fixed (wrong) 20 fps.
            fps = self._reader.measured_fps if self._reader is not None else 0.0
            self._recorder = ClipRecorder(path, fps=fps or 20.0)
            self._rec_start = time.monotonic()
            self._update_sinks()
            self.record_btn.setText("Stop recording")
            self._update_session_panel()
            self._status("Recording…", 3000)
        else:
            if self._recorder is not None:
                recorder = self._recorder
                self._recorder = None
                self._update_sinks()  # stop writing before closing the file
                path = recorder.output_path
                count = recorder.frames_written
                recorder.close()
                self._recorder = None
                self._rec_start = None
                self._status(
                    f"Saved clip ({count} frames): {path}", 8000
                )
                self.capture_saved.emit(path)
            self.record_btn.setText("Record")
            self._update_session_panel()

    def _on_timelapse_toggled(self, checked: bool) -> None:
        if checked:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self._timelapse_frames_dir = os.path.join(
                self.settings.resolved_video_dir(), f"timelapse_{stamp}_frames"
            )
            self._timelapse = TimelapseCollector(
                self._timelapse_frames_dir,
                interval_sec=self.settings.timelapse_interval_sec,
            )
            self._timelapse.start()
            self._timelapse_write_warned = False
            self._update_sinks()
            self.timelapse_btn.setText("Stop timelapse")
            self._update_session_panel()
            self._status(
                f"Timelapse capturing every {self.settings.timelapse_interval_sec}s…",
                4000,
            )
        else:
            collector = self._timelapse
            self._timelapse = None
            self._update_sinks()
            self.timelapse_btn.setText("Timelapse")
            self._update_session_panel()
            if collector is None or collector.saved == 0:
                self._status("Timelapse stopped, no frames captured.", 5000)
                return
            output = os.path.join(
                self.settings.resolved_video_dir(),
                f"timelapse_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.mp4",
            )
            # Compile off the GUI thread: a long print leaves thousands of
            # frames to encode, which would freeze the live view.
            self.timelapse_btn.setEnabled(False)
            self._status("Compiling timelapse…", 3000)
            self._compile_worker = TimelapseCompileWorker(collector.frames_dir, output, self)
            self._compile_worker.done.connect(self._on_timelapse_compiled)
            self._compile_worker.failed.connect(self._on_timelapse_failed)
            self._compile_worker.start()

    def _on_timelapse_compiled(self, path: str, count: int) -> None:
        self.timelapse_btn.setEnabled(True)
        self._status(f"Timelapse saved ({count} frames): {path}", 8000)
        self.capture_saved.emit(path)
        self._finish_compile()

    def _on_timelapse_failed(self, error: str) -> None:
        self.timelapse_btn.setEnabled(True)
        QMessageBox.warning(self, "Timelapse", error)
        self._finish_compile()

    def _finish_compile(self) -> None:
        worker = self._compile_worker
        self._compile_worker = None
        if worker is not None:
            worker.wait()  # run() has emitted its last signal; join is instant
        if self._close_pending:
            QTimer.singleShot(0, self.quit_app)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.type() == QEvent.WindowStateChange and hasattr(self, "_side_card"):
            # Fullscreen is just the picture: no side panel, menu or status
            # bar. Following the window state (rather than the toggle) also
            # restores them when fullscreen ends any other way (closing the
            # window while docked, showNormal from the tray...).
            normal = not self.isFullScreen()
            # In fullscreen the camera column stays only while adjusting.
            self._side_card.setVisible(normal or self.camera_panel.adjusting())
            self.menuBar().setVisible(normal)
            self.statusBar().setVisible(normal)
        super().changeEvent(event)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            if self._fullscreen_from_hidden:
                # Opened from the Studio dock: go back to living in Studio.
                self._fullscreen_from_hidden = False
                self.hide()
        else:
            self._fullscreen_from_hidden = not self.isVisible()
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
        self.fullscreen_action.setChecked(self.isFullScreen())

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self._toggle_fullscreen()
            return
        super().keyPressEvent(event)

    def show_window(self) -> None:
        """Bring the full window up (from the Studio dock or the tray)."""
        if self.isMinimized() or not self.isVisible():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        """Exit the whole app (File → Exit, tray Exit). Works whether or not
        this window is shown — while docked it usually isn't."""
        self._quitting = True
        if self._defer_for_compile():
            return
        self._shutdown()
        self.hide()
        QApplication.exit(0)

    def set_adjustments(self, adjustments: ImageAdjustments) -> None:
        """Apply new picture adjustments to the live stream and remember them."""
        if adjustments == self.adjustments:
            return
        self.adjustments = adjustments
        if self._reader is not None:
            self._reader.adjustments = adjustments  # picked up on the next frame
        self.settings.image_adjustments = adjustments.to_dict()
        self.save_settings_soon()
        self.adjustments_changed.emit(adjustments)

    def _on_update_available(self, info) -> None:
        self.update_btn.setText(f"Update to {info.version}")
        self.update_btn.setToolTip(f"Wyze Print Monitor {info.version} is available (you have {__version__}).")
        self.update_btn.show()

    def set_side_panel_visible(self, visible: bool) -> None:
        """Show / hide the camera column (used while adjusting in fullscreen)."""
        self._side_card.setVisible(visible or not self.isFullScreen())

    def save_settings_soon(self) -> None:
        """Save settings once rapid changes (slider drags) settle."""
        self._save_timer.start()

    def _status(self, message: str, timeout: int = 0) -> None:
        self.statusBar().showMessage(message, timeout)
        self.status_message.emit(message, timeout)

    # ------------------------------------------------------------------ wyze
    def _auto_connect(self) -> None:
        email = self.settings.wyze_email
        if email and get_secret(SECRET_PASSWORD) and get_secret(SECRET_KEY_ID) and get_secret(SECRET_API_KEY):
            self._on_connect(silent=True)

    def _on_connect(self, silent: bool = False) -> None:
        email = self.settings.wyze_email or ""
        password = get_secret(SECRET_PASSWORD) or ""
        # Keys saved by older versions may still carry a pasted newline.
        key_id = clean_api_credential(get_secret(SECRET_KEY_ID))
        api_key = clean_api_credential(get_secret(SECRET_API_KEY))
        if not (email and password and key_id and api_key):
            if not silent:
                QMessageBox.information(
                    self,
                    "Wyze not configured",
                    "Open File → Settings… and enter your Wyze email, password, "
                    "API key ID and API key first.",
                )
            return
        if not silent:
            # A manual Connect starts over: drop any scheduled retry.
            self._login_retry_timer.stop()
            self._login_retries = 0
        self.conn_label.setText("Connecting to Wyze…")
        self.connect_btn.setEnabled(False)

        def done(ok: bool, _result, error: str) -> None:
            self.connect_btn.setEnabled(True)
            if not ok:
                if "SupersededLogin" in error:
                    return  # a newer login (e.g. from Settings) took over
                if "NeedTwoFactor" in error:
                    self._ask_2fa()
                    return
                message, transient = describe_login_error(error)
                if transient and self._login_retries < len(LOGIN_RETRY_DELAYS_S):
                    delay = LOGIN_RETRY_DELAYS_S[self._login_retries]
                    self._login_retries += 1
                    self._login_retry_timer.start(delay * 1000)
                    when = f"{delay // 60} min" if delay >= 60 else f"{delay} s"
                    self.conn_label.setText(f"{message} Retrying in {when}.")
                else:
                    self.conn_label.setText(message)
                if not silent:
                    QMessageBox.critical(self, "Wyze login failed", f"{message}\n\nDetails: {error}")
                return
            self._login_retries = 0
            self._login_retry_timer.stop()
            self._on_refresh_cameras()

        self.controller.login(email, password, key_id, api_key, done)

    def _ask_2fa(self) -> None:
        code, accepted = QInputDialog.getText(
            self,
            "Two-factor authentication",
            "Enter the verification code Wyze sent you:",
        )
        if not accepted or not code.strip():
            self.conn_label.setText("2FA cancelled.")
            return

        def done(ok: bool, _result, error: str) -> None:
            if not ok:
                self.conn_label.setText("2FA failed.")
                QMessageBox.critical(self, "2FA failed", error)
                return
            self._on_refresh_cameras()

        self.controller.submit_2fa(code.strip(), done)

    def _on_refresh_cameras(self) -> None:
        def done(ok: bool, result, error: str) -> None:
            if not ok:
                self.conn_label.setText("Could not list cameras.")
                QMessageBox.critical(self, "Camera list failed", error)
                return
            self._cameras = result or []
            # Block signals while rebuilding: selecting the first row would
            # otherwise fire currentRowChanged, which overwrites the saved
            # camera_mac before we restore the selection.
            self.camera_list.blockSignals(True)
            try:
                self.camera_list.clear()
                for cam in self._cameras:
                    item = QListWidgetItem(f"{cam.nickname}\n{cam.product_model}")
                    item.setData(Qt.UserRole, cam.mac)
                    self.camera_list.addItem(item)
                self._select_sidebar_camera_from_settings()
                if self.camera_list.currentRow() < 0 and self.camera_list.count() == 1:
                    # Nothing saved (first login) and only one camera: it's
                    # the one. Otherwise every control would stay disabled
                    # until the user clicks it — and the Studio dock hides
                    # the picker when there's a single camera.
                    self.camera_list.setCurrentRow(0)
            finally:
                self.camera_list.blockSignals(False)
            self._update_connection_ui()

        self.controller.list_cameras(done)

    def _select_sidebar_camera_from_settings(self) -> None:
        if not self._cameras or not self.settings.camera_mac:
            return
        for row in range(self.camera_list.count()):
            if self.camera_list.item(row).data(Qt.UserRole) == self.settings.camera_mac:
                self.camera_list.setCurrentRow(row)
                return

    def _on_sidebar_camera_selected(self) -> None:
        self._update_connection_ui()

    def _selected_camera(self) -> CameraInfo | None:
        row = self.camera_list.currentRow()
        if row < 0:
            return None
        mac = self.camera_list.item(row).data(Qt.UserRole)
        return next((c for c in self._cameras if c.mac == mac), None)

    def _update_connection_ui(self) -> None:
        cam = self._selected_camera()
        if cam is None:
            if self.controller.logged_in:
                self.conn_label.setText("Select a camera on the left.")
            else:
                self.conn_label.setText("Not connected to Wyze.")
            self._set_controls_enabled(False)
            return
        self.settings.camera_mac = cam.mac
        self.settings.camera_nickname = cam.nickname
        save_settings(self.settings)
        self.conn_label.setText(f"Connected: {cam.nickname}")
        self.info_model.setText(cam.product_model or "—")
        self.info_status.setText("Online" if cam.is_online else "Offline")
        self.info_firmware.setText(cam.firmware or "—")
        self.info_ip.setText(cam.ip or "—")
        # The toggles still show the previously selected camera. Hold them
        # disabled as "…" until this camera's real states are read, so a first
        # click can never send the opposite of what the user intends.
        self._set_controls_enabled(True)
        self._apply_toggle_states(None, loading=True)
        mac = cam.mac

        def done(ok: bool, state, error: str) -> None:
            selected = self._selected_camera()
            if selected is None or selected.mac != mac:
                return  # the user moved on to another camera meanwhile
            if not ok:
                self._status(f"Could not read camera state: {error}", 6000)
                state = None
            self._apply_toggle_states(state)

        self.controller.get_state(mac, done)

    def _apply_toggle_states(self, state: CameraState | None, loading: bool = False) -> None:
        """Show the camera's siren/spotlight/motion/notify states.

        A state that is unknown (not reported, or the read failed) is shown as
        "unknown" but left usable; ``loading`` disables the toggles entirely.
        Signals are blocked so displaying a state never sends a command.
        """
        def label(name: str, value: bool | None) -> str:
            if loading:
                return f"{name}: …"
            return f"{name}: {'unknown' if value is None else ('on' if value else 'off')}"

        values = {
            self.siren_btn: ("Siren", state.siren if state else None),
            self.spotlight_btn: ("Spotlight", state.spotlight if state else None),
            self.motion_check: ("Motion detection", state.motion if state else None),
            self.notify_check: ("Push notifications", state.notifications if state else None),
        }
        for widget, (name, value) in values.items():
            widget.blockSignals(True)
            try:
                widget.setChecked(bool(value))
                if isinstance(widget, QPushButton):
                    widget.setText(label(name, value))
                else:
                    widget.setText(name if value is not None and not loading else label(name, value))
                widget.setEnabled(not loading)
            finally:
                widget.blockSignals(False)

    def _cam_action(self, label: str, fn, *args) -> None:
        mac = self.settings.camera_mac
        if not self.controller.logged_in or not mac:
            QMessageBox.information(
                self, label, "Connect to Wyze and select a camera first."
            )
            return
        self._status(f"{label}…")

        def done(ok: bool, _result, error: str) -> None:
            if ok:
                self._status(f"{label}: done", 4000)
            else:
                self._status(f"{label}: failed", 4000)
                QMessageBox.warning(self, label, error)

        fn(mac, *args, done)

    def _on_restart(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restart camera",
            "Restart the camera? The stream will drop for about a minute.",
        )
        if answer == QMessageBox.Yes:
            self._cam_action("Restart", self.controller.restart)

    def _on_siren_toggled(self, checked: bool) -> None:
        self.siren_btn.setText(f"Siren: {'on' if checked else 'off'}")

        def done(ok: bool, _result, error: str) -> None:
            if not ok:
                self.siren_btn.setChecked(not checked)
                self.siren_btn.setText(f"Siren: {'off' if checked else 'on'}")
                QMessageBox.warning(self, "Siren", error)

        mac = self.settings.camera_mac
        if not self.controller.logged_in or not mac:
            self.siren_btn.setChecked(False)
            self.siren_btn.setText("Siren: off")
            QMessageBox.information(self, "Siren", "Connect to Wyze and select a camera first.")
            return
        self.controller.set_siren(mac, checked, done)

    def _on_spotlight_toggled(self, checked: bool) -> None:
        self.spotlight_btn.setText(f"Spotlight: {'on' if checked else 'off'}")

        def done(ok: bool, _result, error: str) -> None:
            if not ok:
                self.spotlight_btn.setChecked(not checked)
                self.spotlight_btn.setText(f"Spotlight: {'off' if checked else 'on'}")
                QMessageBox.warning(self, "Spotlight", error)

        mac = self.settings.camera_mac
        if not self.controller.logged_in or not mac:
            self.spotlight_btn.setChecked(False)
            self.spotlight_btn.setText("Spotlight: off")
            QMessageBox.information(self, "Spotlight", "Connect to Wyze and select a camera first.")
            return
        self.controller.set_spotlight(mac, checked, done)

    def _on_motion_changed(self, on: bool) -> None:
        self.motion_check.setText("Motion detection")
        self._cam_action(
            "Motion detection " + ("on" if on else "off"),
            self.controller.set_motion_detection,
            on,
        )

    def _on_notify_changed(self, on: bool) -> None:
        self.notify_check.setText("Push notifications")
        self._cam_action(
            "Notifications " + ("on" if on else "off"),
            self.controller.set_notifications,
            on,
        )

    # ------------------------------------------------------------------ misc
    def set_print_watch_enabled(self, on: bool) -> None:
        """Turn Print watch on/off (menu, camera panel). Without the model,
        turning it on opens Settings at the download instead."""
        if on and not failure_model.is_installed():
            self.print_watch_action.blockSignals(True)
            self.print_watch_action.setChecked(False)
            self.print_watch_action.blockSignals(False)
            self._open_settings(tab="watch")
            return
        if bool(self.settings.ai_watch) != on:
            self.settings.ai_watch = on
            save_settings(self.settings)
            self.print_watch.apply_settings()
            self._status("Print watch " + ("on" if on else "off") + ".", 4000)
        self._sync_print_watch_action()

    def _sync_print_watch_action(self) -> None:
        on = bool(self.settings.ai_watch)
        if self.print_watch_action.isChecked() != on:
            self.print_watch_action.blockSignals(True)
            self.print_watch_action.setChecked(on)
            self.print_watch_action.blockSignals(False)

    def _open_settings(self, tab: str = "") -> None:
        # Don't let a background re-login with the old saved credentials run
        # while the user is testing new ones in Settings.
        retry_was_pending = self._login_retry_timer.isActive()
        self._login_retry_timer.stop()
        dialog = SettingsDialog(self.settings, self.controller, self)
        self.update_manager.check(silent=True)  # quietly: the Update button appears if there is one
        if tab == "watch":
            dialog.show_watch_tab()
        accepted = dialog.exec() == QDialog.Accepted
        if self.controller.logged_in:
            self._login_retries = 0
        elif retry_was_pending:
            self._login_retry_timer.start()
        if accepted:
            save_settings(self.settings)
            self._apply_always_on_top()
            self.on_top_action.setChecked(self.settings.always_on_top)
            if effective_rtsp_url(self.settings):
                self._start_stream()
            else:
                # URL was cleared: stop the old camera instead of letting it
                # keep streaming (and recording) with the stale URL.
                self._stop_stream()
                self.print_watch.set_stream(None)
            self.print_watch.apply_settings()
            self._sync_print_watch_action()
            # The dialog may have completed a fresh login, in which case the
            # main window's camera list is still empty — repopulate it so the
            # controls don't stay disabled after setup.
            if self.controller.logged_in:
                self._on_refresh_cameras()
            else:
                self._select_sidebar_camera_from_settings()

    def _forget_credentials(self) -> None:
        answer = QMessageBox.question(
            self,
            "Forget credentials",
            "Remove your Wyze password, API key ID and API key from the OS keyring?",
        )
        if answer == QMessageBox.Yes:
            forget_secrets()
            self._status("Wyze credentials removed from keyring.", 5000)

    def _apply_always_on_top(self) -> None:
        # Capture visibility BEFORE changing flags: setWindowFlag can hide the
        # window on some platforms, which would make a later isVisible() check
        # lie and leave the app hidden.
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.settings.always_on_top)
        # Toggling the flag needs the window to be re-shown on some platforms.
        if was_visible:
            self.show()

    def _toggle_always_on_top(self, checked: bool) -> None:
        self.settings.always_on_top = checked
        save_settings(self.settings)
        self._apply_always_on_top()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.hide_on_close and not self._quitting:
            # Docked in Studio: the stream, recordings and controls live on
            # there, so closing the window just tucks it away.
            if self.isFullScreen():
                self.showNormal()
            self._fullscreen_from_hidden = False
            self.hide()
            event.ignore()
            return
        if self._defer_for_compile():
            event.ignore()
            return
        self._shutdown()
        super().closeEvent(event)
        # quitOnLastWindowClosed is off while docking (the docked panels and
        # tray keep the app alive), so end the event loop explicitly. exit()
        # rather than quit(): quit() first asks every top-level window to
        # close, and the windows embedded in Studio veto that.
        QApplication.exit(0)

    def _defer_for_compile(self) -> bool:
        if self._compile_worker is not None and self._compile_worker.isRunning():
            # The window owns the QThread: never tear down a running encoder.
            # Defer the exit; _finish_compile re-issues it once the MP4 is
            # finished, however long that takes.
            self._close_pending = True
            self._status("Finishing timelapse compile — the app will close when it's done…")
            return True
        return False

    def _shutdown(self) -> None:
        """Release the camera, threads and settings. Safe to call twice."""
        if self._shut_down:
            return
        self._shut_down = True
        if self._reader is not None:
            self._reader.set_sinks(None, None)
        if self._recorder is not None:
            self._recorder.close()
        self._stop_stream()
        # Join every stream thread before the window (their parent) is
        # destroyed. Each blocking FFmpeg call is bounded by its own timeout,
        # so this cannot hang forever.
        for reader in list(self._retired_readers):
            reader.request_stop()
            reader.wait()
        self._retired_readers.clear()
        self.update_manager.shutdown()
        self.print_watch.shutdown()
        self.print_scheduler.alarm.stop()
        if self._queue_window is not None:
            self._queue_window.close()
        self.alert_popup.close()
        self.runner.stop()
        save_settings(self.settings)
