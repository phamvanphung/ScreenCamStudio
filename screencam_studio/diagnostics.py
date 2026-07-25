from __future__ import annotations

import json
import shutil
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionDiagnostics:
    profile: str = ""
    capture_backend: str = ""
    video_encoder: str = ""
    target_fps: int = 0
    requested_width: int = 0
    requested_height: int = 0
    expected_pixel_format: str = "yuv420p"
    screen_sharpen_strength: float = 0.0
    desktop_audio_enabled: bool = False
    microphone_enabled: bool = False
    desktop_audio_device: str = ""
    microphone_device: str = ""
    wall_duration_seconds: float = 0.0
    capture_frames: int = 0
    submitted_frames: int = 0
    encoded_frames: int = 0
    duplicated_frames: int = 0
    capture_intervals_ms: list[float] = field(default_factory=list)
    encoder_write_ms: list[float] = field(default_factory=list)
    desktop_underflow_blocks: int = 0
    microphone_underflow_blocks: int = 0
    desktop_peak_dbfs: float | None = None
    microphone_peak_dbfs: float | None = None
    audio_output_frames: int = 0
    output_probe: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
        return round(ordered[index], 3)

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data["capture_interval_p50_ms"] = self._percentile(
            self.capture_intervals_ms, 0.50
        )
        data["capture_interval_p95_ms"] = self._percentile(
            self.capture_intervals_ms, 0.95
        )
        data["capture_interval_max_ms"] = (
            round(max(self.capture_intervals_ms), 3)
            if self.capture_intervals_ms
            else None
        )
        data["encoder_write_p95_ms"] = self._percentile(
            self.encoder_write_ms, 0.95
        )
        data["encoder_write_max_ms"] = (
            round(max(self.encoder_write_ms), 3)
            if self.encoder_write_ms
            else None
        )
        data["actual_capture_fps"] = (
            round(self.capture_frames / self.wall_duration_seconds, 3)
            if self.wall_duration_seconds > 0
            else 0.0
        )
        # Keep the report compact; raw samples are not useful to the end user.
        data.pop("capture_intervals_ms", None)
        data.pop("encoder_write_ms", None)
        return data


def find_ffprobe(ffmpeg_path: str) -> str:
    candidate = Path(ffmpeg_path).with_name(
        "ffprobe.exe" if Path(ffmpeg_path).suffix.lower() == ".exe" else "ffprobe"
    )
    if candidate.exists():
        return str(candidate)
    return shutil.which("ffprobe") or ""


def probe_output(ffmpeg_path: str, output_file: str) -> dict[str, Any]:
    ffprobe = find_ffprobe(ffmpeg_path)
    if not ffprobe or not Path(output_file).exists():
        return {}

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,bit_rate:stream=index,codec_type,codec_name,profile,pix_fmt,color_space,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels,bit_rate,duration",
        "-of",
        "json",
        output_file,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            return {"error": completed.stderr.strip()}
        return json.loads(completed.stdout or "{}")
    except Exception as exc:
        return {"error": str(exc)}


def write_diagnostics(
    diagnostics: SessionDiagnostics,
    ffmpeg_path: str,
    output_file: str,
) -> str:
    diagnostics.output_probe = probe_output(ffmpeg_path, output_file)

    streams = diagnostics.output_probe.get("streams", [])
    video_duration = None
    audio_duration = None
    for stream in streams:
        try:
            duration = float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
        if stream.get("codec_type") == "video":
            video_duration = duration
        elif stream.get("codec_type") == "audio":
            audio_duration = duration

    if video_duration is not None and audio_duration is not None:
        drift_ms = round((audio_duration - video_duration) * 1000, 2)
        diagnostics.output_probe["audio_minus_video_ms"] = drift_ms
        if abs(drift_ms) > 80:
            diagnostics.warnings.append(
                f"Độ lệch thời lượng audio/video {drift_ms:+.0f} ms vượt ngưỡng 80 ms."
            )

    if diagnostics.audio_output_frames:
        diagnostics.output_probe["audio_writer_duration_seconds"] = round(
            diagnostics.audio_output_frames / 48000.0, 6
        )

    if diagnostics.target_fps and diagnostics.capture_intervals_ms:
        frame_ms = 1000 / diagnostics.target_fps
        p95 = diagnostics._percentile(diagnostics.capture_intervals_ms, 0.95)
        if p95 is not None and p95 > frame_ms * 1.8:
            diagnostics.warnings.append(
                "Capture p95 chậm hơn 1,8 lần chu kỳ frame; nên giảm profile."
            )
        write_p95 = diagnostics._percentile(diagnostics.encoder_write_ms, 0.95)
        if write_p95 is not None and write_p95 > frame_ms * 0.9:
            diagnostics.warnings.append(
                "Encoder write p95 gần/vượt chu kỳ frame; output có nguy cơ giật."
            )

    if diagnostics.encoded_frames:
        duplicate_ratio = diagnostics.duplicated_frames / diagnostics.encoded_frames
        diagnostics.output_probe["duplicated_frame_ratio"] = round(duplicate_ratio, 4)
        if duplicate_ratio > 0.08:
            diagnostics.warnings.append(
                f"Frame lặp {duplicate_ratio * 100:.1f}% vượt ngưỡng 8%."
            )

    if diagnostics.desktop_audio_enabled:
        if diagnostics.desktop_peak_dbfs is None or diagnostics.desktop_peak_dbfs < -60:
            diagnostics.warnings.append(
                "Đã bật âm thanh máy nhưng không phát hiện tín hiệu rõ."
            )
        if diagnostics.desktop_underflow_blocks > 5:
            diagnostics.warnings.append(
                f"Âm thanh máy thiếu {diagnostics.desktop_underflow_blocks} block 10 ms."
            )

    if diagnostics.microphone_enabled:
        if diagnostics.microphone_peak_dbfs is None or diagnostics.microphone_peak_dbfs < -60:
            diagnostics.warnings.append(
                "Đã bật microphone nhưng không phát hiện giọng/tín hiệu rõ."
            )
        if diagnostics.microphone_underflow_blocks > 5:
            diagnostics.warnings.append(
                f"Microphone thiếu {diagnostics.microphone_underflow_blocks} block 10 ms."
            )

    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        diagnostics.warnings.append("Output không có video stream.")
    else:
        if diagnostics.requested_width and int(video_stream.get("width", 0) or 0) != diagnostics.requested_width:
            diagnostics.warnings.append("Độ rộng output không đúng profile đã chọn.")
        if diagnostics.requested_height and int(video_stream.get("height", 0) or 0) != diagnostics.requested_height:
            diagnostics.warnings.append("Độ cao output không đúng profile đã chọn.")
        actual_pixel_format = str(video_stream.get("pix_fmt") or "")
        diagnostics.output_probe["expected_pixel_format"] = diagnostics.expected_pixel_format
        if (
            diagnostics.expected_pixel_format
            and actual_pixel_format
            and actual_pixel_format != diagnostics.expected_pixel_format
        ):
            diagnostics.warnings.append(
                f"Pixel format output {actual_pixel_format} không đúng "
                f"{diagnostics.expected_pixel_format}."
            )

        rate_text = str(video_stream.get("avg_frame_rate") or "0/0")
        try:
            numerator_text, denominator_text = rate_text.split("/", 1)
            denominator = float(denominator_text)
            output_fps = float(numerator_text) / denominator if denominator else 0.0
        except (ValueError, ZeroDivisionError):
            output_fps = 0.0
        diagnostics.output_probe["output_fps"] = round(output_fps, 3)
        if diagnostics.target_fps and abs(output_fps - diagnostics.target_fps) > 0.1:
            diagnostics.warnings.append(
                f"FPS output {output_fps:.3f} không đúng mục tiêu {diagnostics.target_fps} FPS."
            )

    format_data = diagnostics.output_probe.get("format", {})
    try:
        output_duration = float(format_data.get("duration"))
    except (TypeError, ValueError):
        output_duration = 0.0
    if output_duration > 0 and diagnostics.wall_duration_seconds > 0:
        duration_error_ms = round(
            (output_duration - diagnostics.wall_duration_seconds) * 1000, 2
        )
        diagnostics.output_probe["output_minus_wall_ms"] = duration_error_ms
        allowed_ms = max(100.0, diagnostics.wall_duration_seconds * 1000 * 0.03)
        if abs(duration_error_ms) > allowed_ms:
            diagnostics.warnings.append(
                f"Timeline output lệch thời gian quay {duration_error_ms:+.0f} ms."
            )

    diagnostics.output_probe["quality_result"] = (
        "PASS" if not diagnostics.warnings else "WARNING"
    )

    report_path = str(Path(output_file).with_suffix(".diagnostics.json"))
    Path(report_path).write_text(
        json.dumps(diagnostics.summary(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path
