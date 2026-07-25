@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".scs_venv_path.txt" (
  echo Chua co thong tin moi truong. Hay chay run.bat truoc.
  pause
  exit /b 1
)

set /p "VENV_DIR="<".scs_venv_path.txt"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Khong tim thay moi truong Python:
  echo %VENV_DIR%
  echo Hay chay run.bat lai.
  pause
  exit /b 1
)

"%PYTHON_EXE%" "%~dp0self_test.py"
set "RESULT=%errorlevel%"
echo.
if "%RESULT%"=="0" (
  echo Self-test co ban da hoan tat. Xem self_test_report.json.
) else (
  echo Self-test phat hien loi quan trong. Xem self_test_report.json.
)
pause
exit /b %RESULT%
