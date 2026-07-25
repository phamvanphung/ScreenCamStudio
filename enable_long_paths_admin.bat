@echo off
setlocal EnableExtensions

net session >nul 2>nul
if errorlevel 1 (
  echo This file must be run as Administrator.
  echo Right-click it and choose "Run as administrator".
  pause
  exit /b 1
)

reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f
if errorlevel 1 (
  echo Could not enable Windows Long Paths.
  pause
  exit /b 1
)

echo Windows Long Paths have been enabled.
echo Restart Windows before installing packages again.
pause
