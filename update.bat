@echo off
title EmlaLock AI Bot Updater
cd /d "%~dp0"
echo.
echo Updating EmlaLock AI Bot from GitHub...
echo Keep this window open until the update finishes.
echo.
python update.py
if errorlevel 1 (
    echo.
    echo Update did not complete. See the error above.
) else (
    echo.
    echo Update finished.
)
echo.
pause
