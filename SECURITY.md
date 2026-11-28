# 보안 및 개인정보 투명성 안내 (Security & Privacy Audit Guide)

이 프로그램은 **바이러스나 악성코드가 포함되지 않은 100% 공개 Python 소스 코드**로 배포됩니다.
실행 파일(`.exe`, `.dll`, `.sys`, `.pyd` 등)을 숨겨서 묶지 않았으므로, 의심되는 부분이 있다면 누구나 `src/` 폴더의 모든 코드를 직접 열어보거나 ChatGPT·Gemini 등에 붙여넣어 안전성을 검증할 수 있습니다.

---

## 1. 이 프로그램이 절대 하지 않는 일
- ❌ **기존 브라우저 쿠키·비밀번호 탈취 금지:** 사용자가 평소 쓰는 Chrome/Edge 프로필을 절대 읽거나 수정하지 않습니다.
- ❌ **개발자 서버로의 데이터 전송 금지:** API 키, 마이크 음성, 대화 기록을 개발자나 제3자 서버로 전송하지 않습니다.
- ❌ **게임 파일 변조 금지:** Steam 블루 아카이브 폴더는 프로필 초상화/음성 추출 시 **읽기 전용**으로만 접근합니다.
- ❌ **숨겨진 바이너리·드라이버 미포함:** 배포 패키지에 임의의 `.exe`, `.dll`, `.sys`, `.pyd` 파일이 포함되어 있지 않습니다. (첫 실행 시 사용자 PC의 공식 Python으로 공식 HTTPS PyPI 패키지만 `.venv`에 설치합니다.)

---

## 2. 적용된 핵심 보안 보호 장치 (Defense-in-Depth)

### A. API 키·OAuth 토큰 암호화 저장 (Windows DPAPI) 및 폴더 접근 제어 (ACL)
- **Windows DPAPI 디스크 암호화 (`src/utils/app_paths.py`):** 사용자가 입력한 Fish Audio API 키, **Fish Audio 공식 MCP OAuth 토큰(`fish_mcp_oauth_data`: `access_token`, `refresh_token`, 클라이언트 등록 정보)**, Gemini / Claude / OpenAI(ChatGPT) API 키, 그리고 선택적 Colab S2-Pro 서버 인증 토큰(`colab_s2pro_api_key`)은 `config.json`에 평문으로 저장되지 않고, Windows 표준 암호화 API(`CryptProtectData`, `CRYPTPROTECT_UI_FORBIDDEN`)를 통해 현재 Windows 사용자 계정에 귀속된 암호문(`dpapi:...`)으로 암호화되어 디스크에 저장됩니다. 다른 PC나 다른 Windows 계정으로 파일을 복사해도 복호화할 수 없습니다.
- **`%LOCALAPPDATA%` 사용자 전용 ACL 격리 (`harden_user_data_acl`):** 설정·API 키·OAuth 토큰·브라우저 프로필·대화 기록이 저장되는 `%LOCALAPPDATA%\VoiceCompanionData\` 폴더는 Windows `icacls` 접근 제어를 적용해 **현재 로그인한 본인 계정(및 Windows 시스템 권한)만** 접근하도록 제한합니다.
- **API 키 HTTP 헤더 전송 & 로그 자동 마스킹 (`redact_secrets`):**
  - Gemini(`x-goog-api-key`), Claude(`x-api-key`), OpenAI(`Authorization: Bearer`), Fish Audio API/MCP(`Authorization: Bearer`), 선택적 Colab S2-Pro(`Authorization: Bearer`) 호출 시 인증 토큰을 URL 쿼리(`?key=...`)가 아닌 HTTP 헤더로만 전송해 프록시 로그·스택 트레이스·URL 문자열 노출을 차단합니다.
  - 실행 로그(`bridge_runtime.log`), 오류 팝업, UI 로그 창에 기록되는 모든 예외/메시지는 `redact_secrets()`로 자동 필터링되어 `AIza...`, `sk-...`, `sk-ant-...`, `Bearer ...`, `x-goog-api-key`, `x-api-key`, `access_token`, `refresh_token`, `fish_mcp_oauth_data`, `colab_s2pro_api_key`, `dpapi:...` 등 민감 정보가 출력되지 않습니다.
- **Fish Audio 공식 MCP OAuth PKCE & 로컬 콜백 격리 (`src/controller/fish_mcp_backend.py`):**
  - Fish Audio 공식 MCP(`https://api.fish.audio/mcp`) 웹 크레딧 로그인 시 표준 OAuth 2.1 + PKCE(`S256`) + 동적 클라이언트 등록(RFC 7591)만 사용하며, 인증 콜백 서버는 오직 **`127.0.0.1` 임시 포트(`/callback`) 로컬 루프백**에만 바인딩되고 CSRF `state` 검증 후 로그인 완료 즉시 종료됩니다.
  - MCP가 반환한 합성 오디오 다운로드 URL(`_validate_audio_download_url`)은 반드시 **`https://` 표준 443 포트 + TLS 검증(`verify=True`)**, 모든 리다이렉트 홉(최대 3회)에 대한 **DNS/IP 기반 SSRF 차단(로컬호스트·사설망·링크로컬·비공인 IP 거부)** 및 **32 MB 바이트 크기 상한** 검증을 통과한 경우에만 다운로드합니다.
- **선택적 사용자 Colab S2-Pro 서버 엔드포인트 검증 (`src/controller/colab_s2pro_backend.py`):**
  - 사용자가 별도로 본인 Google Colab S2-Pro 서버를 연결할 경우에만 활성화되며(기본값 비활성, 개인 Drive 경로나 개인 노트북 ID 하드코딩 0건), 서버 URL은 **공개 `https://` 호스트(사설/루프백/링크로컬 IP 리터럴 및 DNS 해석 결과 내부 IP에 대한 SSRF 차단 적용)** 또는 로컬 SSH 터널(`http://127.0.0.1` / `localhost`)만 허용합니다.
  - 스트리밍 WAV 응답에 **64 KB 헤더 상한**, **16-bit PCM 포맷·채널 검증**, **32 MB 단일 스트림 크기 상한**을 적용하고 모든 HTTP 오류 메시지는 `redact_secrets()`로 마스킹합니다.

### B. 브라우저 CDP 출처 잠금 (Origin Lockdown) 및 DOM/네트워크 보호
- **CDP 원격 출처 차단 (`--remote-allow-origins=*` 제거):** 전용 Edge/Chrome 실행 시 위험한 `--remote-allow-origins=*` 옵션을 완전히 제거하고 **`--remote-debugging-address=127.0.0.1`** 로 로컬 루프백에만 바인딩합니다. 이로써 사용자가 평소 쓰는 일반 브라우저의 악성 웹사이트가 WebSocket CSRF나 DNS 리바인딩으로 로컬 디버깅 포트를 탈취하는 공격을 차단합니다.
- **로컬 WebSocket 프레임 크기 제한 & TLS 강제:** 로컬 CDP 클라이언트(`LocalCDPClient`)에 최대 프레임/메시지 크기 상한을 적용해 메모리 고갈(OOM DoS)을 방지하며, 모든 외부 API 통신은 TLS 검증(`verify=True`)이 활성화된 **HTTPS 전용**으로만 수행합니다.
- **`selectors.json` 도메인 검증 및 JS 직렬화:** 브라우저 자동화 대상 URL은 엄격히 `https://gemini.google.com/...`, `https://claude.ai/...`, `https://chatgpt.com/...` 공식 도메인만 허용하며, DOM 셀렉터와 프롬프트 텍스트는 항상 `json.dumps()`로 안전하게 직렬화해 스크립트 인젝션을 차단합니다.
- **PowerShell 명령어 인젝션 차단 (`src/controller/proactive_lead_engine.py`):** 보조 모드(VB-Cable)의 로컬 TTS 호출 시 사용자/대화 문구를 PowerShell 명령줄에 직접 문자열 보간(`-Command`)하지 않고 환경 변수 전달 방식으로 격리하며, 출력 파일 경로가 캐시 디렉터리 밖으로 벗어나지 않도록 검증합니다.

### C. 공급망·런처 보안 (`1_실행하기.bat`, `2_코랩_실행하기.bat`, `tools/colab_s2pro_launcher.py` & 의존성 버전 상한 고정)
- **공식 HTTPS PyPI 고정 및 환경 변수 초기화:** `1_실행하기.bat`과 `2_코랩_실행하기.bat`은 모두 Python/pip 실행 전에 `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_TRUSTED_HOST`, `PIP_NO_INDEX`, `PIP_CONSTRAINT` 등 하이재킹 위험 환경 변수를 모두 초기화하고, 모든 `pip install`에 `--index-url https://pypi.org/simple --no-input` 옵션을 강제합니다.
- **현재 폴더(CWD) 바이너리 심기/DLL 하이재킹 방지:** 두 배치 런처 모두 `NoDefaultCurrentDirectoryInExePath=1`을 설정하고 `%SystemRoot%\System32\chcp.com`, `where.exe`, `choice.exe`, `timeout.exe`를 절대 경로로 호출합니다. 또한 `where.exe $PATH:...` 검색 및 경로 검증을 통해 프로그램 폴더(`%APP_DIR%`) 안에 심어진 가짜 `python.exe`·`py.exe` 등은 절대 실행하지 않으며, 임시 프로브 파일도 사용자 전용 `%LOCALAPPDATA%\Temp` 하위의 고유 디렉터리에서만 안전하게 생성·즉시 삭제합니다.
- **Colab 자동 연결 런처 검증 (`tools/colab_s2pro_launcher.py`):** 노트북 URL은 반드시 `https://colab.research.google.com/...` 공식 도메인만 허용하고, 서버 URL은 SSRF 차단 검증(`validate_colab_server_url`)을 통과한 주소만 저장·연결합니다. 개인 Drive ID나 개인 경로는 일절 하드코딩되지 않습니다.
- **주 버전 상한 고정 (`requirements.txt`, `requirements-cuda.txt`):** 모든 패키지에 주 버전 상한(예: `>=1.1.0,<2.0.0`, `>=1.26.0,<3.0.0`)을 명시해 향후 예기치 않은 호환성 파괴 또는 오염된 신규 메이저 버전 자동 설치 위험을 줄였습니다.

### D. 압축 폭탄(Decompression Bomb) 및 경로 탐색(Path Traversal) 방어
- **바이너리/에셋 추출기 보호 (`src/extractor/`):** PNG 디코더(`decode_png_rgba`)에 최대 해상도 및 `zlib.decompressobj` 최대 전개 바이트 상한(`max_length`)을 적용하고 UnityFS/LZ4 블록 할당 크기를 제한해 압축 폭탄을 차단합니다. `.molru` 및 캐시 파일 저장 시 `../` 등 경로 이탈(Zip-Slip / Path Traversal)을 엄격히 차단합니다.
- **대화 기억·보이스 ID 검증 (`memory_manager.py`, `aru_fish_bridge.py`, `fish_batch_builder.py`):** 캐릭터 ID·세션 ID 경로 이탈을 차단하고, Fish Audio 모델 ID는 32자리 16진수 형식(`^[0-9a-fA-F]{32}$`) 검증 통과 후에만 API URL 및 로컬 캐시 경로에 사용합니다.

### E. 배포 전 자동 정적 보안 감사 게이트 (릴리스 빌드 검증)
- 모든 공식 공개 릴리스 빌드는 패키징 단계의 자동 정적 보안 감사를 통과해야만 생성됩니다:
  - API 키·DPAPI 암호문·개인 로컬/Google Drive 경로 및 개인 Drive ID 유출 차단
  - 실행/바이너리 파일(`.exe`, `.dll`, `.sys`, `.pyd` 등) 포함 차단
  - 위험 코드 패턴 자동 차단: `--remote-allow-origins=*`, `verify=False`, `shell=True`, `eval(`, `exec(`, `pickle.load`, `SafeLoader` 없는 `yaml.load(`, 비-로컬 `http://` URL, `?key=` API URL, PowerShell `-Command` 동적 문자열 보간, `.bat` 파일의 UTF-8 BOM 및 멀티라인 `if (` 블록

---

## 3. 이 프로그램이 사용하는 권한 및 네트워크 주소 전체 목록

### A. 네트워크 통신 도메인 (전수 목록)
| 도메인 | 용도 | 관련 코드 |
| --- | --- | --- |
| `api.fish.audio` / `fish.audio` | 사용자가 입력한 Fish Audio API 키 또는 공식 MCP OAuth(`https://api.fish.audio/mcp`) 웹 크레딧으로 캐릭터 TTS 음성 생성 및 보이스 모델 조회 | `src/controller/aru_fish_bridge.py`, `src/controller/fish_mcp_backend.py`, `src/controller/fish_batch_builder.py` |
| Fish Audio MCP가 반환하는 오디오 다운로드 호스트 (`https://` 전용 · 공인 IP) | 공식 MCP `text_to_speech`가 반환한 합성 오디오 파일 다운로드 (TLS `verify=True`, 매 리다이렉트 홉 DNS/IP SSRF 차단, 32 MB 크기 상한 적용) | `src/controller/fish_mcp_backend.py` |
| `colab.research.google.com` 및 사용자가 직접 입력한 Colab S2-Pro 서버 (`https://` 공인 호스트 또는 로컬 `127.0.0.1` · 선택 기능 · 기본 비활성) | 고급 사용자가 `2_코랩_실행하기.bat` 또는 설정 창에서 본인 Google Colab S2-Pro 서버를 연결한 경우에만 노트북 열기 및 TTS 음성 생성에 사용 (공식 Colab 도메인 검증, DNS/IP SSRF 차단, 64 KB WAV 헤더·32 MB 스트림 상한, Bearer 토큰 마스킹 적용) | `src/controller/colab_s2pro_backend.py`, `tools/colab_s2pro_launcher.py`, `2_코랩_실행하기.bat` |
| `gemini.google.com` | 제미니 웹 모드 사용 시 독립 브라우저 창에서 대화 송수신 | `src/controller/gemini_connector.py` |
| `generativelanguage.googleapis.com` | 제미니 API 모드 사용 시 사용자가 입력한 API 키(`x-goog-api-key` 헤더)로 대화 요청 | `src/controller/gemini_connector.py` |
| `claude.ai` | 클로드 웹 모드 사용 시 독립 브라우저 창에서 대화 송수신 | `src/controller/gemini_connector.py` |
| `api.anthropic.com` | 클로드 API 모드 사용 시 사용자가 입력한 API 키(`x-api-key` 헤더)로 대화 요청 | `src/controller/gemini_connector.py` |
| `chatgpt.com` | 챗지피티 웹 모드 사용 시 독립 브라우저 창에서 대화 송수신 | `src/controller/gemini_connector.py` |
| `api.openai.com` | 챗지피티(OpenAI) API 모드 사용 시 사용자가 입력한 API 키(`Authorization: Bearer` 헤더)로 대화 요청 | `src/controller/gemini_connector.py` |
| `translate.googleapis.com` / `clients5.google.com` | 빠른 자막 번역(`fast_nmt`) 선택 시 일본어→한국어 자막 번역 | `src/controller/subtitle_translator.py` |
| `pypi.org` / `files.pythonhosted.org` | 최초 1회 실행 시 `1_실행하기.bat`이 공식 HTTPS PyPI 패키지 설치 | `1_실행하기.bat`, `requirements*.txt` |
| `huggingface.co` | 최초 1회 로컬 음성 인식 모델(`faster-whisper-small`) 다운로드 | `faster-whisper` 라이브러리 |
| `127.0.0.1` (로컬 전용) | ① 프로그램이 직접 띄운 전용 Edge/Chrome 창 제어용 로컬 CDP 통신(원격 출처 차단) ② Fish Audio 공식 MCP OAuth 로그인 시 임시 루프백 콜백 수신(`http://127.0.0.1:<port>/callback`, 완료 즉시 종료) | `src/controller/gemini_connector.py`, `src/controller/fish_mcp_backend.py` |

### B. 브라우저 실행 방식 (`BrowserProfile` 격리)
- AI 웹 로그인 모드(Gemini · Claude · ChatGPT) 선택 시, 사용자 PC에 이미 설치된 공식 Edge 또는 Chrome을 **`--user-data-dir=%LOCALAPPDATA%\VoiceCompanionData\BrowserProfile`** 및 **`--remote-debugging-address=127.0.0.1`** 옵션으로 실행합니다.
- 기존 브라우저와 완전히 분리된 별도 폴더이므로 기존 로그인 정보·북마크·쿠키에 접근하지 않습니다.
- 전용 창을 닫거나 위치를 제어하기 위해 Windows 표준 Win32 API(`ctypes` `CreateToolhelp32Snapshot`, `EnumWindows`)만 사용합니다.

### C. 로컬 파일 저장 위치
- 프로그램 폴더에는 어떠한 개인정보도 저장되지 않습니다.
- 모든 설정·암호화된 API 키/OAuth 토큰·브라우저 로그인 세션·로컬 TTS 오디오 캐시·대화 기억·로그는 오직 Windows 표준 사용자 경로인 아래 폴더에만 저장됩니다:
  ```text
  %LOCALAPPDATA%\VoiceCompanionData\
  ```
- 프로그램을 삭제하거나 개인정보를 완전히 지우고 싶다면 위 폴더만 삭제하면 흔적이 남지 않습니다.

### D. 선택적 Windows 기능 안내
- **마이크 접근:** 실시간 음성 대화(모드 1)에서 사용자의 목소리를 내 PC 내부의 `faster-whisper`로 텍스트 변환하기 위해 사용합니다.
- **Windows 기본 TTS (`System.Speech`):** 보조 모드(모드 2 · VB-Cable)에서 "침묵 시 먼저 말 걸기" 트리거 음성을 API 비용 없이 만들기 위해 `src/controller/proactive_lead_engine.py`에서 Windows 내장 음성 합성 기능을 호출합니다. (기본 모드 1에서는 사용하지 않습니다.)

---

## 4. 직접 무결성 검증하는 방법
1. **SHA-256 해시 확인 (PowerShell):**
   ```powershell
   Get-FileHash .\BlueArchive_VoiceCompanion_Release.zip -Algorithm SHA256
   ```
   배포 페이지의 `SHA256SUMS.txt` 값과 일치하는지 확인하세요.
2. **소스 코드 직접 점검:**
   - 모든 실행 코드는 `src/` 및 `tools/colab_s2pro_launcher.py`의 일반 텍스트(`.py`) 파일입니다.
   - `1_실행하기.bat`과 `2_코랩_실행하기.bat` 역시 메모장으로 열어 환경 변수 초기화와 공식 PyPI 설치 명령(`pip install --index-url https://pypi.org/simple ...`)을 한 줄씩 직접 확인할 수 있습니다.
3. **숨겨진 바이너리 미포함 직접 확인 (PowerShell — 압축 해제 폴더 기준):**
   ```powershell
   Get-ChildItem -Recurse -File -Include *.exe,*.dll,*.sys,*.pyd,*.msi,*.vbs,*.ps1
   ```
   *(※ `.venv` 생성 전에 압축을 푼 직후 실행하면 결과가 0건으로 출력되어야 정상입니다. 공식 릴리스는 배포 전 내부 정적 보안 감사 게이트로 시크릿 유출, 위험 패턴, 배치 런처 무결성을 자동 검증한 뒤 패키징되며, 개발자 전용 패키징 스크립트는 공개 배포판 및 공개 저장소에 포함되지 않습니다.)*
