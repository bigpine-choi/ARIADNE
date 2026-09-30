# 후속 Godot 클라이언트의 v0.1 시제품 검증

기준일: 2026-10-01, Asia/Seoul. 대상: `godot/`의 GDScript 4.x 클라이언트.

현행 개발은 [웹 버전의 핵심 흐름을 먼저 검증한 뒤 Godot 클라이언트를 추가](DEVELOPMENT_PLAN.ko.md)하는 순서다. 이 문서는 기존 시제품과 실제 검사 결과를 보존하며, Godot의 플랫폼·사용성 검증을 웹 MVP의 선행조건으로 두지 않는다. 웹 화면은 아직 구현하지 않았다.

## 구현 범위

화면은 로컬 Python 서비스의 저장값을 조회하고 수정한다. Godot 파일에 운영 DB를 만들거나 업무 완료·기한 계산 규칙을 복제하지 않는다.

- 가상 업무 목록, 제목·펀드·담당자 검색, 상태 필터와 키보드 목록 선택
- 업무명 한글 입력용 `LineEdit`, 신규 등록, 제목·상태 수정 및 변경 사유 입력
- 업무 ID와 서버 버전, 상태와 분리된 기한 위험, 완료조건·검토 근거 표시
- 근거의 원문 발췌·원천·위치·페이지·조항과 서버 감사 이력 조회
- 저장 후 서버 재조회, 버전 충돌 표시, 서비스 장애와 마지막 조회값 표시
- 선택 업무 요약 복사, Windows Desktop 및 단일 스레드 Web 내보내기 후보 설정

다섯 메뉴 중 Work Queue가 실제 목록·상세 작업 화면이다. Command Center와 Evidence & Audit는 같은 저장값을 조회하며, v0.1에서 별도의 자동 우선순위·종합 감사 화면을 구현하지 않는다. Calendar·Funds는 후속 범위를 명시한 안내 화면이다. 첨부 문서 렌더러와 원본 파일 열기는 아직 없다. 근거 탭은 서비스가 제공하는 발췌와 위치 정보를 보여 준다.

완료조건이나 검토 근거가 없는 업무를 ‘완료’로 저장하면 **서비스가 거절한다**. UI는 실패를 표시하고 저장값을 임의로 변경하지 않는다. 신규 업무의 기한·펀드·담당자는 미확정으로 등록하며, 기한이나 완료 근거 편집은 이 시제품의 화면 범위에 포함하지 않는다.

## 이번 환경의 실제 검증 결과

| 검증 | 결과 | 의미와 한계 |
| --- | --- | --- |
| 필수 리소스·참조·괄호·설정·요청 필드 정적 검사 | 통과 | `python scripts/check_godot.py --source-only`. GDScript 엔진 파싱을 대신하지 않음 |
| 검사 스크립트 Python 컴파일 | 통과 | `python -m py_compile scripts/check_godot.py` |
| Godot 실행기 확보 | 실패 | PATH와 일반 설치 경로에 실행기 없음. 공식 GitHub 배포 및 `downloads.godotengine.org`의 정상 다운로드 연결이 timeout으로 실패 |
| Godot import·실행·headless API/UI smoke | 미실행 | 재현 가능한 검사 코드는 작성했으나 실제 엔진 결과 없음 |
| Windows 한국어 IME·글꼴·클립보드·시각적 배치 | 미검증 | 화면 코드와 문자열만으로 사용성 통과를 판단하지 않음 |
| Windows 패키징·패키지 재실행 | 미검증 | export preset만 존재함 |
| Web export·브라우저 사용성·한국어 글꼴 | 미검증 | 배포·연동까지 완성한 상태가 아님 |

## 원격 CI의 실제 엔진 검증

[CI 실행 36739193704](https://github.com/bigpine-choi/ARIADNE/actions/runs/36739193704), 코드 커밋 `3ca86c86c7a702d4108e2be6f3b0a9bee0644301`에서 Linux Godot 4.5.1 import·scene 실행 및 API/UI smoke와 서비스 재시작 보존이 통과했다. 임시 DB·빈 포트로 실제 클라이언트의 가상 업무 근거 열람→한글 생성·수정→이력→근거 없는 완료 거절→서비스 재시작 후 저장값 보존을 확인했다. 위 표의 로컬 실행기 미확보와 구분한다.

엔진 실행의 기술 검사는 통과했으나 IME·시각적 사용성·Windows 패키징·Web 배포는 미검증이다. Godot의 제품 기능·배포 방식과 사용성 판단은 **웹 핵심 흐름 검증 이후의 확장 단계로 이관**한다. 데이터·서비스 계층은 공통으로 사용하고 화면은 별도로 구현하며 웹 버전을 유지한다.

## 실행 및 엔진 검사

저장소 루트에서 Python 3.12 이상과 Godot 4.5.1 이상의 표준 GDScript 실행기를 사용한다. 엔진에는 버전에 맞는 export templates가 별도로 필요하다. 아래 DB는 검증 전용으로 운영 DB와 구분한다.

```powershell
# 터미널 1: 검증 전용 로컬 서비스 실행
python -m ariadne --db ..\ARIADNE-check.sqlite3 serve --demo

# 터미널 2: 본인 PC의 실제 Godot 경로 사용
$env:GODOT_BIN = 'C:\Tools\Godot\Godot.exe'
python scripts/check_godot.py --require-runtime --api-smoke

# 화면 실행 또는 Godot에서 godot/project.godot를 열고 F6 대신 F5 실행
& $env:GODOT_BIN --path godot
```

기본 API 주소는 `http://127.0.0.1:8765`다. 포트를 바꿨다면 Godot 실행 전 `ARIADNE_API_BASE` 환경변수에 같은 로컬 주소를 지정한다. 환경변수는 데스크톱 실행에 사용하는 개발 설정이며 웹의 서비스 검색·인증 기능이 아니다.

엔진 검사 스크립트는 Godot import와 실제 스크립트 로딩 결과를 확인한다. `--api-smoke`는 가상 업무를 선택하여 근거를 읽고, 한글 문자열을 등록·수정하고, 상태 변경·서버 재조회·이력과 완료 거절을 검사한다. 이는 문자열의 HTTP 왕복 검사다. 실제 키보드로 한글을 조합하는 IME 검사는 별도로 해야 한다.

smoke 출력의 `GODOT_SMOKE_OK id=...` 값을 기록한다. 서비스를 종료한 뒤 같은 DB로 다시 실행하고 다음 명령으로 저장 보존을 확인한다.

```powershell
python scripts/check_godot.py --require-runtime --verify-id '출력된 업무 ID'
```

Godot 실행기가 없으면 기본 검사는 `NOT RUN`을 표시한다. `--require-runtime`, `--api-smoke`, `--verify-id`는 실행기 없음을 실패로 처리하므로 CI에서 정적 검사만 통과한 결과가 엔진 통과로 오인되지 않는다.

## 수동 완료조건

| 대상 | 확인 절차 | 통과 기준 |
| --- | --- | --- |
| 한글 IME | 실제 Windows 키보드로 `날인본 수령·검토` 입력, 조합 중 수정·백스페이스·붙여넣기 | 조합이 끊기거나 글자가 누락되지 않으며 등록·재조회 값이 같음 |
| 저장·재실행 | 등록·상태 수정 후 UI와 서비스를 정상 종료하고 같은 DB로 재실행 | 같은 업무와 버전·상태·변경 이력이 보존됨 |
| 긴 목록 | 가상 업무 1,000건 이상에서 검색·상태 필터·위아래 키·스크롤 수행 | 선택 행과 상세 ID가 일치하고 업무를 놓치지 않음. 소요시간·지연도 기록 |
| 변경 경합 | 상세를 연 후 다른 클라이언트에서 같은 업무 수정, 이전 화면에서 저장 | 409 충돌을 표시하며 오래된 값이 저장되지 않음 |
| 대기 중 입력 | 느린 서비스 응답 동안 검색·필터·목록 이동 시도 | 입력이 잠겨 상세와 선택 ID가 달라지지 않음 |
| 완료 거절 | 근거 없는 신규 업무의 상태를 완료로 저장 | 오류 표시, 서버 상태·버전이 보존됨 |
| 근거·복사 | 가상 업무의 발췌·위치·감사 이력을 확인하고 요약을 메모장에 붙여넣기 | 근거와 복사된 한글이 원문·서버 조회값과 일치함 |
| 화면 크기 | 1000×700, 1280×800, 1440×900 및 Windows 배율 변경 | 주요 입력·버튼·탭 접근 가능, 내용 잘림·겹침 없음 |
| 연결 장애 | 서비스를 중단하고 새로고침·저장, 다시 실행하고 새로고침 | 오류와 오래된 조회값 표시, 실패한 저장을 성공으로 표시하지 않음 |
| Windows export | templates 설치 후 Windows Desktop 내보내기, 생성된 exe 실행 | API 연결·한글 입력·저장·재실행·복사 정상 |

## Godot Web export 후보의 현재 제약

현재 Python 서비스는 API만 제공한다. 같은 출처 이외의 브라우저 요청을 거절하고 CORS·preflight를 허용하지 않는다. 따라서 다른 포트의 HTTP 서버나 공개 호스팅에서 Godot Web export를 실행하면 로컬 API와 연동되지 않는다. Web preset 존재는 웹 대시보드 배포 완료를 의미하지 않는다.

먼저 개발할 브라우저 웹 클라이언트와 Godot Web export는 별개의 화면 구현이다. 웹 MVP에는 공통 API와 같은 출처 제공 구조, 브라우저 사용성 검증이 필요하며 Godot export는 요구하지 않는다. 아래 export 검사는 Godot의 웹 배포를 실제 확장 방식으로 선택할 때 수행한다.

웹 검증에는 export 파일을 같은 승인된 서비스 출처에서 제공하는 경로, 한국어 글꼴 패키징·라이선스, 실제 브라우저 IME·복사·보안 정책 검사가 필요하다. 네이티브 `SystemFont` 후보는 맑은 고딕과 Noto Sans CJK KR을 사용하지만, 시스템 글꼴 사용이 제한되는 Web 환경의 한국어 표시를 보장하지 않는다. 실제 팀 공유에는 인증·권한·중앙 서비스가 추가로 필요하다.

## 공식 참고

- [Godot HTTPRequest](https://docs.godotengine.org/en/stable/classes/class_httprequest.html)
- [Godot SystemFont](https://docs.godotengine.org/en/stable/classes/class_systemfont.html)
- [Godot 웹 내보내기](https://docs.godotengine.org/en/stable/tutorials/export/exporting_for_web.html)

공식 문서는 API 사용 근거이며 이 시제품의 실행·배포 통과 증거가 아니다.
