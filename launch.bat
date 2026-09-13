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
:: 缺失时只复制一份空配置**继续启动**：API 地址与 Key 由用户在 /settings 页面填写，
:: 引擎与 dsh 都从这一份 api.json 读，不必先手工编辑文件（别再 notepad + exit）。
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
