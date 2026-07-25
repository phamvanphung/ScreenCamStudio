from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from screencam_studio.runtime_compat import inspect_runtime

IMPORT_CHECKS = {
    "PySide6": "PySide6",
    "NumPy": "numpy",
    "OpenCV": "cv2",
    "MSS": "mss",
    "PyAudioWPatch": "pyaudiowpatch",
    "DXcam": "dxcam",
}


def main() -> int:
    runtime = inspect_runtime(include_packages=True)
    imports: dict[str, dict[str, str]] = {}
    import_errors: list[str] = []

    for label, module_name in IMPORT_CHECKS.items():
        try:
            importlib.import_module(module_name)
            imports[label] = {"status": "PASS", "module": module_name}
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            imports[label] = {
                "status": "ERROR",
                "module": module_name,
                "error": message,
            }
            import_errors.append(f"{label}: {message}")

    report = runtime.to_dict()
    report["imports"] = imports
    report["status"] = "PASS" if runtime.supported and not import_errors else "ERROR"
    if import_errors:
        report["import_errors"] = import_errors

    output = Path(__file__).resolve().parent / "runtime_compatibility.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nCompatibility report: {output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
