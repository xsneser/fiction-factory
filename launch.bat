@echo off
cd /d "%~dp0"
title NovelEngine

echo.
echo   [NovelEngine v2.0] Novel Factory
echo   Starting...
echo.

:: --- Check Python ---
set PYTHON=

:: Try python
python --version >nul 2>&1
if %errorlevel% equ 0 set PYTHON=python

:: Try py launcher
if "%PYTHON%"=="" (
    py --version >nul 2>&1
    if %errorlevel% equ 0 set PYTHON=py
)

if "%PYTHON%"=="" (
    echo [ERROR] Python not found.
    echo         Install from https://www.python.org/downloads/
    echo         Check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [OK] Python: %PYTHON%

:: --- Check api.json ---
:: Keep this file ASCII-only: cmd.exe reads .bat in the OEM codepage (GBK here),
:: so UTF-8 Chinese comments get mis-decoded and eat neighbouring characters
:: (an "echo" can turn into "cho"), which kills the whole script.
:: Missing api.json -> copy template and CONTINUE (configure API at /settings).
if not exist "api.json" (
    if exist "api.example.json" (
        copy /y "api.example.json" "api.json" >nul
        echo [INFO] api.json created. Configure API address / Key at http://localhost:58080/settings
    ) else (
        echo [ERROR] api.json and api.example.json missing
        pause
        exit /b 1
    )
)

:: --- Install deps ---
echo [INFO] Checking packages...
%PYTHON% -c "import flask, requests, fontTools" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing...
    %PYTHON% -m pip install flask requests fonttools -q 2>nul
    if exist "requirements.txt" %PYTHON% -m pip install -r requirements.txt -q 2>nul
)

:: --- Launch ---
echo.
echo   ========================================
echo     Web UI: http://localhost:58080
echo     Close this window to stop.
echo   ========================================
echo.

start "" http://localhost:58080

%PYTHON% ui/web_ui.py
set ERR=%errorlevel%

if %ERR% neq 0 (
    echo.
    echo [ERROR] Exit code: %ERR%
    pause
)
