@echo off
setlocal EnableExtensions
if "%~1"=="" (
  echo Keo file MP4 tam hoac MP4 chua co audio tha vao BAT nay.
  echo Script se tim file WAV sidecar cung phien va tao _recovered.mp4.
  pause
  exit /b 1
)

set "VIDEO=%~f1"
set "STEM=%~dpn1"
set "AUDIO="
set "OUT=%STEM%_recovered.mp4"

rem Truong hop input: ten.recording.mp4 -> ten.recording.wav
if exist "%STEM%.wav" set "AUDIO=%STEM%.wav"

rem Truong hop input: ten.mp4 -> ten.recording.wav
if not defined AUDIO if exist "%STEM%.recording.wav" set "AUDIO=%STEM%.recording.wav"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  if exist "%~dp0tools\ffmpeg.exe" (
    set "FFMPEG=%~dp0tools\ffmpeg.exe"
  ) else (
    echo Khong tim thay ffmpeg.exe trong PATH hoac tools.
    pause
    exit /b 1
  )
) else (
  set "FFMPEG=ffmpeg"
)

if defined AUDIO (
  echo Dang ghep:
  echo Video: %VIDEO%
  echo Audio: %AUDIO%
  "%FFMPEG%" -hide_banner -y -i "%VIDEO%" -i "%AUDIO%" -map 0:v:0 -map 1:a:0 -c:v copy -af "aresample=48000:async=1:first_pts=0,apad" -c:a aac -b:a 192k -ar 48000 -ac 2 -shortest -movflags +faststart "%OUT%"
) else (
  echo Khong tim thay WAV sidecar. Dang chuan hoa MP4 video-only...
  "%FFMPEG%" -hide_banner -y -i "%VIDEO%" -map 0:v:0 -c:v copy -an -movflags +faststart "%OUT%"
)

if errorlevel 1 (
  echo Phuc hoi that bai. File goc van duoc giu nguyen.
) else (
  echo Da tao: %OUT%
)
pause
