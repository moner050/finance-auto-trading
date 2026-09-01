**§11.7 승격 (2026-08-27)**

화면이 아니라 도메인부터 만들었다. §17이 "GUI는 권위의 원천이 아니라 기존 권위에
대한 조작 인터페이스이며 승격 상태를 수동으로 편집할 수 없다"고 못박기 때문에,
화면이 먼저 나오면 화면이 권위가 된다.

- `execution/promotion/models.py` — DB도 화면도 모르는 규칙. 세션은 **거래일**
  하나이며(같은 날짜 두 번은 이틀이 아니라 하루를 두 번 본 것), 실행된 전략
  manifest에 고정된다(소스나 설정 해시가 바뀌면 이전 세션은 다른 것에 대한
  세션이다). `verify()`는 판정이 아니라 **막는 이유 전부**를 돌려준다.
- `exec_promotion_session` — 완료 조건이 **체크 제약**으로 들어가 있다. 화면도
  CLI도 SQL 클라이언트도 똑같이 막힌다. `(binding, mode, exchange_date)` 유니크.
- `repositories/promotion.py` — 증거를 **루프가 쓴 테이블에서 직접 센다.** 호출자가
  건넨 숫자를 믿는 완료였다면 화면이 스스로 준비 완료를 선언할 수 있게 된다.
- §11.7 화면 — 타임라인, 세션 청구, 차단 사유 표시, 확인된 것만 완료.
  막힌 세션에는 완료 버튼이 아예 나오지 않는다. 눌러서 거절을 읽게 만드는 버튼은
  차단 사유를 보여주는 것의 반대다.

**남은 것:** Shadow/Paper 모드를 루프가 실제로 구분해 도는 배선. 지금은 세션을
청구하고 증거를 세는 곳까지이며, 루프가 SHADOW로 돌 때 주문을 내지 않는다는
사실은 `execution/controls/gates.py`가 이미 강제한다(RUNTIME_MODE_DENIED).

**§11.5 유니버스는 아직 기능이 아니라 스키마부터다**

유니버스 스냅샷·manifest·staged/active 개념이 스키마에 통째로 없다
(`core_instrument`는 평평한 목록이고, 이는 §11.5가 명시적으로 금지하는
"한 종목만 추가해 유니버스를 조용히 넓히는" 모양이다). §11.4 정책 화면과 같은
구조(불변 스냅샷 + digest + staged/active 전환)를 그대로 가져올 수 있다.

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
4. ~~**적용 가능한 스키마 없음.**~~ Phase 0에서 해결했다.

## 작업 원칙

- `AGENT.txt`를 실제로 지킨다. 새 추상화 계층·권한 원장·증거 체인을 추가하지 않는다.
  막으려는 사고를 한 문장으로 말할 수 없으면 만들지 않는다.
- 각 단계는 **관측 가능한 결과**로 끝난다. "구현 완료"가 아니라 "이 명령을 돌리면
  이것이 보인다"로 정의한다.
- 실주문 경로는 Phase 5 전까지 어디에도 연결하지 않는다. Paper와 read-only만 쓴다.
- 변경 범위의 tests·Ruff·Pyright를 매번 통과시킨다. 현재 기준선은
  `1400 passed, 17 skipped`, Ruff/Pyright clean이다.

---

## Phase 0 — 스키마 통합 ✅ 완료 (`610afc1` … `6fc3a9f`)

**한 일**

- 44개 마이그레이션 체인을 `0001_initial` 하나로 대체하고, 손으로 전사하는 대신
  `scripts/generate-initial-migration.py`가 ORM metadata에서 생성한다.
  `migrations/env.py`의 `target_metadata`를 연결했다 — `None`이던 탓에 모델과 스키마가
  갈라져도 아무도 알 수 없었다.
- 참조가 0인 테이블 4개를 제거하고, 정리 과정에서 사라졌던
  `exec_provider_account_binding`을 정체성·범위만으로 복원했다. 75개 테이블이 남았다.

**실제 MySQL에 적용하며 드러난 것**

배포돼 있던 스키마와 대조하니 **제약 104개가 DB에는 있고 ORM에는 없었다** — 외래키 66,
CHECK 29, UNIQUE 9, 70개 중 35개 테이블. 손으로 쓴 마이그레이션이 사실상 정본이었고,
ORM에서 생성한 스키마는 ORM에 충실했기에 그 무결성 보장을 조용히 버렸다.

이를 모델에 선언해 복원했다. **FK 41→106, CHECK 76→101, UNIQUE 78→85.**
복원 불가 8개는 사유가 있다 — 5개는 ORM이 모델링하지 않는 v5 퇴역 컬럼 참조,
3개는 provider 바인딩의 증거 해시 참조 2개와 더 강한 CHECK와 중복되는 1개.

`exec_order`·intent·reconciliation diff·risk decision이 서로를 참조하는 순환이 있어
어떤 생성 순서로도 FK가 풀리지 않는다. 덤프와 같이 생성 중 FK 검사를 끈다.

**검증 (실제 MySQL)**

| 항목 | 결과 |
| --- | --- |
| `alembic upgrade head` (빈 DB) | 성공, `0001_initial` |
| 테이블 | 76 = ORM 75 + `alembic_version`, 누락·초과 0 |
| 멱등성 | 재실행 no-op |
| downgrade base → upgrade head | 왕복 정상 |
| ORM drift (autogenerate) | **0** |
| 전체 스위트 | **1528 passed**, skip 0 |
| ruff / pyright | clean / 0 errors |

통합 테스트 3건도 고쳤다. 구 CI가 `AUTOTRADER_TEST_MIGRATION_TARGET=0011`로 훨씬
오래된 스키마에 돌린 탓에 최종 스키마를 만난 적이 없던 것들이다 — flush 순서, 데드락
재시도, 포지션 표시통화, async 테스트 안의 alembic 호출.

**Redis 전송도 검증했다**

Redis 7.0.15에 연결해 outbox/inbox 스트림 경로를 실제로 돌렸다. 그동안 skip만 되던
테스트들이라 두 가지 가정이 드러났다 — 스트림 엔트리 ID를 `"1-0"`으로 하드코딩한 것
(Redis는 시계에서 ID를 만든다), 그리고 제거된 official-fact 핀에서 온 정확한 컨테이너
버전 단언이다. 후자는 코드가 실제로 요구하는 것으로 바꿨다: CHECK 제약을 위한 MySQL
8.0.16, 스트림을 위한 Redis 5.0, 그리고 `XADD`·`XAUTOCLAIM` 존재 확인.

마지막 검사는 값이 있다. 이번 작업에 처음 제시된 서버는 `PING`에 응답했지만 3.0.504였고
전송이 쓰는 스트림 명령이 하나도 없었다.

**전체 스위트: `1528 passed`, skip 0.**

## Phase 1 — 증거 조립기 ✅ 완료 (`c8d38a2`)

이 프로젝트에 한 번도 존재한 적 없던 연결 고리를 만들었다.

**한 일**

- `src/autotrader/strategies/david_v6/assembly.py` 신설. 완성봉·거래소 캘린더·이벤트·
  비용 입력을 받아 모든 evaluator를 돌리고, 각 사실에 그것을 만든 입력의 provenance를
  기록한 `V6EvidenceBundle`을 반환한다.
- **fail-closed.** 입력이 없으면 값을 지어내지 않고 해당 사실을 UNAVAILABLE로 두고
  고유한 blocker code를 붙인다. 불완전한 수집이 거래 가능한 셋업을 만들 수 없다.
- 미완성 봉은 조립 전체를 실패시키지 않고 걸러낸다. 수집이 조금 일찍 돈 것은 흔한
  일이지 오류가 아니다. 타임프레임 정의는 `evidence.TIMEFRAMES` 한 곳만 쓴다.
- `derive_indicators()` 추가. 엔진은 지표로 등급을 매기고 번들이 뒷받침하지 않는
  evidence hash를 거부하는데, **이 지표를 만드는 코드가 없어 조립된 번들이 애초에
  등급을 받을 수 없었다.** 이제 방향 주장은 호출자의 선언이 아니라 정규 다이버전스
  자체에서 나온다.
- MACD 12/26/9를 `metodo.macd_series`로 공개. §12가 일봉 스윙과 HLIT에 같은 오실레이터를
  지정하므로 복제하면 둘이 갈라진다.

**검증**

명세가 서술하는 셋업이 실제로 나오는 봉을 만들어 테스트했다. 랠리가 앵커 A가 될
스윙 고점을 찍고, 이어지는 감속 하락이 거래량 감소 속에 미세한 저점 갱신을 반복하며
모멘텀은 상승한다 — 정규 강세 다이버전스와 소진 시퀀스가 동시에 성립하는 형태다.

실제 봉에서 처음으로 25/50/66이 그려졌다.

```
anchor_a = 120.725   anchor_b = 119.700
25% = 119.95625   50% = 120.21250   66% = 120.37650  (target)
exhaustion confirmed, zones 3개
```

조립 → 지표 파생 → `evaluate_v6`까지 이어져 결정이 나오는 것을 테스트로 고정했다.
신규 테스트 19건.

## Phase 2 — 매매 루프 데몬 (Paper 전용) ✅ 완료

**끝난 것**

- **`MySqlDispatchStore`** (`3a4afb7`). 프로토콜만 있고 구현이 없어 주문을 보낼 경로가
  아예 없었다. 얇게 다시 썼다 — 전송 전 표식과 종결 상태만 담고, 권한 판정은 커맨드
  생성 시점에 이미 끝났으므로 가져오지 않았다.
- **틱** (`5d6e563`). 포트 뒤에 둔 다섯 단계 — DISARMED 확인 → 조립 → 평가 → **항상 기록**
  → 거래 가능할 때만 실행.
- **MySQL 배선과 end-to-end** (`7bffcbc`).
- **비동기 페이퍼 체결** (`3a72db0`). 체결 봉은 주문 전송 시점에 아직 마감되지 않는다.
- **스케줄 루프** (`bd16d9f`).
- **시장 데이터 어댑터와 진입점** (`53c02e5`). `BinanceContextSource`(봉당 한 번만 평가),
  `BinanceExecutionBars`, 그리고 `binance_paper.py`. 라이브 Binance 봉으로 실제 결정이
  MySQL에 남는 것을 확인했다.
- **인스트루먼트 등록 경로** (`115d434`). `CoreReferenceRepository`는 읽기만 있었고
  애플리케이션이 인스트루먼트를 만드는 경로가 없어, 호출자가 어느 테이블에도 없는
  UUID를 지어내는 수밖에 없었다.
- **통합 테스트 전부 활성화** (`e80bbd1`). 76개가 조용히 건너뛰어지고 있었다 —
  테스트는 `DATABASE_URL`만 읽는데 애플리케이션은 `MYSQL_*`로 URL을 만든다.
  Redis는 더 나빴다: `REDIS_HOST/PORT/PW`를 읽는 코드가 `src/`에 아예 없었다.
- **CI에 MySQL/Redis 서비스** (`22896ec`), **acceptance 스위트** (`b9a1811`).

**검증 (실제 MySQL + Redis)**

`1519 passed` + 통합 76 + acceptance 7. skip 0. ruff·pyright·import boundary 클린.

---

## Phase 3 — 실 브로커 정산과 보호 — 실계좌 스모크만 남음

계획을 세울 때 전제했던 것이 성립하지 않았다. `PROTECTIVE` 인텐트를 만드는 코드가
`src/` 어디에도 없었고, `MySqlFillStore`/`FillRepository`는 참조가 0건이었다.
체결이 `exec_position`에 도달하는 경로가 통째로 없었으므로, "포지션이 있으면 보호
손절 필수"를 검사해봐야 항상 통과하는 검사가 됐을 것이다.

**끝난 것**

- **체결 → 포지션 원장** (`9dfccfd`). 영수증을 `BrokerExecutionEvent`로 옮기고 정산이
  적용한다. 커맨드 id를 중복 제거 키로 삼아 정산이 두 번 돌아도 포지션이 두 배가 되지
  않는다.
- **대기 손절** (`9dfccfd`). 커맨드가 트리거가를 지니고, 그것이 *체결 가격*이 아니라
  *어느 봉이 그 주문을 해소하는가*를 정한다. 일회성 주문의 규칙은 그대로다.
  체결은 트리거가에, 갭이면 시가에.
- **보호 손절 배치** (`0d21edd`). 진입이 체결되면 정산이 §9.2가 명명한 가격에 손절을
  놓는다. `create_from_risk_decision`이 `REDUCE`를 거부하던 것을 열되 권한과 묶었다.
- **보호 강제** (`a61ce3b`). 보호 없는 열린 포지션이면 incident를 남기고
  `BLOCK_NEW_EXPOSURE`로 신규 노출만 막는다. 전면 HALT는 보호 주문 자체도 막는다.
- **포지션 대조** (`670ba84`). 기존 정산은 미체결 주문만 봤다. 세 가지 불일치 —
  브로커가 모르는 포지션, 브로커만 아는 포지션, 수량 불일치 — 가 모두 차단이다.
  보유 수량 0은 양쪽 모두 금지해 "없다"와 "확인 안 했다"를 구분한다.
- **루프 배선** (`0a3459a`). 패스 순서는 리스 → 정산 → 대조 → 보호 검사 → 평가.
  대조가 보호보다 앞인 이유는, 실제로 없을지 모르는 포지션에 맞춰 크기를 잡은 손절은
  아무것도 지키지 않기 때문이다.
  페이퍼 루프에도 진짜 상대가 있다 — 저널 영수증은 브로커가, 포지션 원장은 fill store가
  쓰므로 둘의 불일치는 견해 차이가 아니라 한쪽 경로의 결함이다.

**실계좌 스냅샷 리더 (2026-08-27)**

`BrokerSnapshotReader` 포트에 세 브로커를 맞췄다. 어댑터는 커넥션도 자격증명도
쥐지 않는다 — 스냅샷을 돌려주는 콜러블을 받으므로 전송과 비밀값은 이미 그것을
소유한 곳에 남고, 실계좌 없이도 시험할 수 있다.

- `live_snapshots.py` — 심볼 → instrument_id 변환과 조립. 규칙 두 개가 핵심이다.
  **평평한 종목은 0이 아니라 부재**(Binance는 한 번이라도 증거금을 잡은 모든
  심볼을 0으로 보고한다), **모르는 심볼은 누락이 아니라 거부**(빼면 브로커가
  들고 있다는 종목에 대해 계좌가 비었다고 보고하게 되고, 대조는 drift 0을
  낸다 — 대조가 잡으라고 있는 바로 그 실패다).
- `live_readers.py` — 세 번역기와 `LiveSnapshotReader`. 주문 id는 **쓰기 쪽과
  같은 빌더**를 쓴다(`kis_provider_order_id` / `toss_provider_order_id` /
  `binance_provider_order_id`). 여기서 형식을 두 번째로 유도하면 두 쪽이 갈라지고,
  기록해 둔 것과 다른 주문 id는 남이 낸 주문으로 읽힌다.

**포트를 하나 고쳤다.** `BrokerOpenOrder.broker_client_order_id`가 필수였는데,
KIS에는 그런 필드가 아예 없고 Toss는 주문 목록에서 빼고 준다. 필수로 두면 세 중
둘은 영원히 대조할 수 없다. 이제 선택값이고, 비교는 **브로커 자신의 주문 id**로
키를 잡는다. 쌍으로 키를 잡으면 "브로커가 되돌려주지 않았다"가 "다른 주문이다"로
바뀌어 한 주문이 두 번 보고된다. 양쪽에 client id가 있고 다를 때만
`OPEN_ORDER_CLIENT_ID_MISMATCH`로 막는다 — 주문 id는 같은데 client id가 다르면,
우리가 내지 않은 주문이 이 계좌에 있고 그것이 우리 id를 가져간 것이다.

**남은 것 — 실계좌 스모크**

자격증명이 DB로 옮겨진 뒤 세 계좌에서 실제로 읽어 drift 0을 확인하는 것.
Binance 실계좌 키는 현재 읽기 전용이며 어떤 주문 경로에도 배선하지 않았다.
US 종목은 시드에 NYSE 거래소만 있으므로 NASDAQ 심볼은 등록이 먼저 필요하다 —
모르는 심볼은 조용히 빠지지 않고 스냅샷을 거부하므로 이 사실은 시끄럽게 드러난다.

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

**4d 진행 상황 (2026-08-27)**

- 완료: HALT/DISARM/EMERGENCY, ARM 재개, `backoffice_command` 명령 원장,
  §11.3 비밀값 화면, **§11.4 리스크 정책 화면 (네 항목 전부)**.
  - 버전 생성 — 승인된 정의 중 DB에 없는 것을 행으로 만든다. 입력 항목이 없고
    값은 정의에서 그대로 쓴다. 2차 비밀번호는 §9 목록대로 **활성화에만** 걸린다.
  - 버전 비교·검증 — 필드별 차이 표시, 2차 비밀번호, 한 트랜잭션 전환.
  - 승인 한도 표시 — 1거래 1%, 레버리지 7, 세션 거래 8,
    KRW 1,000,000 / USD 2,000 / 2,000 USDT. 모두 코드에서 읽는다.
  - 계좌-정책 연결 — `exec_account_risk_policy_binding` 읽기·쓰기 양쪽 배선.
    루프는 `bound_policy()`로 연결에서 정책을 읽고, 연결이 없으면 시작하지
    않는다. 통화·정산자산은 정책 정의에서 나오며 폼에서 고를 수 없다.

**리스크 값의 권위는 코드 (2026-08-27 결정)**

`load_active_policy`는 스냅샷을 DB 행이 아니라 `APPROVED_V6_RISK_POLICIES`
정의에서 만들고, 정의와 다른 행은 거부한다. 즉 **화면에서 숫자를 바꿀 수 없다.**
이 제약은 의도된 것이다(운영 화면에서 리스크 값을 임의로 넓히지 못하게 한다).

거부는 매매 시점이 아니라 화면에서 일어난다. `policy_row_refusal`이 버전 적용과
계좌 연결 양쪽을 막고, 생성은 `policy_row_values` — 검사와 **같은 매핑** — 으로
행을 쓰므로 만들어진 버전이 거부당하는 일은 구조적으로 없다.

새 값을 쓰는 절차:
1. `APPROVED_V6_RISK_POLICIES`에 새 `version` 문자열로 정의를 추가하고 배포한다.
2. §11.4 화면 "버전 생성"에서 행을 만든다.
3. 같은 화면에서 활성화하고(2차 비밀번호) 계좌에 연결한다(2차 비밀번호).

`TRADE_RISK_CEILING`(1%), `MAX_LEVERAGE`(7), `SESSION_TRADE_UPPER_BOUND`(8)은
어떤 정의도 넘을 수 없는 상한이다.

**§11.2 쓰기 절반 (2026-08-27)**

`exec_provider_account_binding`은 읽는 코드도 쓰는 코드도 없었다. 이제
`ProviderBindings`가 읽고 쓰며, 개정은 덮어쓰지 않고 쌓인다 — 개정 3에 기록된
정산 실행이 개정 4 이후에도 읽혀야 하기 때문이다. 통화·환경은 계정에서 나오고
account_seq는 Toss만 요구하므로, 폼으로 범위를 넓힐 수 없다.

§9 목록대로 관문을 나눴다. **사용 시작과 provider 바인딩**은 2차 비밀번호를
요구하고, **계정 추가와 사용 중지**는 인증과 CSRF만 요구한다. 추가된 계정은
비활성이라 매매할 수 없고, 중지된 계정도 매매할 수 없다. 계정을 빼는 조작은
HALT와 같은 이유로 승인 경로가 고장 났을 때도 되어야 한다.

`AccountRepository.create`가 `enabled=True`를 박아 두고 있어서, §9가 막는 상태를
추가 단계에서 그냥 나눠주고 있었다. `enabled`를 기본값 없는 필수 인자로 바꿨다.

**§11.6 provider 증거 (2026-08-27)**

읽기 전용 화면. 루프의 `exec_reconciliation_run`/`_diff`, Toss·Binance 캡처
파이프라인의 실행, `binance_usdm_configuration_fact`의 키 권한을 보여준다.
불일치는 종류와 양쪽 id만 나오고 provider 응답 원문과 비밀값은 나오지 않으며,
지문은 앞 12자만 보여준다.

**§11.6에서 못 하는 것 두 가지를 화면에 적어 뒀다.**
- 요청 한도는 트레이더 프로세스 메모리에만 있고 테이블이 없다. 백오피스는 다른
  프로세스이므로 정직하게 보여줄 수 없다.
- 일회성 provider 캡처 실행은 넣지 않았다. 증거를 보는 화면이 증거를 만들기
  시작하면 둘을 구분할 수 없고, 실 자격증명으로 외부 호출을 버튼 뒤에 두는 것은
  별도 판단이 필요하다.

**§11.5 유니버스와 §11.7 승격은 화면이 아니라 기능이다**

둘 다 뒷받침하는 테이블이 스키마에 **없다**. §11.5는 유니버스 스냅샷·manifest·
staged/active 개념이 통째로 없고(`core_instrument`는 평평한 목록이다), §11.7은
승격 타임라인·세션 클레임 테이블이 없다(`adoption_store`는 브로커 주문 인수로
다른 것이다). 화면 작업이 아니라 설계 + 마이그레이션 + 화면이므로 방향을 먼저
정해야 한다.

**검증**

- 브라우저 E2E: 로그인 → 대시보드 → HALT.
- 루프가 틱을 도는 중에 화면에서 HALT를 눌러 실제로 멈추는지 확인.
- 미인증 요청과 다른 이메일이 전부 거부되는지 확인.

**완료 기준:** 브라우저에서 로그인해 루프 상태를 보고 정지시킬 수 있다.

---

## 트레이더 진입점 (2026-08-27)

트레이더를 시작할 방법이 없었다. 백오피스에는 `__main__.py`가 있는데 트레이더에는
없었고, `binance_paper.run()`은 아무도 호출하지 않았다. Phase 2~3에서 만든 것이
전부 테스트에서만 돌고 있었다는 뜻이다.

```
python -m autotrader.apps.trader --account <alias> --check
```

`--check`가 계정·브로커·provider 바인딩·정책 바인딩·인스트루먼트·manifest를 DB에서
해석하고, **없는 것을 한 번에 전부 이름으로** 보고한다. 하나 고치면 다음 것을
알려주는 방식은 운영자를 재시작으로 목록을 훑게 만든다.

**아직 루프를 돌리지 못한다.** `BinanceLoopInputs`의 여섯 입력이 `src/` 안에
생산자가 없다 — `ExchangeCalendar`, `OrderFlowThresholds`, `FeeSchedule`,
`PessimismInputs`, benchmark 수익률 계열, `atr_ratio`/`range_efficiency`.
다섯 타입은 단위 테스트에서만 생성된다.

지어내지 않고 시작도 하지 않는 쪽을 골랐다. 지어내야 할 값이 tick size·수수료·
자본이고 이는 "운영자의 돈이며 어떤 전략도 지어내서는 안 되는" 것이다. 게다가
`assembly.py`가 regime 없이는 `REGIME_UNAVAILABLE`로 막으므로, 절반쯤 지어낸
입력으로 켜면 **도는 것처럼 보이면서 매 패스 거래를 거부하는** 상태가 된다.
그게 안 켜지는 것보다 나쁘다.

`UNSOURCED_INPUTS`가 그 목록이며 작업 항목이다.

---

## Regime을 원저자 규칙으로 되돌림 (2026-08-28)

전략이 Binance에서 단 한 건의 결정도 만들지 못한 이유를 추적한 결과, 막고 있던
것이 **원저자 방법론에 없는 규칙**이었다.

§2.1은 regime을 하나로 정의한다 — SMA 6/70/200에 대해
`slope(sma200)>0 AND sma70>sma200 AND slope(sma70)>0`. SMA 길이는 "A0 실측,
최적화 대상 아님"으로 못박혀 있다. `_trend`는 이미 정확히 그 규칙이었다.

그런데 `regime.py`가 그 위에 둘을 얹고 있었다.

- **`pessimism_extreme`이 없으면 regime 전체가 UNAVAILABLE** — 원저자에게 비관
  극단은 `signal_c`(0선 아래 MACD 교차) 하나의 조건이다. 게다가 그가 실제로 쓴
  것은 정량 지표가 아니라 언론 패닉 신호이고, 문서는 `action_on_extreme:
  "메소드 적용, 자동 진입 아님"`이라고 적는다. 이것이 모든 거래의 전제조건이
  되어 있었다.
- **`sideways`·`low_volatility`가 스스로 `excluded`를 만듦** — §2.1의 규칙에
  없다. v6.0 작성자가 덧붙인 필터이고, `range_efficiency`는 어느 문서에도
  정의가 없다.

둘 다 게이트에서 뺐다. 관측은 계속하고 기록도 남으므로 나중에 "그 조건에서
성과가 어땠나"를 데이터로 볼 수 있다.

**`benchmark_returns`는 선택이 아니었다.** 리베이스는 양의 상수배이고 이동평균은
선형이라, 종가로 계산한 SMA와 수익률로 재구성한 지수의 SMA는 비교·기울기 결과가
같다. 그 종목 자신의 일간 수익률이 원저자 규칙 그 자체다. `daily_returns()`.

**`ceros` 임계 둘은 optional이 됐다.** §15.2가 "미공개 · LOW · `telemetry_only`"로
분류하고 코드도 어떤 결정에서도 읽지 않는데, 필수 양수값으로 요구하고 있었다.

**이 변경은 시스템이 더 많이 거래하게 만든다.** 세션 내내 안전장치를 조여 온
방향과 반대다. 다만 없는 규칙 때문에 거래를 거부하는 것도 정확한 동작은 아니다.
`--check`의 부족 항목이 일곱에서 셋으로 줄었고, 남은 `put_call_percentile`은
이제 거래를 막지 않고 `signal_c`만 사용 불가로 둔다.

---

## 세션 경계를 측정으로 정함 (2026-08-28)

전략은 인트라데이다. 마감 30분 전에 진입을 끊고 10분 전에 청산을 요구하며,
포지션을 다음 날로 넘기지 않는다. 무기한 선물에는 마감이 없으므로 하루를 어딘가
잘라야 하는데, 원저자는 스스로 닫히는 지수선물을 거래했으므로 이 질문을 겪은 적이
없다.

관례가 아니라 측정으로 정했다. BTCUSDT 시간당 거래대금, 62일 중앙값 기준:

    13:00  3.07배      20:00  0.66배   <- 가장 얕음
    14:00  2.98배      21:00  0.69배
    15:00  2.44배      23:00  0.69배
    16:00  1.56배      22:00  0.75배

**마감 20:00 UTC.** 얕은 블록(20~23시) 전체가 세션 밖에 놓여 그 구간에 포지션을
들고 있지 않게 되고, 강제 청산 창은 19:50 — 중앙값의 0.93배인 19시대 — 에 떨어진다.
얕은 유동성으로 청산하는 것은 반드시 일어나야 할 청산을 나쁜 가격의 청산으로
만드는 방법이다.

대안은 16:00 마감이었다. 하루 중 가장 깊은 시간대로 청산하지만 세션이 얕은 블록을
포지션을 든 채로 관통한다. 지침은 얕은 시간대를 피하는 것이었다.

`session_date_for()`가 필요한 이유: 세션이 20:00에 열리므로 05:00 UTC는 아직
전날 세션에 속한다. 달력 날짜로 키를 잡으면 아직 열리지 않은 세션에 넣게 된다.

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

**진행 (2026-08-28)**

`infra/compose/`에 스택을 만들었다. Caddy만 80/443을 공개하고, 나머지는 published
port가 없다. `tests/architecture/test_compose_exposure.py`가 이 성질을 검사한다 —
compose 파일은 타입 검사도 테스트도 받지 않는 곳이라 주장으로 남겨두지 않았다.

- **migrate** — one-shot. backoffice와 capture가
  `service_completed_successfully`를 기다린다. 마이그레이션 안 된 DB 위의
  백오피스는 거부하지 않고 빈 금고를 그린다.
- **backoffice** — Caddy 뒤에서 8000을 듣는다.
- **capture** — 매일 UTC `CAPTURE_HOUR`에 돈다. 풋콜 계열이 존재하는 유일한
  이유이며, 이 컨테이너가 안 뜬 날은 그 측정을 영영 못 얻는다. 실패해도 루프는
  계속된다 — 거래소 하나가 안 되는 것은 하루치 손실이고, 종료하면 그 뒤 모든
  날을 잃는다.
- **trader-check** — 워커가 아니다. 루프에 생산자 없는 입력이 있으므로 무엇이
  없는지 보고하고 끝난다. `--profile check`로만 뜬다.
- **MySQL·Redis** — `--profile local-data`로만 뜨고 loopback에만 바인드한다.
  이 프로젝트는 이미 외부 DB를 쓰고 있고, 기본으로 두 번째를 띄우면 두 DB가
  반씩 채워지는 길이다.

**컨테이너에 넣기 전에 고친 것:** 백오피스가 `BACKOFFICE_PUBLIC_URL`의 호스트명에
바인드하고 있었다. 리버스 프록시 뒤에서는 컨테이너가 바인드할 수 없는 도메인이다.
`BACKOFFICE_BIND_HOST`/`BACKOFFICE_BIND_PORT`로 분리했고 기본은 loopback이다 —
모든 인터페이스에 바인드하는 것은 누가 그러라고 말했기 때문이어야 한다.

Dockerfile도 고쳤다. `alembic.ini`와 `migrations/`를 복사하지 않아서 migrate
서비스가 붙어서는 올릴 게 없다고 보고했을 것이고, root로 돌고 있었다 — 마스터
키와 DB 비밀번호가 그 프로세스 환경에 들어온다.

**미검증:** 이 머신에 Docker가 없어 이미지 빌드와 스택 기동을 실행해 보지 못했다.
compose 구조와 노출 성질은 테스트로 확인했고, 실제 빌드와 브라우저 E2E는 Ubuntu
호스트에서 해야 한다.

---

---

## Binance 라이브 키 확인과 수수료 (2026-08-31)

자격증명 12개가 `finance_auto_trading_prod`에 저장된 뒤, **읽기 전용 호출만**으로
라이브 계정을 확인했다. 전송 계층에 GET 3개 경로만 화이트리스트했으므로 주문
경로는 쓰이지 않은 게 아니라 **도달 불가능**했다.

**키는 실제로 읽기 전용이다.** `/sapi/v1/account/apiRestrictions`가
`enableReading: true`, 나머지(`enableFutures`·`enableSpotAndMarginTrading`·
`enableWithdrawals`·`enableInternalTransfer`·`permitsUniversalTransfer`) 전부
false로 답했다. `/fapi/v2/account`의 `canTrade: true`는 **키가 아니라 계정**에
대한 값이라 이 질문에 답하지 못한다 — 그것만 봤으면 반대로 읽었을 것이다.

**두 가지가 남는다.**

- `ipRestrict: false` — 키가 어느 IP에서나 쓰인다. 지금은 읽기 전용이라 노출
  피해가 제한되지만, 주문 권한을 켜는 순간 이 설정이 유일한 방벽이 된다.
- `totalWalletBalance: 0` — USD-M 선물 지갑이 비어 있다. `APPROVED_CAPITAL`은
  2,000 USDT 증거금인데 아직 이체된 적이 없다.

**수수료는 정의 문제가 아니라 인증 문제였다.** BTCUSDT maker 0.02%, taker 0.05%.
`binance_commission.py`가 서명 호출로 읽고, 두 다리 모두 taker로 계산한다 —
진입이 대기하면 maker를 벌지만 크로스하면 taker를 내고, 이 숫자는 거래가 자기
비용을 넘는지 판정하는 필터에 들어간다. 리베이트를 가정하면 값을 매기지 않은
거래가 통과한다.

`--check`의 부족 항목이 셋에서 둘로 줄었다: `put_call_percentile`(60일 누적
필요), 그리고 원저자 regime에 없는 `atr_ratio`/`range_efficiency`.

---

---

## Phase 3 라이브 스모크 (2026-08-31)

IP 제한이 걸리고 USD-M 지갑에 362.52 USDT가 들어온 뒤, Phase 3의 스냅샷 리더를
**처음으로 실계좌에 대고** 돌렸다. 여덟 개 서명 엔드포인트 전부 정확히 파싱됐고
거부 없음. 포지션 0, 미체결 0, 창 안의 income 1건(방금 그 입금).

**`capture_binance_usdm_account`는 실제 시계로는 절대 통과할 수 없었다.**
`_epoch_ms`가 `as_of`에 정확한 밀리초를 요구했는데 `datetime.now(UTC)`는 그런
값을 만들지 않는다. 호출자가 `src/` 안에 없어서 — 유닛 테스트가 상수를 넘겨주는
곳에서만 불렸다 — 이 조건이 한 번도 물린 적이 없었다. 또 한 번 "한쪽만 연결됨".

캡처가 스스로 밀리초로 **내림**하도록 고쳤다. 반올림이 아닌 이유는 아직 오지
않은 순간을 읽었다고 주장하지 않기 위해서고, 내림한 값을 스냅샷의 `as_of`로
돌려주므로 스냅샷은 자기가 질의한 바로 그 순간을 말한다.

**운영 범위가 정해졌다.** BTCUSDT mid 77,688 · tick 0.10 · 최소수량 0.001 BTC
(= 77.7 USDT 명목) · 최소명목 50 USDT. 지갑 362.52 USDT에 `TRADE_RISK_CEILING`
1%를 적용하면 거래당 위험은 **3.63 USDT**다. 손절폭이 진입가의 약 4.7%보다
넓어지면 산출 수량이 거래소 최소수량 밑으로 떨어져 주문이 거부된다. 이 전략의
손절은 그보다 훨씬 좁으므로 실제 제약은 아니지만, 자본이 이 크기일 때의 경계다.

**`APPROVED_CAPITAL`의 2,000 USDT와 실제 362.52 USDT는 다르다.** 엔진은 승인액이
아니라 실계좌 자본으로 사이징하므로(`min(session_start_equity, current_equity)`)
거래 크기는 틀리지 않는다. 승인액은 화면이 "얼마를 들고 있어야 하는가"를 보여주기
위한 값이고, 지금은 사람이 눈으로 비교한다.

---

---

## §11.5 유니버스 권위 (2026-08-31)

Phase 4의 마지막 화면. 스키마·리포지토리·화면·테스트까지 끝났다.

**행 편집기가 없는 이유를 코드로 옮겼다.** 유니버스는 개별 종목의 모음이 아니라
특정 날짜의 발표된 목록 하나다. 그래서 쓰기 단위가 매니페스트 전체이고, 종목을
받는 라우트가 존재하지 않는다 — 라우트 집합 자체를 테스트가 고정한다.

**표가 필요했던 이유는 과거다.** "그 거래를 결정한 날 이 종목이 KOSPI 200
보통주였는가"는 답이 있는 질문이고, 어제 목록을 덮어쓰는 순간 답이 사라진다.
활성화는 supersede이고, `membership(as_of=...)`은 그 날짜에 유효했던 스냅샷을
찾는다. superseded 행도 후보에 포함된다 — 그게 어제에 대한 질문의 답이다.

**상태를 열로 두지 않았다.** 타임스탬프 셋과 마커가 상태다. 상태 열은 자기가
요약하는 시각과 어긋날 수 있고, 이건 backoffice_secret_activation이 이미 쓰던
방식이다. 유니버스당 활성 하나는 NULL 마커 유니크 인덱스, 생애주기는 CHECK
제약 — 화면이든 CLI든 떠도는 SQL 클라이언트든 같은 규칙을 받는다.

**파서가 지어내지 않고 거부하는 것 셋.** 주식 유니버스에서 보통주 플래그가 없는
구성원(`NOT_MEMBER_AS_OF`와 `NOT_COMMON_STOCK_AS_OF`는 서로 다른 말을 한다),
무기한 선물에 붙은 주식 종류(BTCUSDT에는 없다), 중복 심볼(마지막 것을 남기면
우선주가 옆에 적힌 보통주를 덮는다).

**다이제스트는 구성원과 날짜를 덮고 출처는 덮지 않는다.** 같은 발표 목록을 다른
미러에서 받아도 같은 유니버스이고, 종목 하나가 바뀌면 언제나 다른 유니버스다.

**스테이징은 2차 비밀번호를 요구하지 않고 활성화는 요구한다.** 아무도 읽지 않는
목록을 저장하는 것은 아무것도 바꾸지 않는다. §9가 비밀번호를 두는 곳은 활성화 —
돌고 있는 루프 밑에서 전략의 필터가 바뀌는 순간이다. 스테이징도 감사에는 남는다.
"나중에 활성화된 그 목록을 누가 올렸는가"는 활성화 기록만으로는 답이 안 나온다.

**아직 한쪽만 연결되어 있다.** `evaluate_cash_universe`를 부르는 곳이 `src/`에
없고, 만들 수도 없다 — 현금 시장 루프 자체가 없고, 이 필터의 나머지 입력(20일
거래대금 중앙값, 섹터 70일 수익률 순위, 국가 강도)도 생산자가 없다. Binance에서
유니버스는 `NOT_APPLICABLE`(`UNIVERSE_CASH_ONLY`)이므로 유일하게 실계좌가 있는
시장에서는 이 필터를 지나지 않는다. 권위 쪽은 완성됐고, 전략 쪽은 KRX/US 루프가
생기는 시점의 작업이다.

---

---

## put_call_percentile: 백필 불가, 그리고 애초에 원저자 것이 아님 (2026-08-31)

"이전 데이터로 수집하면 되지 않나"를 확인했다. **안 된다 — 측정했다.**

| Deribit 엔드포인트 | 이력 |
|---|---|
| `get_trade_volumes` | 롤링 24시간·7일·30일. 일별 없음 |
| `get_last_trades_by_currency_and_time` | 24시간 전 응답, **30시간 전부터 빈 응답** |

체결 테이프에서 일별 콜/풋 물량을 재구성하는 우회로도 막힌다. 공개 테이프 보관이
약 하루다. 운영 DB 상태: breadth 398일, realised_volatility 369일(둘 다 백필됨),
put/call **1일**. 일일 캡처로 60일을 채우면 10월 말이었다.

**3자 데이터 백필은 하지 않는다.** 퍼센타일은 자기 계열 안에서의 순위다. 앞 60일만
남의 자로 잰 계열이 되면 오늘을 남의 자에 대고 순위 매기는 것이고, 그건 없는 것보다
나쁘다 — 측정처럼 보이는 숫자가 나온다.

**그런데 이 지표 자체가 원저자 것이 아니었다.** §2.3이 명시적으로 나눠 적는다.

```yaml
quantitative: [put_call_ratio, vix_percentile, breadth]   # 커리큘럼 M3
qualitative: media_panic_signal                            # (A0) 실제로 사용
```

정량 3종은 교재이고, `(A0) 실제로 사용` 표시가 붙은 것은 정성적 언론 패닉 신호다.
원저자는 신문을 읽었다. 교재의 3종을 **전부** 요구한 탓에, 그가 쓰지도 않은 성분
하나가 없다는 이유로 `signal_c`가 영구히 사용 불가였다. regime 때와 같은 종류의
문제다 — 원저자 방법에 없는 규칙이 결정을 막고 있었다.

**의도적으로 지킨 것 둘.** 임계는 느슨해지지 않는다: 극단은 여전히 동의하는 성분
둘을 요구하므로, 둘만 측정되면 둘 다 동의해야 한다. 성분이 하나뿐이면 `False`가
아니라 `None`이다 — 하나는 애초에 둘이 아니다. 그리고 없는 퍼센타일은 여전히
건너뛰지 채우지 않는다. 채운 값은 이후 모든 순위에 계속 세어진다.

**운영 데이터로 확인.** 전에는 `None`이던 것이 이제 판정을 낸다 — 변동성 68분위,
breadth 60분위는 극단이 아니고, 그게 정답이며 이전에는 얻을 수 없던 답이다.
put/call은 60일이 쌓이면 세 번째 성분으로 합류한다. 버리지 않고 계속 기록한다.

**변동성 성분은 종목 자신의 실현변동성이고 원저자의 것은 VIX 퍼센타일이다.**
이 대체는 이제 코드에 이름으로 적혀 있다.

**이 변경도 필터를 느슨하게 만든다.** regime 때와 같은 이유다 — 원저자에게 없던
규칙 때문에 거래를 거부하는 것도 정확한 동작은 아니다.

---

---

## 확정 규칙과 추정 규칙의 분리 (2026-08-31)

문서 5행이 다른 무엇보다 먼저 놓는 원칙:

> **확정 규칙과 추정 규칙을 절대 섞지 않는다.** A0/A1은 전략 코어로 고정할 수
> 있지만, V1/C/X1은 ... 백테스트·섀도 거래를 통과하기 전에는 **주문 권한을 주지
> 않는다.**

세 군데가 이를 어기고 있었다.

**1. 셋업 등급이 주문 크기를 정하고 있었다.** §21.3은 제목부터 "연구용 점수표"이고
끝에 "이 점수는 David의 직접식이 아니다 ... Ablation으로 검증하기 위한 연구
프레임이다"라고 적는다. §15.2는 이 점수가 대신하는 Cyborg 큰 구간 판정을
`score_only`로 분류한다. 그런데 등급 A가 Binance에서 0.0050을, NORMAL이 0.0025를
받았다 — **연구용 표가 실제 주문을 두 배로 키우고 있었다.** 사이징은 주문 권한이다.
`RESEARCH_SCORE_AUTHORITY`가 `SCORE_ONLY`인 동안 상향 비율은 normal로 눌린다.

**크기만 누른다.** 막는 등급은 계속 막는다 — 현금 정책에 A_CANDIDATE 비율이 없는
것은 여전히 거부이고, 거부와 작게 주문하는 것은 다른 답이며 전자를 후자로 바꾸는
일은 절대 없어야 한다.

**2. `mandatory_indicator_codes`가 아무 코드나 받았다.** 지표를 필수로 만드는 것은
주문 권한의 가장 강한 형태다 — 그것이 없으면 진입 자체가 없다. §15.2가
`score_only`로 두는 다섯(Secado, Cero osmótico, 반전 MIG, 그리고 미공개 Big Trades
임계값에 기대는 둘)을 `HYPOTHESIS_CODES`로 묶고 필수 지정을 이름으로 거부한다.
엔진 테스트가 "매칭된 지표 전부를 필수로" 두고 있어서 여태 드러나지 않았다.

**3. `telemetry_only`를 읽는 코드가 없었다.** 모든 telemetry 액션이 우연히 수량
없이 만들어져서 규칙이 생성 방식으로만 지켜지고 있었다. 이제 dataclass의 검증이다 —
telemetry 액션은 order_style·quantity·stop_price·account_halt를 가질 수 없다.
25%·50% 되돌림이 그 사례다(§11.4: 주문 없음, 66%만 청산).

권위는 configuration manifest에 들어간다. 등급이 포지션을 키울 수 없던 동안의
결정과 키울 수 있게 된 뒤의 결정은 **다른 빌드의 결정**이다.

**바꾸지 않은 것 하나.** `BLOCKING_BIG_TRADE_AHEAD`는 -4점이 아니라 하드
블로커다. Big Trades 임계값은 미공개이고 §15.2는 `score_only`로 두므로 문자 그대로는
불일치다. 그러나 문서의 원칙은 추정 규칙에 **주문 권한**을 주지 말라는 것이고,
블로커는 거절만 한다 — 아무것도 승인하지 않는다. 이를 점수로 낮추면 시스템이 더
많이 거래하게 되고, 그건 문서가 요구하지 않는 해석에 기대어 안전장치를 푸는 일이다.
그대로 둔다.

**이 변경들은 앞의 둘과 달리 조인다.**

---

---

## 원저자 방식 전면 재검토 (2026-08-31)

§12(A0 코어 설정)를 기준으로 구현 전체를 문서와 대조했다. §12는 `prohibited`
목록과 엔진별 고정값을 함께 담고 있어 대조 기준으로 쓸 수 있다.

### 일치 확인된 것

| 문서 | 코드 |
|---|---|
| SMA 6/70/200, MACD 12/26/9 | `metodo.py` 그대로 |
| `regime: slope(sma200)>0 AND sma70>sma200 AND slope(sma70)>0` | `trend_up` 동일 |
| `signal_a/b/c`, `exit: cross_down(sma6, sma70)` | `normal_technical_confirmation`, `metodo_exit_signal` |
| `sector_rank_max: 3`, `exclude_sectors: [real_estate, financials, energy]` | `universe.py` |
| HLIT 앵커 `A = max(high) between low1..low2 ; B = low2` | `hlit.py:_setup` 그대로 (약세는 거울) |
| `fib_levels [0.25,0.50,0.66]`, `target 0.66` | `FIB_LEVELS`, `TARGET_LEVEL` |
| `precondition: regular_divergence_required` | 히든 다이버전스는 setup을 만들지 않음 |
| `exhaustion_def: 연속 3+ 극점 갱신 + 계단식 거래량 감소` | `len(pivot_history) >= 3` + `price_extends and volume_decreases` |
| `open_first_15min size_multiplier 0.5`, `monday score_penalty -1` | `_OPEN_WINDOW_MULTIPLIER`, 월요일 강등 |
| `news: 10분/120분/NFP 전 세션` | `calendar.py` |
| `trade_frequency.hard_upper_bound: 8` | `SESSION_TRADE_UPPER_BOUND` |
| `stop.order_type STOP_MARKET`, `percentage_based: forbidden` | 구조적 스톱 + trigger_price, limit 없음 |
| `never_add_while_losing`, `never_widen_stop` | `favorable <= 0` 거부, `_non_widening_stop` |
| `add_threshold: 0.35 × ATR(14, 5m)` (§14.2 정규화) | `_add_action` 임계 |
| `BE +1.5포인트 → 왕복 수수료 + 슬리피지 + 버퍼로 재계산` (§14.2) | `_break_even_stop` |

`blind_limit_entry_at_zone`·`range_breakout_entry`·`fighting_big_trades`·
`martingale`·`stop_limit_protective`·`fixed_daily_trade_count`는 해당 코드가
아예 없거나 반대 동작으로 구현되어 있다.

### 고친 것: `permanent_long_only`

§12의 `prohibited`에 `permanent_long_only`가 있고, §2.2는 숏을 롱의 정확한
거울(`downtrend_regime and cross_down(sma6, sma70)`)로 주며 §2.4는 Baxter를 숏
대상으로 든다. **원저자는 숏을 한다.**

엔진은 시장 코드에 대고 `CASH_SHORT_UNSUPPORTED`를 냈다. 이건 "현금 시장에서
메소드가 롱 전용"으로 읽힌다. 사실이 아니다 — 그 두 계좌가 **현물이라 차입을 못
할 뿐**이다. `SPOT_ONLY_MARKETS` / `SPOT_VENUE_CANNOT_SHORT`로 바꿨다. 증거금
가능한 현금 거래소가 생기면 그 선의 반대편에 놓이지, 원저자가 취한 적 없는 자세를
물려받지 않는다. 동작은 그대로다. Binance USD-M에서 거울이 실제로 도달 가능한지도
테스트가 고정한다.

### 앞선 세 건과 함께

이번 재검토로 원저자 정합성 작업은 다음 다섯 건이 됐다.

1. regime에서 원저자 규칙에 없는 게이트 제거 (느슨해짐)
2. 비관 극단을 커리큘럼 3종 전부가 아니라 측정된 것으로 판단 (느슨해짐)
3. 연구용 점수표에서 주문 권한 회수 — 등급이 포지션을 키우지 못함 (조여짐)
4. `telemetry_only`를 검증으로 (조여짐)
5. 롱 전용을 거래소 제약으로 정정 (동작 불변)

---

---

## Big Trade를 타이핑한 숫자가 아니라 테이프에 대고 재기 (2026-08-31)

### Big Trade가 무엇인가

§19.1(A1, 플랫폼 구조 확인됨): ATAS의 Big Trades는 **단일 대형 체결 또는 유사
체결의 누적 그룹**이다. `Cumulative Trades` 모드에 `Auto Filter`
(Strong/Medium/Weak)로 표시하며 고정 계약 수가 아니다. 문서는 고정값을 명시적으로
거부한다 — "고정값 하나를 선택하는 대신 'RTH 한 세션당 의미 있는 마커가 몇 개
나오는가'를 기준으로 조정하는 것이 낫다", 목표는 "실제로 경로를 막는 소수의
이벤트만".

§22.5가 Binance 정규화를 정확히 준다.

```python
aggregated_event = aggregate_trades(same_aggressor_side=True,
                                    window_ms=150, max_price_distance_ticks=2)
normal_big_trade  = event_notional >= rolling_quantile(0.995)
extreme_big_trade = event_notional >= rolling_quantile(0.999)
```

### 대조

**집계 절반은 이미 정확했다** — 같은 공격 방향, 150ms, 2틱, 그리고 체결이 아니라
**이벤트** 단위 명목가 합산. 이게 핵심이다: 기관이 주문을 쪼개면 체결은 여러
건이고, 따로 재면 마커가 찾으려던 참가자가 그대로 숨는다.

**임계값 절반이 틀렸다.** 운영자가 두 숫자를 타이핑하게 되어 있었고, 그
`OrderFlowThresholds`는 테스트에서만 생성되고 있었다 — 또 한쪽만 연결됨.

### 고친 것

분위수 방식으로 교체. **최근접 순위**를 쓴다 — 보간 분위수는 어떤 이벤트도 갖지
않은 명목가를 만드는데, 그 값을 이벤트 명목가와 비교하기 때문이다.

**실패 모드 둘, 같은 절에서 가져왔다.**

- **표본 200건 미만 → `big_trades = None`**, 빈 튜플이 아니다. "장애물 없음"과
  "보지 못함"은 다른 답이고, 후자를 길이 열렸다고 읽으면 안 된다.
  `blocking_big_trade_ahead`는 False 대신 예외를 던지고, 엔진이
  `BIG_TRADE_SAMPLE_INSUFFICIENT`로 막고, 점수는 어느 쪽으로도 주지 않는다.
- **평평한 분포는 `>=`를 전부 통과한다.** §22.5의 2차 통제가 이걸 위한 것이다 —
  `세션당 이벤트 수를 함께 통제한다`, `[5, 10, 20]`. **20**(그리드 상단)을 택했다.
  이 마커는 진입을 거절만 하므로 상한이 클수록 더 많이 거절한다. 작은 값은 안전을
  깔끔함과 바꾸는 것이다.

표본이 짧아도 Big Trades만 사라진다. MIG·secado·ceros는 봉과 체결에서 직접 읽고
무엇에도 순위 매기지 않으므로, 같이 무효화하면 측정된 사실을 숨기게 된다.

운영자가 지어내야 할 order-flow 숫자가 **2개 → 0개**가 됐다.

### 함께 확인한 것: 방해 Big Trade는 감점이자 거부다

§9.2가 둘 다를 명시한다 — "진행 방향 앞의 Big Trade → 진입 감점 **또는** 거부".
-4점(§21.3)은 이미 구현되어 있었고, 하드 블로커는 별개 규칙이다. 거부 쪽 근거가 더
강하다: §9.2의 `never: "Big Trade와 맞서 진입하지 않는다"`, §12의
`prohibited: fighting_big_trades`, §21.2의 필수조건
`no_blocking_big_trade_before_1_5r`, 그리고 §0이 기록한 v6.0의 정정("확인
신호(+1점)" → "회피 대상. 맞서 싸우지 않는다"). 반면 §21.3은 스스로 "David의
직접식이 아니다"라고 적는다.

게다가 §21.3을 `SCORE_ONLY`로 묶은 뒤라 -4점만 남기면 **효과가 0이 된다.**
블로커를 빼는 것은 완화가 아니라 삭제다. 테스트로 이 관계를 고정했다.

---

---

## Shadow 루프가 돈다, 그리고 어디서 멈추는가 (2026-09-01)

`--run --shadow --leverage <n>`이 실계좌에 대해 실제로 시작한다. 자격증명 호출은
전부 진입점에서 한 번만 일어나고 루프는 값만 받는다 — 수수료와 잔고를 읽는 키는
주문도 낼 수 있는 키이고, 그 키를 든 루프는 쓰는 것과 한 import 거리다.

```
mode          SHADOW
account       lmhml0237 (LIVE)
equity        352.78276087 USDT
tick size     0.10
orders        none; this loop has no execution port to submit to
```

### 돌려보고 찾은 것

**프로덕션이 4개 테이블 뒤처져 있었다.** 통합 마이그레이션을 모델에서 재생성하는
방식은 테스트 DB가 매번 새로 만들어져서 동작하고, **이미 스탬프된 DB에서는 조용히
동작하지 않는다.** `apply_schema`가 드리프트를 보고하고 없는 테이블을 만든다.
컬럼 드리프트는 손대지 않는다 — 고치려면 기존 행이 무엇을 담을지 정해야 하고,
그건 사람이 쓰고 읽는 마이그레이션이다.

**전체 계좌 캡처가 원인을 지운다.** 잔고 하나에 8개 엔드포인트를 부르고 하나만
실패해도 `"snapshot is incomplete"`에 `from None`으로 원인을 버린다. 진입점은
`/fapi/v3/balance` 하나만 읽는다 — 질문 하나, 엔드포인트 하나.

**`NOT_LEADER`.** 결정이 안 쓰인 첫 이유는 리스였다. 매 실행이 새
`runtime_instance_id`를 만들고 TTL이 2분이라 짧은 반복 실행이 자기 전임자의 리스에
걸렸다. 리스가 정확히 제 일을 한 것이다.

### 지금 막고 있는 것: 체결 스트림이 없다

리스를 잡은 뒤에도 결정이 0인 이유가 나왔다.

```
5m bars        : 1499     ✓
risk context   : built    ✓
daily bars     : 1499     ✓
trade prints   : 0        ← 여기
loop inputs    : None
```

`BinanceUsdmMarketData.trade_prints`는 **스토어에서 읽기만 한다.** 쓰는 쪽은
`ingest_agg_trade`이고 그것은 **웹소켓 이벤트**를 받는다. 그리고 웹소켓
클라이언트가 `src/` 어디에도 없다 — `ingest_agg_trade`를 부르는 것은 테스트뿐이고,
`pyproject.toml`에 웹소켓 의존성도 없다.

그래서 order-flow 절반(Big Trades, MIG, secado, ceros, 30초 ATR, extreme delta)은
프로덕션에서 데이터 출처가 없다. `_recover_gap`이 REST로 빈 구간을 메우도록 이미
쓰여 있는 걸 보면 설계는 스트림을 전제하고 있었다.

이것이 다음 작업이고, 작은 일이 아니다: 재연결, 순서 보장, 갭 복구, 그리고 그
전부가 이미 있는 체크포인트 규약과 맞아야 한다.

---

## 미검증 항목

이 계획을 시작하는 시점에 아래는 **한 번도 확인된 적이 없다.** 어느 것도 되어 있다고
가정하지 않는다.

- MySQL 스키마 적용 (마이그레이션이 실제 DB에 적용된 적 없음)
- 실계좌 provider 호출 — Binance는 계정 스냅샷 캡처까지 실인증으로 확인됨
  (2026-08-31). KIS/Toss는 아직 실인증으로 호출된 적 없음
- Google OAuth
- Docker Compose 기동과 브라우저 E2E
- 전략의 실제 손익 특성 — 백테스트 하네스(`research/david_v6/backtest.py`)는 있지만
  실데이터로 돌린 결과가 없다

## 참고

전략 문서는 이 전략의 승률 86% 보고에 대해 원저자 본인이 "그 비율은 내려간다"고
경고한 것을 기록하고 있다. 그 숫자를 시스템 목표로 삼지 않는다.
