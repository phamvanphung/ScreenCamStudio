@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".scs_venv_path.txt" (
  echo Run run.bat once before building the application.
  pause
  exit /b 1
)

set /p "VENV_DIR="<".scs_venv_path.txt"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo The saved virtual environment was not found:
  echo %VENV_DIR%
  echo Run run.bat again.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,11) and struct.calcsize('P')*8 == 64 else 1)"
if errorlevel 1 (
  echo The build environment must use 64-bit CPython 3.11 or newer.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -m pip install --upgrade pyinstaller pyinstaller-hooks-contrib
if errorlevel 1 goto :build_error

"%PYTHON_EXE%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name ScreenCamStudio ^
  --collect-all mss ^
  --collect-all pyaudiowpatch ^
  --collect-all dxcam ^
  main.py
if errorlevel 1 goto :build_error

echo.
echo Build completed: dist\ScreenCamStudio\ScreenCamStudio.exe
echo Put ffmpeg.exe and ffprobe.exe in:
echo dist\ScreenCamStudio\tools\
pause
exit /b 0

:build_error
echo.
echo Build failed. Review the output above for the missing module or hook.
pause
exit /b 1
