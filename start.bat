@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)

if not defined PYCMD (
    echo.
    echo  [ERROR] Python not found.
    echo.
    echo  Please install Python 3.11 or newer:
    echo    https://www.python.org/downloads/
    echo.
    echo  During installation, check "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PYCMD% "%~dp0start.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo  [ERROR] Startup failed with exit code %RC%.
    echo.
    pause
)

endlocal
exit /b %RC%
