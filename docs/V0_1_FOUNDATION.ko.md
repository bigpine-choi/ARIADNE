# v0.1 1주차 구현 및 실행 안내

기준일: 2026-10-01, Asia/Seoul. 실제 업무자료를 사용하지 않는 로컬 기반 구현이다. 회사 계정 연동과 Godot의 최종 채택은 아직 완료된 것으로 보지 않는다.

## 구현한 동작

- 업무·기한·완료조건·근거의 엄격한 데이터 계약과 11개 상태
- SQLite 저장, 사용자 확인 필드 보호, 이전·이후 값과 주체·시각·근거·사유가 남는 변경 이력
- 변경 버전을 비교하는 동시 수정 충돌 처리
- 원본을 변경하지 않는 근거 메타데이터, 별도 추출 결과, 별도 AI 수정 제안
- 모든 완료조건에 검토한 완료 증거가 연결된 경우에만 완료 저장
- 메일 발송·첨부 수령·초안 자료만으로 완료 처리 차단
- 확정 기한의 위험을 업무 상태와 별도로 계산
- 펀드의 정식 이름·영문명·SPC 별칭 등록과 모호한 이름의 자동 연결 거부
- 재실행 보존, SQLite 일관 백업과 재열람
- 가상 데이터 CLI, 로컬 JSON API, API를 호출하는 Godot 업무 목록 시제품

Godot 화면에는 업무 등록·선택·검색·상태 필터·제목/상태 수정·근거 발췌·변경 이력·요약 복사가 포함된다. Calendar와 Funds의 완성 화면은 후속 버전이며 해당 메뉴에 준비 중 표시를 둔다. 원본 파일 열기·문서 렌더링, 기한·완료 증거 편집, AI 제안 승인 화면은 아직 없다. 이들 데이터는 API/저장 계층에서 관리할 수 있다.

## 실행

Python 3.12 이상을 사용한다. 운영 의존성은 Python 표준 라이브러리뿐이다. 소스 루트에서 다음 명령을 실행한다. 기존 백서 PDF 생성 스크립트는 별도 의존성을 사용하는 과거 도구이며 아래 실행과 관계없다.

```powershell
python -m ariadne doctor
python -m ariadne serve --demo
```

다른 터미널 또는 Godot 프로젝트 관리자에서 `godot/project.godot`을 열고 F6 대신 **F5로 프로젝트를 실행**한다. API는 `http://127.0.0.1:8765`에만 바인딩한다. 브라우저로 이 주소를 여는 것은 Godot 화면 실행이 아니다.

```powershell
godot --path godot
```

`godot` 명령이 PATH에 없으면 실제 Godot 실행 파일 경로를 사용한다. Windows에서 콘솔용 실행 파일을 사용하면 오류 메시지를 볼 수 있다. `--demo`는 가상 펀드·요청·업무 한 건을 추가하며 재실행해도 사용자 수정값을 초기화하지 않는다.

DB 기본 경로는 Windows `%LOCALAPPDATA%\ARIADNE\ariadne.sqlite3`, Linux `$XDG_STATE_HOME/ariadne` 또는 `~/.local/state/ariadne/ariadne.sqlite3`다. `ARIADNE_DATA_DIR` 또는 명령 앞의 `--db`로 위치를 바꿀 수 있다. 실제 회사자료 저장 위치는 회사의 허용 범위 확인 전 확정하지 않는다.

```powershell
python -m ariadne --db C:\ARIADNE-Test\synthetic.sqlite3 demo
python -m ariadne --db C:\ARIADNE-Test\synthetic.sqlite3 list
python -m ariadne --db C:\ARIADNE-Test\synthetic.sqlite3 backup C:\ARIADNE-Test\backup.sqlite3
```

백업 대상이 이미 있거나 원본 DB와 같으면 덮어쓰지 않고 거절한다. 복원은 별도 백업 파일을 `--db`로 지정해 읽는 방식으로 검증한다. DB 파일을 소스 저장소에 두거나 팀이 직접 공동 편집하지 않는다.

## 데이터 구조

| 저장 영역 | 내용 | 변경 원칙 |
| --- | --- | --- |
| evidence | 원천 ID·해시·스레드·페이지/조항·근거 발췌·최종본 여부 | 등록한 근거는 변경 불가. 같은 문서의 다른 위치는 별도 근거 가능 |
| extractions | 근거 ID·모델 버전·추출 결과 | 업무 확정값과 분리, 같은 추출 ID의 내용 변경 불가 |
| work_items | 업무·기한·상태·완료조건·버전·확인 필드 | 명시적 사용자 수정과 버전 검사 |
| proposals | AI 제안·확인 필드 충돌·제안 기준 버전·승인/거절 | 제안 저장만으로 업무값 변경 없음 |
| funds | 정식 식별자·이름·별칭 | 검토한 이름만 등록, 중복·모호한 연결 차단 |
| activities / audit_log | 활동·변경 전후·주체·시각·근거·사유 | 앱 API는 수정·삭제 기능 미제공 |

SQLite 스키마 버전은 `PRAGMA user_version=1`, 앱 식별자는 `PRAGMA application_id=1095911745`다. 앱 식별자·표 구조를 검사한 뒤 저장 모드를 설정한다. 지원하지 않는 버전 및 정체가 불명확한 기존 DB는 자동 수정하지 않는다. 운영 파일 자체를 열 수 있는 사용자의 직접 변경까지 방지하는 감사 저장소는 아니다.

업무 JSON 계약은 [work-item.schema.json](../schemas/work-item.schema.json), 근거 계약은 [evidence.schema.json](../schemas/evidence.schema.json)이다. API 조회는 저장 계약에 `version`, `confirmedFields`, `dueRisk`를 추가한다. 수정은 조회 결과 전체가 아닌 바꿀 필드만 `patch`로 전달한다.

11개 상태는 신규, 검토 필요, 진행 중, 내부 승인 대기, 외부 회신 대기, 일정 확정 대기, 완료, 보류, 기한 초과, 재개, 취소다. `dueRisk=overdue`여도 `status=waiting_external`을 유지해 회신 대기 사유가 사라지지 않는다. 위험 계산은 한국 날짜를 사용하며 미확정 날짜를 임의로 마감으로 확정하지 않는다.

완료 상태에는 완료조건이 최소 하나 있어야 하고 각 조건에 `completion` 또는 `review_completion` 근거 및 수동 검토자·시각이 있어야 한다. 초안은 완료 근거로 인정하지 않는다. 원천의 최종본 표시와 증거 역할은 사람이 제공하는 메타데이터이며 v0.1이 문서 서명이나 실제 지급 사실을 자동 검증한 결과는 아니다.

AI 요청은 실행하지 않는다. `record_extraction`과 `propose`는 향후 연결 모듈이 사용할 저장 인터페이스다. AI 제안 승인에는 현재 업무 버전과 제안 생성 버전이 모두 맞아야 한다. 사용자 수정 후 오래된 제안은 새로 생성해야 한다. 거절은 현재 버전을 확인한 뒤 업무값을 바꾸지 않는다.

## API

| 요청 | 역할 |
| --- | --- |
| GET `/api/health`, `/api/statuses` | 연결·연동 미설정 상태·11개 상태 목록 |
| GET/POST `/api/work-items` | 업무 조회·사용자 등록 |
| GET/PATCH `/api/work-items/{id}` | 상세·일부 필드 수정 |
| GET `/api/work-items/{id}/audit` | 해당 업무 이력 |
| GET/POST `/api/evidence`, GET `/api/evidence/{id}` | 근거 메타데이터 등록·열람 |
| GET/POST `/api/funds` | 펀드 정식 ID·별칭 등록·조회 |
| GET/POST `/api/work-items/{id}/proposals` | 업무와 분리된 AI 제안 |
| POST `/api/proposals/{id}/resolve` | 명시적 사용자 승인 또는 거절 |

등록 요청은 `{item, actorId, reason}`, 수정은 `{patch, expectedVersion, actorId, reason, evidenceIds?}`다. 제안 등록은 `{patch, baseVersion, reason, evidenceIds?}`이고 제안 승인·거절은 `{decision, expectedVersion, actorId, reason}`이다. `decision`은 `accepted` 또는 `rejected`다. 제안 등록 API는 자동 분석을 수행하지 않고 전달받은 제안만 저장한다.

응답은 목록 `{items}`, 업무 `{item}`, 근거 `{evidence}`, 제안 `{proposal}`, 이력 `{entries}`다. 상태 목록은 `{items: [{id, label}]}`다. 오류는 `{error: {code, message}}`, 검증 실패 400, 미존재 404, 버전·중복 충돌 409다. 요청은 JSON이어야 하며 최대 1MiB다. 버전은 양의 정수이며 문자열과 불리언은 거절한다.

로컬 서비스는 Host·Origin을 자신의 루프백 출처로 제한한다. 팀 인증·권한·암호화·중앙 서버 기능은 아직 없다. `actorId`는 로컬 감사용 표기이며 인증된 사용자 신원이 아니다. `ai`·`system`은 직접 사용자 변경/제안 승인을 수행할 수 없다. 외부 메일 발송·자료 전송·삭제·운용지시 실행 API는 없다.

## 검증

```powershell
python -m unittest discover -s tests -v
python scripts/check_godot.py --source-only
python scripts/godot_smoke.py --godot C:\Tools\Godot\Godot.exe
```

첫 명령은 저장·완료 판정·확정값 보호·충돌·이력 원자성·HTTP 제한·독립 프로세스 재실행·백업을 검증한다. 두 번째는 리소스 연결과 제한된 소스 구조 검사이며 GDScript 파싱이나 실행 결과가 아니다. 세 번째는 **설치된 실제 Godot**으로 별도 임시 DB와 빈 포트를 사용해 생성→수정→이력→완료 거절→서비스 재시작→보존 흐름을 검증한다. 실행기가 없으면 미실행을 알리고 실패 코드 2로 종료한다.

GitHub Actions에는 Python 3.12/3.13/3.14의 Linux·Windows 회귀 검사와 공식 Godot 4.5.1을 사용하는 Linux 엔진 검사 작업이 있다. 설정 파일이 있다는 사실은 원격 검사가 성공했다는 뜻이 아니다. 실제 관측 결과는 [검증 기록](VALIDATION_V0_1.ko.md)에 남긴다.

[Godot 검증 체크리스트](GODOT_VALIDATION.ko.md)의 한글 IME, 긴 목록, 원본 문서 열람, 복사, Windows 실행과 웹 배포를 확인한 뒤 화면 기술을 채택한다. 웹 프리셋만으로 현재 로컬 API가 인터넷 서비스가 되지 않는다. 웹 클라이언트와 API를 같은 출처로 제공하는 배포 및 팀 권한은 후속 작업이다.

## 다음 구현

1. Windows Godot 시제품 실행·한글 입력·목록·근거 열람 검증
2. 회사의 저장·Graph·외부 AI 허용 범위 확인
3. v0.2 MSG 수입과 본문·첨부·스레드 정규화, 대표 업무 정답 대조

이번 구현은 메일 파싱·Outlook 자동수집·계약 일정 계산·자금집행을 포함하지 않는다.
