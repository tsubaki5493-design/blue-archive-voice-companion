@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 블루 아카이브 AI 보이스 컴패니언 — 실행 및 자동 설치

set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

set "VENV_DIR=%APP_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "READY_FLAG=%VENV_DIR%\.deps_installed"

if exist "%VENV_PYW%" if exist "%READY_FLAG%" goto :launch_app

echo ========================================================================
echo   블루 아카이브 AI 보이스 컴패니언 — 최초 1회 자동 설치
echo ========================================================================
echo.
echo [안심 안내] 100%% 공개 Python 소스 코드이며 숨겨진 실행 파일이 없습니다.
echo [안심 안내] 현재 폴더의 .venv 안에 공식 PyPI 패키지만 설치됩니다.
echo [안심 안내] API 키와 로그인 정보는 %%LOCALAPPDATA%%\VoiceCompanionData\ 에만 저장됩니다.
echo.

echo [1/4] 64비트 Python 3.11~3.14 설치 여부를 확인하는 중...
call :find_python
if defined SYS_PY_EXE goto :python_ready

echo.
echo [안내] 이 PC에 호환되는 64비트 Python 3.11~3.14가 설치되어 있지 않습니다.

where py >nul 2>&1
if errorlevel 1 goto :try_winget_install
py install --help >nul 2>&1
if errorlevel 1 goto :try_winget_install

echo Windows의 공식 Python 관리자에서 Python 3.12를 자동으로 설치할 수 있습니다.
choice /C YN /M "공식 Python 3.12를 지금 자동 설치할까요"
if errorlevel 2 goto :python_manual_guide

echo.
echo [설치 중] py install 3.12 로 공식 Python 3.12를 설치하는 중입니다...
py install 3.12
call :find_python
if defined SYS_PY_EXE goto :python_ready
goto :try_winget_fallback

:try_winget_install
where winget >nul 2>&1
if errorlevel 1 goto :python_manual_guide

echo Microsoft 공식 설치 도구인 winget으로 64비트 Python 3.12를 자동 설치할 수 있습니다.
choice /C YN /M "공식 64비트 Python 3.12를 지금 자동 설치할까요"
if errorlevel 2 goto :python_manual_guide

:try_winget_fallback
where winget >nul 2>&1
if errorlevel 1 goto :python_manual_guide
echo.
echo [설치 중] winget으로 공식 64비트 Python 3.12를 설치하는 중입니다. 약 30초~1분 걸립니다...
winget install -e --id Python.Python.3.12 --architecture x64 --accept-source-agreements --accept-package-agreements
call :find_python
if defined SYS_PY_EXE goto :python_ready

:python_manual_guide
echo.
echo [직접 설치 안내]
echo   1. https://www.python.org/downloads/ 에서 Python 3.12 64-bit Windows 버전을 설치해 주세요.
echo   2. 설치 창 맨 아래 "Add python.exe to PATH" 를 체크한 뒤 설치해 주세요.
echo   3. 설치 후 1_실행하기.bat 을 다시 더블클릭하면 자동으로 실행됩니다.
echo.
pause
exit /b 1

:python_ready
echo [1/4] Python 확인 완료: "%SYS_PY_EXE%"
echo.

if exist "%VENV_PY%" goto :venv_ready
echo [2/4] 전용 실행 환경 .venv 생성 중... 약 10~20초 걸립니다.
"%SYS_PY_EXE%" -m venv "%VENV_DIR%"
if errorlevel 1 goto :venv_failed
if not exist "%VENV_PY%" goto :venv_failed
goto :install_deps

:venv_ready
echo [2/4] 기존 .venv 폴더를 이어서 사용합니다.

:install_deps
echo.
echo [3/4] 필수 파이썬 패키지 다운로드 및 설치 중... 최초 1회 약 1~2분 소요됩니다.
echo       아래에 설치 진행 상황이 표시되니 완료될 때까지 창을 닫지 마세요.
echo.
"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 "%VENV_PY%" -m ensurepip --upgrade

"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 echo [안내] pip 업그레이드는 건너뛰고 패키지 설치를 계속합니다.

"%VENV_PY%" -m pip install --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements.txt"
if not errorlevel 1 goto :check_gpu

echo.
echo [재시도] Windows 시스템 인증서 모드로 패키지 설치를 한 번 더 시도합니다...
"%VENV_PY%" -m pip install --use-feature=truststore --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements.txt"
if errorlevel 1 goto :pip_failed

:check_gpu
if "%VOICE_COMPANION_SKIP_CUDA%"=="1" goto :verify_imports
where nvidia-smi >nul 2>&1
if errorlevel 1 goto :verify_imports
nvidia-smi >nul 2>&1
if errorlevel 1 goto :verify_imports

echo.
echo ========================================================================
echo   [NVIDIA 그래픽카드 감지!] GPU 초고속 음성 인식 가속을 켤 수 있습니다.
echo ========================================================================
echo   - Y 키: NVIDIA GPU 가속 설치 — 약 1.9GB 다운로드, 음성 인식 5배 빠름, 10초 뒤 자동 선택
echo   - N 키: 건너뛰고 지금 바로 CPU 모드로 실행 — 용량 절약, 바로 시작 가능
echo.
choice /C YN /T 10 /D Y /M "NVIDIA GPU 가속 패키지를 설치할까요"
if errorlevel 2 goto :verify_imports

echo.
echo [GPU 가속 설치 중] NVIDIA CUDA 런타임 약 1.9GB를 다운로드하는 중입니다.
echo                     인터넷 속도에 따라 1~3분 걸릴 수 있으니 창을 닫지 마세요...
echo.
"%VENV_PY%" -m pip install --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements-cuda.txt"
if errorlevel 1 echo [안내] GPU 가속 설치를 건너뛰고 CPU 모드로 자동 동작합니다.

:verify_imports
echo.
echo [4/4] 설치된 실행 모듈 최종 점검 중...
"%VENV_PY%" -c "import tkinter, numpy, scipy, httpx, msgpack, requests, sounddevice, soundfile, pyperclip, faster_whisper; print('[확인 완료] 모든 필수 모듈 정상 로드!')"
if errorlevel 1 goto :verify_failed

echo installed> "%READY_FLAG%"
echo.
echo ========================================================================
echo   설치 완료! 잠시 후 블루 아카이브 보이스 컴패니언 창이 열립니다.
echo   다음부터는 1_실행하기.bat 을 누르면 1초 만에 바로 열립니다.
echo ========================================================================
if "%VOICE_COMPANION_NO_LAUNCH%"=="1" exit /b 0
timeout /t 2 /nobreak >nul

:launch_app
if "%VOICE_COMPANION_NO_LAUNCH%"=="1" exit /b 0
if exist "%VENV_PYW%" goto :launch_pyw
start "" "%VENV_PY%" "%APP_DIR%src\controller\aru_fish_bridge.py"
exit /b 0

:launch_pyw
start "" "%VENV_PYW%" "%APP_DIR%src\controller\aru_fish_bridge.py"
exit /b 0

:venv_failed
echo.
echo [오류] 전용 가상환경 .venv 생성에 실패했습니다.
echo        위 화면의 오류 메시지를 확인한 뒤 1_실행하기.bat 을 다시 실행해 주세요.
echo.
pause
exit /b 1

:pip_failed
echo.
echo [오류] 필수 패키지 설치에 실패했습니다.
echo        위 화면의 ERROR 메시지를 확인해 주세요.
echo        인터넷 또는 방화벽/백신 차단 여부를 확인한 뒤 1_실행하기.bat 을 다시 실행해 주세요.
echo.
pause
exit /b 1

:verify_failed
echo.
echo [오류] 일부 필수 모듈을 불러오지 못했습니다.
echo        위 오류 내용을 확인한 뒤 .venv 폴더를 삭제하고 1_실행하기.bat 을 다시 실행해 주세요.
echo.
pause
exit /b 1

:find_python
set "SYS_PY_EXE="
call :try_py "py" "-3.12"
if defined SYS_PY_EXE exit /b 0
call :try_py "py" "-3.13"
if defined SYS_PY_EXE exit /b 0
call :try_py "py" "-3.14"
if defined SYS_PY_EXE exit /b 0
call :try_py "py" "-3.11"
if defined SYS_PY_EXE exit /b 0
call :try_py "py" "-3"
if defined SYS_PY_EXE exit /b 0
call :try_py "python" ""
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Python\pythoncore-3.11-64\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "C:\Program Files\Python312\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "C:\Program Files\Python313\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "C:\Program Files\Python314\python.exe"
if defined SYS_PY_EXE exit /b 0
call :try_py_path "C:\Program Files\Python311\python.exe"
exit /b 0

:try_py
where "%~1" >nul 2>&1
if errorlevel 1 exit /b 0
set "PROBE_OUT=%TEMP%\ba_py_probe_%RANDOM%.txt"
call "%~1" %~2 -c "import sys, tkinter, venv; ok = sys.maxsize > 2**32 and sys.version_info[0] == 3 and 11 <= sys.version_info[1] <= 14; print('BA_PY_OK|' + sys.executable if ok else '')" > "%PROBE_OUT%" 2>nul
call :read_probe "%PROBE_OUT%"
exit /b 0

:try_py_path
if not exist "%~1" exit /b 0
set "PROBE_OUT=%TEMP%\ba_py_probe_%RANDOM%.txt"
"%~1" -c "import sys, tkinter, venv; ok = sys.maxsize > 2**32 and sys.version_info[0] == 3 and 11 <= sys.version_info[1] <= 14; print('BA_PY_OK|' + sys.executable if ok else '')" > "%PROBE_OUT%" 2>nul
call :read_probe "%PROBE_OUT%"
exit /b 0

:read_probe
set "PY_PROBE="
if not exist "%~1" exit /b 0
for /f "usebackq tokens=1* delims=|" %%A in ("%~1") do if "%%A"=="BA_PY_OK" set "PY_PROBE=%%B"
del /f /q "%~1" >nul 2>&1
if not defined PY_PROBE exit /b 0
if not exist "%PY_PROBE%" exit /b 0
set "SYS_PY_EXE=%PY_PROBE%"
exit /b 0
