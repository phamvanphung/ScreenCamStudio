@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo This removes only ScreenCam Studio Python environments and caches.
echo Your recordings and source files will not be deleted.
echo.
set /p "CONFIRM=Type YES to continue: "
if /I not "%CONFIRM%"=="YES" exit /b 0

if exist "%LOCALAPPDATA%\SCS\v184" rmdir /s /q "%LOCALAPPDATA%\SCS\v184"
if exist "%LOCALAPPDATA%\SCS\tmp" rmdir /s /q "%LOCALAPPDATA%\SCS\tmp"
if exist ".venv" rmdir /s /q ".venv"
if exist ".scs_venv_path.txt" del /q ".scs_venv_path.txt"

echo Environment reset completed. Run run.bat again.
pause
