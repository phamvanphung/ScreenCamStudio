from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


def safe_call(name: str, function: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = function()
        return {"status": "PASS", "value": value}
    except Exception as exc:
        return {"status": "ERROR", "error": str(exc), "component": name}


def run_ffmpeg_synthetic(ffmpeg_path: str, probe_output) -> dict[str, Any]:
    output = Path(tempfile.gettempdir()) / "screencam_studio_self_test.mp4"
    output.unlink(missing_ok=True)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000:duration=3",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "cfr",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(output),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=creationflags,
        check=False,
    )
    if completed.returncode != 0 or not output.exists():
        return {
            "status": "ERROR",
            "return_code": completed.returncode,
            "error": completed.stderr.strip(),
        }
    return {
        "status": "PASS",
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "probe": probe_output(ffmpeg_path, str(output)),
    }



def run_short_audio_padding_test(ffmpeg_path: str, probe_output) -> dict[str, Any]:
    temp = Path(tempfile.gettempdir())
    video = temp / "scs_padding_video.mp4"
    audio = temp / "scs_padding_audio.wav"
    output = temp / "scs_padding_result.mp4"
    for path in (video, audio, output):
        path.unlink(missing_ok=True)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    create_video = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-t", "3.0", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-an", str(video),
    ]
    create_audio = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1.5", "-ac", "2", "-c:a", "pcm_s16le", str(audio),
    ]
    mux = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-i", str(audio),
        "-filter_complex",
        "[1:a]aresample=48000:async=1:first_pts=0,apad,atrim=start=0:end=3.000000,asetpts=N/SR/TB[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-t", "3.000000",
        "-movflags", "+faststart", str(output),
    ]
    for command in (create_video, create_audio, mux):
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, creationflags=creationflags, check=False,
        )
        if completed.returncode != 0:
            return {"status": "ERROR", "error": completed.stderr.strip()}
    probe = probe_output(ffmpeg_path, str(output))
    try:
        duration = float(probe.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "status": "PASS" if abs(duration - 3.0) <= 0.08 else "ERROR",
        "duration": duration,
        "probe": probe,
    }


def run_screen_quality_test(ffmpeg_path: str, probe_output) -> dict[str, Any]:
    temp = Path(tempfile.gettempdir())
    outputs = {
        "youtube_sharp_420": (temp / "scs_sharp_420.mp4", "yuv420p", "high", "17"),
        "master_ui_444": (temp / "scs_master_444.mp4", "yuv444p", "high444", "14"),
    }
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    results: dict[str, Any] = {}
    overall = "PASS"
    for name, (output, pix_fmt, profile, crf) in outputs.items():
        output.unlink(missing_ok=True)
        command = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            "testsrc2=size=1280x720:rate=30:duration=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", crf,
            "-profile:v", profile, "-pix_fmt", pix_fmt,
            "-sws_flags", "lanczos+accurate_rnd+full_chroma_int",
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-an", str(output),
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, creationflags=creationflags, check=False,
        )
        probe = probe_output(ffmpeg_path, str(output)) if output.exists() else {}
        stream = next(
            (item for item in probe.get("streams", []) if item.get("codec_type") == "video"),
            {},
        )
        actual_pix_fmt = str(stream.get("pix_fmt") or "")
        status = (
            "PASS"
            if completed.returncode == 0 and actual_pix_fmt == pix_fmt
            else "ERROR"
        )
        if status == "ERROR":
            overall = "ERROR"
        results[name] = {
            "status": status,
            "expected_pixel_format": pix_fmt,
            "actual_pixel_format": actual_pix_fmt,
            "size_bytes": output.stat().st_size if output.exists() else 0,
            "error": completed.stderr.strip(),
            "probe": probe,
        }
    return {"status": overall, "profiles": results}

def main() -> int:
    report: dict[str, Any] = {
        "application": "ScreenCam Studio v1.8.4",
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "architecture": platform.architecture()[0],
        },
        "platform": platform.platform(),
    }
    report["python"]["status"] = (
        "PASS" if sys.version_info >= (3, 11) else "ERROR"
    )

    try:
        from screencam_studio.runtime_compat import inspect_runtime

        runtime_report = inspect_runtime(include_packages=True)
        report["runtime_compatibility"] = runtime_report.to_dict()
    except Exception as exc:
        report["runtime_compatibility"] = {
            "status": "ERROR",
            "error": str(exc),
        }

    try:
        from screencam_studio.devices import (
            find_ffmpeg,
            list_cameras,
            list_monitors,
            list_wasapi_loopback_devices,
            list_wasapi_microphones,
        )
        from screencam_studio.diagnostics import find_ffprobe, probe_output
        from screencam_studio.encoder import detect_best_h264_encoder
    except Exception as exc:
        report["dependencies"] = {
            "status": "ERROR",
            "error": str(exc),
            "action": "Chạy run.bat để cài requirements vào .venv.",
        }
        output_path = Path(__file__).resolve().parent / "self_test_report.json"
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nReport: {output_path}")
        return 1

    report["dependencies"] = {"status": "PASS"}
    ffmpeg_path = find_ffmpeg()
    report["ffmpeg"] = {
        "status": "PASS" if ffmpeg_path else "ERROR",
        "path": ffmpeg_path,
    }
    if ffmpeg_path:
        ffprobe_path = find_ffprobe(ffmpeg_path)
        report["ffprobe"] = {
            "status": "PASS" if ffprobe_path else "ERROR",
            "path": ffprobe_path,
        }
        report["hardware_encoder"] = safe_call(
            "hardware_encoder", lambda: detect_best_h264_encoder(ffmpeg_path)
        )
        report["synthetic_output"] = run_ffmpeg_synthetic(
            ffmpeg_path, probe_output
        )
        report["short_audio_padding"] = run_short_audio_padding_test(
            ffmpeg_path, probe_output
        )
        report["screen_quality_profiles"] = run_screen_quality_test(
            ffmpeg_path, probe_output
        )

    report["monitors"] = safe_call(
        "monitors", lambda: [item.label for item in list_monitors()]
    )
    report["cameras"] = safe_call("cameras", list_cameras)

    if os.name == "nt":
        playback, playback_error = list_wasapi_loopback_devices()
        microphones, microphone_error = list_wasapi_microphones()
        report["playback_loopback"] = {
            "status": "PASS" if playback else "WARNING",
            "devices": [item.label for item in playback],
            "message": playback_error,
        }
        report["microphones"] = {
            "status": "PASS" if microphones else "WARNING",
            "devices": [item.label for item in microphones],
            "message": microphone_error,
        }
    else:
        report["playback_loopback"] = {
            "status": "SKIPPED",
            "message": "WASAPI chỉ được kiểm tra trên Windows.",
        }
        report["microphones"] = {
            "status": "SKIPPED",
            "message": "WASAPI chỉ được kiểm tra trên Windows.",
        }

    output_path = Path(__file__).resolve().parent / "self_test_report.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport: {output_path}")

    critical_errors = [
        key
        for key in (
            "dependencies", "ffmpeg", "ffprobe", "synthetic_output",
            "short_audio_padding", "screen_quality_profiles",
        )
        if report.get(key, {}).get("status") == "ERROR"
    ]
    return 1 if critical_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
