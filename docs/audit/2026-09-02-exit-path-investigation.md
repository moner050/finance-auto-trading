# 청산·포지션 관리 경로 조사

- **일자**: 2026-09-02
- **발단**: [감사 F6](2026-09-02-shadow-conformance-findings.md) — `exit_before_blocking_big_trade` 미구현으로 기록했던 항목
- **계획서**: [수정 계획서](2026-09-02-remediation-plan.md) §7이 "청산 경로 조사 선행"으로 남긴 항목

---

## 0. 감사 F6이 틀렸다

감사에 이렇게 적었다: "전체 소스에서 이 규칙에 해당하는 구현이 검색되지 않는다."

**틀렸다.** 구현되어 있다. 내가 `exit_before_blocking_big_trade`와 `EXIT_BEFORE`로 검색했는데, 코드의 이름은 다르다:

`src/autotrader/operations/david_v6_position.py`

```python
class V6PositionActionKind(StrEnum):
    ...
    EXIT_FULL_BLOCKING_BIG_TRADE = "EXIT_FULL_BLOCKING_BIG_TRADE"
```

```python
if facts.blocking_big_trade:
    return (_full_exit(position, V6PositionActionKind.EXIT_FULL_BLOCKING_BIG_TRADE, False),)
```

**그런데 조사 결과 문제는 F6보다 훨씬 크다.**

---

## 1. 포지션 관리 모듈 전체가 도달 불가다

`manage_v6_position`의 호출자를 전부 찾은 결과:

```
tests/property/test_david_v6_position_invariants.py
tests/unit/operations/test_david_v6_position.py
```

**프로덕션 호출자가 없다.** `src/` 어디에서도 부르지 않는다.

이 모듈이 구현하고 있는 것 전부가 실행되지 않는다:

| 동작 | 원문 근거 |
|---|---|
| `ACTIVATE_INITIAL_STOP` | 초기 구조적 손절 |
| `MOVE_STOP_TO_BREAK_EVEN` | "이미 100~200 정도의 이익이 있을 때… 포지션을 보호합니다" |
| `ADD_AND_MOVE_STOP` | "30포인트 유리해지면 계약을 하나 더 얹고 손절은 가중평균 BE로" |
| `EXIT_FULL_FIB_66` | "목표는 다우 이론의 2/3인 66%다" |
| `EXIT_FULL_BLOCKING_BIG_TRADE` | **"앞을 막는 대형 체결과는 싸우지 않고 나온다"** ← F6 |
| `EXIT_FULL_METODO_CROSS_DOWN` | Método 이탈 청산 |
| `EMERGENCY_EXIT_FULL` | 보호 실패 시 |
| `RECORD_FIB_25` / `RECORD_FIB_50_RESEARCH` | 텔레메트리 |
| `OBSERVE_PARTIAL_1_2R` / `1_5R` | 부분 청산 관측 |

F7(캘린더), F3(ATH 플래그)과 같은 형태다 — **작성됐고, 테스트됐고, 아무도 부르지 않는다.** 다만 규모가 다르다. 저 둘은 입력 하나였고 이것은 전략의 **청산 측면 전체**다.

---

## 2. 루프가 실제로 하는 일

`loop.py:run_pass`:

```
리스 확보
  → settlement.settle       정산
  → reconciliation.reconcile 브로커와 대조
  → protection.unprotected   보호 없는 포지션이 있으면 중단
  → source.context_for       새 진입 평가
  → run_tick
```

**보유 포지션을 관리하는 단계가 없다.** 매 패스는 "새로 들어갈까"만 묻는다.

`protection.unprotected`는 청산이 아니다. 보호 없는 포지션이 있으면 **새 진입을 막을 뿐** 그 포지션에 대해 아무것도 하지 않는다.

---

## 3. 실제로 존재하는 유일한 청산 경로

`composition.py`의 체결 수신 훅 하나뿐이다.

```python
# 진입 체결이 들어오면 구조적 손절을 만들어 즉시 전송한다
intent_type=IntentType.PROTECTIVE,
reason_code=STRUCTURAL_STOP,
order_style=OrderStyle.MARKET,
terms=OrderTerms(trigger_price=plan.structural_stop, ...)
```

즉 **진입 체결 → 초기 손절 1개 생성·전송**. 여기서 끝이다.

포지션은 벌거벗은 채로 남지 않는다. 그러나 그 손절은:

- 브레이크이븐으로 **움직이지 않는다**
- 목표(fib 66)에 닿아도 **익절하지 않는다**
- 대형 체결이 앞을 막아도 **나오지 않는다**
- 가산(add) 규칙이 **작동하지 않는다**

**초기 손절에 걸릴 때까지 들고 있는 것이 유일한 청산 방식이다.**

---

## 4. 세션 마감 규칙도 진입만 막는다

`sessions.py`는 `must_be_flat`, `reduce_only`, `entry_cutoff_at`, `flat_at`을 계산한다. 그런데 소비되는 방식은 **블로커**뿐이다:

```python
if session_open and decision >= entry_cutoff:
    blockers.append("ENTRY_CUTOFF_REACHED")
if must_be_flat:
    blockers.append("FLAT_CUTOFF_REACHED")
```

블로커는 `engine.py:156`을 통해 **새 결정을 REJECT로** 만든다. **보유 포지션을 청산하지 않는다.**

원문은 세션 마감 10분 전 **평평해야 한다**고 요구하고 오버나이트를 금지한다. 현재 구현은 "그 시각 이후 새로 들어가지 않는다"까지만 한다. **이미 들고 있으면 그대로 밤을 넘긴다.**

`V6PositionFacts.reduce_only` 필드가 존재하지만, 그 모듈이 호출되지 않으므로 함께 죽어 있다.

---

## 5. 지금 위험하지 않은 이유, 그리고 언제 위험해지는지

**섀도는 주문 포트가 없다.** 포지션을 열지 않으므로 청산할 것도 없다. 오늘 이 공백은 무해하다.

**페이퍼 승격 시점에 실재하는 공백이 된다.** 페이퍼는 실제로 포지션을 열고, 그 포지션은 초기 손절 외에 아무 관리도 받지 못한다. 목표에 닿아도 안 판다. 세션이 끝나도 안 닫는다.

**라이브에서는 돈이다.**

---

## 6. 배선에 필요한 것

`V6PositionFacts`가 요구하는 입력과 현재 조달 가능성:

| 필드 | 조달 |
|---|---|
| `current_price`, `atr_5m`, `tick_size` | ✅ 이미 있음 |
| `actual_entry_fee_per_unit`, `taker_exit_fee_per_unit` | ✅ `FeeSchedule` |
| `q95_adverse_stop_slippage`, `slippage_sample_sufficient` | ✅ `stop_slippage_from_bars` |
| `fib_25_price`, `fib_50_price`, `fib_66_price` | ✅ HLIT 레벨 (`hlit.py`) |
| `blocking_big_trade` | ✅ 주문 흐름 증거에 이미 있음 (결정 19/23건에서 발동 중) |
| `metodo_exit_signal` | ✅ `metodo.py` |
| `protection_failed` | ✅ `protection` 저장소 |

**새로 측정해야 할 입력이 없다.** 전부 이미 계산되고 있거나 조달 가능하다.

`V6ManagedPosition`이 요구하는 상태(평균 진입가, 잔량, 활성 손절, 가산 횟수, BE 활성 여부 등)는 **어딘가에 저장되어야 한다.** 현재 그 상태를 들고 있는 테이블이 있는지는 이 조사에서 확인하지 않았다 — 배선 착수 전 확인이 필요하다.

---

## 7. 남는 질문 (배선 전에 답해야 함)

1. **루프의 어느 단계에 들어가는가.** `protection.unprotected` 다음, `source.context_for` 앞이 자연스럽다 — 이미 들고 있는 것을 정리한 뒤 새로 들어갈지 묻는 순서다.
2. **`V6ManagedPosition` 상태를 어디에 저장하는가.** 브로커가 알려주는 것(수량, 평균가)과 우리만 아는 것(가산 횟수, BE 활성, 원래 승인 위험)이 섞여 있다.
3. **섀도에서도 돌릴 것인가.** 섀도는 포지션이 없으므로 실행할 대상이 없다. 그러나 배선을 페이퍼에서 처음 켜면 그때 처음 실행되는 코드가 된다.
4. **`manage_v6_position`이 돌려주는 행동을 누가 주문으로 바꾸는가.** 현재 `_create_protective_order`가 체결 훅에만 있다.

---

## 8. 조사 이후 — 무엇이 배선됐나

이 조사에 이어 배선이 진행됐다. §7의 네 질문에 대한 답:

1. **어느 단계인가** — 루프가 아니라 **틱 안**, 증거 조립 직후·진입 평가 앞. 매니저와 진입이 같은 조립을 보게 하기 위해서다. 행동이 있었으면 그 패스는 끝난다.
2. **상태를 어디에 저장하나** — 거의 전부 기존 테이블에서 파생된다. 저장이 필요했던 건 텔레메트리 마크 4종뿐이고 `strategy_david_v6_position_mark`에 들어간다.
3. **섀도에서도 도나** — 돈다. 다만 섀도는 포지션을 열지 않으므로 대상이 없다. 거부 싱크가 붙어 있다.
4. **행동을 누가 주문으로 바꾸나** — `MySqlPositionActions`. 전량 청산 4종만 만들고 나머지는 **소리 내어 거부**한다.

배선 중 두 결함이 잡혔다: 랏을 가산으로 센 것(부분체결 시 관리 불가), 명시적 0이 전량 청산이 된 것. 둘 다 감사 문서 F6에 기록.

이후 F10(세션 마감 청산 `8377a2a`), halt(`954939e`), 페이퍼 브로커의 취소·교체(`e50e877`), 손절 이동(`226e39a`), 가산(`4e6b6c8`)이 차례로 배선됐다. **행동 9종이 전부 주문 또는 기록으로 이어진다.**

조사 §3이 "실제로 존재하는 유일한 청산 경로는 진입 체결 시의 초기 손절 하나"라고 적었던 상태는 해소됐다. 다만 섀도는 포지션을 열지 않으므로 **아직 한 번도 실행되지 않았다.**

---

## 9. 감사 문서에 반영할 것

F6의 서술을 다음으로 대체해야 한다:

> ~~`exit_before_blocking_big_trade` 미구현~~
> → **포지션 관리 모듈 전체가 프로덕션에서 호출되지 않음.** `EXIT_FULL_BLOCKING_BIG_TRADE`를 포함한 9개 행동이 모두 도달 불가. 유일한 청산은 진입 체결 시 생성되는 초기 손절 하나이며, 그것은 움직이지도 익절하지도 않는다. 세션 마감 flat 요구도 진입 차단에만 쓰인다.

심각도는 **중간 → 높음**으로 올린다. 섀도에서 무해하다는 판단은 유지하되, **페이퍼 승격의 선행 조건**이다.

반영 완료. 현재 상태는 감사 문서 F6 항목 참조.
