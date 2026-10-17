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

rem 必须把 endlocal 与 exit 写在同一行：endlocal 会丢弃 RC 变量，
rem 若先 endlocal 再 exit /b %RC%，RC 已不存在，退出码会丢（pause 成功后
rem ERRORLEVEL 为 0，脚本会以 0 退出，调用方拿不到失败信号）。
rem 复合命令在解析期统一展开 %RC%，此时变量仍在作用域内。
endlocal & exit /b %RC%
