@echo off
REM ==========================================================
REM  PCB-CAM  -  one-click test suite
REM  1) rebuild demo data      2) loader/analysis self-test
REM  3) front-end headless smoke test (needs a running server)
REM ==========================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."

set "PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "C:\Python312\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found.
  exit /b 1
)

set "NODE="
where node >nul 2>nul && set "NODE=node"

set "PORT=8790"
if not "%PCB_CAM_PORT%"=="" set "PORT=%PCB_CAM_PORT%"

echo.
echo === [1/3] rebuild demo samples ==========================
"%PY%" tests\make_samples.py
if errorlevel 1 goto :fail

echo.
echo === [2/3] parser / analysis self-test ===================
"%PY%" tests\selftest.py
if errorlevel 1 goto :fail

echo.
echo === [3/3] front-end headless smoke test =================
if not defined NODE (
  echo [SKIP] node.js not found - cannot run UI smoke test.
  goto :done
)

curl -s -o nul "http://127.0.0.1:%PORT%/api/state"
if errorlevel 1 (
  echo starting server on port %PORT% ...
  start "" /min "%PY%" -m pcb_cam.server --port %PORT%
  ping -n 4 127.0.0.1 >nul
)

"%NODE%" tests\ui_smoke.js static\index.html http://127.0.0.1:%PORT% > "%TEMP%\pcbcam-ui.json"
if errorlevel 1 ( type "%TEMP%\pcbcam-ui.json" & goto :fail )
"%PY%" tests\ui_report.py "%TEMP%\pcbcam-ui.json"
if errorlevel 1 goto :fail

set "PCB_SAMPLE=odb"
"%NODE%" tests\ui_smoke.js static\index.html http://127.0.0.1:%PORT% > "%TEMP%\pcbcam-ui2.json"
if errorlevel 1 ( type "%TEMP%\pcbcam-ui2.json" & goto :fail )
"%PY%" tests\ui_report.py "%TEMP%\pcbcam-ui2.json"
if errorlevel 1 goto :fail
set "PCB_SAMPLE="

:done
echo.
echo ==========================================================
echo  ALL TESTS PASSED
echo  Web UI: http://127.0.0.1:%PORT%/
echo ==========================================================
if /I not "%1"=="silent" pause
exit /b 0

:fail
echo.
echo *** TEST FAILED ***
if /I not "%1"=="silent" pause
exit /b 1
