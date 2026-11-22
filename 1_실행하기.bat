@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "READY_FLAG=%VENV_DIR%\.deps_installed"

if exist "%VENV_PYW%" if exist "%READY_FLAG%" goto :launch_app

echo ========================================================================
echo   블루 아카이브 AI 보이스 컴패니언 — 최초 1회 자동 준비 중
echo ========================================================================
echo.
echo [안심 안내] 100%% 공개 소스 코드이며 숨겨진 실행 파일(.exe/.dll/.sys)이 없습니다.
echo [안심 안내] 이 폴더 안의 .venv에 공식 Python 패키지만 설치되며,
echo             API 키·로그인 정보는 %%LOCALAPPDATA%%\VoiceCompanionData\ 에만 저장됩니다.
echo.

call :find_python
if not defined SYS_PY (
    echo [안내] 이 PC에 Python 3.10 이상이 설치되어 있지 않습니다.
    where winget >nul 2>&1
    if not errorlevel 1 (
        echo Microsoft 공식 설치 도구(winget)로 Python 3.12를 자동 설치한 뒤 바로 이어서 실행합니다.
        choice /C YN /M "공식 Python 3.12를 지금 자동 설치할까요"
        if not errorlevel 2 (
            echo.
            echo [설치 중] 공식 Python 3.12 설치 중... (약 30초~1분 소요)
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            call :find_python
        )
    )
)

if not defined SYS_PY (
    echo.
    echo [직접 설치 안내]
    echo   1. https://www.python.org/downloads/ 에서 Python 3.12 (Windows)를 설치해 주세요.
    echo   2. 설치 창 맨 아래 "Add python.exe to PATH" 를 체크한 뒤 설치해 주세요.
    echo   3. 설치 후 [1_실행하기.bat] 을 다시 더블클릭하면 자동으로 실행됩니다.
    echo.
    pause
    exit /b 1
)

echo [1/3] Python 확인 완료
if not exist "%VENV_PY%" (
    echo [2/3] 전용 실행 환경(.venv) 생성 중...
    call %SYS_PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [오류] 가상환경 생성에 실패했습니다.
        pause
        exit /b 1
    )
) else (
    echo [2/3] 기존 .venv 환경을 이어서 준비합니다...
)

echo [3/3] 필수 패키지 자동 설치 중 (최초 1회만 진행, 약 1~2분 소요)...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo [오류] 필수 패키지 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
    pause
    exit /b 1
)

where nvidia-smi >nul 2>&1
if not errorlevel 1 (
    nvidia-smi >nul 2>&1
    if not errorlevel 1 (
        echo.
        echo [NVIDIA 그래픽카드 감지!] GPU 초고속 음성 인식 가속을 켤 수 있습니다.
        echo   - [Y] GPU 가속 설치 (추천 · 약 1.9GB 추가 다운로드 · 8초 후 자동 선택)
        echo   - [N] 건너뛰고 지금 바로 CPU 모드로 실행 (용량 절약 · 정상 동작)
        choice /C YN /T 8 /D Y /M "GPU 가속 패키지를 설치할까요"
        if not errorlevel 2 (
            echo [GPU 가속 설치 중] 잠시만 기다려 주세요...
            "%VENV_PY%" -m pip install -r "%~dp0requirements-cuda.txt"
            if errorlevel 1 (
                echo [안내] GPU 가속 설치를 건너뛰고 CPU 모드로 자동 실행합니다.
            )
        )
    )
)

echo installed> "%READY_FLAG%"
echo.
echo [준비 완료!] 잠시 후 프로그램이 자동으로 열립니다...
echo (다음부터는 설치 과정 없이 1초 만에 바로 열립니다!)
timeout /t 2 /nobreak >nul

:launch_app
if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" "%~dp0src\controller\aru_fish_bridge.py"
) else (
    start "" "%VENV_PY%" "%~dp0src\controller\aru_fish_bridge.py"
)
exit /b 0

:find_python
set "SYS_PY="
call :try_py "py -3.12"
if defined SYS_PY exit /b 0
call :try_py "py -3.13"
if defined SYS_PY exit /b 0
call :try_py "py -3.14"
if defined SYS_PY exit /b 0
call :try_py "py -3.11"
if defined SYS_PY exit /b 0
call :try_py "py -3.10"
if defined SYS_PY exit /b 0
call :try_py "py -3"
if defined SYS_PY exit /b 0
call :try_py "python"
if defined SYS_PY exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined SYS_PY exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined SYS_PY exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if defined SYS_PY exit /b 0
call :try_py_path "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined SYS_PY exit /b 0
call :try_py_path "C:\Program Files\Python312\python.exe"
if defined SYS_PY exit /b 0
call :try_py_path "C:\Program Files\Python313\python.exe"
if defined SYS_PY exit /b 0
call :try_py_path "C:\Program Files\Python311\python.exe"
exit /b 0

:try_py
%~1 -c "import sys, tkinter; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "SYS_PY=%~1"
exit /b 0

:try_py_path
if not exist "%~1" exit /b 0
"%~1" -c "import sys, tkinter; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "SYS_PY="%~1""
exit /b 0
