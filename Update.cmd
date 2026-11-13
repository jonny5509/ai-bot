@echo off
setlocal
cd /d "%~dp0"
echo.
echo Updating EmlaLock AI Bot from GitHub...
echo Keep this window open until the update finishes.
echo.

set "PYTHON_CMD="
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
  goto :run
)

where py >nul 2>nul
if not errorlevel 1 (
  for %%V in (3.12 3.13 3.14 3.15 3.16) do (
    if not defined PYTHON_CMD (
      py -0p 2>nul | findstr /C:":%%V" >nul 2>&1
      if not errorlevel 1 (
        py -%%V -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=py -%%V"
      )
    )
  )
)
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  echo Python was not found. Install Python 3.12+ and try again.
  echo.
  pause
  exit /b 1
)

:run
"%PYTHON_CMD%" "%~dp0update.py"
if errorlevel 1 (
  echo.
  echo Update did not complete. See the error above.
) else (
  echo.
  echo Update finished.
)
echo.
pause
