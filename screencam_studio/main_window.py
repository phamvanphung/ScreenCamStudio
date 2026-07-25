from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .audio_loopback import AudioSourceSpec, measure_input_device_peak
from .capture import CaptureConfig, CaptureWorker
from .devices import (
    LoopbackDeviceInfo,
    MicrophoneDeviceInfo,
    MonitorInfo,
    find_ffmpeg,
    list_cameras,
    list_monitors,
    list_wasapi_loopback_devices,
    list_wasapi_microphones,
)
from .encoder import EncoderConfig, FFmpegEncoder
from .quality import QUALITY_PROFILES, get_quality_profile


class AudioScanWorker(QThread):
    scan_completed = Signal(object, object, str)

    def run(self) -> None:
        loopback_devices, loopback_error = list_wasapi_loopback_devices()
        microphone_devices, microphone_error = list_wasapi_microphones()
        errors = " | ".join(
            message for message in (loopback_error, microphone_error) if message
        )
        self.scan_completed.emit(loopback_devices, microphone_devices, errors)




class MicrophoneTestWorker(QThread):
    test_completed = Signal(object, int)
    test_failed = Signal(str)

    def __init__(self, device: MicrophoneDeviceInfo) -> None:
        super().__init__()
        self.device = device

    def run(self) -> None:
        try:
            peak_dbfs, samples = measure_input_device_peak(
                AudioSourceSpec(
                    device_index=self.device.index,
                    name=self.device.name,
                    sample_rate=self.device.sample_rate,
                    channels=self.device.channels,
                    volume_percent=100,
                    kind="microphone",
                    enhance_microphone=False,
                ),
                duration_seconds=3.0,
            )
            self.test_completed.emit(peak_dbfs, samples)
        except Exception as exc:
            self.test_failed.emit(str(exc))


class SessionStopWorker(QThread):
    stop_completed = Signal(str)
    stop_failed = Signal(str)

    def __init__(
        self,
        encoder: FFmpegEncoder | None,
        capture_worker: CaptureWorker | None,
    ) -> None:
        super().__init__()
        self.encoder = encoder
        self.capture_worker = capture_worker

    def run(self) -> None:
        capture = self.capture_worker
        saved_file = ""
        error_message = ""

        try:
            if capture is not None:
                capture.set_encoder(None)
                capture.requestInterruption()

            if self.encoder is not None:
                saved_file = self.encoder.stop()
        except Exception as exc:
            error_message = str(exc)

        # Always wait for capture shutdown, even when encoder finalization failed.
        # Session 2 must never overlap the previous webcam/screen resources.
        if capture is not None and capture.isRunning():
            if not capture.wait(10000):
                shutdown_error = (
                    "Luồng quay màn hình cũ chưa dừng hoàn toàn. "
                    "Hãy đóng ứng dụng rồi mở lại trước khi quay tiếp."
                )
                error_message = (
                    f"{error_message} | {shutdown_error}"
                    if error_message
                    else shutdown_error
                )

        if error_message:
            self.stop_failed.emit(error_message)
        else:
            self.stop_completed.emit(saved_file)


APP_STYLE = """
QMainWindow, QWidget {
    background: #101319;
    color: #edf1f7;
    font-family: "Segoe UI";
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #2a303a;
    border-radius: 9px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    background: #161b22;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QLineEdit, QComboBox, QSpinBox, QTextEdit {
    background: #0d1117;
    border: 1px solid #303743;
    border-radius: 6px;
    padding: 7px;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
    border: 1px solid #4f8cff;
}
QPushButton {
    background: #273142;
    border: 1px solid #39465c;
    border-radius: 7px;
    padding: 8px 12px;
    font-weight: 600;
}
QPushButton:hover {
    background: #334158;
}
QPushButton:pressed {
    background: #1f2937;
}
QPushButton:disabled {
    color: #798292;
    background: #202631;
}
QPushButton#recordButton {
    background: #b4232d;
    border-color: #df3945;
}
QPushButton#streamButton {
    background: #1d6f42;
    border-color: #2aa763;
}
QPushButton#stopButton {
    background: #8a5b12;
    border-color: #c28725;
}
QLabel#preview {
    background: #05070a;
    border: 1px solid #2a303a;
    border-radius: 10px;
    color: #7f8998;
}
QLabel#status {
    color: #aeb8c8;
    padding: 5px;
}
QScrollArea {
    border: none;
}
QSlider::groove:horizontal {
    height: 5px;
    background: #303743;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 16px;
    margin: -6px 0;
    background: #4f8cff;
    border-radius: 8px;
}
"""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ScreenCam Studio v1.8.4 — Sharp Screen Final")
        self.resize(1360, 850)
        self.setMinimumSize(1050, 680)

        self.monitors: list[MonitorInfo] = []
        self.capture_worker: CaptureWorker | None = None
        self.encoder: FFmpegEncoder | None = None
        self.latest_preview: QImage | None = None
        self._audio_scan_worker: AudioScanWorker | None = None
        self._mic_test_worker: MicrophoneTestWorker | None = None
        self._session_stop_worker: SessionStopWorker | None = None
        self._pending_desktop_audio = None
        self._pending_microphone = None
        self._pending_stop_error = ""
        self._close_pending = False

        self._build_ui()
        self.setStyleSheet(APP_STYLE)

        QTimer.singleShot(100, self.refresh_devices)

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setMinimumWidth(390)
        settings_scroll.setMaximumWidth(500)

        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(10, 10, 10, 10)
        settings_layout.setSpacing(10)

        title = QLabel("ScreenCam Studio")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        subtitle = QLabel("Clock chung A/V, capture native và tối ưu độ nét màn hình.")
        subtitle.setStyleSheet("color: #9aa5b5;")
        settings_layout.addWidget(title)
        settings_layout.addWidget(subtitle)

        source_group = QGroupBox("1. Nguồn hình ảnh")
        source_form = QFormLayout(source_group)

        self.monitor_combo = QComboBox()
        self.monitor_combo.currentIndexChanged.connect(self._apply_quality_profile)
        self.camera_combo = QComboBox()
        self.camera_combo.addItem("Không dùng webcam", None)

        self.position_combo = QComboBox()
        self.position_combo.addItem("Góc phải dưới", "bottom-right")
        self.position_combo.addItem("Góc trái dưới", "bottom-left")
        self.position_combo.addItem("Góc phải trên", "top-right")
        self.position_combo.addItem("Góc trái trên", "top-left")

        self.camera_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.camera_size_slider.setRange(15, 45)
        self.camera_size_slider.setValue(25)
        self.camera_size_label = QLabel("25%")
        self.camera_size_slider.valueChanged.connect(
            lambda value: self.camera_size_label.setText(f"{value}%")
        )
        size_row = QWidget()
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.addWidget(self.camera_size_slider)
        size_layout.addWidget(self.camera_size_label)

        self.mirror_checkbox = QCheckBox("Lật gương webcam")
        self.mirror_checkbox.setChecked(True)

        self.capture_backend_combo = QComboBox()
        self.capture_backend_combo.addItem("DXcam — hiệu năng cao", "dxcam")
        self.capture_backend_combo.addItem(
            "MSS — tương thích video trình duyệt", "mss"
        )

        refresh_button = QPushButton("Quét lại màn hình / camera")
        refresh_button.clicked.connect(self.refresh_devices)

        source_form.addRow("Màn hình:", self.monitor_combo)
        source_form.addRow("Webcam:", self.camera_combo)
        source_form.addRow("Vị trí webcam:", self.position_combo)
        source_form.addRow("Kích thước:", size_row)
        source_form.addRow("Capture backend:", self.capture_backend_combo)
        source_form.addRow("", self.mirror_checkbox)
        source_form.addRow("", refresh_button)
        settings_layout.addWidget(source_group)

        video_group = QGroupBox("2. Chất lượng video")
        video_form = QFormLayout(video_group)

        self.quality_combo = QComboBox()
        for profile in QUALITY_PROFILES:
            self.quality_combo.addItem(profile.label, profile.key)
        native_index = self.quality_combo.findData("native_sharp_30")
        self.quality_combo.setCurrentIndex(native_index if native_index >= 0 else 1)
        self.quality_combo.currentIndexChanged.connect(self._apply_quality_profile)

        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("1280 × 720 (HD)", (1280, 720))
        self.resolution_combo.addItem("1920 × 1080 (Full HD)", (1920, 1080))
        self.resolution_combo.addItem("2560 × 1440 (2K)", (2560, 1440))

        self.fps_combo = QComboBox()
        self.fps_combo.addItem("30 FPS", 30)
        self.fps_combo.addItem("60 FPS", 60)
        # The output profile is authoritative. This prevents a report labelled
        # 1080p while the actual file is manually changed to 720p.
        self.resolution_combo.setEnabled(False)
        self.fps_combo.setEnabled(False)

        self.performance_combo = QComboBox()
        self.performance_combo.addItem(
            "Tự động — ưu tiên encoder phần cứng",
            {"preset": "superfast", "preview_fps": 8, "preview_width": 960},
        )
        self.performance_combo.addItem(
            "Chỉ CPU — tương thích",
            {"preset": "veryfast", "preview_fps": 7, "preview_width": 900},
        )

        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(1500, 30000)
        self.bitrate_spin.setSingleStep(500)
        self.bitrate_spin.setValue(6000)
        self.bitrate_spin.setSuffix(" kbps")

        self.quality_description = QLabel("")
        self.quality_description.setWordWrap(True)
        self.quality_description.setStyleSheet("color: #9aa5b5; font-size: 12px;")

        video_form.addRow("Profile đầu ra:", self.quality_combo)
        video_form.addRow("Độ phân giải:", self.resolution_combo)
        video_form.addRow("Tốc độ khung hình:", self.fps_combo)
        video_form.addRow("Encoder:", self.performance_combo)
        video_form.addRow("Bitrate stream/mức trần:", self.bitrate_spin)
        video_form.addRow("", self.quality_description)
        settings_layout.addWidget(video_group)

        audio_group = QGroupBox("3. Âm thanh máy tính + microphone")
        audio_form = QFormLayout(audio_group)

        self.desktop_audio_combo = QComboBox()
        self.desktop_audio_combo.addItem("Không thu âm thanh máy", None)
        self.microphone_combo = QComboBox()
        self.microphone_combo.addItem("Không thu microphone", None)

        self.desktop_volume_spin = QSpinBox()
        self.desktop_volume_spin.setRange(0, 200)
        self.desktop_volume_spin.setValue(100)
        self.desktop_volume_spin.setSuffix("%")

        self.microphone_volume_spin = QSpinBox()
        self.microphone_volume_spin.setRange(0, 200)
        self.microphone_volume_spin.setValue(100)
        self.microphone_volume_spin.setSuffix("%")

        self.enhance_mic_checkbox = QCheckBox("Lọc tiếng ù và cân bằng giọng nói")
        self.enhance_mic_checkbox.setChecked(True)

        self.audio_sync_spin = QSpinBox()
        self.audio_sync_spin.setRange(-500, 500)
        self.audio_sync_spin.setSingleStep(10)
        self.audio_sync_spin.setValue(0)
        self.audio_sync_spin.setSuffix(" ms")
        self.audio_sync_spin.setToolTip(
            "Số âm đưa audio sớm hơn; số dương làm audio trễ hơn. Mặc định 0 ms."
        )

        self.audio_refresh_button = QPushButton("Quét lại thiết bị âm thanh")
        self.audio_refresh_button.clicked.connect(self.refresh_audio_devices)

        self.mic_test_button = QPushButton("Test mic 3 giây")
        self.mic_test_button.clicked.connect(self.test_microphone)

        open_playback_devices = QPushButton("Mở Playback devices")
        open_playback_devices.clicked.connect(self.open_windows_playback_devices)

        audio_buttons = QWidget()
        audio_buttons_layout = QHBoxLayout(audio_buttons)
        audio_buttons_layout.setContentsMargins(0, 0, 0, 0)
        audio_buttons_layout.addWidget(self.audio_refresh_button)
        audio_buttons_layout.addWidget(self.mic_test_button)
        audio_buttons_layout.addWidget(open_playback_devices)

        self.desktop_audio_status_label = QLabel("")
        self.desktop_audio_status_label.setWordWrap(True)
        self.desktop_audio_status_label.setStyleSheet(
            "color: #e8ba66; font-size: 12px;"
        )

        audio_form.addRow("Âm thanh máy:", self.desktop_audio_combo)
        audio_form.addRow("", self.desktop_audio_status_label)
        audio_form.addRow("Âm lượng máy:", self.desktop_volume_spin)
        audio_form.addRow("Microphone:", self.microphone_combo)
        audio_form.addRow("Âm lượng mic:", self.microphone_volume_spin)
        audio_form.addRow("Bù đồng bộ A/V:", self.audio_sync_spin)
        audio_form.addRow("", self.enhance_mic_checkbox)
        audio_form.addRow("", audio_buttons)

        audio_note = QLabel(
            "Tiếng máy và microphone được trộn thành một luồng PCM 48 kHz stereo "
            "trước khi vào FFmpeg. Video và audio dùng cùng một clock và cùng jitter buffer."
        )
        audio_note.setWordWrap(True)
        audio_note.setStyleSheet("color: #9aa5b5; font-size: 12px;")
        audio_form.addRow("", audio_note)
        settings_layout.addWidget(audio_group)

        ffmpeg_group = QGroupBox("4. FFmpeg và thư mục lưu")
        ffmpeg_form = QFormLayout(ffmpeg_group)

        self.ffmpeg_edit = QLineEdit(find_ffmpeg())
        ffmpeg_browse = QPushButton("Chọn ffmpeg.exe")
        ffmpeg_browse.clicked.connect(self.browse_ffmpeg)
        ffmpeg_row = QWidget()
        ffmpeg_layout = QHBoxLayout(ffmpeg_row)
        ffmpeg_layout.setContentsMargins(0, 0, 0, 0)
        ffmpeg_layout.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_layout.addWidget(ffmpeg_browse)

        default_output = Path.home() / "Videos" / "ScreenCamStudio"
        self.output_edit = QLineEdit(str(default_output))
        output_browse = QPushButton("Chọn thư mục")
        output_browse.clicked.connect(self.browse_output_folder)
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_edit, 1)
        output_layout.addWidget(output_browse)

        ffmpeg_form.addRow("FFmpeg:", ffmpeg_row)
        ffmpeg_form.addRow("Lưu video:", output_row)
        settings_layout.addWidget(ffmpeg_group)

        stream_group = QGroupBox("5. YouTube Live / RTMPS")
        stream_form = QFormLayout(stream_group)
        self.stream_url_edit = QLineEdit()
        self.stream_url_edit.setPlaceholderText(
            "Dán Stream URL từ YouTube Studio"
        )
        self.stream_key_edit = QLineEdit()
        self.stream_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.stream_key_edit.setPlaceholderText("Dán Stream Key")
        self.show_key_checkbox = QCheckBox("Hiện Stream Key")
        self.show_key_checkbox.toggled.connect(
            lambda checked: self.stream_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal
                if checked
                else QLineEdit.EchoMode.Password
            )
        )
        stream_note = QLabel(
            "Ứng dụng không lưu Stream Key. Không gửi key này cho người khác."
        )
        stream_note.setWordWrap(True)
        stream_note.setStyleSheet("color: #e8ba66; font-size: 12px;")

        stream_form.addRow("Stream URL:", self.stream_url_edit)
        stream_form.addRow("Stream Key:", self.stream_key_edit)
        stream_form.addRow("", self.show_key_checkbox)
        stream_form.addRow("", stream_note)
        settings_layout.addWidget(stream_group)

        buttons_group = QGroupBox("6. Điều khiển")
        buttons_layout = QGridLayout(buttons_group)
        self.preview_button = QPushButton("Bắt đầu xem trước")
        self.preview_button.clicked.connect(self.toggle_preview)

        self.record_button = QPushButton("Quay MP4")
        self.record_button.setObjectName("recordButton")
        self.record_button.clicked.connect(self.start_recording)

        self.stream_button = QPushButton("Phát trực tiếp")
        self.stream_button.setObjectName("streamButton")
        self.stream_button.clicked.connect(self.start_streaming)

        self.stop_button = QPushButton("Dừng và lưu video")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.clicked.connect(self.stop_encoder)
        self.stop_button.setEnabled(False)

        buttons_layout.addWidget(self.preview_button, 0, 0, 1, 2)
        buttons_layout.addWidget(self.record_button, 1, 0)
        buttons_layout.addWidget(self.stream_button, 1, 1)
        buttons_layout.addWidget(self.stop_button, 2, 0, 1, 2)
        settings_layout.addWidget(buttons_group)
        self._apply_quality_profile()
        settings_layout.addStretch(1)

        settings_scroll.setWidget(settings_widget)
        splitter.addWidget(settings_scroll)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(12, 12, 12, 12)

        self.preview_label = QLabel(
            "Chọn màn hình và nhấn “Bắt đầu xem trước”"
        )
        self.preview_label.setObjectName("preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(600, 340)
        preview_layout.addWidget(self.preview_label, 1)

        status_frame = QFrame()
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(0, 4, 0, 4)
        self.status_label = QLabel("Sẵn sàng")
        self.status_label.setObjectName("status")
        self.fps_label = QLabel("0.0 FPS")
        self.fps_label.setStyleSheet("color: #8eb7ff;")
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.fps_label)
        preview_layout.addWidget(status_frame)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(145)
        self.log_box.setPlaceholderText("Nhật ký FFmpeg sẽ hiển thị tại đây.")
        preview_layout.addWidget(self.log_box)

        splitter.addWidget(preview_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 930])

        self.setCentralWidget(splitter)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.append(f"[{timestamp}] {message}")

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def refresh_devices(self) -> None:
        selected_monitor_index = self.monitor_combo.currentData()
        selected_camera_index = self.camera_combo.currentData()

        try:
            self.monitors = list_monitors()
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi màn hình", str(exc))
            self.monitors = []

        self.monitor_combo.clear()
        for monitor in self.monitors:
            self.monitor_combo.addItem(monitor.label, monitor.index)

        if selected_monitor_index is not None:
            found_index = self.monitor_combo.findData(selected_monitor_index)
            if found_index >= 0:
                self.monitor_combo.setCurrentIndex(found_index)

        self.camera_combo.clear()
        self.camera_combo.addItem("Không dùng webcam", None)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for index, label in list_cameras():
                self.camera_combo.addItem(label, index)
        finally:
            QApplication.restoreOverrideCursor()

        if selected_camera_index is not None:
            found_index = self.camera_combo.findData(selected_camera_index)
            if found_index >= 0:
                self.camera_combo.setCurrentIndex(found_index)

        self.refresh_audio_devices()
        self._apply_quality_profile()
        self.log(
            f"Tìm thấy {len(self.monitors)} màn hình và "
            f"{max(0, self.camera_combo.count() - 1)} camera."
        )

    @staticmethod
    def _find_audio_device_index(combo: QComboBox, keywords: tuple[str, ...]) -> int:
        for index in range(1, combo.count()):
            name = str(combo.itemData(index) or combo.itemText(index)).casefold()
            if any(keyword in name for keyword in keywords):
                return index
        return -1

    def open_windows_playback_devices(self) -> None:
        if os.name != "nt":
            QMessageBox.information(
                self,
                "Chỉ hỗ trợ Windows",
                "Nút này chỉ hoạt động trên Windows.",
            )
            return

        try:
            # Trang 0 của mmsys.cpl là tab Playback.
            subprocess.Popen(
                ["control.exe", "mmsys.cpl,,0"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Không mở được cài đặt âm thanh",
                f"Hãy mở Sound Control Panel > Playback thủ công.\n\n{exc}",
            )

    def refresh_audio_devices(self) -> None:
        worker = self._audio_scan_worker
        if worker is not None and worker.isRunning():
            self.desktop_audio_status_label.setText(
                "Đang quét thiết bị âm thanh ở chế độ nền…"
            )
            return

        self._pending_desktop_audio = self.desktop_audio_combo.currentData()
        self._pending_microphone = self.microphone_combo.currentData()

        self.audio_refresh_button.setEnabled(False)
        self.mic_test_button.setEnabled(False)
        self.desktop_audio_status_label.setText(
            "Đang quét Playback/WASAPI và các backend microphone ở chế độ nền…"
        )
        self.desktop_audio_status_label.setStyleSheet(
            "color: #e8ba66; font-size: 12px;"
        )

        worker = AudioScanWorker()
        worker.scan_completed.connect(self._apply_audio_scan_result)
        worker.finished.connect(self._audio_scan_finished)
        self._audio_scan_worker = worker
        worker.start()

    def _audio_scan_finished(self) -> None:
        self.audio_refresh_button.setEnabled(True)
        self.mic_test_button.setEnabled(True)
        worker = self._audio_scan_worker
        self._audio_scan_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._close_pending and self._session_stop_worker is None:
            QTimer.singleShot(0, self.close)

    def _apply_audio_scan_result(
        self,
        loopback_devices: list[LoopbackDeviceInfo],
        microphone_devices: list[MicrophoneDeviceInfo],
        scan_error: str,
    ) -> None:
        selected_desktop = self._pending_desktop_audio
        selected_microphone = self._pending_microphone

        self.desktop_audio_combo.clear()
        self.desktop_audio_combo.addItem("Không thu âm thanh máy", None)
        self.microphone_combo.clear()
        self.microphone_combo.addItem("Không thu microphone", None)

        for device in loopback_devices:
            self.desktop_audio_combo.addItem(device.label, device)
        for device in microphone_devices:
            self.microphone_combo.addItem(device.label, device)

        if loopback_devices:
            status = (
                f"Đã tìm thấy {len(loopback_devices)} Playback và "
                f"{len(microphone_devices)} lựa chọn microphone."
            )
            if scan_error:
                status += f" Lưu ý: {scan_error}"
            self.desktop_audio_status_label.setText(status)
            self.desktop_audio_status_label.setStyleSheet(
                "color: #79d69b; font-size: 12px;"
            )
            self.desktop_audio_combo.setToolTip(
                "Chọn đúng loa/tai nghe/màn hình đang phát âm thanh."
            )
        else:
            self.desktop_audio_status_label.setText(
                scan_error
                or "Không tìm thấy thiết bị Playback hỗ trợ WASAPI Loopback."
            )
            self.desktop_audio_status_label.setStyleSheet(
                "color: #e8ba66; font-size: 12px;"
            )

        desktop_index = self.desktop_audio_combo.findData(selected_desktop)
        if desktop_index >= 0 and selected_desktop is not None:
            self.desktop_audio_combo.setCurrentIndex(desktop_index)
        elif loopback_devices:
            default_combo_index = 1
            for combo_index in range(1, self.desktop_audio_combo.count()):
                device = self.desktop_audio_combo.itemData(combo_index)
                if isinstance(device, LoopbackDeviceInfo) and device.is_default:
                    default_combo_index = combo_index
                    break
            self.desktop_audio_combo.setCurrentIndex(default_combo_index)

        microphone_index = self.microphone_combo.findData(selected_microphone)
        if microphone_index >= 0 and selected_microphone is not None:
            self.microphone_combo.setCurrentIndex(microphone_index)
        elif microphone_devices:
            default_combo_index = 1
            for combo_index in range(1, self.microphone_combo.count()):
                device = self.microphone_combo.itemData(combo_index)
                if isinstance(device, MicrophoneDeviceInfo) and device.is_default:
                    default_combo_index = combo_index
                    break
            self.microphone_combo.setCurrentIndex(default_combo_index)

        self.log(
            "Thiết bị âm thanh: "
            f"{len(loopback_devices)} Playback/WASAPI, "
            f"{len(microphone_devices)} microphone/PortAudio."
        )

    def _selected_monitor_info(self) -> MonitorInfo | None:
        monitor_index = self.monitor_combo.currentData()
        if monitor_index is None:
            return self.monitors[0] if self.monitors else None
        return next(
            (monitor for monitor in self.monitors if monitor.index == int(monitor_index)),
            self.monitors[0] if self.monitors else None,
        )

    def _resolved_profile_resolution(self) -> tuple[int, int]:
        profile = get_quality_profile(str(self.quality_combo.currentData() or ""))
        if not profile.uses_native_resolution:
            return profile.width, profile.height
        monitor = self._selected_monitor_info()
        if monitor is not None:
            # H.264 requires even dimensions for common pixel formats.
            return monitor.width - (monitor.width % 2), monitor.height - (monitor.height % 2)
        return 1920, 1080

    def _apply_quality_profile(self) -> None:
        # This slot is also connected before all controls are fully constructed.
        if not hasattr(self, "quality_combo") or not hasattr(self, "resolution_combo"):
            return
        profile = get_quality_profile(str(self.quality_combo.currentData() or ""))
        width, height = self._resolved_profile_resolution()
        resolution = (width, height)
        resolution_index = self.resolution_combo.findData(resolution)
        if resolution_index < 0:
            label = (
                f"{width} × {height} (gốc màn hình)"
                if profile.uses_native_resolution
                else f"{width} × {height}"
            )
            self.resolution_combo.addItem(label, resolution)
            resolution_index = self.resolution_combo.findData(resolution)
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        fps_index = self.fps_combo.findData(profile.fps)
        if fps_index >= 0:
            self.fps_combo.setCurrentIndex(fps_index)
        self.bitrate_spin.setValue(profile.stream_bitrate_kbps)
        hardware_note = " Cần encoder phần cứng." if profile.requires_hardware else ""
        master_note = (
            " Ghi local bằng CPU để giữ 4:4:4; livestream vẫn tự dùng yuv420p."
            if profile.force_software_recording
            else ""
        )
        scale_note = (
            " Không scale màn hình."
            if profile.uses_native_resolution
            else " Scale bằng bicubic sắc nét nếu độ phân giải nguồn khác output."
        )
        self.quality_description.setText(
            f"{profile.description}{hardware_note}{master_note}{scale_note} "
            f"Recording: H.264 CFR, {profile.recording_pix_fmt}, CRF/CQ "
            f"{profile.recording_crf}; AAC {profile.audio_bitrate_kbps} kbps, 48 kHz."
        )

    def test_microphone(self) -> None:
        worker = self._mic_test_worker
        if worker is not None and worker.isRunning():
            return
        device = self.microphone_combo.currentData()
        if device is None or not isinstance(device, MicrophoneDeviceInfo):
            QMessageBox.information(
                self,
                "Chưa chọn microphone",
                "Hãy chọn một microphone rồi nhấn Test mic 3 giây.",
            )
            return
        self.mic_test_button.setEnabled(False)
        self.desktop_audio_status_label.setText(
            "Đang test microphone — hãy nói liên tục trong 3 giây…"
        )
        worker = MicrophoneTestWorker(device)
        worker.test_completed.connect(self._microphone_test_completed)
        worker.test_failed.connect(self._microphone_test_failed)
        worker.finished.connect(self._microphone_test_finished)
        self._mic_test_worker = worker
        worker.start()

    def _microphone_test_completed(self, peak_dbfs, samples: int) -> None:
        if peak_dbfs is None or samples <= 0:
            message = "Không nhận được mẫu âm thanh từ microphone này."
            style = "color: #ff7878; font-size: 12px;"
        elif float(peak_dbfs) < -65:
            message = (
                f"Mic gần như im lặng ({float(peak_dbfs):.1f} dBFS). "
                "Hãy chọn cùng tên mic ở backend khác, ưu tiên MME/DirectSound/WDM-KS."
            )
            style = "color: #ff7878; font-size: 12px;"
        elif float(peak_dbfs) < -45:
            message = f"Mic có tín hiệu nhưng khá nhỏ: {float(peak_dbfs):.1f} dBFS."
            style = "color: #e8ba66; font-size: 12px;"
        else:
            message = f"Mic hoạt động tốt: peak {float(peak_dbfs):.1f} dBFS."
            style = "color: #85d49a; font-size: 12px;"
        self.desktop_audio_status_label.setText(message)
        self.desktop_audio_status_label.setStyleSheet(style)

    def _microphone_test_failed(self, message: str) -> None:
        self.desktop_audio_status_label.setText(f"Test microphone thất bại: {message}")
        self.desktop_audio_status_label.setStyleSheet(
            "color: #ff7878; font-size: 12px;"
        )

    def _microphone_test_finished(self) -> None:
        self.mic_test_button.setEnabled(True)
        worker = self._mic_test_worker
        self._mic_test_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._close_pending and self._session_stop_worker is None:
            QTimer.singleShot(0, self.close)

    def browse_ffmpeg(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn ffmpeg.exe",
            self.ffmpeg_edit.text() or str(Path.home()),
            "FFmpeg (ffmpeg.exe);;Tất cả tệp (*.*)",
        )
        if filename:
            self.ffmpeg_edit.setText(filename)
            self.refresh_audio_devices()

    def browse_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Chọn thư mục lưu video",
            self.output_edit.text() or str(Path.home()),
        )
        if folder:
            self.output_edit.setText(folder)

    def _capture_config(self) -> CaptureConfig:
        monitor_index = self.monitor_combo.currentData()
        if monitor_index is None:
            raise ValueError("Chưa chọn màn hình cần quay.")

        width, height = self._resolved_profile_resolution()
        profile = get_quality_profile(str(self.quality_combo.currentData() or ""))
        performance = self.performance_combo.currentData() or {}
        return CaptureConfig(
            monitor_index=int(monitor_index),
            camera_index=self.camera_combo.currentData(),
            camera_position=self.position_combo.currentData(),
            camera_width_percent=self.camera_size_slider.value(),
            mirror_camera=self.mirror_checkbox.isChecked(),
            output_width=int(width),
            output_height=int(height),
            fps=int(self.fps_combo.currentData()),
            preview_fps=int(performance.get("preview_fps", profile.preview_fps)),
            preview_max_width=int(performance.get("preview_width", profile.preview_max_width)),
            prefer_dxcam=self.capture_backend_combo.currentData() != "mss",
            screen_sharpen_strength=profile.screen_sharpen_strength,
            upscale_filter=profile.upscale_filter,
        )

    def start_preview(self) -> bool:
        stop_worker = self._session_stop_worker
        if stop_worker is not None and stop_worker.isRunning():
            return False
        if self.capture_worker is not None and self.capture_worker.isRunning():
            return True

        try:
            config = self._capture_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Thiếu thông tin", str(exc))
            return False

        worker = CaptureWorker(config)
        worker.preview_frame.connect(self.show_preview_frame)
        worker.status_message.connect(self.set_status)
        worker.capture_error.connect(self.on_capture_error)
        worker.fps_updated.connect(
            lambda fps: self.fps_label.setText(f"{fps:.1f} FPS")
        )
        worker.finished.connect(self.on_capture_finished)

        self.capture_worker = worker
        self._set_config_enabled(False)
        self.preview_button.setText("Dừng xem trước")
        worker.start()
        return True

    def stop_preview(self) -> None:
        if self.encoder is not None and self.encoder.is_running:
            QMessageBox.information(
                self,
                "Đang quay",
                "Nhấn “Dừng và lưu video” để dừng cả quay và xem trước.",
            )
            return
        self._begin_session_stop(None, self.capture_worker)

    def _begin_session_stop(
        self,
        encoder: FFmpegEncoder | None,
        capture_worker: CaptureWorker | None,
        *,
        error_message: str = "",
    ) -> None:
        active_worker = self._session_stop_worker
        if active_worker is not None and active_worker.isRunning():
            return
        if encoder is None and (
            capture_worker is None or not capture_worker.isRunning()
        ):
            self._reset_session_ui("")
            return

        self._pending_stop_error = error_message
        if capture_worker is not None:
            capture_worker.set_encoder(None)
            capture_worker.requestInterruption()

        self.stop_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.stream_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.audio_refresh_button.setEnabled(False)
        self.mic_test_button.setEnabled(False)
        self.set_status(
            "Đang dừng nguồn thu và hoàn tất MP4 ở chế độ nền…"
        )
        self.log("Đang dừng phiên quay/phát; giao diện vẫn tiếp tục phản hồi.")

        worker = SessionStopWorker(encoder, capture_worker)
        worker.stop_completed.connect(self._on_session_stop_complete)
        worker.stop_failed.connect(self._on_session_stop_failed)
        worker.finished.connect(self._on_session_stop_worker_finished)
        self._session_stop_worker = worker
        worker.start()

    def _reset_session_ui(self, saved_file: str) -> None:
        self.encoder = None
        self.capture_worker = None
        self.latest_preview = None
        self.preview_label.clear()
        self.preview_label.setText(
            "Chọn màn hình và nhấn “Bắt đầu xem trước”"
        )
        self.preview_button.setText("Bắt đầu xem trước")
        self.preview_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.record_button.setEnabled(True)
        self.stream_button.setEnabled(True)
        self.audio_refresh_button.setEnabled(True)
        self.mic_test_button.setEnabled(True)
        self._set_config_enabled(True)
        self.fps_label.setText("0.0 FPS")

        if saved_file:
            self.set_status(f"Đã dừng và lưu video: {saved_file}")
            self.log(f"Đã lưu video: {saved_file}")
        else:
            self.set_status("Đã dừng quay/phát và xem trước.")
            self.log("Đã dừng quay/phát và xem trước.")

    def _on_session_stop_complete(self, saved_file: str) -> None:
        pending_error = self._pending_stop_error
        self._pending_stop_error = ""
        self._reset_session_ui(saved_file)

        if pending_error:
            QMessageBox.critical(self, "Lỗi âm thanh / FFmpeg", pending_error)

    def _on_session_stop_failed(self, message: str) -> None:
        self._pending_stop_error = ""
        self._reset_session_ui("")
        self.log(f"Lỗi khi dừng phiên: {message}")
        QMessageBox.critical(self, "Không thể hoàn tất video", message)
    def _on_session_stop_worker_finished(self) -> None:
        worker = self._session_stop_worker
        self._session_stop_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._close_pending:
            QTimer.singleShot(0, self.close)

    def toggle_preview(self) -> None:
        if self.capture_worker is not None and self.capture_worker.isRunning():
            self.stop_preview()
        else:
            self.start_preview()

    def _validate_ffmpeg(self) -> str:
        path = self.ffmpeg_edit.text().strip()
        if not path:
            raise ValueError(
                "Chưa tìm thấy FFmpeg. Hãy chọn file ffmpeg.exe."
            )
        if os.path.sep in path and not Path(path).exists():
            raise ValueError("Đường dẫn ffmpeg.exe không tồn tại.")
        return path

    def _encoder_config(self, mode: str) -> EncoderConfig:
        ffmpeg_path = self._validate_ffmpeg()
        capture_config = self._capture_config()

        output_file = ""
        stream_url = ""
        stream_key = ""

        desktop_audio = self.desktop_audio_combo.currentData()
        microphone = self.microphone_combo.currentData()
        if desktop_audio is not None and not isinstance(
            desktop_audio, LoopbackDeviceInfo
        ):
            raise ValueError(
                "Nguồn âm thanh máy không hợp lệ. Hãy quét lại thiết bị âm thanh."
            )
        if microphone is not None and not isinstance(
            microphone, MicrophoneDeviceInfo
        ):
            raise ValueError(
                "Nguồn microphone không hợp lệ. Hãy quét lại thiết bị âm thanh."
            )

        if mode == "record":
            output_dir = Path(self.output_edit.text().strip())
            output_dir.mkdir(parents=True, exist_ok=True)
            # Milliseconds plus collision checking guarantee that every session
            # receives a new path, including immediately repeated recordings.
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
            candidate = output_dir / f"screen_recording_{stamp}.mp4"
            suffix = 2
            while candidate.exists() or candidate.with_name(
                candidate.stem + ".recording.mp4"
            ).exists():
                candidate = output_dir / f"screen_recording_{stamp}_{suffix}.mp4"
                suffix += 1
            output_file = str(candidate)
        else:
            stream_url = self.stream_url_edit.text().strip()
            stream_key = self.stream_key_edit.text().strip()
            if not stream_url or not stream_key:
                raise ValueError(
                    "Hãy dán Stream URL và Stream Key từ YouTube Studio."
                )
            if not (
                stream_url.startswith("rtmp://")
                or stream_url.startswith("rtmps://")
            ):
                raise ValueError("Stream URL phải bắt đầu bằng rtmp:// hoặc rtmps://.")

        return EncoderConfig(
            ffmpeg_path=ffmpeg_path,
            mode=mode,  # type: ignore[arg-type]
            width=capture_config.output_width,
            height=capture_config.output_height,
            fps=capture_config.fps,
            video_bitrate_kbps=self.bitrate_spin.value(),
            quality_profile_key=str(self.quality_combo.currentData() or "youtube_1080p30"),
            video_encoder_mode=(
                "auto" if self.performance_combo.currentIndex() == 0 else "software"
            ),
            x264_preset=str(
                (self.performance_combo.currentData() or {}).get(
                    "preset", "superfast"
                )
            ),
            desktop_audio_index=(
                desktop_audio.index if desktop_audio is not None else None
            ),
            desktop_audio_name=(
                desktop_audio.name if desktop_audio is not None else ""
            ),
            desktop_audio_sample_rate=(
                desktop_audio.sample_rate if desktop_audio is not None else 48000
            ),
            desktop_audio_channels=(
                desktop_audio.channels if desktop_audio is not None else 2
            ),
            microphone_index=(
                microphone.index if microphone is not None else None
            ),
            microphone_name=(
                microphone.name if microphone is not None else ""
            ),
            microphone_sample_rate=(
                microphone.sample_rate if microphone is not None else 48000
            ),
            microphone_channels=(
                microphone.channels if microphone is not None else 1
            ),
            enhance_microphone=self.enhance_mic_checkbox.isChecked(),
            desktop_audio_volume=self.desktop_volume_spin.value(),
            microphone_volume=self.microphone_volume_spin.value(),
            audio_sync_offset_ms=self.audio_sync_spin.value(),
            output_file=output_file,
            stream_url=stream_url,
            stream_key=stream_key,
        )

    def _start_encoder(self, mode: str) -> None:
        stop_worker = self._session_stop_worker
        if stop_worker is not None and stop_worker.isRunning():
            return
        if self.encoder is not None and self.encoder.is_running:
            QMessageBox.information(
                self,
                "Đang hoạt động",
                "Ứng dụng đang quay hoặc phát trực tiếp.",
            )
            return

        if not self.start_preview():
            return

        try:
            config = self._encoder_config(mode)
            capture = self.capture_worker
            if capture is None:
                raise RuntimeError("Luồng xem trước chưa sẵn sàng.")

            self.set_status("Đang đồng bộ khung hình mở đầu…")
            QApplication.processEvents()
            initial_frame = capture.wait_for_initial_frame(timeout=4.0)
            if initial_frame is None:
                raise RuntimeError(
                    "Không lấy được khung hình mở đầu từ màn hình đã chọn."
                )

            encoder = FFmpegEncoder(config)
            encoder.log_message.connect(self.log)
            encoder.encoder_error.connect(self.on_encoder_error)
            encoder.encoder_stopped.connect(self.log)
            encoder.start(initial_frame.data, initial_frame.timestamp)
        except Exception as exc:
            QMessageBox.critical(self, "Không thể khởi động FFmpeg", str(exc))
            self.log(f"Lỗi: {exc}")
            # A failed encoder start must not leave a hidden preview session
            # occupying the camera or monitor capture resources.
            if self.capture_worker is not None:
                self._begin_session_stop(None, self.capture_worker)
            return

        self.encoder = encoder
        if self.capture_worker is not None:
            self.capture_worker.set_encoder(encoder)

        self.stop_button.setEnabled(True)
        self.record_button.setEnabled(False)
        self.stream_button.setEnabled(False)

        if mode == "record":
            self.set_status(f"Đang quay: {config.output_file}")
            self.log(f"Bắt đầu quay MP4: {config.output_file}")
        else:
            self.set_status("Đang phát trực tiếp RTMPS.")
            self.log("Bắt đầu phát trực tiếp. Stream Key được ẩn khỏi nhật ký.")

    def start_recording(self) -> None:
        self._start_encoder("record")

    def start_streaming(self) -> None:
        answer = QMessageBox.question(
            self,
            "Bắt đầu phát trực tiếp?",
            "Hình ảnh và âm thanh sẽ được gửi đến Stream URL đã nhập. Tiếp tục?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_encoder("stream")

    def stop_encoder(self) -> None:
        self._begin_session_stop(self.encoder, self.capture_worker)

    def _set_config_enabled(self, enabled: bool) -> None:
        widgets = [
            self.monitor_combo,
            self.quality_combo,
            self.camera_combo,
            self.position_combo,
            self.camera_size_slider,
            self.mirror_checkbox,
            self.capture_backend_combo,
            self.performance_combo,
            self.desktop_audio_combo,
            self.microphone_combo,
            self.desktop_volume_spin,
            self.microphone_volume_spin,
            self.enhance_mic_checkbox,
            self.audio_sync_spin,
        ]
        for widget in widgets:
            widget.setEnabled(enabled)
        # Resolution/FPS are always controlled by the selected quality profile.
        self.resolution_combo.setEnabled(False)
        self.fps_combo.setEnabled(False)

    def show_preview_frame(self, image: QImage) -> None:
        try:
            self.latest_preview = image
            pixmap = QPixmap.fromImage(image)
            scaled = pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                # Preview is intentionally optimized for responsiveness. This
                # does not change the quality of the recorded/streamed video.
                Qt.TransformationMode.FastTransformation,
            )
            self.preview_label.setPixmap(scaled)
        finally:
            worker = self.capture_worker
            if worker is not None:
                worker.preview_consumed()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self.latest_preview is not None:
            self.show_preview_frame(self.latest_preview)

    def on_capture_error(self, message: str) -> None:
        self.log(f"Lỗi capture: {message}")
        if self.encoder is not None:
            self._begin_session_stop(
                self.encoder,
                self.capture_worker,
                error_message=message,
            )
        else:
            QMessageBox.critical(self, "Lỗi quay màn hình", message)

    def on_capture_finished(self) -> None:
        stop_worker = self._session_stop_worker
        if stop_worker is not None and stop_worker.isRunning():
            return
        self.preview_button.setText("Bắt đầu xem trước")
        if self.encoder is None or not self.encoder.is_running:
            self.capture_worker = None
            self.latest_preview = None
            self._set_config_enabled(True)

    def on_encoder_error(self, message: str) -> None:
        self.log(f"Lỗi encoder: {message}")
        self.set_status("FFmpeg hoặc WASAPI gặp lỗi; đang đóng phiên an toàn…")
        self._begin_session_stop(
            self.encoder,
            self.capture_worker,
            error_message=message,
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        stop_worker = self._session_stop_worker
        audio_worker = self._audio_scan_worker
        mic_worker = self._mic_test_worker
        session_active = (
            self.encoder is not None
            or (
                self.capture_worker is not None
                and self.capture_worker.isRunning()
            )
        )

        if stop_worker is not None and stop_worker.isRunning():
            self._close_pending = True
            event.ignore()
            return

        if session_active:
            self._close_pending = True
            event.ignore()
            self._begin_session_stop(self.encoder, self.capture_worker)
            return

        if mic_worker is not None and mic_worker.isRunning():
            event.ignore()
            self._close_pending = True
            self.set_status("Đang chờ hoàn tất test microphone…")
            return

        if audio_worker is not None and audio_worker.isRunning():
            self._close_pending = True
            event.ignore()
            return

        event.accept()

