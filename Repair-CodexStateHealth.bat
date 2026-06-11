@echo off
setlocal
chcp 65001 >nul

set "SCRIPT=%~dp0Repair-CodexStateHealth.ps1"

if not exist "%SCRIPT%" (
  echo.
  echo ERROR: Repair-CodexStateHealth.ps1 was not found next to this BAT file.
  echo Put Repair-CodexStateHealth.bat and Repair-CodexStateHealth.ps1 in the same folder.
  echo.
  pause
  exit /b 1
)

:MAIN_MENU

echo.
echo === Codex state health tool ===
echo.
echo 1. Check only
echo 2. Fix and sync to latest session provider
echo 3. Advanced: choose target provider
echo 0. Exit
echo.
set /p MODE=Choose mode [1/2/3/0]: 
if "%MODE%"=="" set "MODE=1"

set "TARGET_PROVIDER=auto"

if "%MODE%"=="0" exit /b 0
if "%MODE%"=="3" goto PROVIDER_MENU

echo.
echo TargetProvider=auto ^(newest user thread^)
echo.

if "%MODE%"=="2" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Fix -TargetProvider "%TARGET_PROVIDER%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -TargetProvider "%TARGET_PROVIDER%"
)

echo.
echo Done. Press any key to close.
pause >nul
exit /b 0

:PROVIDER_MENU
echo.
echo === Advanced provider sync ===
echo.
echo 1. auto detect from newest user thread ^(Recommended^)
echo 2. openai
echo 3. custom
echo 4. anyrouter
echo 5. Type manually
echo 0. Back
echo.
set /p PROVIDER_CHOICE=Choose provider [1/2/3/4/5/0]: 

if "%PROVIDER_CHOICE%"=="0" goto MAIN_MENU
if "%PROVIDER_CHOICE%"=="2" (
  set "TARGET_PROVIDER=openai"
) else if "%PROVIDER_CHOICE%"=="3" (
  set "TARGET_PROVIDER=custom"
) else if "%PROVIDER_CHOICE%"=="4" (
  set "TARGET_PROVIDER=anyrouter"
) else if "%PROVIDER_CHOICE%"=="5" (
  set /p TARGET_PROVIDER=Enter model_provider value: 
) else (
  set "TARGET_PROVIDER=auto"
)

if "%TARGET_PROVIDER%"=="" set "TARGET_PROVIDER=auto"

:ADVANCED_ACTION
echo.
echo TargetProvider=%TARGET_PROVIDER%
echo.
echo 1. Check only
echo 2. Fix and sync provider
echo 0. Back
echo.
set /p ADV_MODE=Choose action [1/2/0]: 
if "%ADV_MODE%"=="0" goto PROVIDER_MENU

if "%ADV_MODE%"=="2" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Fix -TargetProvider "%TARGET_PROVIDER%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -TargetProvider "%TARGET_PROVIDER%"
)

echo.
echo Done. Press any key to close.
pause >nul
