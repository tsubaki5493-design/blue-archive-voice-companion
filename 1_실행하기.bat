@echo off
setlocal EnableExtensions
set "NoDefaultCurrentDirectoryInExePath=1"
"%SystemRoot%\System32\chcp.com" 65001 >nul
title MomoTalk Live — 실행 및 자동 설치

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

if exist "%VENV_PYW%" if exist "%VENV_PY%" if exist "%READY_FLAG%" goto :check_deps_hash
if exist "%VENV_PYW%" if exist "%VENV_PY%" goto :probe_existing_venv
goto :first_time_setup

:check_deps_hash
"%VENV_PY%" -c "import hashlib, os, pathlib, sys; p = pathlib.Path(os.environ['APP_DIR']) / 'requirements.txt'; f = pathlib.Path(os.environ['READY_FLAG']); cur = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'installed'; stored = f.read_text(encoding='utf-8', errors='ignore').strip() if f.is_file() else ''; sys.exit(0 if stored == cur else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :launch_app
goto :first_time_setup

:probe_existing_venv
"%VENV_PY%" -c "import tkinter, numpy, scipy, httpx, h2, msgpack, requests, sounddevice, soundfile, pyperclip, faster_whisper" >nul 2>&1
if not "%ERRORLEVEL%"=="0" goto :first_time_setup
"%VENV_PY%" -c "import hashlib, os, pathlib; p = pathlib.Path(os.environ['APP_DIR']) / 'requirements.txt'; f = pathlib.Path(os.environ['READY_FLAG']); val = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'installed'; f.write_text(val + '\n', encoding='utf-8')" >nul 2>&1
goto :launch_app

:first_time_setup
set "REBUILD_VENV=0"
if exist "%VENV_PY%" goto :check_existing_venv_for_update
if exist "%VENV_DIR%" set "REBUILD_VENV=1"
goto :first_time_banner

:check_existing_venv_for_update
if not exist "%VENV_PYW%" goto :mark_broken_venv
"%VENV_PY%" -c "import importlib.util, sys; ok = (sys.maxsize > 2**32) and (sys.version_info[0] == 3 and 11 <= sys.version_info[1] <= 14) and (importlib.util.find_spec('tkinter') is not None) and (importlib.util.find_spec('_tkinter') is not None); sys.exit(0 if ok else 1)" >nul 2>&1
if not "%ERRORLEVEL%"=="0" goto :mark_broken_venv
echo ========================================================================
echo   MomoTalk Live — 패키지 업데이트 확인
echo ========================================================================
echo.
echo [업데이트 안내] requirements.txt 변경 또는 신규 설치가 감지되어 기존 .venv 패키지를 동기화합니다.
echo.
goto :venv_ready

:mark_broken_venv
set "REBUILD_VENV=1"
echo [안내] 기존 .venv 환경이 손상되었거나 호환되지 않아 새로 구성합니다...
echo.
goto :first_time_banner

:first_time_banner
echo ========================================================================
echo   MomoTalk Live — 최초 1회 자동 설치
echo ========================================================================
echo.
echo [안심 안내] 100%% 공개 Python 소스 코드이며 숨겨진 실행 파일이 없습니다.
echo [안심 안내] 현재 폴더의 .venv 안에 공식 HTTPS PyPI 패키지만 설치됩니다.
echo [안심 안내] API 키와 로그인 정보는 %%LOCALAPPDATA%%\VoiceCompanionData\ 에만 암호화/격리 저장됩니다.
echo.

echo [1/4] 64비트 Python 3.11~3.14 설치 여부를 확인하는 중...
set "PY_REJECT_INFO="
call :find_python
if defined SYS_PY_EXE goto :python_ready

echo.
echo [안내] 이 PC에 호환되는 64비트 Python 3.11~3.14가 설치되어 있지 않습니다.
if defined PY_REJECT_INFO call :echo_py_reject "[진단] 발견되었지만 호환되지 않은 Python:"

call :resolve_safe_cmd "py"
if not defined SAFE_CMD goto :try_winget_install
set "PY_MGR_CMD=%SAFE_CMD%"
call "%PY_MGR_CMD%" list >nul 2>&1
if errorlevel 1 goto :try_winget_install

echo Windows의 공식 Python 관리자에서 Python 3.12를 자동으로 설치할 수 있습니다.
"%SystemRoot%\System32\choice.exe" /C YN /M "공식 Python 3.12를 지금 자동 설치할까요"
if errorlevel 2 goto :python_manual_guide

echo.
echo [설치 중] py install 3.12 로 공식 Python 3.12를 설치하는 중입니다...
call "%PY_MGR_CMD%" install 3.12
call :find_python
if defined SYS_PY_EXE goto :python_ready
goto :try_winget_fallback

:try_winget_install
call :resolve_safe_cmd "winget"
if not defined SAFE_CMD goto :python_manual_guide
set "WINGET_CMD=%SAFE_CMD%"

echo Microsoft 공식 설치 도구인 winget으로 64비트 Python 3.12를 자동 설치할 수 있습니다.
"%SystemRoot%\System32\choice.exe" /C YN /M "공식 64비트 Python 3.12를 지금 자동 설치할까요"
if errorlevel 2 goto :python_manual_guide

:try_winget_fallback
call :resolve_safe_cmd "winget"
if not defined SAFE_CMD goto :python_manual_guide
set "WINGET_CMD=%SAFE_CMD%"
echo.
echo [설치 중] winget으로 공식 64비트 Python 3.12를 설치하는 중입니다. 약 30초~1분 걸립니다...
"%WINGET_CMD%" install -e --id Python.Python.3.12 --architecture x64 --accept-source-agreements --accept-package-agreements
call :find_python
if defined SYS_PY_EXE goto :python_ready

echo.
echo [추가 시도] Python 3.12가 인식되지 않아 공식 64비트 Python 3.13 설치를 시도합니다...
"%WINGET_CMD%" install -e --id Python.Python.3.13 --architecture x64 --accept-source-agreements --accept-package-agreements
call :find_python
if defined SYS_PY_EXE goto :python_ready

:python_manual_guide
echo.
if defined PY_REJECT_INFO call :echo_py_reject "[진단 정보]"
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

if "%REBUILD_VENV%"=="1" goto :recreate_venv
if exist "%VENV_PY%" goto :venv_ready
goto :create_venv

:recreate_venv
echo [2/4] 손상된 기존 .venv 폴더를 정리하는 중...
rmdir /s /q "%VENV_DIR%" >nul 2>&1
if exist "%VENV_PY%" goto :venv_failed

:create_venv
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
echo       공식 HTTPS PyPI (https://pypi.org/simple) 에서만 다운로드됩니다.
echo       완료될 때까지 창을 닫지 마세요.
echo.
"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 "%VENV_PY%" -m ensurepip --upgrade

"%VENV_PY%" -m pip install --index-url https://pypi.org/simple --no-input --upgrade pip
if errorlevel 1 echo [안내] pip 업그레이드는 건너뛰고 패키지 설치를 계속합니다.

"%VENV_PY%" -m pip install --index-url https://pypi.org/simple --no-input --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements.txt"
if not errorlevel 1 goto :check_gpu

echo.
echo [재시도] Windows 시스템 인증서 모드로 패키지 설치를 한 번 더 시도합니다...
"%VENV_PY%" -m pip install --index-url https://pypi.org/simple --no-input --use-feature=truststore --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements.txt"
if errorlevel 1 goto :pip_failed

:check_gpu
if "%VOICE_COMPANION_SKIP_CUDA%"=="1" goto :verify_imports
if not exist "%APP_DIR%requirements-cuda.txt" goto :verify_imports
"%VENV_PY%" -c "import importlib.metadata as m, sys; req = ('nvidia-cublas-cu12', 'nvidia-cudnn-cu12', 'nvidia-cuda-nvrtc-cu12'); ok = all(m.distribution(r) for r in req); sys.exit(0 if ok else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :cuda_already_installed
call :resolve_safe_cmd "nvidia-smi"
if not defined SAFE_CMD goto :verify_imports
"%SAFE_CMD%" >nul 2>&1
if errorlevel 1 goto :verify_imports

echo.
echo ========================================================================
echo   [NVIDIA 그래픽카드 감지!] GPU 초고속 음성 인식 가속을 켤 수 있습니다.
echo ========================================================================
echo   - Y 키: NVIDIA GPU 가속 설치 — 약 1.9GB 다운로드, 음성 인식 5배 빠름, 10초 뒤 자동 선택
echo   - N 키: 건너뛰고 지금 바로 CPU 모드로 실행 — 용량 절약, 바로 시작 가능
echo.
"%SystemRoot%\System32\choice.exe" /C YN /T 10 /D Y /M "NVIDIA GPU 가속 패키지를 설치할까요"
if errorlevel 2 goto :verify_imports

echo.
echo [GPU 가속 설치 중] NVIDIA CUDA 런타임 약 1.9GB를 다운로드하는 중입니다.
echo                     인터넷 속도에 따라 1~3분 걸릴 수 있으니 창을 닫지 마세요...
echo.
"%VENV_PY%" -m pip install --index-url https://pypi.org/simple --no-input --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements-cuda.txt"
if not errorlevel 1 goto :verify_imports
"%VENV_PY%" -m pip install --index-url https://pypi.org/simple --no-input --use-feature=truststore --prefer-binary --retries 5 --timeout 60 -r "%APP_DIR%requirements-cuda.txt"
if errorlevel 1 echo [안내] GPU 가속 설치를 건너뛰고 CPU 모드로 자동 동작합니다.
goto :verify_imports

:cuda_already_installed
echo [GPU 가속 확인] 기존 NVIDIA CUDA 런타임이 이미 설치되어 있어 재설치를 건너뜁니다.

:verify_imports
echo.
echo [4/4] 설치된 실행 모듈 최종 점검 중...
"%VENV_PY%" -c "import tkinter, numpy, scipy, httpx, h2, msgpack, requests, sounddevice, soundfile, pyperclip, faster_whisper; print('[확인 완료] 모든 필수 모듈 정상 로드!')"
if errorlevel 1 goto :verify_failed

"%VENV_PY%" -c "import hashlib, os, pathlib; p = pathlib.Path(os.environ['APP_DIR']) / 'requirements.txt'; f = pathlib.Path(os.environ['READY_FLAG']); val = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'installed'; f.write_text(val + '\n', encoding='utf-8')" >nul 2>&1
if errorlevel 1 echo installed> "%READY_FLAG%"
echo.
echo ========================================================================
echo   설치 완료! 잠시 후 MomoTalk Live 창이 열립니다.
echo   다음부터는 1_실행하기.bat 을 누르면 1초 만에 바로 열립니다.
echo ========================================================================
if "%VOICE_COMPANION_NO_LAUNCH%"=="1" exit /b 0
"%SystemRoot%\System32\timeout.exe" /t 2 /nobreak >nul

:launch_app
if "%VOICE_COMPANION_NO_LAUNCH%"=="1" exit /b 0
if exist "%VENV_PYW%" goto :launch_pyw
start "" "%VENV_PY%" "%APP_DIR%src\controller\aru_fish_bridge.py" %*
exit /b 0

:launch_pyw
start "" "%VENV_PYW%" "%APP_DIR%src\controller\aru_fish_bridge.py" %*
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
if defined SYS_PY_EXE exit /b 0
if exist "%LOCALAPPDATA%\Python" for /d %%D in ("%LOCALAPPDATA%\Python\*") do call :try_py_path "%%~fD\python.exe"
if defined SYS_PY_EXE exit /b 0
if exist "%LOCALAPPDATA%\Programs\Python" for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do call :try_py_path "%%~fD\python.exe"
if defined SYS_PY_EXE exit /b 0
for /d %%D in ("C:\Program Files\Python*") do call :try_py_path "%%~fD\python.exe"
if defined SYS_PY_EXE exit /b 0
call :scan_reg_python "HKCU\Software\Python\PythonCore"
if defined SYS_PY_EXE exit /b 0
call :scan_reg_python "HKLM\Software\Python\PythonCore"
exit /b 0

:scan_reg_python
for /f "usebackq tokens=1,2*" %%A in (`"%SystemRoot%\System32\reg.exe" query "%~1" /s /v ExecutablePath 2^>nul`) do call :accept_reg_python "%%A" "%%C"
exit /b 0

:accept_reg_python
if defined SYS_PY_EXE exit /b 0
if /I not "%~1"=="ExecutablePath" exit /b 0
if "%~2"=="" exit /b 0
call :try_py_path "%~2"
exit /b 0

:resolve_safe_cmd
set "SAFE_CMD="
for /f "usebackq delims=" %%P in (`"%SystemRoot%\System32\where.exe" $PATH:%~1 2^>nul`) do call :accept_safe_cmd "%%~fP"
exit /b 0

:accept_safe_cmd
if defined SAFE_CMD exit /b 0
if not exist "%~1" exit /b 0
call :check_not_in_app_dir "%~1"
if "%IN_APP_DIR%"=="1" exit /b 0
set "SAFE_CMD=%~1"
exit /b 0

:check_not_in_app_dir
set "IN_APP_DIR=0"
set "CHK_PATH=%~f1"
if not defined CHK_PATH exit /b 0
if /I "%CHK_PATH%"=="%APP_DIR%" set "IN_APP_DIR=1"
if "%IN_APP_DIR%"=="1" exit /b 0
if /I "%CHK_PATH%\"=="%APP_DIR%" set "IN_APP_DIR=1"
if "%IN_APP_DIR%"=="1" exit /b 0
set "CHK_DIR=%~dp1"
if /I "%CHK_DIR%"=="%APP_DIR%" set "IN_APP_DIR=1"
if "%IN_APP_DIR%"=="1" exit /b 0
:walk_parent_loop
if not defined CHK_DIR exit /b 0
if "%CHK_DIR:~-1%"=="\" set "CHK_DIR=%CHK_DIR:~0,-1%"
if not defined CHK_DIR exit /b 0
if /I "%CHK_DIR%\"=="%APP_DIR%" set "IN_APP_DIR=1"
if "%IN_APP_DIR%"=="1" exit /b 0
if "%CHK_DIR:~-1%"==":" exit /b 0
if "%CHK_DIR%"=="\" exit /b 0
for %%D in ("%CHK_DIR%") do set "NEXT_DIR=%%~dpD"
if /I "%NEXT_DIR%"=="%CHK_DIR%\" exit /b 0
if /I "%NEXT_DIR%"=="%CHK_DIR%" exit /b 0
set "CHK_DIR=%NEXT_DIR%"
goto :walk_parent_loop

:make_probe_file
set "PROBE_DIR="
set "PROBE_OUT="
set "PROBE_BASE=%LOCALAPPDATA%\Temp"
if not defined LOCALAPPDATA set "PROBE_BASE=%TEMP%"
if not defined PROBE_BASE exit /b 1
if not exist "%PROBE_BASE%" mkdir "%PROBE_BASE%" >nul 2>&1
set "PROBE_DIR=%PROBE_BASE%\ba_py_probe_%RANDOM%_%RANDOM%_%RANDOM%"
if exist "%PROBE_DIR%" set "PROBE_DIR=%PROBE_BASE%\ba_py_probe_%RANDOM%_%RANDOM%_%RANDOM%_r2"
if exist "%PROBE_DIR%" exit /b 1
mkdir "%PROBE_DIR%" 2>nul
if errorlevel 1 exit /b 1
set "PROBE_OUT=%PROBE_DIR%\probe.txt"
if exist "%PROBE_OUT%" goto :cleanup_probe_fail
exit /b 0

:cleanup_probe_fail
rmdir /s /q "%PROBE_DIR%" >nul 2>&1
set "PROBE_DIR="
set "PROBE_OUT="
exit /b 1

:try_py
set "TRY_PY_ARG=%~2"
for /f "usebackq delims=" %%P in (`"%SystemRoot%\System32\where.exe" $PATH:%~1 2^>nul`) do call :probe_cmd_candidate "%%~fP"
exit /b 0

:probe_cmd_candidate
if defined SYS_PY_EXE exit /b 0
if not exist "%~1" exit /b 0
call :check_not_in_app_dir "%~1"
if "%IN_APP_DIR%"=="1" exit /b 0
call :make_probe_file
if errorlevel 1 exit /b 0
call "%~1" %TRY_PY_ARG% -c "import importlib.util, os, sys; app = os.path.normcase(os.path.realpath(os.environ.get('APP_DIR', ''))).rstrip('\\/') + os.sep; exe = os.path.normcase(os.path.realpath(sys.executable)); errs = []; (sys.maxsize <= 2**32) and errs.append('32bit'); (not (sys.version_info[0] == 3 and 11 <= sys.version_info[1] <= 14)) and errs.append('ver=' + sys.version.split()[0]); (importlib.util.find_spec('tkinter') is None or importlib.util.find_spec('_tkinter') is None) and errs.append('no-tkinter'); (importlib.util.find_spec('venv') is None) and errs.append('no-venv'); exe.startswith(app) and errs.append('in-app-dir'); out = ('BA_PY_OK|' + sys.executable) if not errs else ('BA_PY_NO|' + sys.executable + ' (' + ','.join(errs) + ')'); open(os.environ['PROBE_OUT'], 'w', encoding='utf-8').write(out + '\n')" >nul 2>&1
call :read_probe
exit /b 0

:try_py_path
if defined SYS_PY_EXE exit /b 0
if not exist "%~1" exit /b 0
call :check_not_in_app_dir "%~1"
if "%IN_APP_DIR%"=="1" exit /b 0
call :make_probe_file
if errorlevel 1 exit /b 0
"%~1" -c "import importlib.util, os, sys; app = os.path.normcase(os.path.realpath(os.environ.get('APP_DIR', ''))).rstrip('\\/') + os.sep; exe = os.path.normcase(os.path.realpath(sys.executable)); errs = []; (sys.maxsize <= 2**32) and errs.append('32bit'); (not (sys.version_info[0] == 3 and 11 <= sys.version_info[1] <= 14)) and errs.append('ver=' + sys.version.split()[0]); (importlib.util.find_spec('tkinter') is None or importlib.util.find_spec('_tkinter') is None) and errs.append('no-tkinter'); (importlib.util.find_spec('venv') is None) and errs.append('no-venv'); exe.startswith(app) and errs.append('in-app-dir'); out = ('BA_PY_OK|' + sys.executable) if not errs else ('BA_PY_NO|' + sys.executable + ' (' + ','.join(errs) + ')'); open(os.environ['PROBE_OUT'], 'w', encoding='utf-8').write(out + '\n')" >nul 2>&1
call :read_probe
exit /b 0

:read_probe
set "PY_PROBE="
if not defined PROBE_OUT goto :cleanup_probe
if not exist "%PROBE_OUT%" goto :cleanup_probe
for /f "usebackq tokens=1* delims=|" %%A in ("%PROBE_OUT%") do call :parse_probe_line "%%A" "%%B"
:cleanup_probe
if defined PROBE_OUT del /f /q "%PROBE_OUT%" >nul 2>&1
if defined PROBE_DIR rmdir /s /q "%PROBE_DIR%" >nul 2>&1
set "PROBE_OUT="
set "PROBE_DIR="
if not defined PY_PROBE exit /b 0
if not exist "%PY_PROBE%" exit /b 0
call :check_not_in_app_dir "%PY_PROBE%"
if "%IN_APP_DIR%"=="1" exit /b 0
set "SYS_PY_EXE=%PY_PROBE%"
exit /b 0

:parse_probe_line
if "%~1"=="BA_PY_OK" set "PY_PROBE=%~2"
if "%~1"=="BA_PY_NO" if not defined PY_REJECT_INFO set "PY_REJECT_INFO=%~2"
exit /b 0

:echo_py_reject
setlocal EnableDelayedExpansion
echo %~1 !PY_REJECT_INFO!
endlocal
exit /b 0
