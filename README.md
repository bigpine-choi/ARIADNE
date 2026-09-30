# ARIADNE

메일·업무 목록·일정을 모아 펀드 운영 업무와 후속 조치를 관리하는 Windows 업무 지원 도구입니다. 개발 기획명은 Fund Ops Workbench입니다.

부동산 펀드 운용 실무의 메일, Teams 업무 목록, 반복 일정과 증빙을 한곳에서 관리하기 위한 개발 기획 저장소입니다.

현재 단계는 **개발 기획과 데이터 계약 초안**입니다. 실행 가능한 앱, 실제 계정 연동, API 호출 또는 자동 발송 기능은 아직 구현하지 않았습니다.

## 먼저 읽기

- [개발계획 백서](docs/WHITEPAPER.ko.md): 실제 업무 관찰, 자동화 우선순위, IDE와 기술 선택, AI 역할, 운영 설계, 6주 로드맵
- [개발계획 백서 PDF](docs/Fund-Ops-Workbench-Whitepaper.ko.pdf)
- [개발 백로그](docs/BACKLOG.ko.md): 작업별 산출물과 완료 조건
- [업무 데이터 계약 초안](schemas/work-item.schema.json)
- [가상 업무 예제](examples/work-item.json)

## 기술 결정

| 항목 | 결정 |
| --- | --- |
| IDE | VS Code |
| 앱 | Electron + React + TypeScript, Windows 우선 |
| 저장 | 로컬 SQLite, 운영 데이터와 Git 저장소 분리 |
| 연동 | Microsoft Graph, Google Sheets API; 수동 가져오기부터 검증 |
| OpenAI API | 선택 기능으로 채택, 요약·추출·초안 작성 담당 |
| Jev | 한국어 업무 분류의 비교 실험 대상으로 채택, MVP 필수 의존성 제외 |
| Godot | 이번 업무 앱에는 미사용 |
| Power Automate | 기존 Outlook·Office 작업의 제한적 보조 수단 |

기술 선택은 설계 제안이며 설치나 유료 구독 승인을 뜻하지 않습니다. 상세 근거와 공식 문서 링크는 백서에 있습니다.

## 첫 번째 사용 흐름

1. 사용자가 선택한 업무 목록과 일정표를 가져옵니다.
2. 프로그램이 업무·기한·대기 사유 후보와 출처를 제시합니다.
3. 사용자가 모호한 기한과 중복 업무를 확인합니다.
4. 오늘 할 일과 공유용 업무 목록 초안을 생성합니다.

실제 메일·대화 원문, 거래처 연락처, 계약서, 토큰, 계좌 및 투자자 데이터는 이 저장소에 포함하지 않습니다. 예제는 가상 데이터입니다. 운영 데이터에는 별도의 회사 승인 저장 위치와 접근 정책을 적용합니다.

## 개발 착수

백로그의 M0부터 진행합니다. 회사 계정의 API 접근 가능 여부를 먼저 확인하고, 접근이 불가능해도 수동 가져오기와 로컬 업무 관리가 동작하도록 만듭니다. SDK 버전은 구현 시작일에 호환성을 검증하고 잠금 파일에 고정합니다.

## 백서 PDF 생성

[PDF 생성 코드](scripts/build_whitepaper.py)는 `docs/WHITEPAPER.ko.md`를 읽어 `docs/Fund-Ops-Workbench-Whitepaper.ko.pdf`를 생성합니다. Python, `reportlab`, `pypdf`와 Windows 맑은 고딕 글꼴이 필요합니다. 저장소 루트에서 `python scripts/build_whitepaper.py`를 실행합니다.

작성 기준일: 2026-09-30, Asia/Seoul.
