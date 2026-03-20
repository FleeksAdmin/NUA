@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY_MINOR=3.11"
set "PY_RELEASE=3.11.9"
set "PY_DIR=%LocalAppData%\Programs\Python\Python311"
set "PY_EXE=%PY_DIR%\python.exe"
set "PY_INSTALLER=%TEMP%\python-%PY_RELEASE%-amd64.exe"
set "DEFAULT_PACKAGES=requests selenium pillow rapidocr-onnxruntime"

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
    echo Python %PY_MINOR% not found. Downloading Python %PY_RELEASE%...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PY_RELEASE%/python-%PY_RELEASE%-amd64.exe' -OutFile '%PY_INSTALLER%' -UseBasicParsing } catch { Write-Error $_; exit 1 }"
    if errorlevel 1 goto :fail

    echo Installing Python %PY_RELEASE%...
    "%PY_INSTALLER%" /quiet InstallAllUsers=0 Include_pip=1 Include_launcher=1 Include_test=0 SimpleInstall=1 PrependPath=1 TargetDir="%PY_DIR%"
    if errorlevel 1 goto :fail

    del /q "%PY_INSTALLER%" >nul 2>&1

    if exist "%PY_EXE%" (
        set "PY_MODE=local"
    ) else (
        where py >nul 2>&1
        if errorlevel 1 goto :fail
        py -3.11 -V >nul 2>&1
        if errorlevel 1 goto :fail
        set "PY_MODE=launcher"
    )
)

echo Checking pip...
call :python -m pip --version >nul 2>&1
if errorlevel 1 (
    call :python -m ensurepip --upgrade
    if errorlevel 1 goto :fail
)

echo Updating pip...
call :python -m pip install --upgrade pip
if errorlevel 1 (
    echo pip upgrade failed, continuing with current pip.
)

if exist "requirements.txt" (
    echo Installing dependencies from requirements.txt...
    call :python -m pip install -r "requirements.txt"
    if errorlevel 1 (
        echo Retrying dependencies install with --user...
        call :python -m pip install --user -r "requirements.txt"
        if errorlevel 1 goto :fail
    )
) else (
    echo Installing dependencies for main.py...
    call :python -m pip install %DEFAULT_PACKAGES%
    if errorlevel 1 (
        echo Retrying dependencies install with --user...
        call :python -m pip install --user %DEFAULT_PACKAGES%
        if errorlevel 1 goto :fail
    )
)

echo.
echo DONE. READE README.MD TO CONTIUNE
exit /b 0

:python
if /i "%PY_MODE%"=="launcher" (
    py -3.11 %*
) else (
    "%PY_EXE%" %*
)
exit /b %errorlevel%

:fail
echo.
echo Setup failed.
exit /b 1
