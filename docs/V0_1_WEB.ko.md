# v0.1.1 로컬 웹 파일럿

기준일: 2026-10-01, Asia/Seoul. 개발 브랜치: `feature/web-workbench-v0.1`.

foundation의 업무 모델·SQLite·JSON API를 유지하고 웹 화면과 한 번 설정하는 OpenAI 키 보관 기능을 추가했다. 실제 OpenAI 호출·메일 연결은 아직 포함하지 않는다.

## 실행과 업무 확인

Python 3.12 이상을 설치하고 이 브랜치를 받은 폴더에서 실행한다. 실행용 외부 Python 패키지나 Node 설치, 웹 빌드는 필요 없다.

Windows PowerShell:

```powershell
python -m ariadne --db "$env:LOCALAPPDATA\ARIADNE\demo-web.sqlite3" serve --demo
```

Linux/macOS의 웹 기능 확인:

```bash
python -m ariadne --db /tmp/ariadne-demo-web.sqlite3 serve --demo
```

<http://127.0.0.1:8765>를 열면 가상 업무가 나타난다. 같은 경로로 재실행하면 업무·근거·변경 이력이 보존된다. `--demo`는 동일 가상 자료를 중복 생성하지 않는다. 빈 업무 목록으로 시작하려면 별도 DB 경로를 지정하고 `--demo`를 생략한다. 서버는 터미널에서 `Ctrl+C`로 종료한다. Linux 예시의 `/tmp`는 임시 경로다.

1. 왼쪽 목록에서 업무를 선택하고 검색·펀드·상태 필터를 사용한다.
2. 상세에서 제목·담당자·상태·대기 사유를 수정하고 기록자와 변경 사유를 입력해 저장한다.
3. 근거 탭에서 출처·원문 위치·근거 종류·초안 여부를 확인하고 이력에서 변경 전후 값을 확인한다.
4. 오래된 창에서 저장하면 충돌을 표시한다. 최신 내용 불러오기를 누르면 미저장 입력을 별도 복사 가능한 텍스트로 보존한다.
5. 새 업무에는 요청 근거·위치·완료조건을 함께 입력한다. 입력한 기한은 확인 전 예상값이다.
6. 수정 제안은 사용자가 내용을 검토하고 사유를 입력해 수락·거절한다. 수락에는 업무 버전 검사를 적용한다.

메일 발송·첨부 수령만으로 완료 처리하지 않는다. 모든 완료조건에 검토된 최종 증빙이 연결돼야 서버에서 완료를 허용한다. 이 버전의 화면에는 완료 증빙을 검토·연결하는 편집 기능이 없으며, 데모의 발송 근거만으로는 완료할 수 없다. 근거 위치는 안전한 텍스트로 표시하며 원본 파일을 자동 실행하지 않는다.

## OpenAI 키를 한 번만 설정하기

Windows에서 웹 화면의 **연결 설정**을 열고 OpenAI API 키를 저장한다. 일반적인 OpenAI API 키 방식은 키 한 개를 사용한다. 다른 서비스의 앱 키·시크릿 쌍을 매번 입력하는 화면으로 만들지 않았다.

- 저장: 현재 Windows 사용자의 자격 증명 관리자에 `ariadne/openai/default` 항목으로 보관한다.
- 재실행: 같은 PC·Windows 계정이면 저장 상태를 다시 확인하고 서버 코드에서 기존 키를 불러올 수 있다.
- 교체: 설정에서 새 키를 저장하면 기존 항목을 교체한다.
- 삭제: 설정의 삭제 동작으로 OS 저장 항목을 제거한다. 공급자 측 키 폐기와는 별개다.

키를 저장해도 유료 API 호출이나 인증 테스트를 실행하지 않는다. 화면에는 **저장 여부**만 표시하며 기존 키나 일부 문자를 다시 보여주지 않는다. 입력칸은 요청을 보낸 직후 비우고 브라우저 저장소·SQLite·환경 파일·Git에 키를 남기지 않는다. 현재 지원 대상은 Windows다. 다른 OS 또는 저장소 접근 실패 시 미지원/오류를 표시하고 평문 저장으로 대체하지 않는다.

DB 백업에 키는 포함되지 않는다. PC·Windows 계정 변경, 키 삭제·폐기 때는 다시 설정한다. OS 저장소는 같은 사용자로 실행되는 다른 프로그램과의 보안 격리를 제공하지 않는다. 업무 UI와 API도 로컬 개인 파일럿이며 `actorId`는 감사용 이름이지 로그인 신원이 아니다.

Outlook은 별도 OAuth 연결이다. 최초 로그인 후 토큰 안전 보관·자동 갱신은 v0.3에서 구현할 예정이며, 권한 회수나 정책 변경 시 재로그인이 필요하다.

## 현재 API와 코드 경계

| 경로·메서드 | 역할 |
| --- | --- |
| `GET /`, `/assets/app.css`, `/assets/app.js` | 패키지에 포함한 허용된 웹 파일만 제공 |
| 기존 `/api/work-items`, `/api/evidence`, `/api/proposals` | 같은 저장소·업무 규칙을 사용하는 조회·변경 |
| `GET /api/connections/openai` | `supported`, `configured`, `backend`, `message`만 반환 |
| `POST /api/connections/openai` | `{ "apiKey": "입력값" }` 저장·교체 |
| `DELETE /api/connections/openai` | 저장한 키 삭제 |

키 조회 HTTP 경로는 없다. 후속 OpenAI 어댑터는 서버 안에서 `CredentialService.get_openai_key()`를 호출한다. 외부 호출 코드에 키를 다시 입력받거나 클라이언트에 돌려주는 흐름을 추가하지 않는다. 현재 업무 DB는 schema 1이며 이번 변경에서 마이그레이션하지 않았다. 기존 Godot API 경로도 유지한다.

## 검증 기록

- 기존 42개 테스트를 포함한 Python 65개: Linux/Python 3.12에서 **64개 통과, Windows 전용 1개 제외**.
- 실제 HTTP 요청으로 정적 자원·출처 검사·기존 API·키 저장/교체/삭제·비노출·미지원 오류를 확인했다. 자격증명 HTTP 테스트는 가상 메모리 저장소만 사용했다.
- wheel 패키지를 빌드·별도 위치에 풀어 HTML/CSS/JS 세 자원 제공을 확인했다.
- Godot 소스 정합성 검사를 통과했다. 로컬 Godot 엔진 실행 검증은 하지 않았다.
- Chromium 인수 스크립트에 목록·저장·완료 차단·두 창 충돌·안전한 텍스트 출력·필터·수기 등록·제안 검토·키 상태·서버 재시작의 10개 흐름을 추가했다. 로컬은 Chromium 설치 파일을 확보하지 못해 실행하지 못했다.
- GitHub Actions에서 Linux/Windows × Python 3.12~3.14, Windows 가상 키 OS 저장, Chromium, 기존 Godot 검증을 실행하도록 구성했다. 이 문서 작성 시 원격 결과는 대기 중이며 해당 커밋의 Actions 결과로 판정한다.

수동 재현:

```bash
python -m unittest discover -s tests -v
python scripts/check_godot.py
```

브라우저 검증 개발 환경만 Playwright와 Chromium을 추가로 설치한다. 설치 명령과 고정 버전은 `.github/workflows/foundation.yml`의 `web` 작업을 기준으로 하고 `node scripts/web_smoke.mjs`를 실행한다. 실제 회사 자료·계정 키·유료 AI 호출은 테스트에 사용하지 않는다.

## 다음 개발

MSG/EML 가져오기·정규화·중복 식별과 정답셋을 먼저 만들고, 저장된 키를 사용하는 서버 측 OpenAI 추출 어댑터를 연결한다. 정식 OpenAPI·스키마 통합, 완료 근거 검토 화면, 회사 PC의 한글 IME·사용성 확인은 남은 v0.1 작업이다. 팀 운영·상시 동기화·문서 제출 자동화가 준비된 버전으로 표시하지 않는다.
