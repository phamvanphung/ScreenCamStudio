from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

import cv2
import mss
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from .devices import open_camera_capture
from .encoder import FFmpegEncoder


CameraPosition = Literal[
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
]


@dataclass(frozen=True)
class CaptureConfig:
    monitor_index: int
    camera_index: int | None
    camera_position: CameraPosition
    camera_width_percent: int
    mirror_camera: bool
    output_width: int
    output_height: int
    fps: int
    preview_fps: int = 8
    preview_max_width: int = 960
    prefer_dxcam: bool = True
    screen_sharpen_strength: float = 0.0
    upscale_filter: str = "lanczos"


@dataclass(frozen=True)
class InitialFrame:
    data: bytes
    timestamp: float


class CameraReader:
    """Continuously reads a webcam without blocking screen capture."""

    def __init__(
        self,
        camera_index: int,
        fps: int,
        on_status: Callable[[str], None],
    ) -> None:
        self.camera_index = camera_index
        self.fps = fps
        self.on_status = on_status
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: cv2.VideoCapture | None = None
        self._capture_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._sequence = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="webcam-reader",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        capture, backend_name = open_camera_capture(self.camera_index)
        if capture is None:
            self.on_status(
                "Không mở được webcam bằng MSMF/Auto; tiếp tục chỉ quay màn hình."
            )
            return

        with self._capture_lock:
            self._capture = capture
        try:
            self.on_status(f"Webcam đang dùng backend {backend_name}.")
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            capture.set(cv2.CAP_PROP_FPS, min(self.fps, 30))
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            while not self._stop_event.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    self._stop_event.wait(0.01)
                    continue
                with self._frame_lock:
                    self._latest_frame = frame
                    self._sequence += 1
        finally:
            with self._capture_lock:
                self._capture = None
            capture.release()

    def latest_frame(self) -> tuple[np.ndarray | None, int]:
        with self._frame_lock:
            return self._latest_frame, self._sequence

    def stop(self) -> None:
        self._stop_event.set()
        with self._capture_lock:
            capture = self._capture
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


class CaptureWorker(QThread):
    preview_frame = Signal(QImage)
    status_message = Signal(str)
    capture_error = Signal(str)
    fps_updated = Signal(float)

    def __init__(self, config: CaptureConfig) -> None:
        super().__init__()
        self.config = config
        self._encoder: FFmpegEncoder | None = None
        self._encoder_lock = threading.Lock()
        self._preview_lock = threading.Lock()
        self._preview_pending = False
        self._initial_frame_request = threading.Event()
        self._initial_frame_ready = threading.Event()
        self._latest_frame_lock = threading.Lock()
        self._latest_initial_frame: InitialFrame | None = None
        self._capture_backend = ""

    @property
    def capture_backend(self) -> str:
        return self._capture_backend

    def set_encoder(self, encoder: FFmpegEncoder | None) -> None:
        with self._encoder_lock:
            self._encoder = encoder
        if encoder is not None and self._capture_backend:
            encoder.set_capture_backend(self._capture_backend)

    def _get_encoder(self) -> FFmpegEncoder | None:
        with self._encoder_lock:
            return self._encoder

    def wait_for_initial_frame(self, timeout: float = 4.0) -> InitialFrame | None:
        self._initial_frame_ready.clear()
        self._initial_frame_request.set()
        if not self._initial_frame_ready.wait(timeout=max(0.1, timeout)):
            return None
        with self._latest_frame_lock:
            return self._latest_initial_frame

    def preview_consumed(self) -> None:
        with self._preview_lock:
            self._preview_pending = False

    def _reserve_preview_slot(self) -> bool:
        with self._preview_lock:
            if self._preview_pending:
                return False
            self._preview_pending = True
            return True

    @staticmethod
    def _camera_geometry(
        output_width: int,
        output_height: int,
        cam_width: int,
        cam_height: int,
        position: CameraPosition,
        width_percent: int,
    ) -> tuple[int, int, int, int, int]:
        target_width = max(160, int(output_width * width_percent / 100))
        target_height = max(90, int(target_width * cam_height / max(cam_width, 1)))
        max_height = int(output_height * 0.48)
        if target_height > max_height:
            ratio = max_height / target_height
            target_width = max(1, int(target_width * ratio))
            target_height = max_height
        margin = max(12, int(output_width * 0.012))
        border = max(3, int(output_width * 0.003))
        if position == "top-left":
            x, y = margin, margin
        elif position == "top-right":
            x, y = output_width - target_width - margin, margin
        elif position == "bottom-left":
            x, y = margin, output_height - target_height - margin
        else:
            x = output_width - target_width - margin
            y = output_height - target_height - margin
        return (
            max(0, min(x, output_width - target_width)),
            max(0, min(y, output_height - target_height)),
            target_width,
            target_height,
            border,
        )

    @staticmethod
    def _overlay_prepared_camera(
        frame: np.ndarray,
        prepared: np.ndarray,
        geometry: tuple[int, int, int, int, int],
    ) -> None:
        output_height, output_width = frame.shape[:2]
        x, y, target_width, target_height, border = geometry
        cv2.rectangle(
            frame,
            (max(0, x - border - 2), max(0, y - border - 2)),
            (
                min(output_width - 1, x + target_width + border + 2),
                min(output_height - 1, y + target_height + border + 2),
            ),
            (20, 20, 20),
            thickness=-1,
        )
        cv2.rectangle(
            frame,
            (max(0, x - border), max(0, y - border)),
            (
                min(output_width - 1, x + target_width + border),
                min(output_height - 1, y + target_height + border),
            ),
            (245, 245, 245),
            thickness=-1,
        )
        frame[y : y + target_height, x : x + target_width] = prepared

    @staticmethod
    def _to_preview_qimage(frame: np.ndarray, max_width: int) -> QImage:
        height, width = frame.shape[:2]
        preview = frame
        if width > max_width:
            scale = max_width / width
            preview = cv2.resize(
                frame,
                (max_width, max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        rgb = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        image_height, image_width, channels = rgb.shape
        return QImage(
            rgb.data,
            image_width,
            image_height,
            image_width * channels,
            QImage.Format.Format_RGB888,
        ).copy()

    def _process_frame(
        self,
        screen_bgr: np.ndarray,
        camera_reader: CameraReader | None,
        state: dict,
    ) -> np.ndarray:
        target_width = self.config.output_width
        target_height = self.config.output_height
        source_height, source_width = screen_bgr.shape[:2]

        if source_width == target_width and source_height == target_height:
            needs_copy = (
                camera_reader is not None
                or self.config.screen_sharpen_strength > 0
            )
            composed = (
                np.ascontiguousarray(screen_bgr.copy())
                if needs_copy
                else np.ascontiguousarray(screen_bgr)
            )
        else:
            scale = min(target_width / source_width, target_height / source_height)
            resized_width = max(1, int(source_width * scale))
            resized_height = max(1, int(source_height * scale))
            if scale < 1:
                interpolation = cv2.INTER_AREA
            elif self.config.upscale_filter == "lanczos":
                interpolation = cv2.INTER_LANCZOS4
            else:
                interpolation = cv2.INTER_CUBIC
            resized = cv2.resize(
                screen_bgr,
                (resized_width, resized_height),
                interpolation=interpolation,
            )
            composed = state.get("canvas")
            if composed is None or composed.shape[:2] != (target_height, target_width):
                composed = np.zeros((target_height, target_width, 3), dtype=np.uint8)
                state["canvas"] = composed
            composed.fill(0)
            offset_x = (target_width - resized_width) // 2
            offset_y = (target_height - resized_height) // 2
            composed[
                offset_y : offset_y + resized_height,
                offset_x : offset_x + resized_width,
            ] = resized

        # Sharpen only the desktop layer. The webcam is overlaid afterwards so its
        # already-good image is not altered. A low-strength unsharp mask restores
        # text/UI edges lost during resize and YUV conversion without creating
        # strong halos.
        sharpen = max(0.0, min(0.35, self.config.screen_sharpen_strength))
        if sharpen > 0:
            blurred = cv2.GaussianBlur(composed, (0, 0), sigmaX=0.8, sigmaY=0.8)
            cv2.addWeighted(
                composed,
                1.0 + sharpen,
                blurred,
                -sharpen,
                0.0,
                dst=composed,
            )

        if camera_reader is not None:
            camera_frame, sequence = camera_reader.latest_frame()
            if camera_frame is not None:
                if sequence != state.get("camera_sequence"):
                    source = cv2.flip(camera_frame, 1) if self.config.mirror_camera else camera_frame
                    geometry = self._camera_geometry(
                        target_width,
                        target_height,
                        source.shape[1],
                        source.shape[0],
                        self.config.camera_position,
                        self.config.camera_width_percent,
                    )
                    _, _, cam_w, cam_h, _ = geometry
                    state["prepared_camera"] = cv2.resize(
                        source,
                        (cam_w, cam_h),
                        interpolation=cv2.INTER_AREA,
                    )
                    state["camera_geometry"] = geometry
                    state["camera_sequence"] = sequence
                prepared = state.get("prepared_camera")
                geometry = state.get("camera_geometry")
                if prepared is not None and geometry is not None:
                    self._overlay_prepared_camera(composed, prepared, geometry)
        return composed

    def _deliver_frame(self, composed: np.ndarray, timestamp: float) -> None:
        encoder = self._get_encoder()
        need_initial = self._initial_frame_request.is_set()
        if need_initial or (encoder is not None and encoder.is_running):
            frame_bytes = composed.tobytes()
            if need_initial:
                with self._latest_frame_lock:
                    self._latest_initial_frame = InitialFrame(frame_bytes, timestamp)
                self._initial_frame_request.clear()
                self._initial_frame_ready.set()
            if encoder is not None and encoder.is_running:
                encoder.write_frame(frame_bytes, timestamp)

    def _emit_preview_if_due(
        self,
        composed: np.ndarray,
        now: float,
        last_preview: float,
        preview_interval: float,
    ) -> float:
        if now - last_preview < preview_interval or not self._reserve_preview_slot():
            return last_preview
        try:
            self.preview_frame.emit(
                self._to_preview_qimage(composed, self.config.preview_max_width)
            )
            return now
        except Exception:
            self.preview_consumed()
            raise

    def _run_dxcam(self, camera_reader: CameraReader | None) -> None:
        import dxcam

        output_index = max(0, self.config.monitor_index - 1)
        camera = dxcam.create(
            device_idx=0,
            output_idx=output_index,
            output_color="BGR",
            max_buffer_len=16,
        )
        camera.start(target_fps=self.config.fps, video_mode=True)
        self._capture_backend = f"DXcam/DXGI output {output_index}"
        self.status_message.emit(
            f"Đang xem trước bằng {self._capture_backend}."
        )

        frame_interval = 1.0 / max(1, self.config.fps)
        preview_interval = 1.0 / max(1, self.config.preview_fps)
        next_deadline = time.perf_counter()
        last_preview = 0.0
        fps_window_start = time.perf_counter()
        frames_counted = 0
        state: dict = {}
        try:
            while not self.isInterruptionRequested():
                next_deadline += frame_interval
                wait = next_deadline - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                elif wait < -frame_interval * 3:
                    next_deadline = time.perf_counter()

                frame = camera.get_latest_frame()
                if frame is None:
                    self.msleep(1)
                    continue
                timestamp = time.perf_counter()
                composed = self._process_frame(frame, camera_reader, state)
                self._deliver_frame(composed, timestamp)
                last_preview = self._emit_preview_if_due(
                    composed,
                    timestamp,
                    last_preview,
                    preview_interval,
                )
                frames_counted += 1
                elapsed = timestamp - fps_window_start
                if elapsed >= 1.0:
                    self.fps_updated.emit(frames_counted / elapsed)
                    frames_counted = 0
                    fps_window_start = timestamp
        finally:
            try:
                camera.stop()
            except Exception:
                pass
            try:
                camera.release()
            except Exception:
                pass

    def _run_mss(self, camera_reader: CameraReader | None) -> None:
        self._capture_backend = "MSS fallback"
        self.status_message.emit("DXcam không dùng được; đang dùng MSS fallback.")
        frame_interval = 1.0 / max(1, self.config.fps)
        preview_interval = 1.0 / max(1, self.config.preview_fps)
        next_deadline = time.perf_counter()
        last_preview = 0.0
        fps_window_start = time.perf_counter()
        frames_counted = 0
        state: dict = {}

        with mss.mss() as sct:
            if self.config.monitor_index >= len(sct.monitors):
                raise RuntimeError("Màn hình đã chọn không còn tồn tại.")
            monitor = sct.monitors[self.config.monitor_index]
            while not self.isInterruptionRequested():
                next_deadline += frame_interval
                wait = next_deadline - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                elif wait < -frame_interval * 3:
                    next_deadline = time.perf_counter()

                screenshot = sct.grab(monitor)
                timestamp = time.perf_counter()
                bgra = np.asarray(screenshot, dtype=np.uint8)
                composed = self._process_frame(bgra[:, :, :3], camera_reader, state)
                self._deliver_frame(composed, timestamp)
                last_preview = self._emit_preview_if_due(
                    composed,
                    timestamp,
                    last_preview,
                    preview_interval,
                )
                frames_counted += 1
                elapsed = timestamp - fps_window_start
                if elapsed >= 1.0:
                    self.fps_updated.emit(frames_counted / elapsed)
                    frames_counted = 0
                    fps_window_start = timestamp

    def run(self) -> None:
        camera_reader: CameraReader | None = None
        try:
            cv2.setNumThreads(max(1, min(4, os.cpu_count() or 2)))
            if self.config.camera_index is not None:
                camera_reader = CameraReader(
                    self.config.camera_index,
                    self.config.fps,
                    self.status_message.emit,
                )
                camera_reader.start()

            used_dxcam = False
            if os.name == "nt" and self.config.prefer_dxcam:
                try:
                    self._run_dxcam(camera_reader)
                    used_dxcam = True
                except Exception as exc:
                    self.status_message.emit(f"DXcam lỗi ({exc}); chuyển sang MSS.")
            if not used_dxcam and not self.isInterruptionRequested():
                self._run_mss(camera_reader)
        except Exception as exc:
            self.capture_error.emit(str(exc))
        finally:
            if camera_reader is not None:
                camera_reader.stop()
            self.status_message.emit("Đã dừng xem trước.")
