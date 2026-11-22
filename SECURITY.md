# 보안 및 개인정보 투명성 안내 (Security & Privacy Audit Guide)

이 프로그램은 **바이러스나 악성코드가 포함되지 않은 100% 공개 Python 소스 코드**로 배포됩니다.
실행 파일(`.exe`, `.dll`, `.sys` 등)을 숨겨서 묶지 않았으므로, 의심되는 부분이 있다면 누구나 `src/` 폴더의 모든 코드를 직접 열어보거나 ChatGPT·Gemini 등에 붙여넣어 안전성을 검증할 수 있습니다.

---

## 1. 이 프로그램이 절대 하지 않는 일
- ❌ **기존 브라우저 쿠키·비밀번호 탈취 금지:** 사용자가 평소 쓰는 Chrome/Edge 프로필을 절대 읽거나 수정하지 않습니다.
- ❌ **개발자 서버로의 데이터 전송 금지:** API 키, 마이크 음성, 대화 기록을 개발자나 제3자 서버로 전송하지 않습니다.
- ❌ **게임 파일 변조 금지:** Steam 블루 아카이브 폴더는 프로필 초상화/음성 추출 시 **읽기 전용**으로만 접근합니다.
- ❌ **숨겨진 바이너리·드라이버 미포함:** 배포 패키지에 임의의 `.exe`, `.dll`, `.sys` 파일이 포함되어 있지 않습니다. (첫 실행 시 사용자 PC의 공식 Python으로 PyPI 공식 패키지만 `.venv`에 설치합니다.)

---

## 2. 이 프로그램이 사용하는 권한 및 네트워크 주소 전체 목록

### A. 네트워크 통신 도메인 (전수 목록)
| 도메인 | 용도 | 관련 코드 |
| --- | --- | --- |
| `api.fish.audio` / `fish.audio` | 사용자가 입력한 Fish Audio API 키로 캐릭터 TTS 음성 생성 및 보이스 모델 조회 | `src/controller/aru_fish_bridge.py`, `src/controller/fish_batch_builder.py` |
| `gemini.google.com` | 제미니 웹 모드 사용 시 독립 브라우저 창에서 대화 송수신 | `src/controller/gemini_connector.py` |
| `generativelanguage.googleapis.com` | 제미니 API 모드 사용 시 사용자가 입력한 API 키로 대화 요청 | `src/controller/gemini_connector.py` |
| `translate.googleapis.com` / `clients5.google.com` | 빠른 자막 번역(`fast_nmt`) 선택 시 일본어→한국어 자막 번역 | `src/controller/subtitle_translator.py` |
| `huggingface.co` | 최초 1회 로컬 음성 인식 모델(`faster-whisper-small`) 다운로드 | `faster-whisper` 라이브러리 |
| `127.0.0.1` (로컬 전용) | 프로그램이 직접 띄운 전용 Edge/Chrome 창 제어용 로컬 CDP 통신 | `src/controller/gemini_connector.py` |

### B. 브라우저 실행 방식 (`BrowserProfile` 격리)
- 제미니 웹 로그인 모드 선택 시, 사용자 PC에 이미 설치된 공식 Edge 또는 Chrome을 **`--user-data-dir=%LOCALAPPDATA%\VoiceCompanionData\BrowserProfile`** 옵션으로 실행합니다.
- 기존 브라우저와 완전히 분리된 별도 폴더이므로 기존 로그인 정보·북마크·쿠키에 접근하지 않습니다.
- 전용 창을 닫거나 위치를 제어하기 위해 Windows 표준 Win32 API(`ctypes` `CreateToolhelp32Snapshot`, `EnumWindows`)만 사용합니다.

### C. 로컬 파일 저장 위치
- 프로그램 폴더에는 어떠한 개인정보도 저장되지 않습니다.
- 모든 설정·API 키·브라우저 로그인 세션·대화 기억·로그는 오직 Windows 표준 사용자 경로인 아래 폴더에만 저장됩니다:
  ```text
  %LOCALAPPDATA%\VoiceCompanionData\
  ```
- 프로그램을 삭제하거나 개인정보를 완전히 지우고 싶다면 위 폴더만 삭제하면 흔적이 남지 않습니다.

### D. 선택적 Windows 기능 안내
- **마이크 접근:** 실시간 음성 대화(모드 1)에서 사용자의 목소리를 내 PC 내부의 `faster-whisper`로 텍스트 변환하기 위해 사용합니다.
- **Windows 기본 TTS (`System.Speech`):** 보조 모드(모드 2 · VB-Cable)에서 "침묵 시 먼저 말 걸기" 트리거 음성을 API 비용 없이 만들기 위해 `src/controller/proactive_lead_engine.py`에서 Windows 내장 음성 합성 기능을 호출합니다. (기본 모드 1에서는 사용하지 않습니다.)

---

## 3. 직접 무결성 검증하는 방법
1. **SHA-256 해시 확인 (PowerShell):**
   ```powershell
   Get-FileHash .\BlueArchive_VoiceCompanion_Release.zip -Algorithm SHA256
   ```
   배포 페이지의 `SHA256SUMS.txt` 값과 일치하는지 확인하세요.
2. **소스 코드 직접 점검:**
   - 모든 실행 코드는 `src/` 폴더의 일반 텍스트(`.py`) 파일입니다.
   - `1_실행하기.bat` 역시 메모장으로 열어 설치 명령(`pip install -r requirements.txt`)을 한 줄씩 직접 확인할 수 있습니다.
