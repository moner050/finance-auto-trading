# Finance Auto Trading 인수인계

> 기준 시점: 2026-08-25 / 코드 기준 커밋: `5293939` / 브랜치: `codex/task-2-hlit-h1-exhaustion`
>
> 이 문서는 소스 코드와 로컬 검증 기록의 인수인계용 요약이다. 운영 DB, 브로커 계정,
> 외부 API, Google OAuth를 실시간으로 재검증한 결과가 아니며, 서비스가 LIVE라는 뜻도 아니다.

## 1. 현재 상태

프로젝트는 David Trullás Vila 전략의 **v6만 런타임에 남긴 상태**다. v5 런타임
표면은 퇴역했고, KIS 국내 현금, Toss 미국 현금, Binance USD-M 선물에 대한 v6
실행·복구·조정·승격 제어 코드가 소스에 구현되어 있다.

현재 가장 큰 미완료 축은 운영자를 위한 **단일 사용자 백오피스**다. 백오피스 13개
작업 중 1~3번까지 구현·검토가 끝났고, 4번부터 13번은 아직 시작하지 않았다.
따라서 현재 커밋에는 로그인 가능한 GUI, DB 비밀값 등록 CLI, Docker/Caddy 배포
패키지가 없다.

## 2. 즉시 참고할 문서

| 목적 | 문서 |
| --- | --- |
| 백오피스 요구사항·보안 경계 | [`docs/superpowers/specs/2026-08-25-trading-backoffice-design.md`](superpowers/specs/2026-08-25-trading-backoffice-design.md) |
| 백오피스의 순서화된 구현 작업 | [`docs/superpowers/plans/2026-08-25-trading-backoffice-implementation-plan.md`](superpowers/plans/2026-08-25-trading-backoffice-implementation-plan.md) |
| 실제 수행 원장 및 사전 판단 | [`.superpowers/sdd/2026-08-25-trading-backoffice-implementation-plan/progress.md`](../.superpowers/sdd/2026-08-25-trading-backoffice-implementation-plan/progress.md) |
| v6 전략의 원본 분석 기준 | [`docs/David_Trullas_Vila_전략분석_자동매매용_v6.0_화면역공학통합판.md`](David_Trullas_Vila_전략분석_자동매매용_v6.0_화면역공학통합판.md) |
| v6 활성화·권한 구현 계획 | [`docs/superpowers/plans/2026-08-25-david-v6-activation-authority-implementation-plan.md`](superpowers/plans/2026-08-25-david-v6-activation-authority-implementation-plan.md) |
| 이전 v5 퇴역 및 v6 전환 계획 | [`docs/superpowers/plans/2026-08-24-david-v6-cutover-activation-implementation-plan.md`](superpowers/plans/2026-08-24-david-v6-cutover-activation-implementation-plan.md) |

## 3. 완료된 기능

### v6 거래 기반

- David v5 운영 경로를 퇴역하고 v6 선택 경로만 남겼다.
- KIS, Toss, Binance USD-M의 계약 증거·비밀 참조·주문 복구·조정·보호·권한 및
  승격 제어가 커밋 이력에 구현되어 있다.
- 바인딩 단위 승격 권한, 정확한 시점의 종목 유니버스, 완전한 세션 매니페스트,
  Binance 권한 관찰, readiness/incident, 원자적 LIVE 활성화·HALT, v6 CLI가
  구현되어 있다. 대표 커밋은 `fb034e6`부터 `f455078`까지다.
- 이 구현은 LIVE 전환을 자동으로 승인하지 않는다. 각 바인딩은 별도 read-only
  readiness, 두 번의 Shadow, 두 번의 Paper, 조정 결과, 보호 상태, 독점 소유권을
  확인한 뒤에만 활성화할 수 있다.

### 백오피스 기반 (완료: Task 1~3)

| 작업 | 완료 커밋 | 결과 |
| --- | --- | --- |
| 1. 설정·의존성 | `2070221`, `30391e8` | HTTPS 공개 URL과 정확히 32바이트인 master key만 허용하는 설정 검증을 추가했다. 잘못된 URL authority 수용 결함도 보완했다. |
| 2. MySQL 스키마·ORM | `9376b17` | migration `0044`와 백오피스 비밀값, 활성화, 2차 비밀번호, bootstrap authority, 명령 원장 스키마를 추가했다. |
| 3. 암호화·2차 비밀번호 | `edd5610`, `5293939` | AES-256-GCM, 정확한 `secret://db/<lower-kebab-name>@active`, 도메인 분리 SHA-256 지문, Argon2id, 출력 마스킹을 추가했다. |

Task 3는 독립 리뷰에서 평문 문자열이 객체 수명 동안 남는 문제를 발견해 수정했다.
수정 후 `SecretValue`는 직접 평문 문자열/바이트를 보관하지 않으며, accessor 호출의
임시 mutable 버퍼를 즉시 덮어쓴다. 이는 우발적 평문 캐시를 막는 경계일 뿐,
프로세스 메모리가 침해된 경우의 보안 경계는 아니다.

## 4. 남은 구현 순서

백오피스 계획의 아래 순서를 바꾸지 않는 것이 안전하다. 각 작업은 RED → GREEN,
독립 리뷰, focused commit을 유지한다.

1. **Task 4 — 비밀값 영속화·최초 bootstrap·rekey**: MySQL repository와 로컬
   non-echo CLI를 작성한다. 현재/이전 master key를 엄격히 검증하고, 모든 암호문
   재암호화는 하나의 잠금 트랜잭션으로 처리한다.
2. **Task 5 — provider dotenv 제거**: KIS/Toss/Binance의 provider caller를 DB
   resolver로 이동한다. 기존 `.env` provider secret 경로는 fail-closed로 남긴다.
3. **Task 6 — Google OIDC·Redis session·CSRF·one-use approval**: 검증된
   `lmhml0237@gmail.com`만 인증한다. 위험 작업에는 action-bound 2차 비밀번호
   승인을 부여한다.
4. **Task 7 — 인증된 대시보드**: FastAPI/Jinja/HTMX 읽기 전용 shell, redacted
   projection, health 화면을 구현한다.
5. **Task 8 — 명령 gateway 및 감사**: durable idempotency, optimistic digest,
   트랜잭션과 redacted audit을 모든 mutation의 공통 경계로 만든다.
6. **Task 9 — Secrets/Accounts/Bindings/Policy/Universe 화면**: 직접 테이블
   편집 없이 typed command만 사용한다.
7. **Task 10 — Provider/Reconciliation/Promotion/LIVE 화면**: 기존 서비스만
   감싸며 CLI subprocess나 ad-hoc SQL을 허용하지 않는다.
8. **Task 11 — GLOBAL/Incident/Audit/System 화면**: HALT·DISARM·EMERGENCY는
   2차 비밀번호 없이 즉시 가능해야 한다.
9. **Task 12 — Ubuntu Docker Compose/Caddy**: Caddy만 80/443을 공개하고,
   MySQL/Redis/backoffice/worker는 private network에 둔다.
10. **Task 13 — 최종 검증**: security scan, browser/Docker, disposable MySQL,
    전체 suite를 수행한다. 실제 Google OAuth smoke는 정확한 도메인과 배포 권한을
    받은 뒤에만 한다.

## 5. 반드시 지켜야 할 운영·보안 계약

- 서버 `.env`에는 DB 접속 정보와 활성 32바이트 master key만 둔다. `.env`를
  이미지 또는 Git에 넣지 않는다.
- provider key, account identifier, Google OAuth secret은 MySQL의 AES-GCM
  암호문으로만 보관한다. 2차 비밀번호는 Argon2id verifier만 저장한다.
- 브라우저·로그·감사·예외·HTML/JSON에 평문 비밀값, 암호문, nonce, verifier,
  OAuth token, authorization header, raw provider payload, 전체 계좌번호를
  남기지 않는다.
- 위험한 활성화/변경은 CSRF, typed command, durable idempotency, optimistic
  digest, fresh one-use second-password approval, redacted audit을 모두
  통과해야 한다.
- HALT, DISARM, kill-switch escalation, EMERGENCY는 인증과 CSRF만 필요하며,
  2차 비밀번호·provider·secret·readiness/freshness에 의존하면 안 된다.
- Web route는 ORM trading-state DML, 직접 SQL console, CLI subprocess 호출을
  해서는 안 된다.
- LIVE는 화면에서 보이는 digest만으로 활성화하지 않는다. 기존 lock-and-recompute
  트랜잭션으로 각 exact binding을 다시 판정해야 한다.

## 6. 검증 기록과 미검증 항목

가장 최근 백오피스 Task 3의 로컬 검증 기록은 다음과 같다.

- focused security/application tests: `38 passed`
- Pyright: `0 errors`
- Ruff format/check: 통과
- Task 3 최초 구현 시 전체 suite: `2200 passed, 128 skipped` (수정 라운드 후에는
  focused regression만 재실행됨)

Task 1과 2도 각 focused test와 full suite를 통과했으며, Task 2의 migration 적용은
**사용자 승인된 disposable MySQL**이 필요해 Task 13으로 명시적으로 미뤄졌다.

아직 검증하지 않은 사항:

- Task 4 이후 백오피스 기능 전체의 MySQL 트랜잭션/동시성/rollback 검증
- Docker Compose와 Caddy의 실제 기동 및 브라우저 E2E
- Google OAuth의 private staging smoke
- 운영 DB에서의 read-only readiness와 provider/계정/position/reconciliation 사실
- Shadow 2회와 Paper 2회가 각 LIVE binding별로 실제로 `PASSED`인지의 persisted
  evidence
- LIVE 활성화 및 운영 배포

## 7. 다음 담당자를 위한 시작 절차

1. 이 문서가 가리키는 worktree와 `git status --short`를 먼저 확인한다. 이 문서의
   기준은 `5293939`이며, 깨끗한 worktree에서 이어가는 것을 전제로 한다.
2. [`progress.md`](../.superpowers/sdd/2026-08-25-trading-backoffice-implementation-plan/progress.md)의
   ruling 네 개와 Task 1~3 완료 기록을 읽는다. Task 4를 시작하기 전에 재설계하거나
   Task 1~3을 다시 구현하지 않는다.
3. Task 4의 disposable MySQL 테스트에 쓸 대상은 `TEST_DISPOSABLE_DATABASE_URL`만
   사용한다. 비로컬 DB를 쓰려면 사용자의 명시적 승인과
   `AUTOTRADER_AUTHORIZED_TEST_DATABASE_FINGERPRINT` 일치, 정확한 cleanup이
   필요하다.
4. 운영 DB에 쓰기 전에는 2026-08-24/25 v6 activation 계획의 read-only gate를
   완료하고, 상태를 `LIVE`로 추정하거나 선언하지 않는다.
5. 변경 후에는 최소한 변경 범위의 tests, Ruff, Pyright를 실행한다. 백오피스 전체
   완료 시에는 Task 13의 전체 검증을 수행하고 결과와 외부 smoke-test 공백을 기록한다.

## 8. 결정 기록

- Docker worker는 현재 실제로 구현된 반복 Toss reconciliation만 패키징한다.
  KIS/Binance용 연속 worker loop를 새로 가정하지 않는다.
- 실제 Google OAuth staging smoke는 공개 배포 권한이 아니다. 정확한 domain과
  deployment authority가 있어야 수행한다.
- master-key 테스트 literal은 32바이트로 decode되는 값만 사용한다. 양식에 있던
  35바이트 literal은 거부하는 것이 맞다.
- `0044` 스키마의 생략된 길이·collation·FK·unique/state 조건은
  `.superpowers/sdd/2026-08-25-trading-backoffice-implementation-plan/task-2-schema-ruling.md`
  에 확정되어 있다. 이미 커밋된 migration의 후속 수정은 additive migration이어야 한다.
