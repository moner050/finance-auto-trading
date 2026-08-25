# Finance Auto Trading

David Trullás Vila v6 전략을 KIS(국내 현물), Toss(미국 현물), Binance USD-M(무기한
선물)에 적용하는 단일 계정 자동매매 서비스다. 운영자는 백오피스 화면 하나로 전략을
관찰하고 통제한다.

전략 명세: [`docs/strategy/`](docs/strategy/)
백오피스 요구사항: [`docs/design/backoffice.md`](docs/design/backoffice.md)
작업 계획: [`docs/plans/`](docs/plans/)

## 현재 상태

이 브랜치는 이전 640커밋 시도에서 **핵심 로직만 추려낸 새 기준선**이다. 전략 평가,
리스크, 주문 수명주기, 브로커 어댑터, 시장 데이터 수집은 이식했고 증거·권한 원장,
승격 게이트, 세션 매니페스트, readiness CLI는 제거했다.

아직 없는 것:

- 연속 매매 루프 (완성봉 수집 → 평가 → 주문 → 정산)
- 백오피스 웹 화면
- 적용 가능한 DB 스키마 (마이그레이션은 통합 재작성 대상)

주문을 실제로 내는 경로는 어디에도 연결되어 있지 않다.

## 개발

```
uv sync --frozen
uv run python -m pytest -q
uv run ruff format --check . && uv run ruff check . && uv run pyright
```

로컬 인프라는 `infra/compose/.env.example`을 `infra/compose/.env`로 복사한 뒤
`infra/compose/`에서 Docker Compose로 띄운다. MySQL과 Redis 포트는 루프백에만
바인딩된다.
