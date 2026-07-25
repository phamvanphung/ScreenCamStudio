from __future__ import annotations

import importlib.metadata
import os
import platform
import struct
import sys
from dataclasses import asdict, dataclass
from typing import Any

MIN_PYTHON = (3, 11)
# These are the versions currently covered by published Windows wheels for the
# complete dependency set. The launcher does not block newer Python versions;
# it reports a warning so future dependency releases can work without changing
# application source code.
CURRENTLY_VERIFIED_MAX = (3, 14)

PACKAGE_DISTRIBUTIONS = {
    "PySide6": "PySide6",
    "NumPy": "numpy",
    "OpenCV": "opencv-python",
    "MSS": "mss",
    "PyAudioWPatch": "PyAudioWPatch",
    "DXcam": "dxcam",
}


@dataclass(frozen=True)
class RuntimeCompatibility:
    python_version: str
    executable: str
    architecture_bits: int
    implementation: str
    operating_system: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    packages: dict[str, str]

    @property
    def supported(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["supported"] = self.supported
        return data


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for label, distribution in PACKAGE_DISTRIBUTIONS.items():
        try:
            versions[label] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[label] = "not installed"
    return versions


def inspect_runtime(*, include_packages: bool = True) -> RuntimeCompatibility:
    errors: list[str] = []
    warnings: list[str] = []
    version = sys.version_info[:2]
    architecture_bits = struct.calcsize("P") * 8

    if version < MIN_PYTHON:
        errors.append(
            "ScreenCam Studio requires CPython 3.11 or newer; "
            f"detected {platform.python_version()}."
        )

    if platform.python_implementation() != "CPython":
        errors.append(
            "Only CPython is supported because camera/audio dependencies use "
            "native Windows wheels."
        )

    if os.name != "nt":
        errors.append("ScreenCam Studio is a Windows application.")

    if architecture_bits != 64:
        errors.append(
            "A 64-bit Python installation is required for DXcam, OpenCV and "
            "WASAPI audio dependencies."
        )

    if version > CURRENTLY_VERIFIED_MAX:
        warnings.append(
            "This Python version is newer than the dependency set verified for "
            "this release. The application does not block it, but installation "
            "depends on compatible wheels being available from PyPI."
        )

    return RuntimeCompatibility(
        python_version=platform.python_version(),
        executable=sys.executable,
        architecture_bits=architecture_bits,
        implementation=platform.python_implementation(),
        operating_system=platform.platform(),
        errors=tuple(errors),
        warnings=tuple(warnings),
        packages=_package_versions() if include_packages else {},
    )


def ensure_supported_runtime() -> RuntimeCompatibility:
    report = inspect_runtime(include_packages=False)
    if report.errors:
        raise RuntimeError("\n".join(report.errors))
    return report
