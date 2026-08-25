# 자동매매 재구축 계획 — 매매 루프 우선

**기준 브랜치:** `main` (orphan, `306d292`)
**작성일:** 2026-08-25
**전략 근거:** [`docs/strategy/David_Trullas_Vila_전략분석_자동매매용_v6.0_화면역공학통합판.md`](../strategy/)
**백오피스 요구사항:** [`docs/design/backoffice.md`](../design/backoffice.md)

## 왜 다시 세우는가

이전 시도는 640커밋·61k줄을 쌓고도 화면 한 장과 매매 루프 하나가 없었다. 개인 단일
계정 도구인데 증거 provenance·권한 원장·바인딩별 승격 게이트·세션 매니페스트·
fencing token 같은 기관용 컴플라이언스 구조를 먼저 지었기 때문이다. 전략 평가·리스크·
주문 수명주기·브로커 어댑터는 품질이 좋았지만, 그것들을 **실제로 돌리는 것**이 없었다.

`main`은 그 핵심만 남긴 기준선이다. 이 계획은 **돌아가는 매매 루프를 먼저 만들고**,
그 위에 관측·통제 화면을 붙인다. 거버넌스는 실제로 위험을 막을 때만, 그 시점에 최소
형태로 추가한다.

## 지금 있는 것과 없는 것

전략 체인의 각 고리는 개별적으로 존재하고 테스트도 통과한다:

```
완성봉 → [evaluate_divergence / evaluate_exhaustion / build_hlit_zones /
          evaluate_regime / evaluate_metodo / evaluate_session /
          evaluate_event_window / estimate_round_trip_cost /
          evaluate_cash_universe / aggregate_order_flow / build_profile]
       → V6EvidenceBundle → evaluate_v6 → V6Decision → to_strategy_decision
       → OrderIntentFactory.from_strategy_decision → OrderService.create_from_risk_decision
       → DispatchService.dispatch → 브로커
```

**끊어진 곳은 세 군데다:**

1. **증거 조립기 없음.** 완성봉을 각 evaluator에 먹이고 결과를 `EvidenceItem`으로
   감싸 `V6EvidenceBundle`을 만드는 모듈이 없다. 시장 데이터와 전략 엔진이 코드상
   연결된 적이 없다.
2. **루프 없음.** 전부 일회성 호출이다. 주기적으로 시장을 스캔하고 주문을 내는
   데몬이 없다.
3. **`DispatchStore` 구현 없음.** 프로토콜만 있고, 유일한 구현(`MySqlDispatchStore`,
   870줄)은 정리 과정에서 제거했다. 얇게 다시 써야 한다.
4. **적용 가능한 스키마 없음.** 마이그레이션 44개는 실제 MySQL에 한 번도 적용된 적이
   없고, 삭제된 전략 테이블까지 누적돼 있어 전부 제거했다.

## 작업 원칙

- `AGENT.txt`를 실제로 지킨다. 새 추상화 계층·권한 원장·증거 체인을 추가하지 않는다.
  막으려는 사고를 한 문장으로 말할 수 없으면 만들지 않는다.
- 각 단계는 **관측 가능한 결과**로 끝난다. "구현 완료"가 아니라 "이 명령을 돌리면
  이것이 보인다"로 정의한다.
- 실주문 경로는 Phase 5 전까지 어디에도 연결하지 않는다. Paper와 read-only만 쓴다.
- 변경 범위의 tests·Ruff·Pyright를 매번 통과시킨다. 현재 기준선은
  `1400 passed, 17 skipped`, Ruff/Pyright clean이다.

---

## Phase 0 — 스키마 통합

루프가 결정을 기록하려면 적용 가능한 스키마가 먼저 있어야 한다.

**작업**

- `migrations/env.py`의 `target_metadata`를 `CoreBase.metadata`에 연결한다. 지금은
  `None`이라 ORM과 스키마의 drift를 아무도 감지하지 못한다.
- 남은 78개 ORM 테이블에 대해 `migrations/versions/0001_initial.py`를 단일
  마이그레이션으로 작성한다.
- 사용할 테이블만 남긴다. `strategy_shadow_candidate`,
  `exec_order_intent_legacy_strategy_link`, `ops_gate_scenario_scope` 등 이전 전략
  잔재는 이 단계에서 잘라낸다.

**검증**

- 사용자 승인 후 폐기용 MySQL(`TEST_DISPOSABLE_DATABASE_URL`)에 `alembic upgrade head`.
- 두 번 연속 실행이 멱등인지 확인.
- ORM ↔ 스키마 drift가 비어 있는지 autogenerate diff로 확인.
- CI에 integration 잡을 다시 넣는다 (Compose로 MySQL/Redis 기동 → migrate → 통합 테스트).

**완료 기준:** 빈 MySQL에서 `alembic upgrade head`가 성공하고 drift가 0이다.

---

## Phase 1 — 증거 조립기

이 프로젝트에서 한 번도 존재한 적 없는 연결 고리다. 가장 먼저 만든다.

**작업**

- `src/autotrader/strategies/david_v6/assembly.py` 신설.
- 시장별 조립 함수 3개: `assemble_krx_cash`, `assemble_us_cash`, `assemble_binance_usdm`.
  각각 완성봉(+ Binance는 체결 스트림), 거래소 캘린더, 수수료 스케줄을 받아
  기존 evaluator를 호출하고 `EvidenceItem`으로 감싼 `V6EvidenceBundle`을 반환한다.
- 시장별 필수 사실은 이미 `engine.py`에 정의돼 있다 — 현금시장은 `universe/zones/
  divergence/exhaustion`, Binance는 추가로 `order_flow/profile`, 공통은 `regime/
  calendar/session/costs`.
- **fail-closed가 핵심이다.** 입력이 없거나 봉이 미완성이면 값을 지어내지 말고
  `EvidenceState`를 AVAILABLE이 아닌 상태로 두고 blocker code를 붙인다. `evaluate_v6`가
  알아서 REJECT한다.
- provenance는 실제 출처로 채운다 (`source`=브로커/거래소, `observed_at`=봉 종료시각,
  `digest_sha256`=입력 봉의 정규 다이제스트).

**검증**

- 시장별 골든 픽스처로 조립 → `evaluate_v6` → 기대 등급/blocker 확인.
- 필수 사실을 하나씩 빼면서 해당 blocker code가 정확히 나오는지 확인.
- 전략 불변식 테스트: 다이버전스 없이 피보나치 존이 생성되지 않는다, 미완성 봉이
  섞이면 AVAILABLE이 되지 않는다.

**완료 기준:** 실제 Binance 공개 REST에서 받은 BTCUSDT 완성봉으로 조립한 번들이
`evaluate_v6`를 통과해 결정(대개 REJECT + blocker)을 만들어낸다.

---

## Phase 2 — 매매 루프 데몬 (Paper 전용)

**작업**

- `src/autotrader/apps/trader/` 신설. 시장별 async 루프 하나씩, 각자의 주기로 돈다.
- 한 틱의 흐름:
  1. `ops_trading_control` 확인 → DISARMED면 즉시 중단
  2. `ops_scheduler_lease`로 단일 인스턴스 보장
  3. watermark 이후의 **완성봉만** 조회
  4. Phase 1 조립기로 번들 생성
  5. `evaluate_v6` 호출
  6. 결정을 **항상 저장한다** — REJECT와 blocker 포함. 이게 백오피스가 보여줄 내용이다
  7. tradeable이면 `to_strategy_decision` → `OrderIntentFactory` → `OrderService`
     → `DispatchService`로 `InternalPaperBroker`에 제출
- 얇은 `MySqlDispatchStore` 재작성. 제거된 870줄 버전의 권한 판정 계층은 가져오지
  않는다. 필요한 것은 멱등한 attempt 기록과 unknown 상태 처리뿐이다.
- 주기: KRX/US 현금은 세션 중 5분·일봉, Binance는 5분·1시간 + 5초/30초 로컬 집계.

**검증**

- 한 세션 동안 Binance read-only + paper broker로 돌려서 결정이 쌓이는 것을 확인한다.
- 재시작 복구: 루프를 강제 종료했다 다시 띄웠을 때 봉을 건너뛰거나 중복 주문하지 않는다.
- DISARM 즉시성: 틱 도중 DISARMED로 바꾸면 그 틱에서 멈춘다.

**완료 기준:** 명령 하나로 루프가 뜨고, 한 세션 뒤 DB에 결정 이력이 남아 있으며,
paper 포지션이 전략대로 열리고 닫힌다.

---

## Phase 3 — 실 브로커 정산과 보호

주문은 여전히 paper다. 여기서는 **실제 계좌 상태를 읽어** 로컬 상태와 맞춘다.

**작업**

- read-only 경로 연결: KIS `account_snapshot`, Toss `us_account_snapshot`,
  Binance `account`. 기존 어댑터를 그대로 쓴다.
- 매 틱 정산: 기존 `execution/reconciliation/service.py`와 브로커별 reconciliation
  모듈로 브로커 사실 ↔ 로컬 포지션을 비교하고 차이를 `exec_reconciliation_diff`에 남긴다.
- 보호 손절 강제: 전략 불변식 "포지션이 있으면 보호 손절 필수"를 런타임에서 검사한다.
  보호 없는 포지션을 발견하면 incident를 남기고 해당 시장을 HALT한다.

**검증**

- 무포지션 계좌로 한 세션 정산해 drift가 0인지 확인.
- 로컬 포지션을 인위적으로 틀어놓고 정산이 그것을 잡아내는지 확인.

**완료 기준:** 세 브로커 모두에 대해 실계좌 스냅샷을 읽어 drift 0을 보고한다.

---

## Phase 4 — 백오피스

`docs/design/backoffice.md`의 요구사항을 따르되, 순서는 **읽기 먼저**다. 볼 것이
생긴 뒤에 통제를 붙인다.

**4a. 비밀값 영속화**
- MySQL AES-256-GCM 비밀값 저장소 + 로컬 non-echo bootstrap CLI.
  암호화 코드(`security/secret_crypto.py`)는 이미 있다.
- KIS/Toss/Binance provider caller를 dotenv에서 DB resolver로 옮긴다. 기존 `.env`
  경로는 fail-closed로 남긴다.
- 서버 `.env`에는 DB 접속정보와 32바이트 master key만 남는다.

**4b. 인증**
- Google OIDC로 `lmhml0237@gmail.com` 단일 검증 이메일만 허용.
- Redis 세션 + 모든 mutation에 CSRF.

**4c. 읽기 전용 대시보드**
- FastAPI + Jinja + HTMX. 루프 상태, 최근 결정과 blocker, 포지션, 정산 차이,
  incident를 보여준다. Phase 2~3이 쌓은 데이터가 여기서 처음 눈에 보인다.
- 평문 비밀값·암호문·nonce·OAuth token·전체 계좌번호는 화면·로그·예외 어디에도
  남기지 않는다.

**4d. 통제**
- typed command gateway 하나를 모든 mutation의 공통 경계로 만든다 (멱등키, 감사 기록).
  Web route는 ORM DML이나 CLI subprocess를 직접 호출하지 않는다.
- 화면: 계정·바인딩·리스크 정책·유니버스·HALT/DISARM.
- **HALT·DISARM·EMERGENCY는 인증과 CSRF만 요구한다.** 2차 비밀번호, provider 가용성,
  비밀값 복호화, readiness에 의존하면 안 된다. 안전장치가 다른 것에 의존하면
  안전장치가 아니다.

**검증**

- 브라우저 E2E: 로그인 → 대시보드 → HALT.
- 루프가 틱을 도는 중에 화면에서 HALT를 눌러 실제로 멈추는지 확인.
- 미인증 요청과 다른 이메일이 전부 거부되는지 확인.

**완료 기준:** 브라우저에서 로그인해 루프 상태를 보고 정지시킬 수 있다.

---

## Phase 5 — LIVE 승격

제거한 8단계 승격 의식을 되살리지 않는다. 대신 실제로 위험을 줄이는 최소 조건만 둔다.

- 바인딩(계정×시장) 단위로: read-only readiness 통과 + paper 세션 N회 정산 drift 0
  + 운영자의 명시적 활성화 + 2차 비밀번호 승인.
- 활성화는 단일 트랜잭션에서 조건을 다시 판정하고 기록한다. 화면에 보이던 값만
  믿고 켜지 않는다.
- 첫 실주문은 한 시장에서 가능한 최소 수량으로 하고, 끝까지 추적한 뒤 다시 HALT한다.

**완료 기준:** 최소 수량 실주문 1건이 제출·체결·정산까지 확인된다.

---

## Phase 6 — 배포

- Ubuntu Docker Compose: Caddy, backoffice, trader worker, MySQL, Redis,
  one-shot migration.
- Caddy만 80/443을 공개하고 나머지는 private network에 둔다.
- migration·인증·비밀값·DB·Redis 실패 시 fail-closed.

---

## 미검증 항목

이 계획을 시작하는 시점에 아래는 **한 번도 확인된 적이 없다.** 어느 것도 되어 있다고
가정하지 않는다.

- MySQL 스키마 적용 (마이그레이션이 실제 DB에 적용된 적 없음)
- 실계좌 provider 호출 (KIS/Toss/Binance 어느 것도 실인증으로 호출된 적 없음)
- Google OAuth
- Docker Compose 기동과 브라우저 E2E
- 전략의 실제 손익 특성 — 백테스트 하네스(`research/david_v6/backtest.py`)는 있지만
  실데이터로 돌린 결과가 없다

## 참고

전략 문서는 이 전략의 승률 86% 보고에 대해 원저자 본인이 "그 비율은 내려간다"고
경고한 것을 기록하고 있다. 그 숫자를 시스템 목표로 삼지 않는다.
