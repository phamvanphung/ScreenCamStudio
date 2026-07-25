from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QObject, Signal

from .audio_loopback import AudioMixerBridge, AudioSourceSpec
from .diagnostics import SessionDiagnostics, find_ffprobe, write_diagnostics
from .quality import get_quality_profile
from .session_clock import SessionClock


EncoderMode = Literal["record", "stream"]
_ENCODER_CACHE: dict[str, str] = {}


@dataclass(frozen=True)
class EncoderConfig:
    ffmpeg_path: str
    mode: EncoderMode
    width: int
    height: int
    fps: int
    video_bitrate_kbps: int
    x264_preset: str = "superfast"
    quality_profile_key: str = "youtube_1080p30"
    video_encoder_mode: str = "auto"

    desktop_audio_index: int | None = None
    desktop_audio_name: str = ""
    desktop_audio_sample_rate: int = 48000
    desktop_audio_channels: int = 2

    microphone_index: int | None = None
    microphone_name: str = ""
    microphone_sample_rate: int = 48000
    microphone_channels: int = 1
    enhance_microphone: bool = True

    desktop_audio_volume: int = 100
    microphone_volume: int = 100
    audio_sync_offset_ms: int = 0
    output_file: str = ""
    stream_url: str = ""
    stream_key: str = ""


@dataclass(frozen=True)
class _FrameSample:
    timestamp: float
    data: bytes


def _test_encoder(ffmpeg_path: str, encoder: str) -> bool:
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    encoder_args: dict[str, list[str]] = {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4"],
        "h264_qsv": ["-c:v", "h264_qsv", "-preset", "veryfast"],
        "h264_amf": ["-c:v", "h264_amf", "-quality", "speed"],
    }
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x180:r=30",
        "-frames:v",
        "3",
        *encoder_args[encoder],
        "-f",
        "null",
        null_target,
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=creationflags,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_best_h264_encoder(ffmpeg_path: str) -> str:
    cached = _ENCODER_CACHE.get(ffmpeg_path)
    if cached:
        return cached
    for encoder in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if _test_encoder(ffmpeg_path, encoder):
            _ENCODER_CACHE[ffmpeg_path] = encoder
            return encoder
    _ENCODER_CACHE[ffmpeg_path] = "libx264"
    return "libx264"


class FFmpegEncoder(QObject):
    """Timestamped frame scheduler and one-clock A/V encoder."""

    log_message = Signal(str)
    encoder_stopped = Signal(str)
    encoder_error = Signal(str)

    def __init__(self, config: EncoderConfig) -> None:
        super().__init__()
        self.config = config
        self.profile = get_quality_profile(config.quality_profile_key)
        self.clock = SessionClock()
        self.diagnostics = SessionDiagnostics(
            profile=self.profile.label,
            target_fps=config.fps,
            requested_width=config.width,
            requested_height=config.height,
            expected_pixel_format=(
                self.profile.recording_pix_fmt
                if config.mode == "record"
                else "yuv420p"
            ),
            screen_sharpen_strength=self.profile.screen_sharpen_strength,
            desktop_audio_enabled=config.desktop_audio_index is not None,
            microphone_enabled=config.microphone_index is not None,
            desktop_audio_device=config.desktop_audio_name,
            microphone_device=config.microphone_name,
        )

        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._stopping = False
        self._audio_mixer: AudioMixerBridge | None = None
        self._video_writer_thread: threading.Thread | None = None
        self._video_writer_done = threading.Event()

        self._frames: deque[_FrameSample] = deque(maxlen=max(120, config.fps * 6))
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._last_output_frame: bytes | None = None
        self._last_submitted_timestamp: float | None = None
        self._last_diagnostic_capture_timestamp: float | None = None

        self._working_output_file = ""
        self._audio_wave_file = ""
        self._saved_output_file = ""
        self._diagnostics_file = ""
        self._video_encoder = "libx264"

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def saved_output_file(self) -> str:
        return self._saved_output_file

    @property
    def diagnostics_file(self) -> str:
        return self._diagnostics_file

    def set_capture_backend(self, backend_name: str) -> None:
        self.diagnostics.capture_backend = backend_name

    def _audio_sources(self) -> list[AudioSourceSpec]:
        cfg = self.config
        sources: list[AudioSourceSpec] = []
        if cfg.desktop_audio_index is not None:
            sources.append(
                AudioSourceSpec(
                    device_index=cfg.desktop_audio_index,
                    name=cfg.desktop_audio_name or "Desktop audio",
                    sample_rate=cfg.desktop_audio_sample_rate,
                    channels=cfg.desktop_audio_channels,
                    volume_percent=cfg.desktop_audio_volume,
                    kind="desktop",
                )
            )
        if cfg.microphone_index is not None:
            sources.append(
                AudioSourceSpec(
                    device_index=cfg.microphone_index,
                    name=cfg.microphone_name or "Microphone",
                    sample_rate=cfg.microphone_sample_rate,
                    channels=cfg.microphone_channels,
                    volume_percent=cfg.microphone_volume,
                    kind="microphone",
                    enhance_microphone=cfg.enhance_microphone,
                )
            )
        return sources

    def _handle_audio_error(self, message: str) -> None:
        if not self._stopping:
            self.encoder_error.emit(message)

    def _prepare_audio(self) -> str:
        sources = self._audio_sources()
        if not sources:
            self._audio_wave_file = ""
            return ""

        output_wave_path = ""
        if self.config.mode == "record":
            final_path = Path(self.config.output_file)
            wave_path = final_path.with_name(final_path.stem + ".recording.wav")
            wave_path.unlink(missing_ok=True)
            output_wave_path = str(wave_path)
            self._audio_wave_file = output_wave_path
        else:
            self._audio_wave_file = ""

        mixer = AudioMixerBridge(
            sources=sources,
            prebuffer_ms=self.profile.prebuffer_ms,
            audio_sync_offset_ms=self.config.audio_sync_offset_ms,
            output_wave_path=output_wave_path,
            on_log=self.log_message.emit,
            on_error=self._handle_audio_error,
        )
        mixer.prepare()
        mixer.start(self.clock)
        self._audio_mixer = mixer
        return mixer.input_url

    def _video_codec_args(self) -> list[str]:
        cfg = self.config
        profile = self.profile
        encoder = self._video_encoder
        bitrate = max(1500, int(cfg.video_bitrate_kbps or profile.stream_bitrate_kbps))
        max_record = max(bitrate, profile.max_recording_bitrate_kbps)
        is_record = cfg.mode == "record"
        pixel_format = profile.recording_pix_fmt if is_record else "yuv420p"
        b_frames = 2 if is_record else 0

        common = [
            "-pix_fmt",
            pixel_format,
            "-sws_flags",
            "lanczos+accurate_rnd+full_chroma_int",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-g",
            str(max(cfg.fps * 2, 30)),
            "-keyint_min",
            str(max(cfg.fps * 2, 30)),
            "-sc_threshold",
            "0",
            "-bf",
            str(b_frames),
            "-fps_mode",
            "cfr",
        ]

        if encoder == "h264_nvenc":
            if is_record:
                return [
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "p6",
                    "-tune",
                    "hq",
                    "-rc",
                    "vbr",
                    "-cq",
                    str(profile.recording_crf),
                    "-b:v",
                    f"{bitrate}k",
                    "-maxrate",
                    f"{max_record}k",
                    "-bufsize",
                    f"{max_record * 2}k",
                    *common,
                ]
            return [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p5",
                "-tune",
                "ll",
                "-rc",
                "cbr",
                "-b:v",
                f"{bitrate}k",
                "-maxrate",
                f"{bitrate}k",
                "-bufsize",
                f"{bitrate * 2}k",
                *common,
            ]

        if encoder == "h264_qsv":
            if is_record:
                return [
                    "-c:v",
                    "h264_qsv",
                    "-preset",
                    "slow",
                    "-global_quality",
                    str(profile.recording_crf),
                    *common,
                ]
            return [
                "-c:v",
                "h264_qsv",
                "-preset",
                "medium",
                "-b:v",
                f"{bitrate}k",
                "-maxrate",
                f"{bitrate}k",
                "-bufsize",
                f"{bitrate * 2}k",
                *common,
            ]

        if encoder == "h264_amf":
            if is_record:
                qp = profile.recording_crf
                return [
                    "-c:v",
                    "h264_amf",
                    "-quality",
                    "quality",
                    "-rc",
                    "cqp",
                    "-qp_i",
                    str(max(0, qp - 2)),
                    "-qp_p",
                    str(qp),
                    "-qp_b",
                    str(min(51, qp + 2)),
                    *common,
                ]
            return [
                "-c:v",
                "h264_amf",
                "-quality",
                "balanced",
                "-rc",
                "cbr",
                "-b:v",
                f"{bitrate}k",
                "-maxrate",
                f"{bitrate}k",
                "-bufsize",
                f"{bitrate * 2}k",
                *common,
            ]

        preset = profile.software_preset or cfg.x264_preset or "fast"
        if is_record:
            h264_profile = "high444" if pixel_format == "yuv444p" else "high"
            return [
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(profile.recording_crf),
                "-profile:v",
                h264_profile,
                "-x264-params",
                "aq-mode=3:aq-strength=0.8",
                *common,
            ]
        return [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-tune",
            "zerolatency",
            "-b:v",
            f"{bitrate}k",
            "-maxrate",
            f"{bitrate}k",
            "-bufsize",
            f"{bitrate * 2}k",
            "-profile:v",
            "high",
            *common,
        ]

    def _build_command(self, audio_url: str) -> list[str]:
        cfg = self.config
        command = [
            cfg.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-thread_queue_size",
            "1024",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-video_size",
            f"{cfg.width}x{cfg.height}",
            "-framerate",
            str(cfg.fps),
            "-i",
            "pipe:0",
        ]

        has_audio = bool(audio_url)
        if has_audio:
            command.extend(
                [
                    "-thread_queue_size",
                    "8192",
                    "-f",
                    "s16le",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-i",
                    audio_url,
                ]
            )

        command.extend(["-map", "0:v:0", *self._video_codec_args()])
        if has_audio:
            command.extend(
                [
                    "-map",
                    "1:a:0",
                    "-af",
                    "aresample=48000:first_pts=0",
                    "-c:a",
                    "aac",
                    "-b:a",
                    f"{self.profile.audio_bitrate_kbps}k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                ]
            )
        else:
            command.append("-an")

        if cfg.mode == "record":
            if not cfg.output_file:
                raise ValueError("Thiếu đường dẫn file quay.")
            final_path = Path(cfg.output_file)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            working_path = final_path.with_name(final_path.stem + ".recording.mp4")
            working_path.unlink(missing_ok=True)
            self._working_output_file = str(working_path)
            command.extend(
                [
                    "-f",
                    "mp4",
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof",
                    "-frag_duration",
                    "1000000",
                    "-flush_packets",
                    "1",
                    self._working_output_file,
                ]
            )
        else:
            if not cfg.stream_url or not cfg.stream_key:
                raise ValueError("Thiếu Stream URL hoặc Stream Key.")
            target = f"{cfg.stream_url.rstrip('/')}/{cfg.stream_key.strip()}"
            command.extend(
                [
                    "-flvflags",
                    "no_duration_filesize",
                    "-muxdelay",
                    "0",
                    "-muxpreload",
                    "0",
                    "-f",
                    "flv",
                    target,
                ]
            )
        return command

    def start(
        self,
        initial_frame: bytes,
        initial_timestamp: float | None = None,
    ) -> None:
        expected_size = self.config.width * self.config.height * 3
        if len(initial_frame) != expected_size:
            raise ValueError("Khung hình mở đầu không đúng kích thước output.")

        with self._lock:
            if self.is_running:
                return
            force_software = (
                self.config.mode == "record"
                and self.profile.force_software_recording
            )
            self._video_encoder = (
                detect_best_h264_encoder(self.config.ffmpeg_path)
                if self.config.video_encoder_mode == "auto" and not force_software
                else "libx264"
            )
            self.diagnostics.video_encoder = self._video_encoder
            self.log_message.emit(
                f"Video encoder: {self._video_encoder}; "
                f"pixel format: "
                f"{self.profile.recording_pix_fmt if self.config.mode == 'record' else 'yuv420p'}."
            )

            audio_url = ""
            try:
                audio_url = self._prepare_audio()
                command = self._build_command(audio_url)
            except Exception:
                mixer = self._audio_mixer
                self._audio_mixer = None
                if mixer is not None:
                    mixer.stop(timeout=1.0)
                raise

            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    creationflags=creationflags,
                )
            except OSError as exc:
                mixer = self._audio_mixer
                self._audio_mixer = None
                if mixer is not None:
                    mixer.stop(timeout=1.0)
                raise RuntimeError(f"Không khởi động được FFmpeg: {exc}") from exc

            # Feed exactly one complete raw frame so FFmpeg can finish opening
            # input 0 and proceed to the TCP audio input. The scheduled writer
            # starts at frame index 1, so this frame is not duplicated in count.
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("FFmpeg không tạo được video pipe.")
            try:
                process.stdin.write(initial_frame)
            except (BrokenPipeError, OSError) as exc:
                process.terminate()
                raise RuntimeError(f"FFmpeg không nhận frame khởi tạo: {exc}") from exc

            if self._audio_mixer is not None and not self._audio_mixer.wait_connected(5.0):
                try:
                    process.stdin.close()
                except OSError:
                    pass
                process.terminate()
                mixer = self._audio_mixer
                self._audio_mixer = None
                if mixer is not None:
                    mixer.stop(timeout=1.0)
                raise RuntimeError("FFmpeg không kết nối được với audio mixer.")

            self._stopping = False
            self._video_writer_done.clear()
            timestamp = initial_timestamp or time.perf_counter()
            with self._frame_lock:
                self._frames.clear()
                self._frames.append(_FrameSample(timestamp, initial_frame))
                self._last_output_frame = initial_frame
                self._last_submitted_timestamp = timestamp
                self._last_diagnostic_capture_timestamp = None
            self.diagnostics.encoded_frames = 1
            self._video_writer_thread = threading.Thread(
                target=self._write_frames,
                name="timestamped-video-writer",
                daemon=True,
            )
            self._video_writer_thread.start()
            threading.Thread(
                target=self._read_stderr,
                name="ffmpeg-stderr",
                daemon=True,
            ).start()
            start_time = self.clock.arm(delay_seconds=0.28)
            self.log_message.emit(
                "A/V dùng một clock chung; bắt đầu sau "
                f"{int((start_time - time.perf_counter()) * 1000)} ms để tạo jitter buffer."
            )

    def write_frame(self, frame_bytes: bytes, capture_timestamp: float | None = None) -> bool:
        if not self.is_running or self._stopping:
            return False
        timestamp = capture_timestamp or time.perf_counter()
        with self._frame_lock:
            self._last_submitted_timestamp = timestamp
            self._frames.append(_FrameSample(timestamp, frame_bytes))

            # Prebuffer frames are useful to the scheduler but must not inflate
            # measured capture FPS. Diagnostics begin only at the shared clock.
            start_time = self.clock.start_time
            if start_time is not None and timestamp >= start_time:
                if self._last_diagnostic_capture_timestamp is not None:
                    interval_ms = (
                        timestamp - self._last_diagnostic_capture_timestamp
                    ) * 1000
                    if 0 < interval_ms < 2000:
                        self.diagnostics.capture_intervals_ms.append(interval_ms)
                self._last_diagnostic_capture_timestamp = timestamp
                self.diagnostics.capture_frames += 1
                self.diagnostics.submitted_frames += 1
        self._frame_event.set()
        return True

    def _select_frame(self, target_timestamp: float, half_interval: float) -> tuple[bytes | None, bool]:
        selected: bytes | None = None
        with self._frame_lock:
            while self._frames and self._frames[0].timestamp <= target_timestamp + half_interval:
                selected = self._frames.popleft().data
            if selected is None:
                selected = self._last_output_frame
            duplicated = selected is self._last_output_frame
            if selected is not None:
                self._last_output_frame = selected
        return selected, duplicated

    def _write_frames(self) -> None:
        fps = max(1, self.config.fps)
        frame_interval = 1.0 / fps
        prebuffer = self.profile.prebuffer_ms / 1000.0
        # Frame 0 was written synchronously during startup to unblock FFmpeg's
        # audio input connection. Continue from frame 1 on the shared timeline.
        frame_index = 1
        slow_logs = 0

        try:
            if not self.clock.start_event.wait(timeout=10.0):
                return
            start_time = self.clock.start_time
            if start_time is None:
                return
            output_origin = start_time + prebuffer

            while True:
                stop_time = self.clock.stop_time
                if stop_time is not None:
                    total_frames = max(1, int(math.ceil((stop_time - start_time) * fps)))
                    if frame_index >= total_frames:
                        break

                deadline = output_origin + frame_index * frame_interval
                now = time.perf_counter()
                if now < deadline:
                    self._frame_event.wait(min(0.01, deadline - now))
                    self._frame_event.clear()
                    continue

                target_timestamp = start_time + frame_index * frame_interval
                frame, duplicated = self._select_frame(target_timestamp, frame_interval * 0.55)
                if frame is None:
                    self._frame_event.wait(0.01)
                    self._frame_event.clear()
                    continue

                with self._lock:
                    process = self._process
                    stdin = process.stdin if process is not None else None
                if process is None or process.poll() is not None or stdin is None:
                    break

                write_start = time.perf_counter()
                try:
                    stdin.write(frame)
                except (BrokenPipeError, OSError) as exc:
                    if not self._stopping:
                        self.encoder_error.emit(f"FFmpeg ngừng nhận video: {exc}")
                    break
                write_ms = (time.perf_counter() - write_start) * 1000
                self.diagnostics.encoder_write_ms.append(write_ms)
                self.diagnostics.encoded_frames += 1
                if duplicated:
                    self.diagnostics.duplicated_frames += 1
                frame_index += 1

                if write_ms > frame_interval * 1000 and slow_logs < 3:
                    slow_logs += 1
                    self.log_message.emit(
                        f"Encoder mất {write_ms:.0f} ms/frame, cao hơn chu kỳ "
                        f"{frame_interval * 1000:.1f} ms; nên hạ profile nếu lặp lại."
                    )
        finally:
            self._video_writer_done.set()

    def _read_stderr(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.stderr is None:
            return
        for raw_line in iter(process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                self.log_message.emit(line)

    def _working_video_duration(self, working_path: Path) -> float:
        ffprobe = find_ffprobe(self.config.ffmpeg_path)
        if ffprobe:
            command = [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(working_path),
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    creationflags=creationflags,
                    check=False,
                )
                if completed.returncode == 0:
                    duration = float((completed.stdout or "0").strip())
                    if duration > 0:
                        return duration
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return max(
            1.0 / max(1, self.config.fps),
            self.diagnostics.encoded_frames / max(1, self.config.fps),
        )

    def _finalize_recording(self) -> str:
        final_path = Path(self.config.output_file)
        working_path = Path(self._working_output_file)
        audio_path = Path(self._audio_wave_file) if self._audio_wave_file else None
        if not working_path.exists() or working_path.stat().st_size < 1024:
            raise RuntimeError("FFmpeg không tạo được file video có dữ liệu.")

        video_duration = self._working_video_duration(working_path)
        optimized_path = final_path.with_name(final_path.stem + ".finalizing.mp4")
        optimized_path.unlink(missing_ok=True)
        command = [
            self.config.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(working_path),
        ]
        has_audio_file = (
            audio_path is not None
            and audio_path.exists()
            and audio_path.stat().st_size > 44
        )
        if has_audio_file:
            # The video stream is authoritative. Pad a short WAV with silence or
            # trim an overlong WAV to the exact captured video duration. Never use
            # -shortest here: it previously removed the last seconds of video.
            command.extend(
                [
                    "-i",
                    str(audio_path),
                    "-filter_complex",
                    (
                        "[1:a]aresample=48000:async=1:first_pts=0,"
                        f"apad,atrim=start=0:end={video_duration:.6f},"
                        "asetpts=N/SR/TB[a]"
                    ),
                    "-map",
                    "0:v:0",
                    "-map",
                    "[a]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    f"{self.profile.audio_bitrate_kbps}k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-t",
                    f"{video_duration:.6f}",
                ]
            )
        else:
            command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-an",
                    "-t",
                    f"{video_duration:.6f}",
                ]
            )
        command.extend(["-movflags", "+faststart", str(optimized_path)])

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=creationflags,
            check=False,
        )
        if (
            completed.returncode == 0
            and optimized_path.exists()
            and optimized_path.stat().st_size > 1024
        ):
            final_path.unlink(missing_ok=True)
            os.replace(optimized_path, final_path)
            working_path.unlink(missing_ok=True)
            if audio_path is not None:
                audio_path.unlink(missing_ok=True)
        else:
            # Always keep the full captured video and sidecar audio. The recovery
            # script can remux them later without losing the end of the timeline.
            final_path.unlink(missing_ok=True)
            os.replace(working_path, final_path)
            optimized_path.unlink(missing_ok=True)
            error_text = (completed.stderr or "Không rõ lỗi mux/finalize.").strip()
            Path(str(final_path) + ".finalize-warning.txt").write_text(
                error_text,
                encoding="utf-8",
            )
            self.log_message.emit(
                "Không ghép/tối ưu được MP4; đã giữ video đầy đủ và WAV sidecar."
            )
        return str(final_path)

    def stop(self) -> str:
        with self._lock:
            process = self._process
            if process is None:
                return self._saved_output_file
            self._stopping = True

        stop_time = self.clock.stop()
        self._frame_event.set()
        self._video_writer_done.wait(timeout=20.0)

        with self._lock:
            process = self._process
            if process is not None and process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        mixer = self._audio_mixer
        self._audio_mixer = None
        if mixer is not None:
            mixer.stop(timeout=10.0)
            metrics = mixer.metrics
            self.diagnostics.desktop_underflow_blocks = metrics.desktop_underflow_blocks
            self.diagnostics.microphone_underflow_blocks = metrics.microphone_underflow_blocks
            self.diagnostics.desktop_peak_dbfs = metrics.desktop_peak_dbfs
            self.diagnostics.microphone_peak_dbfs = metrics.microphone_peak_dbfs
            self.diagnostics.audio_output_frames = metrics.output_frames

        if process is not None:
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

        self.diagnostics.wall_duration_seconds = max(
            0.0,
            stop_time - (self.clock.start_time or stop_time),
        )

        saved_file = ""
        if self.config.mode == "record":
            saved_file = self._finalize_recording()
            self._saved_output_file = saved_file
            self._diagnostics_file = write_diagnostics(
                self.diagnostics,
                self.config.ffmpeg_path,
                saved_file,
            )
            self.log_message.emit(f"Báo cáo kiểm tra: {self._diagnostics_file}")
        else:
            self._saved_output_file = ""

        with self._lock:
            self._process = None
            self._stopping = False

        message = (
            f"Đã lưu {saved_file}" if saved_file else "Đã dừng phát trực tiếp."
        )
        self.encoder_stopped.emit(message)
        return saved_file
