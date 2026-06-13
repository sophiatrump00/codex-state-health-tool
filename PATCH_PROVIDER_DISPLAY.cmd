@echo off
setlocal
chcp 437 >nul
set "SQLSWITCH_STDIO_ENCODING=ascii"
set "PYTHONIOENCODING=ascii:replace"
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }" >nul 2>nul
if errorlevel 1 (
    echo Requesting Administrator permission...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

set "SCRIPT=%~dp0SQLSwitchCodex.py"

if exist "%LocalAppData%\Python\bin\python.exe" (
    "%LocalAppData%\Python\bin\python.exe" "%SCRIPT%" provider-patch
    pause
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
    py "%SCRIPT%" provider-patch
    pause
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%" provider-patch
    pause
    exit /b %ERRORLEVEL%
)

if exist "%UserProfile%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    "%UserProfile%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "%SCRIPT%" provider-patch
    pause
    exit /b %ERRORLEVEL%
)

echo Could not find Python.
echo Try installing Python, or run SQLSwitchCodex.py with a full python.exe path.
pause
exit /b 1
