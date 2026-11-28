@echo off
setlocal EnableExtensions
set "NoDefaultCurrentDirectoryInExePath=1"
"%SystemRoot%\System32\chcp.com" 65001 >nul
title MomoTalk Live — Colab S2-Pro 자동 연결 실행

set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

REM --- Sanitize Python & pip environment variables (anti-hijack / anti-index-redirect) ---
set "ERRORLEVEL="
set "PYTHONPATH="
set "PYTHONSTARTUP="
set "PYTHONHOME="
set "PYTHONUSERBASE="
set "PYTHONINSPECT="
set "PYTHONNOUSERSITE=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PIP_INDEX_URL="
set "PIP_EXTRA_INDEX_URL="
set "PIP_TRUSTED_HOST="
set "PIP_FIND_LINKS="
set "PIP_CONFIG_FILE="
set "PIP_NO_INDEX="
set "PIP_CONSTRAINT="
set "PIP_TARGET="
set "PIP_PREFIX="

set "VENV_DIR=%APP_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "READY_FLAG=%VENV_DIR%\.deps_installed"
set "LAUNCHER_PY=%APP_DIR%tools\colab_s2pro_launcher.py"

if not exist "%LAUNCHER_PY%" goto :launcher_missing
if exist "%VENV_PYW%" if exist "%VENV_PY%" if exist "%READY_FLAG%" goto :check_deps_hash
goto :bootstrap_venv

:check_deps_hash
"%VENV_PY%" -c "import hashlib, os, pathlib, sys; p = pathlib.Path(os.environ['APP_DIR']) / 'requirements.txt'; f = pathlib.Path(os.environ['READY_FLAG']); cur = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'installed'; stored = f.read_text(encoding='utf-8', errors='ignore').strip() if f.is_file() else ''; sys.exit(0 if stored == cur else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :launch_colab
goto :bootstrap_venv

:bootstrap_venv
set "CALLER_NO_LAUNCH=%VOICE_COMPANION_NO_LAUNCH%"
if not exist "%APP_DIR%1_실행하기.bat" goto :setup_missing
echo ========================================================================
echo   MomoTalk Live — 최초 1회 환경 점검 및 설치
echo ========================================================================
echo.
set "VOICE_COMPANION_NO_LAUNCH=1"
call "%APP_DIR%1_실행하기.bat"
if errorlevel 1 exit /b 1
set "VOICE_COMPANION_NO_LAUNCH=%CALLER_NO_LAUNCH%"
if not exist "%VENV_PY%" goto :setup_failed
goto :launch_colab

:launch_colab
if "%VOICE_COMPANION_NO_LAUNCH%"=="1" exit /b 0
set "FORCE_CONSOLE=0"
call :check_console_args %*
if "%FORCE_CONSOLE%"=="1" goto :launch_console
if exist "%VENV_PYW%" goto :launch_pyw

:launch_console
"%VENV_PY%" "%LAUNCHER_PY%" %*
exit /b %ERRORLEVEL%

:launch_pyw
start "" "%VENV_PYW%" "%LAUNCHER_PY%" %*
exit /b 0

:check_console_args
if "%~1"=="" exit /b 0
if /I "%~1"=="--check-only" set "FORCE_CONSOLE=1"
if /I "%~1"=="--no-launch" set "FORCE_CONSOLE=1"
if /I "%~1"=="-h" set "FORCE_CONSOLE=1"
if /I "%~1"=="--help" set "FORCE_CONSOLE=1"
shift
goto :check_console_args

:launcher_missing
echo [오류] Colab 자동 연결 런처 파일이 없습니다: "%LAUNCHER_PY%"
echo        프로젝트 폴더가 손상되지 않았는지 확인해 주세요.
pause
exit /b 1

:setup_missing
echo [오류] 1_실행하기.bat 파일을 찾을 수 없어 .venv 환경을 준비할 수 없습니다.
pause
exit /b 1

:setup_failed
echo [오류] 가상환경(.venv) 준비가 완료되지 않았습니다.
echo        먼저 1_실행하기.bat 을 실행해 설치를 완료해 주세요.
pause
exit /b 1
