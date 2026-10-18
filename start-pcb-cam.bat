@echo off
REM ==========================================================
REM  PCB-CAM  -  local PCB CAM viewer / analyzer
REM  Drag a folder (or file, or .zip/.tgz) onto this bat to
REM  load it directly; double-click to open the demo board.
REM ==========================================================
setlocal
cd /d "%~dp0"

REM ---- locate a Python 3 interpreter -----------------------
set "PY="

for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found.
  echo         Install Python 3.8+ or set PCB_CAM_PY to python.exe
  echo         e.g.  set PCB_CAM_PY=D:\Python312\python.exe
  echo.
  pause
  exit /b 1
)
if defined PCB_CAM_PY set "PY=%PCB_CAM_PY%"

set "PORT=8790"
if not "%PCB_CAM_PORT%"=="" set "PORT=%PCB_CAM_PORT%"

set "TARGET=%~1"

echo ==========================================================
echo  Python : %PY%
echo  URL    : http://127.0.0.1:%PORT%/
if defined TARGET echo  Loading: %TARGET%
echo  Stop   : Ctrl+C or close this window
echo ==========================================================
echo.

if "%TARGET%"=="" (
  %PY% -m pcb_cam.server --port %PORT% --open
) else (
  %PY% -m pcb_cam.server --port %PORT% --open --path "%TARGET%"
)

echo.
echo Server stopped.
pause
