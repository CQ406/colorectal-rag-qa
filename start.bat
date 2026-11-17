@echo off
REM =====================================================================
REM  One-click launcher for the Colorectal RAG system.
REM  Double-click this file to run.
REM
REM  Why this wrapper exists:
REM    - A .ps1 file cannot be run by double-click (Windows opens Notepad).
REM    - Windows blocks unsigned .ps1 scripts under the default
REM      RemoteSigned execution policy.
REM    - This wrapper calls PowerShell with -ExecutionPolicy Bypass,
REM      which applies to this run only and does not change system settings.
REM
REM  IMPORTANT: keep this file pure ASCII.
REM    Batch files containing non-ASCII text break under mismatched
REM    code pages (cmd parse page vs console page vs file encoding).
REM    All Chinese output is produced by start-all.ps1 instead, which is
REM    UTF-8 with BOM and therefore decodes reliably.
REM =====================================================================

setlocal
cd /d "%~dp0"

REM Use UTF-8 code page so start-all.ps1 Chinese output renders correctly
chcp 65001 >nul 2>nul

echo.
echo ======================================================================
echo  Colorectal Surgery Perioperative Knowledge Base - Launcher
echo ======================================================================
echo.

where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] PowerShell not found. Windows 10/11 is required.
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0start-all.ps1" (
    echo [ERROR] start-all.ps1 not found in this folder.
    echo.
    pause
    exit /b 1
)

REM start.bat -CheckOnly / -SkipWarmup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
set EXITCODE=%errorlevel%

echo.
if %EXITCODE% neq 0 (
    echo ======================================================================
    echo  [FAILED] Launcher exited with code %EXITCODE%
    echo           Check the output above and the logs\ folder.
    echo ======================================================================
) else (
    echo ======================================================================
    echo  Startup finished. Open the URLs shown above in your browser.
    echo ======================================================================
)

echo.
echo Press any key to close this window.
echo (Services keep running in the background.)
pause >nul
endlocal