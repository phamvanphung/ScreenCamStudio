@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ScreenCam Studio supports standard 64-bit CPython 3.11 or newer.
rem Optional usage: run.bat 3.13
rem The virtual environment is intentionally stored in a short LocalAppData path
rem to avoid the Windows MAX_PATH problem while installing PySide6 QML files.

set "PY_CMD="
set "REQUESTED_VERSION=%~1"

if defined REQUESTED_VERSION (
  where py >nul 2>nul
  if errorlevel 1 (
    echo The Python Launcher is required when selecting an exact version.
    echo Example: run.bat 3.13
    pause
    exit /b 1
  )

  py -%REQUESTED_VERSION% -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,11) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Python %REQUESTED_VERSION% 64-bit was not found or is older than 3.11.
    pause
    exit /b 1
  )
  set "PY_CMD=py -%REQUESTED_VERSION%"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,11) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
  )

  if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
      python -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,11) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
      if not errorlevel 1 set "PY_CMD=python"
    )
  )
)

if not defined PY_CMD (
  echo ScreenCam Studio requires standard 64-bit CPython 3.11 or newer.
  echo Install Python from python.org and enable the Python Launcher or PATH.
  pause
  exit /b 1
)

set "PY_TAG="
for /f "delims=" %%V in ('%PY_CMD% -c "import sys;print(f'{sys.version_info.major}{sys.version_info.minor}')"') do set "PY_TAG=%%V"
if not defined PY_TAG (
  echo Could not determine the selected Python version.
  pause
  exit /b 1
)

set "SCS_HOME=%LOCALAPPDATA%\SCS"
set "VENV_DIR=%SCS_HOME%\v184\py%PY_TAG%"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PIP_CACHE_DIR=%SCS_HOME%\pip-cache"
set "TEMP=%SCS_HOME%\tmp"
set "TMP=%TEMP%"

if not exist "%SCS_HOME%" mkdir "%SCS_HOME%"
if not exist "%PIP_CACHE_DIR%" mkdir "%PIP_CACHE_DIR%"
if not exist "%TEMP%" mkdir "%TEMP%"

if exist "%PYTHON_EXE%" (
  "%PYTHON_EXE%" -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,11) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Existing short-path environment is invalid. Recreating it...
    rmdir /s /q "%VENV_DIR%"
  )
)

if not exist "%PYTHON_EXE%" (
  echo Creating isolated environment in the short path:
  echo %VENV_DIR%
  %PY_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

>".scs_venv_path.txt" echo %VENV_DIR%

if exist ".venv" (
  echo.
  echo Note: the old local .venv is ignored because its path may be too long.
  echo You may delete it after this installation succeeds.
)

echo.
echo Updating packaging tools...
"%PYTHON_EXE%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :install_error

echo.
echo Installing Python 3.11+ compatible dependencies...
"%PYTHON_EXE%" -m pip install --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 goto :install_error

"%PYTHON_EXE%" -m pip check
if errorlevel 1 goto :install_error

echo.
echo Verifying runtime and native modules...
"%PYTHON_EXE%" "%~dp0check_runtime.py"
if errorlevel 1 goto :compat_error

echo.
echo Running with:
"%PYTHON_EXE%" -c "import sys,struct; print(sys.executable); print(sys.version); print(str(struct.calcsize('P')*8) + '-bit')"
echo.
"%PYTHON_EXE%" "%~dp0main.py"
exit /b %errorlevel%

:install_error
echo.
echo Dependency installation failed.
echo The environment path used was:
echo %VENV_DIR%
echo.
echo If the message still mentions Windows Long Path support, run
echo enable_long_paths_admin.bat as Administrator, restart Windows, and try again.
pause
exit /b 1

:compat_error
echo.
echo Runtime compatibility validation failed.
echo Open runtime_compatibility.json for the exact component that failed.
pause
exit /b 1
