@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
set "PY_MODE="

where py >nul 2>&1
if not errorlevel 1 (
    py -3.11 -V >nul 2>&1
    if not errorlevel 1 set "PY_MODE=launcher"
)

if not defined PY_MODE (
    if exist "%PY_EXE%" set "PY_MODE=local"
)

if not defined PY_MODE (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_MODE=python"
)

if not defined PY_MODE (
    echo Python not found. Run setup.bat first.
    pause
    exit /b 1
)

set "TEST_URL="
set /p "TEST_URL=Paste test URL: "
if not defined TEST_URL (
    echo URL is required.
    pause
    exit /b 1
)

echo.
echo Select mode:
echo 1 - Auto pass (use --auto)
echo 2 - Highlight only
set "MODE="
set /p "MODE=Enter 1 or 2: "

set "EXTRA_MODE="
if "%MODE%"=="1" set "EXTRA_MODE=--auto"
if "%MODE%"=="2" set "EXTRA_MODE="

if not "%MODE%"=="1" if not "%MODE%"=="2" (
    echo Invalid mode.
    pause
    exit /b 1
)

echo.
echo Select provider:
echo 1 - g4f (GPT OSS 120B, default) - best free option and recommended
echo 2 - DeepSeek - paid API, usually less effective, API key required
set "PROVIDER="
set /p "PROVIDER=Enter 1 or 2 [default 1]: "
if not defined PROVIDER set "PROVIDER=1"

set "PROVIDER_FLAG=--gpt"
if "%PROVIDER%"=="2" set "PROVIDER_FLAG=--deep"
if not "%PROVIDER%"=="1" if not "%PROVIDER%"=="2" (
    echo Invalid provider.
    pause
    exit /b 1
)

set "DEEP_KEY="
if "%PROVIDER%"=="2" (
    echo.
    echo DeepSeek requires your API key.
    set /p "DEEP_KEY=Enter DeepSeek API key (sk-...): "
    if not defined DEEP_KEY (
        echo DeepSeek API key is required.
        pause
        exit /b 1
    )
)

echo.
echo Starting main.py...
if "%PROVIDER%"=="2" (
    call :run_python main.py --har "naurok.com.ua.har" --url "%TEST_URL%" %PROVIDER_FLAG% %EXTRA_MODE% --deep-key "%DEEP_KEY%"
) else (
    call :run_python main.py --har "naurok.com.ua.har" --url "%TEST_URL%" %PROVIDER_FLAG% %EXTRA_MODE%
)
set "EXIT_CODE=%errorlevel%"
echo.
echo Script finished with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:run_python
if /I "%PY_MODE%"=="launcher" (
    py -3.11 %*
) else if /I "%PY_MODE%"=="local" (
    "%PY_EXE%" %*
) else (
    python %*
)
exit /b %errorlevel%
