"""Settings dialog: RTSP URL, Wyze credentials, camera picker, folders,
and the print watch (AI failure detection)."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, autostart, failure_model
from ..config import (
    SECRET_API_KEY,
    SECRET_KEY_ID,
    SECRET_PASSWORD,
    SECRET_RTSP_URL,
    Settings,
    clean_api_credential,
    delete_secret,
    effective_rtsp_url,
    get_secret,
    save_secret,
)
from ..quality import LABELS as QUALITY_LABELS
from ..quality import QUALITIES
from ..wyze_control import CameraInfo, WyzeController


def _select(combo: QComboBox, value) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


class SettingsDialog(QDialog):
    """Edits Settings; tests the Wyze login and fills the camera picker."""

    def __init__(
        self,
        settings: Settings,
        controller: WyzeController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(500, 560)
        self._watch = getattr(parent, "print_watch", None)
        self._settings = settings
        self._controller = controller
        self._cameras: list[CameraInfo] = []

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Show the real URL (from the keyring when present); the settings file
        # only ever holds a redacted copy.
        self.rtsp_edit = QLineEdit(effective_rtsp_url(settings))
        self.rtsp_edit.setPlaceholderText("rtsps://user:pass@192.168.x.x:322/stream0")
        # The URL carries the camera password: hidden unless asked for.
        self.rtsp_edit.setEchoMode(QLineEdit.Password)
        show_rtsp = QCheckBox("Show")
        show_rtsp.toggled.connect(
            lambda shown: self.rtsp_edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        )
        rtsp_row = QHBoxLayout()
        rtsp_row.addWidget(self.rtsp_edit, 1)
        rtsp_row.addWidget(show_rtsp)
        form.addRow("RTSP URL", rtsp_row)

        self.quality_combo = QComboBox()
        for key in QUALITIES:
            self.quality_combo.addItem(QUALITY_LABELS[key], key)
        self.quality_combo.setCurrentIndex(max(self.quality_combo.findData(settings.stream_quality), 0))
        self.quality_combo.setToolTip(
            "Auto shows full HD and switches to the camera's 360p stream when frames "
            "drop below 10 fps. 720p keeps everything smooth on slower PCs."
        )
        form.addRow("Video quality", self.quality_combo)

        self.email_edit = QLineEdit(settings.wyze_email)
        self.email_edit.setPlaceholderText("you@example.com")
        form.addRow("Wyze email", self.email_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Stored in OS keyring")
        if get_secret(SECRET_PASSWORD):
            self.password_edit.setPlaceholderText("Saved in keyring (leave blank to keep)")
        form.addRow("Wyze password", self.password_edit)

        self.key_id_edit = QLineEdit()
        self.key_id_edit.setPlaceholderText("Wyze API key ID")
        if get_secret(SECRET_KEY_ID):
            self.key_id_edit.setPlaceholderText("Saved in keyring (leave blank to keep)")
        form.addRow("API key ID", self.key_id_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Wyze API key")
        if get_secret(SECRET_API_KEY):
            self.api_key_edit.setPlaceholderText("Saved in keyring (leave blank to keep)")
        form.addRow("API key", self.api_key_edit)

        help_label = QLabel(
            "Create the API key ID + key at Wyze's developer portal, then paste them here. "
            "Credentials are kept in your OS keyring, never in the settings file."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #9a9ea5;")
        form.addRow(help_label)

        self.test_button = QPushButton("Connect && list cameras")
        self.test_button.clicked.connect(self._on_test)
        form.addRow(self.test_button)

        self.camera_combo = QComboBox()
        self._refresh_camera_combo()
        form.addRow("Camera", self.camera_combo)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(2, 300)
        self.interval_spin.setValue(settings.timelapse_interval_sec)
        self.interval_spin.setSuffix(" s")
        form.addRow("Timelapse interval", self.interval_spin)

        self.snapshot_edit = QLineEdit(settings.resolved_snapshot_dir())
        snap_btn = QPushButton("Browse…")
        snap_btn.clicked.connect(lambda: self._browse(self.snapshot_edit))
        snap_row = QHBoxLayout()
        snap_row.addWidget(self.snapshot_edit)
        snap_row.addWidget(snap_btn)
        form.addRow("Snapshot folder", snap_row)

        self.video_edit = QLineEdit(settings.resolved_video_dir())
        vid_btn = QPushButton("Browse…")
        vid_btn.clicked.connect(lambda: self._browse(self.video_edit))
        vid_row = QHBoxLayout()
        vid_row.addWidget(self.video_edit)
        vid_row.addWidget(vid_btn)
        form.addRow("Video folder", vid_row)

        self.on_top_check = QCheckBox("Keep window always on top")
        self.on_top_check.setChecked(settings.always_on_top)
        form.addRow(self.on_top_check)

        self.autostart_check = QCheckBox("Launch at startup")
        if autostart.supported():
            try:
                self.autostart_check.setChecked(autostart.is_enabled())
            except (NotImplementedError, OSError):
                self.autostart_check.setEnabled(False)
        else:
            self.autostart_check.setEnabled(False)
            self.autostart_check.setToolTip("Not supported on this platform")
        form.addRow(self.autostart_check)

        self.dock_check = QCheckBox("Show inside eufyMake Studio's Device tab")
        self.dock_check.setChecked(settings.dock_in_studio)
        self.dock_row_spin = QSpinBox()
        self.dock_row_spin.setRange(0, 20)
        self.dock_row_spin.setSpecialValueText("Any printer")
        self.dock_row_spin.setPrefix("Row ")
        self.dock_row_spin.setValue(settings.dock_printer_row)
        self.dock_row_spin.setToolTip(
            "Which row of Studio's \"My Printer\" list has this camera. The live "
            "view only replaces Studio's Monitor while that printer is selected."
        )
        if sys.platform != "win32":
            self.dock_check.setEnabled(False)
            self.dock_row_spin.setEnabled(False)
            self.dock_check.setToolTip("Windows only")
        form.addRow(self.dock_check)
        self.updates_check = QCheckBox("Check for updates automatically")
        self.updates_check.setChecked(settings.check_updates)
        form.addRow(self.updates_check)
        updates_row = QHBoxLayout()
        updates_row.addWidget(QLabel(f"Version {__version__}"))
        self.check_now_btn = QPushButton("Check for updates now")
        self.check_now_btn.setEnabled(getattr(parent, "update_manager", None) is not None)
        self.check_now_btn.clicked.connect(lambda: parent.update_manager.check(silent=False))
        updates_row.addWidget(self.check_now_btn)
        updates_row.addStretch(1)
        form.addRow(updates_row)
        form.addRow("Printer with camera", self.dock_row_spin)

        tabs = self._tabs = QTabWidget()
        general = QWidget()
        general.setLayout(form)
        tabs.addTab(general, "General")
        tabs.addTab(self._build_watch_tab(settings), "Print watch")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_watch_tab(self) -> None:
        self._tabs.setCurrentIndex(1)

    # ------------------------------------------------------------------ print watch
    def _build_watch_tab(self, settings: Settings) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        intro = QLabel(
            "Watches the camera for failed prints (spaghetti, blobs, parts knocked "
            "loose) with an AI model that runs on this PC — no account, no cloud. "
            "It only runs while Studio shows a print running."
        )
        intro.setWordWrap(True)
        column.addWidget(intro)

        self.model_label = QLabel()
        self.model_label.setWordWrap(True)
        column.addWidget(self.model_label)
        model_row = QHBoxLayout()
        self.download_btn = QPushButton(f"Download model ({failure_model.MODEL_SIZE // 1_000_000} MB)")
        self.download_btn.clicked.connect(self._on_download)
        self.cancel_download_btn = QPushButton("Cancel")
        self.cancel_download_btn.clicked.connect(lambda: self._watch and self._watch.cancel_download())
        self.remove_model_btn = QPushButton("Remove model")
        self.remove_model_btn.clicked.connect(self._on_remove_model)
        for button in (self.download_btn, self.cancel_download_btn, self.remove_model_btn):
            model_row.addWidget(button)
        model_row.addStretch(1)
        column.addLayout(model_row)
        self.download_bar = QProgressBar()
        self.download_bar.setTextVisible(True)
        column.addWidget(self.download_bar)
        credit = QLabel(
            'Model by <a href="https://www.obico.io">Obico</a> (formerly The Spaghetti '
            f'Detective), <a href="{failure_model.MODEL_LICENSE_URL}">AGPL-3.0</a>. '
            "It's downloaded straight from Obico and checked against its published checksum."
        )
        credit.setWordWrap(True)
        credit.setOpenExternalLinks(True)
        credit.setStyleSheet("color: gray; font-size: 11px;")
        column.addWidget(credit)

        watch_form = QFormLayout()
        self.watch_check = QCheckBox("Watch prints for failures")
        self.watch_check.setChecked(settings.ai_watch)
        watch_form.addRow(self.watch_check)
        self.auto_pause_check = QCheckBox("Pause the print in Studio when the AI is very sure")
        self.auto_pause_check.setChecked(settings.ai_auto_pause)
        self.auto_pause_check.setToolTip(
            "Only after the failure signal holds for over 30 seconds. Less certain "
            "problems only alert you. It never stops a print on its own."
        )
        watch_form.addRow(self.auto_pause_check)
        self.sensitivity_combo = QComboBox()
        for key, text in (("low", "Low — fewer alerts"), ("normal", "Normal"), ("high", "High — earlier alerts")):
            self.sensitivity_combo.addItem(text, key)
        index = self.sensitivity_combo.findData(settings.ai_sensitivity)
        self.sensitivity_combo.setCurrentIndex(index if index >= 0 else 1)
        watch_form.addRow("Sensitivity", self.sensitivity_combo)
        self.alert_level_combo = QComboBox()
        self.alert_level_combo.addItem("Possible problems and likely failures", "all")
        self.alert_level_combo.addItem("Only likely failures", "failures")
        _select(self.alert_level_combo, settings.ai_alert_level)
        watch_form.addRow("Alert me about", self.alert_level_combo)
        self.pause_after_combo = QComboBox()
        for seconds, text in ((30, "30 seconds"), (60, "1 minute"), (120, "2 minutes")):
            self.pause_after_combo.addItem(text, seconds)
        _select(self.pause_after_combo, settings.ai_pause_after_s)
        self.pause_after_combo.setToolTip("How long a failure has to keep showing before it pauses.")
        watch_form.addRow("Pause once sure for", self.pause_after_combo)
        self.interval_combo = QComboBox()
        for seconds, text in ((10, "10 seconds (fastest)"), (20, "20 seconds"), (30, "30 seconds (lightest)")):
            self.interval_combo.addItem(text, seconds)
        _select(self.interval_combo, settings.ai_check_interval_s)
        self.interval_combo.setToolTip("Less often uses less CPU, but notices problems later.")
        watch_form.addRow("Check every", self.interval_combo)
        self._area_pending = False  # a new watch area, applied on Save
        self._area = self._watch.watch_area() if self._watch is not None else None
        self.area_label = QLabel()
        self.area_btn = QPushButton("Mark the print bed…")
        self.area_btn.setToolTip("Ignore anything the camera sees outside the bed (cables, shelves, the floor).")
        self.area_btn.clicked.connect(self._on_watch_area)
        area_row = QHBoxLayout()
        area_row.addWidget(self.area_label, 1)
        area_row.addWidget(self.area_btn)
        watch_form.addRow("Watch area", area_row)
        self._refresh_area_label()
        self.without_studio_check = QCheckBox("Also watch when Studio isn't showing a print")
        self.without_studio_check.setChecked(settings.ai_watch_without_studio)
        self.without_studio_check.setToolTip(
            "For prints started without eufyMake Studio. Watching then runs whenever "
            "the video is on, even with nothing printing, so it may alert for nothing."
        )
        watch_form.addRow(self.without_studio_check)

        self.sound_combo = QComboBox()
        self.sound_combo.addItem("Alarm until I respond (failures), chime otherwise", "alarm")
        self.sound_combo.addItem("Chime once", "beep")
        self.sound_combo.addItem("Silent", "off")
        _select(self.sound_combo, settings.ai_sound)
        watch_form.addRow("Alert sound", self.sound_combo)
        self.notify_check = QCheckBox("Show Windows notifications")
        self.notify_check.setChecked(settings.ai_notifications)
        watch_form.addRow(self.notify_check)
        self.popup_check = QCheckBox("Pop up the alert on top of other windows (until I respond)")
        self.popup_check.setChecked(settings.ai_popup)
        self.popup_check.setToolTip(
            "A small alert card in the corner of the screen, above Studio and other apps, "
            "with the snapshot and Pause / Stop buttons. It doesn't take your keyboard focus."
        )
        watch_form.addRow(self.popup_check)
        self.test_alert_btn = QPushButton("Test alert")
        self.test_alert_btn.setToolTip("Shows the alert and plays the sound with the settings above.")
        self.test_alert_btn.clicked.connect(self._on_test_alert)
        test_row = QHBoxLayout()  # a normal-size button, not a full-width bar
        test_row.addWidget(self.test_alert_btn)
        test_row.addStretch(1)
        watch_form.addRow(test_row)

        self.sound_check = QCheckBox("Also alert me about unusual printer sounds")
        self.sound_check.setChecked(settings.sound_alerts)
        self.sound_check.setToolTip(
            "Learns how this print normally sounds and alerts on grinding, clicking "
            "or a loud bang. Sound alone never pauses."
        )
        watch_form.addRow(self.sound_check)
        self.sound_note = QLabel()
        self.sound_note.setWordWrap(True)
        self.sound_note.setStyleSheet("color: #e0a040;")
        watch_form.addRow(self.sound_note)
        self._refresh_sound_note()
        column.addLayout(watch_form)
        column.addStretch(1)

        self.watch_check.toggled.connect(self._refresh_watch)
        self.auto_pause_check.toggled.connect(self._refresh_watch)
        if self._watch is not None:
            self._watch.download_progress.connect(self._on_download_progress)
            self._watch.download_finished.connect(self._on_download_finished)
            self._watch.status_changed.connect(self._refresh_sound_note)
        self._refresh_watch()
        return page

    def _refresh_area_label(self) -> None:
        self.area_label.setText("The print bed only" if self._area else "The whole picture")

    def _on_watch_area(self) -> None:
        from .watch_area import WatchAreaDialog

        frame = self._watch.latest_frame() if self._watch is not None else None
        if frame is None or not self._watch.has_camera():
            QMessageBox.information(self, "Watch area", "Start the video first: the bed is marked on the camera picture.")
            return
        dialog = WatchAreaDialog(frame, self._area, self)
        if dialog.exec() == QDialog.Accepted:
            self._area = dialog.area
            self._area_pending = True
            self._refresh_area_label()

    def _refresh_sound_note(self) -> None:
        """Kept live: the sound connection can drop or recover while open."""
        sound = self._watch.sound_status() if self._watch is not None else ""
        trouble = sound.startswith("unavailable")
        if trouble:
            self.sound_note.setText("Sound alerts aren't working right now: " + sound.split(": ", 1)[-1] + ".")
        self.sound_note.setVisible(trouble)

    def _refresh_watch(self) -> None:
        installed = failure_model.is_installed()
        downloading = self._watch is not None and self._watch.downloading()
        if downloading:
            self.model_label.setText("Downloading the model…")
        elif installed:
            self.model_label.setText("✓ Model installed.")
        else:
            self.model_label.setText("The model isn't downloaded yet — it's needed before watching can start.")
        self.download_btn.setVisible(not installed and not downloading)
        self.download_btn.setEnabled(self._watch is not None)
        self.cancel_download_btn.setVisible(downloading)
        self.remove_model_btn.setVisible(installed and not downloading)
        self.download_bar.setVisible(downloading)
        on = self.watch_check.isChecked()
        for widget in (
            self.auto_pause_check,
            self.sensitivity_combo,
            self.sound_check,
            self.alert_level_combo,
            self.pause_after_combo,
            self.interval_combo,
            self.area_btn,
            self.without_studio_check,
        ):
            widget.setEnabled(on)
        self.pause_after_combo.setEnabled(on and self.auto_pause_check.isChecked())

    def _on_test_alert(self) -> None:
        """Try the sound/notification choices as currently picked (unsaved).
        Clicking again stops it (Settings is modal: the app's own Dismiss
        and Silence can't be reached while it's open)."""
        if self._watch is None:
            return
        if self._stop_test_alert():
            return
        s = self._settings
        saved = (s.ai_sound, s.ai_notifications, s.ai_popup)
        s.ai_sound = self.sound_combo.currentData()
        s.ai_notifications = self.notify_check.isChecked()
        s.ai_popup = self.popup_check.isChecked()
        try:
            started = self._watch.test_alert()
        finally:
            s.ai_sound, s.ai_notifications, s.ai_popup = saved
        if started:
            self.test_alert_btn.setText("Stop test alert")
        else:
            QMessageBox.information(
                self,
                "Test alert",
                "A real Print watch alert is showing right now — deal with that first.",
            )

    def _on_download(self) -> None:
        if self._watch is None:
            return
        self.download_bar.setRange(0, 100)
        self.download_bar.setValue(0)
        self._watch.download_model()
        self._refresh_watch()

    def _on_download_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.download_bar.setValue(int(done * 100 / total))
            self.download_bar.setFormat(f"{done / 1e6:.0f} / {total / 1e6:.0f} MB")

    def _on_download_finished(self, ok: bool, message: str) -> None:
        self._refresh_watch()
        self.model_label.setText(("✓ " if ok else "") + message)
        if ok and not self.watch_check.isChecked():
            self.watch_check.setChecked(True)  # downloaded it to use it

    def _on_remove_model(self) -> None:
        if self._watch is None:
            return
        answer = QMessageBox.question(self, "Remove model", "Delete the downloaded AI model (~200 MB)?")
        if answer == QMessageBox.Yes:
            self._watch.remove_model()
            self._refresh_watch()

    def _stop_test_alert(self) -> bool:
        """End a running test alert; True if there was one."""
        self.test_alert_btn.setText("Test alert")
        watch = self._watch
        if watch is not None and watch.alert is not None and watch.alert.severity == "test":
            watch.dismiss()
            return True
        return False

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        self._stop_test_alert()  # a test never outlives Settings
        if self._watch is not None:
            for signal, slot in (
                (self._watch.download_progress, self._on_download_progress),
                (self._watch.download_finished, self._on_download_finished),
                (self._watch.status_changed, self._refresh_sound_note),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        super().done(result)

    # ------------------------------------------------------------------ slots
    def _browse(self, edit: QLineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if directory:
            edit.setText(directory)

    def _refresh_camera_combo(self) -> None:
        self.camera_combo.clear()
        for cam in self._cameras:
            label = f"{cam.nickname} ({cam.product_model})"
            self.camera_combo.addItem(label, cam.mac)
        if self._settings.camera_mac:
            index = self.camera_combo.findData(self._settings.camera_mac)
            if index >= 0:
                self.camera_combo.setCurrentIndex(index)

    def _on_test(self) -> None:
        email = self.email_edit.text().strip()
        password = self.password_edit.text() or get_secret(SECRET_PASSWORD) or ""
        # Clean before falling back: a field holding only stray whitespace
        # counts as empty, so the saved key is used instead.
        key_id = clean_api_credential(self.key_id_edit.text()) or clean_api_credential(
            get_secret(SECRET_KEY_ID)
        )
        api_key = clean_api_credential(self.api_key_edit.text()) or clean_api_credential(
            get_secret(SECRET_API_KEY)
        )
        if not (email and password and key_id and api_key):
            QMessageBox.warning(
                self,
                "Missing credentials",
                "Fill in email, password, API key ID and API key first.",
            )
            return
        self.test_button.setEnabled(False)
        self.test_button.setText("Connecting…")

        def done(ok: bool, _result, error: str) -> None:
            self.test_button.setEnabled(True)
            self.test_button.setText("Connect && list cameras")
            if not ok:
                if "SupersededLogin" in error:
                    return  # a newer test login is already running
                if "NeedTwoFactor" in error:
                    QMessageBox.information(
                        self,
                        "2FA required",
                        "Wyze asked for a verification code. Save these settings, "
                        "then use Connect in the main window and enter the code when asked.",
                    )
                else:
                    QMessageBox.critical(self, "Login failed", error)
                return
            self._controller.list_cameras(self._on_cameras)

        self._controller.login(email, password, key_id, api_key, done)

    def _on_cameras(self, ok: bool, result, error: str) -> None:
        if not ok:
            QMessageBox.critical(self, "Could not list cameras", error)
            return
        self._cameras = result or []
        self._refresh_camera_combo()
        QMessageBox.information(
            self, "Connected", f"Found {len(self._cameras)} camera(s) on your account."
        )

    def _on_save(self) -> None:
        s = self._settings
        rtsp_url = self.rtsp_edit.text().strip()
        s.rtsp_url = rtsp_url  # save_settings() redacts credentials on write
        secrets_ok = True
        if rtsp_url:
            # Evaluate the call first: every secret must be attempted even if
            # an earlier one failed.
            secrets_ok = save_secret(SECRET_RTSP_URL, rtsp_url) and secrets_ok
        else:
            delete_secret(SECRET_RTSP_URL)
        s.wyze_email = self.email_edit.text().strip()
        s.timelapse_interval_sec = self.interval_spin.value()
        s.snapshot_dir = self.snapshot_edit.text().strip()
        s.video_dir = self.video_edit.text().strip()
        s.always_on_top = self.on_top_check.isChecked()
        s.dock_in_studio = self.dock_check.isChecked()
        s.stream_quality = self.quality_combo.currentData()
        s.check_updates = self.updates_check.isChecked()
        s.dock_printer_row = self.dock_row_spin.value()
        s.ai_watch = self.watch_check.isChecked()
        s.ai_auto_pause = self.auto_pause_check.isChecked()
        s.ai_sensitivity = self.sensitivity_combo.currentData()
        s.sound_alerts = self.sound_check.isChecked()
        s.ai_alert_level = self.alert_level_combo.currentData()
        s.ai_pause_after_s = self.pause_after_combo.currentData()
        s.ai_check_interval_s = self.interval_combo.currentData()
        s.ai_sound = self.sound_combo.currentData()
        s.ai_notifications = self.notify_check.isChecked()
        s.ai_popup = self.popup_check.isChecked()
        s.ai_watch_without_studio = self.without_studio_check.isChecked()
        if self._area_pending and self._watch is not None:
            self._watch.set_watch_area(self._area)
        mac =self.camera_combo.currentData()
        if mac:
            s.camera_mac = mac
            s.camera_nickname = self.camera_combo.currentText()
        # Only overwrite secrets the user actually typed.
        if self.password_edit.text():
            secrets_ok = save_secret(SECRET_PASSWORD, self.password_edit.text()) and secrets_ok
        key_id = clean_api_credential(self.key_id_edit.text())
        if key_id:
            secrets_ok = save_secret(SECRET_KEY_ID, key_id) and secrets_ok
        api_key = clean_api_credential(self.api_key_edit.text())
        if api_key:
            secrets_ok = save_secret(SECRET_API_KEY, api_key) and secrets_ok
        if not secrets_ok:
            QMessageBox.warning(
                self,
                "Secrets not saved",
                "No OS keyring is available, so passwords and API keys are "
                "kept only in memory: they work for this session but will "
                "not survive a restart.",
            )
        if autostart.supported() and self.autostart_check.isEnabled():
            try:
                autostart.set_enabled(self.autostart_check.isChecked())
            except (NotImplementedError, OSError) as exc:
                QMessageBox.warning(
                    self,
                    "Launch at startup",
                    f"Could not change the startup setting:\n{exc}",
                )
        self.accept()


def forget_secrets() -> None:
    for name in (SECRET_PASSWORD, SECRET_KEY_ID, SECRET_API_KEY):
        delete_secret(name)
