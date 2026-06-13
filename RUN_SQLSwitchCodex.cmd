@echo off
setlocal
chcp 437 >nul
set "SQLSWITCH_STDIO_ENCODING=ascii"
set "PYTHONIOENCODING=ascii:replace"
cd /d "%~dp0"

set "SCRIPT=%~dp0SQLSwitchCodex.py"

if exist "%LocalAppData%\Python\bin\python.exe" (
    "%LocalAppData%\Python\bin\python.exe" "%SCRIPT%" english-menu %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
    py "%SCRIPT%" english-menu %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%" english-menu %*
    exit /b %ERRORLEVEL%
)

if exist "%UserProfile%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    "%UserProfile%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "%SCRIPT%" english-menu %*
    exit /b %ERRORLEVEL%
)

echo Could not find Python.
echo Try installing Python, or run SQLSwitchCodex.py with a full python.exe path.
pause
exit /b 1
