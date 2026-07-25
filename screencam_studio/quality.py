from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityProfile:
    key: str
    label: str
    width: int
    height: int
    fps: int
    recording_crf: int
    stream_bitrate_kbps: int
    max_recording_bitrate_kbps: int
    software_preset: str
    audio_bitrate_kbps: int
    preview_fps: int
    preview_max_width: int
    prebuffer_ms: int
    requires_hardware: bool
    description: str
    recording_pix_fmt: str = "yuv420p"
    force_software_recording: bool = False
    screen_sharpen_strength: float = 0.0
    upscale_filter: str = "cubic"

    @property
    def uses_native_resolution(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @property
    def is_master_444(self) -> bool:
        return self.recording_pix_fmt == "yuv444p"


QUALITY_PROFILES: tuple[QualityProfile, ...] = (
    QualityProfile(
        key="stable_720p30",
        label="Ổn định — 720p / 30 FPS",
        width=1280,
        height=720,
        fps=30,
        recording_crf=20,
        stream_bitrate_kbps=5000,
        max_recording_bitrate_kbps=9000,
        software_preset="veryfast",
        audio_bitrate_kbps=192,
        preview_fps=8,
        preview_max_width=900,
        prebuffer_ms=180,
        requires_hardware=False,
        description=(
            "Ưu tiên ổn định cho laptop/PC phổ thông. Phù hợp khi nguồn màn hình "
            "không lớn hơn 1280×720."
        ),
        screen_sharpen_strength=0.08,
    ),
    QualityProfile(
        key="youtube_1080p30",
        label="Chuẩn YouTube sắc nét — 1080p / 30 FPS",
        width=1920,
        height=1080,
        fps=30,
        recording_crf=17,
        stream_bitrate_kbps=9000,
        max_recording_bitrate_kbps=22000,
        software_preset="fast",
        audio_bitrate_kbps=256,
        preview_fps=8,
        preview_max_width=960,
        prebuffer_ms=200,
        requires_hardware=False,
        description=(
            "Tối ưu chữ, trình duyệt và video YouTube ở Full HD. Dùng bitrate và "
            "mức chất lượng cao hơn bản 1.8.3."
        ),
        screen_sharpen_strength=0.14,
    ),
    QualityProfile(
        key="native_sharp_30",
        label="Sắc nét nhất — giữ nguyên độ phân giải màn hình / 30 FPS",
        width=0,
        height=0,
        fps=30,
        recording_crf=16,
        stream_bitrate_kbps=12000,
        max_recording_bitrate_kbps=32000,
        software_preset="fast",
        audio_bitrate_kbps=256,
        preview_fps=8,
        preview_max_width=960,
        prebuffer_ms=200,
        requires_hardware=False,
        description=(
            "Khuyên dùng cho quay màn hình: không phóng to hoặc thu nhỏ nguồn, nên "
            "chữ và chi tiết UI giữ nét tốt nhất."
        ),
        screen_sharpen_strength=0.10,
    ),
    QualityProfile(
        key="smooth_1080p60",
        label="Chuyển động mượt — 1080p / 60 FPS",
        width=1920,
        height=1080,
        fps=60,
        recording_crf=17,
        stream_bitrate_kbps=12000,
        max_recording_bitrate_kbps=26000,
        software_preset="veryfast",
        audio_bitrate_kbps=256,
        preview_fps=10,
        preview_max_width=960,
        prebuffer_ms=240,
        requires_hardware=True,
        description=(
            "Dành cho cuộn trang, thao tác nhanh hoặc video chuyển động; nên có "
            "NVENC, Quick Sync hoặc AMF."
        ),
        screen_sharpen_strength=0.12,
    ),
    QualityProfile(
        key="high_1440p30",
        label="YouTube 2K sắc nét — 1440p / 30 FPS",
        width=2560,
        height=1440,
        fps=30,
        recording_crf=16,
        stream_bitrate_kbps=18000,
        max_recording_bitrate_kbps=40000,
        software_preset="fast",
        audio_bitrate_kbps=256,
        preview_fps=7,
        preview_max_width=960,
        prebuffer_ms=240,
        requires_hardware=True,
        description=(
            "Dùng bicubic sắc nét và sharpen nhẹ khi cần scale lên 1440p. Nếu màn hình là "
            "1080p, profile giữ nguyên độ phân giải thường nét hơn file local."
        ),
        screen_sharpen_strength=0.20,
    ),
    QualityProfile(
        key="master_native_444",
        label="MASTER chữ/UI — độ phân giải gốc, 4:4:4 / 30 FPS",
        width=0,
        height=0,
        fps=30,
        recording_crf=14,
        stream_bitrate_kbps=16000,
        max_recording_bitrate_kbps=80000,
        software_preset="medium",
        audio_bitrate_kbps=320,
        preview_fps=7,
        preview_max_width=960,
        prebuffer_ms=220,
        requires_hardware=False,
        description=(
            "Chất lượng local cao nhất cho chữ nhỏ và giao diện: H.264 4:4:4, "
            "CRF 14, CPU encoder. File lớn hơn và không dùng cho livestream."
        ),
        recording_pix_fmt="yuv444p",
        force_software_recording=True,
        screen_sharpen_strength=0.08,
    ),
)


def get_quality_profile(key: str) -> QualityProfile:
    for profile in QUALITY_PROFILES:
        if profile.key == key:
            return profile
    return next(profile for profile in QUALITY_PROFILES if profile.key == "native_sharp_30")
