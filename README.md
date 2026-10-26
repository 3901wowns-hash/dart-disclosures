# 관심기업 주요공시

관심 기업의 주요 공시(유상증자, 유형자산 취득, 잠정실적 등)를 DART 오픈API로 매일 모아 보여주는 사이트입니다.

- 사이트: https://3901wowns-hash.github.io/dart-disclosures/
- 갱신: 매일 오전 10시(한국시간), GitHub Actions 자동 실행 (몇 분~수십 분 늦을 수 있음)
- 보관: 최근 1년

## 관심 기업 · 공시 종류 바꾸기

`config.json` 을 고쳐 커밋하면 바로 다시 수집합니다.

- `companies`: 회사 이름과 종목코드. 새 회사는 처음 수집할 때 1년치를 받습니다.
- `categories`: 공시 종류 이름과 보고서명 키워드(띄어쓰기 무시). 키워드를 추가했다면
  Actions → "공시 수집 및 사이트 배포" → Run workflow 에서 "모든 기업을 1년치로 다시 받기"를 체크해 실행하세요.

## 구성

| 파일 | 역할 |
|---|---|
| `collect.py` | DART 에서 공시를 받아 `docs/data/disclosures.json` 에 저장 |
| `summarize.py` | 최근 `summary_days`(90일) 공시의 원문 표에서 핵심 항목을 뽑아 `docs/data/summaries.json` 에 저장 (AI 없이 규칙 기반) |
| `docs/index.html` | 사이트 화면 |
| `.github/workflows/update.yml` | 매일 수집 → 커밋 → GitHub Pages 배포 |

DART 인증키는 저장소 Settings → Secrets and variables → Actions 의 `DART_API_KEY` 에 있습니다.
