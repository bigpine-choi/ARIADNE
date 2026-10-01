# 공유 폴더와 로컬 주간보고

기준일: 2026-10-01, Asia/Seoul. 구현과 운영 활성화를 구분한다.

이번 연결은 수집 시 MCP·플러그인이나 Microsoft Graph에 의존하지 않는다.
기존 `feature/week1-foundation`의 Python 업무 모델·SQLite·HTTP 서버를
재사용하고 같은 서버에 한국어 웹 화면을 연결했다. 업무 DB의 schema 1은
유지하고 원천 버전·보고 실행은 별도 `automation.sqlite3`에 보관한다.
Godot 클라이언트는 기존 브랜치에 유지하며 이번 범위에서 새로 복제하지 않는다.

## 수집 방식과 실제 확인

| 원천 | 선택한 방식 | 확인한 범위 |
| --- | --- | --- |
| 공유 폴더 | UNC 경로 재귀 스캔, SHA-256 비교, SQLite BLOB 원본 보관 | 실제 Excel 42개·PDF 17개. 최초 59개 저장, 재실행 변경 0개, 보관 바이트와 원본 전부 일치 |
| 메일 | 기존 Windows Outlook COM, 별도 STA 프로세스 | 본인 사서함의 수신 81건·발신 29건, UTC 시각 검사, 110건 재수집 중복 저장 0건 |
| Teams | 현재 열린 지정 대화에서 공식 메시지 이동 단축키와 메시지 복사 | 실제 대화 본문 복사와 이전 메시지 이동 확인. UI 접근성 텍스트는 반환되지 않음 |

실제 업무 원문·DB·공유 주소·사서함·Teams 표본은 소스 저장소 밖에만
보관했다. Git에는 코드, 일반화한 연결 설명, 가상 테스트만 포함한다.
표의 개수는 개발 당일의 제한된 관찰이며 전 사서함의 전체성 평가가 아니다.

### 파일 버전

웹 화면의 `변경 파일 저장` 버튼은 설정된 폴더만 읽는다. 처음에는 모든
파일을 저장하고 이후 신규·내용 변경·삭제 상태를 기록한다. 같은 내용은
중복 저장하지 않는다. 수정일과 크기가 같은 변경도 내용 해시로 구분한다.
삭제는 이전 버전의 삭제가 아니라 원천에서 사라졌다는 표시다.
버전 목록에서 원래 확장자·이름으로 보관본을 내려받을 수 있다.

네트워크 단절·잠긴 파일·읽는 중 변경·용량 초과가 발생하면 해당 스캔 전체를
취소하고 이전 상태를 유지한다. 일부 폴더 읽기 실패를 파일 삭제로 처리하지
않는다. 읽기와 확정을 하나의 SQLite 쓰기 트랜잭션으로 직렬화한다.
한 파일 64 MiB, 한 스캔 512 MiB 제한이 있다. Excel 임시 파일과 링크·정션은
수집에서 제외한다. 버튼 클릭 사이에 생겼다가 사라진 중간 버전은 복원할 수 없다.
DB에는 기본 암호화가 없으므로 회사에서 허용한 저장 위치·백업 정책으로 운영한다.

### Outlook

회사 관리자 API 승인 없이 현재 로그인한 classic Outlook 프로필을 사용한다.
본인 SMTP 주소와 정확히 일치하는 계정 하나의 받은·보낸 편지함 및 그 하위
폴더만 수집한다. 다른 공유 사서함은 자동으로 포함하지 않는다.
UTC DASL 필터와 정확한 기간 검사를 사용한다. StoreID와 EntryID로 구분하며
InternetMessageID도 함께 보관한다. 폴더 이동으로 ID가 바뀌거나 다른
사서함에 같은 메일이 있는 경우 완전한 동일성 병합을 보장하지 않는다.
임의 원본 이동·삭제·읽음 상태 변경을 하지 않는다.

본문은 일반 텍스트로 수집하고 첨부 개수만 보존한다. 첨부 내용 분석은 이번
범위에 없다. 100,000자를 초과한 본문이나 읽기 실패는 불완전 수집으로 표시한다.
자동분류 규칙으로 받은 편지함 밖 루트 폴더에 이동된 메일, 로컬 캐시 밖의
메일, 보관 사서함까지 수집했다고 가정하지 않는다.
회사 Outlook의 프로그램 접근 경고가 나오면 보안을 낮추거나 우회하지 않는다.

### Teams의 제한

Outlook과 같은 메시지 컬렉션 COM 인터페이스를 확인하지 못했다. 이번 PC에서
접근성 텍스트가 비어 있어, Ctrl+M으로 메시지 영역 이동, Ctrl+J로 최신 위치
이동, Ctrl+C로 본문 복사, 위쪽 방향키로 이전 메시지 이동하는 로컬 수집기를
추가했다. **COM과 같은 완전한 이력 수집기는 아니다.**

현재 Teams가 전면에 열려 있고 창 제목의 대화명이 설정 목록과 정확히 일치할
때만 실행한다. 최대 100개 메시지를 읽으며 창·대화가 바뀌면 중단한다. 메시지
입력·붙여넣기·Enter·전송 키를 사용하지 않는다. 클립보드의 기존 일반 텍스트
형식을 복원하며 이미지 등 복원할 수 없는 형식이 들어 있으면 수집을 시작하지
않는다. 복사가 실패하면 이전 클립보드 내용을 대화로 가져오지 않는다.

복사본에는 작성자·발송 날짜가 빠지고 가상화된 과거 이력이나 다른 대화가
누락될 수 있다. 같은 본문의 사본은 같은 스냅샷으로 식별하며 서로 다른 실제
메시지인지 단정하지 않는다. 작성 시각은 `null`, 관찰 시각은 별도이며 주간
실적으로 자동 확정하지 않는다. 이 경로는 반드시 불완전 수집 상태를 반환한다.
개별 메시지 복사·이동은 이 PC에서 확인했고, Win32 수집기 전체를 지속 실행해
100건 이력·작성자·날짜 정확도를 검증한 것은 아니다.

완전한 주간 Teams 이력이 필수라면 승인된 내보내기 또는 Graph 연결, 또는
회사 정책에 맞는 별도 수집 경로를 확보해야 한다. 숨겨진 캐시·세션 토큰·회사
관리자 영역을 직접 읽는 방식은 구현하지 않았다.

## 실행

Python 3.12 이상과 classic Outlook이 필요하다. Excel 생성에 `openpyxl`을
사용한다. Outlook COM과 기본 Teams 복사기는 추가 Microsoft SDK가 필요 없다.
선택적인 접근성 TextPattern 경로만 `pywinauto`를 사용한다.

```powershell
python -m pip install '.[automation]'
```

`examples/automation-settings.json`을 소스 저장소 밖의 로컬 설정 파일로
복사해 `folder`, `mailbox`, `teams_chats`를 채운다. `folder`에는 매핑 드라이브
대신 실제 UNC 주소를 지정한다. API 키는 설정 JSON에 넣지 않고 실행 프로세스의
`OPENAI_API_KEY` 환경변수로 공급한다. 사용 가능한 API 모델 이름은 `model`에
넣는다. 키를 채팅·Git·로그에 붙여 넣지 않는다.

```powershell
python -m ariadne.automation_cli --settings C:\LocalConfig\automation.json doctor
python -m ariadne.automation_cli --settings C:\LocalConfig\automation.json sync
python -m ariadne.automation_cli --settings C:\LocalConfig\automation.json collect-mail
python -m ariadne.automation_cli --settings C:\LocalConfig\automation.json collect-teams
python -m ariadne serve --automation-settings C:\LocalConfig\automation.json
```

웹 주소는 `http://127.0.0.1:8765/`다. 다른 PC에 공개하지 않는다.
`ARIADNE_DATA_DIR`을 지정하지 않으면 Windows의 `%LOCALAPPDATA%\ARIADNE`에
자료를 저장한다. 수집 설정을 HTTP 요청으로 바꾸거나 임의 파일 경로를 읽을
수 없다. Host·Origin 검사와 JSON 요청 제한을 유지한다.

## 금요일 보고 작업

`weekly`는 가장 최근의 한국시간 금요일 16시를 종료시각으로 지난 7일을
분석한다. 기간은 시작 포함·종료 미포함이며 다음 주와 이어져 금요일 저녁의
자료가 빠지지 않는다. 수집·정규화·DB 저장·AI 추출·Excel 검증·발송 접수
순서다. AI에 근거 ID를 요구하고 없는 근거를 반환하면 생성에 실패한다.
엑셀에는 주간업무보고, 근거, 수집 범위를 저장하고 수식으로 해석될 수 있는
원문 문자열은 일반 텍스트로 기록한다. 사용자 확정 업무 상태는 바꾸지 않는다.

OpenAI Responses API를 `store:false`로 호출한다. 이것이 공급자의 모든 로그
보존을 없애는 설정이라는 뜻은 아니다. 입력 글자 수 제한을 넘으면 내용을
조용히 잘라내지 않고 실패한다. 계정의 모델 접근·과금·회사 전송 허용 범위는
운영 환경에서 설정한다. 이번 PC에는 API 키가 없어 실제 AI 호출은 수행하지 않았다.

기본 설정에서는 `enabled`, `auto_send`, `allow_partial_report` 모두 false다.
`enabled=true`로 실행을 켜고 `auto_send=true`로 본인에게만 발송하도록 설정할
수 있다. **현재 로컬 Teams 수집은 항상 불완전하므로** 이를 포함해 초안을
받으려면 `allow_partial_report=true`를 명시해야 한다. 이 경우 보고서에
누락·날짜 미확인 표시가 남는다. 완전 수집 보고서로 바꾸지는 않는다.

수집·준비 실패는 같은 주에 최대 3회 시도한다. `prepared` 이후 매번 AI를
재호출하지 않는다. 발송 직전에 `sending`을 영속화하고, 오류·시간 초과는
`delivery_unknown`으로 남긴다. 발송 결과 미확인·발송 접수 상태는 자동
재시도하지 않는다. Outlook Send의 성공은 전송 접수이지 최종 배달 확인이 아니다.
프로세스가 죽어 collecting 상태에 남으면 중지 여부를 확인한 뒤
`AutomationStore.release_failed_collection`으로 명시적으로 풀 수 있다.
이 메서드는 sending·delivery_unknown·submitted 상태를 풀지 못한다.

Windows 작업 스케줄러 등록 도구:

```powershell
.\scripts\register-weekly-task.ps1 -Python C:\Python\python.exe `
  -Project C:\Projects\ARIADNE -Settings C:\LocalConfig\automation.json `
  -DataDirectory C:\LocalData\ARIADNE
```

현재 로그인한 사용자 권한으로 다음 날부터 5분마다 `tick`을 확인하고, 금요일
16시 이후 주간 작업을 한 번 실행한다. 절전·종료로 놓친 경우 월요일 16시까지
재개할 수 있다. 잠긴 PC에서 Teams 수집이나 로그인 없는 Outlook 실행을 보장하지
않는다. 암호 저장·SYSTEM·관리자 실행·보안 설정 변경을 요구하지 않는다.
이번 개발에서는 **예약 작업을 등록하거나 실메일을 발송하지 않았다.**

## 검증과 미완료 항목

- 기존 42개 회귀 테스트와 신규 28개 테스트, 합계 70개 통과.
- 실제 공유 폴더 59개 저장·재실행·보관 바이트 일치 및 메일 110건 중복 방지 확인.
- 화면의 동기화 버튼·버전 다운로드·Host/Origin 차단을 검증.
- 가상 원천으로 실제 XLSX 생성·재열기, 금요일 경계·미완료 원천 차단·중복
  발송 방지·불명확한 전송 결과·AI 거절·입력 검증을 확인.
- Teams 전체 주간 이력, 실제 OpenAI 분석 품질, 최종 배달, Windows 예약 작업의
  장기 실행은 미검증. 모든 기능이 운영 중이라고 표시하지 않는다.
- 기존 회사 전체 주간회의 Excel에는 여러 업무·계산 시트가 있다. 이번 출력은
  개인 주간보고용 별도 파일이며 기존 회의록이나 수식·투자 실적을 덮어쓰지 않는다.

## 공식 근거

- Outlook 날짜 필터: https://learn.microsoft.com/en-us/office/vba/api/outlook.items.restrict
- Teams 메시지 이동 단축키: https://support.microsoft.com/en-us/accessibility/teams/keyboard-shortcuts-for-microsoft-teams
- OpenAI 구조화 출력: https://developers.openai.com/api/docs/guides/structured-outputs

공식 문서는 지원 기능의 근거이며 본인 계정의 무인 실행 성공을 보증하지 않는다.
