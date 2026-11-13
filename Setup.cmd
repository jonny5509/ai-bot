@echo off
setlocal
cd /d "%~dp0"
echo Opening local setup wizard...
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
  goto :run
)

set "PYTHON_CMD="
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
  if not errorlevel 1 (
    python -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)
if not defined PYTHON_CMD (
  echo Python 3.12+ was not found. Install Python 3.12 or newer, or run with a specific interpreter.
  echo.
  pause
  exit /b 1
)

:run
"%PYTHON_CMD%" "%~dp0setup.py"
echo.
pause
