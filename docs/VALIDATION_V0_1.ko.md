# v0.1 1주차 검증 기록

기준일: 2026-10-01, Asia/Seoul. 모든 사례는 가상 데이터다. 과거 메일 ZIP·회의자료 또는 회사 계정으로 업무 추출을 검증한 결과가 아니다.

## 관측 결과

| 검사 | 실행 환경 | 결과 |
| --- | --- | --- |
| `python -m unittest discover -s tests -v` | Linux, Python 3.12.14, SQLite 3.53.1 | 42개 통과 |
| 저장·완료조건·변경 이력·사용자 보호·동시 수정 | 위 unittest의 실제 임시 SQLite | 통과 |
| 한국어 JSON 생성·조회·수정·HTTP 제한 | 실제 루프백 HTTP 서비스 | 통과 |
| 별도 CLI 프로세스·재실행·중복 데모·백업 재열람 | 실제 Python 자식 프로세스 | 통과 |
| Godot 검사 도구의 독립 임시 서비스 시작·종료 | 임시 DB, OS가 배정한 빈 포트 | 통과 |
| `python scripts/check_godot.py --source-only` | Godot 리소스·설정·제한된 소스 검사 | 통과; GDScript 파싱이나 실행을 뜻하지 않음 |
| Python 소스 컴파일·`git diff --check` | 현재 작업본 | 통과 |
| 로컬 Godot import·scene 실행·API/UI smoke | 실행기 없음, 공식 다운로드 timeout | 미실행 |
| GitHub Actions Python·Godot 실행 | 워크플로 작성 | 원격 실행 결과 확인 전 |
| Windows IME·패키징·긴 목록·클립보드·시각 배치 | 사용자 PC/실제 UI 검증 필요 | 미검증 |
| Web export·같은 출처 배포·글꼴·IME | 브라우저/배포 검증 필요 | 미검증 |
| 회사 Graph·저장·외부 AI 허용 범위 | 회사 계정·운영 조건 필요 | 미확인 |

테스트는 완료 증거 없이 상태를 완료로 바꾸는 시도, 발송/첨부 도착/초안 증거의 오용, 누락된 근거, 불가능한 날짜, 미확정 기한, 모든 완료조건 미충족, 자동 주체의 직접 변경, 사용자 확인 필드의 제안 충돌, 오래된 제안·수정 버전, 근거 재등록·변경, 감사 기록 실패 시 전체 롤백, 두 연결의 동시 수정, 펀드 별칭의 모호함, 백업 재열람을 포함한다.

HTTP 검사는 다른 Origin·Host, JSON이 아닌 브라우저 폼, 비정수 버전, 과대 Content-Length, 중복 JSON 키, 잘못된 제안 승인값을 포함한다. 잘못된 제안 승인값에서 HTTP 500이 발생한 결함을 수정했고, 400 반환과 데이터 보존을 확인했다.

## 완료 판정

B02 모델과 B03 로컬 저장·이력의 v0.1 가상 사례 검증을 완료했다. B01은 Python·SQLite 확인만 완료했으며 회사 조건이 남아 있다. B04는 시제품과 검사 코드를 작성했으나 플랫폼·사용성 검증은 남아 있다. **v0.1 전체 완료 또는 회사 실사용 가능 상태로 표시하지 않는다.**

원격 CI 결과는 커밋 후 직접 조회해 판단한다. 자동 테스트의 Unicode 왕복이 통과하더라도 물리적인 한글 IME 조합, 시각적 배치, 실제 Windows/Web 사용성이 검증되는 것은 아니다.

## 재현

```powershell
python -m unittest discover -s tests -v
python scripts/check_godot.py --source-only
python -m ariadne doctor
```

Godot 실행기가 설치된 환경:

```powershell
python scripts/godot_smoke.py --godot '실제 Godot 실행파일 경로'
```

이 명령은 기존 운영 서비스 포트를 사용하지 않고 매번 임시 DB·빈 포트를 사용한다. 실행기 import, 실제 UI 스크립트 생성·수정·근거·이력·완료 거절, 서비스 재시작 후 보존을 확인한다. 원본 문서 렌더링·IME·플랫폼 export는 [Godot 체크리스트](GODOT_VALIDATION.ko.md)의 별도 확인 대상이다.
