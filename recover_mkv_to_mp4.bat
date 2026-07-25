@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if "%~1"=="" (
  echo Keo file .mkv cua ban tha vao file recover_mkv_to_mp4.bat.
  pause
  exit /b 1
)

set "INPUT=%~f1"
set "OUTPUT=%~dpn1_recovered.mp4"
set "FFMPEG=%~dp0tools\ffmpeg.exe"
if not exist "%FFMPEG%" set "FFMPEG=%~dp0ffmpeg.exe"
if not exist "%FFMPEG%" set "FFMPEG=ffmpeg"

echo Dang phuc hoi: "%INPUT%"
"%FFMPEG%" -hide_banner -y -fflags +genpts -i "%INPUT%" -map 0:v:0 -map 0:a? -c copy -avoid_negative_ts make_zero -movflags +faststart "%OUTPUT%"
if not errorlevel 1 goto :success

echo Stream copy that bai, dang ma hoa lai video va audio...
"%FFMPEG%" -hide_banner -y -fflags +genpts -i "%INPUT%" -map 0:v:0 -map 0:a? -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 224k -movflags +faststart "%OUTPUT%"
if errorlevel 1 (
  echo Khong phuc hoi duoc. Hay gui file .ffmpeg-error.txt neu co.
  pause
  exit /b 1
)

:success
echo Da tao: "%OUTPUT%"
pause
