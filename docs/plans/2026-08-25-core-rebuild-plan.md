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
