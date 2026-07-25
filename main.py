from __future__ import annotations

import os
import sys

from screencam_studio.runtime_compat import ensure_supported_runtime

# Fail early with a clear message before loading native GUI/media modules.
ensure_supported_runtime()

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
# Avoid slow or unstable hardware transforms when OpenCV opens webcams through MSMF.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from screencam_studio.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ScreenCam Studio")
    app.setOrganizationName("Local")

    # Some Windows/Qt combinations report an inherited point size of -1.
    # Set a real application font so Qt never calls setPointSize(-1).
    font = QFont("Segoe UI")
    font.setPointSize(10)
    app.setFont(font)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
