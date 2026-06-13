@echo off
setlocal
chcp 437 >nul
set "SQLSWITCH_STDIO_ENCODING=ascii"
set "PYTHONIOENCODING=ascii:replace"
cd /d "%~dp0"

net session >nul 2>nul
if errorlevel 1 (
    echo Requesting Administrator permission...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

set "SCRIPT=%~dp0SQLSwitchCodex.py"
set "MENU=simple-menu"

if exist "%LocalAppData%\Python\bin\python.exe" (
    "%LocalAppData%\Python\bin\python.exe" "%SCRIPT%" %MENU%
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
    py "%SCRIPT%" %MENU%
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%" %MENU%
    exit /b %ERRORLEVEL%
)

if exist "%UserProfile%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    "%UserProfile%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "%SCRIPT%" %MENU%
    exit /b %ERRORLEVEL%
)

echo Could not find Python.
echo Try installing Python, or run SQLSwitchCodex.py with a full python.exe path.
pause
exit /b 1

