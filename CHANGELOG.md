# Changelog

## v1.8.4 — Sharp Screen Final

- Thêm profile mặc định **Sắc nét nhất — giữ nguyên độ phân giải màn hình**.
- Thêm profile **MASTER chữ/UI — native H.264 4:4:4, CRF 14**.
- Thay upscale `INTER_LINEAR` bằng bicubic sắc nét.
- Thêm unsharp mask nhẹ chỉ cho desktop, không xử lý webcam.
- Hạ CRF/CQ cho tất cả profile chất lượng cao.
- Nâng NVENC lên `p6/HQ`, QSV lên `slow`, AMF lên `quality`.
- Recording dùng 2 B-frame; livestream vẫn low-latency.
- Thêm AQ mode 3 cho libx264 và metadata BT.709.
- Diagnostics ghi pixel format, codec profile, bitrate và mức sharpen.
- Self-test kiểm tra riêng output 4:2:0 và MASTER 4:4:4.
- Môi trường Python short-path chuyển sang `%LOCALAPPDATA%\SCS\v184`.

## v1.8.3 — A/V, microphone và streaming

- Không dùng audio ngắn để cắt video.
- Pad/trim audio theo timeline video.
- Thêm nhiều backend microphone và nút test mic.
- Thêm lựa chọn DXcam/MSS cho video trình duyệt.
- Khóa profile output đúng độ phân giải thực.

## v1.8.x

- Một clock chung cho capture, scheduler và audio.
- Tách video/audio khi recording và ghép ở bước finalize.
- Hỗ trợ CPython 3.11+, gồm Python 3.13.
- Short-path virtual environment để tránh lỗi PySide6/MAX_PATH.
