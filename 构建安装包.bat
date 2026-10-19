@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal EnableExtensions EnableDelayedExpansion

rem ===========================================================================
rem  AI Resume Generator —— Windows 安装包一键构建
rem
rem  产物：desktop\src-tauri\target\release\bundle\nsis\*.exe（NSIS 安装程序）
rem  终端用户双击安装即可使用，**不需要安装 Python**（后端已打成 sidecar）。
rem
rem  阶段：
rem    1/5 检查环境      node / npm / rustc / cargo / .venv
rem    2/5 构建依赖      pyinstaller、pillow；缺失时生成全套应用图标
rem    3/5 打包后端      PyInstaller -> packaging\out\resume-server.exe -> binaries\
rem    4/5 构建安装包    npm install + tauri build（首次 10-20 分钟，有心跳）
rem    5/5 报告产物      打印最新安装包的路径与大小，并**自动打开安装程序**
rem
rem  注意：本文件必须保持 CRLF 换行；末尾用 `endlocal & exit /b %RC%` 单行形式，
rem  否则退出码会永远是 0（先 endlocal 会丢变量）。
rem ===========================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "PACKAGING=%ROOT%\packaging"
set "DESKTOP=%ROOT%\desktop"
set "SRC_TAURI=%DESKTOP%\src-tauri"
set "BINARIES=%SRC_TAURI%\binaries"
set "ICON_ICO=%SRC_TAURI%\icons\icon.ico"
set "BUNDLE=%SRC_TAURI%\target\release\bundle\nsis"
set "STALE=%SRC_TAURI%\target\release\bundle\_stale"
set "TRIPLE="
set "OUT="
set "RC=0"

rem 参数（都可省略）：
rem   --no-launch  构建成功后不自动打开安装程序（自动化/CI 用）
rem   --no-pause   结束后不等待按键（自动化/CI 用）
set "AUTO_LAUNCH=1"
set "DO_PAUSE=1"
for %%A in (%*) do (
    if /i "%%~A"=="--no-launch" set "AUTO_LAUNCH="
    if /i "%%~A"=="--no-pause" set "DO_PAUSE="
)

echo.
echo ================================================================
echo    AI RESUME GENERATOR  ·  桌面安装包构建
echo ================================================================
echo    项目目录：%ROOT%
echo.

rem ---------------------------------------------------------------------------
rem  阶段 1/5：检查环境
rem ---------------------------------------------------------------------------
echo [1/5] 检查构建环境...

set "MISSING="
where node >nul 2>nul || set "MISSING=!MISSING! node"
where npm  >nul 2>nul || set "MISSING=!MISSING! npm"
where rustc >nul 2>nul || set "MISSING=!MISSING! rustc"
where cargo >nul 2>nul || set "MISSING=!MISSING! cargo"

if defined MISSING goto :no_toolchain
if not exist "%VENV_PY%" goto :no_venv

for /f "tokens=*" %%V in ('node -v 2^>nul') do echo    Node.js : %%V
for /f "tokens=*" %%V in ('cargo -V 2^>nul') do echo    Cargo   : %%V
echo    Python  : %VENV_PY%

rem 目标三元组（sidecar 二进制必须带此后缀命名）
for /f "tokens=2" %%T in ('rustc -vV 2^>nul ^| findstr /b "host:"') do set "TRIPLE=%%T"
if not defined TRIPLE goto :no_triple
echo    目标平台: !TRIPLE!
echo.

rem ---------------------------------------------------------------------------
rem  阶段 2/5：构建依赖
rem ---------------------------------------------------------------------------
echo [2/5] 准备构建依赖（PyInstaller / Pillow / 图标）...

"%VENV_PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo    安装 PyInstaller 与 Pillow...
    "%VENV_PY%" -m pip install --disable-pip-version-check -q pyinstaller pillow
    if errorlevel 1 goto :pip_failed
) else (
    echo    PyInstaller 已安装
)

if exist "%ICON_ICO%" goto :icons_ready

echo    生成应用图标（1024x1024 PNG + 全套平台图标）...
if not exist "%PACKAGING%\build" mkdir "%PACKAGING%\build"
"%VENV_PY%" "%PACKAGING%\make_icon.py" "%PACKAGING%\build"
if errorlevel 1 goto :icon_failed

rem `tauri icon` 需要 @tauri-apps/cli，先确保桌面端依赖就位
if exist "%DESKTOP%\node_modules" goto :icon_cli_ready
pushd "%DESKTOP%"
echo    首次安装桌面端依赖（npm install）...
call npm install --no-fund --no-audit
set "NPM_RC=!errorlevel!"
popd
if not "!NPM_RC!"=="0" goto :npm_install_failed

:icon_cli_ready
pushd "%DESKTOP%"
call npx --no-install tauri icon "..\packaging\build\icon-1024.png"
set "ICON_RC=!errorlevel!"
popd
if not "!ICON_RC!"=="0" goto :icon_failed
if not exist "%ICON_ICO%" goto :icon_failed
echo    图标已生成：%SRC_TAURI%\icons\

:icons_ready
echo.

rem ---------------------------------------------------------------------------
rem  阶段 3/5：打包后端 sidecar
rem ---------------------------------------------------------------------------
echo [3/5] 打包后端为独立可执行文件（约 1-3 分钟）...
echo    PyInstaller 正在收集依赖，请稍候...

pushd "%ROOT%"
"%VENV_PY%" -m PyInstaller --noconfirm --clean ^
    --distpath "%PACKAGING%\out" ^
    --workpath "%PACKAGING%\build\pyinstaller" ^
    "%PACKAGING%\resume-server.spec"
set "PYI_RC=!errorlevel!"
popd
if not "!PYI_RC!"=="0" goto :pyinstaller_failed
if not exist "%PACKAGING%\out\resume-server.exe" goto :pyinstaller_failed

if not exist "%BINARIES%" mkdir "%BINARIES%"
copy /y "%PACKAGING%\out\resume-server.exe" "%BINARIES%\resume-server-!TRIPLE!.exe" >nul
if errorlevel 1 goto :sidecar_copy_failed
for %%S in ("%BINARIES%\resume-server-!TRIPLE!.exe") do set "SIDECAR_SIZE=%%~zS"
set /a SIDECAR_MB=!SIDECAR_SIZE!/1048576
echo    后端 sidecar 就绪：binaries\resume-server-!TRIPLE!.exe（约 !SIDECAR_MB! MB）
echo.

rem ---------------------------------------------------------------------------
rem  阶段 4/5：构建安装包
rem ---------------------------------------------------------------------------
echo [4/5] 构建 NSIS 安装包...
echo    首次构建需要编译 Rust 依赖，通常 10-20 分钟，请勿关闭本窗口。

rem ---- 关键：把 cargo 产物目录钉死在本项目内 ----
rem 本机（以及不少开发机）在**用户级环境变量**里设了 CARGO_TARGET_DIR，指向别的项目
rem （例如 D:\forest-gap-tools\rust\target）。若不处理，本次构建的 exe / 安装包会写进
rem 那个共享目录：既与本项目「产物应在本项目内」的约定不符，也会和别人的构建产物混在
rem 一起（曾出现过「关掉的窗口显示的是另一个项目的页面」这类混淆事故）。
rem tauri_build.py 会用 --target-dir 把 CARGO_TARGET_DIR 覆盖成 %SRC_TAURI%\target
rem （默认值即如此），这里先如实告知，避免用户看到日志里的路径感到意外。
if defined CARGO_TARGET_DIR (
    echo.
    echo    [环境提示] 检测到全局环境变量 CARGO_TARGET_DIR：
    echo       %CARGO_TARGET_DIR%
    echo    它会让产物写到上面这个目录、而不是本项目内。本次构建会**忽略**它，
    echo    产物固定输出到：%SRC_TAURI%\target\release\
    echo    （仅影响本次构建，不会修改你的系统环境变量。）
)

rem 把上一次的安装包挪走：避免「构建失败后用户双击了旧包」
if exist "%BUNDLE%\*.exe" (
    if not exist "%STALE%" mkdir "%STALE%"
    move /y "%BUNDLE%\*.exe" "%STALE%\" >nul
    echo    上一次的安装包已挪到 _stale\，本次构建成功后不再需要。
)

if exist "%DESKTOP%\node_modules" goto :npm_ready
pushd "%DESKTOP%"
echo    安装桌面端依赖（npm install）...
call npm install --no-fund --no-audit
set "NPM_RC=!errorlevel!"
popd
if not "!NPM_RC!"=="0" goto :npm_install_failed

:npm_ready
echo    开始编译（npx tauri build）；每 15 秒打印一次心跳，说明没有卡死。
echo.
rem 用 Python 托管子进程：既能实时转发输出，又能每 15 秒打印心跳，还能拿到
rem 真实退出码。批处理自身做不到「后台跑 + 心跳 + 退出码」，而 PowerShell 的
rem Start-Process 在本机会因 HTTPS_PROXY / https_proxy 键名大小写重复而失败，
rem 故统一交给 packaging\tauri_build.py（只依赖已有的 .venv）。
"%VENV_PY%" "%PACKAGING%\tauri_build.py" --cwd "%DESKTOP%" --interval 15 --log "%DESKTOP%\build.log"
set "BUILD_RC=!errorlevel!"
if not "!BUILD_RC!"=="0" goto :build_failed
echo    构建命令已完成。
echo.

rem ---------------------------------------------------------------------------
rem  阶段 4.5：把默认安装目录改成 D:\<产品名> 并重打包
rem ---------------------------------------------------------------------------
rem  Tauri 的 currentUser 模式默认装到 %LOCALAPPDATA%\<产品名>，而且 NSIS 会把
rem  「上一次的安装路径」记进注册表并优先复用 —— 只要有人用 /D= 装过一次，
rem  之后即使不带参数也会沿用那个目录（本项目就因此一度默认装到 D:\_aitest\app）。
rem  这里用 Tauri 已展开好的 installer.nsi 改写 $INSTDIR 再用 makensis 重编译，
rem  不改动整份模板，模板升级也不会失效。重打包失败时保留 tauri 原包。
echo    设置默认安装目录为 D:\AI Resume Generator ...
"%VENV_PY%" "%PACKAGING%\repack_nsis.py" --desktop "%DESKTOP%" --drive D
if errorlevel 1 (
    echo    [注意] 重打包未完成，将使用 tauri 原安装包（默认目录仍是用户目录）。
) else (
    echo    默认安装目录已设置。
)
echo.

rem ---------------------------------------------------------------------------
rem  阶段 5/5：报告产物
rem ---------------------------------------------------------------------------
echo [5/5] 检查安装包产物...

if not exist "%BUNDLE%\*.exe" goto :no_artifact

rem 用通配符 + 按修改时间倒序取第一条：不写死版本号
for /f "delims=" %%F in ('dir /b /o-d "%BUNDLE%\*.exe" 2^>nul') do (
    set "OUT=%BUNDLE%\%%F"
    goto :got_artifact
)

:got_artifact
if not defined OUT goto :no_artifact
for %%S in ("%OUT%") do set "OUT_SIZE=%%~zS"
set /a OUT_MB=!OUT_SIZE!/1048576

echo.
echo ================================================================
echo    构建成功
echo ================================================================
echo    安装包：!OUT!
echo    大小  ：!OUT_SIZE! 字节（约 !OUT_MB! MB）
echo.
echo    终端用户双击该安装包即可安装，无需 Python 环境。
echo    数据保存在：%%LOCALAPPDATA%%\AIResumeGenerator\data
echo ================================================================
echo.

rem 构建成功后**自动打开安装程序**（不再询问 Y/N）：
rem   构建的意义就是装一次验证，多问一步纯属打断流程。
rem   start 的第一个参数是「窗口标题」，必须留空 ""，否则含空格的路径会被当成标题。
rem   自动化里不想弹窗时加 --no-launch。
if not defined AUTO_LAUNCH goto :done
echo    正在启动安装程序……
echo    （安装向导会弹出来；如需跳过，下次运行加 --no-launch）
echo.
start "" "!OUT!"

:done
set "RC=0"
goto :finish

rem ---------------------------------------------------------------------------
rem  失败分支
rem ---------------------------------------------------------------------------
:no_toolchain
echo.
echo [错误] 缺少构建工具：!MISSING!
echo.
echo   请先安装并**重新打开**命令行窗口（PATH 需要刷新）：
echo     Node.js 18+   https://nodejs.org/zh-cn/download
echo     Rust (MSVC)   https://rustup.rs/   （安装时选择 x86_64-pc-windows-msvc）
echo     Visual Studio Build Tools（Rust 的 MSVC 链接器依赖）
echo.
echo   装好后重新运行本脚本。
set "RC=1"
goto :finish

:no_venv
echo.
echo [错误] 找不到 %VENV_PY%
echo.
echo   请先在项目目录运行一次源码版启动器，它会创建虚拟环境并安装依赖：
echo     python start.py
echo   （或手动：python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt）
set "RC=1"
goto :finish

:no_triple
echo.
echo [错误] 无法从 `rustc -vV` 解析目标平台（host: 行）。
echo   请确认 Rust 安装正确：rustc -vV
set "RC=1"
goto :finish

:pip_failed
echo.
echo [错误] 安装 PyInstaller / Pillow 失败。
echo   请检查网络与 pip 源（代理环境可设置 NO_PROXY 或换国内源）。
set "RC=1"
goto :finish

:icon_failed
echo.
echo [错误] 生成应用图标失败。
echo   可手动执行排查：
echo     "%VENV_PY%" "%PACKAGING%\make_icon.py" "%PACKAGING%\build"
echo     cd desktop ^&^& npx tauri icon ..\packaging\build\icon-1024.png
set "RC=1"
goto :finish

:npm_install_failed
echo.
echo [错误] npm install 失败（desktop\）。
echo   请检查网络、npm 源，或先手动执行：cd desktop ^&^& npm install
set "RC=1"
goto :finish

:pyinstaller_failed
echo.
echo [错误] 打包后端失败，未生成 packaging\out\resume-server.exe。
echo   常见原因：
echo     - 依赖缺失：.venv\Scripts\pip install -r requirements.txt
echo     - 杀毒软件拦截了 PyInstaller 写 exe
echo     - 磁盘空间不足
set "RC=1"
goto :finish

:sidecar_copy_failed
echo.
echo [错误] 复制 sidecar 到 desktop\src-tauri\binaries\ 失败。
echo   请确认该目录可写，且目标文件未被占用。
set "RC=1"
goto :finish

:build_failed
echo.
echo [错误] tauri build 失败（退出码 !BUILD_RC!）。
if exist "%STALE%\*.exe" (
    move /y "%STALE%\*.exe" "%BUNDLE%\" >nul
    echo   已把上一次能用的安装包挪回原处，你可以继续使用它。
)
echo.
echo   常见原因：
echo     - NSIS / WiX 首次构建需要联网下载打包工具（请检查代理与网络）
echo     - 磁盘空间不足（Rust target 目录可达数 GB）
echo     - 项目路径含中文或空格导致个别工具异常（本机路径：%ROOT%）
echo     - Rust 首次编译被中途 Ctrl+C 打断，可重试
set "RC=1"
goto :finish

:no_artifact
echo.
echo [错误] 构建命令返回成功，但没有找到安装包（%BUNDLE%\*.exe）。
echo   为避免误装旧包，本次不启动任何安装程序。
echo   请查看上方的 tauri build 输出定位原因。
set "RC=1"
goto :finish

:finish
echo.
if "!RC!"=="0" (
    echo 完成。
) else (
    echo 构建未成功，请按上面的提示处理后重试。
)
rem 自动化（--no-pause）时不要停下来等按键
if defined DO_PAUSE pause
endlocal & exit /b %RC%
