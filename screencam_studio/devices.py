from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import mss


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int

    @property
    def label(self) -> str:
        return (
            f"Màn hình {self.index}: {self.width}×{self.height} "
            f"(x={self.left}, y={self.top})"
        )


@dataclass(frozen=True)
class LoopbackDeviceInfo:
    """Windows Playback endpoint exposed as a WASAPI loopback input."""

    index: int
    name: str
    channels: int
    sample_rate: int
    is_default: bool = False

    @property
    def label(self) -> str:
        default_text = " — Mặc định" if self.is_default else ""
        clean_name = self.name.replace(" [Loopback]", "").strip()
        return f"{clean_name}{default_text}"


@dataclass(frozen=True)
class MicrophoneDeviceInfo:
    """Microphone/input endpoint exposed by a PortAudio host API."""

    index: int
    name: str
    channels: int
    sample_rate: int
    host_api_name: str = ""
    is_default: bool = False

    @property
    def label(self) -> str:
        default_text = " — Mặc định" if self.is_default else ""
        host_text = f" [{self.host_api_name}]" if self.host_api_name else ""
        return f"{self.name.strip()}{host_text}{default_text}"


def list_monitors() -> list[MonitorInfo]:
    with mss.mss() as sct:
        monitors: list[MonitorInfo] = []
        # sct.monitors[0] is the virtual desktop containing all displays.
        for index, mon in enumerate(sct.monitors[1:], start=1):
            monitors.append(
                MonitorInfo(
                    index=index,
                    left=int(mon["left"]),
                    top=int(mon["top"]),
                    width=int(mon["width"]),
                    height=int(mon["height"]),
                )
            )
        return monitors


def open_camera_capture(index: int) -> tuple[cv2.VideoCapture | None, str]:
    """Open a webcam using Windows Media Foundation first.

    OpenCV builds on newer Python versions can expose DirectShow but still reject
    capture-by-index. MSMF is the modern Windows backend; CAP_ANY remains a safe
    fallback for machines whose camera driver selects another working backend.
    """
    backends: list[tuple[int, str]]
    if os.name == "nt":
        backends = [
            (cv2.CAP_MSMF, "MSMF"),
            (cv2.CAP_ANY, "Auto"),
        ]
    else:
        backends = [(cv2.CAP_ANY, "Auto")]

    for backend, backend_name in backends:
        capture = cv2.VideoCapture(index, backend)
        if capture.isOpened():
            return capture, backend_name
        capture.release()
    return None, ""


def list_cameras(max_index: int = 6) -> list[tuple[int, str]]:
    cameras: list[tuple[int, str]] = []
    consecutive_failures = 0

    for index in range(max_index):
        capture, backend_name = open_camera_capture(index)
        if capture is None:
            consecutive_failures += 1
            # Most Windows systems enumerate cameras from index 0 without gaps.
            # Stop early after several misses to avoid a long device scan.
            if consecutive_failures >= 4 and cameras:
                break
            continue

        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = capture.read()
            if ok and frame is not None:
                cameras.append((index, f"Camera {index} ({backend_name})"))
                consecutive_failures = 0
            else:
                consecutive_failures += 1
        finally:
            capture.release()
    return cameras


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def find_ffmpeg() -> str:
    candidates = [
        app_base_dir() / "ffmpeg.exe",
        app_base_dir() / "tools" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which("ffmpeg")
    return found or ""


def _safe_channels(value: object, *, microphone: bool) -> int:
    try:
        maximum = int(value)
    except (TypeError, ValueError):
        maximum = 1

    if microphone:
        # Most microphones are mono. Preserve stereo only when the endpoint
        # actually exposes it, but never open large multichannel arrays here.
        return max(1, min(maximum, 2))
    return max(1, min(maximum, 2))


def _safe_sample_rate(value: object) -> int:
    try:
        rate = int(round(float(value)))
    except (TypeError, ValueError):
        return 48000
    return rate if 8000 <= rate <= 192000 else 48000


def list_wasapi_loopback_devices() -> tuple[list[LoopbackDeviceInfo], str]:
    """Return Playback endpoints through PyAudioWPatch loopback devices."""
    if os.name != "nt":
        return [], "WASAPI Loopback chỉ khả dụng trên Windows."

    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return (
            [],
            "Thiếu PyAudioWPatch. Hãy chạy lại run.bat để cài thư viện mới.",
        )

    try:
        with pyaudio.PyAudio() as audio:
            default_index: int | None = None
            try:
                default_loopback = audio.get_default_wasapi_loopback()
                default_index = int(default_loopback["index"])
            except (OSError, KeyError, TypeError, ValueError):
                default_index = None

            devices: list[LoopbackDeviceInfo] = []
            seen_indexes: set[int] = set()
            for raw in audio.get_loopback_device_info_generator():
                try:
                    index = int(raw["index"])
                    name = str(raw["name"]).strip()
                except (KeyError, TypeError, ValueError):
                    continue

                channels = _safe_channels(raw.get("maxInputChannels", 0), microphone=False)
                sample_rate = _safe_sample_rate(raw.get("defaultSampleRate", 48000))
                if index in seen_indexes or not name:
                    continue

                seen_indexes.add(index)
                devices.append(
                    LoopbackDeviceInfo(
                        index=index,
                        name=name,
                        channels=channels,
                        sample_rate=sample_rate,
                        is_default=index == default_index,
                    )
                )

            devices.sort(key=lambda item: (not item.is_default, item.name.casefold()))
            if not devices:
                return [], "Windows không trả về thiết bị Playback hỗ trợ WASAPI Loopback."
            return devices, ""
    except OSError as exc:
        return [], f"Không khởi tạo được WASAPI: {exc}"
    except Exception as exc:
        return [], f"Không quét được Playback devices: {exc}"


def list_wasapi_microphones() -> tuple[list[MicrophoneDeviceInfo], str]:
    """Return microphone inputs from every PortAudio host API.

    Some Realtek/USB drivers expose a WASAPI endpoint that opens successfully but
    returns silence. Listing WASAPI, WDM-KS, DirectSound and MME separately lets
    the user select the backend that actually carries microphone samples.
    """
    if os.name != "nt":
        return [], "Microphone input chỉ khả dụng trên Windows trong bản này."

    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return (
            [],
            "Thiếu PyAudioWPatch. Hãy chạy lại run.bat để cài thư viện mới.",
        )

    try:
        with pyaudio.PyAudio() as audio:
            default_index: int | None = None
            try:
                default_index = int(audio.get_default_input_device_info()["index"])
            except (OSError, KeyError, TypeError, ValueError):
                default_index = None

            devices: list[MicrophoneDeviceInfo] = []
            seen: set[tuple[str, str, int, int]] = set()
            host_priority = {
                "windows wasapi": 0,
                "windows wdm-ks": 1,
                "windows directsound": 2,
                "mme": 3,
            }

            for index in range(audio.get_device_count()):
                try:
                    raw = audio.get_device_info_by_index(index)
                    if bool(raw.get("isLoopbackDevice", False)):
                        continue
                    max_input_channels = int(raw.get("maxInputChannels", 0))
                    if max_input_channels <= 0:
                        continue
                    name = str(raw.get("name", "")).strip()
                    if not name:
                        continue
                    host_index = int(raw.get("hostApi", -1))
                    host_raw = audio.get_host_api_info_by_index(host_index)
                    host_name = str(host_raw.get("name", "PortAudio")).strip()
                    sample_rate = _safe_sample_rate(raw.get("defaultSampleRate", 48000))
                    channels = _safe_channels(max_input_channels, microphone=True)
                except (OSError, KeyError, TypeError, ValueError):
                    continue

                dedupe_key = (
                    name.casefold(),
                    host_name.casefold(),
                    channels,
                    sample_rate,
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                devices.append(
                    MicrophoneDeviceInfo(
                        index=int(raw["index"]),
                        name=name,
                        channels=channels,
                        sample_rate=sample_rate,
                        host_api_name=host_name,
                        is_default=int(raw["index"]) == default_index,
                    )
                )

            devices.sort(
                key=lambda item: (
                    not item.is_default,
                    host_priority.get(item.host_api_name.casefold(), 9),
                    item.name.casefold(),
                )
            )
            if not devices:
                return [], "Không tìm thấy microphone/thiết bị đầu vào PortAudio."
            return devices, ""
    except OSError as exc:
        return [], f"Không khởi tạo được microphone: {exc}"
    except Exception as exc:
        return [], f"Không quét được microphone: {exc}"
