from __future__ import annotations

import math
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np

from .session_clock import SessionClock


SourceKind = Literal["desktop", "microphone"]


@dataclass(frozen=True)
class AudioSourceSpec:
    device_index: int
    name: str
    sample_rate: int
    channels: int
    volume_percent: int
    kind: SourceKind
    enhance_microphone: bool = False


@dataclass
class AudioMixerMetrics:
    desktop_underflow_blocks: int = 0
    microphone_underflow_blocks: int = 0
    desktop_peak_dbfs: float | None = None
    microphone_peak_dbfs: float | None = None
    output_frames: int = 0


class _SourceCapture:
    """PortAudio input source with a bounded queue and no blocking in callback."""

    def __init__(
        self,
        spec: AudioSourceSpec,
        on_log: Callable[[str], None] | None,
    ) -> None:
        self.spec = spec
        self.on_log = on_log
        self.sample_rate = int(spec.sample_rate or 48000)
        self.channels = max(1, min(int(spec.channels or 1), 2))
        self.frames_per_buffer = max(240, int(round(self.sample_rate * 0.01)))
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=400)
        self.audio = None
        self.stream = None
        self.pyaudio = None
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.overflow_count = 0

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)

    def _callback(self, in_data, frame_count, time_info, status_flags):
        del frame_count, time_info
        pyaudio = self.pyaudio
        if pyaudio is None:
            return (None, 1)
        if self.closed.is_set():
            return (None, pyaudio.paComplete)
        if status_flags:
            self.overflow_count += 1
        if in_data:
            try:
                self.queue.put_nowait(bytes(in_data))
            except queue.Full:
                # Audio must remain current. Drop the oldest packet instead of
                # growing latency or blocking PortAudio's real-time callback.
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except queue.Empty:
                    pass
                try:
                    self.queue.put_nowait(bytes(in_data))
                except queue.Full:
                    self.overflow_count += 1
        self.ready.set()
        return (None, pyaudio.paContinue)

    def open(self) -> None:
        import pyaudiowpatch as pyaudio

        self.pyaudio = pyaudio
        self.audio = pyaudio.PyAudio()
        device = self.audio.get_device_info_by_index(self.spec.device_index)
        max_channels = int(device.get("maxInputChannels", 0))
        if max_channels <= 0:
            raise RuntimeError(f"{self.spec.name}: thiết bị không còn là nguồn thu.")

        preferred_rate = int(round(float(device.get("defaultSampleRate", self.sample_rate))))
        rates: list[int] = []
        for rate in (preferred_rate, self.sample_rate, 48000, 44100):
            rate = int(rate)
            if 8000 <= rate <= 192000 and rate not in rates:
                rates.append(rate)

        preferred_channels = max(1, min(self.channels, max_channels, 2))
        channel_candidates: list[int] = []
        # Many Realtek/USB microphone endpoints advertise stereo but only deliver
        # valid samples when opened as mono. Desktop loopback remains stereo-first.
        if self.spec.kind == "microphone":
            for channels in (1, preferred_channels, min(max_channels, 2)):
                if 1 <= channels <= max_channels and channels not in channel_candidates:
                    channel_candidates.append(channels)
        else:
            for channels in (preferred_channels, min(max_channels, 2), 1):
                if 1 <= channels <= max_channels and channels not in channel_candidates:
                    channel_candidates.append(channels)

        last_error: Exception | None = None
        for channels in channel_candidates:
            for rate in rates:
                try:
                    frames = max(240, int(round(rate * 0.01)))
                    stream = self.audio.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        frames_per_buffer=frames,
                        input=True,
                        input_device_index=self.spec.device_index,
                        stream_callback=self._callback,
                        start=False,
                    )
                    self.channels = channels
                    self.sample_rate = rate
                    self.frames_per_buffer = frames
                    self.stream = stream
                    stream.start_stream()
                    self._log(
                        f"Đã mở {self.spec.kind}: {self.spec.name} "
                        f"({self.sample_rate} Hz, {self.channels} kênh)."
                    )
                    return
                except Exception as exc:
                    last_error = exc

        raise RuntimeError(f"Không mở được {self.spec.name}: {last_error}")

    def clear(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                return

    def close(self) -> None:
        self.closed.set()
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        audio = self.audio
        self.audio = None
        if audio is not None:
            try:
                audio.terminate()
            except Exception:
                pass
        self.pyaudio = None


class _PCMResampler:
    """Small stateful linear resampler from native PCM to 48 kHz stereo."""

    TARGET_RATE = 48000

    def __init__(self, source: _SourceCapture) -> None:
        self.source = source
        self.buffer = np.empty((0, 2), dtype=np.float32)
        self.position = 0.0
        self.underflow_blocks = 0
        self.peak = 0.0
        self.prev_x = np.zeros(2, dtype=np.float32)
        self.prev_y = np.zeros(2, dtype=np.float32)

    def reset(self) -> None:
        self.source.clear()
        self.buffer = np.empty((0, 2), dtype=np.float32)
        self.position = 0.0
        self.prev_x.fill(0)
        self.prev_y.fill(0)

    def _append_pcm(self, data: bytes) -> None:
        if not data:
            return
        raw = np.frombuffer(data, dtype=np.int16)
        channels = self.source.channels
        usable = (raw.size // channels) * channels
        if usable <= 0:
            return
        frames = raw[:usable].reshape(-1, channels).astype(np.float32)
        if channels == 1:
            frames = np.repeat(frames, 2, axis=1)
        elif channels > 2:
            frames = frames[:, :2]
        self.buffer = np.concatenate((self.buffer, frames), axis=0)

    def _ensure_frames(self, required_index: int, wait_seconds: float = 0.0) -> None:
        # Drain everything already produced by PortAudio without blocking the
        # session timeline. A missing source becomes silence; it must never make
        # a 10 ms output block take 80+ ms and shorten the final recording.
        while len(self.buffer) <= required_index:
            try:
                data = self.source.queue.get_nowait()
            except queue.Empty:
                break
            self._append_pcm(data)
            self.source.queue.task_done()

        if len(self.buffer) <= required_index and wait_seconds > 0:
            try:
                data = self.source.queue.get(timeout=min(wait_seconds, 0.004))
            except queue.Empty:
                return
            self._append_pcm(data)
            self.source.queue.task_done()
            while len(self.buffer) <= required_index:
                try:
                    data = self.source.queue.get_nowait()
                except queue.Empty:
                    break
                self._append_pcm(data)
                self.source.queue.task_done()

    def _enhance_microphone(self, block: np.ndarray) -> np.ndarray:
        # Lightweight DC/rumble blocker. It avoids a noise gate, so beginnings
        # and endings of words are not cut off.
        alpha = 0.985
        out = np.empty_like(block)
        prev_x = self.prev_x.copy()
        prev_y = self.prev_y.copy()
        for index in range(block.shape[0]):
            current = block[index]
            filtered = current - prev_x + alpha * prev_y
            out[index] = filtered
            prev_x = current
            prev_y = filtered
        self.prev_x = prev_x
        self.prev_y = prev_y

        normalized = out / 32768.0
        rms = float(np.sqrt(np.mean(np.square(normalized)))) if normalized.size else 0.0
        # Gentle automatic gain only when a real mic signal exists. Do not raise
        # the noise floor of a disconnected/silent endpoint.
        if rms > 0.0015:
            target_rms = 0.10
            gain = min(6.0, max(1.0, target_rms / rms))
            normalized *= gain
        magnitude = np.abs(normalized)
        threshold = 0.22
        ratio = 3.0
        above = magnitude > threshold
        compressed_mag = magnitude.copy()
        compressed_mag[above] = threshold + (magnitude[above] - threshold) / ratio
        normalized = np.sign(normalized) * compressed_mag
        return np.clip(normalized * 32768.0, -32768, 32767)

    def read(self, target_frames: int, wait_seconds: float = 0.0) -> np.ndarray:
        ratio = self.source.sample_rate / self.TARGET_RATE
        end_position = self.position + max(0, target_frames - 1) * ratio
        required_index = int(math.floor(end_position)) + 1
        self._ensure_frames(required_index, wait_seconds)

        if len(self.buffer) <= required_index:
            self.underflow_blocks += 1
            if len(self.buffer) == 0:
                self.buffer = np.zeros((required_index + 1, 2), dtype=np.float32)
            else:
                pad_count = required_index + 1 - len(self.buffer)
                # Use zero rather than repeating audio; repeating creates buzz.
                self.buffer = np.concatenate(
                    (self.buffer, np.zeros((pad_count, 2), dtype=np.float32)),
                    axis=0,
                )

        positions = self.position + np.arange(target_frames, dtype=np.float64) * ratio
        lower = np.floor(positions).astype(np.int64)
        fraction = (positions - lower).astype(np.float32)[:, None]
        upper = lower + 1
        output = self.buffer[lower] * (1.0 - fraction) + self.buffer[upper] * fraction

        next_position = self.position + target_frames * ratio
        consumed = int(math.floor(next_position))
        if consumed > 0:
            self.buffer = self.buffer[consumed:]
            next_position -= consumed
        self.position = next_position

        if self.source.spec.kind == "microphone" and self.source.spec.enhance_microphone:
            output = self._enhance_microphone(output)

        volume = max(0, min(self.source.spec.volume_percent, 200)) / 100.0
        output *= volume
        if output.size:
            self.peak = max(self.peak, float(np.max(np.abs(output))))
        return output


class AudioMixerBridge:
    """Capture and mix desktop + microphone against one shared session clock.

    Recording mode writes a 48 kHz stereo WAV sidecar. Streaming mode exposes a
    TCP PCM input for FFmpeg. Recording audio therefore never blocks the video
    pipe; both streams are joined only after capture has stopped.
    """

    TARGET_RATE = 48000
    TARGET_CHANNELS = 2
    BLOCK_FRAMES = 480  # 10 ms

    def __init__(
        self,
        sources: list[AudioSourceSpec],
        prebuffer_ms: int,
        audio_sync_offset_ms: int = 0,
        output_wave_path: str = "",
        on_log: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.sources = [_SourceCapture(spec, on_log) for spec in sources]
        self.resamplers = [_PCMResampler(source) for source in self.sources]
        self.prebuffer_seconds = max(0.08, min(prebuffer_ms / 1000.0, 0.5))
        self.audio_sync_offset_ms = max(-500, min(audio_sync_offset_ms, 500))
        self.output_wave_path = output_wave_path
        self.on_log = on_log
        self.on_error = on_error
        self.metrics = AudioMixerMetrics()

        self.clock: SessionClock | None = None
        self._thread: threading.Thread | None = None
        self._connection: socket.socket | None = None
        self._stop_event = threading.Event()
        self.connected = threading.Event()
        self._listener: socket.socket | None = None
        self.port = 0

        if not self.output_wave_path:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.25)
            self._listener = listener
            self.port = int(listener.getsockname()[1])

    @property
    def input_url(self) -> str:
        return f"tcp://127.0.0.1:{self.port}" if self.port else ""

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)

    def _error(self, message: str) -> None:
        if self.on_error is not None and not self._stop_event.is_set():
            self.on_error(message)

    def prepare(self) -> None:
        opened: list[_SourceCapture] = []
        try:
            for source in self.sources:
                source.open()
                opened.append(source)
        except Exception:
            for source in opened:
                source.close()
            raise

    def start(self, clock: SessionClock) -> None:
        self.clock = clock
        self._thread = threading.Thread(
            target=self._run,
            name="audio-mixer-bridge",
            daemon=True,
        )
        self._thread.start()

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self.connected.wait(timeout=max(0.1, timeout))

    def _accept_ffmpeg(self) -> socket.socket | None:
        listener = self._listener
        if listener is None:
            return None
        while not self._stop_event.is_set():
            try:
                connection, _ = listener.accept()
                connection.settimeout(None)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1048576)
                return connection
            except socket.timeout:
                continue
            except OSError:
                return None
        return None

    @staticmethod
    def _dbfs(peak: float) -> float | None:
        if peak <= 0:
            return None
        return round(20 * math.log10(min(peak / 32768.0, 1.0)), 2)

    def _mix_frames(self, frame_count: int, wait_seconds: float = 0.0) -> np.ndarray:
        if not self.resamplers:
            return np.zeros((frame_count, 2), dtype=np.float32)
        # One small shared wait budget. A silent microphone must not stall desktop
        # audio or delay the final WAV writer.
        per_source_wait = wait_seconds / max(1, len(self.resamplers))
        blocks = [
            resampler.read(frame_count, per_source_wait)
            for resampler in self.resamplers
        ]
        mixed = np.sum(blocks, axis=0)
        mixed = np.tanh(mixed / 30000.0) * 30000.0
        return np.clip(mixed, -32768, 32767)

    def _produce_audio(self, write_payload: Callable[[bytes], None]) -> None:
        clock = self.clock
        if clock is None:
            raise RuntimeError("Audio mixer không nhận được clock bắt đầu.")
        wait_deadline = time.perf_counter() + 10.0
        while not clock.start_event.is_set() and not self._stop_event.is_set():
            if time.perf_counter() >= wait_deadline:
                raise RuntimeError("Audio mixer không nhận được clock bắt đầu.")
            self._stop_event.wait(0.05)
        if self._stop_event.is_set():
            return
        start_time = clock.start_time
        if start_time is None:
            raise RuntimeError("Clock audio không hợp lệ.")

        while time.perf_counter() < start_time and not self._stop_event.is_set():
            time.sleep(min(0.005, start_time - time.perf_counter()))
        for resampler in self.resamplers:
            resampler.reset()

        sync_frames = int(round(self.audio_sync_offset_ms * self.TARGET_RATE / 1000))
        delayed_frames = max(0, sync_frames)
        advance_frames = max(0, -sync_frames)
        if advance_frames:
            frames_left = advance_frames
            while frames_left > 0:
                take = min(self.BLOCK_FRAMES, frames_left)
                for resampler in self.resamplers:
                    resampler.read(take, 0.0)
                frames_left -= take

        sent_frames = 0
        output_origin = start_time + self.prebuffer_seconds
        destination = "WAV sidecar" if self.output_wave_path else "FFmpeg TCP"
        self._log(
            f"Audio mixer -> {destination}: 48 kHz stereo, "
            f"jitter buffer {int(self.prebuffer_seconds * 1000)} ms."
        )

        while not self._stop_event.is_set():
            stop_time = clock.stop_time
            if stop_time is not None:
                target_frames_total = max(
                    0,
                    int(round((stop_time - start_time) * self.TARGET_RATE)),
                )
                if sent_frames >= target_frames_total:
                    break

            deadline_wall = output_origin + sent_frames / self.TARGET_RATE
            now = time.perf_counter()
            # While recording, preserve the shared real-time clock. Once stop_time
            # is known, finish the exact remaining sample count immediately; do
            # not let missing input packets make finalization time out.
            if stop_time is None and now < deadline_wall:
                self._stop_event.wait(min(0.01, deadline_wall - now))
                continue

            frames_this_block = self.BLOCK_FRAMES
            if stop_time is not None:
                remaining = target_frames_total - sent_frames
                frames_this_block = min(frames_this_block, remaining)
                if frames_this_block <= 0:
                    break

            if delayed_frames > 0:
                silence_frames = min(frames_this_block, delayed_frames)
                payload = np.zeros((silence_frames, 2), dtype=np.int16).tobytes()
                delayed_frames -= silence_frames
                if silence_frames < frames_this_block:
                    mix = self._mix_frames(
                        frames_this_block - silence_frames,
                        0.003 if stop_time is None else 0.0,
                    )
                    payload += mix.astype(np.int16).tobytes()
            else:
                mix = self._mix_frames(
                    frames_this_block,
                    0.003 if stop_time is None else 0.0,
                )
                payload = mix.astype(np.int16).tobytes()

            write_payload(payload)
            sent_frames += frames_this_block
            self.metrics.output_frames = sent_frames

    def _run(self) -> None:
        try:
            if self.output_wave_path:
                import wave
                from pathlib import Path

                path = Path(self.output_wave_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.unlink(missing_ok=True)
                self.connected.set()
                with wave.open(str(path), "wb") as wave_file:
                    wave_file.setnchannels(self.TARGET_CHANNELS)
                    wave_file.setsampwidth(2)
                    wave_file.setframerate(self.TARGET_RATE)
                    self._produce_audio(wave_file.writeframesraw)
            else:
                connection = self._accept_ffmpeg()
                if connection is None:
                    return
                self._connection = connection
                self.connected.set()
                self._produce_audio(connection.sendall)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Expected when a livestream destination or FFmpeg closes first.
            pass
        except Exception as exc:
            self._error(f"Lỗi audio mixer: {exc}")
        finally:
            for resampler in self.resamplers:
                if resampler.source.spec.kind == "desktop":
                    self.metrics.desktop_underflow_blocks += resampler.underflow_blocks
                    self.metrics.desktop_peak_dbfs = self._dbfs(resampler.peak)
                else:
                    self.metrics.microphone_underflow_blocks += resampler.underflow_blocks
                    self.metrics.microphone_peak_dbfs = self._dbfs(resampler.peak)
            self._close_resources()

    def _close_resources(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for source in self.sources:
            source.close()

    def stop(self, timeout: float = 8.0) -> None:
        clock = self.clock
        if clock is None or clock.stop_time is None:
            self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, timeout))
        if thread is not None and thread.is_alive():
            self._stop_event.set()
            connection = self._connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            thread.join(timeout=1.0)
        self._stop_event.set()
        self._close_resources()


def measure_input_device_peak(
    spec: AudioSourceSpec,
    duration_seconds: float = 3.0,
) -> tuple[float | None, int]:
    """Open one input endpoint and measure its raw peak without recording."""
    source = _SourceCapture(spec, None)
    source.open()
    peak = 0
    samples = 0
    deadline = time.perf_counter() + max(0.5, duration_seconds)
    try:
        while time.perf_counter() < deadline:
            try:
                payload = source.queue.get(timeout=0.10)
            except queue.Empty:
                continue
            raw = np.frombuffer(payload, dtype=np.int16)
            if raw.size:
                peak = max(peak, int(np.max(np.abs(raw.astype(np.int32)))))
                samples += int(raw.size // max(1, source.channels))
            source.queue.task_done()
    finally:
        source.close()
    if peak <= 0:
        return None, samples
    return round(20 * math.log10(min(peak / 32768.0, 1.0)), 2), samples
